# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — the correctness grader's THREE-family deps wiring, and its fence.

The grader's whole claim is that three families reading one rubric agree with
each other. That property is destroyed silently if the wiring hands two of the
three slots the same plane — an "independent" second opinion from the writer's
own family is the writer agreeing with itself, and nothing about the descriptor
would look wrong. That is exactly the R2 void, and this file is where it fails
loudly instead.

Every leg is optional and every leg DEGRADES: a refused or unresolvable family
is simply absent from ``deps.extras``, the handler grades with what it has, and
the claims that ended up with one family's label are flagged on the row. A
build failure here would take the whole analyst down for a missing second
opinion, which is worse than a measured number that says how many families
produced it.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.data.analysts.deterministic_handlers.correctness_grader import (
    F0_DEPS_EXTRA_KEY,
    F2_DEPS_EXTRA_KEY,
    F3_DEPS_EXTRA_KEY,
)
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
from legba.runtime.analyst_deps_kinds import wire_correctness_grader_kind_deps
from legba.runtime.deps import StandardDeps
from legba.runtime.external_grader_route import JUDGE_STACK_REF_ENV

_CORE = "llm.primary.openai_compat"
_F2 = "llm.audit.openrouter_llama33_70b.openai_compat"
_F3 = "llm.judge.openrouter_mistral_large.openai_compat"
_JUDGE = "llm.judge.openrouter_nemotron120b"
_ANTHROPIC = "llm.anthropic.opus_4_7"


class _Stub:
    """A resolved handler double. Identity is all these tests need."""

    def __init__(self, component_id: str) -> None:
        self.component_id = component_id


async def _stub_secrets(_secret_id: str) -> bytes:
    return b"stub-secret-bytes"


async def _resolve_llm() -> Any:
    return _Stub(_CORE)


def _descriptor(**llm_over: Any) -> AnalystDescriptor:
    llm: dict[str, Any] = {
        "temperature": 0.0,
        "primary": {"factory_kind": "stack_ref", "raw": _CORE,
                    "expected_family": "llm_provider"},
        "family_f2": {"factory_kind": "stack_ref", "raw": _F2,
                      "expected_family": "llm_provider"},
        "family_f3": {"factory_kind": "stack_ref", "raw": _F3,
                      "expected_family": "llm_provider"},
    }
    llm.update(llm_over)
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="correctness_grader",
            name="Correctness grader",
            schema_uri="legba/analyst/1.0.0",
            version="0" * 16,
            kind=AnalystKind.DETERMINISTIC,
            type_signature=TypeSignature(
                input_type="legba.x.In", output_type="legba.x.Out",
            ),
            state=LifecycleState.ACTIVE,
            owner="test",
        ),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="deterministic",
            impl="legba.data.analysts.deterministic:run_method",
            sub_handler="correctness_grader",
            llm=llm,
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )


def _deps() -> StandardDeps:
    return StandardDeps(
        pg_pool=object(),  # type: ignore[arg-type]
        nats_publish=None,
        secrets_resolve=_stub_secrets,
    )


async def _wire(descriptor, *, builder=None, registry_client=object()):
    async def _default_builder(component_id, **_kw):
        return _Stub(component_id)

    async def _wire_det(desc, deps, resolve, *, component_id, extra_key,
                        purpose):
        from dataclasses import replace

        return replace(
            deps, extras={**dict(deps.extras), extra_key: _Stub(component_id)}
        )

    return await wire_correctness_grader_kind_deps(
        descriptor, _deps(),
        registry_client=registry_client,
        resolve_llm=_resolve_llm,
        component_id=_CORE,
        wire_deterministic_llm=_wire_det,
        handler_builder=builder or _default_builder,
    )


@pytest.mark.asyncio
async def test_all_three_families_wire_under_three_distinct_keys(monkeypatch):
    monkeypatch.setenv(JUDGE_STACK_REF_ENV, _JUDGE)
    deps = await _wire(_descriptor())
    assert deps.extras[F0_DEPS_EXTRA_KEY].component_id == _CORE
    assert deps.extras[F2_DEPS_EXTRA_KEY].component_id == _F2
    assert deps.extras[F3_DEPS_EXTRA_KEY].component_id == _F3
    assert len({F0_DEPS_EXTRA_KEY, F2_DEPS_EXTRA_KEY, F3_DEPS_EXTRA_KEY}) == 3
    assert deps.extras[f"{F2_DEPS_EXTRA_KEY}_ref"] == _F2


@pytest.mark.asyncio
async def test_the_producer_family_is_disclosed_not_fenced(monkeypatch):
    """F0 IS the writer's plane, and that is deliberate.

    Fencing it would delete the only $0 family and with it any correctness
    number on a deployment that spends nothing. PREREG_P1 §4 registered the
    disclosure and VERDICT_P1v4 measured F0 not to be the outlier.
    """
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    deps = await _wire(_descriptor())
    assert F0_DEPS_EXTRA_KEY in deps.extras


@pytest.mark.asyncio
async def test_a_paid_family_on_the_writers_plane_is_refused(monkeypatch):
    """A second opinion from the writer's own family is the writer agreeing
    with itself."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    deps = await _wire(_descriptor(
        family_f2={"factory_kind": "stack_ref", "raw": _CORE},
    ))
    assert F2_DEPS_EXTRA_KEY not in deps.extras
    assert F3_DEPS_EXTRA_KEY in deps.extras, "the other family still wires"


@pytest.mark.asyncio
async def test_a_paid_family_on_the_live_judges_plane_is_refused(monkeypatch):
    """Read at resolution time, not captured at import: the operator repoints
    the whole deployment's judging with that one variable, and a fence that
    cached it would fence yesterday's judge."""
    monkeypatch.setenv(JUDGE_STACK_REF_ENV, _F3)
    deps = await _wire(_descriptor())
    assert F3_DEPS_EXTRA_KEY not in deps.extras
    assert F2_DEPS_EXTRA_KEY in deps.extras


@pytest.mark.asyncio
async def test_anthropic_is_refused_outright(monkeypatch):
    """A HARD standing rule, not a preference: Anthropic is consult-only and
    never scheduled."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    deps = await _wire(_descriptor(
        family_f3={"factory_kind": "stack_ref", "raw": _ANTHROPIC},
    ))
    assert F3_DEPS_EXTRA_KEY not in deps.extras


@pytest.mark.asyncio
async def test_two_paid_families_on_the_same_plane_is_one_family(monkeypatch):
    """Two 'independent' families that are the same family are one family with
    two names — precisely the mistake the R2 void was made of."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    deps = await _wire(_descriptor(
        family_f3={"factory_kind": "stack_ref", "raw": _F2},
    ))
    assert F2_DEPS_EXTRA_KEY in deps.extras
    assert F3_DEPS_EXTRA_KEY not in deps.extras


@pytest.mark.asyncio
async def test_a_family_that_fails_to_resolve_degrades_it_does_not_raise(
    monkeypatch,
):
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)

    async def _builder(component_id, **_kw):
        if component_id == _F2:
            raise RuntimeError("registry 503")
        return _Stub(component_id)

    deps = await _wire(_descriptor(), builder=_builder)
    assert F2_DEPS_EXTRA_KEY not in deps.extras
    assert F3_DEPS_EXTRA_KEY in deps.extras
    assert F0_DEPS_EXTRA_KEY in deps.extras


@pytest.mark.asyncio
async def test_no_registry_client_leaves_the_core_plane_wired_alone(monkeypatch):
    """The $0 family needs no registry fetch; the paid ones do. Losing the
    registry must cost the second opinion, not the whole instrument."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    deps = await _wire(_descriptor(), registry_client=None)
    assert F0_DEPS_EXTRA_KEY in deps.extras
    assert F2_DEPS_EXTRA_KEY not in deps.extras
    assert F3_DEPS_EXTRA_KEY not in deps.extras


@pytest.mark.asyncio
async def test_a_descriptor_that_declares_no_paid_family_wires_only_f0(
    monkeypatch,
):
    """The $0-only shape, and it must build cleanly rather than warn."""
    monkeypatch.delenv(JUDGE_STACK_REF_ENV, raising=False)
    descriptor = _descriptor()
    descriptor.method.llm = {
        "primary": {"factory_kind": "stack_ref", "raw": _CORE},
    }
    deps = await _wire(descriptor)
    assert F0_DEPS_EXTRA_KEY in deps.extras
    assert F2_DEPS_EXTRA_KEY not in deps.extras
    assert F3_DEPS_EXTRA_KEY not in deps.extras
