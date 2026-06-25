# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Analyst descriptor schema (per L-101 §4).

`AnalystKind` is an *open* taxonomy per L-101 §8 and L-178 — the 9 built-in
kinds below ship with the package, and operators register additional kinds
at runtime via `AnalystKindRegistry` (or, equivalently, by inserting rows
into `vocabulary_entries` with `family='analyst_kind'`; the registry mirrors
that table on refresh).

The schema's `AnalystIdentity.kind` field accepts any string that's either a
built-in enum value OR a runtime-registered value. Built-in kinds keep their
enum form so existing `AnalystKind.OPTIMIZER`-style comparisons stay valid;
extension kinds round-trip as plain strings. `AnalystKind` extends `str` so
the two forms compare equal byte-for-byte.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .lifecycle import LifecycleState
from .target import OutputBinding, AnalystId
from .action_pack import ActionPackRef


class AnalystKind(str, Enum):
    INLINE_TARGET = "inline_target"
    CROSS_TARGET_RAW = "cross_target_raw"
    META_FINDINGS_SYNTHESIZER = "meta_findings_synthesizer"
    DETERMINISTIC = "deterministic"
    PREDICTOR = "predictor"
    CRITIC = "critic"
    OPTIMIZER = "optimizer"
    CROSS_ANALYST_CORRELATOR = "cross_analyst_correlator"
    # PIECE A — the reified-typed-Nexus producer (META analyst, LLM-typed).
    RELATIONSHIP_REIFIER = "relationship_reifier"
    # PIECE C — the ACH competing-hypotheses META analyst (canonical name;
    # `ach` is the documented alias, see competing_hypotheses.KIND_ALIASES).
    COMPETING_HYPOTHESES = "competing_hypotheses"
    CONSULT_ON_DEMAND = "consult_on_demand"
    # Altitude-3 on-demand deep analysis — the deep-consult staged Dapr
    # Workflow's submit kind (anchor §5 PIECE 4).
    DEEP_CONSULT = "deep_consult"
    # Open taxonomy — new values registered via AnalystKindRegistry (below)
    # or by inserting rows into `vocabulary_entries` with family='analyst_kind'.


# ---------------------------------------------------------------------------
# AnalystKindRegistry — runtime-extensible kind set
# ---------------------------------------------------------------------------
#
# The descriptor registry seeds this from `vocabulary_entries` (family =
# 'analyst_kind') at startup; new entries land via the registry CRUD path.
# Schema validation consults the registry to allow extension-registered
# values without touching the closed enum.

_BUILTIN_KINDS: frozenset[str] = frozenset(k.value for k in AnalystKind)


class AnalystKindRegistry:
    """Process-wide registry of analyst kinds.

    Built-in kinds are always present; runtime extensions register via
    `register()` (or via the descriptor registry's vocabulary sync, which
    pulls family='analyst_kind' rows out of `vocabulary_entries` on
    refresh + on NATS `vocabulary.updated.analyst_kind` events).

    The kind value shape mirrors the `analyst_kind` family in
    `VocabularyEntry`: lowercase snake_case (validated at insert time).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._extensions: set[str] = set()

    # -- mutation -------------------------------------------------------

    def register(self, value: str) -> None:
        """Add a new kind. Idempotent. Validates the value shape."""
        if not isinstance(value, str) or not value:
            raise ValueError(f"analyst kind must be a non-empty string, got {value!r}")
        if value != value.lower() or " " in value:
            raise ValueError(
                f"analyst kind {value!r} must be lowercase_snake_case"
            )
        if value in _BUILTIN_KINDS:
            return  # built-ins are always known
        with self._lock:
            self._extensions.add(value)

    def unregister(self, value: str) -> None:
        """Remove a runtime-registered kind. Built-ins cannot be removed.

        Used by the descriptor registry when a `vocabulary_entries` row for
        `analyst_kind` is marked deprecated.
        """
        with self._lock:
            self._extensions.discard(value)

    def replace_extensions(self, values: set[str]) -> None:
        """Atomically swap the extension set (used after a cache refresh).

        Malformed values (not lowercase_snake_case, non-string, empty) are
        silently dropped so a noisy DB snapshot can't poison the in-process
        cache. Strict callers should use `register()` which validates and
        raises.
        """
        cleaned: set[str] = set()
        for v in values:
            if not isinstance(v, str) or not v:
                continue
            if v in _BUILTIN_KINDS:
                continue
            if v != v.lower() or " " in v:
                continue
            cleaned.add(v)
        with self._lock:
            self._extensions = cleaned

    # -- read -----------------------------------------------------------

    def is_valid(self, value: str) -> bool:
        if value in _BUILTIN_KINDS:
            return True
        with self._lock:
            return value in self._extensions

    def values(self) -> set[str]:
        """Return the union of built-in + extension kinds."""
        with self._lock:
            return set(_BUILTIN_KINDS) | set(self._extensions)

    def extension_values(self) -> set[str]:
        """Return only the runtime-registered (non-built-in) kinds."""
        with self._lock:
            return set(self._extensions)

    def builtin_values(self) -> set[str]:
        return set(_BUILTIN_KINDS)


# Module-level singleton. The descriptor registry refreshes its contents
# from `vocabulary_entries` (family='analyst_kind') at start() and on each
# `vocabulary.updated.analyst_kind` NATS event.
ANALYST_KIND_REGISTRY = AnalystKindRegistry()


def register_analyst_kind(value: str) -> None:
    """Convenience: register an extension kind on the module singleton."""
    ANALYST_KIND_REGISTRY.register(value)


def is_known_analyst_kind(value: str) -> bool:
    """Convenience: check the module singleton."""
    return ANALYST_KIND_REGISTRY.is_valid(value)


class TypeSignature(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    input_type: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    output_type: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    deps_type: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$",
        default="legba.runtime.deps.StandardDeps",
    )


class AnalystIdentity(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    id: AnalystId
    name: str
    schema_uri: str = Field(pattern=r"^legba/analyst/\d+\.\d+\.\d+$")
    version: str = Field(pattern=r"^[a-f0-9]{16,64}$")
    # Open taxonomy: built-in `AnalystKind` enum values OR a runtime
    # extension registered via `ANALYST_KIND_REGISTRY`. Stored as the bare
    # string canonical value; comparisons against `AnalystKind.X` still hold
    # because the enum extends `str`.
    kind: str
    type_signature: TypeSignature
    state: LifecycleState = LifecycleState.DRAFT
    owner: str
    inherits: list[AnalystId] = Field(default_factory=list, max_length=4)

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, v: Any) -> str:
        # Accept the enum form for ergonomic in-process construction; the
        # canonical wire/storage form is the underlying string value.
        if isinstance(v, AnalystKind):
            return v.value
        if isinstance(v, str):
            return v
        raise ValueError(f"kind must be a string or AnalystKind, got {type(v).__name__}")

    @field_validator("kind", mode="after")
    @classmethod
    def _kind_in_registry(cls, v: str) -> str:
        # Shape check first — kinds outside the registry must still obey the
        # `lowercase_snake_case` rule used by `VocabularyEntry`.
        if not v or v != v.lower() or " " in v:
            raise ValueError(
                f"analyst kind {v!r} must be lowercase_snake_case"
            )
        if not ANALYST_KIND_REGISTRY.is_valid(v):
            raise ValueError(
                f"unknown analyst kind {v!r}; "
                f"register it via ANALYST_KIND_REGISTRY.register() or insert a "
                f"vocabulary_entries row with family='analyst_kind' first"
            )
        return v


class SubscriptionTargets(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    predicate: str | None = None
    data_types: list[str] = Field(default_factory=list)
    time_window: str = "24h"

    @field_validator("predicate", mode="after")
    @classmethod
    def _compile_predicate(cls, v: str | None) -> str | None:
        """Compile-time check per L-104 §5 for analyst.subscription.targets.predicate."""
        if v is None:
            return v
        from ..predicates import (
            PredicateCompilationError,
            PredicateSurface,
            compile_predicate,
        )
        try:
            compile_predicate(v, PredicateSurface.ANALYST_SUBSCRIPTION)
        except PredicateCompilationError as exc:
            raise ValueError(
                f"analyst.subscription.targets.predicate failed to compile: {exc}"
            ) from exc
        return v


class SubscriptionAnalyst(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    id: AnalystId
    data_types: list[str] = Field(default_factory=list)
    time_window: str = "24h"


class SubscriptionBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    targets: SubscriptionTargets | None = None
    other_analysts: list[SubscriptionAnalyst] = Field(default_factory=list)
    substrate: dict[str, Any] = Field(
        default_factory=lambda: {"direct_queries": False}
    )


class FieldMapping(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)

    from_path: str = Field(alias="from")
    to: str
    transform: str | None = None


class MappingBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    fields: list[FieldMapping] = Field(default_factory=list)
    schema_drift_policy: Literal[
        "fail", "warn_and_continue", "drop_unknown"
    ] = "warn_and_continue"


# ---------------------------------------------------------------------------
# Typed retry policies per failure mode (Phase 5 hardening item 5)
# ---------------------------------------------------------------------------
#
# The runtime's actor exception handler classifies failures into three
# buckets — transient / budget / hard.  Each gets its own retry policy
# declared in ``method.retry`` so a noisy upstream (transient) doesn't
# blow through a hard-failure DLQ, and budget exhaustion can pause the
# actor cleanly instead of DLQ'ing.
#
# Defaults (when ``method.retry`` is absent) mirror the prior implicit
# behavior:
#
#   * transient: 3 attempts, exponential backoff capped at 60 s.
#   * budget:    pause_until_next_window (the actor sets a cooldown until
#                the next bucket start; today the bucket is per-day UTC).
#   * hard:      DLQ on first failure, no retries.
#
# Descriptor shape:
#
#   method:
#     retry:
#       transient: { max_attempts: 5, backoff: exponential, max_delay_seconds: 30 }
#       budget:    { strategy: pause_until_next_window }
#       hard:      { strategy: dlq_and_alert, max_attempts: 1 }


class TransientRetryPolicy(BaseModel):
    """Retry policy for transient (network/5xx/429-shaped) failures."""

    model_config = ConfigDict(strict=True, extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=20)
    backoff: Literal["exponential", "linear", "constant"] = "exponential"
    initial_delay_seconds: float = Field(default=1.0, ge=0.0, le=600.0)
    max_delay_seconds: float = Field(default=60.0, ge=0.0, le=3600.0)
    multiplier: float = Field(default=2.0, ge=1.0, le=10.0)


class BudgetRetryPolicy(BaseModel):
    """Policy for budget-exhausted failures.

    ``pause_until_next_window`` — the actor sets a cooldown of
    ``cooldown_seconds`` (default 1h).
    ``demote_and_continue`` — F-2 (2026-06-09): the strategy stays in the
    schema, but no production resolver wires a fallback model yet, so its
    CURRENT behavior is an explicit, audited pause until the budget
    window resets (cooldown to the next UTC-day bucket; a
    ``budget_demotion_events`` row + a warning log record the demotion).
    Real cheap-model fallback demotion (wiring ``method.llm.fallback``
    into ``fallback_run_method``) is a declared seam — docs/SEAMS.md /
    docs/DIRECTION.md.
    ``dlq`` — give up on this run; pair with the global envelope when
    you want hard caps.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    strategy: Literal[
        "pause_until_next_window",
        "demote_and_continue",
        "dlq",
    ] = "pause_until_next_window"
    cooldown_seconds: int = Field(default=3600, ge=0, le=24 * 3600)


class HardRetryPolicy(BaseModel):
    """Policy for hard (4xx / unrecoverable) failures."""

    model_config = ConfigDict(strict=True, extra="forbid")

    strategy: Literal["dlq_and_alert", "drop", "pause"] = "dlq_and_alert"
    max_attempts: int = Field(default=1, ge=1, le=5)


class RetryBlock(BaseModel):
    """``method.retry`` — typed retry policies per failure mode.

    Absence of any sub-block keeps the prior implicit behavior.  Each
    sub-block is independently optional so an analyst can override only
    the transient policy and leave budget/hard at defaults.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    transient: TransientRetryPolicy = Field(default_factory=TransientRetryPolicy)
    budget: BudgetRetryPolicy = Field(default_factory=BudgetRetryPolicy)
    hard: HardRetryPolicy = Field(default_factory=HardRetryPolicy)


class GatherBlock(BaseModel):
    """``method.gather`` — the bounded GATHER tool-call phase for the agentic
    ``inline_target`` assessors (S5).

    The inline_target run is single-shot by default (ORIENT → one LLM call →
    REFLECT). When the analyst's effective read pack is bound (the assessor
    grants ``substrate_read`` via ``action_packs`` AND the target allows it via
    ``allowed_action_packs`` — the existing three-way agency gate, NO new
    enable flag here), the runner inserts a bounded GATHER phase that lets the
    assessor query the substrate mid-run before synthesizing. This block tunes
    that phase; it is purely advisory when no read pack is effective (the
    GATHER no-ops, degrade-not-drop, and REFLECT still lands a finding).

    Distinct from consult's multi-round ReAct loop: GATHER defaults to a SINGLE
    round to stay inside the P-1 invoke timeout + the per-descriptor token
    budget. Raise ``max_rounds`` deliberately for an assessor you want to
    investigate harder, and bump ``method.budget_tokens_per_day`` to match (the
    GATHER LLM rounds count against the SAME per-descriptor cap — no new budget
    machinery).

    Fields
    ------
    max_rounds:
        Hard cap on GATHER tool-call rounds (1..6; default 1). Wired into
        ``deps.max_rounds`` by the deps-builder, mirroring consult's
        ``options['max_tool_rounds']`` lever.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    max_rounds: int = Field(default=1, ge=1, le=6)


class MethodBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    # Per L-105 §2.1 / Wave B prereqs:
    #
    #   * ``llm_planner``      — multi-step planner with a static prompt.
    #   * ``llm_single_turn``  — one-shot Predict-style LLM call.
    #   * ``deterministic``    — pure-code handler (no LLM in the loop).
    #   * ``hybrid``           — stat or code core + optional LLM wrapper
    #                             (e.g. predictor stat forecaster + narrative).
    #   * ``react_loop``       — ReAct-style tool-using loop (consult_on_demand).
    #                             Distinct from ``llm_planner`` because the loop
    #                             dispatches tool calls between LLM rounds; the
    #                             optimizer (L-176) needs to know it's a multi-
    #                             round surface for replay shaping.
    #   * ``stat_forecaster``  — pure statistical forecaster (AutoARIMA etc.)
    #                             with no LLM in the critical path (the narrative
    #                             is decorative).  Distinct from ``hybrid`` so
    #                             the budget enforcer can zero out per-run LLM
    #                             allowances when the narrative LLM is omitted.
    #   * ``critic``           — L-175 critic kind.  LLM call but structurally
    #                             different from a single-turn finding (the
    #                             output is a rubric-graded critique).
    #   * ``dspy_compile``     — L-176 optimizer kind.  Compiles candidate
    #                             ``dspy.Module``s against a trace set.  Not
    #                             an LLM-bearing analyst surface itself — the
    #                             trial calls inside the compile loop are.
    kind: Literal[
        "llm_planner",
        "llm_single_turn",
        "deterministic",
        "hybrid",
        "react_loop",
        "stat_forecaster",
        "critic",
        "dspy_compile",
    ]
    prompt_module: str | None = None
    impl: str | None = None
    # Deterministic kind only: which sub-handler the dispatcher routes to (one of
    # `legba.data.analysts.deterministic.SUB_HANDLERS`). The runtime injects this
    # into the run `options` at fire time (cadence + coalesced trigger both),
    # falling back to `identity.id` when omitted — so a descriptor whose id
    # matches its sub-handler name (e.g. `cross_source_dedup`) needn't set it.
    sub_handler: str | None = None
    # tools are granted via AnalystDescriptor.action_packs (pivot — retired the
    # flat tools_whitelist; a pack with one tool covers the old single-tool case).
    llm: dict[str, Any] = Field(default_factory=dict)  # values are FactoryValue subclasses
    retries: int = Field(default=2, ge=0, le=10)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    budget_tokens_per_run: int | None = None
    budget_tokens_per_day: int | None = None
    # S5 — the bounded GATHER tool-call phase for agentic inline_target
    # assessors. Absent → the runner's GATHER default (single round) holds when
    # a read pack is effective; present → tunes the round cap. NEVER an opt-in
    # flag (engagement is the three-way agency gate); purely a tunable.
    gather: GatherBlock = Field(default_factory=GatherBlock)
    # Typed retry policies per failure mode (Phase 5 hardening item 5).
    # When the block is absent the runtime uses ``RetryBlock()`` defaults,
    # which match the prior implicit behavior.
    retry: RetryBlock = Field(default_factory=RetryBlock)

    # Kinds that fundamentally require a prompt_module (the LLM-bearing
    # surfaces).  ``stat_forecaster`` is intentionally NOT here — its LLM
    # narrative is optional.  ``dspy_compile`` is NOT here — the optimizer
    # operates over OTHER kinds' prompt_modules; it doesn't carry one of
    # its own at this level.
    _LLM_PROMPT_REQUIRED_KINDS = frozenset({
        "llm_planner",
        "llm_single_turn",
        "react_loop",
        "critic",
    })

    @model_validator(mode="after")
    def _impl_or_prompt(self) -> "MethodBlock":
        if self.kind in self._LLM_PROMPT_REQUIRED_KINDS and not self.prompt_module:
            raise ValueError(f"method.kind={self.kind} requires prompt_module")
        if self.kind == "deterministic" and not self.impl:
            raise ValueError("method.kind=deterministic requires impl")
        return self


class CadenceBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    trigger: str | None = None
    fallback_schedule: str | None = None
    cooldown_seconds: int = Field(default=0, ge=0)

    @field_validator("trigger", mode="after")
    @classmethod
    def _compile_trigger(cls, v: str | None) -> str | None:
        """Compile-time check per L-104 §5 for analyst.cadence.trigger."""
        if v is None:
            return v
        from ..predicates import (
            PredicateCompilationError,
            PredicateSurface,
            compile_predicate,
        )
        try:
            compile_predicate(v, PredicateSurface.CADENCE_TRIGGER)
        except PredicateCompilationError as exc:
            raise ValueError(
                f"analyst.cadence.trigger failed to compile: {exc}"
            ) from exc
        return v


class EvalBlock(BaseModel):
    """Eval-loop configuration for an analyst (per L-105).

    Fields
    ------
    rubric:
        Free-form rubric the critic kind grades the analyst's output
        against (typically JSON listing named dimensions, sometimes plain
        text). Required for any analyst the operator wants critiqued.
    judge:
        Optional pinned judge analyst id. When unset, the runtime picks
        a critic from the registered critic analysts (avoiding the
        analyzed analyst's own model per the heterogeneity guard).
    ground_truth:
        Optional reference to a ground-truth set (descriptor id or URI)
        used by the optimizer's promotion gate.
    optimizer:
        Free-form optimizer block (DSPy/GEPA hyperparameters,
        promotion policy, etc.). The L-176 optimizer reads its own
        config keys out of this dict.
    allow_self_correlated:
        Critic-loop escape hatch (per L-105 §3 + L-175). Default
        ``False`` — the critic refuses to grade an analyst's output
        with the same model that produced it (correlated noise rather
        than signal). Operators that accept the noise floor can flip
        this to ``True`` on the analyzed analyst's descriptor; the
        runtime then forwards it to the critic via
        ``options["allow_self_correlated"]`` and
        :func:`legba.data.analysts.critic._assert_heterogeneous` lets
        the call through.

        Promoted out of the ``optimizer`` dict to make the contract
        visible at registry-diff / operator-review time (a heterogeneity
        bypass shouldn't be a buried free-form-dict key).
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    rubric: str | None = None
    judge: AnalystId | None = None
    ground_truth: str | None = None
    optimizer: dict[str, Any] | None = None
    allow_self_correlated: bool = False


class GroundingBlock(BaseModel):
    """``grounding`` — analysis-time current-world-state injection (Tier 1).

    Stale-cutoff analyst LLMs (world_assessor / country_assessor) backfill
    current facts (officeholders, alliances, ongoing-conflict state) from a
    training prior that predates the present — e.g. calling the CURRENT US
    president a "former" one. The signal slice alone doesn't restate these
    facts, so the model has no in-context correction. This block opts an
    analyst INTO a deps-builder step that, before the LLM call, reads the
    substrate for CURRENT authoritative facts (the temporal-honesty gate:
    ``superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())``,
    RESTRICTED to curated/seed source rows — the provenance gate, env-overridable
    via ``LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES``) about the analyst's target geo + the
    top entities in the slice, and PREPENDS a dated "AUTHORITATIVE CURRENT
    CONTEXT" preamble to the LLM context (treat-as-ground-truth-over-prior).

    Conservative by construction: ``enabled`` defaults to ``False`` so an
    analyst that doesn't declare the block (or declares it disabled) is
    untouched — no extra substrate read, no preamble. Token-capped via
    ``max_facts``.

    Fields
    ------
    enabled:
        Master switch. ``False`` (default) → no grounding injection at all.
    scope:
        Which entity sets the resolver grounds against, in priority order:
          * ``target_geo``     — the analyst's target country/geo (the
            single most load-bearing scope: a country analyst's own head of
            state / bloc memberships).
          * ``slice_entities`` — the top entities mentioned in the signal
            slice (so a US-scoped slice grounding picks up named figures).
        Defaults to both. An empty list disables resolution (equivalent to
        ``enabled: false`` for the resolver) while keeping the block present.
    sources:
        Grounding backends:
          * ``substrate``  — the structured temporal facts + signed nexuses,
            restricted to operator-vetted provenance, rendered in the
            "AUTHORITATIVE CURRENT CONTEXT" ground-truth block.
          * ``situations`` — ongoing situation FRAMES the platform has
            clustered from recent findings, rendered in a SEPARATE, clearly-
            labelled "ASSESSED SITUATIONS" block (analysis-derived, NOT ground
            truth — never laundered into the ground-truth block). Phase 5a.
          * ``vector:world_context`` — the declared Tier-2 follow-up (a curated
            unstructured-brief collection); accepted so descriptors can
            pre-declare it, but the resolver does not act on it yet.
    max_facts:
        Hard cap on the number of current facts folded into the preamble
        (token budget). 1..200; default 30.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = False
    scope: list[Literal["target_geo", "slice_entities"]] = Field(
        default_factory=lambda: ["target_geo", "slice_entities"]
    )
    # Always-on grounding candidate names, prepended regardless of slice content
    # (the resolver matches them against facts.subject / nexuses.subject). For a
    # GLOBAL meta-analyst whose slice can be flooded by a high-volume source,
    # this guarantees the major ongoing world-state (active-conflict parties)
    # grounds even when today's slice doesn't surface them. Empty by default.
    static_candidates: list[str] = Field(default_factory=list, max_length=32)
    sources: list[
        Literal["substrate", "situations", "graph_structure", "vector:world_context"]
    ] = Field(default_factory=lambda: ["substrate"])
    max_facts: int = Field(default=30, ge=1, le=200)


class AnalystDescriptor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    identity: AnalystIdentity
    subscription: SubscriptionBlock
    mapping: MappingBlock = Field(default_factory=MappingBlock)
    method: MethodBlock
    cadence: CadenceBlock
    outputs: list[OutputBinding] = Field(default_factory=list)
    # action packs this analyst may use (∩ target.allowed_action_packs ∩
    # pack applicability, gated by governor — PIVOT §4.8). Supersedes the
    # flat tools_whitelist as the agency-grant surface.
    action_packs: list[ActionPackRef] = Field(default_factory=list)
    eval: EvalBlock | None = None
    # Tier-1 knowledge grounding — analysis-time current-world-state injection.
    # Optional + off-by-default (a descriptor without the block is unchanged);
    # opt in per-analyst (world_assessor / country_assessor) by declaring
    # ``grounding: {enabled: true}``.
    grounding: GroundingBlock | None = None

    @model_validator(mode="after")
    def _kind_constraints(self) -> "AnalystDescriptor":
        if self.identity.kind == AnalystKind.OPTIMIZER and not self.eval:
            raise ValueError("optimizer analyst must declare an eval block")
        if (
            self.identity.kind == AnalystKind.CRITIC
            and self.method.kind not in ("llm_planner", "llm_single_turn", "critic")
        ):
            raise ValueError("critic analyst method.kind must be an LLM kind")
        return self
