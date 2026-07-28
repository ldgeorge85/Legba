# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P0-T2 verify-judge wiring tests (the BATCH 1 gap-closer).

The faithfulness verify pass (``legba.data.provenance.verify``) always runs its
deterministic citation-presence FLOOR; an OPTIONAL cross-family 8B judge refines
it ONLY when (a) the inline_target descriptor declares ``method.llm.verify`` AND
(b) the ``LEGBA_VERIFY_LLM_JUDGE`` flag gates the judge ON. Until this wiring
landed, NOTHING populated ``_AnalystDeps.verify_judge`` — so the live judge could
never turn on (the floor always stood).

These tests cover the two halves of the close:

  * ``_verify_llm_component_id(descriptor)`` — the helper that pulls the StackRef
    ``raw`` off ``method.llm.verify`` (present → the id; absent → None), mirroring
    ``_narrate_llm_component_id``.

  * the host resolver's resolution RULE (replicated here byte-for-byte against the
    REAL helper + the REAL verify-seam flag reader + a mocked handler factory, so
    the flag semantics are exercised, not re-implemented):
        ref present + flag ON  + factory returns a handler → that handler
        ref absent                                         → None
        ref present + flag OFF                             → None
        factory raises (flag ON, ref present)              → None (soft-fail)

  No Dapr / Postgres / registry / live LLM — the factory is an ``AsyncMock`` and
  the flag is driven via ``monkeypatch.setenv``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from legba.data.provenance.verify import _VERIFY_LLM_JUDGE_ENV, _llm_judge_enabled
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.runtime.analyst_deps_builder import (
    JUDGE_STACK_REF_ENV,
    _verify_llm_component_id,
    resolve_judge_route,
    resolve_judge_route_from_llm_block,
)

_VERSION = "0" * 64
_PRIMARY_LLM_REF = "llm.primary.openai_compat"
_VERIFY_LLM_REF = "slm.internal.llama31_8b.openai_compat"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _identity() -> AnalystIdentity:
    return AnalystIdentity(
        id="test_inline_target",
        name="test analyst",
        schema_uri="legba/analyst/1.0.0",
        version=_VERSION,
        kind=AnalystKind.INLINE_TARGET,
        type_signature=TypeSignature(
            input_type="legba.x.In",
            output_type="legba.x.Out",
        ),
        state=LifecycleState.ACTIVE,
        owner="test",
    )


def _descriptor(*, llm: dict[str, Any]) -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=_identity(),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.prompts.inline_target.v1",
            llm=llm,
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )


def _llm_with_verify() -> dict[str, Any]:
    return {
        "primary": Property.StackRef(
            raw=_PRIMARY_LLM_REF, expected_family="llm_provider",
        ).model_dump(),
        "verify": Property.StackRef(
            raw=_VERIFY_LLM_REF, expected_family="llm_provider",
        ).model_dump(),
    }


def _llm_without_verify() -> dict[str, Any]:
    return {
        "primary": Property.StackRef(
            raw=_PRIMARY_LLM_REF, expected_family="llm_provider",
        ).model_dump(),
    }


class _StubJudge:
    """An LLMProviderHandler-shaped double exposing ``chat_complete`` (verify.py's
    contract surface) so the resolved object is the kind verify expects."""

    subprovider = "stub-judge"

    async def chat_complete(  # pragma: no cover — not invoked in these tests
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw,
    ):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# The resolution RULE under test — replicated from dapr_host's resolver so the
# nested host closure need not be booted, but exercising the REAL helper + REAL
# flag reader (only the handler factory is mocked).
# ---------------------------------------------------------------------------


async def _resolve_verify_judge(
    descriptor: AnalystDescriptor,
    *,
    llm_handler_factory: Any,
) -> Any:
    """Mirror of the host resolver's verify_judge block (dapr_host.py)."""
    verify_judge: Any = None
    verify_component_id = _verify_llm_component_id(descriptor)
    if verify_component_id and _llm_judge_enabled():
        try:
            verify_judge = await llm_handler_factory(verify_component_id)
        except Exception:  # noqa: BLE001 — soft-fail, the floor still runs
            verify_judge = None
    return verify_judge


# ---------------------------------------------------------------------------
# _verify_llm_component_id — the helper
# ---------------------------------------------------------------------------


def test_verify_llm_component_id_present() -> None:
    descriptor = _descriptor(llm=_llm_with_verify())
    assert _verify_llm_component_id(descriptor) == _VERIFY_LLM_REF


def test_verify_llm_component_id_absent() -> None:
    descriptor = _descriptor(llm=_llm_without_verify())
    assert _verify_llm_component_id(descriptor) is None


def test_verify_llm_component_id_accepts_bare_string() -> None:
    # The open dict may carry a bare id string (not only a StackRef dump).
    descriptor = _descriptor(
        llm={"primary": _PRIMARY_LLM_REF, "verify": _VERIFY_LLM_REF},
    )
    assert _verify_llm_component_id(descriptor) == _VERIFY_LLM_REF


# ---------------------------------------------------------------------------
# Resolution rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ref_present_flag_on_resolves_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_VERIFY_LLM_JUDGE_ENV, "1")
    judge = _StubJudge()
    factory = AsyncMock(return_value=judge)
    descriptor = _descriptor(llm=_llm_with_verify())

    resolved = await _resolve_verify_judge(descriptor, llm_handler_factory=factory)

    factory.assert_awaited_once_with(_VERIFY_LLM_REF)
    assert resolved is judge
    # The resolved object is what verify.py / actor_critic call.
    assert hasattr(resolved, "chat_complete")


@pytest.mark.asyncio
async def test_ref_absent_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_VERIFY_LLM_JUDGE_ENV, "1")  # flag ON, but no ref
    factory = AsyncMock(return_value=_StubJudge())
    descriptor = _descriptor(llm=_llm_without_verify())

    resolved = await _resolve_verify_judge(descriptor, llm_handler_factory=factory)

    assert resolved is None
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_ref_present_flag_off_yields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_VERIFY_LLM_JUDGE_ENV, raising=False)  # flag OFF (default)
    factory = AsyncMock(return_value=_StubJudge())
    descriptor = _descriptor(llm=_llm_with_verify())

    resolved = await _resolve_verify_judge(descriptor, llm_handler_factory=factory)

    assert resolved is None
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_raises_soft_fails_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_VERIFY_LLM_JUDGE_ENV, "1")
    factory = AsyncMock(side_effect=RuntimeError("registry 500"))
    descriptor = _descriptor(llm=_llm_with_verify())

    # Must NOT propagate — the deterministic floor still runs.
    resolved = await _resolve_verify_judge(descriptor, llm_handler_factory=factory)

    assert resolved is None
    factory.assert_awaited_once_with(_VERIFY_LLM_REF)


# ---------------------------------------------------------------------------
# P2-4 — the judge ROUTE (resolve_judge_route): the explicit resolution ladder
#   env LEGBA_JUDGE_STACK_REF → method.llm.judge → method.llm.verify →
#   method.llm.primary, gated on the judge/verify opt-in key.
# ---------------------------------------------------------------------------

_JUDGE_LLM_REF = "llm.judge.second_model"
_ENV_JUDGE_REF = "llm.judge.env_override"


async def _resolve_verify_judge_v2(
    descriptor: AnalystDescriptor,
    *,
    llm_handler_factory: Any,
) -> tuple[Any, str]:
    """Mirror of the P2-4 host resolver block (dapr_host.py): route → flag →
    factory, returning ``(handler, verify_judge_ref)``."""
    verify_judge: Any = None
    verify_judge_ref: str = ""
    route = resolve_judge_route(descriptor)
    if route is not None and _llm_judge_enabled():
        try:
            verify_judge = await llm_handler_factory(route.component_id)
            verify_judge_ref = route.component_id
        except Exception:  # noqa: BLE001 — soft-fail, the floor still runs
            verify_judge = None
            verify_judge_ref = ""
    return verify_judge, verify_judge_ref


def test_route_matches_legacy_resolution_for_todays_live_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BYTE-IDENTICAL TODAY: for the live descriptor shape (verify key, no
    judge key, env unset) the route resolves the exact ref the legacy helper
    resolved, via the verify rung."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    descriptor = _descriptor(llm=_llm_with_verify())
    route = resolve_judge_route(descriptor)
    assert route is not None
    assert route.component_id == _verify_llm_component_id(descriptor)
    assert route.source == "method.llm.verify"


@pytest.mark.asyncio
async def test_route_same_ref_in_same_client_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same ref in → same client out: for today's live shape the new route
    resolves the IDENTICAL handler object the legacy rule resolved (one cached
    factory, both rules), and stamps that ref as verify_judge_ref."""
    monkeypatch.setenv(_VERIFY_LLM_JUDGE_ENV, "1")
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    cache: dict[str, Any] = {}

    async def factory(component_id: str) -> Any:
        return cache.setdefault(component_id, _StubJudge())

    descriptor = _descriptor(llm=_llm_with_verify())
    legacy = await _resolve_verify_judge(descriptor, llm_handler_factory=factory)
    routed, ref = await _resolve_verify_judge_v2(
        descriptor, llm_handler_factory=factory
    )
    assert routed is legacy  # the SAME client object
    assert ref == _VERIFY_LLM_REF
    assert len(cache) == 1  # both rules resolved through the one ref


def test_route_none_without_optin_even_with_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env override REPOINTS judge calls; it never conscripts an analyst
    that did not opt into a judge (no judge/verify key → no route, ever)."""
    monkeypatch.setenv(JUDGE_STACK_REF_ENV, _ENV_JUDGE_REF)
    assert resolve_judge_route(_descriptor(llm=_llm_without_verify())) is None
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    assert resolve_judge_route(_descriptor(llm=_llm_without_verify())) is None


def test_route_env_override_repoints_every_opted_in_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(JUDGE_STACK_REF_ENV, _ENV_JUDGE_REF)
    route = resolve_judge_route(_descriptor(llm=_llm_with_verify()))
    assert route is not None
    assert route.component_id == _ENV_JUDGE_REF
    assert route.source == f"env:{JUDGE_STACK_REF_ENV}"


def test_route_judge_key_preferred_over_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    llm = _llm_with_verify()
    llm["judge"] = Property.StackRef(
        raw=_JUDGE_LLM_REF, expected_family="llm_provider",
    ).model_dump()
    route = resolve_judge_route(_descriptor(llm=llm))
    assert route is not None
    assert route.component_id == _JUDGE_LLM_REF
    assert route.source == "method.llm.judge"


def test_route_malformed_refs_fall_to_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal rung: opted in (key present) but judge/verify refs malformed →
    the judge runs on the producer plane rather than silently degrading."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    llm = _llm_without_verify()
    llm["verify"] = {}  # present (opt-in) but carries no resolvable ref
    route = resolve_judge_route(_descriptor(llm=llm))
    assert route is not None
    assert route.component_id == _PRIMARY_LLM_REF
    assert route.source == "method.llm.primary"


def test_route_from_llm_block_dict_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed-dict path (GEPA measure arm) resolves through the same ladder,
    accepting bare-string refs as well as StackRef dumps."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    route = resolve_judge_route_from_llm_block(
        {"primary": _PRIMARY_LLM_REF, "verify": _VERIFY_LLM_REF},
    )
    assert route is not None
    assert route.component_id == _VERIFY_LLM_REF
    assert route.source == "method.llm.verify"
    assert resolve_judge_route_from_llm_block({"primary": _PRIMARY_LLM_REF}) is None
    assert resolve_judge_route_from_llm_block(None) is None


@pytest.mark.asyncio
async def test_route_flag_off_yields_no_handler_and_empty_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_VERIFY_LLM_JUDGE_ENV, raising=False)  # flag OFF
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    factory = AsyncMock(return_value=_StubJudge())
    handler, ref = await _resolve_verify_judge_v2(
        _descriptor(llm=_llm_with_verify()), llm_handler_factory=factory
    )
    assert handler is None
    assert ref == ""
    factory.assert_not_awaited()
