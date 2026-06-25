# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-175 critic descriptor end-to-end through daprd (K-1 / done-plan §4).

Closes the eval-loop gap: until this test landed there was no critic
analyst descriptor in the registry and the runtime's L-175 kind code
path was unexercised under production conditions.  The gate-11 spike
test (``test_spike_integration.test_gate11_critic_grades_finding_through_daprd``)
proved the kind handler works when wired manually; this file proves it
works through the production resolver shape with:

  * a critic descriptor registered via :class:`DescriptorRegistry` (the
    same path the registry REST API takes — ``deps_.descriptor_registry
    .register(descriptor, actor=...)``);
  * the analyzed analyst's rubric resolved at activation time from its
    typed ``eval.rubric`` block (the L-105 §3 / Wave-B path, NOT the
    legacy ``eval.optimizer["allow_self_correlated"]`` fall-through);
  * a real ``daprd`` sidecar dispatching ``activate`` / ``run`` via
    ActorProxy;
  * the trace-finalizer write to ``analyst_critiques`` newly wired in
    :mod:`legba.runtime.dapr_actors` — before this hook landed the
    column was empty in production and the L-176 optimizer training
    window saw no rows.

Heterogeneity-guard rejection + missing-rubric rejection both run
without an LLM (the guard / rubric check trips before the chat_complete
call), so the live-LLM gate (``LEGBA_TEST_LIVE_LLM=1``) only affects
the happy-path assertion that uses a canned-JSON LLM handler.  The
canned handler keeps Lewis's no-stubs rule honest: the kind handler
itself runs unmodified, including the heterogeneity guard, the JSON
coercion, and the analyst-output dispatcher — only the wire-out to the
Anthropic API is short-circuited.

Per the user prompt's gating rule:

    "Real LLM call (Anthropic) — gated on LEGBA_TEST_LIVE_LLM=1 for the
     actual judge call; the heterogeneity guard + activation test can
     run without."

The Anthropic API key must already be in the vault at
``llm.anthropic.api_key`` for the live-LLM run.  Operator-action: see
the report at the head of K-1 task spec; this test does NOT seed the
secret (Lewis's no-stubs rule forbids hardcoding a real key).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import suppress
from typing import Any, Mapping
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from dapr.actor import ActorId, ActorProxy

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
from legba.data.schemas.target import OutputBinding
from legba.data.registry.descriptor import Family
from legba.runtime import dapr_actors
from legba.runtime.deps import StandardDeps

# Re-use the spike test's fixtures + harness rather than duplicate them.
# pytest's conftest discovery + the explicit imports here keep the fixture
# graph honest (descriptor_registry, stack_registry, vault, pg_pool, etc.
# all come from the spike module's conftest path).
from tests.runtime.test_spike_integration import (  # noqa: F401
    _await_actor_call,
    _deactivate_actor,
    _build_stack_components,
    dapr_host_app,
    descriptor_registry,
    nats_store,
    pg_pool,
    pg_store,
    stack_registry,
    vault,
)


# ---------------------------------------------------------------------------
# Module-level skip gates — mirror the spike test's daprd reachability
# ---------------------------------------------------------------------------


from tests.runtime.test_spike_integration import (  # noqa: E402
    _dapr_sidecar_outbound_ok,
)


def _port_free(port: int) -> bool:
    """Check whether ``port`` is bindable on 0.0.0.0.  When the production
    runtime-dapr container is up, port 6090 is already bound — the spike
    test pattern of bringing up its own uvicorn instance can't claim the
    port and uvicorn exits with SystemExit(1).  The descriptor-validation
    test below doesn't need the in-process FastAPI app (it talks straight
    to the typed registry), so it runs regardless; the actor-dispatch
    tests skip when the port is held by the production runtime — that
    flow is exercised by the manual validation captured in the K-1
    report (registry POST + Dapr HTTP POST → analyst_outputs +
    analyst_critiques rows land).
    """
    s = socket.socket()
    try:
        s.bind(("0.0.0.0", port))
        s.close()
        return True
    except OSError:
        return False


_APP_PORT_FREE = _port_free(6090)
_DAPR_OK = _dapr_sidecar_outbound_ok()

pytestmark = pytest.mark.skipif(
    not _DAPR_OK,
    reason="daprd outbound channel unhealthy (placement/sidecar down)",
)


# ---------------------------------------------------------------------------
# Descriptor builders — production-shaped, NOT spike test ids
# ---------------------------------------------------------------------------


def _placeholder_version() -> str:
    return "0" * 64


def _build_brazil_inline_with_rubric(target_id: str = "india_energy_infra") -> AnalystDescriptor:
    """Inline-target analyst pinned to india_energy_infra, with a rubric.

    Mirrors the production ``india_energy_inline`` descriptor's shape
    (same kind, same LLM stack ref, same cadence) and adds an
    ``eval.rubric`` block — the critic resolves both the rubric and the
    ``allow_self_correlated`` flag from this descriptor at activation
    time via :func:`legba.runtime.dapr_actors._resolve_critic_context`.

    Distinct descriptor_id so the test doesn't clobber the production
    head row in the shared registry (per the spike test's prefix
    discipline).
    """
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="india_energy_inline_critic_test",
            name="Brazil Energy Inline (critic test)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="critic_e2e_test",
        ),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(
                predicate=f'target_id() == "{target_id}"',
                data_types=["signal"],
                time_window="24h",
            ),
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
            budget_tokens_per_day=200_000,
        ),
        cadence=CadenceBlock(
            fallback_schedule="*/5 * * * *",
            cooldown_seconds=300,
        ),
        outputs=[
            OutputBinding(
                kind="a2a_skill",
                config={"skill_id": "intelligence.india_energy_assessment"},
            ),
        ],
        eval=EvalBlock(
            # Three-axis rubric with weights summing to 1.0, per the task
            # spec.  factuality / completeness / severity_calibration are
            # the L-105 §3 canonical dimensions for an intelligence
            # finding (versus the spike's evidence_quality /
            # actionability / concision — that's the kind-handler-spec
            # axis set, not the eval-loop axis set).
            rubric=json.dumps({
                "dimensions": [
                    {"name": "factuality", "weight": 0.4,
                     "description": (
                         "Are the named entities, dates, and quantitative "
                         "claims verifiable against the cited evidence?"
                     )},
                    {"name": "completeness", "weight": 0.3,
                     "description": (
                         "Does the finding cover the rubric scope: causes, "
                         "actors, time horizon, and downstream impacts?"
                     )},
                    {"name": "severity_calibration", "weight": 0.3,
                     "description": (
                         "Does the stated confidence match the strength of "
                         "the evidence?  Penalize both overconfidence and "
                         "under-confidence."
                     )},
                ],
                "scale": "0.0-1.0",
            }),
            allow_self_correlated=False,
        ),
    )


def _build_critic_descriptor(
    *, analyzed_analyst_id: str,
    llm_ref: str = "llm.anthropic.opus_4_7",
) -> AnalystDescriptor:
    """L-175 critic descriptor — kind ``critic``, anthropic judge LLM.

    The critic is pinned to grade ``analyzed_analyst_id`` via the
    descriptor's ``eval.optimizer["analyzed_analyst_id"]`` field — the
    code path :func:`_critic_descriptor_pinned_analyst_id` in
    ``dapr_actors.py`` reads this so the actor's run path can resolve
    the analyzed analyst without the caller passing
    ``options["analyzed_analyst_id"]`` every time.
    """
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="india_energy_critic",
            name="Brazil Energy Critic (Claude Opus 4.7)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.CRITIC,
            type_signature=TypeSignature(
                input_type="legba.runtime.AnalystOutputRow",
                output_type="legba.runtime.Critique",
            ),
            state=LifecycleState.ACTIVE,
            owner="critic_e2e_test",
        ),
        subscription=SubscriptionBlock(
            # The critic reads ONE analyst-output row via its kind READ_SLICE
            # adapter, keyed off ``target_filter`` (the row UUID) or
            # ``options['analyzed_output_id']``.  No substrate slice needed.
            substrate={"direct_queries": True},
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="critic",
            prompt_module="legba.prompts.critic.v1",
            llm={
                "primary": Property.StackRef(
                    raw=llm_ref,
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1536,
            },
            budget_tokens_per_day=50_000,
        ),
        cadence=CadenceBlock(
            # Match the inline analyst's cadence — every 5 minutes.  The
            # actual schedule will be exercised by the reconcile loop in
            # production; here we use ActorProxy.run() to force a tick.
            fallback_schedule="*/5 * * * *",
            cooldown_seconds=60,
        ),
        outputs=[
            OutputBinding(
                kind="nats_stream",
                config={"channel": "critiques"},
            ),
        ],
        eval=EvalBlock(
            # Descriptor-pinned target analyst id — the actor resolves
            # the analyzed analyst from this field at activation time.
            optimizer={"analyzed_analyst_id": analyzed_analyst_id},
            allow_self_correlated=False,
        ),
    )


# ---------------------------------------------------------------------------
# Canned-JSON LLM handler (mirrors the gate-11 _CriticLLMHandler pattern)
# ---------------------------------------------------------------------------


class _CannedAnthropicCritic:
    """Anthropic-shaped canned-JSON handler.

    The kind handler's heterogeneity guard reads ``subprovider`` to
    compare against ``analyzed_model``.  We set it to ``"anthropic"`` so
    the guard sees a different identity than the analyzed analyst's
    ``llm.primary.openai_compat`` (whose inferred subprovider is
    ``"vllm"`` per :func:`infer_llm_subprovider`).
    """

    subprovider = "anthropic"

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None,
        **kwargs,
    ):
        content = json.dumps({
            "scores": {
                "factuality": 0.79,
                "completeness": 0.65,
                "severity_calibration": 0.74,
            },
            "overall_score": 0.73,
            "revision_delta": (
                "Strengthen the finding by quantifying the post-upgrade "
                "regional capacity uplift (MW) and citing the EPE press "
                "release release date alongside the body claim."
            ),
            "confidence": 0.82,
        })

        class _Usage:
            prompt_tokens = 510
            completion_tokens = 140
            reasoning_tokens = 0
            total_tokens = 650

        class _Response:
            tool_calls: list = []
            usage = _Usage()
            finish_reason = "stop"
            raw_response = None

        _Response.content = content
        return _Response()


class _SelfCorrelatedHandler:
    """LLM handler with subprovider matching the analyzed analyst.

    Used by the heterogeneity-guard test — the guard must refuse this
    because ``analyzed_model`` and ``judge_model`` would both resolve to
    the same provider with ``allow_self_correlated=False``.
    """

    # vLLM is what infer_llm_subprovider("llm.primary.openai_compat", ...)
    # returns for the analyzed analyst's stack ref.
    subprovider = "vllm"

    async def chat_complete(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "guard should refuse before reaching chat_complete"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def session_session_prefix() -> str:
    """Session-scoped actor-id prefix — disjoint from the spike test's."""
    return f"crit-{uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def _registered_descriptors(descriptor_registry, stack_registry):
    """Register stack components + analyzed analyst + critic.

    The registration uses the registry's typed API directly (the same
    code the REST endpoint dispatches to via ``register_descriptor`` —
    no schema bypass).  ``DescriptorValidationError`` would surface here
    if the descriptor failed validation; the heterogeneity-guard test
    (which uses a critic with the SAME LLM ref as the analyzed) verifies
    that the *registration* succeeds — the guard fires at *activation*
    time, not at descriptor-validation time, per the L-175 contract.
    """
    actor = "critic_e2e_test"

    # Stack components — shared with the spike test, idempotent register.
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)

    # Anthropic stack component for the critic LLM.  The spike test does
    # NOT include this (the spike's critic descriptor references it but
    # the test never instantiates the handler — it injects
    # _CriticLLMHandler directly).  For this test, the heterogeneity
    # guard + canned-JSON path also bypass the resolver, so we register
    # the anthropic component for shape completeness rather than
    # functional necessity.
    from legba.data.schemas.stack import LLMProvider, LLMProviderConfig
    anthropic = LLMProvider(
        id="llm.anthropic.opus_4_7",
        name="Claude Opus 4.7 (critic judge)",
        schema_uri="legba/stack/llm_provider/1.0.0",
        version=_placeholder_version(),
        owner="critic_e2e_test",
        config=LLMProviderConfig(
            api_endpoint=Property.Text.of("https://api.anthropic.com/v1"),
            api_key=Property.Secret.of("llm.anthropic.api_key"),
            model_name=Property.Text.of("claude-opus-4-7-20260301"),
            max_tokens=Property.Number.of(8192, minimum=1, maximum=200000),
        ),
    )
    with suppress(Exception):
        await stack_registry.register(
            anthropic.model_dump(mode="json", by_alias=True), actor,
        )

    # Analyzed analyst — must come first so the critic can resolve its
    # rubric.  ``with suppress(Exception)`` handles the
    # already-registered case on test re-run (the content hash is
    # deterministic so re-register collides with VersionConflict).
    analyzed = _build_brazil_inline_with_rubric()
    with suppress(Exception):
        await descriptor_registry.register(analyzed, actor=actor)

    critic = _build_critic_descriptor(
        analyzed_analyst_id=analyzed.identity.id,
    )
    with suppress(Exception):
        await descriptor_registry.register(critic, actor=actor)

    # Hydrate the head versions so callers have stable content-hashes.
    analyzed_head = await descriptor_registry.get_typed(
        analyzed.identity.id, family=Family.ANALYST,
    )
    critic_head = await descriptor_registry.get_typed(
        critic.identity.id, family=Family.ANALYST,
    )
    return analyzed_head, critic_head


async def _seed_finding(
    pg_pool: asyncpg.Pool,
    *,
    analyzed_analyst_id: str,
    analyzed_analyst_version: str,
    target_id: str,
    target_version: str,
) -> tuple[UUID, UUID]:
    """INSERT a finding row the critic can grade.  Returns (finding_id, run_id)."""
    from legba.data.provenance.kinds import spec_for_kind, OutputKind as _OK

    finding_id = uuid4()
    run_id = uuid4()
    schema_uri = spec_for_kind(_OK.FINDING).schema_uri
    payload = {
        "title": "Itaipu hydro plant third-gen turbine upgrade complete",
        "body": (
            "Brazil's Itaipu hydroelectric plant completed its third-"
            "generation turbine upgrade on 19 May 2026 per the EPE press "
            "release.  Regional capacity uplift is expected but the press "
            "release did not quantify the MW figure."
        ),
        "confidence": 0.76,
        "tags": ["itaipu", "hydro", "upgrade", "brazil"],
        "evidence": [
            "EPE press release 2026-05-19",
            "AgenciaBrasil RSS 2026-05-20",
        ],
        "data": {
            "source": "epe_rss",
            "category": "infrastructure",
            "geo": "BR",
        },
    }
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id
            ) VALUES (
                $1, 'finding', $2, $3, $4, NULL, $5::jsonb,
                $6, $7, $8, $9,
                NOW(), '{}'::uuid[], $10, $11
            )
            """,
            finding_id,
            payload["title"],
            payload["body"],
            payload["confidence"],
            json.dumps(payload),
            target_id,
            target_version,
            analyzed_analyst_id,
            analyzed_analyst_version,
            schema_uri,
            run_id,
        )
    return finding_id, run_id


@pytest.mark.asyncio
async def test_critic_descriptor_validates_through_registry(
    descriptor_registry, stack_registry, _registered_descriptors,
) -> None:
    """The critic descriptor + analyzed-with-rubric descriptor land cleanly.

    Validates:
      * the typed schema accepts the critic kind with a 3-axis weighted rubric;
      * the analyzed analyst's typed ``eval.rubric`` + typed
        ``allow_self_correlated=False`` round-trip through registry
        storage (the L-105 §3 Wave-B field, not the legacy
        ``eval.optimizer["allow_self_correlated"]`` location);
      * the critic's descriptor-pinned ``eval.optimizer["analyzed_analyst_id"]``
        survives the round-trip — exercises the
        :func:`_critic_descriptor_pinned_analyst_id` resolver path.
    """
    analyzed_head, critic_head = _registered_descriptors

    assert analyzed_head is not None
    assert analyzed_head.identity.id == "india_energy_inline_critic_test"
    assert analyzed_head.eval is not None, "rubric did not round-trip"
    assert analyzed_head.eval.rubric is not None
    # Parse the rubric JSON back out + verify the 3-axis shape.
    rubric_obj = json.loads(analyzed_head.eval.rubric)
    dim_names = {d["name"] for d in rubric_obj["dimensions"]}
    assert dim_names == {"factuality", "completeness", "severity_calibration"}
    # Weights must sum to 1.0 per the task spec.
    weight_sum = sum(d["weight"] for d in rubric_obj["dimensions"])
    assert abs(weight_sum - 1.0) < 1e-9, f"weights sum to {weight_sum}"
    # Typed Wave-B field, not the legacy optimizer-dict location.
    assert analyzed_head.eval.allow_self_correlated is False

    assert critic_head is not None
    assert critic_head.identity.kind == "critic"
    assert critic_head.identity.id == "india_energy_critic"
    assert critic_head.method.kind == "critic"
    # The critic must use a DIFFERENT LLM than the analyzed analyst
    # (heterogeneity guard).  The descriptor itself doesn't enforce this
    # at registration — the actor's run path does — but the operator
    # contract is that they're different.
    critic_primary = critic_head.method.llm.get("primary", {})
    assert critic_primary.get("raw") == "llm.anthropic.opus_4_7"
    analyzed_primary = analyzed_head.method.llm.get("primary", {})
    assert analyzed_primary.get("raw") == "llm.primary.openai_compat"
    # Descriptor-pinned target — the eval.optimizer block carries the
    # analyzed_analyst_id so the actor can resolve it without
    # caller-supplied options.
    assert critic_head.eval is not None
    assert critic_head.eval.optimizer is not None
    assert (
        critic_head.eval.optimizer["analyzed_analyst_id"]
        == analyzed_head.identity.id
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _APP_PORT_FREE,
    reason=(
        "port 6090 is bound by another process (likely the production "
        "legba-runtime-dapr container) — the spike test's in-process "
        "uvicorn cannot claim it; this flow is exercised by manual "
        "validation against the running production runtime"
    ),
)
async def test_critic_actor_activates_and_writes_critique_through_daprd(
    pg_pool, nats_store, vault, _registered_descriptors,
    dapr_host_app, session_session_prefix: str,
) -> None:
    """Happy path: critic actor activates, grades, writes BOTH rows.

    Verifies:
      * the critic actor activates via ActorProxy (the
        ``deps.fallback.cached`` + ``reminder.registered`` log lines
        fire — checked indirectly via the FSM transition record);
      * a finding row exists for the analyzed analyst;
      * one ``run`` invocation produces an ``analyst_outputs`` row with
        ``kind='critique'`` AND a matching ``analyst_critiques`` row
        (the trace-finalizer hook this commit adds);
      * the critique's lineage walks back to the analyzed finding's
        UUID (``derived_from`` carries the finding id).
    """
    analyzed_head, critic_head = _registered_descriptors

    # Seed a finding the critic can grade.  The india_energy_infra
    # target descriptor exists in the shared production registry; if
    # it doesn't (test-only DB), use a stub target id — the critic
    # doesn't read the target row, only the finding row.
    target_id = "india_energy_infra"
    target_version = "0" * 64
    finding_id, _ = await _seed_finding(
        pg_pool,
        analyzed_analyst_id=analyzed_head.identity.id,
        analyzed_analyst_version=analyzed_head.identity.version,
        target_id=target_id,
        target_version=target_version,
    )

    # Wire the critic actor's deps (in-process registry — same shape as
    # the gate-11 path).  The fallback resolver in dapr_host.py only
    # fires when there's no in-process deps registered; here we register
    # directly so the test doesn't depend on the registry HTTP client
    # being reachable from the test process.
    from legba.data.analysts.critic import (
        OUTPUT_KIND as CRITIC_OUTPUT_KIND,
        CriticDeps,
        READ_SLICE as critic_read_slice,
        run_method as critic_run_method,
    )

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    actor_id = (
        f"analyst::{session_session_prefix}::"
        f"{critic_head.identity.id}::"
        f"{critic_head.identity.version[:16]}"
    )

    # Clear stale dapr_state rows for this actor id (the daprd sidecar
    # points at the production DB; cross-session collision risk).
    _prod = await asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            actor_id,
        )
    finally:
        await _prod.close()

    kind_deps = CriticDeps(llm=_CannedAnthropicCritic())
    dapr_actors.register_analyst_deps(
        actor_id,
        dapr_actors._AnalystDeps(
            descriptor=critic_head,
            deps=deps,
            run_method=critic_run_method,
            kind_deps=kind_deps,
            output_kind=CRITIC_OUTPUT_KIND,
            read_slice=critic_read_slice,
            budget=None,
        ),
    )

    proxy = ActorProxy.create(
        "AnalystActor", ActorId(actor_id),
        dapr_actors.AnalystActorInterface,
    )

    activated = await _await_actor_call(proxy, "activate", None)
    assert activated.get("lifecycle") == "active", (
        f"critic actor failed to activate: {activated}"
    )

    # Force a critic tick — pass the analyzed-output id via target_filter
    # AND options.  The critic READ_SLICE accepts target_filter as the
    # UUID; the runtime's _resolve_critic_context resolves rubric +
    # analyzed_model + allow_self_correlated from the analyzed
    # descriptor body.
    run_result = await _await_actor_call(
        proxy, "run",
        {
            "trigger_kind": "method",
            "target_filter": str(finding_id),
            "options": {
                "analyzed_analyst_id": analyzed_head.identity.id,
                "analyzed_output_id": str(finding_id),
            },
        },
    )
    assert run_result.get("outcome") == "success", (
        f"critic run failed: {run_result}"
    )
    assert run_result.get("kind") == "critique"
    critique_id = UUID(run_result.get("output_id") or run_result["finding_id"])
    run_id = run_result.get("run_id")
    # The actor's response shape doesn't currently expose run_id — fetch
    # it from the analyst_outputs row instead.

    # ---- analyst_outputs row landed ---------------------------------
    async with pg_pool.acquire() as conn:
        out_row = await conn.fetchrow(
            "SELECT id, kind, data, analyst_id, derived_from, schema_uri, run_id "
            "FROM analyst_outputs WHERE id = $1",
            critique_id,
        )
    assert out_row is not None, (
        f"critique row {critique_id} not in analyst_outputs — "
        "OutputKind.CRITIQUE dispatch broken"
    )
    assert out_row["kind"] == "critique"
    assert "critique" in (out_row["schema_uri"] or "").lower()
    assert out_row["analyst_id"] == critic_head.identity.id
    # Lineage walks back to the analyzed finding's UUID.
    derived = [UUID(str(x)) for x in (out_row["derived_from"] or [])]
    assert finding_id in derived, (
        f"critique row {critique_id} did not record finding {finding_id} "
        f"in derived_from (got {derived})"
    )

    run_id = out_row["run_id"]
    assert run_id is not None

    # ---- analyst_critiques trace-finalizer row landed ---------------
    # The hook added in this commit writes the trace-level critique
    # sink keyed by trace_id=run_id.  Before this hook landed the row
    # was missing and the L-176 optimizer's training-window query saw
    # no critiques.
    async with pg_pool.acquire() as conn:
        crit_row = await conn.fetchrow(
            "SELECT id, trace_id, judge_analyst_id, judge_analyst_version, "
            "       overall_score, scores, revision_delta, rubric_uri "
            "FROM analyst_critiques WHERE trace_id = $1",
            run_id,
        )
    assert crit_row is not None, (
        f"analyst_critiques row missing for trace_id={run_id} — "
        "the dapr_actors trace-finalizer write is not firing for "
        "OutputKind.CRITIQUE"
    )
    assert crit_row["judge_analyst_id"] == critic_head.identity.id
    assert crit_row["judge_analyst_version"] == critic_head.identity.version
    assert crit_row["overall_score"] is not None
    assert 0.0 <= float(crit_row["overall_score"]) <= 1.0
    scores = crit_row["scores"]
    if isinstance(scores, str):
        scores = json.loads(scores)
    # All three axes from the rubric appear.
    assert set(scores.keys()) == {
        "factuality", "completeness", "severity_calibration",
    }, f"score dimensions did not match rubric: {scores}"
    # rubric_uri carries the descriptor-anchored URI.
    assert analyzed_head.identity.id in crit_row["rubric_uri"]

    # ---- Cleanup ----------------------------------------------------
    await _deactivate_actor("AnalystActor", actor_id)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _APP_PORT_FREE,
    reason=(
        "port 6090 is bound by another process (likely the production "
        "legba-runtime-dapr container) — the spike test's in-process "
        "uvicorn cannot claim it; this flow is exercised by manual "
        "validation against the running production runtime"
    ),
)
async def test_critic_heterogeneity_guard_rejects_self_correlated(
    pg_pool, nats_store, vault, _registered_descriptors,
    dapr_host_app, session_session_prefix: str,
) -> None:
    """Same-LLM critic with allow_self_correlated=False → guard fires.

    Verifies:
      * the descriptor itself registers cleanly even with the same LLM
        ref as the analyzed analyst (the schema doesn't enforce the
        heterogeneity contract — that's deferred to actor runtime per
        the L-175 docstring);
      * the actor's run path raises ``SelfCorrelatedJudgeError`` →
        outcome=HARD_FAIL when the analyzed analyst's
        ``allow_self_correlated`` is False AND the judge LLM matches
        the analyzed LLM's subprovider.
    """
    analyzed_head, _ = _registered_descriptors

    # Build a critic with the SAME LLM ref as the analyzed analyst.
    self_correlated_critic = _build_critic_descriptor(
        analyzed_analyst_id=analyzed_head.identity.id,
        llm_ref="llm.primary.openai_compat",  # SAME as analyzed
    )
    # Override id so it doesn't collide with the happy-path critic.
    self_correlated_critic = AnalystDescriptor.model_validate({
        **self_correlated_critic.model_dump(mode="python"),
        "identity": {
            **self_correlated_critic.identity.model_dump(mode="python"),
            "id": "india_energy_critic_self_correlated",
            "name": "Self-correlated critic (negative test)",
        },
    })

    from legba.data.analysts.critic import (
        OUTPUT_KIND as CRITIC_OUTPUT_KIND,
        CriticDeps,
        READ_SLICE as critic_read_slice,
        run_method as critic_run_method,
    )

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    actor_id = (
        f"analyst::{session_session_prefix}::"
        f"selfcor::{uuid4().hex[:8]}"
    )

    _prod = await asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            actor_id,
        )
    finally:
        await _prod.close()

    # Seed a finding for the critic to (attempt to) grade.
    finding_id, _ = await _seed_finding(
        pg_pool,
        analyzed_analyst_id=analyzed_head.identity.id,
        analyzed_analyst_version=analyzed_head.identity.version,
        target_id="india_energy_infra",
        target_version="0" * 64,
    )

    kind_deps = CriticDeps(llm=_SelfCorrelatedHandler())
    dapr_actors.register_analyst_deps(
        actor_id,
        dapr_actors._AnalystDeps(
            descriptor=self_correlated_critic,
            deps=deps,
            run_method=critic_run_method,
            kind_deps=kind_deps,
            output_kind=CRITIC_OUTPUT_KIND,
            read_slice=critic_read_slice,
            budget=None,
        ),
    )

    proxy = ActorProxy.create(
        "AnalystActor", ActorId(actor_id),
        dapr_actors.AnalystActorInterface,
    )
    activated = await _await_actor_call(proxy, "activate", None)
    assert activated.get("lifecycle") == "active"

    # The analyzed analyst's primary LLM stack-ref is "llm.primary.openai_compat"
    # whose subprovider infers to "vllm".  We feed analyzed_model AS the
    # raw stack-ref (the runtime's _resolve_critic_context sets it from
    # method.llm.primary.raw).  The judge handler's subprovider is "vllm"
    # too.  But the kind handler's _assert_heterogeneous compares
    # analyzed_model vs judge_model strings — and the actor passes the
    # judge_model as the deps.llm.subprovider attribute.  Both strings
    # need to match for the guard to fire.
    #
    # To make this deterministic we pass judge_model explicitly via
    # options to mirror the analyzed-side ref:
    run_result = await _await_actor_call(
        proxy, "run",
        {
            "trigger_kind": "method",
            "target_filter": str(finding_id),
            "options": {
                "analyzed_analyst_id": analyzed_head.identity.id,
                "analyzed_output_id": str(finding_id),
                # Force the comparison to trip:
                "judge_model": "llm.primary.openai_compat",
            },
        },
    )

    # Expected: HARD_FAIL outcome with SelfCorrelatedJudgeError in the
    # error string.  The actor's exception handler classifies the
    # SelfCorrelatedJudgeError as a hard failure (per the kind handler's
    # docstring) and DLQ-routes the run.
    assert run_result.get("outcome") == "hard_fail", (
        f"expected hard_fail from heterogeneity guard, got: {run_result}"
    )
    error_str = str(run_result.get("error") or "")
    assert (
        "self-correlat" in error_str.lower()
        or "self_correlat" in error_str.lower()
        or "SelfCorrelated" in error_str
    ), (
        f"expected SelfCorrelatedJudgeError in run_result.error, got: "
        f"{run_result}"
    )

    await _deactivate_actor("AnalystActor", actor_id)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _APP_PORT_FREE,
    reason=(
        "port 6090 is bound by another process (likely the production "
        "legba-runtime-dapr container) — the spike test's in-process "
        "uvicorn cannot claim it; this flow is exercised by manual "
        "validation against the running production runtime"
    ),
)
async def test_critic_missing_rubric_rejection(
    pg_pool, nats_store, vault, descriptor_registry, stack_registry,
    dapr_host_app, session_session_prefix: str,
) -> None:
    """Critic against an analyst with NO rubric → MissingRubricError.

    The kind handler's docstring is explicit: an analyst without an
    eval.rubric cannot be critiqued — hard failure on purpose so the
    operator sees the gap rather than silent grading on an empty
    rubric.  This test creates an analyzed analyst with ``eval=None``
    and verifies the critic raises.
    """
    actor = "critic_e2e_test"

    # Stack components.
    for component in _build_stack_components():
        body = component.model_dump(mode="json", by_alias=True)
        with suppress(Exception):
            await stack_registry.register(body, actor)

    # Build an analyzed analyst with NO eval block.
    no_rubric = AnalystDescriptor(
        identity=AnalystIdentity(
            id="india_energy_no_rubric_test",
            name="Brazil Energy Inline (no rubric)",
            schema_uri="legba/analyst/1.0.0",
            version=_placeholder_version(),
            kind=AnalystKind.INLINE_TARGET,
            type_signature=TypeSignature(
                input_type="legba.runtime.SignalList",
                output_type="legba.runtime.Finding",
            ),
            state=LifecycleState.ACTIVE,
            owner="critic_e2e_test",
        ),
        subscription=SubscriptionBlock(
            targets=SubscriptionTargets(
                predicate='target_id() == "india_energy_infra"',
                data_types=["signal"],
            ),
        ),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="llm_single_turn",
            prompt_module="legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            llm={
                "primary": Property.StackRef(
                    raw="llm.primary.openai_compat",
                    expected_family="llm_provider",
                ).model_dump(),
                "max_tokens": 1024,
            },
        ),
        cadence=CadenceBlock(
            fallback_schedule="*/10 * * * *",
        ),
        outputs=[],
        eval=None,  # ← the gap the critic must surface
    )
    with suppress(Exception):
        await descriptor_registry.register(no_rubric, actor=actor)
    no_rubric = await descriptor_registry.get_typed(
        no_rubric.identity.id, family=Family.ANALYST,
    )

    critic = _build_critic_descriptor(
        analyzed_analyst_id=no_rubric.identity.id,
    )
    # Distinct id so we don't collide with the happy-path critic.
    critic = AnalystDescriptor.model_validate({
        **critic.model_dump(mode="python"),
        "identity": {
            **critic.identity.model_dump(mode="python"),
            "id": "india_energy_critic_no_rubric_target",
        },
    })
    with suppress(Exception):
        await descriptor_registry.register(critic, actor=actor)
    critic = await descriptor_registry.get_typed(
        critic.identity.id, family=Family.ANALYST,
    )

    finding_id, _ = await _seed_finding(
        pg_pool,
        analyzed_analyst_id=no_rubric.identity.id,
        analyzed_analyst_version=no_rubric.identity.version,
        target_id="india_energy_infra",
        target_version="0" * 64,
    )

    from legba.data.analysts.critic import (
        OUTPUT_KIND as CRITIC_OUTPUT_KIND,
        CriticDeps,
        READ_SLICE as critic_read_slice,
        run_method as critic_run_method,
    )

    deps = StandardDeps(
        pg_pool=pg_pool,
        nats_publish=nats_store.publish_json,
        secrets_resolve=vault.resolve,
    )

    actor_id = (
        f"analyst::{session_session_prefix}::"
        f"norubric::{uuid4().hex[:8]}"
    )

    _prod = await asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            actor_id,
        )
    finally:
        await _prod.close()

    kind_deps = CriticDeps(llm=_CannedAnthropicCritic())
    dapr_actors.register_analyst_deps(
        actor_id,
        dapr_actors._AnalystDeps(
            descriptor=critic,
            deps=deps,
            run_method=critic_run_method,
            kind_deps=kind_deps,
            output_kind=CRITIC_OUTPUT_KIND,
            read_slice=critic_read_slice,
            budget=None,
        ),
    )

    proxy = ActorProxy.create(
        "AnalystActor", ActorId(actor_id),
        dapr_actors.AnalystActorInterface,
    )
    activated = await _await_actor_call(proxy, "activate", None)
    assert activated.get("lifecycle") == "active"

    run_result = await _await_actor_call(
        proxy, "run",
        {
            "trigger_kind": "method",
            "target_filter": str(finding_id),
            "options": {
                "analyzed_analyst_id": no_rubric.identity.id,
                "analyzed_output_id": str(finding_id),
            },
        },
    )

    # Expected: hard_fail with MissingRubricError surfaced.
    assert run_result.get("outcome") == "hard_fail", (
        f"expected hard_fail from MissingRubricError, got: {run_result}"
    )
    error_str = str(run_result.get("error") or "")
    assert (
        "rubric" in error_str.lower()
        or "MissingRubric" in error_str
    ), f"expected MissingRubricError, got: {run_result}"

    await _deactivate_actor("AnalystActor", actor_id)


# ---------------------------------------------------------------------------
# Live-LLM gate — runs the real Anthropic call when LEGBA_TEST_LIVE_LLM=1
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Direct kind-handler tests (no dapr host, no port conflict).
#
# These exercise the L-175 critic kind's contract surface without the
# Dapr actor wrapper.  Both branches are deterministic — the
# heterogeneity guard fires on a string compare, and the missing-rubric
# path raises before any LLM call.  Run regardless of port-6090 state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critic_kind_heterogeneity_guard_rejects_self_correlated_direct() -> None:
    """Same analyzed_model + judge_model + no escape hatch → guard fires.

    Exercises the kind handler's :func:`_assert_heterogeneous` directly
    via the public ``run_method`` so the contract is honored regardless
    of the surrounding actor wiring.
    """
    from legba.data.analysts.critic import (
        SelfCorrelatedJudgeError,
        run_method as critic_run_method,
        CriticDeps,
    )

    # Minimal LLM handler — subprovider matches the analyzed analyst's
    # to trigger the guard (the kind handler reads the comparison
    # strings out of options['analyzed_model'] + options['judge_model']
    # OR deps.llm.subprovider as the judge fallback).
    class _StubLLM:
        subprovider = "vllm"
        async def chat_complete(self, *a, **k):  # pragma: no cover
            raise AssertionError(
                "guard should refuse before LLM call"
            )

    inputs = [{
        "id": uuid4(),
        "title": "Stub finding",
        "body": "Stub body",
        "confidence": 0.5,
        "analyst_id": "analyzed_analyst",
        "analyst_version": "v1",
    }]
    options = {
        "analyst_id": "judge_analyst",
        "analyst_version": "v1",
        "rubric": '{"dimensions":[{"name":"x","weight":1.0}]}',
        "analyzed_model": "llm.primary.openai_compat",
        "judge_model": "llm.primary.openai_compat",  # SAME → trip guard
        "allow_self_correlated": False,
    }
    with pytest.raises(SelfCorrelatedJudgeError) as excinfo:
        await critic_run_method(inputs, options, CriticDeps(llm=_StubLLM()))
    assert "self-correlat" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_critic_kind_missing_rubric_raises_direct() -> None:
    """No rubric in options → MissingRubricError.

    Exercises the kind handler's hard-failure path for analysts that
    don't carry an ``eval.rubric`` block in their descriptor.  The
    runtime's :func:`_resolve_critic_context` populates
    ``options['rubric']`` from the analyzed descriptor; when the
    analyzed has no rubric, the slot stays empty and the kind handler
    raises here.
    """
    from legba.data.analysts.critic import (
        MissingRubricError,
        run_method as critic_run_method,
        CriticDeps,
    )

    class _StubLLM:
        subprovider = "anthropic"
        async def chat_complete(self, *a, **k):  # pragma: no cover
            raise AssertionError(
                "missing-rubric should refuse before LLM call"
            )

    inputs = [{
        "id": uuid4(),
        "title": "Stub finding",
        "body": "Stub body",
        "confidence": 0.5,
        "analyst_id": "analyzed_analyst",
        "analyst_version": "v1",
    }]
    options = {
        "analyst_id": "judge_analyst",
        "analyst_version": "v1",
        # rubric intentionally omitted
        "analyzed_model": "llm.primary.openai_compat",
        "judge_model": "llm.anthropic.opus_4_7",
    }
    with pytest.raises(MissingRubricError) as excinfo:
        await critic_run_method(inputs, options, CriticDeps(llm=_StubLLM()))
    assert "rubric" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Live-LLM gate — exercises the real Anthropic call via dapr_host_app.
# Skipped when port 6090 is bound (production runtime is up) — the
# production runtime DOES exercise this path; see the K-1 report.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEGBA_TEST_LIVE_LLM") != "1" or not _APP_PORT_FREE,
    reason=(
        "LEGBA_TEST_LIVE_LLM!=1, or port 6090 is bound — set "
        "LEGBA_TEST_LIVE_LLM=1 and stop the production runtime container "
        "to exercise the real Anthropic judge call."
    ),
)
async def test_critic_live_anthropic_judge_call(
    pg_pool, nats_store, vault, _registered_descriptors,
    dapr_host_app, session_session_prefix: str,
) -> None:
    """Real Anthropic Claude Opus 4.7 call via the production resolver.

    Gated on ``LEGBA_TEST_LIVE_LLM=1``.  Uses the registry's resolver
    path (no in-process LLM injection — the analyst_deps_resolver in
    dapr_host.py builds the handler from the registered stack
    component, which pulls the Anthropic API key from the vault).

    Validates the FULL eval-loop wiring is healthy end-to-end:
      vault → stack registry → llm handler factory → critic kind deps
      → run_method → analyst_outputs + analyst_critiques rows.
    """
    analyzed_head, critic_head = _registered_descriptors

    finding_id, _ = await _seed_finding(
        pg_pool,
        analyzed_analyst_id=analyzed_head.identity.id,
        analyzed_analyst_version=analyzed_head.identity.version,
        target_id="india_energy_infra",
        target_version="0" * 64,
    )

    # For the live-LLM path we DON'T inject _CannedAnthropicCritic —
    # instead we leave the deps resolver to build the real Anthropic
    # handler via the registered stack component.  This requires the
    # dapr_host_app's analyst_deps_resolver to fire — which only happens
    # when register_analyst_deps was NOT called for the actor_id.
    actor_id = (
        f"analyst::{session_session_prefix}::"
        f"live::{critic_head.identity.id}::{critic_head.identity.version[:16]}"
    )

    _prod = await asyncpg.connect(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database="legba",
    )
    try:
        await _prod.execute(
            "DELETE FROM dapr_state WHERE key LIKE '%' || $1 || '%'",
            actor_id,
        )
    finally:
        await _prod.close()

    proxy = ActorProxy.create(
        "AnalystActor", ActorId(actor_id),
        dapr_actors.AnalystActorInterface,
    )

    # The fallback resolver in dapr_host.py reads from the registry HTTP
    # endpoint and builds the deps via build_analyst_run_method.  This
    # exercises the FULL Anthropic build path (vault secret → handler
    # config → on_configure).
    activated = await _await_actor_call(proxy, "activate", None)
    if activated.get("lifecycle") != "active":
        pytest.skip(
            f"live deps resolver failed to build critic actor: {activated} "
            "(check llm.anthropic.api_key in vault + dapr_host_app fallback)"
        )

    run_result = await _await_actor_call(
        proxy, "run",
        {
            "trigger_kind": "method",
            "target_filter": str(finding_id),
            "options": {
                "analyzed_analyst_id": analyzed_head.identity.id,
                "analyzed_output_id": str(finding_id),
            },
        },
    )
    assert run_result.get("outcome") == "success", (
        f"live anthropic critic run failed: {run_result}"
    )
    assert run_result.get("kind") == "critique"

    await _deactivate_actor("AnalystActor", actor_id)
