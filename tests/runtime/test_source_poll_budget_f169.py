# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""a per-source poll budget for the sources whose items cost NER.

GDELT and ISW polled under the generic 30 s budget while enrichment costs
~7-15 s an item, so each poll wrote ~2-4 items and GDELT (whose handler commits
its file cursor before the first yield) dropped the rest of every file. The
actor already resolved ``poll_budget_seconds`` per source (descriptor config >
handler advertisement > 30 s, clamped to 240 s); what was missing was the knob
on the two handlers' ``extra='forbid'`` config schemas, and the value on the two
descriptors. This file locks:

  * the knob is declared on ``gdelt_files`` and ``json_api`` (unset by default,
    refused above the actor's ceiling) and the generic default is still 30 s;
  * ``source.gdelt.files`` and ``source.isw.assessments`` carry 180 s and parse
    through the runtime schema + the production handler build; a descriptor
    without the knob still resolves 30 s;
  * the actor's walk honours the resolved budget;
  * a poll CANCELLED mid-walk (a roll's bounded drain) is recorded capped, so a
    newest-first dated walk holds its cursor instead of advancing past entries
    it never reached.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pydantic
import pytest
import yaml

import legba.runtime.source_actor as sa
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources._contract import InMemoryStateStore, Signal, SourceContext
from legba.data.sources.gdelt_files import GDELTFilesConfig
from legba.data.sources.json_api import JsonApiConfig
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import (
    SourceCore,
    SourceDeps,
    _RawConfig,
    resolve_poll_bounds,
)
from legba.runtime.source_factory import build_source_handler

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"
_RAISED = {
    "source_gdelt_files.yaml": "source.gdelt.files",
    "source_isw.yaml": "source.isw.assessments",
}


def _load(name: str) -> SourceDescriptor:
    body = yaml.safe_load((_DESCRIPTORS / name).read_text())
    return SourceDescriptor.model_validate(body, strict=False)


# --- 1. the knob is declared; the defaults are unchanged --------------------


def test_the_generic_budget_and_ceiling_are_unchanged() -> None:
    assert sa._POLL_BUDGET_S == 30.0
    assert sa._MAX_POLL_BUDGET_S == 240.0
    assert sa._POLL_BUDGET_CONFIG_KEY == "poll_budget_seconds"


@pytest.mark.parametrize("model, required", [
    (GDELTFilesConfig, {}),
    (JsonApiConfig, {"url_template": "https://example.invalid/feed"}),
])
def test_the_knob_is_declared_unset_by_default_and_bounded(model, required) -> None:
    assert model(**required).poll_budget_seconds is None
    assert model(**required, poll_budget_seconds=180).poll_budget_seconds == 180.0
    for bad in (0, -1, 241):
        with pytest.raises(pydantic.ValidationError):
            model(**required, poll_budget_seconds=bad)


# --- 2. the two descriptors carry it ----------------------------------------


@pytest.mark.parametrize("name", sorted(_RAISED))
def test_the_two_descriptors_carry_180_through_the_production_build(name) -> None:
    sd = _load(name)
    assert sd.identity.id == _RAISED[name]
    handler = build_source_handler(sd.identity.kind, sd.config or {})
    assert resolve_poll_bounds(sd, handler) == (180.0, 100)


def test_a_descriptor_without_the_knob_keeps_the_generic_budget() -> None:
    sd = _load("source_gdelt_bigquery.yaml")  # same family, not raised (draft)
    assert "poll_budget_seconds" not in (sd.config or {})
    budget, _ = resolve_poll_bounds(sd, SimpleNamespace())
    assert budget == 30.0


# --- 3. the actor honours it ------------------------------------------------


class _FakeConn:
    async def fetchrow(self, query, *args):
        return {"id": args[0]}

    async def fetchval(self, *a, **k):  # pragma: no cover
        return None

    async def execute(self, *a, **k):  # pragma: no cover
        return None


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


def _descriptor(source_id: str, **config) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name="f169", kind="rss", schema_uri="legba/source/3.0.0",
            version="b" * 16, owner="test:f169", created=datetime.now(tz=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme", languages=["en"]),
        acquisition="poll",
        config={"url": "http://unused", **config},
        cadence=CadenceBlock(schedule=Cron(raw="*/15 * * * *")),
        pipeline=SourcePipeline(media="reference"),
    )


def _entry(ts: datetime, title: str) -> Signal:
    return Signal(
        source_id="s", modality="text",
        payload={"title": title, "external_id": title, "_published_at_dt": ts},
        content_hash=uuid4().hex,
    )


def _core(sd: SourceDescriptor, handler, store: InMemoryStateStore) -> SourceCore:
    core = SourceCore(
        f"source::{sd.identity.id}::{uuid4().hex[:6]}",
        SourceDeps(descriptor=sd, deps=StandardDeps(pg_pool=_FakePool(), nats_publish=None)),
    )
    core.sd.handler = handler

    def _make_context():
        return SourceContext(
            target_id=sd.identity.id, target_version=sd.identity.version,
            source_id=sd.identity.id, config=_RawConfig(**(sd.config or {})),
            state_store=store, scope_geo=[], scope_languages=["en"],
        )

    core._make_context = _make_context  # type: ignore[method-assign]
    return core


class _Yields:
    def __init__(self, signals, *, lookback: float | None = None, block_after: int | None = None):
        self._signals = signals
        self._block_after = block_after
        self.reached = asyncio.Event()
        if lookback is not None:
            self.cursor_lookback_seconds = lookback

    async def pull(self, ctx, since=None):
        for i, s in enumerate(self._signals):
            if self._block_after is not None and i == self._block_after:
                self.reached.set()
                await asyncio.Event().wait()  # held until the turn is cancelled
            yield s


@pytest.mark.asyncio
@pytest.mark.parametrize("config, written", [
    ({}, 3),                                                    # 30 s -> 3 items
    ({"poll_budget_seconds": {"factory_kind": "number", "raw": 180}}, 18),
])
async def test_the_walk_stops_at_the_resolved_budget(monkeypatch, config, written) -> None:
    """A fake clock charges 10 s per budget check: the walk processes as many
    entries as the RESOLVED budget allows — 3 at the generic 30 s, 18 at the
    descriptor's 180 s."""
    ticks = iter(range(0, 10_000, 10))
    monkeypatch.setattr(sa, "time", SimpleNamespace(monotonic=lambda: float(next(ticks))))
    now = datetime.now(tz=timezone.utc)
    entries = [_entry(now - timedelta(hours=2) + timedelta(minutes=i), f"e{i}") for i in range(40)]
    sd = _descriptor(f"source.test.f169_{uuid4().hex[:8]}", **config)
    result = await _core(sd, _Yields(entries), InMemoryStateStore()).pull_once()
    assert result["signals_written"] == written


# --- 4. a cancelled walk is a capped walk -----------------------------------


@pytest.mark.asyncio
async def test_a_poll_cancelled_mid_walk_holds_a_newest_first_cursor() -> None:
    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(hours=3)
    newest_first = [_entry(now - timedelta(minutes=10 * (i + 1)), f"n{i}") for i in range(5)]
    store = InMemoryStateStore()
    await store.set("cursor", {"last_pulled_at": since.isoformat()})
    handler = _Yields(newest_first, lookback=72 * 3600.0, block_after=2)
    sd = _descriptor(f"source.test.f169c_{uuid4().hex[:8]}")
    task = asyncio.create_task(_core(sd, handler, store).pull_once())
    await asyncio.wait_for(handler.reached.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cursor = await store.get("cursor")
    # Two entries were written before the cut, newest-first: had the walk been
    # treated as complete the cursor would jump to n0 (10 min ago) past the
    # three it never reached. Capped, it holds where it was.
    assert datetime.fromisoformat(cursor["last_pulled_at"]) == since
