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

import logging
import threading
from datetime import date
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

    @field_validator("state", mode="before")
    @classmethod
    def _coerce_state(cls, v: Any) -> Any:
        # strict=True disables pydantic's str→Enum coercion, and the wire form
        # (registry /typed JSON) carries the bare string ("active"). Without
        # this mirror of ``_coerce_kind``, EVERY typed-descriptor parse fails
        # `is_instance_of` and actor activation spins in a hot refetch loop —
        # the 2026-08-01 unit-fleet outage. Accept the enum for in-process
        # construction and coerce known strings on the wire path.
        if isinstance(v, str):
            return LifecycleState(v)
        return v

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
    # W1-E (2026-08-03): explicit target_id allowlist for kinds that read a
    # UNION across N named targets in a single run (cross_target_raw) rather
    # than fanning out one run per predicate-matched target (inline_target).
    # cross_target_raw.READ_SLICE has referenced `subscription.targets.id_list`
    # since it was written, but the field never existed on this model — every
    # access silently fell through `getattr(..., "id_list", None)` to the
    # target_filter/empty fallback. Declared here so the module's documented
    # contract type-validates.
    #
    # A subscription carrying `id_list` is a UNION binding, and the runtime
    # honours that on both trigger paths: `AnalystActor._cadence_targets()`
    # returns None (ONE global run — READ_SLICE re-resolves this list) instead
    # of fanning out per target, and `_analyst_ids_for_target()` registers no
    # per-target coalescing trigger. `id_list` beats `predicate` if a
    # descriptor declares both (READ_SLICE prefers it unconditionally); the
    # ignored predicate is logged, never silently dropped.
    id_list: list[str] | None = None

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


def _env_limited_dep(exc: ModuleNotFoundError) -> str | None:
    """The absent NON-legba dependency name, or ``None``.

    Mirror of ``registry.descriptor_refs._missing_dependency_name``'s
    philosophy: when an import chain dies on a third-party package, the code
    being imported exists in-tree and only this process's environment is short
    a dependency. A missing ``legba.*`` module, by contrast, is a real
    breakage and must propagate.
    """
    if exc.name and not exc.name.startswith("legba"):
        return exc.name
    return None


def _load_options_catalog() -> tuple[Any, Any] | None:
    """The handler-options resolvers, or ``None`` where this environment
    cannot import their chain (a non-legba dependency is absent — the
    registry image ships lighter than the runtime)."""
    try:
        from ..analysts.handler_options import (
            resolve_handler_options,
            resolve_kind_options,
        )
    except ModuleNotFoundError as exc:
        if dep := _env_limited_dep(exc):
            logging.getLogger(__name__).info(
                "handler-options catalog unimportable here: dependency %r "
                "absent in this environment", dep,
            )
            return None
        raise
    return resolve_handler_options, resolve_kind_options


def _is_json_option_value(value: Any) -> bool:
    """True for a value a registry ``body`` jsonb column round-trips intact.

    Scalars and flat lists of scalars only — a nested object would let a
    descriptor smuggle structure past the flat-key option catalog, and the
    house rule is flat schemas.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    if isinstance(value, (list, tuple)):
        return all(
            isinstance(v, (str, int, float, bool)) or v is None for v in value
        )
    return False


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
    # P2-T1 (unit-factory): an INLINE system prompt for a bounded reasoning unit.
    # A new inline_target unit (e.g. "leadership-transition risk") is JUST a
    # descriptor — its OWN scope predicate (subscription.targets.predicate) + its
    # OWN system prompt + its OWN eval.rubric, with NO new Python kind module.
    # ``prompt_module`` resolves a "module:attr" path to a prompt constant;
    # ``system_prompt`` lets a unit carry the prompt text VERBATIM in the
    # descriptor (no Python at all). The deps-builder prefers ``prompt_module``
    # when it resolves, else falls back to this inline string; unset on both →
    # the kind default _SYSTEM_PROMPT. Inert for kinds that ignore it.
    system_prompt: str | None = None
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
    # X-1 — operator-settable thresholds for a DETERMINISTIC sub-handler.
    #
    # Every deterministic handler was written to read its knobs out of the run
    # ``options`` mapping, but nothing ever fed descriptor values into it: this
    # block did not exist and the runtime built ``options`` from scratch at fire
    # time, so ~60 documented thresholds fleet-wide were unreachable dead
    # config. This is the missing channel. The runtime merges the validated
    # subset into the run options at fire time (``dapr_actors``) — see
    # ``legba.data.analysts.handler_options`` for the per-sub-handler catalog,
    # the reserved-key list and the loud-degrade contract.
    #
    # Deliberately a free-form ``dict[str, Any]`` (mirroring ``llm`` above and
    # the action-pack side's ``extra="allow"`` ``ToolSpec.config``) rather than
    # a typed block: the admissible keys are a property of the HANDLER, not of
    # the schema, and pinning them here would force a schema bump — and a
    # registry+runtime rebuild — every time a handler gained a knob. Structural
    # constraints that can never be legitimate (a non-string key, a private
    # ``_`` key, a value no registry row can carry, options on a kind that has
    # no handler) are refused HERE, at registration. Catalog-level problems (an
    # unknown key, an out-of-range value) degrade loudly at fire time instead of
    # refusing, because registry rows outlive code: a knob renamed in a later
    # release must not brick activation for every descriptor still carrying the
    # old name.
    #
    # Absent (the shipped state of all 162 descriptors) → contributes NOTHING to
    # the run options mapping, so every handler default resolves exactly as it
    # did before this field existed.
    options: dict[str, Any] = Field(default_factory=dict)

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
        # An LLM-bearing kind needs a system prompt — supplied EITHER as a
        # ``prompt_module`` ("module:attr" path) OR, for a P2-T1 bounded unit
        # authored entirely in the descriptor, as an inline ``system_prompt``
        # string. One of the two must be present.
        if (
            self.kind in self._LLM_PROMPT_REQUIRED_KINDS
            and not self.prompt_module
            and not (self.system_prompt and self.system_prompt.strip())
        ):
            raise ValueError(
                f"method.kind={self.kind} requires prompt_module or system_prompt"
            )
        if self.kind == "deterministic" and not self.impl:
            raise ValueError("method.kind=deterministic requires impl")
        self._check_options_structure()
        return self

    def _check_options_structure(self) -> None:
        """X-1 — structural gate on ``method.options`` (registration-time).

        Refuses ONLY what can never be a legitimate operator intent and can
        never arise from code/registry version skew:

        * a non-string or empty key, or a private ``_``-prefixed key (those
          name test hooks, not operator config);
        * a value shape no registry row can round-trip (JSON scalars, and flat
          lists of them, only).

        The "which kinds may carry options at all" question moved UP to
        :meth:`AnalystDescriptor._check_options_kind` (QW1-B): it needs
        ``identity.kind``, which a :class:`MethodBlock` cannot see. The rule it
        enforces is unchanged in spirit — a block that could only ever be inert
        is refused, because a silent inert block is exactly the dead config X-1
        exists to remove — but the admissible set is now "a deterministic
        sub-handler OR a kind that declares a catalog", not "deterministic only".

        Everything CATALOG-level — an unknown key for this handler, a value
        outside its declared range — is deliberately NOT refused here. Those
        degrade loudly at fire time (see
        :func:`legba.data.analysts.handler_options.resolve_handler_options`)
        so that a knob renamed in a later release cannot brick activation for
        descriptor rows already in the registry.
        """
        if not self.options:
            return
        for key, value in self.options.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("method.options keys must be non-empty strings")
            if key.startswith("_"):
                raise ValueError(
                    f"method.options key {key!r} is private (leading '_'): "
                    "reserved for runtime/test hooks, not operator config"
                )
            if not _is_json_option_value(value):
                raise ValueError(
                    f"method.options[{key!r}] must be a JSON scalar or a flat "
                    f"list of scalars (got {type(value).__name__})"
                )


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
          * ``narratives`` — the reified contested-claim families (mig 0102,
            written by the ``narrative_mapper`` deterministic analyst) for the
            target's scope, rendered in a SEPARATE "ASSESSED NARRATIVES" block
            (detect-only, analysis-derived; echo/lead ordering is descriptive
            publish-order timing, never a coordination verdict — the mapper's
            honesty contract rides in the block header). Empty sidecar ⇒ no
            block. Primary consumer: the narrative_coordination unit.
          * ``vector:world_context`` — opportunistic RAG (S5-T3): a semantic
            search over the curated ``world_context`` vector corpus (unstructured
            country/topic priors, doctrine summaries), rendered in a SEPARATE,
            non-citable "BACKGROUND PRIORS (context, not evidence — do not cite)"
            block BELOW the authoritative preamble. PRIOR, not evidence: never
            citable via ``[N]`` — verify semantics are untouched. Degrades to no
            block when the vector plane is unwired or the collection is empty.
          * ``open_questions`` — R-1 (the corpus_researcher backlog source): the
            bounded, DETERMINISTICALLY-ordered standing question set
            (``hypotheses.status='open_question'``), rendered in a SEPARATE
            "STANDING OPEN QUESTIONS" block instructing the analyst to prefer
            answering one of these over self-selecting a topic (a null result —
            the corpus doesn't resolve it — is a legitimate finding). Capped
            hard at 8 regardless of ``max_facts`` (see
            ``runtime.grounding._MAX_OPEN_QUESTIONS_GROUNDING``); scope-
            independent (does not consult ``scope`` / candidate names).
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
        Literal[
            "substrate",
            "situations",
            "graph_structure",
            "narratives",
            "vector:world_context",
            "open_questions",
        ]
    ] = Field(default_factory=lambda: ["substrate"])
    max_facts: int = Field(default=30, ge=1, le=200)
    # M22 — the FOCUSED ``vector:world_context`` RAG query theme. The RAG query is
    # built as a natural "<target-country> — <rag_theme>" phrase (see
    # analyst_deps_builder._world_context_query), which retrieves the curated
    # country-background corpus FAR better than the pre-M22 query (the unit name +
    # em-dash-joined slice-entity pile, which diluted the geo/topic anchor with
    # person names the officeholder-stripped corpus never contains). Set it to a
    # short phrase naming the CORPUS-PRESENT facets this unit reasons over —
    # government structure, political system, military/security, economy, society —
    # NOT the unit's abstract risk label. Only consulted when ``vector:world_context``
    # is in ``sources``; when unset, the builder falls back to a cleaned form of the
    # descriptor name. Max 200 chars.
    rag_theme: str | None = Field(default=None, max_length=200)


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
        self._check_options_kind()
        self._warn_on_dead_options()
        return self

    def _check_options_kind(self) -> None:
        """X-1/QW1-B — WHICH kinds may carry ``method.options`` at all.

        Two lanes, and nothing else:

        * ``method.kind == "deterministic"`` — the original X-1 lane; the knobs
          are read by the routed sub-handler (:data:`HANDLER_OPTIONS`);
        * an ``identity.kind`` that declares a KIND catalog
          (:data:`ANALYST_KIND_OPTIONS`, e.g. ``inline_target``) — the knobs are
          read by that kind's own ``run_method``.

        Anything else is REFUSED at registration, preserving the rule the
        original gate existed for: a ``method.options`` block that no code path
        reads could only ever be inert, and a silent inert block is exactly the
        dead config X-1 exists to remove. Refusing here (rather than warning) is
        safe because it can only ever fire on a descriptor an operator is
        writing NOW — unlike a CATALOG-level miss, which a later release can
        create for a registry row that already exists (hence warn-not-raise
        there; see :meth:`_warn_on_dead_options`).
        """
        if not self.method.options:
            return
        if self.method.kind == "deterministic":
            return
        try:
            from ..analysts.handler_options import ANALYST_KIND_OPTIONS
        except ModuleNotFoundError as exc:
            if _env_limited_dep(exc):
                # Same environment split as _warn_on_dead_options: the catalog
                # chain needs a runtime-only package (pycountry, live 08-04 —
                # this exact validator 422'd the relationship_reifier PUT).
                # The runtime validates authoritatively with full deps; a
                # dep-light process skips rather than refusing valid updates.
                logging.getLogger(__name__).info(
                    "inert-options check skipped for %s: catalog unimportable "
                    "in this environment", self.identity.id,
                )
                return
            raise

        kind = str(getattr(self.identity.kind, "value", self.identity.kind))
        if kind in ANALYST_KIND_OPTIONS:
            return
        raise ValueError(
            f"method.options is only read for method.kind=deterministic or an "
            f"analyst kind that declares an option catalog "
            f"({sorted(ANALYST_KIND_OPTIONS)}); got method.kind={self.method.kind} "
            f"identity.kind={kind} — a block here would be silently inert"
        )

    def _warn_on_dead_options(self) -> None:
        """X-1 — surface catalog-level ``method.options`` problems at REGISTER.

        The sub-handler name is only resolvable here (``method.sub_handler``
        with the ``identity.id`` fallback the runtime itself uses), so this is
        the earliest point a key can be checked against the live catalog.

        Warn-not-raise, on purpose: the same resolution runs again at fire time
        where it is authoritative, and refusing registration would make a
        renamed knob a fleet outage rather than a log line (see
        ``MethodBlock._check_options_structure``). Registering a descriptor
        whose knobs this build cannot honour is legal — it is just never
        silent.
        """
        if not self.method.options:
            return
        catalog = _load_options_catalog()
        if catalog is None:
            # The catalog's import chain needs a package this PROCESS does not
            # ship — the registry image is lighter than the runtime, and on
            # 2026-08-04 pycountry (via deterministic_handlers →
            # entity_resolution → geocode) 500'd /typed for every
            # options-bearing deterministic analyst, silencing claim_watch and
            # signal_embedder for 14h. This helper is warn-only by contract:
            # an absent dependency downgrades it to a log line, never an
            # exception out of a read path.
            logging.getLogger(__name__).info(
                "dead-options check skipped for %s: catalog unimportable in "
                "this environment", self.identity.id,
            )
            return
        resolve_handler_options, resolve_kind_options = catalog

        if self.method.kind != "deterministic":
            # QW1-B kind lane — checked against the KIND catalog, since no
            # sub-handler is routed for a non-deterministic analyst.
            resolve_kind_options(
                str(getattr(self.identity.kind, "value", self.identity.kind)),
                self.method.options,
                log_context=f"{self.identity.id}@register",
            )
            return
        resolve_handler_options(
            self.method.sub_handler or self.identity.id,
            self.method.options,
            log_context=f"{self.identity.id}@register",
        )


# ---------------------------------------------------------------------------
# S3-T1 — structured I&W indicators (a finding-payload sub-shape)
# ---------------------------------------------------------------------------
#
# A unit's forward-looking "Indicators to watch" PROSE stays EXEMPT from the
# faithfulness pass (it is forward-looking by construction — verify.py drops the
# whole section). S3-T1 adds a SEPARATE, machine-checkable structured MIRROR of
# that prose — ``FindingPayload.data.indicators[]`` — so the future I&W board can
# render resolvable indicators run-over-run AND the verify pass can hold a
# ``triggered`` indicator to a citation (an uncited triggered indicator DEMOTES
# faithfulness; ``not_observed`` / ``expired`` stay forward-looking + exempt).
#
# This lives in the schemas package (not provenance/models) because it is the
# typed contract a descriptor-driven finding's output must satisfy; a change here
# is a data/schemas/* change (rebuild BOTH registry + runtime).


class IndicatorEntry(BaseModel):
    """One structured indications-and-warning indicator (S3-T1).

    Fields
    ------
    id:
        A short, stable slug identifying the indicator across runs (so an
        ``indicator_tracker`` (S3-T2) can diff its status run-over-run).
    statement:
        The concrete, resolvable signpost (e.g. "Reservists mobilized in the
        eastern military district").
    status:
        * ``triggered``    — the signpost has fired; MUST carry >=1 ``citations``
          entry (a ``[N]`` signal index). An uncited ``triggered`` indicator is an
          unsupported span that DEMOTES the finding's faithfulness (verify.py).
        * ``not_observed`` — pre-registered but not yet seen (forward-looking → no
          citation required; exempt from faithfulness like the prose watch list).
        * ``expired``      — the ``horizon_date`` passed without the signpost
          firing (forward-looking → exempt).
    horizon_date:
        The date by which the indicator is expected to resolve (ISO ``YYYY-MM-DD``).
    first_seen:
        The date the indicator was first registered (ISO ``YYYY-MM-DD``).
    citations:
        The finding's own ASCII ``[N]`` signal-marker INDICES (the ints keying
        ``data['citations']``) that a ``triggered`` indicator rests on.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=2048)
    status: Literal["triggered", "not_observed", "expired"]
    horizon_date: date
    first_seen: date
    citations: list[int] = Field(default_factory=list, max_length=64)


def validate_indicators(value: Any) -> list[dict[str, Any]]:
    """Validate + normalize a finding payload's ``data['indicators']`` block.

    Returns the normalized list — each entry model_dumped to JSON-safe primitives
    (ISO date strings for ``horizon_date`` / ``first_seen``) so the shape stored in
    the JSONB ``data`` column is canonical and round-trips. ``None`` / absent → an
    empty list.

    Raises ``ValueError`` when the block is present but not a list, or any entry is
    not a well-formed :class:`IndicatorEntry` — the write path then routes the
    payload to the DLQ, the same fail-loud contract the other typed payload fields
    carry. Tolerant ingestion of a noisy LLM ``indicators`` array is the caller's
    job (``inline_target._coerce_indicators`` drops malformed entries BEFORE the
    payload is constructed, so this strict pass only fires on a genuine mis-write).
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("data.indicators must be a list of indicator entries")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"data.indicators[{i}] must be an object")
        out.append(IndicatorEntry(**entry).model_dump(mode="json"))
    return out


# ---------------------------------------------------------------------------
# K-2b — open questions (a finding-payload sub-shape)
# ---------------------------------------------------------------------------
#
# The inline units are single-shot JSON emitters (no tool loop), so they cannot
# call the ``open_question`` agency write tool. The route is payload-field: the
# unit's JSON MAY carry an OPTIONAL top-level ``open_questions`` array which
# lands as ``FindingPayload.data.open_questions[]`` (additive-optional — every
# existing payload without the key validates byte-for-byte unchanged, the
# ``indicators`` precedent). A post-persist conversion in the actor run path
# turns each entry into a queryable ``hypotheses`` row
# (status='open_question'); see ``inline_target.convert_open_questions``.
#
# Same placement rationale as IndicatorEntry: this is the typed contract a
# descriptor-driven finding's output must satisfy, so it lives in data/schemas
# (a change here rebuilds BOTH registry + runtime).

#: Hard cap on ``data.open_questions`` entries (the unit prompts ask for at
#: most 3; the schema tolerates a little headroom, never a flood).
MAX_OPEN_QUESTIONS: int = 5


class OpenQuestionEntry(BaseModel):
    """One genuinely-unresolved analytical question a unit run surfaced (K-2b).

    Fields
    ------
    question:
        The unresolved analytical question, in the unit's own words (e.g. a
        contradiction between cited sources, a missing confirmation for a
        load-bearing claim, an unexplained change).
    refs:
        The finding's own ASCII ``[N]`` citation INDICES (the ints keying
        ``data['citations']``) whose cited evidence raises the question. The
        conversion resolves each to its signal UUID for the hypothesis row's
        ``derived_from`` lineage; an unresolvable index degrades to
        finding-only lineage (never fabricated).
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2048)
    refs: list[int] = Field(default_factory=list, max_length=32)


def validate_open_questions(value: Any) -> list[dict[str, Any]]:
    """Validate + normalize a finding payload's ``data['open_questions']`` block.

    Mirrors :func:`validate_indicators`: ``None``/absent → ``[]``; present but
    not a list, over the :data:`MAX_OPEN_QUESTIONS` cap, or carrying a
    malformed entry → ``ValueError`` (the write path DLQs the payload).
    Tolerant ingestion of a noisy LLM array is the caller's job
    (``inline_target._coerce_open_questions`` drops malformed entries BEFORE
    the payload is constructed, so this strict pass only fires on a genuine
    mis-write).
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("data.open_questions must be a list of question entries")
    if len(value) > MAX_OPEN_QUESTIONS:
        raise ValueError(
            f"data.open_questions carries {len(value)} entries "
            f"(max {MAX_OPEN_QUESTIONS})"
        )
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"data.open_questions[{i}] must be an object")
        out.append(OpenQuestionEntry(**entry).model_dump(mode="json"))
    return out
