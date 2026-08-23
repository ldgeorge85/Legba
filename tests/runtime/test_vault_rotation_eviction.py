# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RUST-5 — LLM handler cache eviction on ``vault.secret.>``.

The defect: rotating a secret in the credential vault
(``CredentialVault.store_secret``) emitted no event, so a runtime process's
cached LLM handler — which resolved-and-cached the OLD plaintext at build
time (``dapr_host._llm_handler_cache`` / ``bearing_gate._GATE_CLIENT_CACHE``)
— kept serving the stale credential until a container recreate. Same failure
shape as S-2's stack-component PUT, different event family.

What is asserted here:

  * a cached handler + a vault rotation event → the NEXT acquire sees the
    NEW credential (mirrors S-2's ``test_component_change_event_makes_the_
    next_build_see_new_config``, over ``evict_all`` instead of a targeted
    ``evict``);
  * NO event → the cache still hits (the caching win survives the fix);
  * the sweep covers EVERY registered cache (host + bearing gate), same as
    the stack informer's sweep;
  * a raising ``evict_all`` never escapes the message loop.

No infrastructure: the informer's message handling is exercised directly
with a fake JetStream message, mirroring
``tests/runtime/test_llm_handler_cache_invalidation.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.runtime.llm_handler_cache import (
    clear_handler_cache_registry,
    evict_all_llm_handlers,
    register_handler_cache,
    registered_cache_labels,
)
from legba.runtime.nats_informer import (
    DEFAULT_VAULT_CONSUMER_DURABLE_PREFIX,
    VAULT_SUBJECT_FILTER,
    NatsVaultRotationInformer,
)


class _FakeMsg:
    """The two attributes the informer touches on a JetStream message."""

    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.acked = 0

    async def ack(self) -> None:
        self.acked += 1


def _informer(evict_all) -> NatsVaultRotationInformer:
    """An informer with no NATS store — ``_handle_message`` never touches it."""
    return NatsVaultRotationInformer(None, evict_all=evict_all)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test owns the sweep set — see the S-2 test file for why."""
    saved = registered_cache_labels()
    clear_handler_cache_registry()
    yield
    clear_handler_cache_registry()
    assert saved is not None  # the import-time registrations are re-made lazily


def test_vault_informer_uses_its_own_durable_prefix():
    """A wedged descriptor/stack consumer must not stall vault eviction."""
    inf = _informer(lambda: 0)
    assert inf.consumer_name.startswith(DEFAULT_VAULT_CONSUMER_DURABLE_PREFIX)
    assert inf._subject_filter == VAULT_SUBJECT_FILTER


# ---------------------------------------------------------------------------
# The behaviour the operator sees: next acquire sees the ROTATED credential
# ---------------------------------------------------------------------------


class _FakeFactory:
    """A stand-in for ``dapr_host._llm_handler_factory``: same
    cache-then-build shape, over a mutable 'live secret' the test can
    rotate."""

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
        # A real build resolves the vault secret and bakes the plaintext into
        # the handler instance — snapshot the 'live' value the same way.
        handler = dict(self.live[component_id])
        self.cache[component_id] = handler
        return handler


async def test_rotation_event_makes_the_next_acquire_see_the_new_secret():
    live = {"llm.primary.openai_compat": {"api_key": "sk-old"}}
    factory = _FakeFactory(live)
    inf = _informer(evict_all_llm_handlers)

    first = await factory.get("llm.primary.openai_compat")
    assert first["api_key"] == "sk-old"
    assert factory.builds == 1

    # The operator rotates the secret; the vault publishes the rotation.
    live["llm.primary.openai_compat"] = {"api_key": "sk-new"}
    msg = _FakeMsg("vault.secret.rotated.llm.primary.openai_compat.api_key")
    await inf._handle_message(msg)

    after = await factory.get("llm.primary.openai_compat")
    assert after["api_key"] == "sk-new", (
        "the rotation must take effect on the next acquire, not the next "
        "container recreate"
    )
    assert factory.builds == 2
    assert msg.acked == 1
    assert inf.stats.evicted == 1


async def test_no_event_still_hits_the_cache():
    """The caching win is the whole reason the cache exists."""
    live = {"llm.primary.openai_compat": {"api_key": "sk-old"}}
    factory = _FakeFactory(live)

    for _ in range(5):
        await factory.get("llm.primary.openai_compat")
    assert factory.builds == 1


async def test_eviction_sweeps_every_registered_cache():
    """A rotation cannot be targeted to one component_id (no secret_id ->
    component_id reverse lookup exists), so it must sweep every cache the
    same way ``evict_all_llm_handlers`` already does for the ops escape
    hatch — the bearing gate's separate cache is covered too."""
    host_cache: dict[str, Any] = {"llm.verify.slm_8b": object()}
    gate_cache: dict[str, Any] = {"llm.verify.slm_8b": object()}
    register_handler_cache("dapr_host.llm_handler", host_cache)
    register_handler_cache("claim_watch.bearing_gate", gate_cache)
    inf = _informer(evict_all_llm_handlers)

    msg = _FakeMsg("vault.secret.rotated.pg.cluster_main.password")
    await inf._handle_message(msg)

    assert host_cache == {}
    assert gate_cache == {}
    assert inf.stats.evicted == 2
    assert msg.acked == 1


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------


async def test_a_raising_evict_all_never_escapes_the_message_loop():
    def _boom() -> int:
        raise RuntimeError("registry unreachable")

    inf = _informer(_boom)
    msg = _FakeMsg("vault.secret.rotated.llm.primary.openai_compat.api_key")
    await inf._handle_message(msg)  # must not raise
    assert inf.stats.parse_errors == 1
    assert msg.acked == 1


async def test_any_subject_shape_evicts_all_no_parsing_needed():
    """Unlike the stack informer, no subject grammar is parsed — the
    informer reacts to the FACT a message arrived, not its contents."""
    register_handler_cache("only", {"x": object()})
    inf = _informer(evict_all_llm_handlers)
    await inf._handle_message(_FakeMsg("vault.secret.rotated"))
    assert inf.stats.evicted == 1
