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
from typing import Any, Awaitable, Callable, Mapping
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
    "build_analyst_run_method",
    "build_llm_handler_from_stack_component",
    "infer_llm_subprovider",
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
        )
    elif kind == "cross_target_raw":
        trio = await _build_cross_target_raw(handler, _resolve_primary_llm)
    elif kind == "meta_findings_synthesizer":
        trio = await _build_meta_findings_synthesizer(handler, _resolve_primary_llm)
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
        trio = _build_deterministic(handler, deps)
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
    # this kind). Unset/unresolvable → None → runner uses the kind default.
    system_prompt = _resolve_prompt_module(
        getattr(descriptor.method, "prompt_module", None)
    )
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
    grounding_hook = _build_grounding_hook(descriptor, pg_pool=pg_pool)
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
    grounding_hook = _build_grounding_hook(descriptor, pg_pool=pg_pool)
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

    from .grounding import (
        SubstrateGroundingResolver,
        build_graph_structure_block,
        build_grounding_preamble,
        build_situations_block,
        collect_grounding_candidates,
        situation_scope_for_target,
    )

    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
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
    # Tier-2 (vector:world_context) is a declared follow-up; the resolver acts
    # only on the structured substrate (facts/nexuses), the situation frames, and
    # the knowledge-graph structure today. A descriptor that declares NONE of the
    # wired sources resolves nothing — surface that rather than silently injecting
    # an empty preamble.
    if not want_substrate and not want_situations and not want_graph_structure:
        logger.info(
            "analyst_deps_builder.grounding.no_wired_source analyst=%r "
            "sources=%r — only 'substrate' (facts/nexuses), 'situations', and "
            "'graph_structure' are wired today (vector:world_context is a "
            "follow-up); no preamble built",
            descriptor.identity.id, sources,
        )

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
        return "\n".join(parts) if parts else None

    return _hook


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
    handler: KindHandler,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """meta_findings_synthesizer — second-order synthesis kind.

    Same shape as :func:`_build_cross_target_raw`: the kind's
    ``MetaFindingsDeps`` Protocol asks only for an ``llm`` attribute.
    """
    llm = await resolve_llm()
    return (
        handler.run_method,
        _LLMOnlyDeps(llm=llm),
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


def _build_deterministic(
    handler: KindHandler,
    deps: StandardDeps,
) -> tuple[Callable[..., Any], Any | None, OutputKind]:
    """deterministic — code-only sub-dispatched kind.

    The kind's module-level ``run_method`` takes the
    :class:`StandardDeps` bundle directly (sub-handlers reach into
    ``deps.pg_pool`` etc.).  No LLM resolution.  Sub-handler selection
    happens at run time via ``options["sub_handler"]`` — the descriptor
    must populate that field (validated by the kind itself, not here).
    """
    return handler.run_method, deps, handler.output_kind


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
    """
    from ..data.analysts.consult_on_demand import ConsultOnDemandDeps

    llm = await resolve_llm()
    if substrate_query_port is None:
        raise AnalystDepsBuildError(
            "consult_on_demand requires a SubstrateQueryPort but none was "
            "supplied — no production implementation exists in-tree yet "
            "(see analyst_deps_builder report). Pass substrate_query_port "
            "explicitly when binding this analyst."
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
        ),
        handler.output_kind,
    )


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


def _primary_llm_component_id(descriptor: AnalystDescriptor) -> str | None:
    """Extract the StackRef ``raw`` field from ``descriptor.method.llm.primary``.

    The descriptor stores the StackRef as a model_dump (a plain dict
    with ``raw`` + ``expected_family`` keys) when the body comes back
    from the registry, OR as a live :class:`Property.StackRef` instance
    in tests that construct the descriptor in-process.  Both shapes are
    handled.
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return None
    primary = llm.get("primary")
    if primary is None:
        return None
    if isinstance(primary, Mapping):
        raw = primary.get("raw")
        return str(raw) if isinstance(raw, str) and raw else None
    raw = getattr(primary, "raw", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(primary, str) and primary:
        return primary
    return None


def _narrate_llm_component_id(descriptor: AnalystDescriptor) -> str | None:
    """Extract the StackRef ``raw`` from ``descriptor.method.llm.narrate`` (or None).

    The OPTIONAL second LLM ref for the per-phase split (journal §4.1): the voice
    (field-notes + NARRATE) plane. ``method.llm`` is an open ``dict[str, Any]``
    (schemas/analyst.py), so the ``narrate`` key needs NO schema change. Absent /
    malformed → None, and the caller leaves the voice on the primary handler
    (zero-regression). Handles the same shape variants as
    :func:`_primary_llm_component_id` (registry model_dump vs live StackRef).
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    if not isinstance(llm, Mapping):
        return None
    narrate = llm.get("narrate")
    if narrate is None:
        return None
    if isinstance(narrate, Mapping):
        raw = narrate.get("raw")
        return str(raw) if isinstance(raw, str) and raw else None
    raw = getattr(narrate, "raw", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(narrate, str) and narrate:
        return narrate
    return None


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
    """

    __slots__ = ("llm",)

    def __init__(self, llm: LLMProviderHandler) -> None:
        self.llm = llm

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
