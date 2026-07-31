# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-kind analyst ``run_method`` + ``kind_deps`` builder (L-241).

The Dapr host (:mod:`legba.runtime.dapr_host`) registers a deps resolver
that constructs an :class:`legba.runtime.dapr_actors._AnalystDeps` bundle
on cache miss.  Three of that bundle's fields are kind-specific:

  * ``run_method`` — the kind module's module-level ``run_method``
    callable (or an ``AnalystRunFn``-shaped wrapper for the LLM-bearing
    backward-compat path);
  * ``kind_deps`` — the kind's deps dataclass / Protocol-shaped object
    threaded as the third positional argument to ``run_method`` (per
    L-102 §5);
  * ``output_kind`` — the :class:`OutputKind` enum value the analyst-
    output dispatcher writes for this kind (FINDING / PREDICTION /
    CRITIQUE / PROMPT_MODULE_CANDIDATE).

The runtime walks ``legba.data.analysts`` at startup via
:func:`legba.data.analysts.discover_analyst_kinds`; this module is the
adapter between that registry and the actor host.  Per-kind branches
live below.  Each branch:

  1. Reads the LLM stack-component reference out of the analyst
     descriptor's ``method.llm.primary`` slot (a
     :class:`Property.StackRef` dump — ``{"raw": "<id>", ...}``);
  2. Resolves it to a configured handler via
     :func:`build_llm_handler_from_stack_component` (skipped for kinds
     whose ``run_method`` doesn't need an LLM, like the optimizer);
  3. Constructs the kind's deps dataclass.

Lewis's no-stubs rule (see ``feedback_no_mocks.md``): a kind that
cannot be wired against today's substrate raises a clear
:class:`AnalystDepsBuildError`.  No silent no-ops.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, NamedTuple, Sequence
from urllib.parse import urlparse

import asyncpg

from ..data.analysts import KindHandler, discover_analyst_kinds
from ..data.provenance.kinds import OutputKind
from ..data.provenance.receipts import RuntimeReceiptChain
from ..data.schemas.analyst import AnalystDescriptor
from ..data.schemas.stack import LLMProviderConfig
from ..data.stack.llm import LLM_HANDLERS
from ..data.stack.llm.base import LLMProviderHandler, TelemetryHandle
from .deps import StandardDeps
from .receipt_chain_factory import build_receipt_chain_for_analyst
from .registry_client import RegistryClientError, RegistryHTTPClient

logger = logging.getLogger(__name__)


__all__ = [
    "AnalystDepsBuildError",
    "JUDGE_ROUTE_CONFIGURED",
    "JUDGE_ROUTE_FALLBACK_PRIMARY",
    "JUDGE_ROUTE_FALLBACK_VERIFY",
    "JUDGE_STACK_REF_ENV",
    "JudgeRoute",
    "build_analyst_run_method",
    "build_llm_handler_from_stack_component",
    "build_search_handler_from_stack_component",
    "infer_llm_subprovider",
    "resolve_judge_route",
    "resolve_judge_route_from_llm_block",
    "resolve_llm_budget_params",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AnalystDepsBuildError(RuntimeError):
    """Raised when the builder cannot construct deps for a kind.

    Surfaces:

      * Unknown analyst kind (descriptor's identity.kind not in the
        :func:`discover_analyst_kinds` registry).
      * Missing / malformed ``method.llm.primary`` StackRef when the
        kind requires an LLM.
      * Stack-component lookup failures (registry 404, transport).
      * Subprovider resolution failures (unrecognised id pattern AND
        unparseable endpoint).
      * Substrate-port gaps (e.g. ``consult_on_demand`` without a
        production :class:`SubstrateQueryPort` implementation).

    The actor host treats this as a hard failure — the actor's
    ``activate()`` returns an error so the operator sees the gap rather
    than the actor silently no-op-ing.
    """


# ---------------------------------------------------------------------------
# Public entry — the builder the host's deps resolver calls
# ---------------------------------------------------------------------------


async def build_analyst_run_method(
    descriptor: AnalystDescriptor,
    *,
    deps: StandardDeps,
    registry_client: RegistryHTTPClient,
    pg_pool: asyncpg.Pool,
    llm_handler_factory: (
        Callable[[str], Awaitable[LLMProviderHandler]] | None
    ) = None,
    temporal_client: Any | None = None,
    deep_consult_client: Any | None = None,
    substrate_query_port: Any | None = None,
    embedding_service: Any | None = None,
    qdrant_client: Any | None = None,
    nlp_client: Any | None = None,
    tools_registry: Mapping[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] | None = None,
    consult_agency_binding: Any | None = None,
    inline_target_agency_binding: Any | None = None,
    inline_target_budget_precheck: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[
    Callable[..., Any],
    Any | None,
    OutputKind,
    RuntimeReceiptChain,
    Callable[..., Any] | None,
]:
    """Build the kind-specific ``run_method`` + ``kind_deps`` + ``output_kind`` + ``receipt_chain`` + ``read_slice`` quint.

    Parameters
    ----------
    descriptor:
        The parsed :class:`AnalystDescriptor`. ``descriptor.identity.kind``
        selects the kind module via :func:`discover_analyst_kinds`.
    deps:
        :class:`StandardDeps` bundle (pg_pool / NATS publisher / secrets
        resolver / logger).  Passed through unmodified for kinds whose
        ``run_method`` consumes it directly (today: ``deterministic``).
    registry_client:
        :class:`RegistryHTTPClient` used to fetch the LLM stack-component
        config bound to ``descriptor.method.llm.primary``.
    pg_pool:
        Postgres pool — used to build the production
        :class:`SubstrateQueryPort` for ``consult_on_demand`` (when no
        explicit ``substrate_query_port`` is passed) and surfaced via
        the predictor deps (currently unused there but kept consistent
        with the per-kind shape).
    llm_handler_factory:
        Optional override.  When supplied this callable is invoked with
        the stack-component id and must return an already-configured
        :class:`LLMProviderHandler` instance (``on_configure`` already
        called).  Tests pass a closure over a hand-rolled handler;
        production passes ``None`` and gets the default path via
        :func:`build_llm_handler_from_stack_component`.
    temporal_client:
        Optional pre-built durable-workflow client for the optimizer kind
        (the slot name is historical).  ``None`` → the kind's
        :class:`legba.data.analysts.optimizer.OptimizerDeps.__post_init__`
        picks via
        :func:`legba.data.analysts.optimizer._resolve_workflow_client`
        (Dapr Workflow when env-gated + available, in-process otherwise).
    substrate_query_port:
        Optional pre-built :class:`SubstrateQueryPort` for the
        ``consult_on_demand`` kind.  Tests inject a stub; production
        callers leave ``None`` — the builder raises
        :class:`AnalystDepsBuildError` because no in-tree production
        port exists yet (see report).
    embedding_service:
        Optional hosted embedding client (L-114) the host builds once at
        bring-up from the ``embed.primary.openai_compat`` stack component.
        Threaded into the grounded-kind (``inline_target`` /
        ``journal_assessor``) grounding hook so the resolver carries it for
        the ``vector:world_context`` RAG (S5-T3); ``None`` when the embedding
        service wasn't provisioned (grounding stays vector-free).
    qdrant_client:
        Optional raw async Qdrant client the host built once at bring-up
        (``AsyncQdrantClient``). Threaded — alongside ``embedding_service`` —
        into the grounded-kind grounding hook so the S5-T3 opportunistic
        ``vector:world_context`` RAG can cosine-search the curated corpus at
        GROUND time. ``None`` (no vector store) keeps grounding on the structured
        substrate path (no BACKGROUND PRIORS block).
    nlp_client:
        Optional hosted-NLP client SOURCE the host built once at bring-up (the
        process-lifetime :class:`LazyNlpClient` holder for the
        ``nlp.local.legba_models`` stack component). Threaded — mirroring
        ``embedding_service`` for signal_embedder — onto the ``reenrich_ner``
        deterministic sub-handler's deps so its one-time NER-backfill sweep can
        re-run the LIVE translate-then-NER path over the pre-fix historical backlog.
        Resolved LAZILY inside ``_wire_reenrich_ner`` (only for that bound
        sub-handler), so no other analyst pays a registry round-trip for a client it
        never uses. ``None`` (NLP plane not provisioned) → the sweep no-ops per tick.

    Returns
    -------
    tuple
        ``(run_method, kind_deps, output_kind, receipt_chain, read_slice)``.

      * ``run_method`` — module-level callable from the kind module
        (3-arg ``(inputs, options, deps)`` per L-102, except
        ``inline_target`` whose runner is the 2-arg back-compat shape
        the spike's :class:`LLMAnalystRunner` codified).
      * ``kind_deps`` — kind-specific deps object (``None`` for
        inline_target since its runner closes over the bundle itself).
      * ``output_kind`` — the kind module's ``OUTPUT_KIND`` (FINDING by
        default; CRITIQUE / PREDICTION / PROMPT_MODULE_CANDIDATE for
        kinds that override).
      * ``receipt_chain`` — the per-analyst
        :class:`RuntimeReceiptChain` (L-107 §7). Hydrated lazily from
        ``analyst_traces`` on first ``record()``; the actor's run path
        calls ``record()`` after every successful analyst-output write
        so the chain links each run via ``prev_receipt_hash``. Cached
        per ``(pg_pool, analyst_id)`` in
        :mod:`legba.runtime.receipt_chain_factory` so the head pointer
        + per-analyst lock persist across runs of the same analyst.
      * ``read_slice`` — the kind's optional ``READ_SLICE`` adapter
        (None for kinds that consume the default 24h substrate slice;
        present for the L-175 critic kind which reads ONE analyst-output
        row by id).  The actor's run path dispatches through this when
        non-None, falling back to ``_read_substrate_slice`` otherwise.

    Raises
    ------
    AnalystDepsBuildError
        On any failure path (unknown kind, missing LLM ref, etc.).
    """
    kind = descriptor.identity.kind
    registry = discover_analyst_kinds()
    handler = registry.get(kind)
    if handler is None:
        raise AnalystDepsBuildError(
            f"unknown analyst kind {kind!r} — not present in "
            f"discover_analyst_kinds() (known kinds: {sorted(registry)!r})"
        )

    # Build a closure the per-kind branches call when they need an LLM. The
    # by-id form lets a kind resolve a SECOND handler (the per-phase LLM split —
    # journal §4.1: an Opus narrate handler alongside the gpt-oss/vLLM primary).
    async def _resolve_llm_component(component_id: str) -> LLMProviderHandler:
        if llm_handler_factory is not None:
            return await llm_handler_factory(component_id)
        if deps.secrets_resolve is None:
            raise AnalystDepsBuildError(
                f"analyst {descriptor.identity.id!r}: cannot resolve LLM "
                f"{component_id!r} — StandardDeps.secrets_resolve is None"
            )
        return await build_llm_handler_from_stack_component(
            component_id,
            registry_client=registry_client,
            secrets_resolve=deps.secrets_resolve,
        )

    async def _resolve_primary_llm() -> LLMProviderHandler:
        component_id = _primary_llm_component_id(descriptor)
        if not component_id:
            raise AnalystDepsBuildError(
                f"analyst {descriptor.identity.id!r} (kind={kind!r}) requires "
                "an LLM but method.llm.primary is unset / malformed"
            )
        return await _resolve_llm_component(component_id)

    # Per-kind branches.  Each returns the trio; the receipt chain is
    # tacked on at the top level so the per-kind branches stay focused
    # on the kind-specific wiring.
    if kind == "inline_target":
        trio = await _build_inline_target(
            descriptor, handler, _resolve_primary_llm, pg_pool=pg_pool,
            agency_binding=inline_target_agency_binding,
            budget_precheck=inline_target_budget_precheck,
            embedding_service=embedding_service,
            qdrant_client=qdrant_client,
        )
    elif kind == "cross_target_raw":
        trio = await _build_cross_target_raw(handler, _resolve_primary_llm)
    elif kind == "meta_findings_synthesizer":
        trio = await _build_meta_findings_synthesizer(
            descriptor, handler, _resolve_primary_llm,
        )
    elif kind == "cross_analyst_correlator":
        trio = await _build_cross_analyst_correlator(handler, _resolve_primary_llm)
    elif kind == "relationship_reifier":
        trio = await _build_relationship_reifier(
            descriptor, handler, _resolve_primary_llm, pg_pool=pg_pool, deps=deps,
        )
    elif kind == "competing_hypotheses":
        trio = await _build_competing_hypotheses(
            descriptor, handler, _resolve_primary_llm, pg_pool=pg_pool, deps=deps,
        )
    elif kind == "deterministic":
        trio = await _build_deterministic(
            descriptor, handler, deps, _resolve_primary_llm,
            embedding_service=embedding_service,
            nlp_client=nlp_client,
        )
    elif kind == "predictor":
        trio = await _build_predictor(descriptor, handler, _resolve_primary_llm)
    elif kind == "critic":
        trio = await _build_critic(
            descriptor,
            handler,
            _resolve_primary_llm,
            tools_registry=tools_registry,
        )
    elif kind == "optimizer":
        trio = _build_optimizer(handler, temporal_client=temporal_client)
    elif kind == "consult_on_demand":
        trio = await _build_consult_on_demand(
            handler,
            _resolve_primary_llm,
            substrate_query_port=substrate_query_port,
            agency_binding=consult_agency_binding,
            # F1 model picker: thread the by-id resolver so the kind's run_method
            # can build a per-request handler for the operator's chosen plane.
            # It is bound to the consult allowlist inside _build_consult_on_demand.
            resolve_llm_component=_resolve_llm_component,
        )
    elif kind == "deep_consult":
        trio = _build_deep_consult(
            descriptor, handler, deep_consult_client=deep_consult_client,
        )
    elif kind == "journal_assessor":
        # The journal runs on the in-actor llm_planner envelope (NOT deep_consult
        # — plan §4.1). It reuses the inline_target deps shape + GATHER loop but
        # emits a JournalPayload (off-chain). The GATHER binding (for the
        # journal_read pack) is wired by the host's §4.9-generalized gate and
        # threaded through the same `inline_target_agency_binding` channel.
        trio = await _build_journal_assessor(
            descriptor, handler, _resolve_primary_llm, pg_pool=pg_pool,
            agency_binding=inline_target_agency_binding,
            budget_precheck=inline_target_budget_precheck,
            resolve_llm_component=_resolve_llm_component,
            embedding_service=embedding_service,
            qdrant_client=qdrant_client,
        )
    elif kind == "entity_researcher":
        # E4 — the entity de-fragmentation analyst. Like relationship_reifier: a
        # global META kind that BOTH calls the LLM (merge adjudication on the
        # $0 core plane) AND reads/writes entity_profiles directly. deps carry
        # the resolved primary LLM + pg_pool + budget; merge_mode gates apply.
        trio = await _build_entity_researcher(
            descriptor, handler, _resolve_primary_llm, pg_pool=pg_pool, deps=deps,
        )
    elif kind == "signal_salience":
        # S-1 — the per-signal salience scorer. Like entity_researcher: a global
        # META sweep that BOTH calls the LLM (consequence scoring on the $0 core
        # plane) AND reads/writes signals directly (un-scored pool in,
        # signals.salience out). deps carry the resolved primary LLM + pg_pool +
        # budget; score_mode gates apply (dry-run default).
        trio = await _build_signal_salience(
            descriptor, handler, _resolve_primary_llm, pg_pool=pg_pool, deps=deps,
        )
    else:
        # Defensive — discover_analyst_kinds returned a kind we didn't
        # switch on. Happens only when a new kind lands without a
        # builder branch; surface the gap so the operator notices rather
        # than letting the actor silently take a wrong code path.
        raise AnalystDepsBuildError(
            f"analyst kind {kind!r} discovered by the registry but has no "
            f"builder branch in analyst_deps_builder — add one before binding "
            f"this analyst"
        )

    run_method, kind_deps, output_kind = trio
    receipt_chain = build_receipt_chain_for_analyst(
        descriptor.identity.id,
        descriptor.identity.version,
        pg_pool=pg_pool,
    )
    # Surface the per-kind READ_SLICE adapter (if any).  The actor's run
    # path dispatches through this for kinds that read from non-default
    # tables — today: the L-175 critic kind reads ONE row from
    # ``analyst_outputs`` by id rather than the 24h substrate-slice
    # default.  Without this surface the production resolver would
    # dispatch the critic through ``_read_substrate_slice`` (signals
    # table) and NOOP every run — that was the K-1 bug pre-fix.
    read_slice = handler.read_slice
    return run_method, kind_deps, output_kind, receipt_chain, read_slice


# ---------------------------------------------------------------------------
# Per-kind branches
# ---------------------------------------------------------------------------


def _resolve_prompt_module(spec: str | None) -> str | None:
    """Resolve a descriptor ``method.prompt_module`` ("module:attr" path) to its
    system-prompt string, or ``None`` when unset/unresolvable so the runner falls
    back to the kind default ``_SYSTEM_PROMPT``.

    ``legba.runtime.analyst_method:_DEFAULT_SYSTEM`` IS that default (aliased), so
    honouring it is a no-op for every existing inline_target analyst — only a
    genuinely custom prompt (e.g. world_assessor's ``_WORLD_ASSESSOR_SYSTEM``)
    changes the runtime prompt.
    """
    if not spec or ":" not in spec:
        return None
    import importlib

    mod_name, _, attr = spec.partition(":")
    try:
        value = getattr(importlib.import_module(mod_name), attr, None)
    except Exception:
        return None
    return value if isinstance(value, str) else None


async def _build_inline_target(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    pg_pool: "asyncpg.Pool | None" = None,
    agency_binding: Any | None = None,
    budget_precheck: Callable[[], Awaitable[bool]] | None = None,
    embedding_service: Any | None = None,
    qdrant_client: Any | None = None,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """inline_target — LLM-bearing finding kind (single-shot or agentic).

    Wraps the configured LLM handler in an
    :class:`InlineTargetRunner` (2-arg ``AnalystRunFn`` shape) and
    returns ``kind_deps=None`` so the actor dispatches via the legacy
    2-arg path (the runner closes over the bundle).  This matches the
    spike's wiring shape and keeps the LLMAnalystRunner adapter in
    play.

    S5 agentic GATHER: when ``agency_binding`` is supplied (the host wires it
    ONLY when the ``substrate_read`` pack resolves EFFECTIVE for this
    (assessor, target) — assessor grant ∩ target allow), the runner runs a
    bounded GATHER tool-call phase before synthesis. The round cap comes from
    ``method.gather.max_rounds`` (default 1); the soft latency guard uses
    ``method.timeout_seconds``. ``agency_binding=None`` → the legacy single-shot
    runner, byte-for-byte unchanged.
    """
    from ..data.analysts.inline_target import InlineTargetRunner

    llm = await resolve_llm()
    max_tokens = _read_method_llm_option(descriptor, "max_tokens", default=1024)
    temperature = _read_method_llm_option(descriptor, "temperature", default=0.2)
    # S5 GATHER round cap + latency guard (only consulted when agency_binding is
    # wired — otherwise inert). ``method.gather.max_rounds`` is a typed field
    # (GatherBlock, default 1); ``method.timeout_seconds`` is the P-1 invoke cap.
    gather = getattr(descriptor.method, "gather", None)
    max_rounds = int(getattr(gather, "max_rounds", 1) or 1) if gather is not None else 1
    invoke_timeout_seconds = float(
        getattr(descriptor.method, "timeout_seconds", 180) or 180
    )
    # Honour the descriptor's declared system prompt (previously decorative for
    # this kind). P2-T1 (unit-factory): a bounded reasoning unit is JUST a
    # descriptor — its OWN system prompt drives synthesis. Resolve a
    # ``method.prompt_module`` ("module:attr") to its prompt constant; when that
    # is unset/unresolvable, fall back to an INLINE ``method.system_prompt``
    # string (the unit's prompt text carried verbatim in the descriptor — no
    # Python module needed to author a new unit). Both unset → None → the runner
    # uses the kind default _SYSTEM_PROMPT.
    system_prompt = _resolve_prompt_module(
        getattr(descriptor.method, "prompt_module", None)
    )
    if system_prompt is None:
        inline_prompt = getattr(descriptor.method, "system_prompt", None)
        if isinstance(inline_prompt, str) and inline_prompt.strip():
            system_prompt = inline_prompt
    # Close the optimizer loop (#37): if an operator has promoted a GEPA
    # candidate for this analyst (data->>'promotion_gate' = 'promoted'), its
    # evolved instruction text becomes the live system prompt. Best-effort —
    # falls back to the baseline on any lookup issue. Takes effect when the
    # analyst's deps (re)build; promotion is a deliberate operator action.
    from ..data.analysts.optimizer import resolve_promoted_system_prompt

    system_prompt = await resolve_promoted_system_prompt(
        pg_pool, descriptor.identity.id, default=system_prompt,
    )
    # Keep the shared analytic-tradecraft preamble on the live prompt even when a
    # GEPA-promoted candidate replaced it (the candidate evolves the task block,
    # not the house standard). No-op in the normal case (the baseline constants
    # already carry it).
    from ..data.analysts._tradecraft import with_preamble_if_absent

    system_prompt = with_preamble_if_absent(system_prompt)
    # Tier-1 knowledge grounding (L-241): when the descriptor opts in
    # (``grounding.enabled``) and we hold a substrate pg_pool, install a hook
    # the runner calls per run to PREPEND a dated "AUTHORITATIVE CURRENT
    # CONTEXT" preamble (current officeholders / alliances about the target
    # geo + slice entities) — the fix for stale-cutoff models. Off (None) for
    # every analyst that doesn't declare an enabled block; degrade to None when
    # no pool is wired.
    grounding_hook = _build_grounding_hook(
        descriptor, pg_pool=pg_pool, embedder=embedding_service,
        qdrant=qdrant_client,
    )
    if agency_binding is not None:
        logger.info(
            "analyst_deps_builder.inline_target.gather_enabled analyst=%r "
            "max_rounds=%d invoke_timeout_s=%.0f — substrate_read pack is "
            "EFFECTIVE; the GATHER phase is engaged",
            descriptor.identity.id, max_rounds, invoke_timeout_seconds,
        )
    runner = InlineTargetRunner(
        llm,
        max_tokens=int(max_tokens),
        temperature=float(temperature),
        system_prompt=system_prompt,
        grounding_hook=grounding_hook,
        agency_binding=agency_binding,
        max_rounds=max_rounds,
        invoke_timeout_seconds=invoke_timeout_seconds,
        budget_precheck=budget_precheck,
    )
    return runner, None, handler.output_kind


async def _build_journal_assessor(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    pg_pool: "asyncpg.Pool | None" = None,
    agency_binding: Any | None = None,
    budget_precheck: Callable[[], Awaitable[bool]] | None = None,
    resolve_llm_component: (
        Callable[[str], Awaitable[LLMProviderHandler]] | None
    ) = None,
    embedding_service: Any | None = None,
    qdrant_client: Any | None = None,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """journal_assessor — Legba's first-person reflective voice (plan §4.8).

    The journal runs on the in-actor llm_planner envelope (NOT the deep_consult
    Dapr workflow — §4.1). It reuses the ``InlineTargetDeps`` bundle + the GATHER
    loop, but dispatches its OWN ``run_method`` (3-arg) which emits a
    ``JournalPayload`` off-chain. Returns the trio
    ``(run_method, InlineTargetDeps, OutputKind.JOURNAL)``.

    THE HEADLINE FIX (§4.2): the journal's system prompt is the JOURNAL persona
    (legba.prompts.journal_assessor:JOURNAL_SYSTEM) threaded VERBATIM — it is
    NEVER wrapped by ``_tradecraft.with_preamble`` / ``with_preamble_if_absent``
    (the JSON-only / BLUF / estimative anti-voice). That wrapping is what would
    quietly kill the voice; we author the journal-specific narrate contract
    inside JOURNAL_SYSTEM instead.

    PER-PHASE LLM SPLIT (§4.1): the heavy GATHER loop runs on the PRIMARY handler
    (``method.llm.primary`` → the gpt-oss / vLLM plane, ``Reasoning: high``) while the VOICE
    (the field-notes seam + NARRATE) runs on the SECOND handler resolved from the
    OPTIONAL ``method.llm.narrate`` ref (→ Opus). When ``method.llm.narrate`` is
    absent (or no ``resolve_llm_component`` is supplied), ``llm_narrate`` stays
    None and the voice falls back to the primary handler — the prior behavior,
    byte-for-byte. ``method.max_tokens`` now governs ONLY the Opus narrate output
    (the gpt-oss/vLLM gather plane never receives max_tokens — vLLM serves its own
    budget, vllm.py:119), so it is threaded as the narrate cap; it is inert on the
    primary handler's gather calls.
    """
    from ..data.analysts.inline_target import InlineTargetDeps

    llm = await resolve_llm()
    max_tokens = _read_method_llm_option(descriptor, "max_tokens", default=4096)
    temperature = _read_method_llm_option(descriptor, "temperature", default=0.2)
    # PER-PHASE LLM SPLIT — resolve the OPTIONAL second (narrate) handler. Present
    # only when the descriptor sets ``method.llm.narrate.raw`` AND a by-id resolver
    # was threaded. Absent → None → the voice falls back to the primary handler
    # (zero-regression). ``gather_reasoning_high`` is set IFF the split is active
    # (the journal's heavy gather plane is the gpt-oss/vLLM plane, which honors the directive);
    # without a split there is no separate gather plane, so leave it off.
    narrate_component_id = _narrate_llm_component_id(descriptor)
    llm_narrate: LLMProviderHandler | None = None
    narrate_max_tokens: int | None = None
    gather_reasoning_high = False
    if narrate_component_id and resolve_llm_component is not None:
        llm_narrate = await resolve_llm_component(narrate_component_id)
        # method.max_tokens now caps ONLY the Opus narrate output (the gather plane
        # serves its own vLLM budget); thread it as the narrate cap.
        narrate_max_tokens = int(max_tokens)
        gather_reasoning_high = True
        logger.info(
            "analyst_deps_builder.journal_assessor.per_phase_split analyst=%r "
            "gather_plane=%r voice_plane=%r — GATHER on the gpt-oss/vLLM plane (Reasoning:high), "
            "voice on the narrate handler",
            descriptor.identity.id,
            _primary_llm_component_id(descriptor),
            narrate_component_id,
        )
    gather = getattr(descriptor.method, "gather", None)
    max_rounds = int(getattr(gather, "max_rounds", 1) or 1) if gather is not None else 1
    invoke_timeout_seconds = float(
        getattr(descriptor.method, "timeout_seconds", 180) or 180
    )
    # Resolve the journal persona system prompt. DELIBERATELY NOT wrapped by
    # with_preamble — that is the §4.2 headline fix. Unset/unresolvable → None →
    # the run_method falls back to the kind default (which it loads itself).
    system_prompt = _resolve_prompt_module(
        getattr(descriptor.method, "prompt_module", None)
    )
    if system_prompt is None:
        # Belt-and-braces: load the persona directly so a misconfigured
        # prompt_module never silently strips the entire voice (§4.2 caution).
        try:
            from legba.prompts.journal_assessor import JOURNAL_SYSTEM
            system_prompt = JOURNAL_SYSTEM
        except Exception:  # pragma: no cover — import guard
            system_prompt = None
    # The journal may still opt into Tier-1 grounding (it's a META analyst over
    # the global slice) — the GROUND preamble corrects stale-cutoff drift before
    # it narrates (§4.5 point 3). Off (None) unless the descriptor opts in.
    grounding_hook = _build_grounding_hook(
        descriptor, pg_pool=pg_pool, embedder=embedding_service,
        qdrant=qdrant_client,
    )
    if agency_binding is not None:
        logger.info(
            "analyst_deps_builder.journal_assessor.gather_enabled analyst=%r "
            "max_rounds=%d invoke_timeout_s=%.0f — journal_read pack is "
            "EFFECTIVE; the GATHER phase is engaged",
            descriptor.identity.id, max_rounds, invoke_timeout_seconds,
        )
    kind_deps = InlineTargetDeps(
        llm=llm,
        max_tokens=int(max_tokens),
        temperature=float(temperature),
        system_prompt=system_prompt,
        llm_narrate=llm_narrate,
        narrate_max_tokens=narrate_max_tokens,
        grounding_hook=grounding_hook,
        agency_binding=agency_binding,
        max_rounds=max_rounds,
        invoke_timeout_seconds=invoke_timeout_seconds,
        budget_precheck=budget_precheck,
        gather_reasoning_high=gather_reasoning_high,
    )
    # handler.run_method is the journal's 3-arg run_method (inputs, options, deps).
    return handler.run_method, kind_deps, handler.output_kind


def _build_grounding_hook(
    descriptor: AnalystDescriptor,
    *,
    pg_pool: "asyncpg.Pool | None",
    embedder: Any | None = None,
    qdrant: Any | None = None,
) -> Callable[..., Awaitable[Any]] | None:
    """Build the per-run grounding hook for an opted-in analyst, else None.

    Returns ``None`` (no grounding) unless the descriptor declares
    ``grounding.enabled: true`` AND a substrate ``pg_pool`` is available. The
    hook closes over a :class:`SubstrateGroundingResolver` + the descriptor's
    grounding config; per run it (a) extracts candidate target-geo + slice
    entity names from the read slice, (b) resolves their CURRENT authoritative
    facts/nexuses, and (c) renders the dated preamble. All-or-nothing
    degrade-not-drop is owned by the resolver/runner (a read failure → no
    preamble), so this builder never raises on the grounding path.

    ``embedder`` (L-114) + ``qdrant`` (S5-T3) are the hosted embedding client +
    the raw async Qdrant client the host built at bring-up. Threaded into the
    resolver so the opportunistic ``vector:world_context`` RAG can cosine-search
    the curated corpus at GROUND time and render a SEPARATE "BACKGROUND PRIORS
    (context, not evidence — do not cite)" block BELOW the authoritative
    preamble. When either is ``None`` (the vector plane wasn't provisioned) the
    RAG degrades to no block — grounding stays on the structured substrate path.
    """
    grounding = getattr(descriptor, "grounding", None)
    if grounding is None or not getattr(grounding, "enabled", False):
        return None
    if pg_pool is None:
        logger.warning(
            "analyst_deps_builder.grounding.no_pool analyst=%r — grounding "
            "declared enabled but no substrate pg_pool available; skipping",
            descriptor.identity.id,
        )
        return None

    from ..data.analysts.inline_target import (
        GROUNDING_QUESTION_SINK_KEY,
        GROUNDING_RAG_CHUNK_SINK_KEY,
        GROUNDING_RAG_STATS_SINK_KEY,
    )
    from ..data.config import QdrantConfig
    from .grounding import (
        SubstrateGroundingResolver,
        build_graph_structure_block,
        build_grounding_preamble,
        build_narratives_block,
        build_open_questions_block,
        build_situations_block,
        build_world_context_block,
        collect_grounding_candidates,
        situation_scope_for_target,
        target_country_name,
        world_context_min_score,
    )
    from .rag_rollback import is_world_context_enabled

    # L-114 / S5-T3: thread the hosted embedding client + the raw Qdrant client
    # (when the host built them) into the resolver so the opportunistic
    # ``vector:world_context`` RAG can run; the STRUCTURED reads (facts / nexuses
    # / situations / graph-structure) stay vector-free. The collection the Lane-4
    # loader wrote the corpus to is the config's ``world_context_collection`` (so
    # an env override is honored, not just the "world_context" default).
    world_context_collection = QdrantConfig.from_env().world_context_collection
    resolver = SubstrateGroundingResolver(
        pg_pool=pg_pool,
        embedder=embedder,
        qdrant=qdrant,
        world_context_collection=world_context_collection,
    )
    scope = list(getattr(grounding, "scope", None) or [])
    sources = list(getattr(grounding, "sources", None) or [])
    static_candidates = list(getattr(grounding, "static_candidates", None) or [])
    max_facts = int(getattr(grounding, "max_facts", 30) or 30)
    want_substrate = "substrate" in sources
    want_situations = "situations" in sources
    # graph_structure = the SEPARATE "ASSESSED STRUCTURE" block — the knowledge
    # graph's own interesting structures (tense actors / brokers / proxy chains)
    # from the structural_balance + graph_mining metrics, analysis-derived. This
    # is the consume-side of "use the graph in analysis to find interesting edges".
    want_graph_structure = "graph_structure" in sources
    # narratives = the SEPARATE "ASSESSED NARRATIVES" block — the reified
    # contested-claim families (mig 0102, narrative_mapper) for the run's target
    # scope, analysis-derived + detect-only (the block header carries the
    # echo-is-descriptive-not-causal honesty contract). Primary consumer: the
    # narrative_coordination unit. Empty sidecar ⇒ no block (honest empty).
    want_narratives = "narratives" in sources
    # S5-T3 opportunistic RAG — the curated ``world_context`` vector corpus,
    # queried semantically and rendered as the non-citable BACKGROUND PRIORS
    # block. Needs BOTH an embedder AND a Qdrant client; log the gap once at build
    # time so a descriptor that opts in without a provisioned vector plane is
    # observable (the run then degrades to no block).
    want_world_context = "vector:world_context" in sources
    # R-1 — the STANDING OPEN QUESTIONS block (the corpus_researcher backlog
    # source): the bounded, deterministically-ordered standing question set,
    # rendered SEPARATELY from ground truth, telling the analyst to PREFER
    # answering one of these over self-selecting a topic. See
    # ``grounding.SubstrateGroundingResolver.resolve_open_questions`` for the
    # ranking + ``build_open_questions_block`` for the render.
    want_open_questions = "open_questions" in sources
    # AUTO-ROLLBACK KILL-SWITCH (M22 — FIX A: per-run authoritative). A unit rolled
    # back off (via the persisted rag_rollback state or the
    # LEGBA_WORLD_CONTEXT_DISABLED_UNITS env pin) gets NO world_context block even
    # though its descriptor still lists the source — the flip is reverted IN CODE,
    # with no live descriptor PUT / redeploy. CRITICAL: this must NOT be baked into
    # the cached grounding-hook closure here at BUILD time — per-target deps live in
    # _ANALYST_DEPS and are evicted only on a descriptor-version change / process
    # restart, and record_rollback triggers no eviction, so a build-time bake would
    # keep injecting until an unrelated restart (the guard would be as inert as the
    # comments-only one). The authoritative check is re-read EACH RUN inside the hook
    # below; the read here is a fast-path LOG only (it does NOT set
    # want_world_context, so a later re-enable resumes injection on the same closure).
    ws_analyst_id = descriptor.identity.id
    if want_world_context and not is_world_context_enabled(ws_analyst_id):
        logger.info(
            "analyst_deps_builder.grounding.world_context_rolled_back_at_build "
            "analyst=%r — currently in the rag_rollback disabled set; the BACKGROUND "
            "PRIORS block is suppressed (re-checked per run, so this can change "
            "without a restart)",
            ws_analyst_id,
        )
    if want_world_context and (embedder is None or qdrant is None):
        logger.info(
            "analyst_deps_builder.grounding.world_context_unavailable analyst=%r "
            "embedder=%s qdrant=%s — grounding declares vector:world_context but "
            "the vector plane is not fully wired; the RAG block degrades to absent",
            descriptor.identity.id,
            "wired" if embedder is not None else "absent",
            "wired" if qdrant is not None else "absent",
        )
    # A descriptor that declares NONE of the wired sources resolves nothing —
    # surface that rather than silently injecting an empty preamble.
    if not (
        want_substrate
        or want_situations
        or want_graph_structure
        or want_narratives
        or want_world_context
        or want_open_questions
    ):
        logger.info(
            "analyst_deps_builder.grounding.no_wired_source analyst=%r "
            "sources=%r — only 'substrate' (facts/nexuses), 'situations', "
            "'graph_structure', 'narratives', 'vector:world_context', and "
            "'open_questions' are wired; no preamble built",
            descriptor.identity.id, sources,
        )

    # M22 — the FOCUSED RAG query THEME. The pre-M22 query led with the unit's
    # display NAME ("Leadership-Transition Risk Unit") and appended the noisy
    # slice-entity pile (person/org names the officeholder-stripped corpus never
    # contains), which diluted the geo/topic anchor and kept on-target cosines
    # below the floor. The theme is instead a corpus-facet phrase (government /
    # military / economy / society) from the descriptor's grounding.rag_theme, or a
    # cleaned form of its name — combined at run time with the target country into a
    # tight "<country> <theme>" query. Computed once at build time.
    rag_theme = _rag_theme_for_descriptor(descriptor)

    async def _hook(
        inputs: list[Mapping[str, Any]],
        options: Mapping[str, Any],
    ) -> str | None:
        target_id = options.get("target_id")
        parts: list[str] = []
        # Candidate entity set (target geo + slice entities + static) — computed
        # once and shared by the substrate and graph-structure resolvers.
        _candidates: list[str] | None = None

        def candidates() -> list[str]:
            nonlocal _candidates
            if _candidates is None:
                _candidates = collect_grounding_candidates(
                    inputs, target_id=target_id, scope=scope,
                    static_candidates=static_candidates,
                )
            return _candidates

        # R-1 — STANDING OPEN QUESTIONS block (the corpus_researcher backlog
        # source), rendered FIRST so the backlog directive is the first thing
        # the analyst reads (the supporting ground-truth/situations/etc.
        # blocks below are context for the run, not the assignment itself).
        # Fail-safe: any resolver error already degrades to [] inside
        # resolve_open_questions (never raises here); an empty/absent block
        # leaves the sink untouched (stays {}), so the analyst falls back to
        # self-selection — BYTE-IDENTICAL to its behavior before this source
        # existed (requirement: empty backlog -> unchanged fallback).
        if want_open_questions:
            questions = await resolver.resolve_open_questions(limit=max_facts)
            oq_block = build_open_questions_block(questions)
            if oq_block:
                parts.append(oq_block)
                # Fill the tag -> question sink (SAME order as the render, so
                # "Q1"/"Q2"/... match the rendered tags exactly) for REFLECT
                # to resolve the model's ``addressed_question`` answer against.
                oq_sink = options.get(GROUNDING_QUESTION_SINK_KEY)
                if isinstance(oq_sink, dict):
                    for i, q in enumerate(questions, start=1):
                        oq_sink[f"Q{i}"] = {
                            "id": str(q.id),
                            "produced_at": (
                                q.produced_at.isoformat()
                                if isinstance(q.produced_at, datetime)
                                else None
                            ),
                            "harvest_class": q.harvest_class,
                        }

        # Ground-truth block (facts + signed nexuses), provenance-gated.
        if want_substrate:
            facts, nexuses = await resolver.resolve(candidates(), max_facts=max_facts)
            preamble = build_grounding_preamble(facts, nexuses)
            if preamble:
                parts.append(preamble)
        # ASSESSED SITUATIONS block — analysis-derived ongoing frames, rendered
        # SEPARATELY and clearly fenced off from the ground-truth block (the
        # Phase-5 operator decision: situations never ground as fact).
        if want_situations:
            situations = await resolver.resolve_situations(
                scope_target_id=situation_scope_for_target(target_id),
                limit=max_facts,
            )
            block = build_situations_block(situations)
            if block:
                parts.append(block)
        # ASSESSED NARRATIVES block — the reified contested-claim families for
        # this run's target scope (per-country runs scope by subject geo-name
        # match; meta runs read the global recency top). Analysis-derived +
        # detect-only, fenced off from ground truth; empty sidecar ⇒ no block.
        if want_narratives:
            narratives = await resolver.resolve_narratives(
                target_id=target_id, limit=max_facts,
            )
            block = build_narratives_block(narratives)
            if block:
                parts.append(block)
        # ASSESSED STRUCTURE block — the knowledge graph's interesting structures
        # (tense actors / brokers / proxy chains), also analysis-derived + fenced
        # off from ground truth. Scoped to the candidate entities.
        if want_graph_structure:
            # Scope the ASSESSED STRUCTURE block to the RUN's target on a
            # per-country run (D4 contamination fix — the global "US is the most
            # central node" must not override a country slice). A META / no-target
            # run passes target_id=None and keeps the GLOBAL structure unchanged.
            structure = await resolver.resolve_graph_structure(
                candidates(), limit=max_facts, scope_target_id=target_id,
            )
            block = build_graph_structure_block(structure)
            if block:
                parts.append(block)
        # BACKGROUND PRIORS block (S5-T3 opportunistic RAG) — appended LAST so it
        # sits BELOW the authoritative preamble (and the other fenced blocks). The
        # query is the bounded question + target country + top slice entities, all
        # available here at GROUND time. Non-citable prior; an empty collection →
        # no block (no fabricated header). The retrieved chunk ids are recorded
        # into the caller's trace sink (when supplied) for auditable retrieval.
        # PER-RUN KILL-SWITCH (M22 FIX A) — AUTHORITATIVE. Re-read the disabled set
        # EACH run (a cheap env read + a small state-file stat/read) so a rollback
        # fired by rag_watch --enforce / an auto-trigger (writing the rag_rollback
        # state) OR the env pin suppresses injection on the VERY NEXT run, with NO
        # restart or deps eviction. Fail-safe by construction: is_world_context_enabled
        # returns True unless the unit is EXPLICITLY in the disabled set — a missing /
        # malformed state yields no disable signal → enabled; a unit present in the set
        # → suppressed (fail-closed for an explicitly-disabled unit).
        if want_world_context and is_world_context_enabled(ws_analyst_id):
            # FOCUSED query (M22): "<target country> <theme>" for a per-country
            # desk (its Factbook-background chunks are the retrieval target, further
            # scoped by the per-desk country filter); theme + top geo terms for a
            # meta / no-country run. NO unit-name noise, NO person-entity pile.
            country_name = target_country_name(target_id)
            geo_terms = () if country_name else candidates()[:2]
            query = _world_context_query(
                theme=rag_theme, country_name=country_name, geo_terms=geo_terms,
            )
            # Thread the run's target_id so the resolver can apply the per-desk
            # country filter (a single-country desk retrieves only its own
            # country's chunks; a meta / no-target / non-single-country run applies
            # NO filter). The relevance floor is applied resolver-side regardless.
            chunks = await resolver.resolve_world_context(
                query, limit=max_facts, target_id=target_id,
            )
            # M22 instrumentation — the retrieval measurement #179 needs. Record the
            # retained-chunk count + top retained cosine + the active floor into the
            # per-run stats sink (folded into the ground trace event), so the RAG
            # retrieval distribution is auditable run-over-run even when 0 chunks
            # clear the floor (retained=0 = retrieved-but-all-below-floor / empty).
            stats = options.get(GROUNDING_RAG_STATS_SINK_KEY)
            if isinstance(stats, dict):
                stats["world_context_retained"] = len(chunks)
                scores = [c.score for c in chunks if c.score is not None]
                if scores:
                    stats["world_context_top_score"] = round(max(scores), 4)
                stats["world_context_min_score"] = round(world_context_min_score(), 4)
            block = build_world_context_block(chunks)
            if block:
                parts.append(block)
                sink = options.get(GROUNDING_RAG_CHUNK_SINK_KEY)
                if isinstance(sink, list):
                    sink.extend(c.chunk_id for c in chunks)
        return "\n".join(parts) if parts else None

    return _hook


def _world_context_query(
    *, theme: str, country_name: str | None, geo_terms: Sequence[str] = (),
) -> str:
    """Assemble the FOCUSED ``vector:world_context`` RAG query string (M22).

    A tight natural-language phrase keyed on the curated country-background
    corpus's actual facets, NOT the pre-M22 "<unit name> — <slice-entity pile>"
    query. For a PER-COUNTRY desk the target country leads (``"<country> <theme>"``
    — e.g. ``"Iran government structure, political system, and leadership"``); its
    Factbook chunks are the retrieval target and the per-desk country filter scopes
    retrieval further. For a META / no-country run the theme leads, followed by up
    to 2 top geo terms. Returns ``""`` when there is nothing to query on (→ no RAG
    block).

    Live probe (M22, 293-point corpus): the focused phrase clears the recalibrated
    floor on-target (~0.60-0.66) and holds off-target well below (~0.40-0.43),
    where the old diluted query sat at ~0.47-0.59 (below the 0.65 floor → 0/81
    injections). The person-entity pile is DROPPED entirely — the officeholder-
    stripped corpus contains no people, so those terms only pulled the centroid off
    the geo/topic anchor.
    """
    theme = (theme or "").strip()
    if country_name and country_name.strip():
        return f"{country_name.strip()} {theme}".strip()
    # meta / no-country: theme + up to 2 top geo terms (a country desk is the
    # common case; a meta run keeps the query keyed on the theme + whatever geo the
    # slice surfaced).
    terms: list[str] = [theme] if theme else []
    for g in geo_terms:
        if isinstance(g, str) and g.strip():
            terms.append(g.strip())
        if len(terms) >= 3:
            break
    return " ".join(terms).strip()


# Tokens stripped from a descriptor NAME when deriving a fallback RAG theme (the
# unit's abstract risk label — "Risk Unit", "Watch" — is noise against the corpus,
# which is factual country background). Used only when grounding.rag_theme is unset.
_THEME_STOPWORDS: frozenset[str] = frozenset(
    {"unit", "risk", "watch", "the", "a", "an", "and"}
)


def _clean_theme_from_name(name: str) -> str:
    """Derive a corpus-friendly theme phrase from a descriptor NAME (fallback).

    "Leadership-Transition Risk Unit" → "leadership transition"; "Internal-Stability
    / Coup-Risk Unit" → "internal stability coup". Splits on ``-`` / ``/`` /
    whitespace and drops the abstract-label stopwords. Best-effort — a descriptor
    that cares should set an explicit ``grounding.rag_theme``.
    """
    lowered = re.sub(r"[-/]", " ", (name or "").lower())
    tokens = [t for t in re.split(r"\s+", lowered) if t and t not in _THEME_STOPWORDS]
    return " ".join(tokens).strip()


def _rag_theme_for_descriptor(descriptor: Any) -> str:
    """The ``vector:world_context`` query theme for a descriptor: the explicit
    ``grounding.rag_theme`` when set, else a cleaned form of the descriptor name."""
    grounding = getattr(descriptor, "grounding", None)
    theme = getattr(grounding, "rag_theme", None)
    if isinstance(theme, str) and theme.strip():
        return theme.strip()
    name = (
        getattr(descriptor.identity, "name", None)
        or getattr(descriptor.identity, "id", None)
        or ""
    )
    return _clean_theme_from_name(name)


async def _build_cross_target_raw(
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """cross_target_raw — multi-target finding kind.

    3-arg ``run_method``; ``kind_deps`` is a duck-typed bundle exposing
    the ``llm`` attribute (the kind module's ``CrossTargetDeps``
    Protocol).  We use a lightweight ``_CrossTargetDeps`` wrapper rather
    than importing the Protocol — the Protocol isn't a dataclass, and
    we want a stable hashable carrier for the LLM handle.
    """
    llm = await resolve_llm()
    return (
        handler.run_method,
        _LLMOnlyDeps(llm=llm),
        handler.output_kind,
    )


async def _build_meta_findings_synthesizer(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """meta_findings_synthesizer — second-order synthesis kind.

    Same shape as :func:`_build_cross_target_raw`: the kind's
    ``MetaFindingsDeps`` Protocol asks only for an ``llm`` attribute.

    2026-07-24 sampling-audit fix: the descriptor's OPTIONAL
    ``method.llm.temperature`` is threaded through the carrier (the unit
    inline_target path already honored it; this kind silently ignored it).
    Same precedence as the units — descriptor value when set, else the kind
    module's own ``DEFAULT_TEMPERATURE`` (the carrier stays ``None`` so the
    kind's fallback is authoritative, never mirrored here).
    """
    llm = await resolve_llm()
    temperature = _read_method_llm_option(descriptor, "temperature", default=None)
    return (
        handler.run_method,
        _LLMOnlyDeps(
            llm=llm,
            temperature=(None if temperature is None else float(temperature)),
        ),
        handler.output_kind,
    )


async def _build_cross_analyst_correlator(
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """cross_analyst_correlator — analyst-output-row correlator kind.

    Constructs the kind's own :class:`CrossAnalystCorrelatorDeps`
    dataclass so the kind-internal default knobs (max_tokens=2048,
    temperature=0.1) hold without us mirroring them here.
    """
    from ..data.analysts.cross_analyst_correlator import CrossAnalystCorrelatorDeps

    llm = await resolve_llm()
    return (
        handler.run_method,
        CrossAnalystCorrelatorDeps(llm=llm),
        handler.output_kind,
    )


async def _build_relationship_reifier(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    pg_pool: "asyncpg.Pool | None" = None,
    deps: StandardDeps,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """relationship_reifier — the PIECE A reified-typed-Nexus producer.

    META analyst kind that BOTH calls the LLM (typing) AND reads/writes the
    substrate directly (candidate pairs + side-written nexus rows). So its
    ``kind_deps`` carries the resolved LLM, the pg_pool, and the budget
    reporter (the descriptor declares ``method.llm.primary`` +
    ``budget_tokens_per_day``; the kind honors the envelope per-call).
    """
    from ..data.analysts.relationship_reifier import ReifierDeps

    llm = await resolve_llm()
    max_tokens = _read_method_llm_option(
        descriptor, "max_tokens", default=ReifierDeps.max_tokens,
    )
    temperature = _read_method_llm_option(
        descriptor, "temperature", default=ReifierDeps.temperature,
    )
    max_candidates = _read_method_llm_option(
        descriptor, "max_candidates", default=ReifierDeps.max_candidates,
    )
    return (
        handler.run_method,
        ReifierDeps(
            llm=llm,
            pg_pool=pg_pool,
            budget=getattr(deps, "budget", None),
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            max_candidates=int(max_candidates),
        ),
        handler.output_kind,
    )


async def _build_entity_researcher(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    pg_pool: "asyncpg.Pool | None" = None,
    deps: StandardDeps,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """entity_researcher (E4) — the entity de-fragmentation analyst.

    A global META kind that BOTH calls the LLM (merge adjudication on the $0
    core plane) AND reads/writes entity_profiles directly (blocked candidate
    pairs in, tombstone+redirect merges out). Its ``kind_deps`` carry the
    resolved primary LLM, the pg_pool, and the budget reporter. The descriptor's
    ``method.llm.merge_mode`` gates the ONLY mutating behavior (``'apply'`` vs
    the dry-run default ``'adjudicate_only'``).

    R9b: ``trgm_limit`` was READ here and then never passed to the deps — dead
    config in the X-1 sense, so a descriptor PUT enabling the trigram probe did
    nothing at all. It is now threaded (with its ``trgm_min_degree`` floor);
    both ship at their in-source defaults, so today's behavior is unchanged."""
    from ..data.analysts.entity_researcher import EntityResearcherDeps

    llm = await resolve_llm()
    merge_mode = str(_read_method_llm_option(
        descriptor, "merge_mode", default="adjudicate_only"))
    max_pairs = _read_method_llm_option(
        descriptor, "max_pairs", default=EntityResearcherDeps.max_pairs)
    trgm_limit = _read_method_llm_option(
        descriptor, "trgm_limit", default=EntityResearcherDeps.trgm_limit)
    # R9b — the trigram probe's link-count floor. Default 0 (no floor) keeps the
    # shipped behavior byte-identical; raising trgm_limit WITHOUT this is the
    # unbounded ~61s self-join, so the two are set together in the descriptor.
    trgm_min_degree = _read_method_llm_option(
        descriptor, "trgm_min_degree",
        default=EntityResearcherDeps.trgm_min_degree)
    same_min = _read_method_llm_option(
        descriptor, "same_min_confidence",
        default=EntityResearcherDeps.same_min_confidence)
    max_tokens = _read_method_llm_option(
        descriptor, "max_tokens", default=EntityResearcherDeps.max_tokens)
    temperature = _read_method_llm_option(
        descriptor, "temperature", default=EntityResearcherDeps.temperature)
    batch_size = _read_method_llm_option(
        descriptor, "batch_size", default=EntityResearcherDeps.batch_size)
    reclassify_max = _read_method_llm_option(
        descriptor, "reclassify_max", default=EntityResearcherDeps.reclassify_max)
    reclass_min = _read_method_llm_option(
        descriptor, "reclass_min_confidence",
        default=EntityResearcherDeps.reclass_min_confidence)
    # #219 — fraction of reclassify_max split off to the generic-entity pool;
    # default 0.0 (from EntityResearcherDeps) preserves pre-#219 behavior
    # until a descriptor PUT opts in, same precedent as reclassify_max itself
    # shipping at 0 in E6c (`5994594`) before its own live flip (`9b53f00`).
    reclass_entity_share = _read_method_llm_option(
        descriptor, "reclass_entity_share",
        default=EntityResearcherDeps.reclass_entity_share)
    return (
        handler.run_method,
        EntityResearcherDeps(
            llm=llm,
            pg_pool=pg_pool,
            budget=getattr(deps, "budget", None),
            apply=(merge_mode == "apply"),
            max_pairs=int(max_pairs),
            trgm_limit=int(trgm_limit),
            trgm_min_degree=int(trgm_min_degree),
            same_min_confidence=float(same_min),
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            batch_size=int(batch_size),
            reclassify_max=int(reclassify_max),
            reclass_min_confidence=float(reclass_min),
            reclass_entity_share=float(reclass_entity_share),
        ),
        handler.output_kind,
    )


async def _build_signal_salience(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    pg_pool: "asyncpg.Pool | None" = None,
    deps: StandardDeps,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """signal_salience (S-1) — the per-signal consequence scorer.

    A global META sweep that BOTH calls the LLM (salience scoring on the $0 core
    plane) AND reads/writes ``signals`` directly (un-scored recent-text pool in,
    ``signals.salience`` out). Its ``kind_deps`` carry the resolved primary LLM,
    the pg_pool, and the budget reporter. The descriptor's ``method.llm.score_mode``
    gates the ONLY mutating behavior (``'apply'`` vs the dry-run default)."""
    from ..data.analysts.signal_salience import SignalSalienceDeps

    llm = await resolve_llm()
    score_mode = str(_read_method_llm_option(
        descriptor, "score_mode", default="dry_run"))
    max_rows = _read_method_llm_option(
        descriptor, "max_rows", default=SignalSalienceDeps.max_rows)
    batch_size = _read_method_llm_option(
        descriptor, "batch_size", default=SignalSalienceDeps.batch_size)
    window_hours = _read_method_llm_option(
        descriptor, "window_hours", default=SignalSalienceDeps.window_hours)
    max_tokens = _read_method_llm_option(
        descriptor, "max_tokens", default=SignalSalienceDeps.max_tokens)
    temperature = _read_method_llm_option(
        descriptor, "temperature", default=SignalSalienceDeps.temperature)
    return (
        handler.run_method,
        SignalSalienceDeps(
            llm=llm,
            pg_pool=pg_pool,
            budget=getattr(deps, "budget", None),
            apply=(score_mode == "apply"),
            max_rows=int(max_rows),
            batch_size=int(batch_size),
            window_hours=int(window_hours),
            max_tokens=int(max_tokens),
            temperature=float(temperature),
        ),
        handler.output_kind,
    )


async def _build_competing_hypotheses(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    pg_pool: "asyncpg.Pool | None" = None,
    deps: StandardDeps,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """competing_hypotheses — the PIECE C ACH meta-analyst.

    A META analyst that BOTH calls the LLM (competing-hypothesis generation) AND
    reads/writes the substrate directly (the temporally-current evidence base +
    side-written HYPOTHESIS rows). So its ``kind_deps`` carries the resolved LLM,
    the pg_pool, and the budget reporter (the kind budget-gates + degrades-not-
    drops per call). The LLM is an ENRICHMENT — when the descriptor declares no
    ``method.llm.primary`` the kind still runs on its deterministic fallback
    hypothesis set, so we resolve the LLM iff one is declared (the predictor
    precedent).
    """
    from ..data.analysts.competing_hypotheses import ACHDeps

    component_id = _primary_llm_component_id(descriptor)
    llm: LLMProviderHandler | None = None
    if component_id:
        llm = await resolve_llm()
    max_tokens = _read_method_llm_option(
        descriptor, "max_tokens", default=ACHDeps.max_tokens,
    )
    temperature = _read_method_llm_option(
        descriptor, "temperature", default=ACHDeps.temperature,
    )
    max_topics = _read_method_llm_option(
        descriptor, "max_topics", default=ACHDeps.max_topics,
    )
    return (
        handler.run_method,
        ACHDeps(
            llm=llm,
            pg_pool=pg_pool,
            budget=getattr(deps, "budget", None),
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            max_topics=int(max_topics),
        ),
        handler.output_kind,
    )


async def _build_deterministic(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    deps: StandardDeps,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    embedding_service: Any | None = None,
    nlp_client: Any | None = None,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """deterministic — code-only sub-dispatched kind.

    The kind's module-level ``run_method`` takes the
    :class:`StandardDeps` bundle directly (sub-handlers reach into
    ``deps.pg_pool`` etc.).  Sub-handler selection happens at run time via
    ``options["sub_handler"]`` — the descriptor must populate that field
    (validated by the kind itself, not here).

    Three deterministic sub-handlers reach for the SELF-HOSTED vLLM plane
    (``llm.primary.openai_compat`` — NEVER Anthropic / Opus, that plane is
    consult/deep only). All resolve the descriptor's ``method.llm.primary`` via
    the same ``resolve_llm()`` path and merge the handler into ``deps.extras``
    under their own key; the shared :func:`_wire_deterministic_llm` applies the
    ``_is_anthropic_component`` hard-refuse + degrade-not-break for each:

      * ``signal_summarizer`` — ALWAYS-ON (no env flag). Its sweep distills long
        signal bodies on the CORE plane ($0). Wired whenever the bound
        sub-handler is ``signal_summarizer`` AND the descriptor declares
        ``method.llm.primary``. Key: ``signal_summarizer.LLM_DEPS_EXTRA_KEY``.
      * ``fact_contention_arbiter`` (Wave 2b, #101) — FLAG-GATED
        (``LEGBA_FACT_CONTENTION_LLM_TIEBREAK``, default OFF). Breaks a NEAR-TIE
        abstain with a bounded vLLM call. Key:
        ``fact_contention_arbiter.LLM_DEPS_EXTRA_KEY``.
      * ``claim_watch`` (W-B2) — the bearing-CONFIRM leg's batched second
        opinion over gate-passed edges. Wired whenever the bound sub-handler is
        ``claim_watch`` AND the descriptor declares ``method.llm.primary``; the
        shipped descriptor declares none, so the leg is inert until an operator
        adds one. Key: ``bearing_gate.CONFIRM_LLM_DEPS_EXTRA_KEY``. (The GATE
        leg's 8B client is NOT wired here — its component id is a run OPTION
        this builder cannot see, so ``bearing_gate`` resolves it lazily from
        the registry itself.)

    No LLM ref (or the sub-handler doesn't want one, or the flag is off) →
    ``deps`` is threaded UNMODIFIED, so every other deterministic sub-handler
    stays byte-for-byte unchanged. A resolution failure degrades to the no-LLM
    path (the sub-handler's own fallback) rather than blocking deps-build.
    """
    component_id = _primary_llm_component_id(descriptor)
    sub_handler = getattr(descriptor.method, "sub_handler", None)
    is_deterministic = descriptor.identity.kind == "deterministic"

    # signal_summarizer — always-on CORE-plane distillation (no env flag). Wired
    # only for the bound sub-handler so no other deterministic analyst pays for a
    # resolution it never uses.
    if (
        is_deterministic
        and sub_handler == "signal_summarizer"
        and component_id is not None
    ):
        from ..data.analysts.deterministic_handlers.signal_summarizer import (
            LLM_DEPS_EXTRA_KEY as _SUMMARIZER_LLM_KEY,
        )

        deps = await _wire_deterministic_llm(
            descriptor,
            deps,
            resolve_llm,
            component_id=component_id,
            extra_key=_SUMMARIZER_LLM_KEY,
            purpose="signal_summarizer",
        )

    # fact_contention_arbiter Wave-2b tie-break — flag-gated (default OFF). Not
    # keyed on sub_handler (a pre-existing descriptor may carry the llm block
    # without the field); the flag + llm.primary presence is the gate.
    if (
        is_deterministic
        and _fact_contention_tiebreak_enabled()
        and component_id is not None
    ):
        from ..data.analysts.deterministic_handlers.fact_contention_arbiter import (
            LLM_DEPS_EXTRA_KEY as _TIEBREAK_LLM_KEY,
        )

        deps = await _wire_deterministic_llm(
            descriptor,
            deps,
            resolve_llm,
            component_id=component_id,
            extra_key=_TIEBREAK_LLM_KEY,
            purpose="fact_contention_tiebreak",
        )

    # claim_watch — the W-B2 CONFIRM leg's $0 core-plane client. Wired only for
    # the bound sub-handler AND only when the descriptor declares
    # method.llm.primary, so the shipped (llm-less) claim_watch descriptor is
    # byte-for-byte unchanged: no llm block => no wiring => the confirm leg is
    # inert and stamps nothing. Same shared helper as signal_summarizer, so the
    # Anthropic hard-refuse applies here too — a deterministic handler may
    # never route onto the billed plane.
    if (
        is_deterministic
        and sub_handler == "claim_watch"
        and component_id is not None
    ):
        from ..data.analysts.deterministic_handlers.bearing_gate import (
            CONFIRM_LLM_DEPS_EXTRA_KEY as _CLAIM_WATCH_CONFIRM_KEY,
        )

        deps = await _wire_deterministic_llm(
            descriptor,
            deps,
            resolve_llm,
            component_id=component_id,
            extra_key=_CLAIM_WATCH_CONFIRM_KEY,
            purpose="claim_watch_bearing_confirm",
        )

    # corpus_indexer — always-on OpenSearch INDEX PLANE sweep (no LLM). Build +
    # connect an OpenSearchStore and merge it into deps.extras so the sweep can
    # index the next batch of un-indexed signals. Wired only for the bound
    # sub-handler so no other deterministic analyst pays for a store it never uses.
    if is_deterministic and sub_handler == "corpus_indexer":
        from ..data.analysts.deterministic_handlers.corpus_indexer import (
            OS_DEPS_EXTRA_KEY as _CORPUS_OS_KEY,
        )

        deps = await _wire_corpus_indexer_os(
            descriptor, deps, extra_key=_CORPUS_OS_KEY,
        )

    # signal_embedder — always-on Qdrant VECTOR PLANE sweep (no LLM; the hosted
    # embedder is the process-lifetime embedding_service the host built at bring-up
    # from embed.primary.openai_compat). Build + connect a QdrantStore AND thread
    # that embedder, both into deps.extras, so the sweep can embed the next batch
    # of un-embedded signals into legba_signals. Wired only for the bound
    # sub-handlers so no other deterministic analyst pays for a store it never
    # uses. claim_watch RIDES THE SAME PLANE (same wiring, same extras keys):
    # it reads stored signal vectors back by id and embeds only the bounded
    # open-question set — reuse, not a second vector stack.
    if is_deterministic and sub_handler in ("signal_embedder", "claim_watch"):
        deps = await _wire_signal_embedder(
            descriptor, deps, embedding_service=embedding_service,
        )

    # reenrich_ner — ONE-TIME NER-backfill sweep (no LLM; it re-runs the LIVE
    # multilingual/telegram NER over the pre-fix historical backlog). Resolve the
    # hosted NlpServiceClient from the process-lifetime nlp_client source and merge
    # it into deps.extras so the sweep can call the production translate-then-NER
    # path. Wired only for the bound sub-handler so no other deterministic analyst
    # pays for a client it never uses.
    if is_deterministic and sub_handler == "reenrich_ner":
        deps = await _wire_reenrich_ner(descriptor, deps, nlp_client=nlp_client)

    # reenrich_translation — TRANSLATION-backfill sweep (M13/T-1c; no LLM). Reuses
    # the SAME hosted NlpServiceClient source as reenrich_ner (translate is the same
    # /translate plane), threaded under its OWN deps.extras key. Wired only for the
    # bound sub-handler so no other deterministic analyst pays for it.
    if is_deterministic and sub_handler == "reenrich_translation":
        deps = await _wire_reenrich_translation(
            descriptor, deps, nlp_client=nlp_client
        )
    return handler.run_method, deps, handler.output_kind


async def _wire_signal_embedder(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    embedding_service: Any | None,
) -> StandardDeps:
    """Build + connect a :class:`QdrantStore` AND thread the hosted embedding
    client into ``deps.extras`` for the signal_embedder sweep, returning the
    (replaced) deps.

    EMBEDDER-WIRING CHOICE: rather than resolve a SECOND embedding client here
    (which would need the registry + secrets plumbing), we reuse the
    process-lifetime ``embedding_service`` the host already built ONCE at bring-up
    from ``embed.primary.openai_compat`` (the same handle threaded into the
    inline_target / journal grounding RAG). It arrives via
    ``build_analyst_run_method(..., embedding_service=...)`` and is passed straight
    through — no per-analyst rebuild, no extra registry fetch, one shared client.

    Degrade-not-break (mirrors ``_wire_corpus_indexer_os``): any failure to
    build/connect the Qdrant store logs a warning and returns ``deps`` with only
    the embedder wired; if the embedder is absent too, ``deps`` is returned with
    neither — the sweep then no-ops that tick (its own no-plane guard). The
    AsyncQdrantClient is lazy (connect opens no socket), so an unreachable Qdrant
    does not fail here — the actual upsert degrades inside the sweep instead."""
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers.signal_embedder import (
        EMBEDDER_DEPS_EXTRA_KEY as _EMBED_KEY,
        QDRANT_DEPS_EXTRA_KEY as _QDRANT_KEY,
    )

    merged_extras = dict(deps.extras)

    # Thread the pre-built hosted embedder (the vector plane's compute leg).
    if embedding_service is not None:
        merged_extras[_EMBED_KEY] = embedding_service
        logger.info(
            "analyst_deps_builder.signal_embedder.embedder_wired analyst=%r — "
            "reusing the process-lifetime embed.primary.openai_compat client",
            descriptor.identity.id,
        )
    else:
        logger.warning(
            "analyst_deps_builder.signal_embedder.no_embedder analyst=%r — no "
            "embedding_service was provisioned at bring-up; the sweep no-ops until "
            "one is wired",
            descriptor.identity.id,
        )

    # Build + connect the QdrantStore (the vector plane's storage leg).
    try:
        from ..data.qdrant import QdrantStore

        store = QdrantStore.from_env()
        await store.connect()
        merged_extras[_QDRANT_KEY] = store
        logger.info(
            "analyst_deps_builder.signal_embedder.qdrant_wired analyst=%r "
            "host=%s:%d collection=%s — the Qdrant vector plane is wired",
            descriptor.identity.id, store.cfg.host, store.cfg.port,
            store.cfg.signals_collection,
        )
    except Exception as exc:
        logger.warning(
            "analyst_deps_builder.signal_embedder.qdrant_wire_failed analyst=%r "
            "err=%s — degrading to the no-store path (the sweep no-ops this tick)",
            descriptor.identity.id, exc,
        )

    return _dc_replace(deps, extras=merged_extras)


async def _wire_reenrich_ner(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    nlp_client: Any | None,
) -> StandardDeps:
    """Resolve the hosted :class:`NlpServiceClient` and merge it into
    ``deps.extras`` for the reenrich_ner NER-backfill sweep, returning the
    (replaced) deps.

    NLP-WIRING CHOICE (mirrors ``_wire_signal_embedder``'s embedder reuse): rather
    than re-plumb the registry + secrets here, we reuse the process-lifetime
    hosted-NLP source the host already built at bring-up — the
    :class:`~legba.runtime.nlp_client_factory.LazyNlpClient` holder for the
    ``nlp.local.legba_models`` stack component (the SAME source the source-enrichment
    pipeline's ner_multilingual filter binds to). It arrives via
    ``build_analyst_run_method(..., nlp_client=...)``. The holder resolves LAZILY —
    we call it HERE (only for this bound sub-handler) so no other deterministic
    analyst triggers a registry round-trip for a client it never uses.

    Degrade-not-break (mirrors ``_wire_signal_embedder`` / ``_wire_corpus_indexer_os``):
    a ``None`` source, or any resolution failure (models-host unreachable / stack row
    not seeded at deps-build), logs a warning and returns ``deps`` UNMODIFIED — the
    sweep then no-ops that tick (its own no-nlp guard sets ``skipped_no_nlp``). The
    holder does NOT cache a failure, so a later deps (re)build retries once the plane
    heals."""
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers.reenrich_ner import (
        NLP_DEPS_EXTRA_KEY as _NLP_KEY,
    )

    if nlp_client is None:
        logger.warning(
            "analyst_deps_builder.reenrich_ner.no_nlp_source analyst=%r — no hosted "
            "NLP client source was provisioned at bring-up; the NER-backfill sweep "
            "no-ops until one is wired",
            descriptor.identity.id,
        )
        return deps

    # The source is the LazyNlpClient holder (async .get()) in production; accept an
    # already-resolved client too (tests / a future eager wiring) via duck-typing on
    # the async getter.
    resolved: Any | None = nlp_client
    getter = getattr(nlp_client, "get", None)
    if callable(getter):
        try:
            resolved = await getter()
        except Exception as exc:
            logger.warning(
                "analyst_deps_builder.reenrich_ner.nlp_resolve_failed analyst=%r "
                "err=%s — degrading to the no-nlp path (the sweep no-ops this tick; "
                "a later deps rebuild retries once the models-host heals)",
                descriptor.identity.id, exc,
            )
            return deps

    if resolved is None:
        logger.warning(
            "analyst_deps_builder.reenrich_ner.nlp_unresolved analyst=%r — the NLP "
            "source resolved to None; the NER-backfill sweep no-ops this tick",
            descriptor.identity.id,
        )
        return deps

    merged_extras = {**dict(deps.extras), _NLP_KEY: resolved}
    logger.info(
        "analyst_deps_builder.reenrich_ner.nlp_wired analyst=%r — resolved the hosted "
        "NlpServiceClient; the NER-backfill translate-then-NER plane is wired",
        descriptor.identity.id,
    )
    return _dc_replace(deps, extras=merged_extras)


async def _wire_reenrich_translation(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    nlp_client: Any | None,
) -> StandardDeps:
    """Resolve the hosted :class:`NlpServiceClient` and merge it into
    ``deps.extras`` under the reenrich_translation key (M13/T-1c).

    Identical wiring to :func:`_wire_reenrich_ner` (same process-lifetime hosted-NLP
    source — translate is the same ``nlp.local.legba_models`` /translate plane), only
    the extras KEY differs so the two backfill sweeps stay independently addressable.
    Degrade-not-break: a ``None`` source / resolution failure returns ``deps``
    UNMODIFIED and the sweep no-ops that tick (its own no-nlp guard)."""
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers.reenrich_translation import (
        NLP_DEPS_EXTRA_KEY as _NLP_KEY,
    )

    if nlp_client is None:
        logger.warning(
            "analyst_deps_builder.reenrich_translation.no_nlp_source analyst=%r — no "
            "hosted NLP client source was provisioned at bring-up; the translation-"
            "backfill sweep no-ops until one is wired",
            descriptor.identity.id,
        )
        return deps

    # The source is the LazyNlpClient holder (async .get()) in production; accept an
    # already-resolved client too (tests) via duck-typing on the async getter.
    resolved: Any | None = nlp_client
    getter = getattr(nlp_client, "get", None)
    if callable(getter):
        try:
            resolved = await getter()
        except Exception as exc:
            logger.warning(
                "analyst_deps_builder.reenrich_translation.nlp_resolve_failed "
                "analyst=%r err=%s — degrading to the no-nlp path (the sweep no-ops "
                "this tick; a later deps rebuild retries once the models-host heals)",
                descriptor.identity.id, exc,
            )
            return deps

    if resolved is None:
        logger.warning(
            "analyst_deps_builder.reenrich_translation.nlp_unresolved analyst=%r — the "
            "NLP source resolved to None; the translation-backfill sweep no-ops",
            descriptor.identity.id,
        )
        return deps

    merged_extras = {**dict(deps.extras), _NLP_KEY: resolved}
    logger.info(
        "analyst_deps_builder.reenrich_translation.nlp_wired analyst=%r — resolved the "
        "hosted NlpServiceClient; the translation-backfill /translate plane is wired",
        descriptor.identity.id,
    )
    return _dc_replace(deps, extras=merged_extras)


async def _wire_corpus_indexer_os(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    extra_key: str,
) -> StandardDeps:
    """Build + connect an :class:`OpenSearchStore` and merge it into
    ``deps.extras[extra_key]`` for the corpus_indexer sweep, returning the
    (replaced) deps.

    Degrade-not-break: any failure to build/connect the store (opensearch-py
    absent, config error) logs a warning and returns ``deps`` UNMODIFIED — the
    sweep then no-ops that tick (its own no-store guard). The AsyncOpenSearch
    client is lazy (connect opens no socket), so an unreachable OpenSearch does
    not fail here — the actual bulk request degrades inside the sweep instead."""
    try:
        from dataclasses import replace as _dc_replace

        from ..data.opensearch import OpenSearchStore

        store = OpenSearchStore.from_env()
        await store.connect()
        merged_extras = {**dict(deps.extras), extra_key: store}
        deps = _dc_replace(deps, extras=merged_extras)
        logger.info(
            "analyst_deps_builder.corpus_indexer.os_wired analyst=%r "
            "host=%s:%d index=%s — the OpenSearch index plane is wired",
            descriptor.identity.id, store.cfg.host, store.cfg.port, store.cfg.index,
        )
    except Exception as exc:
        logger.warning(
            "analyst_deps_builder.corpus_indexer.os_wire_failed analyst=%r err=%s "
            "— degrading to the no-store path (the sweep no-ops this tick)",
            descriptor.identity.id, exc,
        )
    return deps


async def _wire_deterministic_llm(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    component_id: str,
    extra_key: str,
    purpose: str,
) -> StandardDeps:
    """Resolve the descriptor's SELF-HOSTED primary LLM and merge it into
    ``deps.extras[extra_key]``, returning the (replaced) deps.

    Hard-refuses an Anthropic component (``_is_anthropic_component``): the
    deterministic plane is self-hosted / $0 only, so a mis-wired descriptor can
    never route a deterministic-analyst call onto the billed Opus plane
    (consult / deep_consult are the only sanctioned Anthropic users) — on a
    refuse, ``deps`` is returned UNMODIFIED (the sub-handler stays on its no-LLM
    path). Any resolution failure likewise degrades to the unchanged ``deps``
    rather than blocking deps-build."""
    if _is_anthropic_component(component_id):
        logger.warning(
            "analyst_deps_builder.deterministic.llm_refused_anthropic "
            "analyst=%r purpose=%s llm=%r — the deterministic plane is vLLM-only; "
            "refusing an Anthropic/Opus primary; staying no-LLM",
            descriptor.identity.id, purpose, component_id,
        )
        return deps
    try:
        from dataclasses import replace as _dc_replace

        llm = await resolve_llm()
        merged_extras = {**dict(deps.extras), extra_key: llm}
        deps = _dc_replace(deps, extras=merged_extras)
        logger.info(
            "analyst_deps_builder.deterministic.llm_wired "
            "analyst=%r purpose=%s llm=%r — resolved on the self-hosted vLLM plane",
            descriptor.identity.id, purpose, component_id,
        )
    except Exception as exc:
        logger.warning(
            "analyst_deps_builder.deterministic.llm_resolve_failed "
            "analyst=%r purpose=%s err=%s — degrading to the no-LLM path",
            descriptor.identity.id, purpose, exc,
        )
    return deps


def _fact_contention_tiebreak_enabled() -> bool:
    """``LEGBA_FACT_CONTENTION_LLM_TIEBREAK`` truthy? (default OFF).

    Read here too (not only in the handler) so the runtime resolves the vLLM
    handler ONLY when the flag is set — off → no resolution, no extra registry
    fetch, ``deps`` untouched."""
    import os

    return os.getenv("LEGBA_FACT_CONTENTION_LLM_TIEBREAK", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _is_anthropic_component(component_id: str | None) -> bool:
    """True if a stack-component id names the Anthropic plane (``llm.anthropic.*``).

    The Wave-2b tie-break is vLLM-only; the deterministic deps-builder refuses an
    Anthropic/Opus primary so a mis-wired arbiter descriptor can never route
    billed Opus calls onto the deterministic-analyst plane (consult / deep_consult
    are the only sanctioned Anthropic users)."""
    return bool(component_id) and "anthropic" in component_id.lower()


async def _build_predictor(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """predictor — AutoARIMA + conformal CI + optional narrative.

    The predictor's :class:`PredictorDeps` carries an *optional* LLM
    handle (narrative is decorative; the stat forecast is the
    payload).  We resolve the primary LLM iff the descriptor declares
    one; descriptors without an LLM block run in stat-only mode.
    """
    from ..data.analysts.predictor import PredictorDeps

    component_id = _primary_llm_component_id(descriptor)
    llm: LLMProviderHandler | None = None
    if component_id:
        llm = await resolve_llm()

    horizon_days = _read_method_llm_option(
        descriptor, "horizon_days", default=PredictorDeps.horizon_days,
    )
    ci_level = _read_method_llm_option(
        descriptor, "ci_level", default=PredictorDeps.ci_level,
    )
    return (
        handler.run_method,
        PredictorDeps(
            llm=llm,
            horizon_days=int(horizon_days),
            ci_level=int(ci_level),
        ),
        handler.output_kind,
    )


async def _build_critic(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    tools_registry: Mapping[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] | None = None,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """critic — rubric-graded judge kind.

    The rubric / analyzed_model / analyzed_output_id flow into the
    kind via the actor's ``options`` dict — see
    :func:`legba.runtime.dapr_actors._resolve_critic_context`.

    L-175 tool threading: when the descriptor declares
    ``method.tools_whitelist`` AND the caller supplied a
    ``tools_registry`` containing a callable for each whitelisted name,
    the critic's ReAct loop offers those tools to the judge LLM.
    Names missing from the registry are silently dropped (the critic
    kind also warns); descriptors without a whitelist hit the
    single-turn path unchanged.
    """
    from ..data.analysts.critic import CriticDeps

    llm = await resolve_llm()

    # Resolve whitelisted tools from the registry the runtime owns.
    whitelist = list(getattr(descriptor.method, "tools_whitelist", None) or [])
    resolved_tools: dict[
        str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    ] = {}
    if whitelist and tools_registry:
        for name in whitelist:
            callable_ = tools_registry.get(name)
            if callable_ is None:
                logger.warning(
                    "analyst_deps_builder.critic.tool_unresolved "
                    "analyst=%r tool=%r — not in tools_registry; the "
                    "kind's ReAct loop will skip it",
                    descriptor.identity.id, name,
                )
                continue
            resolved_tools[name] = callable_

    return (
        handler.run_method,
        CriticDeps(
            llm=llm,
            tools=resolved_tools,
            tools_whitelist=whitelist,
        ),
        handler.output_kind,
    )


def _build_optimizer(
    handler: KindHandler,
    *,
    temporal_client: Any | None,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """optimizer — durable-workflow-driven DSPy/GEPA compile kind.

    The :class:`OptimizerDeps.__post_init__` autocompletes
    ``temporal_client`` (historical slot name) via
    :func:`legba.data.analysts.optimizer._resolve_workflow_client`
    (chooses Dapr Workflow vs in-process per env), so we can pass
    ``temporal_client=None`` and let the dataclass auto-pick.  Callers
    that already hold a client (the host's per-process singleton) pass
    it through unchanged.
    """
    from ..data.analysts.optimizer import OptimizerDeps

    if temporal_client is None:
        deps_obj = OptimizerDeps()
    else:
        deps_obj = OptimizerDeps(temporal_client=temporal_client)
    return handler.run_method, deps_obj, handler.output_kind


def _build_deep_consult(
    descriptor: AnalystDescriptor,
    handler: KindHandler,
    *,
    deep_consult_client: Any | None,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """deep_consult — the actor→workflow bridge (anchor §5 PIECE 4).

    THIN: the kind's ``run_method`` only SCHEDULES the deep-consult Dapr
    Workflow (detached) and returns the task id.  The deps carry the
    runtime-resident workflow client + the descriptor's primary LLM component
    id (the workflow's plan/analyze stages resolve their handler from it) + the
    descriptor's per-day budget cap (the analyze stage's budget gate reads it).

    No LLM handler is resolved HERE — inference happens in the workflow
    activities, not the actor (so the actor returns in <1s, never the 180s
    block).  We surface the LLM component id by ref only.
    """
    from ..data.analysts.deep_consult import DeepConsultKindDeps

    component_id = _primary_llm_component_id(descriptor) or ""
    budget_tokens_per_day = getattr(
        descriptor.method, "budget_tokens_per_day", None
    )
    deps_obj = DeepConsultKindDeps(
        workflow_client=deep_consult_client,
        llm_component_id=component_id,
        budget_tokens_per_day=budget_tokens_per_day,
    )
    return handler.run_method, deps_obj, handler.output_kind


async def _build_consult_on_demand(
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    *,
    substrate_query_port: Any | None,
    agency_binding: Any | None = None,
    resolve_llm_component: (
        Callable[[str], Awaitable[LLMProviderHandler]] | None
    ) = None,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """consult_on_demand — ReAct loop over the substrate-tool whitelist.

    Two ports needed: LLM (resolved via the descriptor's
    ``method.llm.primary``) and a
    :class:`SubstrateQueryPort` (``search_signals`` / ``query_facts`` /
    ``inspect_entity`` / ``vector_search``).

    No production :class:`SubstrateQueryPort` exists in-tree today —
    the only implementation is the test stub
    (``tests/runtime/test_spike_integration._StubSubstrate``).  When
    callers don't supply one we raise :class:`AnalystDepsBuildError`
    per the no-stubs rule rather than handing the kind a half-wired
    port and letting it crash at first tool call.

    F1 model picker: when ``resolve_llm_component`` (the by-id resolver) is
    threaded, we wrap it in an ALLOWLIST-BOUND resolver and hand it to the kind
    so a per-request ``llm_component_override`` builds a fresh handler for the
    operator's chosen plane. The bound resolver refuses any id outside
    :data:`legba.data.analysts.consult_on_demand.LLM_OVERRIDE_ALLOWLIST`, so the
    kind can never be steered onto an arbitrary component. None keeps the kind
    on the cached primary (Opus default) — the pre-F1 behavior, unchanged.
    """
    from ..data.analysts.consult_on_demand import (
        LLM_OVERRIDE_ALLOWLIST,
        ConsultOnDemandDeps,
    )

    llm = await resolve_llm()
    if substrate_query_port is None:
        raise AnalystDepsBuildError(
            "consult_on_demand requires a SubstrateQueryPort but none was "
            "supplied — no production implementation exists in-tree yet "
            "(see analyst_deps_builder report). Pass substrate_query_port "
            "explicitly when binding this analyst."
        )
    override_resolver = None
    if resolve_llm_component is not None:
        override_resolver = _bind_override_resolver(
            resolve_llm_component, LLM_OVERRIDE_ALLOWLIST,
        )
    return (
        handler.run_method,
        ConsultOnDemandDeps(
            llm=llm,
            substrate=substrate_query_port,
            # A-3a: the production resolver ALWAYS supplies the
            # substrate_read AgencyToolBinding (and fails loud when it
            # can't). None is the hand-constructed test/embedder path —
            # direct port dispatch, no governance, never wired by the
            # runtime.
            agency_binding=agency_binding,
            resolve_llm_component=override_resolver,
        ),
        handler.output_kind,
    )


def _bind_override_resolver(
    resolve_llm_component: Callable[[str], Awaitable[LLMProviderHandler]],
    allowlist: "frozenset[str] | set[str]",
) -> Callable[[str], Awaitable[LLMProviderHandler]]:
    """Wrap a by-id LLM resolver so it resolves ONLY allowlisted component ids.

    The consult / deep model picker (F1) exposes a per-request plane override.
    The registry front door is the primary gate (it maps a friendly value to one
    of the sanctioned ids and never accepts a raw id), but the run-time resolver
    is bound here too so a component id outside the allowlist is refused with a
    clear error rather than resolved — defense in depth for the LLM plane.
    """

    async def _resolve(component_id: str) -> LLMProviderHandler:
        if component_id not in allowlist:
            raise AnalystDepsBuildError(
                f"consult LLM override {component_id!r} is not in the allowlist "
                f"{sorted(allowlist)!r} — refusing to resolve an unsanctioned "
                f"plane"
            )
        return await resolve_llm_component(component_id)

    return _resolve


# ---------------------------------------------------------------------------
# LLM handler construction
# ---------------------------------------------------------------------------


async def build_llm_handler_from_stack_component(
    component_id: str,
    *,
    registry_client: RegistryHTTPClient,
    secrets_resolve: Callable[[str], Awaitable[bytes]],
) -> LLMProviderHandler:
    """Build + configure an :class:`LLMProviderHandler` from registry data.

    Steps:

      1. Fetch the stack-component row at ``/stack/{component_id}`` via
         the registry HTTP client.
      2. Re-parse the ``config`` JSON into :class:`LLMProviderConfig`
         (the schema enforces FactoryValue shapes on every field).
      3. Infer the subprovider (vllm / anthropic / openai) from the
         component id and endpoint hostname — there's no
         ``subprovider`` field in :class:`LLMProviderConfig` today, so
         we derive it from naming convention + endpoint URL.
      4. Look up the handler class in
         :data:`legba.data.stack.llm.LLM_HANDLERS` and instantiate it.
      5. Call ``on_configure`` so the handler resolves the vault secret
         + populates its model list.  ``on_activate`` is intentionally
         NOT called — that's the actor's lifecycle responsibility per
         L-102 §1, and calling it eagerly would open an httpx pool the
         actor must later close.

    Raises :class:`AnalystDepsBuildError` on registry lookup failures,
    unknown subprovider, or vault resolution failures.
    """
    # 1. Registry fetch.
    try:
        row = await _fetch_stack_component(registry_client, component_id)
    except RegistryClientError as exc:
        raise AnalystDepsBuildError(
            f"stack-component lookup failed for {component_id!r}: {exc}"
        ) from exc
    if row is None:
        raise AnalystDepsBuildError(
            f"stack-component {component_id!r} not found in registry"
        )

    # 2. Re-parse config.  The ``body`` carries the JSON dump (factory
    # values as dicts); parse non-strictly so the FactoryValue subclasses
    # coerce from their dict shapes.
    body = row.get("body") or {}
    raw_config = body.get("config") if isinstance(body, dict) else None
    if not isinstance(raw_config, Mapping):
        raise AnalystDepsBuildError(
            f"stack-component {component_id!r}: body.config is missing or "
            f"non-mapping (got {type(raw_config).__name__})"
        )
    try:
        cfg = LLMProviderConfig.model_validate(dict(raw_config), strict=False)
    except Exception as exc:
        raise AnalystDepsBuildError(
            f"stack-component {component_id!r}: LLMProviderConfig validation "
            f"failed: {exc}"
        ) from exc

    # 3. Infer subprovider.
    endpoint = cfg.api_endpoint.raw
    subprovider = infer_llm_subprovider(component_id, endpoint=endpoint)
    handler_cls = LLM_HANDLERS.get(subprovider)
    if handler_cls is None:
        raise AnalystDepsBuildError(
            f"stack-component {component_id!r}: inferred subprovider "
            f"{subprovider!r} is not in LLM_HANDLERS "
            f"(known: {sorted(LLM_HANDLERS)!r})"
        )

    # 4. Instantiate.
    handler = handler_cls()

    # 5. Configure.  Build a minimal HandlerContext satisfying the
    # Protocol (instance_id, instance_version, secrets, telemetry()).
    ctx = _BuilderHandlerContext(
        instance_id=component_id,
        instance_version=str(row.get("version") or ""),
        config=cfg,
        secrets=_SecretsResolverAdapter(secrets_resolve),
    )
    await handler.on_configure(ctx)
    return handler


async def build_search_handler_from_stack_component(
    component_id: str,
    *,
    registry_client: RegistryHTTPClient,
    secrets_resolve: Callable[[str], Awaitable[bytes]],
) -> Any:
    """Build + configure a ``search_provider`` handler from registry data.

    The search-family twin of
    :func:`build_llm_handler_from_stack_component`, and deliberately the same
    shape: fetch ``/stack/{component_id}``, hand the row to the family's own
    ``build_handler``, and let THAT validate. Two differences worth naming:

      * the FAMILY is validated (``assert_search_component`` — the check
        ``expected_family`` on a StackRef does NOT perform, because it is
        stripped at bind time by ``_FACTORY_KEY_HINTS``), so a route pointed at
        an ``llm.*`` id fails at bind time naming the mismatch rather than at
        first query;
      * the SUBPROVIDER is EXPLICIT (``config.subprovider`` looked up in
        ``SEARCH_HANDLERS``), never inferred from the id or the endpoint host —
        no ``infer_llm_subprovider``-style string ladder.

    Raises :class:`AnalystDepsBuildError` on lookup failure / a missing row /
    a malformed config, and lets the family's own ``HardSearchFailure`` through
    for a wrong-family or unknown-subprovider component. Every one of those is
    a LOUD failure by design: a search route that cannot be resolved must never
    degrade into "the provider returned nothing".
    """
    from ..data.stack.search import build_handler

    try:
        row = await _fetch_stack_component(registry_client, component_id)
    except RegistryClientError as exc:
        raise AnalystDepsBuildError(
            f"search stack-component lookup failed for {component_id!r}: {exc}"
        ) from exc
    if row is None:
        raise AnalystDepsBuildError(
            f"search stack-component {component_id!r} not found in registry "
            "(register it with scripts/bringup_register_stack.py)"
        )
    body = row.get("body") or {}
    if not isinstance(body, Mapping):
        raise AnalystDepsBuildError(
            f"search stack-component {component_id!r}: body is "
            f"{type(body).__name__}, expected a mapping"
        )
    raw_config = body.get("config")
    if not isinstance(raw_config, Mapping):
        raise AnalystDepsBuildError(
            f"search stack-component {component_id!r}: body.config is missing "
            f"or non-mapping (got {type(raw_config).__name__})"
        )
    return await build_handler(
        {
            "id": component_id,
            "schema_uri": str(body.get("schema_uri") or ""),
            "config": dict(raw_config),
        },
        secrets=_SecretsResolverAdapter(secrets_resolve),
    )


def infer_llm_subprovider(component_id: str, *, endpoint: str) -> str:
    """Pick the :data:`LLM_HANDLERS` key for a stack-component.

    Resolution priority (most specific → most generic):

      1. Component id contains ``.anthropic.`` or starts with
         ``llm.anthropic`` → ``anthropic``.
      2. Component id ends in ``.openai_compat`` (the in-house
         convention for vLLM / gpt-oss-120b endpoints) → ``vllm``.
      3. Component id contains ``.openai.`` or starts with ``llm.openai``
         → ``openai``.
      4. Endpoint hostname is ``api.anthropic.com`` → ``anthropic``.
      5. Endpoint hostname is ``api.openai.com`` → ``openai``.
      6. Fall back to ``vllm`` (the OpenAI-compatible self-hosted
         shape) — this is the dominant case in the legba stack today,
         and the budget enforcer's zero-cost table picks it up
         correctly.

    The id-pattern rules are the precision lever — operators registering
    a new LLM stack-component should name it
    ``llm.<subprovider>.<model_slug>`` (anthropic/openai/vllm) OR keep
    the legacy ``.openai_compat`` suffix for self-hosted endpoints.
    """
    cid = (component_id or "").lower()
    if ".anthropic." in cid or cid.startswith("llm.anthropic"):
        return "anthropic"
    if cid.endswith(".openai_compat"):
        return "vllm"
    if ".openai." in cid or cid.startswith("llm.openai"):
        return "openai"
    host = ""
    try:
        parsed = urlparse(endpoint or "")
        host = (parsed.hostname or "").lower()
    except (ValueError, AttributeError):  # pragma: no cover
        host = ""
    if host == "api.anthropic.com":
        return "anthropic"
    if host == "api.openai.com":
        return "openai"
    return "vllm"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_llm_ref_component_id(
    descriptor: AnalystDescriptor, key: str, value: Any,
) -> str | None:
    """Shape-tolerant ``method.llm.<key>`` StackRef extraction, shared by
    :func:`_primary_llm_component_id` / :func:`_narrate_llm_component_id` /
    :func:`_verify_llm_component_id`.

    Recognized shapes:

      * a Mapping with a non-empty string ``"raw"`` key — the property-factory
        form (``{"raw": ..., "factory_kind": "stack_ref", "expected_family":
        "llm_provider"}``) or the registry's model_dump of it;
      * a live :class:`Property.StackRef` instance (``.raw`` attribute);
      * a bare non-empty string.

    ``value is None`` (the key simply absent) is the ordinary, silent
    "not configured" case — every caller's own docstring already documents
    that as zero-regression and it is NEVER warned here.

    A value that IS present but matches NONE of the recognized shapes is a
    descriptor AUTHORING MISTAKE — e.g. ``{"stack_ref": "llm.primary.
    openai_compat"}`` instead of the property-factory form above. Live
    2026-07-30: this exact shape left the W-B2 claim_watch bearing-CONFIRM
    leg dark with ZERO trace — ``_primary_llm_component_id`` returned
    ``None`` silently, indistinguishable from an operator who simply never
    configured ``method.llm.primary`` at all (the documented, expected
    default for that descriptor). Log it LOUD (WARNING, token
    ``llm_ref_malformed``) so a malformed ref is distinguishable from an
    absent one, while still returning ``None`` — the build must never fail
    here; whatever plane this key would have wired just stays unwired,
    exactly as it already did before this guard, only silently.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw = value.get("raw")
        if isinstance(raw, str) and raw:
            return raw
        reason = "mapping has no non-empty string 'raw' key"
    else:
        raw = getattr(value, "raw", None)
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(value, str) and value:
            return value
        reason = "neither a mapping, a StackRef-shaped object, nor a string"
    logger.warning(
        "analyst_deps_builder.llm_ref_malformed analyst=%r key=%s reason=%s "
        "value=%r — degrading to no-ref for method.llm.%s (build NOT failed; "
        "whatever plane this key wires stays unwired until the descriptor is "
        "corrected)",
        descriptor.identity.id, key, reason, value, key,
    )
    return None


def _primary_llm_component_id(descriptor: AnalystDescriptor) -> str | None:
    """Extract the StackRef ``raw`` field from ``descriptor.method.llm.primary``.

    The descriptor stores the StackRef as a model_dump (a plain dict
    with ``raw`` + ``expected_family`` keys) when the body comes back
    from the registry, OR as a live :class:`Property.StackRef` instance
    in tests that construct the descriptor in-process.  Both shapes are
    handled. A PRESENT-but-malformed entry logs a loud
    ``llm_ref_malformed`` WARNING (see :func:`_extract_llm_ref_component_id`)
    rather than degrading silently.
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return None
    return _extract_llm_ref_component_id(descriptor, "primary", llm.get("primary"))


def _narrate_llm_component_id(descriptor: AnalystDescriptor) -> str | None:
    """Extract the StackRef ``raw`` from ``descriptor.method.llm.narrate`` (or None).

    The OPTIONAL second LLM ref for the per-phase split (journal §4.1): the voice
    (field-notes + NARRATE) plane. ``method.llm`` is an open ``dict[str, Any]``
    (schemas/analyst.py), so the ``narrate`` key needs NO schema change. Absent
    → None, silently (zero-regression); the caller leaves the voice on the
    primary handler. A PRESENT-but-malformed entry logs a loud
    ``llm_ref_malformed`` WARNING instead (see
    :func:`_extract_llm_ref_component_id`). Handles the same shape variants as
    :func:`_primary_llm_component_id` (registry model_dump vs live StackRef).
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return None
    return _extract_llm_ref_component_id(descriptor, "narrate", llm.get("narrate"))


def _verify_llm_component_id(descriptor: AnalystDescriptor) -> str | None:
    """Extract the StackRef ``raw`` from ``descriptor.method.llm.verify`` (or None).

    The OPTIONAL judge LLM ref for the P0-T2 faithfulness verify pass: the
    cross-family 8B judge plane (the live ``slm.internal`` Llama-3.1-8B at deploy).
    ``method.llm`` is an open ``dict[str, Any]`` (schemas/analyst.py), so the
    ``verify`` key needs NO schema change. Absent → None, silently
    (zero-regression); the caller leaves ``verify_judge`` unset so the verify
    pass degrades to its deterministic citation-presence floor. A
    PRESENT-but-malformed entry logs a loud ``llm_ref_malformed`` WARNING
    instead (see :func:`_extract_llm_ref_component_id`). Handles the same
    shape variants as :func:`_primary_llm_component_id` (registry model_dump vs
    live StackRef).
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return None
    return _extract_llm_ref_component_id(descriptor, "verify", llm.get("verify"))


# ---------------------------------------------------------------------------
# P2-4 — the JUDGE ROUTE (build for the absent second model)
# ---------------------------------------------------------------------------
#
# Today the faithfulness judge resolves the SAME stack ref as the producer:
# every live escalation-family descriptor declares
# ``verify: {factory_kind: stack_ref, raw: llm.primary.openai_compat}`` — i.e.
# the judge ref literally names the core producer plane. The route below is the
# EXPLICIT judge-route resolution that lets a second (independent) judge model
# drop in later with ONE config change, while resolving byte-identically to the
# current core model until then.
#
# RESOLUTION LADDER (first hit wins; documented contract):
#
#   0. OPT-IN GATE — the descriptor's ``method.llm`` must carry a ``judge`` or
#      ``verify`` KEY (any shape). No key → ``None`` → no judge route: an
#      analyst that never opted into a judge is NEVER judge-wired, exactly as
#      today (the global override below cannot conscript it either — the env
#      var re-POINTS judge calls, it never turns judging ON).
#   1. ``LEGBA_JUDGE_STACK_REF`` (env, non-empty) — the GLOBAL operator
#      override: one setting repoints EVERY judge call in the deployment when
#      the second model lands (source ``env:LEGBA_JUDGE_STACK_REF``).
#   2. ``method.llm.judge`` — the explicit per-descriptor judge ref (the new,
#      preferred key; source ``method.llm.judge``).
#   3. ``method.llm.verify`` — today's live key (source ``method.llm.verify``);
#      every current descriptor resolves here, to the core producer plane.
#   4. ``method.llm.primary`` — the terminal rung (source
#      ``method.llm.primary``): an analyst that OPTED IN but whose judge/verify
#      refs are malformed still judges on the producer's plane rather than
#      silently degrading to un-judged.
#   5. Nothing resolvable → ``None`` (deterministic floor only).
#
# The resolved route's ``component_id`` is what the host hands the LLM handler
# factory AND what gets stamped into the critique row (``judge_llm_ref``) so
# provenance records which model judged, forever.

#: Env var carrying the deployment-wide judge stack-ref override (ladder rung 1).
JUDGE_STACK_REF_ENV = "LEGBA_JUDGE_STACK_REF"

# Judge-route CLASS labels (W-3d, additive provenance): the coarse
# configured-vs-fell-back readout the UI provenance badge needs.
# ``judge_llm_ref`` alone cannot distinguish "the operator configured this
# judge" from "resolution fell down the ladder to the producer plane" — both
# can name the same component id. The class collapses the ladder rung:
#
#   * ``configured``       — rung 1 (env override) or rung 2 (method.llm.judge):
#                            an EXPLICIT judge choice.
#   * ``fallback_verify``  — rung 3 (method.llm.verify): today's live key — the
#                            legacy verify ref, not an explicit judge pick.
#   * ``fallback_primary`` — rung 4 (method.llm.primary): the terminal rung —
#                            opted in but judge/verify refs were malformed, so
#                            judging landed on the producer's own plane.
#
# Stamped alongside ``judge_llm_ref`` on the critique row + its
# ``data.verification`` block (which the findings API projects wholesale), so
# the badge can say WHICH rung won without re-deriving the ladder.
JUDGE_ROUTE_CONFIGURED = "configured"
JUDGE_ROUTE_FALLBACK_VERIFY = "fallback_verify"
JUDGE_ROUTE_FALLBACK_PRIMARY = "fallback_primary"


class JudgeRoute(NamedTuple):
    """A resolved judge route: the stack component id + which ladder rung won."""

    component_id: str
    #: ``env:LEGBA_JUDGE_STACK_REF`` | ``method.llm.judge`` |
    #: ``method.llm.verify`` | ``method.llm.primary``
    source: str

    @property
    def route_class(self) -> str:
        """The coarse configured-vs-fallback class for this route's rung.

        ``""`` for an unrecognized source string (defensive — never fabricate
        a class; the ref itself is still stamped).
        """
        if self.source.startswith("env:") or self.source == "method.llm.judge":
            return JUDGE_ROUTE_CONFIGURED
        if self.source == "method.llm.verify":
            return JUDGE_ROUTE_FALLBACK_VERIFY
        if self.source == "method.llm.primary":
            return JUDGE_ROUTE_FALLBACK_PRIMARY
        return ""


def _stack_ref_raw(value: Any) -> str | None:
    """Shape-tolerant StackRef extraction: dump mapping / live StackRef / bare
    string → the component id, else ``None``. The same variants
    :func:`_primary_llm_component_id` / :func:`_verify_llm_component_id` accept.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw = value.get("raw")
        return str(raw) if isinstance(raw, str) and raw else None
    raw = getattr(value, "raw", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(value, str) and value:
        return value
    return None


def resolve_judge_route_from_llm_block(llm: Any) -> JudgeRoute | None:
    """Resolve the judge route over a raw ``method.llm`` mapping (the ladder
    documented above). Shared by the descriptor-object path
    (:func:`resolve_judge_route`) and typed-dict consumers (the GEPA measure
    arm), so every judge call in the system resolves through ONE ladder.
    """
    if not isinstance(llm, Mapping):
        return None
    # Rung 0 — the opt-in gate: no judge/verify key ⇒ no judge route, ever.
    if "judge" not in llm and "verify" not in llm:
        return None
    # Rung 1 — the global operator override (repoints, never enables).
    env_ref = (os.getenv(JUDGE_STACK_REF_ENV) or "").strip()
    if env_ref:
        return JudgeRoute(component_id=env_ref, source=f"env:{JUDGE_STACK_REF_ENV}")
    # Rung 2 — the explicit per-descriptor judge key.
    judge_ref = _stack_ref_raw(llm.get("judge"))
    if judge_ref:
        return JudgeRoute(component_id=judge_ref, source="method.llm.judge")
    # Rung 3 — today's live key (every current descriptor resolves here).
    verify_ref = _stack_ref_raw(llm.get("verify"))
    if verify_ref:
        return JudgeRoute(component_id=verify_ref, source="method.llm.verify")
    # Rung 4 — terminal: opted in but refs malformed → judge on the producer.
    primary_ref = _stack_ref_raw(llm.get("primary"))
    if primary_ref:
        return JudgeRoute(component_id=primary_ref, source="method.llm.primary")
    return None


def resolve_judge_route(descriptor: AnalystDescriptor) -> JudgeRoute | None:
    """The judge route for an analyst descriptor (``None`` = no judge opt-in).

    See the ladder above. For every LIVE descriptor today (``verify`` key →
    ``llm.primary.openai_compat``, no ``judge`` key, env unset) this resolves
    byte-identically to :func:`_verify_llm_component_id` — asserted by the
    wiring tests' same-ref-in → same-client-out check.
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    return resolve_judge_route_from_llm_block(llm)


async def resolve_llm_budget_params(
    descriptor: AnalystDescriptor,
    *,
    registry_client: RegistryHTTPClient,
) -> tuple[str, str, int]:
    """``(provider, model, estimated_tokens_per_run)`` for the BudgetEnforcer.

    A-5/G5: pre-fix the enforcer was constructed with the raw StackRef
    string as ``provider`` and an empty ``model`` — so ``compute_cost_usd``
    never matched a PRICE_TABLE (USD always 0) — and ``precall_check`` was
    always called with ``estimated_tokens=0``, making the forward-looking
    throttle dead code. This resolves the real values at deps-build time:

      * provider — :func:`infer_llm_subprovider` over the primary LLM stack
        component (anthropic / openai / vllm: the PRICE_TABLE dispatch keys);
      * model — the component config's ``model_name``;
      * estimate — ``method.budget_tokens_per_run`` (explicit per-run
        budget) → ``method.llm["max_tokens"]`` (per-call output cap) → the
        component config's ``max_tokens`` → 0.

    Best-effort: any failure degrades to ``("", "", est)`` with a warning —
    budget observability must never block analyst deps build. Deterministic
    analysts (no LLM ref) resolve to ``("", "", est)`` by design.
    """
    est = 0
    try:
        per_run = getattr(descriptor.method, "budget_tokens_per_run", None)
        if per_run:
            est = int(per_run)
        else:
            llm_max = _read_method_llm_option(
                descriptor, "max_tokens", default=None,
            )
            if llm_max:
                est = int(llm_max)
    except Exception:  # pragma: no cover — malformed descriptor knobs
        est = 0

    component_id = _primary_llm_component_id(descriptor)
    if component_id is None:
        return "", "", est
    try:
        row = await _fetch_stack_component(registry_client, component_id)
        if row is None:
            return "", "", est
        body = row.get("body") or {}
        raw_config = body.get("config") if isinstance(body, dict) else None
        if not isinstance(raw_config, Mapping):
            return "", "", est
        cfg = LLMProviderConfig.model_validate(dict(raw_config), strict=False)
        provider = infer_llm_subprovider(
            component_id, endpoint=cfg.api_endpoint.raw,
        )
        model = str(cfg.model_name.raw)
        if est == 0:
            est = int(cfg.max_tokens.raw or 0)
        return provider, model, est
    except Exception as exc:
        logger.warning(
            "resolve_llm_budget_params.failed component_id=%s err=%s",
            component_id, exc,
        )
        return "", "", est


def _read_method_llm_option(
    descriptor: AnalystDescriptor, key: str, *, default: Any,
) -> Any:
    """Look up ``descriptor.method.llm[key]`` with a typed fallback.

    The descriptor stuffs per-kind knobs (``max_tokens``, ``temperature``,
    ``horizon_days``, …) into the same dict that carries the StackRef.
    Values may be raw scalars or :class:`Property.Number` dumps — we
    accept both.
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return default
    value = llm.get(key)
    if value is None:
        return default
    # FactoryValue dump shape: {"raw": <number>, ...}.
    if isinstance(value, Mapping) and "raw" in value:
        return value["raw"]
    raw = getattr(value, "raw", None)
    if raw is not None:
        return raw
    return value


async def _fetch_stack_component(
    registry_client: RegistryHTTPClient,
    component_id: str,
) -> dict[str, Any] | None:
    """GET ``/stack/{component_id}`` via the registry HTTP client.

    The :class:`RegistryHTTPClient` only ships descriptor-family
    helpers today.  Stack components live under ``/stack/...`` so we
    issue the GET via the client's internal httpx surface.  Returns
    the JSON body on success, ``None`` on 404, raises
    :class:`RegistryClientError` for transport / 5xx.
    """
    import httpx

    client = await registry_client._ensure_client()  # noqa: SLF001
    path = f"{registry_client._api_prefix}/stack/{component_id}"  # noqa: SLF001
    try:
        resp = await client.get(
            path, headers=registry_client._headers(),  # noqa: SLF001
        )
    except httpx.HTTPError as exc:
        raise RegistryClientError(
            f"registry GET {path} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise RegistryClientError(
            f"registry GET {path} returned {resp.status_code}: "
            f"{resp.text[:512]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise RegistryClientError(
            f"registry GET {path} returned non-JSON body: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Internal carriers
# ---------------------------------------------------------------------------


class _LLMOnlyDeps:
    """Minimal Protocol-satisfying carrier for LLM-only kind_deps bundles.

    Used by :func:`_build_cross_target_raw` and
    :func:`_build_meta_findings_synthesizer` — both kinds' Protocols
    only ask for an ``llm`` attribute.  We avoid importing the kind's
    Protocol (it isn't dataclass-shaped) and avoid declaring a dataclass
    per kind so the carrier stays small.

    ``temperature`` (2026-07 sampling-audit fix): the OPTIONAL descriptor
    ``method.llm.temperature`` override. ``None`` (the default — and the only
    value :func:`_build_cross_target_raw` ever sets) means "descriptor didn't
    say" and the kind module falls back to its own default, so every carrier
    built without the knob behaves byte-identically.
    """

    __slots__ = ("llm", "temperature")

    def __init__(
        self, llm: LLMProviderHandler, *, temperature: float | None = None,
    ) -> None:
        self.llm = llm
        self.temperature = temperature

    def __repr__(self) -> str:                              # pragma: no cover
        return f"_LLMOnlyDeps(llm={type(self.llm).__name__})"


class _SecretsResolverAdapter:
    """Adapter from a plain async callable to the
    :class:`CredentialResolverProtocol` shape the handler context uses.

    The runtime passes a bound callable (``vault.resolve``) on
    :attr:`StandardDeps.secrets_resolve`; the LLM handler base class
    expects ``ctx.secrets.resolve(secret_id) -> bytes``.  Bridge here
    rather than monkey-patching the callable.
    """

    def __init__(self, fn: Callable[[str], Awaitable[bytes]]) -> None:
        self._fn = fn

    async def resolve(self, secret_id: str) -> bytes:
        return await self._fn(secret_id)


class _NoopTelemetry:
    """Telemetry sink used during deps-build.

    The real telemetry handle is wired by the runtime at activate time;
    during ``on_configure`` we only need the no-op surface so the
    handler can stash it.  Matches the shape of the internal
    ``_NoopTelemetry`` in :mod:`legba.data.stack.llm.base` but
    duplicated here to keep this module's import surface minimal.
    """

    def log(self, level: int, msg: str, /, **fields: Any) -> None:
        return None

    def event(self, name: str, payload: Mapping[str, Any] | None = None) -> None:
        return None

    def span(self, name: str, /, **attrs: Any) -> Any:
        class _Span:
            def __enter__(self_) -> "_Span":
                return self_

            def __exit__(self_, *a: Any) -> None:
                return None

        return _Span()


class _BuilderHandlerContext:
    """Minimal :class:`HandlerContext` Protocol-satisfying object.

    The LLM handler base reads four attrs off ``ctx``: ``instance_id``,
    ``instance_version``, ``secrets``, ``config`` (also surfaces via
    ``cfg`` fallback), and the method ``telemetry()``.  We populate all
    of them so the handler's ``on_configure`` path completes cleanly.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        instance_version: str,
        config: LLMProviderConfig,
        secrets: _SecretsResolverAdapter,
    ) -> None:
        self.instance_id = instance_id
        self.instance_version = instance_version
        self.config = config
        self.secrets = secrets
        self._telemetry: TelemetryHandle = _NoopTelemetry()  # type: ignore[assignment]

    def telemetry(self) -> TelemetryHandle:
        return self._telemetry


# NOTE: :class:`_BuilderHandlerContext` is duck-compatible with
# :class:`HandlerContext` (the Protocol is ``@runtime_checkable``).  We
# don't emit an import-time conformance probe because constructing one
# requires a populated :class:`LLMProviderConfig` and the import side-
# effect would force eager schema discovery.  Conformance is verified
# inside :func:`build_llm_handler_from_stack_component` (the handler's
# own ``on_configure`` reads each Protocol field).
