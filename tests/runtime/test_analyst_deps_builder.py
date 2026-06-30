# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the per-kind analyst run_method builder (L-241).

Coverage:

  * Every analyst kind that
    :func:`legba.data.analysts.discover_analyst_kinds` returns is
    buildable by :func:`build_analyst_run_method` and surfaces the
    correct ``OutputKind``.
  * Subprovider inference for the ``llm.primary.openai_compat`` stack
    component returns the vLLM handler class.
  * :func:`build_llm_handler_from_stack_component` constructs a
    configured handler against a mocked registry response.
  * An unknown kind raises :class:`AnalystDepsBuildError` with a
    legible message.
  * ``consult_on_demand`` without a substrate port raises (per the
    no-stubs rule).
  * ``optimizer`` resolves its Temporal client via the in-process path
    when ``LEGBA_OPTIMIZER_IN_PROCESS=1`` (avoids requiring a real
    Temporal cluster for the unit test).

The tests do not require Postgres, NATS, Dapr, or the registry
container — every external dependency is supplied via either a stub
callable or a fake ``RegistryHTTPClient``.  ``pg_pool`` is passed as a
sentinel object because none of the builder branches touch it (the
predictor and deterministic branches surface ``StandardDeps``, which
carries the pool; the substrate-tool kinds need a separately-wired
port).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from legba.data.analysts import discover_analyst_kinds
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps
from legba.data.analysts.critic import CriticDeps
from legba.data.analysts.cross_analyst_correlator import CrossAnalystCorrelatorDeps
from legba.data.analysts.inline_target import InlineTargetDeps, InlineTargetRunner
from legba.data.analysts.optimizer import OptimizerDeps
from legba.data.analysts.predictor import PredictorDeps
from legba.data.provenance.kinds import TRACE_ONLY, OutputKind
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    EvalBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Property
from legba.data.stack.llm import AnthropicProviderHandler, VLLMProviderHandler
from legba.runtime.analyst_deps_builder import (
    AnalystDepsBuildError,
    build_analyst_run_method,
    build_llm_handler_from_stack_component,
    infer_llm_subprovider,
)
from legba.runtime.deps import StandardDeps
from legba.runtime.registry_client import RegistryHTTPClient


# ---------------------------------------------------------------------------
# Test fixtures + helpers
# ---------------------------------------------------------------------------


_VERSION = "0" * 64
_PRIMARY_LLM_REF = "llm.primary.openai_compat"


def _placeholder_version() -> str:
    return _VERSION


def _llm_block() -> dict[str, Any]:
    return {
        "primary": Property.StackRef(
            raw=_PRIMARY_LLM_REF,
            expected_family="llm_provider",
        ).model_dump(),
        "max_tokens": 1024,
    }


def _identity(kind: AnalystKind, *, name: str = "test analyst") -> AnalystIdentity:
    return AnalystIdentity(
        id=f"test_{kind.value}",
        name=name,
        schema_uri="legba/analyst/1.0.0",
        version=_placeholder_version(),
        kind=kind,
        type_signature=TypeSignature(
            input_type="legba.x.In",
            output_type="legba.x.Out",
        ),
        state=LifecycleState.ACTIVE,
        owner="test",
    )


def _llm_descriptor(
    kind: AnalystKind, *, method_kind: str = "llm_single_turn",
    prompt_module: str | None = None,
) -> AnalystDescriptor:
    """Build a minimal LLM-bearing descriptor for one of the analyst kinds."""
    if prompt_module is None and method_kind in (
        "llm_planner", "llm_single_turn", "react_loop", "critic",
    ):
        prompt_module = f"legba.prompts.{kind.value}.v1"
    return AnalystDescriptor(
        identity=_identity(kind),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind=method_kind,
            prompt_module=prompt_module,
            llm=_llm_block(),
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )


def _deterministic_descriptor() -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=_identity(AnalystKind.DETERMINISTIC),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="deterministic",
            impl="legba.data.analysts.deterministic.run_method",
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )


def _optimizer_descriptor() -> AnalystDescriptor:
    return AnalystDescriptor(
        identity=_identity(AnalystKind.OPTIMIZER),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="dspy_compile",
            llm=_llm_block(),
            timeout_seconds=3600,
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
        eval=EvalBlock(
            optimizer={
                "analyzed_analyst_id": "some_analyst",
                "max_generations": 1,
            },
        ),
    )


class _StubLLMHandler:
    """Test double matching the LLMHandlerLike Protocol.

    Returned by the test's ``llm_handler_factory`` closure so the
    builder doesn't hit the registry / vault at all.  Subprovider is
    distinct from any real handler so failure-mode assertions don't
    accidentally collide with a real LLM_HANDLERS lookup.
    """

    subprovider = "stub-test"

    async def chat_complete(  # pragma: no cover — not exercised here
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw,
    ):
        raise NotImplementedError


async def _stub_secrets(_secret_id: str) -> bytes:
    return b"stub-secret-bytes"


def _standard_deps() -> StandardDeps:
    """Fabricate a :class:`StandardDeps` for builder calls.

    ``pg_pool`` is an ``object()`` sentinel because none of the
    builder branches touch the pool directly (the deterministic kind
    surfaces it via the kind_deps bundle but doesn't dereference it).
    """
    return StandardDeps(
        pg_pool=object(),  # type: ignore[arg-type]
        nats_publish=None,
        secrets_resolve=_stub_secrets,
    )


# ---------------------------------------------------------------------------
# Tests: every kind builds
# ---------------------------------------------------------------------------


# "Findings as a real output type" cleanup: relationship_reifier +
# competing_hypotheses are now TRACE_ONLY at bind time (their REAL product is
# side-written nexuses/hypotheses; the run is audited in analyst_traces). The
# deterministic kind's bind-time OUTPUT_KIND stays FINDING — its TRACE_ONLY
# split is per-sub-handler (OUTPUT_KIND_BY_SUB_HANDLER), resolved at run time by
# the actor, not at bind time.
_KIND_EXPECTED_OUTPUT: dict[str, object] = {
    "inline_target": OutputKind.FINDING,
    "cross_target_raw": OutputKind.FINDING,
    "meta_findings_synthesizer": OutputKind.FINDING,
    "cross_analyst_correlator": OutputKind.FINDING,
    "relationship_reifier": TRACE_ONLY,
    "competing_hypotheses": TRACE_ONLY,
    "deterministic": OutputKind.FINDING,
    "predictor": OutputKind.PREDICTION,
    "critic": OutputKind.CRITIQUE,
    "optimizer": OutputKind.PROMPT_MODULE_CANDIDATE,
    "consult_on_demand": OutputKind.FINDING,
}


@pytest.mark.asyncio
async def test_inline_target_builds() -> None:
    """inline_target: returns an InlineTargetRunner + None kind_deps."""
    descriptor = _llm_descriptor(AnalystKind.INLINE_TARGET)
    llm = _StubLLMHandler()
    factory = AsyncMock(return_value=llm)
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=factory,
    )
    factory.assert_awaited_once_with(_PRIMARY_LLM_REF)
    assert isinstance(run_method, InlineTargetRunner)
    # inline_target uses the 2-arg back-compat path; kind_deps is None
    # so the actor dispatcher invokes ``await run_method(inputs, options)``.
    assert kind_deps is None
    assert output_kind == OutputKind.FINDING
    # InlineTargetRunner closes over the InlineTargetDeps bundle.
    assert isinstance(run_method._deps, InlineTargetDeps)
    assert run_method._deps.llm is llm
    assert run_method._deps.max_tokens == 1024


@pytest.mark.asyncio
async def test_cross_target_raw_builds() -> None:
    descriptor = _llm_descriptor(AnalystKind.CROSS_TARGET_RAW)
    llm = _StubLLMHandler()
    factory = AsyncMock(return_value=llm)
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=factory,
    )
    factory.assert_awaited_once_with(_PRIMARY_LLM_REF)
    # 3-arg dispatch — run_method is the kind module's own callable.
    from legba.data.analysts.cross_target_raw import run_method as cross_target_rm
    assert run_method is cross_target_rm
    assert kind_deps is not None
    assert kind_deps.llm is llm
    assert output_kind == OutputKind.FINDING


@pytest.mark.asyncio
async def test_meta_findings_synthesizer_builds() -> None:
    descriptor = _llm_descriptor(AnalystKind.META_FINDINGS_SYNTHESIZER)
    llm = _StubLLMHandler()
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=llm),
    )
    from legba.data.analysts.meta_findings_synthesizer import (
        run_method as meta_rm,
    )
    assert run_method is meta_rm
    assert kind_deps is not None
    assert kind_deps.llm is llm
    assert output_kind == OutputKind.FINDING


@pytest.mark.asyncio
async def test_cross_analyst_correlator_builds() -> None:
    descriptor = _llm_descriptor(AnalystKind.CROSS_ANALYST_CORRELATOR)
    llm = _StubLLMHandler()
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=llm),
    )
    from legba.data.analysts.cross_analyst_correlator import (
        run_method as cac_rm,
    )
    assert run_method is cac_rm
    assert isinstance(kind_deps, CrossAnalystCorrelatorDeps)
    assert kind_deps.llm is llm
    assert output_kind == OutputKind.FINDING


@pytest.mark.asyncio
async def test_competing_hypotheses_builds() -> None:
    """competing_hypotheses (PIECE C ACH): ACHDeps carrying the resolved LLM +
    the pg_pool + the budget reporter (META kind: LLM enrichment + direct
    substrate reads/writes, the relationship_reifier shape)."""
    descriptor = _llm_descriptor(
        AnalystKind.COMPETING_HYPOTHESES, method_kind="llm_planner",
    )
    llm = _StubLLMHandler()
    pool = object()
    deps = _standard_deps()
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=deps,
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=pool,  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=llm),
    )
    from legba.data.analysts.competing_hypotheses import ACHDeps
    from legba.data.analysts.competing_hypotheses import run_method as ach_rm

    assert run_method is ach_rm
    assert isinstance(kind_deps, ACHDeps)
    assert kind_deps.llm is llm
    assert kind_deps.pg_pool is pool
    assert kind_deps.budget is deps.budget
    # TRACE_ONLY: competing_hypotheses side-writes HYPOTHESIS rows; the run is
    # audited in analyst_traces, no redundant FINDING receipt in analyst_outputs.
    assert output_kind is TRACE_ONLY


@pytest.mark.asyncio
async def test_deterministic_builds() -> None:
    """deterministic: no LLM resolution; kind_deps == StandardDeps."""
    descriptor = _deterministic_descriptor()
    deps = _standard_deps()
    # Factory must NOT be called for deterministic — no LLM needed.
    factory = AsyncMock(side_effect=AssertionError("must not call LLM factory"))
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=deps,
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=factory,
    )
    from legba.data.analysts.deterministic import run_method as det_rm
    assert run_method is det_rm
    assert kind_deps is deps
    assert output_kind == OutputKind.FINDING
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_deterministic_wires_llm_tiebreak_when_flag_on(monkeypatch) -> None:
    """Wave 2b (#101): flag ON + method.llm.primary -> the self-hosted vLLM
    tie-break handler is injected into kind_deps.extras AND survives the run-time
    resolve in fact_contention_arbiter.handle()."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION_LLM_TIEBREAK", "1")
    descriptor = AnalystDescriptor(
        identity=_identity(AnalystKind.DETERMINISTIC),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="deterministic",
            impl="legba.data.analysts.deterministic.run_method",
            llm=_llm_block(),
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )
    llm = _StubLLMHandler()
    run_method, kind_deps, *_ = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=llm),
    )
    from legba.data.analysts.deterministic_handlers.fact_contention_arbiter import (
        LLM_DEPS_EXTRA_KEY,
        _resolve_tiebreak_llm,
    )
    # (1) the build injected the handler into the kind_deps extras
    assert kind_deps.extras.get(LLM_DEPS_EXTRA_KEY) is llm
    # (2) the handler survives the actor's run-time resolve (flag still ON)
    assert _resolve_tiebreak_llm(kind_deps) is llm
    # (3) the handler survives storage in the Pydantic _AnalystDeps bundle the
    #     actor caches + dispatches from (the live-specific hop: Any-typed
    #     kind_deps must not be copied/stripped by Pydantic).
    from legba.runtime.dapr_actors import _AnalystDeps

    bundle = _AnalystDeps(
        descriptor=descriptor,
        deps=_standard_deps(),
        run_method=run_method,
        kind_deps=kind_deps,
    )
    assert _resolve_tiebreak_llm(bundle.kind_deps) is llm


@pytest.mark.asyncio
async def test_predictor_builds_with_llm() -> None:
    descriptor = _llm_descriptor(
        AnalystKind.PREDICTOR, method_kind="stat_forecaster",
        prompt_module="legba.prompts.predictor.v1",
    )
    llm = _StubLLMHandler()
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=llm),
    )
    from legba.data.analysts.predictor import run_method as pred_rm
    assert run_method is pred_rm
    assert isinstance(kind_deps, PredictorDeps)
    assert kind_deps.llm is llm
    assert output_kind == OutputKind.PREDICTION


@pytest.mark.asyncio
async def test_predictor_builds_without_llm_block() -> None:
    """Predictor descriptor with no LLM block runs in stat-only mode."""
    descriptor = AnalystDescriptor(
        identity=_identity(AnalystKind.PREDICTOR),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="stat_forecaster",
            prompt_module="legba.prompts.predictor.v1",
            llm={},  # empty — no primary StackRef
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )
    factory = AsyncMock(side_effect=AssertionError("must not call LLM factory"))
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=factory,
    )
    assert isinstance(kind_deps, PredictorDeps)
    assert kind_deps.llm is None
    assert output_kind == OutputKind.PREDICTION
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_critic_builds() -> None:
    descriptor = _llm_descriptor(
        AnalystKind.CRITIC, method_kind="critic",
        prompt_module="legba.prompts.critic.v1",
    )
    llm = _StubLLMHandler()
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=llm),
    )
    from legba.data.analysts.critic import run_method as critic_rm
    assert run_method is critic_rm
    assert isinstance(kind_deps, CriticDeps)
    assert kind_deps.llm is llm
    assert output_kind == OutputKind.CRITIQUE


@pytest.mark.asyncio
async def test_optimizer_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """optimizer: uses Temporal in-process when LEGBA_OPTIMIZER_IN_PROCESS=1."""
    monkeypatch.setenv("LEGBA_OPTIMIZER_IN_PROCESS", "1")
    descriptor = _optimizer_descriptor()
    # No LLM factory call expected — optimizer's run_method doesn't take
    # an LLM via deps (it mediates through dspy.settings.lm).
    factory = AsyncMock(side_effect=AssertionError("must not call LLM factory"))
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=factory,
    )
    from legba.data.analysts.optimizer import run_method as opt_rm
    assert run_method is opt_rm
    assert isinstance(kind_deps, OptimizerDeps)
    assert kind_deps.temporal_client is not None
    assert output_kind == OutputKind.PROMPT_MODULE_CANDIDATE
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_consult_on_demand_builds_with_substrate_port() -> None:
    descriptor = _llm_descriptor(
        AnalystKind.CONSULT_ON_DEMAND, method_kind="react_loop",
        prompt_module="legba.prompts.consult_on_demand.v1",
    )
    llm = _StubLLMHandler()

    class _StubSubstrate:
        async def search_signals(self, **kw):                  # pragma: no cover
            return {"refs": []}

        async def query_facts(self, **kw):                     # pragma: no cover
            return {"refs": []}

        async def inspect_entity(self, **kw):                  # pragma: no cover
            return {"refs": []}

        async def vector_search(self, **kw):                   # pragma: no cover
            return {"refs": []}

    substrate = _StubSubstrate()
    run_method, kind_deps, output_kind, _receipt_chain, _read_slice = await build_analyst_run_method(
        descriptor,
        deps=_standard_deps(),
        registry_client=RegistryHTTPClient(base_url="http://invalid"),
        pg_pool=object(),  # type: ignore[arg-type]
        llm_handler_factory=AsyncMock(return_value=llm),
        substrate_query_port=substrate,
    )
    from legba.data.analysts.consult_on_demand import run_method as cod_rm
    assert run_method is cod_rm
    assert isinstance(kind_deps, ConsultOnDemandDeps)
    assert kind_deps.llm is llm
    assert kind_deps.substrate is substrate
    assert output_kind == OutputKind.FINDING


@pytest.mark.asyncio
async def test_consult_on_demand_without_substrate_raises() -> None:
    """No-stubs rule: consult_on_demand without a SubstrateQueryPort fails loudly."""
    descriptor = _llm_descriptor(
        AnalystKind.CONSULT_ON_DEMAND, method_kind="react_loop",
        prompt_module="legba.prompts.consult_on_demand.v1",
    )
    llm = _StubLLMHandler()
    with pytest.raises(AnalystDepsBuildError, match="SubstrateQueryPort"):
        await build_analyst_run_method(
            descriptor,
            deps=_standard_deps(),
            registry_client=RegistryHTTPClient(base_url="http://invalid"),
            pg_pool=object(),  # type: ignore[arg-type]
            llm_handler_factory=AsyncMock(return_value=llm),
            substrate_query_port=None,
        )


# ---------------------------------------------------------------------------
# Tests: output-kind table is consistent
# ---------------------------------------------------------------------------


def test_output_kinds_match_discover_registry() -> None:
    """Cross-check the builder's expected OutputKinds against the kind
    modules' module-level OUTPUT_KIND constants.  This protects against
    a kind module rewiring its output kind without us updating the test
    fixture.
    """
    registry = discover_analyst_kinds()
    for kind_name, expected in _KIND_EXPECTED_OUTPUT.items():
        assert kind_name in registry, (
            f"kind {kind_name!r} no longer discovered by registry"
        )
        actual = registry[kind_name].output_kind
        assert actual == expected, (
            f"kind {kind_name!r}: registry output_kind={actual!r} but test "
            f"expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# Tests: unknown kind raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_kind_raises() -> None:
    """A descriptor whose identity.kind isn't in the registry surfaces a
    clear error rather than silently no-op-ing.

    We can't construct an AnalystDescriptor with a totally bogus kind
    (the field validator rejects unknown kinds), so instead we monkey-
    patch discover_analyst_kinds to return a registry missing the
    inline_target entry — the same code path the builder takes when a
    kind genuinely isn't registered.
    """
    descriptor = _llm_descriptor(AnalystKind.INLINE_TARGET)
    import legba.runtime.analyst_deps_builder as mod
    original = mod.discover_analyst_kinds

    def _empty_registry() -> dict:
        return {}

    mod.discover_analyst_kinds = _empty_registry  # type: ignore[assignment]
    try:
        with pytest.raises(AnalystDepsBuildError, match="unknown analyst kind"):
            await build_analyst_run_method(
                descriptor,
                deps=_standard_deps(),
                registry_client=RegistryHTTPClient(base_url="http://invalid"),
                pg_pool=object(),  # type: ignore[arg-type]
                llm_handler_factory=AsyncMock(return_value=_StubLLMHandler()),
            )
    finally:
        mod.discover_analyst_kinds = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_missing_llm_primary_raises() -> None:
    """An LLM-bearing kind whose descriptor doesn't carry an LLM StackRef
    fails the builder loudly."""
    descriptor = AnalystDescriptor(
        identity=_identity(AnalystKind.INLINE_TARGET),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.prompts.inline_target.v1",
            llm={},  # empty — no primary
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
    )
    with pytest.raises(AnalystDepsBuildError, match="method.llm.primary"):
        await build_analyst_run_method(
            descriptor,
            deps=_standard_deps(),
            registry_client=RegistryHTTPClient(base_url="http://invalid"),
            pg_pool=object(),  # type: ignore[arg-type]
            llm_handler_factory=AsyncMock(return_value=_StubLLMHandler()),
        )


# ---------------------------------------------------------------------------
# Tests: infer_llm_subprovider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "component_id,endpoint,expected",
    [
        ("llm.primary.openai_compat", "https://llm.example.internal/v1", "vllm"),
        ("llm.anthropic.claude_opus_4_7", "https://api.anthropic.com/v1", "anthropic"),
        ("llm.anthropic.claude_sonnet_4_5", "", "anthropic"),
        ("llm.openai.gpt5", "https://api.openai.com/v1", "openai"),
        # Unknown id pattern but the endpoint hostname pins anthropic.
        ("llm.some_alias.x", "https://api.anthropic.com/v1", "anthropic"),
        # Unknown id pattern and unknown endpoint — fall back to vllm.
        ("llm.custom.local", "http://localhost:8000", "vllm"),
    ],
)
def test_infer_llm_subprovider(
    component_id: str, endpoint: str, expected: str,
) -> None:
    assert infer_llm_subprovider(component_id, endpoint=endpoint) == expected


# ---------------------------------------------------------------------------
# Tests: build_llm_handler_from_stack_component
# ---------------------------------------------------------------------------


def _registry_client_with_stack_response(
    component_id: str, body: dict[str, Any], *, status_code: int = 200,
) -> RegistryHTTPClient:
    """Build a :class:`RegistryHTTPClient` whose AsyncClient is wired to
    return ``body`` for a single GET on the stack-component endpoint."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/stack/{component_id}"), (
            f"unexpected path: {request.url.path}"
        )
        if status_code == 404:
            return httpx.Response(404)
        return httpx.Response(status_code, json=body)

    transport = httpx.MockTransport(_handler)
    inner = httpx.AsyncClient(
        transport=transport, base_url="http://registry.test",
    )
    client = RegistryHTTPClient(
        base_url="http://registry.test", token=None, client=inner,
    )
    return client


@pytest.mark.asyncio
async def test_build_llm_handler_constructs_vllm() -> None:
    """A registry response carrying a valid LLMProviderConfig under
    ``body.config`` yields a configured :class:`VLLMProviderHandler`."""
    component_id = _PRIMARY_LLM_REF
    body = {
        "id": component_id,
        "version": _placeholder_version(),
        "body": {
            "id": component_id,
            "name": "gpt-oss-120b via vLLM",
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "version": _placeholder_version(),
            "owner": "test",
            "state": "active",
            "config": {
                "api_endpoint": {
                    "factory_kind": "text",
                    "raw": "https://llm.example.internal/v1",
                },
                "api_key": {
                    "factory_kind": "secret",
                    "raw": "llm.primary.api_key",
                },
                "model_name": {
                    "factory_kind": "text",
                    "raw": "gpt-oss-120b",
                },
                "max_tokens": {
                    "factory_kind": "number",
                    "raw": 16384,
                },
            },
        },
    }
    client = _registry_client_with_stack_response(component_id, body)
    handler = await build_llm_handler_from_stack_component(
        component_id,
        registry_client=client,
        secrets_resolve=_stub_secrets,
    )
    assert isinstance(handler, VLLMProviderHandler)
    # on_configure was called — the handler has a populated config + key.
    assert handler._cfg is not None
    assert handler._api_key == "stub-secret-bytes"
    assert handler._cfg.api_endpoint.raw == "https://llm.example.internal/v1"
    # on_configure's model-list probe opens the client lazily; close it
    # so the test session doesn't leak the connection.
    if handler._client is not None:
        await handler._client.aclose()
    await client.close()


@pytest.mark.asyncio
async def test_build_llm_handler_constructs_anthropic() -> None:
    """A component id matching the ``llm.anthropic.*`` pattern picks the
    Anthropic handler regardless of endpoint."""
    component_id = "llm.anthropic.claude_sonnet_4_5"
    body = {
        "id": component_id,
        "version": _placeholder_version(),
        "body": {
            "id": component_id,
            "name": "Claude Sonnet 4.5",
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "version": _placeholder_version(),
            "owner": "test",
            "state": "active",
            "config": {
                "api_endpoint": {
                    "factory_kind": "text",
                    "raw": "https://api.anthropic.com/v1",
                },
                "api_key": {
                    "factory_kind": "secret",
                    "raw": "llm.anthropic.api_key",
                },
                "model_name": {
                    "factory_kind": "text",
                    "raw": "claude-sonnet-4-5",
                },
                "max_tokens": {
                    "factory_kind": "number",
                    "raw": 8192,
                },
            },
        },
    }
    client = _registry_client_with_stack_response(component_id, body)
    handler = await build_llm_handler_from_stack_component(
        component_id,
        registry_client=client,
        secrets_resolve=_stub_secrets,
    )
    assert isinstance(handler, AnthropicProviderHandler)
    await client.close()


@pytest.mark.asyncio
async def test_build_llm_handler_404_raises() -> None:
    """Registry 404 on stack-component lookup surfaces a clear error."""
    component_id = "llm.nonexistent.x"
    body: dict[str, Any] = {}
    client = _registry_client_with_stack_response(
        component_id, body, status_code=404,
    )
    with pytest.raises(AnalystDepsBuildError, match="not found"):
        await build_llm_handler_from_stack_component(
            component_id,
            registry_client=client,
            secrets_resolve=_stub_secrets,
        )
    await client.close()


@pytest.mark.asyncio
async def test_build_llm_handler_malformed_config_raises() -> None:
    """A registry response missing the body.config block surfaces a
    legible validation error."""
    component_id = _PRIMARY_LLM_REF
    body = {
        "id": component_id,
        "version": _placeholder_version(),
        "body": {"id": component_id, "name": "broken"},  # no .config
    }
    client = _registry_client_with_stack_response(component_id, body)
    with pytest.raises(AnalystDepsBuildError, match="config"):
        await build_llm_handler_from_stack_component(
            component_id,
            registry_client=client,
            secrets_resolve=_stub_secrets,
        )
    await client.close()
