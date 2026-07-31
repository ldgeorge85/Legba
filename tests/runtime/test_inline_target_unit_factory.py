# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-T1 (unit-factory) — a bounded reasoning unit is JUST a descriptor.

A new ``inline_target`` unit (e.g. "leadership-transition risk") needs NO new
Python kind module: it carries its OWN system prompt (``method.prompt_module``
OR an inline ``method.system_prompt``) + its OWN scope predicate + its OWN
``eval.rubric``. This test pins the load-bearing mechanics:

  * ``run_method`` drives synthesis with ``deps.system_prompt`` when set (the
    unit-specific text reaches the LLM ``system=`` argument), and falls back to
    the kind default ``_SYSTEM_PROMPT`` when it is unset / ``None``.
  * the deps-builder resolves a descriptor's ``method.prompt_module`` AND an
    inline ``method.system_prompt`` into ``InlineTargetDeps.system_prompt`` —
    with NO new entry in the kind registry.

Pure-Python: no Postgres / NATS / Dapr / registry container (the deps-builder
branch soft-fails the promotion lookup on the sentinel ``pg_pool`` and the LLM
handler is supplied via an explicit factory, so the registry is never hit).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from legba.data.analysts._tradecraft import ANALYTIC_PREAMBLE
from legba.data.analysts.unit_grounding import (
    UNIT_GROUNDING_CLAUSE,
    with_grounding_clause,
)
from legba.data.analysts.inline_target import (
    InlineTargetDeps,
    InlineTargetRunner,
    _SYSTEM_PROMPT,
    _effective_system_prompt,
    run_method,
)
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    EvalBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    SubscriptionTargets,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.runtime.analyst_deps_builder import build_analyst_run_method
from legba.runtime.deps import StandardDeps
from legba.runtime.registry_client import RegistryHTTPClient


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _CapturingLLM:
    """``LLMHandlerLike`` double that records the ``system=`` it was called with."""

    subprovider = "stub-test"

    def __init__(self) -> None:
        self.systems: list[str | None] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.systems.append(system)
        finding = {
            "title": "unit finding",
            "body": "Body with a cited claim [1].",
            "confidence": 0.5,
            "evidence": ["e1"],
            "tags": ["severity:low"],
        }
        return _Response(content=json.dumps(finding), usage=_Usage())


def _signal_row(*, id_: UUID) -> dict[str, Any]:
    return {
        "id": id_,
        "title": "A leadership reshuffle is reported",
        "produced_at": "2026-06-29T14:00:00+00:00",
        "source_url": "https://example.com/news/reshuffle",
        "data": {"summary": "Cabinet-level changes signalled ahead of the vote."},
    }


_UNIT_PROMPT = (
    "TASK — LEADERSHIP-TRANSITION RISK. Assess the probability and timing of a "
    "change in the target's top leadership. UNIT-SENTINEL-LTR-9f2a."
)


# ---------------------------------------------------------------------------
# _effective_system_prompt — set vs None fallback
# ---------------------------------------------------------------------------


def test_effective_system_prompt_uses_deps_value_when_set() -> None:
    """The descriptor's prompt drives synthesis, VERBATIM — QW1-B appends the
    shared DESK GROUNDING clause after it (one definition for every unit) and
    changes nothing the unit itself wrote."""
    deps = InlineTargetDeps(llm=_CapturingLLM(), system_prompt=_UNIT_PROMPT)
    resolved = _effective_system_prompt(deps)
    assert resolved == with_grounding_clause(_UNIT_PROMPT)
    assert resolved.startswith(_UNIT_PROMPT)
    assert UNIT_GROUNDING_CLAUSE in resolved


def test_effective_system_prompt_falls_back_when_none() -> None:
    deps = InlineTargetDeps(llm=_CapturingLLM(), system_prompt=None)  # type: ignore[arg-type]
    resolved = _effective_system_prompt(deps)
    assert resolved == with_grounding_clause(_SYSTEM_PROMPT)
    assert _SYSTEM_PROMPT.strip() in resolved


def test_the_grounding_clause_is_never_stamped_twice() -> None:
    """A GEPA-promoted candidate that already carries the clause must not get a
    second copy."""
    already = with_grounding_clause(_UNIT_PROMPT)
    deps = InlineTargetDeps(llm=_CapturingLLM(), system_prompt=already)
    resolved = _effective_system_prompt(deps)
    assert resolved == already
    assert resolved.count(UNIT_GROUNDING_CLAUSE) == 1


# ---------------------------------------------------------------------------
# run_method drives synthesis with the unit's prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_synthesizes_with_unit_system_prompt() -> None:
    """A descriptor-supplied unit prompt reaches the synthesis LLM call."""
    llm = _CapturingLLM()
    deps = InlineTargetDeps(llm=llm, system_prompt=_UNIT_PROMPT)
    # No target_id → meta run (off-target guard is a no-op), single synthesis call.
    await run_method([_signal_row(id_=uuid4())], {"analyst_id": "unit.ltr"}, deps)
    assert llm.systems == [with_grounding_clause(_UNIT_PROMPT)]
    # The unit-specific sentinel is in the rendered system prompt.
    assert "UNIT-SENTINEL-LTR-9f2a" in (llm.systems[0] or "")


@pytest.mark.asyncio
async def test_run_method_falls_back_to_default_system_prompt() -> None:
    """deps.system_prompt unset/None → synthesis uses the kind default."""
    llm = _CapturingLLM()
    deps = InlineTargetDeps(llm=llm, system_prompt=None)  # type: ignore[arg-type]
    await run_method([_signal_row(id_=uuid4())], {"analyst_id": "unit.ltr"}, deps)
    assert llm.systems == [with_grounding_clause(_SYSTEM_PROMPT)]


# ---------------------------------------------------------------------------
# Deps-builder threads the descriptor's prompt into InlineTargetDeps
# ---------------------------------------------------------------------------


_VERSION = "0" * 64


def _identity() -> AnalystIdentity:
    return AnalystIdentity(
        id="leadership_transition_unit",
        name="Leadership-Transition Risk Unit",
        schema_uri="legba/analyst/1.0.0",
        version=_VERSION,
        kind=AnalystKind.INLINE_TARGET,
        type_signature=TypeSignature(
            input_type="legba.runtime.SignalList",
            output_type="legba.runtime.Finding",
        ),
        state=LifecycleState.ACTIVE,
        owner="p2_t1_test",
    )


def _llm_block() -> dict[str, Any]:
    return {
        "primary": Property.StackRef(
            raw="llm.primary.openai_compat",
            expected_family="llm_provider",
        ).model_dump(),
        "max_tokens": 1024,
    }


def _unit_descriptor(
    *, prompt_module: str | None = None, system_prompt: str | None = None,
) -> AnalystDescriptor:
    """A bounded-unit descriptor: scope predicate + own prompt + eval.rubric."""
    return AnalystDescriptor(
        identity=_identity(),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(predicate='has_tag("g20")'),
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module=prompt_module,
            system_prompt=system_prompt,
            llm=_llm_block(),
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
        eval=EvalBlock(rubric='{"dimensions": []}'),
    )


async def _stub_secrets(_secret_id: str) -> bytes:
    return b"stub-secret-bytes"


def _standard_deps() -> StandardDeps:
    return StandardDeps(
        pg_pool=object(),  # type: ignore[arg-type]
        nats_publish=None,
        secrets_resolve=_stub_secrets,
    )


async def _build(descriptor: AnalystDescriptor) -> InlineTargetRunner:
    run_method_obj, kind_deps, _kind, _receipt, _slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=_CapturingLLM()),
    )
    assert isinstance(run_method_obj, InlineTargetRunner)
    assert kind_deps is None  # inline_target closes over its bundle
    return run_method_obj


@pytest.mark.asyncio
async def test_deps_builder_threads_inline_system_prompt() -> None:
    """Inline ``method.system_prompt`` → InlineTargetDeps.system_prompt."""
    runner = await _build(_unit_descriptor(system_prompt=_UNIT_PROMPT))
    resolved = runner._deps.system_prompt
    # The unit's verbatim text is threaded through (the house preamble is
    # prepended by with_preamble_if_absent, but the unit block survives).
    assert "UNIT-SENTINEL-LTR-9f2a" in resolved


@pytest.mark.asyncio
async def test_deps_builder_threads_prompt_module() -> None:
    """``method.prompt_module`` ("module:attr") → InlineTargetDeps.system_prompt."""
    runner = await _build(
        _unit_descriptor(
            prompt_module="legba.data.analysts._tradecraft:ANALYTIC_PREAMBLE",
        )
    )
    # Resolved to the module attr (already carries the preamble → not double-wrapped).
    assert runner._deps.system_prompt == ANALYTIC_PREAMBLE


@pytest.mark.asyncio
async def test_deps_builder_prompt_module_wins_over_inline() -> None:
    """When BOTH are set, the resolvable prompt_module takes precedence."""
    runner = await _build(
        _unit_descriptor(
            prompt_module="legba.data.analysts._tradecraft:ANALYTIC_PREAMBLE",
            system_prompt=_UNIT_PROMPT,
        )
    )
    assert runner._deps.system_prompt == ANALYTIC_PREAMBLE
    assert "UNIT-SENTINEL-LTR-9f2a" not in runner._deps.system_prompt
