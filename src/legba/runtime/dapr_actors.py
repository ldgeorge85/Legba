# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dapr-native actor classes — Phase 5a daprd validation (L-002a §6).

These are the "production" actor types that run inside a Dapr Actor host
talking to a real ``daprd`` sidecar. They inherit from
:class:`dapr.actor.Actor`, persist state via ``self._state_manager``
(backed by the ``legba-actor-state`` Postgres state component), and use
Dapr Reminders for cadence (durable across sidecar restarts).

This module is the sole actor host. The pre-reshape embedded asyncio host
(``runtime/actors.py`` + ``runtime/host.py``) retired in L-205 — all actor
plumbing is daprd-native now.

Identity mapping
----------------
Dapr addresses an actor by ``(actor_type, actor_id)`` (both strings).
The actor_id scheme — ``kind::descriptor_id::content_hash[:16]`` —
maps cleanly: ``actor_type`` is ``"TargetActor"`` or ``"AnalystActor"``
(class name), ``actor_id`` is the full ``kind::id::ver16`` string. The
descriptor identity (id + version) is recoverable from the actor_id, so
clients don't need a separate lookup table.

The content-hash slice was widened from 8 to 16 chars in 2026-05 (Phase 5
hardening item 7) to remove the practical collision risk between two
descriptor versions whose hashes share an 8-char prefix. The single
authoritative constructor is :func:`legba.runtime.reconcile._default_actor_id`;
all production call sites route through it (or through
:class:`ReconcileLoop`'s injected ``actor_id_fn``). Test fixtures that
hand-roll an actor_id with a longer prefix grammar (e.g. embedding a
session prefix) must use ``[:16]`` of the descriptor version to match.

State layout
------------
Dapr state is a per-actor KV store keyed by state-name strings. For Phase
5a each actor stores:

  * ``"record"`` — the JSON-serializable subset of :class:`ActorStateRecord`
    (lifecycle, last_run_at, last_outcome, error counters, cooldown).
  * ``"source_cursors"`` — per-source cursor dict (TargetActor only).

The Dapr state component writes its own internal binary representation;
the keys + values above are what the actor SDK puts in / out via
``state_manager.set_state`` / ``get_state``. We don't read the underlying
table directly — that's an explicit guarantee Dapr makes (and that we
validate in the restart-survival test).

Dependency wiring
-----------------
Dapr's actor factory takes ``(ctx, actor_id) -> Actor``. We cannot pass
custom kwargs through. So heavy dependencies (descriptor, source
factories, pipeline factory, deps bundle, LLM handler, budget enforcer)
live in a **process-global registry** populated by the host before
``ActorRuntime.register_actor`` is called.

The registry is keyed by actor_id (so different descriptors get different
deps); when the registry has no entry for a freshly-routed actor_id, the
actor falls back to descriptor lookup via a process-registered async
resolver (see :func:`register_target_deps_resolver` /
:func:`register_analyst_deps_resolver`). The resolver typically wraps a
registry HTTP fetch + the host's standard deps-from-descriptor closure;
once it returns the deps are cached in the in-memory registry so
subsequent invocations hit the cache (Phase 5 hardening item 6).

The fallback is gated by ``LEGBA_DEPS_FALLBACK_ENABLED`` (default ``"1"``).
Set to ``"0"`` to force the legacy "host must pre-register all deps"
behavior — useful when debugging deps-registration ordering bugs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID, uuid4

import asyncpg
from dapr.actor import Actor, ActorInterface, Remindable, actormethod
from dapr.actor.id import ActorId
from enum import Enum
from pydantic import BaseModel

from ..data.provenance._core import AnalystContext
from ..data.provenance.kinds import TRACE_ONLY, OutputKind, _TraceOnly
from ..data.provenance.models import FindingPayload
from ..data.provenance.output_graph import make_conn_age_output_hook
from ..data.provenance.receipts import RuntimeReceiptChain
from ..data.provenance.writes import write_analyst_output
from ..data.schemas.analyst import AnalystDescriptor
from ..data.schemas.target import TargetDescriptor
from .analyst_method import AnalystMethodResult, LLMAnalystRunner
from .budget import BudgetEnforcer
from .dapr_cron import cron_to_reminder_timing
from .deps import StandardDeps
from .lifecycle import ACTIVE, DRAFT, PAUSED, RETIRED, LifecycleEvent, LifecycleFSM
from .logging_setup import (
    bind_run_log_context as _bind_run_log_context,
    reset_run_log_context as _reset_run_log_context,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run outcomes — relocated from the retired embedded `actors.py` (L-205).
# `dapr_actors.py` is the production path; the enum + the factory-dict unwrap
# helper live here so the embedded host can retire cleanly.
# ---------------------------------------------------------------------------


class ActorRunOutcome(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAIL = "transient_fail"
    BUDGET_THROTTLED = "budget_throttled"
    HARD_FAIL = "hard_fail"
    NOOP = "noop"  # nothing to do (no new signals, etc.)


_FACTORY_KEY_HINTS = (
    "regex", "max_length", "minimum", "maximum", "options",
    "fetcher", "expected_family", "schema_fetcher",
    "factory_kind", "item_kind", "key_kind", "value_kind",
)


def _unwrap_factory_dict(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a Property-factory-shaped dict to the bare-value dict expected
    by handler ``config_schema`` constructors.

    The descriptor stores property-factory values as small dicts. Their
    in-memory pydantic dump looks like ``{"raw": "...", "ui_hint": {},
    "regex": None, "max_length": None}`` — the ClassVar ``factory_kind`` is
    not in the dump. The recognition heuristic:

      * Mapping has a ``raw`` key AND every sibling key is in the
        FACTORY_KEY_HINTS allow-list OR equal to ``ui_hint``.

    Recursive — nested mappings (e.g., ``method.llm``) get the same
    treatment. The ``raw`` value itself can be a dict (e.g., OAuth2),
    in which case it's returned as-is.
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, Mapping):
            siblings = set(v.keys()) - {"raw", "ui_hint"}
            looks_like_factory = "raw" in v and (
                not siblings or siblings.issubset(set(_FACTORY_KEY_HINTS))
            )
            if looks_like_factory:
                out[k] = v["raw"]
            else:
                out[k] = _unwrap_factory_dict(v)
        elif isinstance(v, list):
            out[k] = [
                _unwrap_factory_dict(i) if isinstance(i, Mapping) else i
                for i in v
            ]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Process-global dependency registry
# ---------------------------------------------------------------------------


class _TargetDeps(BaseModel):
    """Constructor-time dependencies for a TargetActor. Stored in the
    process-global registry below. Not Dapr-state — purely in-memory.

    pydantic-arbitrary-types is enabled because StandardDeps holds a
    Pool object that isn't JSON-able. We never serialize this; pydantic
    is just being used for typed-dict ergonomics.

    Source-first (L-205 / B5): the legacy target-owned source+pipeline
    factories (``source_factory`` / ``pipeline_factory``) were removed
    along with the dormant E2 pull loop. SourceActor owns acquisition and
    signals are target-agnostic, so a TargetActor needs only its descriptor
    + the StandardDeps bundle (pg pool for the discovery materialiser).
    """

    model_config = {"arbitrary_types_allowed": True}

    descriptor: TargetDescriptor
    deps: StandardDeps


class _AnalystDeps(BaseModel):
    """Constructor-time dependencies for an AnalystActor.

    Fields
    ------
    descriptor:
        The analyst's :class:`AnalystDescriptor` (typed, post-registry).
    deps:
        :class:`StandardDeps` bundle (pg pool, NATS publisher, secrets).
    run_method:
        Callable the actor invokes for the LLM/deterministic phase.  The
        host normally wires this to the kind module's ``run_method``
        wrapped in ``functools.partial(run_method, deps=kind_deps)`` so
        the actor's call site can stay two-arg (back-compat with the
        spike) while the kind-internal signature is 3-arg per L-102.  A
        bare :class:`AnalystRunFn` is also accepted.
    kind_deps:
        Optional kind-specific deps bundle (e.g.
        :class:`legba.data.analysts.inline_target.InlineTargetDeps`).
        When set the actor calls ``run_method(inputs, options, kind_deps)``;
        otherwise it falls back to the legacy two-arg
        ``run_method(inputs, options)`` shape used by ``LLMAnalystRunner``.
    output_kind:
        :class:`OutputKind` to use when writing the run's output row, OR the
        ``TRACE_ONLY`` sentinel for META analyst kinds (relationship_reifier /
        competing_hypotheses / the deterministic maintenance sub-handlers) whose
        real product is side-written — those keep their hash-chained
        ``analyst_traces`` row but write NO ``analyst_outputs`` FINDING. Defaults
        to ``OutputKind.FINDING`` per the spike.  Per-kind overrides flow in via
        the analysts package's ``OUTPUT_KIND`` constant; the per-run effective
        value is resolved at write time by ``_resolve_effective_output_kind``.
    read_slice:
        Optional per-kind substrate-slice reader.  When provided, the
        actor invokes it instead of the default signals-only reader.
        Per-kind readers come from the analyst module's ``READ_SLICE``
        constant.
    budget:
        Optional budget enforcer.  Pre-call checks gate the run; post-
        call records flush back into ``budget_ledger``.
    fallback_run_method:
        Optional fallback ``run_method`` the actor switches to on budget
        exhaustion when ``method.retry.budget.strategy ==
        "demote_and_continue"`` (Phase 5 hardening item 4). Wraps the
        same kind's ``run_method`` against a cheaper LLM (typically the
        ``method.llm.fallback`` StackRef — e.g. an analyst whose primary
        is Anthropic falls back to ``llm.primary.openai_compat``).
    fallback_kind_deps:
        Kind-deps bundle for ``fallback_run_method``. Same shape as
        ``kind_deps`` — the runtime dispatches with this bundle when
        the actor is in demoted state.
    primary_llm_ref:
        StackRef path of the analyst's primary LLM (e.g.
        ``llm.anthropic.claude_opus_4_7``).  Stamped into
        ``budget_demotion_events.primary_llm`` for audit.
    fallback_llm_ref:
        StackRef path of the fallback LLM. Stamped into
        ``budget_demotion_events.fallback_llm`` for audit.
    receipt_chain:
        Per-analyst :class:`RuntimeReceiptChain` (L-107 §7). When set,
        the actor's run path calls ``receipt_chain.record(...)`` AFTER
        the analyst-output INSERT so every run links into the analyst's
        tamper-evident SHA-256 chain. Default ``None`` for back-compat
        with the spike integration test, which builds ``_AnalystDeps``
        directly without a chain. The production deps resolver
        (:func:`legba.runtime.dapr_host`) always populates this via
        :func:`legba.runtime.receipt_chain_factory.build_receipt_chain_for_analyst`.
    """

    model_config = {"arbitrary_types_allowed": True}

    descriptor: AnalystDescriptor
    deps: StandardDeps
    run_method: Any  # AnalystRunFn (Callable)
    kind_deps: Any | None = None
    output_kind: OutputKind | _TraceOnly = OutputKind.FINDING
    read_slice: Any | None = None
    budget: BudgetEnforcer | None = None
    fallback_run_method: Any | None = None
    fallback_kind_deps: Any | None = None
    primary_llm_ref: str = ""
    fallback_llm_ref: str = ""
    receipt_chain: RuntimeReceiptChain | None = None
    # A-3c: EscalationBinding for the escalate_finding pack — set by the
    # host resolver iff the analyst's action_packs grant it (fail-loud when
    # granted but unbindable). When a landed FINDING crosses the pack's
    # severity gate, the run path fires `escalate` through the full agency
    # pipeline. None = no escalation capability.
    escalation: Any | None = None
    # S5: base ``substrate_read`` AgencyToolBinding for an agentic inline_target
    # assessor — set by the host resolver iff the analyst grants `substrate_read`
    # AND its kind is inline_target. The run path re-points it to the running
    # target's allow-list per run (`for_target`) and passes the result via
    # ``options['agency_binding']`` so the kind's GATHER phase engages ONLY when
    # the (assessor, target) read pack is EFFECTIVE. None = no GATHER capability
    # (the single-shot path). The grant leg is the analyst's; the allow leg is
    # the target's — the existing three-way agency gate, no new flag.
    gather_binding: Any | None = None
    # SEAM #22: base write/web AgencyToolBindings for an agentic inline_target
    # assessor — set by the host resolver iff the assessor ALSO grants the
    # `web_access` and/or `propose_facts` packs. Shape:
    #   {"bindings": {tool_name -> base AgencyToolBinding},
    #    "web_fragments": [str]|None, "write_fragments": [str]|None}
    # The base bindings carry the assessor-constant legs (agency, pack, grants);
    # the run path re-points each to the running target's allow-list per run
    # (`for_target`, COPY-ON-WRITE) AND injects the per-run WritebackContext into
    # the write binding's ToolContext (also copy-on-write — never mutating the
    # shared base, which is the documented per-run race risk). None = no
    # write/web GATHER capability (read-only GATHER, or single-shot).
    gather_write_bindings: dict[str, Any] | None = None
    # P0-T2 faithfulness verify — the resolved judge LLM handler for the
    # MANDATORY post-finding verify pass. Set by the host resolver iff the
    # inline_target descriptor declares ``method.llm.verify`` AND the
    # ``LEGBA_VERIFY_LLM_JUDGE`` flag gates the judge ON (the deterministic floor
    # runs regardless of this field). ``None`` → the verify pass degrades to its
    # deterministic citation-presence floor labelled 'judge-unavailable' (it
    # NEVER fabricates a judge number). Scoped to the inline_target finding kind.
    verify_judge: Any | None = None


_TARGET_DEPS: dict[str, _TargetDeps] = {}
_ANALYST_DEPS: dict[str, _AnalystDeps] = {}

# Per-actor demotion state (Phase 5 hardening item 4 — auto-demote on
# budget exhaustion). Keyed by actor_id; value = ISO-8601 timestamp of
# the bucket boundary at which the demotion should expire (today: end
# of UTC day). The actor consults this dict at run start; if present and
# unexpired, dispatches via ``_AnalystDeps.fallback_run_method`` instead
# of the primary ``run_method``. Cleared on bucket rollover OR on
# explicit ``clear_analyst_demotion``.
_ANALYST_DEMOTED_UNTIL: dict[str, str] = {}

# When the global envelope hits, this flag flips ALL analysts to fallback
# for the current bucket — set by whichever actor's precall_check first
# sees global_exhausted. The string value is the ISO bucket-end timestamp.
_GLOBAL_DEMOTED_UNTIL: str | None = None


def _set_analyst_demoted(actor_id: str, until_iso: str) -> None:
    """Mark an analyst actor as demoted until ``until_iso``. Test/host hook."""
    _ANALYST_DEMOTED_UNTIL[actor_id] = until_iso


def _set_global_demoted(until_iso: str | None) -> None:
    """Set/clear the global-demote flag. Test/host hook."""
    global _GLOBAL_DEMOTED_UNTIL
    _GLOBAL_DEMOTED_UNTIL = until_iso


def _is_actor_demoted(actor_id: str, now: datetime) -> bool:
    """Return True iff the actor (or the global envelope) is currently demoted."""
    # Global demotion supersedes per-analyst — when set, every actor demotes.
    if _GLOBAL_DEMOTED_UNTIL is not None:
        try:
            until = datetime.fromisoformat(_GLOBAL_DEMOTED_UNTIL)
            if until > now:
                return True
        except ValueError:
            pass
    per_actor = _ANALYST_DEMOTED_UNTIL.get(actor_id)
    if not per_actor:
        return False
    try:
        until = datetime.fromisoformat(per_actor)
    except ValueError:
        return False
    return until > now


def _bucket_end_iso(now: datetime) -> str:
    """Return ISO-8601 end-of-bucket timestamp for ``now``.

    The bucket is per-day UTC (matches ``budget_ledger.bucket``). The
    end-of-bucket boundary is the next-day midnight UTC.
    """
    next_day = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1))
    return next_day.isoformat()


def clear_analyst_demotion(actor_id: str | None = None) -> None:
    """Clear demotion state. Test hook + bucket-rollover handler.

    With ``actor_id=None`` clears every actor + the global flag (used by
    a midnight cron job in production; tests use it for cleanup).
    """
    if actor_id is None:
        _ANALYST_DEMOTED_UNTIL.clear()
        _set_global_demoted(None)
        return
    _ANALYST_DEMOTED_UNTIL.pop(actor_id, None)


# Fallback resolvers — populated by the host at startup. Called on cache
# miss to reconstruct deps from the descriptor body (typically via a
# registry HTTP lookup followed by the host's standard
# build-deps-from-descriptor closure). Resolver returns ``None`` when the
# descriptor can't be found.
_TargetDepsResolver = Callable[[str], Awaitable["_TargetDeps | None"]]
_AnalystDepsResolver = Callable[[str], Awaitable["_AnalystDeps | None"]]

_TARGET_DEPS_RESOLVER: _TargetDepsResolver | None = None
_ANALYST_DEPS_RESOLVER: _AnalystDepsResolver | None = None

_DEPS_FALLBACK_ENV = "LEGBA_DEPS_FALLBACK_ENABLED"

#: §4.9 — the in-actor GATHER kinds whose per-run binding is re-pointed to the
#: target (inline_target) or self-allowed for the META path (journal_assessor).
#: Was a hard-coded `== "inline_target"` check at the re-point site; this set is
#: the generalization. Kept in lock-step with dapr_host._GATHER_KINDS.
_GATHER_BINDING_KINDS: frozenset[str] = frozenset(
    {"inline_target", "journal_assessor"}
)

#: Opt-in flag for the AGE :DerivedFrom output-lineage mirror (D3). OFF by
#: default — the relational derived_from[] array + recursive-CTE lineage is the
#: source of truth; the graph edge is enrichment, and the per-write MERGE is
#: only paid when an operator sets this.
_AGE_DERIVED_FROM_ENV = "LEGBA_AGE_DERIVED_FROM"


def _fallback_enabled() -> bool:
    """Honor ``LEGBA_DEPS_FALLBACK_ENABLED`` (default true).

    Empty / unset / "1" / "true" / "yes" → enabled.
    Anything else (notably ``"0"``, ``"false"``) → disabled.
    """
    raw = os.environ.get(_DEPS_FALLBACK_ENV, "1").strip().lower()
    return raw in {"", "1", "true", "yes", "on"}


def _age_derived_from_enabled() -> bool:
    """Honor ``LEGBA_AGE_DERIVED_FROM`` (default OFF).

    Only "1"/"true"/"yes"/"on" enable the AGE output-lineage edge mirror;
    unset/empty/anything-else keeps it off (relational lineage unaffected).
    """
    raw = os.environ.get(_AGE_DERIVED_FROM_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _descriptor_declares_verify(descriptor: Any) -> bool:
    """True iff the descriptor declares ``method.llm.verify`` (the P0-T2 verify
    OPT-IN).

    The verify DISPATCH discriminator for the ``meta_findings_synthesizer`` kind:
    both the per-country and the GLOBAL/world composition carry a
    ``method.llm.verify`` block; the OLD global ``meta_synthesizer`` does NOT — so
    keying on this predicate (rather than ``bool(target_id)``) fires the
    faithfulness verify for the target-LESS world composition while keeping the
    old global meta excluded. Lazily imports ``_verify_llm_component_id``
    (runtime-side; the lazy import avoids any module load cycle). Any import/parse
    error → ``False`` (verify degrades to not-firing, never crashes a run).
    """
    try:
        from .analyst_deps_builder import _verify_llm_component_id
    except Exception:  # pragma: no cover — defensive; keeps dispatch total
        return False
    try:
        return _verify_llm_component_id(descriptor) is not None
    except Exception:  # pragma: no cover
        return False


def register_target_deps(actor_id: str, deps: _TargetDeps) -> None:
    """Host calls this before any ActorProxy invocation can route to actor_id."""
    _TARGET_DEPS[actor_id] = deps
    logger.info("dapr_actors.target.deps.registered actor_id=%s", actor_id)


def register_analyst_deps(actor_id: str, deps: _AnalystDeps) -> None:
    _ANALYST_DEPS[actor_id] = deps
    logger.info("dapr_actors.analyst.deps.registered actor_id=%s", actor_id)


def register_target_deps_resolver(resolver: _TargetDepsResolver | None) -> None:
    """Install / clear the global TargetActor deps fallback resolver.

    Called by the host at startup with a closure that:

      1. Parses the actor_id (``kind::descriptor_id::ver16``) to extract
         the descriptor_id,
      2. Fetches the descriptor body via the registry HTTP client,
      3. Builds a :class:`_TargetDeps` from the descriptor using the
         host's standard factories (source / pipeline / deps bundle),
      4. Returns the constructed deps (the caller caches them in
         ``_TARGET_DEPS``).

    Pass ``None`` to clear the resolver (test hook + ops kill-switch).
    """
    global _TARGET_DEPS_RESOLVER
    _TARGET_DEPS_RESOLVER = resolver
    logger.info(
        "dapr_actors.target.deps_resolver.%s",
        "registered" if resolver else "cleared",
    )


def register_analyst_deps_resolver(resolver: _AnalystDepsResolver | None) -> None:
    """Install / clear the global AnalystActor deps fallback resolver.

    Mirrors :func:`register_target_deps_resolver` for the analyst side.
    """
    global _ANALYST_DEPS_RESOLVER
    _ANALYST_DEPS_RESOLVER = resolver
    logger.info(
        "dapr_actors.analyst.deps_resolver.%s",
        "registered" if resolver else "cleared",
    )


async def _resolve_target_deps(actor_id: str) -> _TargetDeps | None:
    """Look up a TargetActor's deps, falling back to the resolver on miss.

    Phase 5 hardening item 6: post-restart, the in-memory ``_TARGET_DEPS``
    is empty until the host re-registers. If an ActorProxy invocation
    arrives before that point we'd otherwise hard-fail. With the fallback
    enabled we fetch the descriptor via the registry, reconstruct deps,
    cache, and proceed.

    Returns ``None`` when:

      * the cache has no entry AND the fallback env flag is off, OR
      * the fallback is on but no resolver is registered, OR
      * the resolver couldn't find the descriptor.

    Errors raised by the resolver propagate (caller logs and returns
    ``hard_fail``) — we never silently swallow a fetch error here.
    """
    deps = _TARGET_DEPS.get(actor_id)
    if deps is not None:
        return deps
    if not _fallback_enabled():
        return None
    resolver = _TARGET_DEPS_RESOLVER
    if resolver is None:
        return None
    logger.info(
        "dapr_actors.target.deps.fallback.lookup actor_id=%s", actor_id,
    )
    deps = await resolver(actor_id)
    if deps is not None:
        _TARGET_DEPS[actor_id] = deps
        logger.info(
            "dapr_actors.target.deps.fallback.cached actor_id=%s", actor_id,
        )
    else:
        logger.warning(
            "dapr_actors.target.deps.fallback.miss actor_id=%s "
            "(resolver could not reconstruct deps)",
            actor_id,
        )
    return deps


async def _resolve_analyst_deps(actor_id: str) -> _AnalystDeps | None:
    """AnalystActor mirror of :func:`_resolve_target_deps`."""
    deps = _ANALYST_DEPS.get(actor_id)
    if deps is not None:
        return deps
    if not _fallback_enabled():
        return None
    resolver = _ANALYST_DEPS_RESOLVER
    if resolver is None:
        return None
    logger.info(
        "dapr_actors.analyst.deps.fallback.lookup actor_id=%s", actor_id,
    )
    deps = await resolver(actor_id)
    if deps is not None:
        _ANALYST_DEPS[actor_id] = deps
        logger.info(
            "dapr_actors.analyst.deps.fallback.cached actor_id=%s", actor_id,
        )
    else:
        logger.warning(
            "dapr_actors.analyst.deps.fallback.miss actor_id=%s "
            "(resolver could not reconstruct deps)",
            actor_id,
        )
    return deps


def clear_deps_registry() -> None:
    """Test hook — clears all registered deps + resolvers. Safe to call any time."""
    global _TARGET_DEPS_RESOLVER, _ANALYST_DEPS_RESOLVER
    _TARGET_DEPS.clear()
    _ANALYST_DEPS.clear()
    _TARGET_DEPS_RESOLVER = None
    _ANALYST_DEPS_RESOLVER = None
    clear_analyst_demotion(None)


def evict_analyst_deps_for_descriptor(descriptor_id: str) -> int:
    """Drop every cached AnalystActor deps entry for ``descriptor_id`` — the
    old primary AND all per-target workers (``analyst::<descriptor_id>::*``).

    Why this exists: per-target WORKER actor ids are version-LESS by design
    (the target_id sits in the slot the primary fills with the descriptor
    content-hash — see :func:`worker_actor_id`). So a descriptor edit does NOT
    change a worker's id, and the otherwise-forever ``_ANALYST_DEPS[worker_id]``
    cache (see :func:`_resolve_analyst_deps`) would keep serving the OLD
    prompt/method/budget/gates until a full runtime restart. The reconcile
    executor calls this on the new-version ``CREATE_ACTOR`` so the next fire
    re-resolves head deps through the fallback resolver.

    Descriptor ids are ``::``-free slugs, so the ``analyst::<id>::`` prefix is
    an exact namespace match. Returns the number of entries evicted.
    """
    prefix = f"analyst::{descriptor_id}::"
    stale = [aid for aid in _ANALYST_DEPS if aid.startswith(prefix)]
    for aid in stale:
        del _ANALYST_DEPS[aid]
    if stale:
        logger.info(
            "dapr_actors.analyst.deps.evicted descriptor_id=%s count=%d "
            "(version bump — workers will re-resolve head)",
            descriptor_id, len(stale),
        )
    return len(stale)


# ---------------------------------------------------------------------------
# A2 concurrency — per-(analyst, target) worker actor ids + fan-out
# ---------------------------------------------------------------------------
#
# The PRIMARY analyst actor (id ``analyst::<descriptor_id>::<ver16>``) keeps
# the cadence reminder. On each tick it FANS OUT one run per matched target to
# a per-target WORKER actor (id ``analyst::<descriptor_id>::<target_id>``).
# Distinct id ⇒ distinct Dapr virtual actor ⇒ own turn-queue ⇒ concurrent
# per-target runs, instead of ~19 countries serializing through the primary's
# single queue.
#
# The worker carries the SAME segment-1 (descriptor_id) as the primary, so the
# analyst deps fallback resolver (``split("::", 2)[1]`` → head descriptor)
# reconstructs the analyst's deps with NO new registration. Workers
# lazy-activate inside ``run`` and register NO reminder (only the primary does).
#
# Fan-out is bounded-concurrent (chunked at ``_FANOUT_CHUNK``) to avoid a
# 19-wide LLM thundering herd against the budget envelope + provider limits.

from .actor_ids import (  # noqa: F401  -- re-export: public API stability (#93)
    _DEFAULT_CRITIC_FANOUT_MAX,
    _FANOUT_CHUNK,
    _critic_fanout_max,
    _split_actor_id,
    _worker_actor_id,
    _worker_proxy_factory,
    reminder_guard_decision,
)


# ---------------------------------------------------------------------------
# Actor interfaces (typed contract for ActorProxy invocations)
# ---------------------------------------------------------------------------


class TargetActorInterface(ActorInterface):
    """Wire-typed surface for TargetActor. Used by ActorProxy clients."""

    @actormethod(name="activate")
    async def activate(self) -> dict[str, Any]: ...

    @actormethod(name="pause")
    async def pause(self) -> dict[str, Any]: ...

    @actormethod(name="resume")
    async def resume(self) -> dict[str, Any]: ...

    @actormethod(name="retire")
    async def retire(self) -> dict[str, Any]: ...

    @actormethod(name="run")
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    @actormethod(name="get_state")
    async def get_state(self) -> dict[str, Any]: ...


class AnalystActorInterface(ActorInterface):
    @actormethod(name="activate")
    async def activate(self) -> dict[str, Any]: ...

    @actormethod(name="pause")
    async def pause(self) -> dict[str, Any]: ...

    @actormethod(name="resume")
    async def resume(self) -> dict[str, Any]: ...

    @actormethod(name="retire")
    async def retire(self) -> dict[str, Any]: ...

    @actormethod(name="run")
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    @actormethod(name="get_state")
    async def get_state(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# TargetActor (Dapr-native)
# ---------------------------------------------------------------------------


class TargetActor(Actor, TargetActorInterface, Remindable):
    """Dapr-native TargetActor.

    Lifecycle hooks
    ---------------
      ``_on_activate``   — re-hydrate dependency handles from the process
                            registry; ensure persistent state record exists.
      ``_on_deactivate`` — flush any pending in-memory state.

    Reminders
    ---------
    The descriptor's per-source schedules become Dapr Reminders named
    ``run_source_<source_id>``. ``receive_reminder`` dispatches to the run
    path, restricting the source pull to that source_id only.
    """

    # ------------------------------------------------------------------ lifecycle

    async def _on_activate(self) -> None:
        actor_id = self.id.id
        logger.info("dapr_actors.target.activate actor_id=%s", actor_id)
        deps = await _resolve_target_deps(actor_id)
        if deps is None:
            # Cache miss + resolver miss (fallback off, no resolver, or
            # descriptor not found in registry). Surface the gap loudly so
            # ops can spot the missing registration / unreachable registry.
            logger.warning(
                "dapr_actors.target.activate.no_deps actor_id=%s "
                "(host did not call register_target_deps and registry "
                "fallback did not resolve)",
                actor_id,
            )
            return
        # Ensure the lifecycle record exists in Dapr state.
        existing = await self._get_record()
        if existing is None:
            rec: dict[str, Any] = {
                "actor_id": actor_id,
                "actor_kind": "target",
                "descriptor_id": deps.descriptor.identity.id,
                "descriptor_version": deps.descriptor.identity.version,
                "lifecycle": DRAFT,
                "last_run_at": None,
                "last_outcome": None,
                "cooldown_until": None,
                "error_count": 0,
                "last_error": None,
            }
            fsm = LifecycleFSM(state=DRAFT)
            fsm.transition(LifecycleEvent.CONFIGURE, initiated_by="dapr_on_activate")
            fsm.transition(LifecycleEvent.ACTIVATE, initiated_by="dapr_on_activate")
            rec["lifecycle"] = fsm.state
            await self._set_record(rec)
            await self._state_manager.set_state("source_cursors", {})
            await self._state_manager.save_state()

        # Source-first (L-205): SourceActor owns acquisition. A target's
        # ``sources`` are SourceRef subscriptions that the fan-out engine
        # delivers to; the target is a PASSIVE SUBSCRIBER and registers NO
        # poll reminder. (The legacy inline-SourceBinding poll path was retired.)

    async def _on_deactivate(self) -> None:
        logger.info("dapr_actors.target.deactivate actor_id=%s", self.id.id)
        await self._state_manager.save_state()

    async def receive_reminder(
        self,
        name: str,
        state: bytes,
        due_time: timedelta,
        period: timedelta,
        ttl: timedelta | None = None,
    ) -> None:
        """Dapr-Reminder callback — fires the run path for one source."""
        logger.info(
            "dapr_actors.target.reminder.fired actor_id=%s reminder=%s",
            self.id.id, name,
        )
        # L-205 retired the target-owned poll path; any surviving
        # ``run_source_*`` reminder is scheduler pollution from a pre-pivot
        # actor generation. Self-disarm on fire (A-1).
        if name.startswith("run_source_"):
            logger.info(
                "dapr_actors.target.reminder.legacy actor_id=%s reminder=%s "
                "— unregistering (L-205 retired path)", self.id.id, name,
            )
            try:
                await self.unregister_reminder(name)
            except Exception as exc:
                logger.warning(
                    "dapr_actors.target.reminder.unregister_failed "
                    "actor_id=%s err=%s", self.id.id, exc,
                )
            return
        # A-1 belt-and-braces: refuse stale fires (version no longer head /
        # actor not active) and self-disarm provably-stale reminders.
        _kind, _descriptor_id, tail = _split_actor_id(self.id.id)
        rec = await self._get_record()
        deps = await _resolve_target_deps(self.id.id)
        head = (
            deps.descriptor.identity.version if deps is not None else None
        )
        decision = reminder_guard_decision(
            record_lifecycle=(rec or {}).get("lifecycle"),
            own_tail=tail,
            head_version=head,
        )
        if decision == "unregister":
            logger.info(
                "dapr_actors.target.reminder.stale actor_id=%s reminder=%s "
                "— unregistering", self.id.id, name,
            )
            try:
                await self.unregister_reminder(name)
            except Exception as exc:
                logger.warning(
                    "dapr_actors.target.reminder.unregister_failed "
                    "actor_id=%s err=%s", self.id.id, exc,
                )
            return
        if decision == "skip":
            logger.info(
                "dapr_actors.target.reminder.skip actor_id=%s lifecycle=%s",
                self.id.id, (rec or {}).get("lifecycle"),
            )
            return
        try:
            payload = json.loads(state.decode("utf-8")) if state else {}
        except Exception:
            payload = {}
        # Dispatch as if it were a method invocation.
        await self.run({"trigger_kind": "reminder", "reminder_state": payload})

    # ------------------------------------------------------------------ ActorInterface methods

    async def activate(self) -> dict[str, Any]:
        """Explicit activate — idempotent, returns the current state record.

        Also resurrects a PAUSED/RETIRED record to ACTIVE: activate() is only
        ever driven when the descriptor head declares this version active, so a
        parked record (operator-paused then re-activated, or a superseded
        version restored as head via rollback) is brought back to ACTIVE. The
        NORMAL supersede path drives RETIRE_ACTOR (retire()) and stays terminal.
        """
        rec = await self._get_record()
        if rec is None:
            # _on_activate hasn't run yet (shouldn't happen since Dapr calls
            # it before any method, but defensive); kick it now.
            await self._on_activate()
            rec = await self._get_record()
        if rec is not None and rec.get("lifecycle") in (PAUSED, RETIRED):
            prior = rec["lifecycle"]
            rec["lifecycle"] = ACTIVE
            await self._set_record(rec)
            await self._state_manager.save_state()
            logger.info(
                "dapr_actors.target.resurrect actor_id=%s from=%s -> active "
                "(descriptor head declares active)",
                self.id.id, prior,
            )
        return rec or {}

    async def pause(self) -> dict[str, Any]:
        rec = await self._get_record()
        if rec is None:
            # No record = the target actor was never created (a descriptor
            # transitioned straight to paused). Nothing to park; no-op rather
            # than 500 (mirrors retire()'s no-record path).
            return {"actor_id": self.id.id, "lifecycle": PAUSED}
        # Idempotent — re-pausing a paused target no-ops rather than 500'ing.
        fsm = LifecycleFSM(state=rec["lifecycle"])
        fsm.transition_idempotent(LifecycleEvent.PAUSE, initiated_by="actor_pause")
        rec["lifecycle"] = fsm.state
        await self._set_record(rec)
        await self._state_manager.save_state()
        return rec

    async def resume(self) -> dict[str, Any]:
        """PAUSED → ACTIVE (RESUME). A passive-subscriber target carries no
        reminder, so resume only flips the lifecycle record back. Idempotent on
        an already-active record; raises on a genuinely-illegal source state."""
        rec = await self._get_record()
        if rec is None:
            # No record = never created; the descriptor head wants it ACTIVE —
            # create + activate fresh rather than 500.
            return await self.activate()
        fsm = LifecycleFSM(state=rec["lifecycle"])
        fsm.transition_idempotent(LifecycleEvent.RESUME, initiated_by="actor_resume")
        rec["lifecycle"] = fsm.state
        await self._set_record(rec)
        await self._state_manager.save_state()
        return rec

    async def retire(self) -> dict[str, Any]:
        rec = await self._get_record()
        if rec is None:
            deps = await _resolve_target_deps(self.id.id)
            if deps is None:
                return {"actor_id": self.id.id, "lifecycle": RETIRED}
            rec = {
                "actor_id": self.id.id,
                "actor_kind": "target",
                "descriptor_id": deps.descriptor.identity.id,
                "descriptor_version": deps.descriptor.identity.version,
                "lifecycle": RETIRED,
            }
        else:
            # Idempotent — re-retiring an already-retired target no-ops.
            fsm = LifecycleFSM(state=rec["lifecycle"])
            fsm.transition_idempotent(LifecycleEvent.RETIRE, initiated_by="actor_retire")
            rec["lifecycle"] = fsm.state
        await self._set_record(rec)
        await self._state_manager.save_state()
        return rec

    async def get_state(self) -> dict[str, Any]:
        """Inspect-only — return the current Dapr-state record."""
        rec = await self._get_record() or {}
        ok, cursors = await self._state_manager.try_get_state("source_cursors")
        rec["source_cursors"] = cursors if ok else {}
        return rec

    async def run(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the target's per-tick work.

        Source-first (L-205 / B5): a non-discovery TargetActor is a PASSIVE
        SUBSCRIBER and has NO work here — the legacy target-owned pull path
        (handler.pull → ingestion pipeline → ``write_target_signal``) was
        retired; SourceActor owns acquisition and signals are target-agnostic.
        A DISCOVERY descriptor still drives the materialiser cycle below.
        """
        payload = payload or {}

        actor_id = self.id.id
        deps_bundle = await _resolve_target_deps(actor_id)
        if deps_bundle is None:
            return {
                "outcome": ActorRunOutcome.HARD_FAIL.value,
                "error": "no deps registered for actor_id",
            }
        rec = await self._get_record()
        if rec is None:
            return {"outcome": ActorRunOutcome.NOOP.value, "reason": "no_state"}
        if rec["lifecycle"] != ACTIVE:
            return {"outcome": ActorRunOutcome.NOOP.value, "reason": f"lifecycle={rec['lifecycle']}"}

        # Wave D: discovery descriptors invoke the materialiser loop
        # instead of the normal ingest path. The actor host registers
        # both kinds against TargetActor; the descriptor.discovery field
        # is the discriminator. The materialiser owns its own cycle
        # cadence (relabel → merge → upsert → diff/retire) and writes
        # to ``target_descriptors``, not ``signals``.
        if deps_bundle.descriptor.discovery is not None:
            return await self._run_discovery_cycle(deps_bundle, rec)

        # Source-first (L-205): a TargetActor is a PASSIVE SUBSCRIBER. Acquisition
        # is owned by SourceActor + the fan-out / subscription engine; the target
        # does NOT pull. The legacy E2 target-owned pull path (handler.pull →
        # ingestion pipeline → write_target_signal) was retired here. A
        # non-discovery target therefore has no work in run().
        rec["last_run_at"] = _utcnow().isoformat()
        rec["last_outcome"] = ActorRunOutcome.NOOP.value
        rec["last_error"] = None
        await self._set_record(rec)
        await self._state_manager.save_state()
        return {"outcome": ActorRunOutcome.NOOP.value, "reason": "passive_subscriber"}

    # ------------------------------------------------------------------ Discovery branch (Wave D)

    async def _run_discovery_cycle(
        self,
        deps_bundle: Any,
        rec: dict[str, Any],
    ) -> dict[str, Any]:
        """Wave D: invoke the discovery materialiser loop in place of
        the normal source ingest path.

        Per L-180 / L-200 + the registry-side materialiser at
        :func:`legba.data.registry.discovered_materializer.reconcile_discovered_targets`:
        the descriptor's ``discovery`` block carries the kind name, the
        relabel chain, and the resync policy. The actor resolves the
        discovery handler via the discovery-kind registry, drains its
        ``discover(ctx)`` stream into a candidate list, and hands the
        list to ``reconcile_discovered_targets``.

        The handler resolution is a soft import — if the discovery-kind
        package isn't reachable from this process (test isolation, etc.)
        the actor returns NOOP rather than crashing the Dapr runtime.
        """
        actor_id = self.id.id
        descriptor = deps_bundle.descriptor

        # Soft imports — keep the actor module loadable in environments
        # that don't ship the discovery kinds.
        try:
            from ..data.discovery import (
                CandidateTarget,
                DiscoveryContext,
                InMemoryStateStore,
                discover_discovery_kinds,
            )
            from ..data.registry.discovered_materializer import (
                reconcile_discovered_targets,
            )
        except ImportError as exc:                            # pragma: no cover
            logger.warning(
                "dapr_actors.target.discovery.import_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            return {
                "outcome": ActorRunOutcome.NOOP.value,
                "reason": "discovery_imports_unavailable",
            }

        kind_name = descriptor.discovery.kind
        kinds = discover_discovery_kinds()
        bundle = kinds.get(kind_name)
        if bundle is None:
            logger.warning(
                "dapr_actors.target.discovery.unknown_kind actor_id=%s kind=%s",
                actor_id, kind_name,
            )
            return {
                "outcome": ActorRunOutcome.HARD_FAIL.value,
                "error": f"unknown_discovery_kind:{kind_name}",
            }

        # Build the handler config from the descriptor's discovery block.
        # The discovery block surfaces list_source + per-kind config; the
        # CONFIG_SCHEMA on the bundle validates the composite.
        cfg_dict: dict[str, Any] = {}
        if descriptor.discovery.list_source:
            cfg_dict["list_source"] = descriptor.discovery.list_source
        cfg_dict.update(descriptor.discovery.config or {})
        try:
            cfg = bundle.config_schema.model_validate(cfg_dict)
        except Exception as exc:
            logger.exception(
                "dapr_actors.target.discovery.cfg_invalid actor_id=%s err=%s",
                actor_id, exc,
            )
            return {
                "outcome": ActorRunOutcome.HARD_FAIL.value,
                "error": f"discovery_config_invalid:{exc}",
            }

        # Walker bundles expose `.discover` — either a module-level
        # async iterator factory or an unbound method on the handler
        # class. Both signatures are `(ctx) -> AsyncIterator[Candidate]`,
        # so we can call uniformly. Production wiring instantiates the
        # handler class once at activation; the convenience module-level
        # delegate is what we hit here.
        ctx = DiscoveryContext(
            discovery_id=descriptor.identity.id,
            discovery_version=descriptor.identity.version,
            config=cfg,
            state_store=InMemoryStateStore(),
            logger=logger.getChild("discovery"),
        )

        # Drain the async iterator.
        candidates: list[CandidateTarget] = []
        try:
            async for c in bundle.discover(ctx):
                candidates.append(c)
        except Exception as exc:
            logger.exception(
                "dapr_actors.target.discovery.discover_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            rec["last_error"] = str(exc)
            rec["error_count"] = int(rec.get("error_count", 0)) + 1
            rec["last_outcome"] = ActorRunOutcome.HARD_FAIL.value
            rec["last_run_at"] = _utcnow().isoformat()
            await self._set_record(rec)
            await self._state_manager.save_state()
            return {
                "outcome": ActorRunOutcome.HARD_FAIL.value,
                "error": str(exc),
            }

        # Invoke the materialiser loop in a single transaction.
        try:
            async with deps_bundle.deps.pg_pool.acquire() as conn:
                result = await reconcile_discovered_targets(
                    conn,
                    descriptor,
                    candidates,
                    dlq=getattr(deps_bundle.deps, "descriptor_dlq", None),
                    nats_publish=deps_bundle.deps.nats_publish,
                )
        except Exception as exc:
            logger.exception(
                "dapr_actors.target.discovery.reconcile_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            rec["last_error"] = str(exc)
            rec["error_count"] = int(rec.get("error_count", 0)) + 1
            rec["last_outcome"] = ActorRunOutcome.HARD_FAIL.value
            rec["last_run_at"] = _utcnow().isoformat()
            await self._set_record(rec)
            await self._state_manager.save_state()
            return {
                "outcome": ActorRunOutcome.HARD_FAIL.value,
                "error": str(exc),
            }

        rec["last_run_at"] = _utcnow().isoformat()
        rec["last_outcome"] = (
            ActorRunOutcome.SUCCESS.value
            if result.inserted_count
            else ActorRunOutcome.NOOP.value
        )
        rec["last_error"] = None
        await self._set_record(rec)
        await self._state_manager.save_state()

        return {
            "outcome": rec["last_outcome"],
            "discovery_cycle": True,
            "candidates_in": result.candidates_in,
            "inserted": result.inserted_count,
            "dropped": result.dropped_count,
            "dlq": result.dlq_count,
            "retired": len(result.retired),
            "paused": result.paused,
        }

    # ------------------------------------------------------------------ Dapr state helpers

    async def _get_record(self) -> dict[str, Any] | None:
        ok, val = await self._state_manager.try_get_state("record")
        return val if ok else None

    async def _set_record(self, rec: dict[str, Any]) -> None:
        await self._state_manager.set_state("record", rec)


# ---------------------------------------------------------------------------
# Typed retry policies (Phase 5 hardening item 5)
# ---------------------------------------------------------------------------


from .actor_retry import (  # noqa: F401  -- re-export: public API stability (#93)
    _classify_exception,
    _retry_delay_seconds,
)


# ---------------------------------------------------------------------------
# AnalystActor (Dapr-native)
# ---------------------------------------------------------------------------


class AnalystActor(Actor, AnalystActorInterface, Remindable):
    """Dapr-native AnalystActor.

    Mirrors :class:`TargetActor` shape. The run path reads substrate,
    invokes the LLM via the registered ``run_method``, records budget
    + writes a finding.
    """

    def _is_worker(self, deps: "_AnalystDeps") -> bool:
        """True iff THIS actor is a per-(analyst, target) WORKER, not the primary.

        A2 concurrency: both share the ``analyst::<descriptor_id>::<tail>``
        grammar. The PRIMARY's tail is the descriptor's content-hash
        (``identity.version[:16]`` per ``_default_actor_id``, or the all-zeros
        fallback when the version is empty); a WORKER's tail is a target_id.
        Comparing the tail against the resolved descriptor's version prefix is
        deterministic — no format-sniffing — and lets ``_on_activate`` refuse
        to register a cadence reminder on a worker (the reminder lives ONLY on
        the primary, which fans out to its workers).
        """
        _kind, _descriptor_id, tail = _split_actor_id(self.id.id)
        if tail is None:
            return False  # malformed / two-segment id — treat as primary.
        version = deps.descriptor.identity.version or ""
        primary_tail = version[:16] or "0" * 16
        return tail != primary_tail

    async def _on_activate(self) -> None:
        actor_id = self.id.id
        logger.info("dapr_actors.analyst.activate actor_id=%s", actor_id)
        deps = await _resolve_analyst_deps(actor_id)
        if deps is None:
            logger.warning(
                "dapr_actors.analyst.activate.no_deps actor_id=%s "
                "(host did not call register_analyst_deps and registry "
                "fallback did not resolve)",
                actor_id,
            )
            return

        # A2 concurrency: a per-target WORKER actor must NOT create a record
        # or register a reminder on cold activation — it lazy-activates inside
        # ``run`` (target_filter in hand) and never carries the cadence
        # heartbeat. Only the PRIMARY actor (tail == descriptor content-hash)
        # creates state + registers the reminder here. Skipping the body keeps
        # the worker a pure on-demand executor reachable solely via the
        # primary's fan-out / a coalesced fire.
        if self._is_worker(deps):
            logger.info(
                "dapr_actors.analyst.activate.worker_noop actor_id=%s "
                "(per-target worker — no record/reminder on activate)",
                actor_id,
            )
            return

        existing = await self._get_record()
        if existing is None:
            rec = {
                "actor_id": actor_id,
                "actor_kind": "analyst",
                "descriptor_id": deps.descriptor.identity.id,
                "descriptor_version": deps.descriptor.identity.version,
                "lifecycle": DRAFT,
                "last_run_at": None,
                "last_outcome": None,
                "cooldown_until": None,
                "error_count": 0,
                "last_error": None,
            }
            fsm = LifecycleFSM(state=DRAFT)
            fsm.transition(LifecycleEvent.CONFIGURE, initiated_by="dapr_on_activate")
            fsm.transition(LifecycleEvent.ACTIVATE, initiated_by="dapr_on_activate")
            rec["lifecycle"] = fsm.state
            await self._set_record(rec)
            await self._state_manager.save_state()

        # Register a single reminder driven by cadence.fallback_schedule.
        schedule = deps.descriptor.cadence.fallback_schedule
        if schedule:
            try:
                due, period = cron_to_reminder_timing(schedule)
                await self.register_reminder(
                    name="run_cadence",
                    state=b"{}",
                    due_time=due,
                    period=period,
                )
                logger.info(
                    "dapr_actors.analyst.reminder.registered "
                    "actor_id=%s due=%s period=%s",
                    actor_id, due, period,
                )
            except Exception as exc:
                logger.warning(
                    "dapr_actors.analyst.reminder.invalid actor_id=%s expr=%r err=%s",
                    actor_id, schedule, exc,
                )

    async def _on_deactivate(self) -> None:
        logger.info("dapr_actors.analyst.deactivate actor_id=%s", self.id.id)
        await self._state_manager.save_state()

    async def _reminder_guard(self, name: str) -> str:
        """A-1 belt-and-braces: refuse stale reminder fires (see
        :func:`reminder_guard_decision`). Unregisters our own reminder when
        this actor's version is no longer the descriptor head or the actor
        is retired — the self-disarm that clears reminder pollution even
        when a retire/version-bump propagation was missed."""
        _kind, _descriptor_id, tail = _split_actor_id(self.id.id)
        rec = await self._get_record()
        deps = await _resolve_analyst_deps(self.id.id)
        head = deps.descriptor.identity.version if deps is not None else None
        decision = reminder_guard_decision(
            record_lifecycle=(rec or {}).get("lifecycle"),
            own_tail=tail,
            head_version=head,
        )
        if decision == "unregister":
            logger.info(
                "dapr_actors.analyst.reminder.stale actor_id=%s reminder=%s "
                "head=%s — unregistering",
                self.id.id, name, (head or "")[:16],
            )
            try:
                await self.unregister_reminder(name)
            except Exception as exc:
                logger.warning(
                    "dapr_actors.analyst.reminder.unregister_failed "
                    "actor_id=%s err=%s", self.id.id, exc,
                )
        elif decision == "skip":
            logger.info(
                "dapr_actors.analyst.reminder.skip actor_id=%s lifecycle=%s",
                self.id.id, (rec or {}).get("lifecycle"),
            )
        return decision

    async def receive_reminder(
        self,
        name: str,
        state: bytes,
        due_time: timedelta,
        period: timedelta,
        ttl: timedelta | None = None,
    ) -> None:
        logger.info(
            "dapr_actors.analyst.reminder.fired actor_id=%s reminder=%s",
            self.id.id, name,
        )
        if await self._reminder_guard(name) != "run":
            return
        # Cadence is the heartbeat that guarantees coverage even when the
        # event-driven coalescing trigger never trips (too few signals).
        #
        #  * TARGET-BOUND analyst (subscription.targets selector present) →
        #    fire ONE run per matched target (target_filter set). The
        #    per-(analyst, target) cooldown keeps each target independent, so
        #    a quiet country doesn't starve a busy one. This is what makes
        #    cadence produce per-country findings instead of one global blob.
        #  * META analyst (no target binding — discovery, cross-target meta,
        #    optimizer) → a single global run (target_filter=None) over the
        #    whole substrate, as before.
        targets = await self._cadence_targets()
        if targets is None:
            # A CRITIC-kind meta analyst must grade analyzed-output ROWS, not
            # run a global substrate slice — its READ_SLICE returns [] without a
            # row id, so a plain global run NOOPs as 'no_inputs' forever (the
            # bug that left the whole critic→optimizer eval loop inert). Resolve
            # its newest-N ungraded findings and fan ONE bounded worker grade per
            # row (target_filter=<finding_id>; critic.READ_SLICE parses it as the
            # analyzed_output_id). Reuses the cadence cooldown + per-analyst
            # budget cap. Returns None for non-critic meta analysts → global run.
            critic_targets = await self._critic_ungraded_targets()
            if critic_targets is not None:
                if critic_targets:
                    logger.info(
                        "dapr_actors.analyst.critic.fanout actor_id=%s ungraded=%d",
                        self.id.id, len(critic_targets),
                    )
                    await self._fanout_to_workers(critic_targets)
                return
            # Meta analyst (no target binding) — single global run.
            await self.run({"trigger_kind": "cadence"})
            return
        # Target-bound analyst — fan out per matched target. An empty match
        # set means nothing to do this tick (NOT a global run).
        if targets:
            logger.info(
                "dapr_actors.analyst.cadence.fanout actor_id=%s targets=%d",
                self.id.id, len(targets),
            )
            await self._fanout_to_workers(targets)

    async def _fanout_to_workers(self, targets: list[str]) -> None:
        """A2 concurrency: dispatch the per-target cadence runs to PER-WORKER
        actors instead of serializing them through THIS (primary) actor's
        turn-queue.

        Each target gets a distinct worker actor id
        (``analyst::<descriptor_id>::<target_id>``) ⇒ a distinct Dapr virtual
        actor ⇒ its own turn-queue ⇒ concurrent execution. The primary
        (this actor — id ``analyst::<id>::<ver16>``) keeps the cadence
        reminder and only orchestrates the fan-out; it does NOT run the
        per-target work itself.

        Dispatch is BOUNDED-CONCURRENT — chunked at :data:`_FANOUT_CHUNK`
        (~5) so a 19-country fan-out doesn't open 19 simultaneous LLM calls
        (a thundering herd that would blow the budget envelope + rate
        limits). Within a chunk the worker ``run`` calls run concurrently;
        chunks are awaited in series.

        The worker reconstructs the analyst's deps via the segment-1 fallback
        resolver (no new registration) and LAZY-ACTIVATES on first ``run``.
        """
        descriptor_id = self._descriptor_id_from_actor_id()
        proxy_factory = _worker_proxy_factory()
        sem = asyncio.Semaphore(_FANOUT_CHUNK)

        async def _dispatch_one(tid: str) -> None:
            async with sem:
                worker_id = _worker_actor_id(descriptor_id, tid)
                try:
                    proxy = proxy_factory(worker_id)
                    await proxy.run({
                        "trigger_kind": "cadence",
                        "target_filter": tid,
                    })
                except Exception as exc:  # pragma: no cover — best-effort fan-out
                    logger.warning(
                        "dapr_actors.analyst.cadence.fanout.worker_failed "
                        "primary=%s worker=%s target=%s err=%s",
                        self.id.id, worker_id, tid, exc,
                    )

        await asyncio.gather(*(_dispatch_one(tid) for tid in targets))

    async def _critic_ungraded_targets(self) -> list[str] | None:
        """Newest-N analyzed-analyst findings this critic has NOT yet graded.

        For a ``critic``-kind analyst, the cadence tick must dispatch one
        graded run per ANALYZED-OUTPUT ROW (the critic grades a single
        ``analyst_outputs`` row, keyed via ``target_filter`` → its
        ``READ_SLICE`` parses the UUID). The analyzed analyst is the
        descriptor's ``eval.optimizer.analyzed_analyst_id`` pin
        (e.g. ``country_assessor``). "Ungraded" = no existing ``critique``
        output carries the finding id in its ``derived_from`` (the critic
        kind stamps ``derived_from=[analyzed_output_id]``), so re-running is
        idempotent — a finding is graded once.

        Returns:
          * ``None``  — this analyst is NOT a critic (caller does the global
            meta run instead).
          * ``[]``    — a critic, but nothing ungraded this tick (no-op).
          * ``[ids]`` — finding ids (strings) to fan out as ``target_filter``.
        """
        deps_bundle = await _resolve_analyst_deps(self.id.id)
        if deps_bundle is None:
            return None
        descriptor = deps_bundle.descriptor
        if getattr(descriptor.identity, "kind", None) != "critic":
            return None
        analyzed_id = _critic_descriptor_pinned_analyst_id(descriptor)
        if not analyzed_id:
            logger.warning(
                "dapr_actors.analyst.critic.no_pin actor_id=%s — critic has no "
                "eval.optimizer.analyzed_analyst_id; nothing to grade",
                self.id.id,
            )
            return []
        critic_id = descriptor.identity.id
        try:
            async with deps_bundle.deps.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT ao.id::text AS id
                    FROM analyst_outputs ao
                    WHERE ao.analyst_id = $1 AND ao.kind = 'finding'
                      AND NOT EXISTS (
                          SELECT 1 FROM analyst_outputs c
                          WHERE c.analyst_id = $2 AND c.kind = 'critique'
                            AND ao.id = ANY(c.derived_from)
                      )
                    ORDER BY ao.produced_at DESC
                    LIMIT $3
                    """,
                    analyzed_id, critic_id, _critic_fanout_max(),
                )
        except Exception as exc:                                # pragma: no cover
            logger.warning(
                "dapr_actors.analyst.critic.ungraded_query_failed actor_id=%s err=%s",
                self.id.id, exc,
            )
            return []
        return [r["id"] for r in rows]

    def _descriptor_id_from_actor_id(self) -> str:
        """Segment-1 of this actor's id — the analyst descriptor_id.

        The primary actor's id is ``analyst::<descriptor_id>::<ver16>``;
        ``split("::", 2)[1]`` recovers the descriptor_id. The worker ids the
        primary fans out to share segment-1, so the deps fallback resolver
        reconstructs the same analyst's deps with no new registration.
        """
        _kind, descriptor_id, _tail = _split_actor_id(self.id.id)
        return descriptor_id

    async def _cadence_targets(self) -> list[str] | None:
        """Active targets this analyst's ``subscription.targets`` selector
        matches — the per-target cadence heartbeat set. ``None`` (or empty)
        means this is a META analyst with no target binding, so the caller
        does a single global run instead.

        Evaluates the optional Starlark predicate (ANALYST_SUBSCRIPTION
        surface, e.g. ``has_tag("g20")``) against each active target's scope,
        mirroring ``source_first_runtime._analyst_ids_for_target``.
        """
        deps_bundle = await _resolve_analyst_deps(self.id.id)
        if deps_bundle is None:
            return None
        sub = getattr(deps_bundle.descriptor, "subscription", None)
        sub_targets = getattr(sub, "targets", None) if sub is not None else None
        if sub_targets is None:
            return None  # meta analyst — global cadence
        pred = getattr(sub_targets, "predicate", None)
        try:
            async with deps_bundle.deps.pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT descriptor_id, body FROM target_descriptors "
                    "WHERE is_head = TRUE AND state = 'active'"
                )
        except Exception as exc:                                # pragma: no cover
            logger.warning(
                "dapr_actors.analyst.cadence_targets.query_failed actor_id=%s err=%s",
                self.id.id, exc,
            )
            return None
        if not pred:
            return [r["descriptor_id"] for r in rows]  # selector, no predicate → all
        from ..data.predicates import PredicateSurface, compile_predicate
        try:
            compiled = compile_predicate(pred, PredicateSurface.ANALYST_SUBSCRIPTION)
        except Exception as exc:                                # pragma: no cover
            logger.warning(
                "dapr_actors.analyst.cadence_targets.compile_failed actor_id=%s err=%s",
                self.id.id, exc,
            )
            return None
        matched: list[str] = []
        for r in rows:
            body = r["body"]
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception as exc:
                    # #95 silent-drop-on-critical-path: an unparseable descriptor
                    # body would otherwise exclude this target from analyst
                    # matching with NO trace (a misconfigured target silently
                    # never analysed). Flow unchanged (still skip) — made visible.
                    logger.warning(
                        "dapr_actors.match.skip_unparsable_body descriptor_id=%s err=%s",
                        r["descriptor_id"], exc,
                    )
                    continue
            scope = (body or {}).get("scope") or {}
            ident = (body or {}).get("identity") or {}
            ctx = {
                "target": {
                    "id": r["descriptor_id"],
                    "kind": str(ident.get("kind") or ""),
                    "scope_geo": list(scope.get("geo") or []),
                    "scope_entity_classes": list(scope.get("entity_classes") or []),
                    "tags": list(scope.get("tags") or []),
                    "abstraction_level": str(ident.get("abstraction_level") or ""),
                }
            }
            try:
                if compiled.evaluate(ctx):
                    matched.append(r["descriptor_id"])
            except Exception as exc:                            # pragma: no cover
                # #95 silent-drop-on-critical-path: a throwing scope predicate
                # would silently exclude this target from matching. Flow
                # unchanged (still skip) — made observable so a broken predicate
                # is diagnosable instead of a target just never being analysed.
                logger.warning(
                    "dapr_actors.match.predicate_eval_failed descriptor_id=%s err=%s",
                    r["descriptor_id"], exc,
                )
                continue
        return matched

    async def activate(self) -> dict[str, Any]:
        # Run the activation path UNCONDITIONALLY (not just on first create). It
        # is idempotent — record creation is guarded by `existing is None`, deps
        # are cached, and the cadence reminder re-registers over its own
        # `run_cadence` name (re-anchored to the next cron boundary). This is
        # what makes the durability heal work: reconcile ENSURE_ACTIVE →
        # proxy.activate() on each resync re-asserts a silently-dropped reminder
        # on a STILL-WARM actor, not only on cold Dapr reactivation. Without it,
        # activate() on a warm actor would just reset the idle timer — keeping a
        # reminder-less actor alive forever without ever re-registering.
        await self._on_activate()
        # RESURRECT-ON-RESTORE: activate() is only ever driven (reconcile
        # ENSURE_ACTIVE / TRANSITION_LIFECYCLE→active / CREATE→active) when the
        # descriptor head DECLARES this version active. If the actor's own record
        # is parked at PAUSED (operator-paused, then descriptor flipped back) or
        # RETIRED (a superseded version restored as head via rollback), the
        # declared-active head is authoritative — bring the record back to ACTIVE
        # so the run path stops NOOP'ing `lifecycle=paused/retired`. _on_activate
        # already (re-)registered the reminder above. This is scoped to the
        # head-is-active reconcile path only; the NORMAL supersede still drives
        # RETIRE_ACTOR (retire()), which stays terminal for that record.
        rec = await self._get_record()
        if rec is not None and rec.get("lifecycle") in (PAUSED, RETIRED):
            prior = rec["lifecycle"]
            rec["lifecycle"] = ACTIVE
            await self._set_record(rec)
            await self._state_manager.save_state()
            logger.info(
                "dapr_actors.analyst.resurrect actor_id=%s from=%s -> active "
                "(descriptor head declares active)",
                self.id.id, prior,
            )
        return rec or {}

    async def _unregister_cadence_reminder(self, *, reason: str) -> None:
        """Drop the ``run_cadence`` reminder. A paused/retired analyst whose
        reminder keeps firing was the G1 pollution: the run path NOOPs but
        the scheduler churns forever (and a retired actor is re-warmed every
        period). Best-effort — no reminder registered is not an error."""
        try:
            await self.unregister_reminder("run_cadence")
            logger.info(
                "dapr_actors.analyst.reminder.unregistered actor_id=%s reason=%s",
                self.id.id, reason,
            )
        except Exception as exc:
            logger.debug(
                "dapr_actors.analyst.reminder.unregister_noop actor_id=%s "
                "reason=%s err=%s", self.id.id, reason, exc,
            )

    async def pause(self) -> dict[str, Any]:
        rec = await self._get_record()
        if rec is None:
            # No record = the actor was never created (a descriptor transitioned
            # straight to paused — that version never had an active actor).
            # Nothing to park; no-op rather than 500 (mirrors retire()'s no-record
            # path). Fixes the reconcile.failed-on-paused symptom where the
            # reconciler pauses a never-activated paused-head version.
            await self._unregister_cadence_reminder(reason="pause_no_record")
            return {"actor_id": self.id.id, "lifecycle": PAUSED}
        # Idempotent: pausing an already-paused actor is a no-op (the reconcile
        # / operator path can re-issue pause without a 500). transition_idempotent
        # returns None when already PAUSED — we still re-assert the reminder
        # unregister so a redundant pause heals a stray reminder.
        fsm = LifecycleFSM(state=rec["lifecycle"])
        fsm.transition_idempotent(LifecycleEvent.PAUSE, initiated_by="actor_pause")
        rec["lifecycle"] = fsm.state
        await self._set_record(rec)
        await self._state_manager.save_state()
        await self._unregister_cadence_reminder(reason="pause")
        return rec

    async def resume(self) -> dict[str, Any]:
        """PAUSED → ACTIVE (RESUME). Symmetric with :meth:`pause`: re-registers
        the cadence reminder that pause unregistered. Idempotent on an already
        ACTIVE actor (no-op transition); raises only on a genuinely-illegal
        source state (e.g. draft)."""
        rec = await self._get_record()
        if rec is None:
            # No record = the actor was never created. The descriptor head wants
            # it ACTIVE (that's why resume was routed here) — create + activate
            # fresh rather than 500 (activate() runs _on_activate, which creates
            # the record + re-registers the cadence reminder).
            return await self.activate()
        fsm = LifecycleFSM(state=rec["lifecycle"])
        fsm.transition_idempotent(LifecycleEvent.RESUME, initiated_by="actor_resume")
        rec["lifecycle"] = fsm.state
        await self._set_record(rec)
        await self._state_manager.save_state()
        # Re-arm the cadence heartbeat exactly as _on_activate does (and as
        # pause tore down). _on_activate is idempotent on an existing record —
        # it skips record creation and only (re-)registers the reminder.
        await self._on_activate()
        return rec

    async def retire(self) -> dict[str, Any]:
        rec = await self._get_record()
        if rec is None:
            await self._unregister_cadence_reminder(reason="retire_no_record")
            return {"actor_id": self.id.id, "lifecycle": RETIRED}
        # Idempotent: retiring an already-retired actor is a no-op — the live
        # version-drift sweep can re-issue RETIRE_ACTOR on a sibling that was
        # already retired without tripping a 500 (the reconcile.version_drift
        # .retire_failed symptom). transition_idempotent returns None at RETIRED.
        fsm = LifecycleFSM(state=rec["lifecycle"])
        fsm.transition_idempotent(LifecycleEvent.RETIRE, initiated_by="actor_retire")
        rec["lifecycle"] = fsm.state
        await self._set_record(rec)
        await self._state_manager.save_state()
        await self._unregister_cadence_reminder(reason="retire")
        return rec

    async def get_state(self) -> dict[str, Any]:
        return await self._get_record() or {}

    async def run(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        target_filter = payload.get("target_filter")
        actor_id = self.id.id
        deps_bundle = await _resolve_analyst_deps(actor_id)
        if deps_bundle is None:
            return {
                "outcome": ActorRunOutcome.HARD_FAIL.value,
                "error": "no deps registered for actor_id",
            }
        rec = await self._get_record()
        if rec is None:
            # A2 LAZY-ACTIVATE: a per-(analyst, target) WORKER actor
            # (id ``analyst::<descriptor_id>::<target_id>``) is only ever
            # reached via the PRIMARY's cadence fan-out or a coalesced fire —
            # both for an already-wired analyst — and _on_activate never ran a
            # CONFIGURE/ACTIVATE for it (workers carry no descriptor of their
            # own; their deps resolve via the segment-1 fallback). When a
            # target_filter is set AND deps resolved, create a MINIMAL ACTIVE
            # record inline so the run proceeds. We register NO reminder here:
            # the cadence heartbeat lives ONLY on the primary actor, which
            # fans out to us. Workers thus have no independent schedule and
            # cannot self-fire.
            #
            # A no-target run with no record stays NOOP/no_state (the meta /
            # global path always has a record from _on_activate, so this only
            # guards a defensive edge).
            if target_filter:
                rec = self._minimal_worker_record(actor_id, deps_bundle, target_filter)
                await self._set_record(rec)
                await self._state_manager.save_state()
                logger.info(
                    "dapr_actors.analyst.worker.lazy_activate actor_id=%s target=%s",
                    actor_id, target_filter,
                )
            else:
                return {"outcome": ActorRunOutcome.NOOP.value, "reason": "no_state"}
        if rec["lifecycle"] != ACTIVE:
            return {"outcome": ActorRunOutcome.NOOP.value, "reason": f"lifecycle={rec['lifecycle']}"}

        now = _utcnow()

        def _as_dt(v: Any) -> datetime:
            # Dapr's DefaultJSONSerializer auto-parses ISO date-strings into
            # datetime objects on deserialize (see DaprJSONDecoder's
            # datetime_regex), so a round-tripped value may already be a
            # datetime rather than the str we wrote. Accept both.
            return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))

        # Global throttle (budget exhaustion → pause the whole analyst) blocks
        # every target.
        cooldown_raw = rec.get("cooldown_until")
        if cooldown_raw and _as_dt(cooldown_raw) > now:
            return {"outcome": ActorRunOutcome.NOOP.value, "reason": "cooldown"}

        # Per-(analyst, target) cadence cooldown — keyed by target_filter so a
        # heartbeat over one country doesn't cool down all the others. The
        # global run (meta analyst / no filter) uses the "_global" key.
        cooldown_key = str(target_filter) if target_filter else "_global"
        cooldowns_by_target = rec.get("cooldown_by_target") or {}
        cd_raw = cooldowns_by_target.get(cooldown_key)
        # The cadence cooldown is stamped at run COMPLETION (now + cooldown_seconds
        # at the set site below). When a descriptor sets cooldown_seconds ≈ its
        # cadence interval, the completion drift (run duration + reminder jitter)
        # pushes cooldown_until a little PAST the next scheduled fire, so that fire
        # is wrongly suppressed and the cadence silently halves (a 6h cron → 12h —
        # the world_assessor / country_assessor symptom). Absorb that drift with a
        # small slack derived from the cooldown itself: negligible for short
        # cadences (e.g. ~45s for a 15-min cooldown), a few minutes for long ones
        # (capped at 10 min), which never meaningfully weakens burst suppression.
        # Applies ONLY to the per-target cadence cooldown — the global budget
        # cooldown (cooldown_until, above) stays strict.
        _cd_slack = timedelta(
            seconds=min(deps_bundle.descriptor.cadence.cooldown_seconds * 0.05, 600.0)
        )
        if cd_raw and _as_dt(cd_raw) > now + _cd_slack:
            return {
                "outcome": ActorRunOutcome.NOOP.value,
                "reason": "cooldown",
                "target": cooldown_key,
            }

        run_id = uuid4()
        # Per-run input overrides — consult_on_demand carries its question
        # in the payload; the runtime supplies it as ``inputs_override`` so
        # we bypass the substrate-slice read entirely.
        inputs_override = payload.get("inputs")
        # Consult chat (Piece 1): the registry mints a request-scoped id and
        # tags the invoke with a mode {chat,deep}. Lift them here so the run can
        # wire a per-run step publisher (4b) and branch the write (TASK 5).
        consult_request_id = (
            inputs_override[0].get("request_id")
            if inputs_override else None
        )
        consult_mode = (
            inputs_override[0].get("mode")
            if inputs_override else None
        )
        # Bind the run/correlation context for structured logging (W-1b §3):
        # every log line emitted underneath this run — across awaits, in any
        # module — carries run_id + analyst_id, and the consult request_id as
        # the correlation_id when present. Reset in the finally below.
        _log_ctx_token = _bind_run_log_context(
            run_id=run_id,
            analyst_id=actor_id,
            correlation_id=consult_request_id,
        )
        try:
            async with deps_bundle.deps.pg_pool.acquire() as conn:
                if inputs_override is not None:
                    # Caller passed the inputs directly (on-demand kinds).
                    inputs = list(inputs_override)
                elif deps_bundle.read_slice is not None:
                    # Per-kind reader — broader-slice / other-analyst reads.
                    inputs = await deps_bundle.read_slice(
                        conn,
                        descriptor=deps_bundle.descriptor,
                        target_filter=target_filter,
                    )
                else:
                    inputs = await _read_substrate_slice(
                        conn,
                        descriptor=deps_bundle.descriptor,
                        target_filter=target_filter,
                    )
                if not inputs:
                    rec["last_run_at"] = now.isoformat()
                    rec["last_outcome"] = ActorRunOutcome.NOOP.value
                    await self._set_record(rec)
                    await self._state_manager.save_state()
                    return {"outcome": ActorRunOutcome.NOOP.value, "reason": "no_inputs"}

                # Typed retry policy block (defaults if absent).
                retry_block = deps_bundle.descriptor.method.retry
                budget_policy = retry_block.budget
                transient_policy = retry_block.transient
                hard_policy = retry_block.hard

                # Pre-call budget check (A-5/G5: with a REAL forward-looking
                # estimate, so the throttle outcome is reachable).
                # Resolution:
                #   * exhausted / global_exhausted → audit row, then strategy:
                #       - pause_until_next_window → cooldown + BUDGET_THROTTLED.
                #       - demote_and_continue (F-2) → EXPLICIT audited pause
                #         until the bucket window resets. Real cheap-model
                #         fallback demotion is a declared seam (docs/SEAMS.md
                #         → DIRECTION.md) — never a silent degrade.
                #       - dlq → BUDGET_THROTTLED + DLQ tag.
                #   * throttle (projected overrun) → cooldown + BUDGET_THROTTLED
                #         without an exhaustion audit row (nothing exhausted yet).
                if deps_bundle.budget is not None:
                    decision = await deps_bundle.budget.precall_check(
                        conn,
                        estimated_tokens=getattr(
                            deps_bundle.budget, "estimated_tokens_per_run", 0,
                        ),
                    )
                    if decision.outcome in ("exhausted", "global_exhausted"):
                        bucket_end = _bucket_end_iso(now)
                        strategy = budget_policy.strategy
                        is_global = decision.outcome == "global_exhausted"
                        # Audit row first — independent of strategy.
                        try:
                            await deps_bundle.budget.record_demotion(
                                conn,
                                cause=decision.cause or ("global" if is_global else "per_analyst"),
                                primary_llm=deps_bundle.primary_llm_ref,
                                fallback_llm=deps_bundle.fallback_llm_ref,
                                tokens_used_at_demote=(
                                    decision.global_tokens_used
                                    if is_global
                                    else decision.tokens_used_today
                                ),
                                tokens_cap_at_demote=(
                                    decision.global_tokens_cap
                                    if is_global
                                    else decision.tokens_budget_per_day
                                ),
                            )
                        except Exception as exc:                # pragma: no cover
                            logger.warning(
                                "dapr_actors.analyst.demotion.audit_failed "
                                "actor_id=%s err=%s",
                                actor_id, exc,
                            )

                        # Strategy dispatch.
                        if strategy == "dlq":
                            rec["last_run_at"] = now.isoformat()
                            rec["last_outcome"] = ActorRunOutcome.BUDGET_THROTTLED.value
                            rec["last_error"] = (
                                f"budget_exhausted_dlq: {decision.detail}"
                            )
                            await self._set_record(rec)
                            await self._state_manager.save_state()
                            return {
                                "outcome": ActorRunOutcome.BUDGET_THROTTLED.value,
                                "detail": decision.detail,
                                "dlq": True,
                            }
                        if strategy == "demote_and_continue":
                            # F-2 (2026-06-09): the strategy stays in the
                            # schema, but no production resolver wires a
                            # fallback model — so its behavior is an EXPLICIT
                            # pause until the quota window resets, audited
                            # above + logged here. Silent fallthrough was
                            # exactly the G5 finding. Real fallback-model
                            # demotion: declared seam, see docs/SEAMS.md.
                            logger.warning(
                                "dapr_actors.analyst.demoted_paused actor_id=%s "
                                "cause=%s — demote_and_continue has no fallback "
                                "model wired; pausing until window reset (%s). "
                                "See docs/SEAMS.md (fallback-model demotion).",
                                actor_id, decision.cause, bucket_end,
                            )
                            rec["last_run_at"] = now.isoformat()
                            rec["last_outcome"] = ActorRunOutcome.BUDGET_THROTTLED.value
                            rec["cooldown_until"] = bucket_end
                            await self._set_record(rec)
                            await self._state_manager.save_state()
                            return {
                                "outcome": ActorRunOutcome.BUDGET_THROTTLED.value,
                                "detail": decision.detail,
                                "demoted_paused_until": bucket_end,
                            }
                        # pause_until_next_window (default).
                        cooldown_s = budget_policy.cooldown_seconds
                        rec["last_run_at"] = now.isoformat()
                        rec["last_outcome"] = ActorRunOutcome.BUDGET_THROTTLED.value
                        rec["cooldown_until"] = (
                            now + timedelta(seconds=cooldown_s)
                        ).isoformat()
                        await self._set_record(rec)
                        await self._state_manager.save_state()
                        return {
                            "outcome": ActorRunOutcome.BUDGET_THROTTLED.value,
                            "detail": decision.detail,
                        }
                    if decision.outcome == "throttle":
                        # Forward-looking: this run's estimate is projected
                        # to cross a cap. Pause for the budget cooldown
                        # instead of knowingly overshooting (the overshoot
                        # used to be the only behavior — estimate was 0).
                        cooldown_s = budget_policy.cooldown_seconds
                        logger.info(
                            "dapr_actors.analyst.budget_throttle actor_id=%s "
                            "cause=%s detail=%s cooldown_s=%d",
                            actor_id, decision.cause, decision.detail, cooldown_s,
                        )
                        rec["last_run_at"] = now.isoformat()
                        rec["last_outcome"] = ActorRunOutcome.BUDGET_THROTTLED.value
                        rec["cooldown_until"] = (
                            now + timedelta(seconds=cooldown_s)
                        ).isoformat()
                        await self._set_record(rec)
                        await self._state_manager.save_state()
                        return {
                            "outcome": ActorRunOutcome.BUDGET_THROTTLED.value,
                            "detail": decision.detail,
                            "throttle": True,
                        }

                derived_ids: list[UUID] = [
                    UUID(str(row["id"])) for row in inputs if row.get("id")
                ]
                options: dict[str, Any] = {
                    "analyst_id": deps_bundle.descriptor.identity.id,
                    "analyst_version": deps_bundle.descriptor.identity.version,
                    "run_id": run_id,
                }
                if target_filter:
                    options["target_id"] = target_filter
                elif (
                    deps_bundle.descriptor.identity.kind == "meta_findings_synthesizer"
                    and _descriptor_declares_verify(deps_bundle.descriptor)
                ):
                    # P3-T5 GLOBAL/world composition: no target_id, so the kind
                    # needs an explicit flag to turn on the WORLD composition
                    # prompt + the [[ref:uuid]] CITE block (else it falls back to
                    # the legacy un-cited _SYSTEM_PROMPT and can never cite →
                    # honest-empty forever). T4: also read the open contested
                    # groups and stamp them so the kind can append the CONTESTED
                    # FACTS block + resolve [[contested:<id>]] markers. The read
                    # is best-effort — an additive enrichment that must NEVER
                    # block the compose (a contention-table error just omits the
                    # block).
                    options["composition"] = True
                    from ..data.analysts.meta_findings_synthesizer import (
                        read_open_contention,
                        thematic_dimension as _thematic_dimension,
                    )

                    # S2-T4: a THEMATIC composition (escalation_composition) is ALSO
                    # target-less + verify-declaring, so it reaches this branch. Its
                    # ``subscription.substrate.thematic_dimension`` marker routes the
                    # kind to the thematic prompt/READ_SLICE (one head per desk of a
                    # UNIT dimension). Stamp it into options; the world-only CONTESTED
                    # FACTS read (a global fact-dispute enrichment) does NOT apply.
                    _theme = _thematic_dimension(deps_bundle.descriptor)
                    if _theme:
                        options["thematic_dimension"] = _theme
                    else:
                        try:
                            options["contention_groups"] = (
                                await read_open_contention(conn)
                            )
                        except Exception as exc:  # pragma: no cover — best-effort
                            logger.warning(
                                "dapr_actors.analyst.contention_read.failed "
                                "actor_id=%s run_id=%s err=%s",
                                actor_id, run_id, exc,
                            )
                # Per-run options pass-through — the consult_on_demand kind
                # reads ``options["sub_handler"]`` (deterministic) and other
                # caller-supplied parameters through this channel.
                for k, v in (payload.get("options") or {}).items():
                    options.setdefault(k, v)
                # Deterministic-kind sub-handler resolution. The deterministic
                # dispatcher routes by ``options["sub_handler"]``, but neither
                # the cadence reminder nor the coalesced-trigger fire supplies
                # one — so inject it from the descriptor (``method.sub_handler``)
                # with a fallback to ``identity.id`` (the convention where the
                # analyst id equals its sub-handler, e.g. cross_source_dedup).
                # Without this, every deterministic analyst would raise
                # DeterministicDispatchError on its first live fire.
                if (
                    deps_bundle.descriptor.identity.kind == "deterministic"
                    and "sub_handler" not in options
                ):
                    options["sub_handler"] = (
                        getattr(deps_bundle.descriptor.method, "sub_handler", None)
                        or deps_bundle.descriptor.identity.id
                    )
                # S5: re-point the agentic inline_target GATHER binding to the
                # RUNNING target's allow-list (the three-way gate's allow leg is
                # per-target). The host wired the base binding (assessor grant
                # leg) iff the assessor grants `substrate_read`; here we resolve
                # the target's `allowed_action_packs` + scope and inject a
                # per-run, target-scoped binding into options. GATHER engages in
                # the kind ONLY when this resolves a pack the target ALSO allows
                # (else `run_pack_tool` denies and folds the block back into the
                # loop — a good, loud no-op). Budget is implicitly gated: the
                # precall_check above already returned `ok` to reach here.
                # §4.9 — the GATHER binding re-point was hard-gated on
                # `== "inline_target"`. Generalize to the in-actor GATHER kind set
                # so journal_assessor's journal_read binding is also re-pointed
                # (its META target_filter=None path self-allows its pack — see
                # _gather_binding_for_target).
                if (
                    deps_bundle.gather_binding is not None
                    and deps_bundle.descriptor.identity.kind
                    in _GATHER_BINDING_KINDS
                ):
                    options["agency_binding"] = await _gather_binding_for_target(
                        conn,
                        base=deps_bundle.gather_binding,
                        target_id=target_filter,
                    )
                    # SEAM #22: re-point the write/web GATHER bindings to the
                    # running target AND inject the per-run WritebackContext
                    # (copy-on-write — never mutate the shared base). Built only
                    # when the assessor also grants web_access/propose_facts; the
                    # runner routes each web/write tool to its pack's binding and
                    # splices the packs' operator-authored guidance into the
                    # GATHER prompt.
                    if deps_bundle.gather_write_bindings is not None:
                        _gw = await _gather_write_bindings_for_target(
                            conn,
                            base=deps_bundle.gather_write_bindings,
                            target_id=target_filter,
                            target_version=(
                                inputs[0].get("target_version") if inputs else None
                            ),
                            run_id=run_id,
                            analyst_id=deps_bundle.descriptor.identity.id,
                            analyst_version=deps_bundle.descriptor.identity.version,
                            nats_publish=deps_bundle.deps.nats_publish,
                        )
                        options["gather_tool_bindings"] = _gw["bindings"]
                        options["gather_web_prompt_fragments"] = _gw["web_fragments"]
                        options["gather_write_prompt_fragments"] = _gw[
                            "write_fragments"
                        ]
                # Meta-kind source-analyst injection (Piece 3, Task A3). The
                # meta_findings_synthesizer / cross_analyst_correlator kinds
                # resolve their actual input rows via READ_SLICE, but the
                # run path uses options['source_analyst_ids'] as the
                # authoritative ordering for data.contributing_analysts so the
                # lineage/provenance reflects descriptor INTENT even when a
                # declared source analyst produced 0 findings this window.
                # Lifted from subscription.other_analysts[].id — the same
                # documented surface READ_SLICE resolves the rows from.
                if (
                    deps_bundle.descriptor.identity.kind
                    in ("meta_findings_synthesizer", "cross_analyst_correlator")
                    and "source_analyst_ids" not in options
                ):
                    sub = getattr(deps_bundle.descriptor, "subscription", None)
                    others = (
                        getattr(sub, "other_analysts", None) or []
                        if sub is not None
                        else []
                    )
                    options["source_analyst_ids"] = [
                        str(getattr(a, "id", ""))
                        for a in others
                        if getattr(a, "id", "")
                    ]
                # Critic-kind options resolution (per L-105 / L-175).  The
                # critic needs the analyzed analyst's rubric +
                # analyzed_model + allow_self_correlated escape hatch +
                # analyzed_output_id.  We look these up from the analyzed
                # analyst's descriptor at activation time so the critic's
                # run_method doesn't have to.
                if deps_bundle.descriptor.identity.kind == "critic":
                    critic_ctx = await _resolve_critic_context(
                        conn,
                        deps=deps_bundle,
                        target_filter=target_filter,
                        payload_options=payload.get("options") or {},
                    )
                    for k, v in critic_ctx.items():
                        options.setdefault(k, v)
                # Dispatch shape: 3-arg per L-102 (module-level run_method
                # accepts kind-deps); fall back to the 2-arg back-compat
                # path when no kind-specific deps were registered (the
                # spike's LLMAnalystRunner wraps everything itself).
                #
                # When the actor is in demoted state (per-analyst or global
                # budget exhausted with strategy=demote_and_continue), pick
                # the fallback run_method + kind_deps. The dispatcher itself
                # is identical — the swap happens at the deps level.
                using_fallback = _is_actor_demoted(actor_id, now) and (
                    deps_bundle.fallback_run_method is not None
                )
                active_run_method = (
                    deps_bundle.fallback_run_method
                    if using_fallback
                    else deps_bundle.run_method
                )
                active_kind_deps = (
                    deps_bundle.fallback_kind_deps
                    if using_fallback
                    else deps_bundle.kind_deps
                )
                if using_fallback:
                    options["llm_demoted"] = True
                    options["llm_ref"] = deps_bundle.fallback_llm_ref

                # Consult streaming (Piece 1, D5): wire a per-run step publisher
                # onto a request-scoped NATS subject so each ReAct step relays
                # live to the registry SSE route. ``dataclasses.replace`` makes a
                # per-run COPY of the shared ConsultOnDemandDeps so concurrent
                # requests publish to their own ``request_id`` subjects and never
                # clobber each other's hook. Fire-and-forget on core NATS — the
                # kind's emitter swallows any publish error.
                if (
                    deps_bundle.descriptor.identity.kind == "consult_on_demand"
                    and consult_request_id
                    and deps_bundle.deps.nats_publish is not None
                    and active_kind_deps is not None
                ):
                    _subject = f"legba.consult.steps.{consult_request_id}"
                    _np = deps_bundle.deps.nats_publish

                    async def _consult_step_publish(
                        step: dict[str, Any], _subj: str = _subject, _np=_np
                    ) -> None:
                        await _np(
                            _subj,
                            json.dumps({"type": "step", **step}).encode("utf-8"),
                        )

                    active_kind_deps = dataclasses.replace(
                        active_kind_deps, step_publish=_consult_step_publish
                    )

                # Transient retry loop. Other exception kinds (budget, hard)
                # break out of the retry early. Defaults preserve current
                # behavior (3 attempts, exponential backoff up to 60s).
                method_result = None
                last_exc: BaseException | None = None
                attempts_made = 0
                max_attempts = transient_policy.max_attempts
                for attempt in range(1, max_attempts + 1):
                    attempts_made = attempt
                    try:
                        method_result = await _invoke_run_method(
                            active_run_method,
                            inputs,
                            options,
                            active_kind_deps,
                        )
                        last_exc = None
                        break
                    except BaseException as exc:
                        last_exc = exc
                        bucket = _classify_exception(exc)
                        if bucket != "transient":
                            # Non-transient — let the outer handler classify.
                            raise
                        if attempt >= max_attempts:
                            # Out of attempts — raise so the outer handler
                            # records HARD_FAIL / DLQ per hard_policy.
                            raise
                        delay = _retry_delay_seconds(transient_policy, attempt)
                        logger.warning(
                            "dapr_actors.analyst.transient_retry "
                            "actor_id=%s attempt=%d/%d delay_s=%.2f err=%s",
                            actor_id, attempt, max_attempts, delay, exc,
                        )
                        await asyncio.sleep(delay)
                if method_result is None and last_exc is not None:
                    # Defensive — should never reach here, the loop either
                    # sets method_result or re-raises. Re-raise to keep the
                    # outer handler in charge of classification.
                    raise last_exc

                # Kinds that read their own substrate (consult_on_demand
                # ReAct tool calls) collect refs DURING run_method and
                # surface them via ``method_result.derived_from``. When
                # present these supersede the input-row-derived list — the
                # input rows for those kinds are control-plane data (a
                # question, scope predicate, etc.), not lineage roots.
                explicit_derived = getattr(method_result, "derived_from", None)
                if explicit_derived:
                    derived_ids = [
                        d if isinstance(d, UUID) else UUID(str(d))
                        for d in explicit_derived
                    ]

                if deps_bundle.budget is not None and method_result.usage:
                    await deps_bundle.budget.record(
                        conn,
                        prompt_tokens=int(method_result.usage.get("prompt_tokens", 0)),
                        completion_tokens=int(method_result.usage.get("completion_tokens", 0)),
                        reasoning_tokens=int(method_result.usage.get("reasoning_tokens", 0)),
                    )

                # Shared terminal-frame publisher (Piece 1, D5). Both the chat
                # short-circuit and the deep success path emit one ``final`` frame
                # so the SSE relay can close deterministically. Fire-and-forget.
                async def _publish_consult_final(row_id_or_none: str | None) -> None:
                    if (
                        consult_request_id
                        and deps_bundle.deps.nats_publish is not None
                    ):
                        done = {
                            "type": "final",
                            "request_id": consult_request_id,
                            "output_id": row_id_or_none,
                            "mode": consult_mode,
                        }
                        try:
                            await deps_bundle.deps.nats_publish(
                                f"legba.consult.steps.{consult_request_id}",
                                json.dumps(done).encode("utf-8"),
                            )
                        except Exception:
                            logger.debug(
                                "consult.final_publish.failed", exc_info=True
                            )

                # Deep-consult short-circuit (anchor §5 PIECE 4): a deep_consult
                # run SCHEDULES a durable Dapr Workflow (plan→acquire→analyze→
                # synthesize) and returns the DETACHED task id IN the envelope —
                # no row written here (the workflow's synthesize stage writes the
                # finding). Same GUARD shape as the chat short-circuit; the actor
                # method returns immediately so the registry's submit POST gets a
                # task id in <1s (NOT the 180s blocking path). The deep_consult
                # kind's run_method stamps ``task_id`` / ``status`` on its result.
                if deps_bundle.descriptor.identity.kind == "deep_consult":
                    task_id = getattr(method_result, "task_id", None)
                    submit_status = getattr(method_result, "submit_status", "running")
                    rec["last_run_at"] = _utcnow().isoformat()
                    rec["last_outcome"] = ActorRunOutcome.SUCCESS.value
                    rec["last_error"] = None
                    await self._set_record(rec)
                    await self._state_manager.save_state()
                    return {
                        "outcome": ActorRunOutcome.SUCCESS.value,
                        "mode": "deep_consult",
                        "task_id": task_id,
                        "status": submit_status,
                        "run_id": str(run_id),
                    }

                # Chat short-circuit (Piece 1, D3/D4): a consult run tagged
                # ``mode=chat`` returns its typed ConsultResponsePayload IN the
                # envelope — no row, no receipt chain, no output event, no emit
                # bindings, no escalation. Budget ``record(...)`` and the
                # ``derived_from`` resolution above already ran, so chat runs
                # still meter tokens and report lineage. This is a GUARD around
                # the existing write path, not a fork of it.
                if (
                    deps_bundle.descriptor.identity.kind == "consult_on_demand"
                    and consult_mode == "chat"
                ):
                    cr = getattr(method_result, "consult_response", None)
                    rec["last_run_at"] = _utcnow().isoformat()
                    rec["last_outcome"] = ActorRunOutcome.SUCCESS.value
                    rec["last_error"] = None
                    await self._set_record(rec)
                    await self._state_manager.save_state()
                    await _publish_consult_final(None)
                    return {
                        "outcome": ActorRunOutcome.SUCCESS.value,
                        "mode": "chat",
                        "consult_response": (
                            cr.model_dump(mode="json") if cr is not None else None
                        ),
                        "derived_from": [str(d) for d in derived_ids],
                    }

                target_id = (
                    target_filter
                    or (inputs[0].get("target_id") if inputs else None)
                )
                target_version = inputs[0].get("target_version") if inputs else None
                analyst_ctx = AnalystContext(
                    analyst_id=deps_bundle.descriptor.identity.id,
                    analyst_version=deps_bundle.descriptor.identity.version,
                    run_id=run_id,
                    target_id=target_id,
                    target_version=target_version,
                )
                # "Findings as a real output type" — resolve the per-run
                # EFFECTIVE output kind. For the sub-dispatched deterministic
                # kind this picks the per-sub-handler value out of
                # OUTPUT_KIND_BY_SUB_HANDLER; for the META kinds
                # (relationship_reifier / competing_hypotheses) the bind-time
                # kind IS the resolved value. The result is either a real
                # OutputKind (write a row) or the TRACE_ONLY sentinel.
                output_kind = _resolve_effective_output_kind(
                    kind=deps_bundle.descriptor.identity.kind,
                    bind_output_kind=deps_bundle.output_kind,
                    options=options,
                )
                # A handler may force THIS run to trace-only (skip the feed row)
                # even though its sub-handler is a real FINDING kind — used by
                # idempotent meta-handlers (situation_clustering / thematic_
                # proposal) to suppress a no-change re-run's repeated summary so
                # it doesn't flood the findings feed. The trace + side-writes
                # still run, so nothing is lost.
                if getattr(method_result, "force_trace_only", False):
                    output_kind = TRACE_ONLY
                trace_only = output_kind is TRACE_ONLY
                # The run summary the receipt chain hashes/persists. For a
                # trace-only kind there is no analyst_outputs row, but the
                # FindingPayload run summary still flows into the trace's
                # output_payload so NOTHING is lost (candidates/typed/written/
                # superseded/decayed counts live in analyst_traces).
                output_payload = (
                    _payload_finding(method_result)
                    if trace_only
                    else _select_output_payload(method_result, output_kind)
                )
                output_row = None
                if trace_only:
                    # TRACE-ONLY: skip the redundant analyst_outputs FINDING
                    # receipt entirely. The kind's run_method already ran its
                    # real side-writes (write_nexus / write_hypothesis /
                    # write_graph_metric / decay/dedup stamps) on its own
                    # connection. The run is fully audited below via the
                    # receipt chain (analyst_traces).
                    logger.info(
                        "dapr_actors.analyst.trace_only actor_id=%s kind=%s "
                        "sub_handler=%s run_id=%s (no analyst_outputs row; "
                        "run audited in analyst_traces)",
                        actor_id,
                        deps_bundle.descriptor.identity.kind,
                        options.get("sub_handler"),
                        run_id,
                    )
                else:
                    # write_analyst_output returns ``(OutputRow, dlq_entry|None)``.
                    # The DLQ entry is None on the happy path; for the spike we
                    # surface the finding row's id directly.
                    #
                    # AGE :DerivedFrom mirror (opt-in, D3) — when enabled, MERGE
                    # the output-lineage edges on the SAME conn (atomic with the
                    # INSERT, best-effort). Default OFF: relational derived_from[]
                    # is the lineage source of truth, so no per-write graph cost.
                    age_hook = (
                        make_conn_age_output_hook(conn)
                        if _age_derived_from_enabled()
                        else None
                    )
                    output_row, _dlq = await write_analyst_output(
                        conn,
                        analyst_ctx=analyst_ctx,
                        kind=output_kind,
                        output_payload=output_payload,
                        derived_from=derived_ids,
                        age_hook=age_hook,
                    )
                    if output_row is None:
                        rec["last_error"] = "analyst_output_validation_failed"
                        rec["last_outcome"] = ActorRunOutcome.HARD_FAIL.value
                        rec["last_run_at"] = _utcnow().isoformat()
                        await self._set_record(rec)
                        await self._state_manager.save_state()
                        return {
                            "outcome": ActorRunOutcome.HARD_FAIL.value,
                            "error": "analyst output failed validation, sent to DLQ",
                        }

                # Extend the per-analyst receipt chain (L-107 §7) when one
                # is wired. The chain INSERT lands in ``analyst_traces``
                # carrying ``(receipt_hash, prev_receipt_hash)``; the
                # output-row id we just wrote folds into the canonical
                # receipt payload so the chain is tamper-evident over the
                # produced rows, not just the run metadata.
                #
                # Back-compat: when ``receipt_chain`` is None (the spike
                # integration test path) the chain step is skipped and
                # the run completes exactly as today.
                receipt_hash: str | None = None
                prev_receipt_hash: str | None = None
                if deps_bundle.receipt_chain is not None:
                    # Thread the kind's per-run trace into the chain row.
                    # ``intermediate_steps`` is the seven-phase trace
                    # populated by every analyst kind (per L-105 §3.1).
                    # ``tool_calls`` is the L-175 native-tool log
                    # populated by kinds whose run_method drove a
                    # tool-use ReAct loop (the critic kind today; future
                    # ReAct kinds adopt the same field).  Defaults to []
                    # for kinds that don't touch either surface so the
                    # chain row's columns stay non-null per the schema.
                    receipt_hash, prev_receipt_hash = (
                        await deps_bundle.receipt_chain.record(
                            run_id=run_id,
                            analyst_id=deps_bundle.descriptor.identity.id,
                            analyst_version=(
                                deps_bundle.descriptor.identity.version
                            ),
                            cadence_trigger=str(
                                payload.get("trigger_kind", "method")
                            ),
                            target_id=target_id,
                            input_row_refs=derived_ids,
                            input_payload=None,
                            prompt_module_hash=getattr(
                                method_result, "prompt_module_hash", None
                            ),
                            prompt_rendered=getattr(
                                method_result, "prompt_rendered", None
                            ),
                            output_row_refs=(
                                [output_row.id] if output_row is not None else []
                            ),
                            output_payload=_receipt_output_payload(
                                output_payload
                            ),
                            run_started_at=now,
                            run_ended_at=_utcnow(),
                            status="success",
                            intermediate_steps=getattr(
                                method_result, "intermediate_steps", None
                            ) or None,
                            tool_calls=getattr(
                                method_result, "tool_calls", None
                            ) or None,
                        )
                    )

                # L-175 trace-finalizer: when the analyst's output kind is
                # CRITIQUE, also write a row to ``analyst_critiques`` so the
                # eval-loop's trace-level critique sink stays consistent
                # with the analyst_outputs row. The kind handler docstring
                # has long noted this as the trace-finalizer's job; before
                # this hook landed the column was empty in production and
                # the optimizer's training-window query (which joins
                # analyst_traces ↔ analyst_critiques on run_id) saw no
                # rows. Best-effort: failure here logs + continues — the
                # row in analyst_outputs already lands.
                if output_kind == OutputKind.CRITIQUE:
                    try:
                        await _write_critique_trace_record(
                            conn,
                            run_id=run_id,
                            trace_written=(receipt_hash is not None),
                            judge_analyst_id=deps_bundle.descriptor.identity.id,
                            judge_analyst_version=(
                                deps_bundle.descriptor.identity.version
                            ),
                            payload=output_payload,
                            options=options,
                        )
                    except Exception as exc:  # pragma: no cover
                        logger.warning(
                            "dapr_actors.analyst.critique_trace.failed "
                            "actor_id=%s run_id=%s err=%s",
                            actor_id, run_id, exc,
                        )

                # P0-T2 — MANDATORY faithfulness verify pass. After an
                # inline_target FINDING lands (with its P0-T1 ``data['citations']``
                # bridge), run the deterministic citation-presence floor (+ the
                # optional flag-gated LLM judge) and persist the verdict as a
                # ``critique`` on THIS conn. The existing finding↔critique gate
                # (substrate_reads_api / substrate_query_port) then folds
                # ``effective_confidence = min(confidence, faithfulness_score)``.
                # SCOPED to the inline_target finding kind (the helper guards);
                # best-effort — a verify failure leaves the finding durable +
                # un-demoted (no regression). TRACE_ONLY runs have no row to grade.
                # P3-T3 widened the fire condition to the per-country COMPOSITION;
                # P3-T5 widens it again to the GLOBAL/world composition. Both are
                # meta_findings_synthesizer findings verified via the SAME
                # generalized pass (their [[ref:uuid]] sub-claim/country-read
                # bridge). The discriminator is now the descriptor DECLARING
                # ``method.llm.verify`` (the opt-in both compositions carry) —
                # NOT ``bool(target_id)``, which excluded the target-LESS world
                # run. The old global ``meta_synthesizer`` (no verify block) stays
                # excluded; the verify_inline_target_finding scope guard's
                # "citations present" check is the belt to this suspenders (the
                # honest-empty composition writes no citations key → no-op).
                verification_block: dict[str, Any] | None = None
                if (
                    not trace_only
                    and output_row is not None
                    and output_kind == OutputKind.FINDING
                    and (
                        deps_bundle.descriptor.identity.kind == "inline_target"
                        or (
                            deps_bundle.descriptor.identity.kind
                            == "meta_findings_synthesizer"
                            and _descriptor_declares_verify(deps_bundle.descriptor)
                        )
                    )
                ):
                    try:
                        verification_block = await verify_inline_target_finding(
                            conn,
                            deps=deps_bundle,
                            finding_id=output_row.id,
                            finding_payload=output_payload,
                            run_id=run_id,
                        )
                    except Exception as exc:  # pragma: no cover — never break a run
                        logger.warning(
                            "dapr_actors.analyst.verify.failed actor_id=%s "
                            "run_id=%s err=%s", actor_id, run_id, exc,
                        )

                # Publish analyst-output event per L-191:
                # ``analyst.<analyst_id>.<channel>`` where ``channel`` is
                # the plural lower-snake form of the output kind name
                # (findings / predictions / situations / …).
                # L-191 "output produced" event on analyst.<id>.<channel>. The
                # per-analyst JetStream stream that captures this subject is
                # created by the analyst's nats_stream output sink — so an
                # analyst with NO output bindings (e.g. the deterministic
                # maintenance analysts: entity_resolution / cross_source_dedup,
                # outputs:[]) has no capturing stream, and a JetStream publish
                # there returns "no response from stream". Skip the publish when
                # there's no sink (nobody is listening), and keep the residual
                # no-stream/no-responders case benign (the finding already
                # persisted to analyst_outputs — this event is best-effort tail).
                if (
                    not trace_only
                    and deps_bundle.deps.nats_publish is not None
                    and deps_bundle.descriptor.outputs
                ):
                    channel = _channel_for_kind(output_kind)
                    subject = (
                        f"analyst.{deps_bundle.descriptor.identity.id}.{channel}"
                    )
                    envelope = {
                        "analyst_id": deps_bundle.descriptor.identity.id,
                        "analyst_version": deps_bundle.descriptor.identity.version,
                        "run_id": str(run_id),
                        "target_id": target_id,
                        "kind": output_kind.value,
                        "produced_at": _utcnow().isoformat(),
                        "receipt_hash": receipt_hash,
                        "prev_receipt_hash": prev_receipt_hash,
                    }
                    try:
                        await deps_bundle.deps.nats_publish(
                            subject, json.dumps(envelope).encode("utf-8"),
                        )
                    except Exception as exc:  # pragma: no cover
                        _m = str(exc).lower()
                        if "no response from stream" in _m or "no responders" in _m or "no stream" in _m:
                            logger.debug(
                                "dapr_actors.analyst.publish.no_stream actor_id=%s subject=%s (benign — no capturing stream)",
                                actor_id, subject,
                            )
                        else:
                            logger.warning(
                                "dapr_actors.analyst.publish.failed actor_id=%s err=%s",
                                actor_id, exc,
                            )

                # L-195/L-197: route the LIVE payload to the analyst's
                # emit-capable output bindings (e.g. stix_bundle → a STIX 2.1
                # bundle on legba.outputs.stix.<target_id> + optional file sink).
                # The notification envelope above only ANNOUNCES the output; this
                # is what actually invokes the producer kinds. Best-effort — the
                # finding is already durable; an export failure logs and never
                # breaks the run. Kinds with no emit surface (substrate /
                # a2a_skill / mcp_tool) are skipped.
                #
                # TRACE-ONLY kinds skip emit entirely: there is no
                # analyst_outputs row to announce/export, and their real product
                # (nexuses / hypotheses / maintenance stamps) is side-written to
                # its own table, not routed through an output binding.
                if not trace_only:
                    await _emit_output_bindings(
                        descriptor=deps_bundle.descriptor,
                        payload=output_payload,
                        output_id=getattr(output_row, "id", None),
                        derived_from=derived_ids,
                        target_id=target_id,
                        nats_publish=deps_bundle.deps.nats_publish,
                        pg_pool=getattr(deps_bundle.deps, "pg_pool", None),
                        http_client=(getattr(deps_bundle.deps, "extras", None) or {}).get(
                            "output_http_client"
                        ),
                    )

                # A-3c (review G2 / decision D1): the escalate_finding pack.
                # When the analyst carries an escalation binding and the
                # landed FINDING crosses the pack's gate, fire `escalate`
                # through the FULL agency pipeline (resolve ∩ allow ∩
                # applicability → governor → channel emit → settle → audit).
                # Best-effort: the finding is already durable — an
                # escalation failure logs and never breaks the run.
                if (
                    output_kind == OutputKind.FINDING
                    and deps_bundle.escalation is not None
                ):
                    try:
                        await _maybe_escalate_finding(
                            conn,
                            escalation=deps_bundle.escalation,
                            payload=output_payload,
                            output_row_id=output_row.id,
                            target_id=target_id,
                            actor_id=actor_id,
                            # S8-T2 — hand the computed faithfulness verdict to
                            # the escalation gate so a verify-DEMOTED finding
                            # gates on its effective confidence, not the raw
                            # LLM-asserted number. NULL when nothing was verified
                            # (TRACE_ONLY / non-verify kind) → gate unchanged.
                            verification_block=verification_block,
                        )
                    except Exception as exc:  # pragma: no cover — best-effort
                        logger.warning(
                            "dapr_actors.analyst.escalation.failed "
                            "actor_id=%s err=%s", actor_id, exc,
                        )

                rec["last_run_at"] = _utcnow().isoformat()
                rec["last_outcome"] = ActorRunOutcome.SUCCESS.value
                rec["last_error"] = None
                if deps_bundle.descriptor.cadence.cooldown_seconds > 0:
                    # Per-(analyst, target) cooldown — keyed by target_filter so
                    # each target throttles independently (a busy country can't
                    # starve a quiet one). "_global" for the meta/global run.
                    cd_key = str(target_filter) if target_filter else "_global"
                    cd_map = rec.get("cooldown_by_target")
                    if not isinstance(cd_map, dict):
                        cd_map = {}
                    cd_map[cd_key] = (
                        _utcnow()
                        + timedelta(seconds=deps_bundle.descriptor.cadence.cooldown_seconds)
                    ).isoformat()
                    rec["cooldown_by_target"] = cd_map
                await self._set_record(rec)
                await self._state_manager.save_state()

                # ``finding_id`` retained for back-compat with the spike
                # integration test; the field is the analyst-output row
                # uuid regardless of the underlying OutputKind, so the
                # name is misleading for non-FINDING kinds.  Expose the
                # canonical ``output_id`` as well so newer callers can
                # pivot off the kind-aware name.  TRACE-ONLY kinds wrote no
                # analyst_outputs row, so both ids are None and ``kind`` is
                # the literal "trace_only" sentinel marker.
                row_id = str(output_row.id) if output_row is not None else None
                # Deep consult terminal frame (Piece 1, D5) — close the SSE
                # relay with the persisted row id when this was a streamed run.
                await _publish_consult_final(row_id)
                return {
                    "outcome": ActorRunOutcome.SUCCESS.value,
                    "finding_id": row_id,
                    "output_id": row_id,
                    "kind": "trace_only" if trace_only else output_kind.value,
                    "trace_only": trace_only,
                    "derived_from": [str(d) for d in derived_ids],
                    "receipt_hash": receipt_hash,
                    "prev_receipt_hash": prev_receipt_hash,
                }
        except Exception as exc:
            # Typed retry policy classification (Phase 5 hardening item 5).
            # Reaching this handler means the in-flight call surfaced an
            # exception that the transient retry loop didn't absorb. Bucket
            # it now + decide outcome accordingly:
            #
            #   * budget    → BUDGET_THROTTLED (cooldown_until per the
            #                 BudgetRetryPolicy.cooldown_seconds);
            #   * transient → TRANSIENT_FAIL (retries already exhausted —
            #                 the next reminder/trigger will reattempt);
            #   * hard      → HARD_FAIL + DLQ (per HardRetryPolicy).
            #
            # Defaults via RetryBlock() apply when the descriptor doesn't
            # set ``method.retry``.
            bucket_kind = _classify_exception(exc)
            try:
                retry_block = deps_bundle.descriptor.method.retry
            except AttributeError:                              # pragma: no cover
                from ..data.schemas.analyst import RetryBlock
                retry_block = RetryBlock()

            if bucket_kind == "budget":
                # BudgetExhausted raised mid-call (uncommon — usually
                # caught by precall_check). Honor budget_policy.
                strategy = retry_block.budget.strategy
                cooldown_s = retry_block.budget.cooldown_seconds
                outcome_val = ActorRunOutcome.BUDGET_THROTTLED.value
                logger.warning(
                    "dapr_actors.analyst.budget_failure actor_id=%s "
                    "strategy=%s err=%s",
                    actor_id, strategy, exc,
                )
                rec["last_error"] = str(exc)
                rec["error_count"] = int(rec.get("error_count", 0)) + 1
                rec["last_run_at"] = _utcnow().isoformat()
                rec["last_outcome"] = outcome_val
                if strategy == "pause_until_next_window":
                    rec["cooldown_until"] = (
                        _utcnow() + timedelta(seconds=cooldown_s)
                    ).isoformat()
                await self._set_record(rec)
                await self._state_manager.save_state()
                return {"outcome": outcome_val, "error": str(exc)}

            if bucket_kind == "transient":
                # Transient failures that escaped the retry loop end up
                # here. Mark TRANSIENT_FAIL so the next reminder fire
                # reattempts; do NOT cool down the actor.
                logger.warning(
                    "dapr_actors.analyst.transient_exhausted actor_id=%s "
                    "err=%s",
                    actor_id, exc,
                )
                rec["last_error"] = str(exc)
                rec["error_count"] = int(rec.get("error_count", 0)) + 1
                rec["last_run_at"] = _utcnow().isoformat()
                rec["last_outcome"] = ActorRunOutcome.TRANSIENT_FAIL.value
                await self._set_record(rec)
                await self._state_manager.save_state()
                return {
                    "outcome": ActorRunOutcome.TRANSIENT_FAIL.value,
                    "error": str(exc),
                }

            # bucket_kind == "hard" (or unknown — see _classify_exception).
            hard_strategy = retry_block.hard.strategy
            logger.exception(
                "dapr_actors.analyst.run.error actor_id=%s strategy=%s err=%s",
                actor_id, hard_strategy, exc,
            )
            rec["last_error"] = str(exc)
            rec["error_count"] = int(rec.get("error_count", 0)) + 1
            rec["last_run_at"] = _utcnow().isoformat()
            if hard_strategy == "pause":
                rec["last_outcome"] = ActorRunOutcome.HARD_FAIL.value
                rec["cooldown_until"] = (
                    _utcnow() + timedelta(hours=1)
                ).isoformat()
            elif hard_strategy == "drop":
                # Drop quietly — no DLQ, no cooldown. Tests can detect via
                # the absence of cooldown_until.
                rec["last_outcome"] = ActorRunOutcome.HARD_FAIL.value
            else:
                # dlq_and_alert (default).
                rec["last_outcome"] = ActorRunOutcome.HARD_FAIL.value
            await self._set_record(rec)
            await self._state_manager.save_state()
            return {
                "outcome": ActorRunOutcome.HARD_FAIL.value,
                "error": str(exc),
                "hard_strategy": hard_strategy,
            }
        finally:
            # Unbind the per-run log correlation context (W-1b §3).
            _reset_run_log_context(_log_ctx_token)

    def _minimal_worker_record(
        self,
        actor_id: str,
        deps_bundle: "_AnalystDeps",
        target_filter: str,
    ) -> dict[str, Any]:
        """Build the inline ACTIVE record for a lazy-activated WORKER actor.

        A2 concurrency: a worker (id ``analyst::<descriptor_id>::<target_id>``)
        has no descriptor of its own — its deps resolve through the segment-1
        fallback resolver against the PRIMARY analyst's head descriptor. So
        the record mirrors the descriptor identity the primary would write in
        ``_on_activate``, but is driven straight to ACTIVE through the same
        FSM transitions (CONFIGURE → ACTIVATE) WITHOUT registering a reminder.
        The cadence heartbeat stays solely on the primary, which fans out to
        this worker; the worker cannot self-fire.
        """
        rec: dict[str, Any] = {
            "actor_id": actor_id,
            "actor_kind": "analyst",
            "descriptor_id": deps_bundle.descriptor.identity.id,
            "descriptor_version": deps_bundle.descriptor.identity.version,
            "lifecycle": DRAFT,
            "last_run_at": None,
            "last_outcome": None,
            "cooldown_until": None,
            "error_count": 0,
            "last_error": None,
            # Marks this record as a per-target worker (vs. the primary /
            # meta global record) for inspection + telemetry.
            "worker_target": target_filter,
        }
        fsm = LifecycleFSM(state=DRAFT)
        fsm.transition(LifecycleEvent.CONFIGURE, initiated_by="dapr_worker_lazy_activate")
        fsm.transition(LifecycleEvent.ACTIVATE, initiated_by="dapr_worker_lazy_activate")
        rec["lifecycle"] = fsm.state
        return rec

    async def _get_record(self) -> dict[str, Any] | None:
        ok, val = await self._state_manager.try_get_state("record")
        return val if ok else None

    async def _set_record(self, rec: dict[str, Any]) -> None:
        await self._state_manager.set_state("record", rec)


# ---------------------------------------------------------------------------
# Helpers — extracted so the actor classes don't carry side-effecty methods
# ---------------------------------------------------------------------------


from .actor_output_emit import (  # noqa: F401  -- re-export: public API stability (#93)
    _utcnow,
    _channel_for_kind,
    _NATS_CHANNEL_BY_KIND,
    _OUTPUT_KIND_HANDLERS,
    _output_kind_handlers,
    _NatsPublishAdapter,
    _emit_output_bindings,
    _maybe_escalate_finding,
    _gather_binding_for_target,
    _gather_write_bindings_for_target,
)


from .actor_payload import (  # noqa: F401  -- re-export: public API stability (#93)
    _invoke_run_method,
    _receipt_output_payload,
    _payload_finding,
    _payload_nested,
    _PAYLOAD_SELECTORS,
    _resolve_effective_output_kind,
    _select_output_payload,
    _write_critique_trace_record,
)


# ---------------------------------------------------------------------------
# Critic-kind options resolution helper
# ---------------------------------------------------------------------------


from .actor_critic import (  # noqa: F401  -- re-export: public API stability (#93)
    _extract_primary_model_ref,
    _resolve_critic_context,
    _critic_descriptor_pinned_analyst_id,
    verify_inline_target_finding,
)


from .actor_substrate_slice import (  # noqa: F401  -- re-export: public API stability (#93)
    _global_slice_per_source_cap,
    _slice_row_cap,
    _diversify_by_source,
    _slice_graph_structure_cap,
    _select_graph_structure_items,
    _SLICE_INTERESTING_KIND_LABELS,
    _read_substrate_slice,
)


__all__ = [
    "AnalystActor",
    "AnalystActorInterface",
    "TargetActor",
    "TargetActorInterface",
    "_AnalystDeps",
    "_PAYLOAD_SELECTORS",
    "_TargetDeps",
    "_bucket_end_iso",
    "_classify_exception",
    "_extract_primary_model_ref",
    "_invoke_run_method",
    "_is_actor_demoted",
    "_payload_finding",
    "_payload_nested",
    "_read_substrate_slice",
    "_resolve_analyst_deps",
    "_resolve_critic_context",
    "_resolve_target_deps",
    "_retry_delay_seconds",
    "_select_output_payload",
    "_split_actor_id",
    "_worker_actor_id",
    "_write_critique_trace_record",
    "_FANOUT_CHUNK",
    "_set_analyst_demoted",
    "_set_global_demoted",
    "clear_analyst_demotion",
    "clear_deps_registry",
    "evict_analyst_deps_for_descriptor",
    "register_analyst_deps",
    "register_analyst_deps_resolver",
    "reminder_guard_decision",
    "register_target_deps",
    "register_target_deps_resolver",
    "verify_inline_target_finding",
]
