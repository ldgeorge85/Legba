# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SOURCE-discovery flavor has an actor-plane entry.

``discovery.materializer.run_source_discovery_cycle`` — the function that
resolves a source-discovery descriptor's declared deps, builds the handler,
drains ``discover(ctx)`` and reconciles into ``source_descriptors`` — had NO
caller anywhere in ``src/`` outside its own tests (W1-E). The kind
(``query_source_discovery``), the materialiser, validate-before-register and
selector auto-wire were all built and tested; the TARGET flavor was driven from
``dapr_actors.TargetActor._run_discovery_cycle`` and the source flavor was
driven by nothing. A source-discovery descriptor therefore could not fire even
at ``state: active``.

These tests drive the REAL dispatch: the shipped template descriptor parsed by
the real schema, the real :class:`SourceCore` / :class:`SourceActor`, the real
materialiser entry, the real handler and the real reconcile loop. Only the
per-candidate DB+network tail is stubbed, and only in the test that needs
candidates to exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.discovery import source_materializer
from legba.data.schemas.source import SourceDescriptor
from legba.runtime import source_actor
from legba.runtime.source_actor import SourceCore, SourceDeps


pytestmark = [pytest.mark.integration]


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "descriptors"
    / "source_query_source_discovery_template.yaml"
)


def _template_body() -> dict[str, Any]:
    return yaml.safe_load(_TEMPLATE_PATH.read_text())


def _descriptor(body: dict[str, Any] | None = None) -> SourceDescriptor:
    """The SHIPPED template, parsed by the real SourceDescriptor schema."""
    return SourceDescriptor.model_validate(body or _template_body(), strict=False)


# ---------------------------------------------------------------------------
# Stub I/O deps — the descriptor and every code path below it are real
# ---------------------------------------------------------------------------


class _Conn:
    """Fails loudly if the cycle touches the DB.

    With an empty roster ``reconcile_discovered_sources`` never enters its
    per-candidate loop, so a query here means the dispatch did something other
    than the source-discovery cycle.
    """

    async def fetch(self, *_a: Any, **_kw: Any):  # pragma: no cover — guard
        raise AssertionError("empty-roster discovery cycle must not query")

    async def fetchrow(self, *_a: Any, **_kw: Any):  # pragma: no cover — guard
        raise AssertionError("empty-roster discovery cycle must not query")


class _PoolCtx:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *_exc):
        return False


class _Pool:
    def acquire(self):
        return _PoolCtx()


class _StandardDepsStub:
    """Only the attributes the discovery cycle actually reads."""

    def __init__(self) -> None:
        self.pg_pool = _Pool()
        self.nats_publish = None
        self.secrets_resolve = None


def _core(descriptor: SourceDescriptor) -> SourceCore:
    sd = SourceDeps(descriptor=descriptor, deps=_StandardDepsStub())
    return SourceCore(f"source::{descriptor.identity.id}::" + "0" * 16, sd)


class _ActorId:
    def __init__(self, actor_id: str) -> None:
        self.id = actor_id


class _FakeStateManager:
    """In-memory stand-in for Dapr's ActorStateManager."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def try_get_state(self, name: str):
        if name in self._store:
            return True, self._store[name]
        return False, None

    async def set_state(self, name: str, value: Any) -> None:
        self._store[name] = value

    async def save_state(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. The dispatch exists and reaches the materialiser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_discovery_descriptor_has_an_actor_entry() -> None:
    """The shipped template runs a full cycle through the real materialiser.

    ``candidates_in`` coming back at all is the proof of wiring: that number is
    produced by :class:`ReconcileSourceResult` at the far end of
    ``run_source_discovery_cycle`` — deps resolution, handler construction,
    ``discover(ctx)`` drain and reconcile all ran to get here. The shipped
    template's roster is deliberately empty (``inline:[]``), so zero is the
    right count.
    """
    descriptor = _descriptor()
    assert descriptor.discovery is not None, "template lost its discovery block"
    assert descriptor.discovery.kind == "query_source_discovery"

    result = await _core(descriptor).run_discovery_cycle()

    assert result["discovery_cycle"] is True
    assert result["discovery_kind"] == "query_source_discovery"
    assert result["candidates_in"] == 0
    assert result["outcome"] == "noop"


@pytest.mark.asyncio
async def test_cycle_drains_the_real_handler_and_reconciles(monkeypatch) -> None:
    """A populated roster flows: real handler -> real reconcile loop.

    Only ``materialize_discovered_source`` (the per-candidate probe + DB
    write) is stubbed — everything above it, including the relabel-carrying
    descriptor and the kind's own spec parsing, is the production path.
    """
    body = _template_body()
    body["discovery"]["config"]["list_source"] = (
        'inline:[{"url": "https://example.invalid/a.xml", "feed_title": "A"}, '
        '{"url": "https://example.invalid/b.xml", "feed_title": "B"}]'
    )
    descriptor = _descriptor(body)

    seen: list[str] = []

    async def _fake_materialize(_conn, candidate, *_a: Any, **_kw: Any):
        seen.append(candidate.natural_key)
        return source_materializer.MaterializeSourceOutcome(
            natural_key=candidate.natural_key,
            source_id=f"src_{len(seen)}",
            version="0" * 16,
        )

    monkeypatch.setattr(
        source_materializer, "materialize_discovered_source", _fake_materialize,
    )

    result = await _core(descriptor).run_discovery_cycle()

    assert result["candidates_in"] == 2
    assert result["registered"] == 2
    assert result["outcome"] == "success"
    assert len(seen) == 2, "both specs reached the reconcile loop"


@pytest.mark.asyncio
async def test_source_actor_run_routes_a_discovery_descriptor(monkeypatch) -> None:
    """``SourceActor.run`` — the method the poll reminder and any force-run
    call — routes a discovery descriptor to the cycle, NOT to the pull path.

    This is the binding that was missing. The discriminator mirrors
    ``TargetActor.run``'s (`descriptor.discovery is not None`) and must sit
    BEFORE the acquisition check: the shipped template declares
    ``acquisition: poll`` with no cadence, so the old ordering would have sent
    it to ``pull_once`` against a placeholder URL.
    """
    descriptor = _descriptor()
    core = _core(descriptor)

    async def _no_pull():  # pragma: no cover — guard
        raise AssertionError("a discovery template must never pull")

    core.pull_once = _no_pull  # type: ignore[assignment]

    actor = object.__new__(source_actor.SourceActor)
    actor.id = _ActorId(f"source::{descriptor.identity.id}::" + "0" * 16)
    actor._state_manager = _FakeStateManager()
    await actor._set_record({"lifecycle": "active"})

    async def _core_resolver():
        return core

    actor._core = _core_resolver  # type: ignore[assignment]

    result = await actor.run({"trigger_kind": "reminder"})

    assert result["discovery_cycle"] is True
    assert result["candidates_in"] == 0
    # The cycle outcome is recorded on the actor record like any other run.
    rec = await actor._get_record()
    assert rec["last_outcome"] == "noop"
    assert rec["last_error"] is None
    assert rec["last_run_at"]


@pytest.mark.asyncio
async def test_a_broken_discovery_config_fails_loud() -> None:
    """An unknown discovery kind raises out of the cycle rather than no-opping.

    The actor's run() converts this into a recorded ``hard_fail``; what must
    not happen is a silent success on a descriptor that never ran.
    """
    body = _template_body()
    body["discovery"]["kind"] = "no_such_discovery_kind"
    # The schema does not constrain the kind string, so this reaches the
    # handler factory exactly as a typo'd descriptor would.
    descriptor = _descriptor(body)

    with pytest.raises(ValueError, match="unknown source-discovery kind"):
        await _core(descriptor).run_discovery_cycle()
