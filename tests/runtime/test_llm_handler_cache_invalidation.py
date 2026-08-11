# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-2 — LLM handler cache invalidation on ``stack.component.>``.

The defect these lock down: ``dapr_host._llm_handler_cache`` keyed a built
handler by ``component_id`` alone, with no version and no invalidation, so the
operator's 2026-08-01 timeout PUT (60→240s, mid-incident) stayed inert for 3.5 h
until a container recreate. The registry was already publishing the change on
``stack.component.>``; nothing consumed it.

What is asserted here:

  * a cached handler + a stack-component change event → the NEXT build sees the
    NEW config (the actual operator-visible behaviour, not just a dict delete);
  * NO event → the cache still hits (the caching win survives the fix — this is
    the regression that would make the fix a performance bug);
  * ``health_changed`` does NOT evict (a flapping endpoint must not churn the
    cache — it carries a liveness verdict, never a body change);
  * the eviction sweeps EVERY registered cache, so the bearing gate's separate
    process-lifetime cache is covered too;
  * a malformed subject is counted + acked, never raised into the fetch loop
    (a dispatch that raises would silently stop ALL propagation).

No infrastructure: the informer's message handling is exercised directly with a
fake JetStream message, so this runs in the default sweep. The NATS wire itself
is covered by the descriptor informer's integration test.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.runtime.llm_handler_cache import (
    clear_handler_cache_registry,
    evict_all_llm_handlers,
    evict_llm_handler,
    register_handler_cache,
    registered_cache_labels,
    unregister_handler_cache,
)
from legba.runtime.nats_informer import (
    DEFAULT_STACK_CONSUMER_DURABLE_PREFIX,
    STACK_SUBJECT_FILTER,
    NatsStackComponentInformer,
    parse_stack_subject,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMsg:
    """The two attributes the informer touches on a JetStream message."""

    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.acked = 0

    async def ack(self) -> None:
        self.acked += 1


def _informer(evict) -> NatsStackComponentInformer:
    """An informer with no NATS store — ``_handle_message`` never touches it."""
    return NatsStackComponentInformer(None, evict=evict)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test owns the sweep set. The registry is module-level (it must be —
    the informer evicts through it from outside any runtime closure), so leaking
    a test's cache into the next one would make failures order-dependent."""
    saved = registered_cache_labels()
    clear_handler_cache_registry()
    yield
    clear_handler_cache_registry()
    assert saved is not None  # the import-time registrations are re-made lazily


# ---------------------------------------------------------------------------
# Subject grammar
# ---------------------------------------------------------------------------


def test_parse_stack_subject_rejoins_the_dotted_component_id():
    """Component ids are dotted, which is exactly why a naive split breaks."""
    assert parse_stack_subject(
        "stack.component.updated.llm_provider.llm.primary.openai_compat"
    ) == ("updated", "llm.primary.openai_compat")
    assert parse_stack_subject(
        "stack.component.registered.postgres.pg.cluster_main"
    ) == ("registered", "pg.cluster_main")


def test_parse_stack_subject_rejects_other_families():
    assert parse_stack_subject("descriptor.updated.analyst.country_assessor") is None
    assert parse_stack_subject("stack.component.updated.llm_provider") is None
    assert parse_stack_subject("") is None


def test_stack_informer_uses_its_own_durable_prefix():
    """A wedged descriptor consumer must not stall stack eviction, and vice
    versa — different streams, different durables."""
    inf = _informer(lambda cid: 0)
    assert inf.consumer_name.startswith(DEFAULT_STACK_CONSUMER_DURABLE_PREFIX)
    assert inf._subject_filter == STACK_SUBJECT_FILTER


# ---------------------------------------------------------------------------
# The behaviour the operator sees: next build reads the LIVE component
# ---------------------------------------------------------------------------


class _FakeFactory:
    """A stand-in for ``dapr_host._llm_handler_factory``: same cache-then-build
    shape, over a mutable 'live component' the test can PUT into."""

    def __init__(self, live: dict[str, Any]) -> None:
        self.cache: dict[str, Any] = {}
        self.live = live
        self.builds = 0
        register_handler_cache("test.factory", self.cache)

    async def get(self, component_id: str) -> Any:
        existing = self.cache.get(component_id)
        if existing is not None:
            return existing
        self.builds += 1
        handler = dict(self.live[component_id])  # snapshot, as a real build is
        self.cache[component_id] = handler
        return handler


async def test_component_change_event_makes_the_next_build_see_new_config():
    live = {"llm.primary.openai_compat": {"timeout_seconds": 60}}
    factory = _FakeFactory(live)
    inf = _informer(evict_llm_handler)

    first = await factory.get("llm.primary.openai_compat")
    assert first["timeout_seconds"] == 60
    assert factory.builds == 1

    # The operator PUTs the new timeout; the registry publishes the change.
    live["llm.primary.openai_compat"] = {"timeout_seconds": 240}
    msg = _FakeMsg(
        "stack.component.updated.llm_provider.llm.primary.openai_compat"
    )
    await inf._handle_message(msg)

    after = await factory.get("llm.primary.openai_compat")
    assert after["timeout_seconds"] == 240, (
        "the PUT must take effect on the next build, not the next recreate"
    )
    assert factory.builds == 2
    assert msg.acked == 1
    assert inf.stats.evicted == 1
    assert inf.stats.parse_errors == 0


async def test_no_event_still_hits_the_cache():
    """The caching win is the whole reason the cache exists — a fix that
    rebuilt on every call would be a worse bug than the one it replaces."""
    live = {"llm.primary.openai_compat": {"timeout_seconds": 60}}
    factory = _FakeFactory(live)

    for _ in range(5):
        await factory.get("llm.primary.openai_compat")
    assert factory.builds == 1


async def test_health_changed_does_not_evict():
    """A latching health prober publishes on every transition. Evicting there
    would let a flapping endpoint rebuild the handler (and re-decrypt vault
    credentials) on a signal that carries no config delta at all."""
    live = {"llm.verify.slm_8b": {"timeout_seconds": 30}}
    factory = _FakeFactory(live)
    inf = _informer(evict_llm_handler)

    await factory.get("llm.verify.slm_8b")
    live["llm.verify.slm_8b"] = {"timeout_seconds": 999}

    msg = _FakeMsg("stack.component.health_changed.llm_provider.llm.verify.slm_8b")
    await inf._handle_message(msg)

    still = await factory.get("llm.verify.slm_8b")
    assert still["timeout_seconds"] == 30
    assert factory.builds == 1
    assert inf.stats.evicted == 0
    assert inf.stats.parse_errors == 0, "handled, just deliberately not evicting"
    assert msg.acked == 1


async def test_retire_and_pause_do_evict():
    """A retired/paused component must stop being served from cache — those
    actions change whether the component should answer at all."""
    for action in ("retired", "paused", "configured", "rolled_back"):
        live = {"llm.judge.cerebras_gemma4_31b.openai_compat": {"v": 1}}
        factory = _FakeFactory(live)
        inf = _informer(evict_llm_handler)
        await factory.get("llm.judge.cerebras_gemma4_31b.openai_compat")
        await inf._handle_message(
            _FakeMsg(
                f"stack.component.{action}.llm_provider."
                "llm.judge.cerebras_gemma4_31b.openai_compat"
            )
        )
        assert inf.stats.evicted == 1, f"{action} should evict"
        clear_handler_cache_registry()


# ---------------------------------------------------------------------------
# The sweep covers every registered cache (the bearing gate's included)
# ---------------------------------------------------------------------------


def test_eviction_sweeps_every_registered_cache():
    host_cache: dict[str, Any] = {"llm.verify.slm_8b": object()}
    gate_cache: dict[str, Any] = {"llm.verify.slm_8b": object()}
    register_handler_cache("dapr_host.llm_handler", host_cache)
    register_handler_cache("claim_watch.bearing_gate", gate_cache)

    assert evict_llm_handler("llm.verify.slm_8b") == 2
    assert host_cache == {}
    assert gate_cache == {}


def test_bearing_gate_registers_its_cache_at_import():
    """The gate holds a SECOND process-lifetime handler cache with the same
    defect; importing it must put it in the sweep set."""
    clear_handler_cache_registry()
    import importlib

    module = importlib.import_module(
        "legba.data.analysts.deterministic_handlers.bearing_gate"
    )
    importlib.reload(module)
    assert "claim_watch.bearing_gate" in registered_cache_labels()


def test_register_is_idempotent_by_identity_and_unregister_removes():
    cache: dict[str, Any] = {"x": object()}
    register_handler_cache("a", cache)
    register_handler_cache("a", cache)
    register_handler_cache("b", cache)
    assert registered_cache_labels() == ("a",)
    assert evict_llm_handler("x") == 1

    unregister_handler_cache(cache)
    assert registered_cache_labels() == ()
    unregister_handler_cache(cache)  # idempotent


def test_evict_all_clears_everything():
    cache: dict[str, Any] = {"a": object(), "b": object()}
    register_handler_cache("only", cache)
    assert evict_all_llm_handlers() == 2
    assert cache == {}
    assert evict_all_llm_handlers() == 0


def test_evicting_an_uncached_component_is_a_quiet_zero():
    register_handler_cache("only", {})
    assert evict_llm_handler("llm.never.built") == 0
    assert evict_llm_handler("") == 0


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------


async def test_malformed_subject_is_counted_and_acked():
    inf = _informer(evict_llm_handler)
    msg = _FakeMsg("stack.component.updated")
    await inf._handle_message(msg)
    assert inf.stats.parse_errors == 1
    assert inf.stats.evicted == 0
    assert msg.acked == 1


async def test_a_raising_evict_never_escapes_the_message_loop():
    """One bad message must not kill the fetch loop — that would silently stop
    every future eviction, which is the failure class this whole item exists to
    remove."""

    def _boom(component_id: str) -> int:
        raise RuntimeError("registry unreachable")

    inf = _informer(_boom)
    msg = _FakeMsg("stack.component.updated.llm_provider.llm.primary.openai_compat")
    await inf._handle_message(msg)  # must not raise
    assert inf.stats.parse_errors == 1
    assert msg.acked == 1
