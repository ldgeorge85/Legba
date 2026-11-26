# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SourceActor — the source-first acquisition runtime (P-06).

A :class:`SourceActor` owns one :class:`SourceDescriptor`. Unlike the legacy
target-owned :class:`~legba.runtime.dapr_actors.TargetActor` (one pull per
*target*), a source pulls/ingests **once** regardless of how many targets
consume it, writes ONE canonical, target-agnostic :class:`Signal` per
observation, and publishes to ``source.<id>.signals`` for the subscription
layer (P-08) to fan out.

Two acquisition modes (descriptor ``acquisition``):

  * **poll** — at activation the actor registers a Dapr **Reminder** derived
    from ``cadence.schedule`` (durable across sidecar restarts; constant-
    period crons map cleanly — variable-period schedules route to Dapr Jobs,
    out of P-06 scope). On each fire ``run`` pulls the handler, runs the
    per-source baseline, writes, publishes, and persists the cursor.
  * **push** — the actor does NOT poll; at activation it registers the
    handler against the shared inbound-webhook router. An inbound POST wakes
    the handler, which emits Signals through an ``emit_signal`` callback the
    actor supplies — the same baseline → write → publish path as poll.

Provisioning (§4.2.1): a poll OR push source may also register an outbound
upstream watch at activation. The actor calls
:func:`legba.data.sources.provision.reconcile_provision` on activate and
:func:`~legba.data.sources.provision.deprovision_all` on retire, idempotently.

Cursor policy (``pull_once``): the ``since`` cursor advances along REAL
forward progress — to the last processed entry's logical timestamp, never a
bare NOW; a hard error keeps the prior position; a zero-yield pull keeps it; a
first-ever pull seeds NOW. An entry skipped for a TRANSIENT reason
(enrichment timeout / NLP unavailable) is not progress — the advance holds at
``max(since, min(last_processed, earliest_skipped - 1µs))`` so the next pull
re-yields it, the rest of that poll bypasses the NLP chain (written unenriched,
``payload.enrichment_pending``), and an entry skipped on 3 consecutive polls is
written unenriched. Mechanism: :mod:`.source_enrich_hold`. The
cursor is never later than now (feed timestamps clamped, a future stored
cursor healed on read), a dated-feed handler (rss, json_api) is handed
``since - lookback`` with a ledger of already-judged entries, and an undated
entry of such a feed advances nothing. Mechanism: :mod:`.source_cursor`.

Design note — the **mechanism** is the deliverable; the handler *library* is
incremental. The core logic lives in :class:`SourceCore` (a plain class,
directly testable against the dev rig without a Dapr sidecar). The thin
:class:`SourceActor` Dapr wrapper delegates to it so the production path and
the tested path are the same code. This module never edits the existing
``dapr_actors.py`` target/analyst actors (those are RED + out of scope).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import asyncpg
from pydantic import BaseModel, ConfigDict

from ..data.claim_hygiene import poll_claim_hygiene, prepare_claim_row, tally
from ..data.schemas.source import SourceDescriptor
from ..data.source_classification import bind_declared
from ..data.tags import EMPTY_INDEX, tag_vocabulary_index
from ..data.filters.ingest_dedupe import IngestDedupe, ingest_dedupe_from_stages
from ..data.sources._contract import Signal, SourceContext, SourceHealth
from ..data.jobs.media import MediaEndpointNotConfiguredError
from ..data.sources.baseline import (
    MEDIA_MODALITIES,
    MediaExtractor,
    default_extractor_registry,
    run_baseline,
)
from ..data.sources.provision import (
    HttpUpstreamClient,
    UpstreamClient,
    deprovision_all,
    desired_watch_set,
    reconcile_provision,
)
from ..data.stack.nlp_service.client import NlpServiceUnavailable
from .actor_turn import bounded_turn_op_or
from .deps import StandardDeps
from .enrichment_timing import cursor_fields as enrichment_cursor_fields
from .source_enrich_hold import EnrichHold
from .source_cursor import (
    CURSOR_FUTURE_TOLERANCE,
    LookbackLedger,
    WalkOrder,
    advance as advance_cursor,
    entry_logical_ts,
    feed_ts,
    lookback_of,
    read_since,
)
from .fetch_receipts import ReceiptRecorder
from .sinks import SinkSet
from .sinks.signal import SignalSink
from .source_factory import build_source_handler, discover_source_kinds
from .source_first_poll_age import FirstPollAgeCap
from .source_keyword_gate import SourceIngestGate
from .source_poll_outcome import classify_poll_outcome, write_poll_outcome
from .source_poll_records import (
    captured, emit_record, finish_poll, link_inbound, protocol_posture, route_record,
    sink_summary as _sink_summary,
)
from .state import FilterStateStore

logger = logging.getLogger(__name__)


# Lifecycle states (mirror runtime/lifecycle constants without importing the
# FSM — the SourceActor keeps its own minimal record).
DRAFT = "draft"
ACTIVE = "active"
PAUSED = "paused"
RETIRED = "retired"

# Per-poll robustness bounds (2026-06-08 stall fix). A poll MUST always finish
# fast enough to reach its cursor-advance within Dapr's drain window — otherwise
# a backlog (e.g. a cursor stuck in the past) makes every poll re-process the
# same large window, get cut mid-flight before advancing, and trap the poller
# in a producing-nothing loop forever. We bound each poll by count AND wall time,
# publish each written signal immediately (fan-out survives an interrupted pull),
# advance the cursor in a `finally`, and time-box per-entry enrichment so one
# hung extractor call can't block the actor. Downstream content-hash dedup makes
# the resulting window overlap a harmless no-op.
_MAX_ENTRIES_PER_POLL = 100      # hard count cap per pull (generic default)
_POLL_BUDGET_S = 30.0            # wall-time budget per pull (generic default)
_ENRICH_TIMEOUT_S = 12.0         # per-entry baseline/enrichment timeout

# R5 (2026-07-30) — PER-SOURCE poll bounds. The two defaults above are sized for
# the modal source (an RSS/JSON feed that returns one page in a couple of
# seconds). They are NOT universal: the telegram handler walks ~30 channels
# behind its own 180s cycle guard, so a 30s outer budget truncated 100 of 105
# live polls and the walk never reached the tail of the channel list at all —
# the generic bound was cutting the pull long before the handler's own
# economics applied. Each source now resolves its own bounds:
#
#   1. the descriptor's ``config`` (the operator's tuning surface — live-PUT
#      per source, no code change), then
#   2. the handler's advertised ``poll_budget_seconds`` /
#      ``max_entries_per_poll`` (the handler knows its own cost; this is the
#      same "handler advertises, actor reads" contract as ``health_state_key``
#      / ``BULK_RESUME_OFFSET_KEY``), then
#   3. the generic defaults above (UNCHANGED for every source that declares
#      nothing — which is all of them but telegram).
#
# Both overrides are CLAMPED to the ceilings below. A poll must still finish
# inside Dapr's drain window and reach its cursor-advance, so no descriptor
# typo may park an actor indefinitely: the ceiling is the invariant, the
# override is a dial inside it.
_MAX_POLL_BUDGET_S = 240.0       # ceiling on any per-source wall-time override
_MAX_ENTRIES_CEILING = 1000      # ceiling on any per-source entry-cap override
# N-L: typed non-document records (observations) are buffered and written in
# one batch, with no per-entry enrichment — bounded by this ceiling and the
# same wall-time budget, not by the document entry cap.
_MAX_SINK_RECORDS_PER_POLL = 20_000
_POLL_BUDGET_CONFIG_KEY = "poll_budget_seconds"
_MAX_ENTRIES_CONFIG_KEY = "max_entries_per_poll"

# S-4 intra-source exact-hash dedup + the canonical signal write moved to the
# document sink (runtime/sinks/signal.py, N-L step 0) — re-exported here so
# every existing import and env-knob reference keeps working.
from .sinks.signal import (  # noqa: E402,F401 — re-exports
    _DEFAULT_INTRASOURCE_DEDUP_WINDOW_HOURS,
    _INSERT_SIGNAL,
    _INTRASOURCE_DEDUP_ENV,
    _INTRASOURCE_DEDUP_WINDOW_ENV,
    _bump_intrasource_duplicate,
    _bump_intrasource_sql,
    _intrasource_dedup_enabled,
    _intrasource_dedup_window_hours,
    _json_or_none,
    lookup_source_credibility,
    write_canonical_signal,
)

# Bulk-dataset traversal (high-water mark). A "bulk" source streams ONE large
# snapshot whose entries all carry the same coarse logical timestamp (e.g.
# OpenSanctions ``targets.simple.csv`` — ~50k rows sharing a daily ``last_seen``),
# so the ``since`` cursor can NOT paginate WITHIN a snapshot. Combined with the
# per-poll cap, a since-only cursor restarts every pull from row 0 and never
# reaches row 101+. For these kinds the actor keeps a separate HIGH-WATER-MARK
# offset (rows already traversed in the current snapshot) in the shared state
# store; the handler resumes from it. The offset resets to 0 when the handler
# reaches end-of-stream (a complete walk) so the next snapshot re-walks fresh.
#
# Detection is mode-aware (not kind-wide): only the dataset-streaming mode of a
# handler needs the offset. A handler opts in by reading
# :data:`BULK_RESUME_OFFSET_KEY` from ``ctx.state_store`` and reporting how many
# rows it traversed via :data:`BULK_TRAVERSED_KEY`.
BULK_RESUME_OFFSET_KEY = "bulk_resume_offset"   # actor -> handler (rows to skip)
BULK_TRAVERSED_KEY = "bulk_traversed"           # handler -> actor (this-pull walk)

# (kind -> config keys whose values mark the dataset-streaming mode). A source
# is bulk-traversed only when its descriptor config selects one of these modes.
_BULK_MODES: dict[str, tuple[str, frozenset[str]]] = {
    "opensanctions": ("mode", frozenset({"bulk_csv"})),
}


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _bulk_resume_field(descriptor: SourceDescriptor) -> str | None:
    """Return the config field selecting a bulk-streaming mode, or None.

    A source is bulk-traversed (needs the high-water-mark offset cursor)
    only when its handler kind is registered in :data:`_BULK_MODES` AND its
    descriptor config selects one of that kind's dataset-streaming modes.
    Returns the config FIELD NAME on a match (caller has already confirmed
    the value) — None otherwise (api/self_hosted/non-bulk kinds keep the
    plain ``since`` cursor).
    """
    spec = _BULK_MODES.get(descriptor.identity.kind)
    if spec is None:
        return None
    field, bulk_values = spec
    cfg = descriptor.config or {}
    raw = cfg.get(field)
    # Config values may be Property-factory-wrapped ({"raw": ...}); unwrap.
    if isinstance(raw, dict) and "raw" in raw:
        raw = raw["raw"]
    # A handler whose mode field defaults to a bulk value (e.g. OpenSanctions
    # ``mode`` defaults to ``bulk_csv``) is bulk-traversed even when the field
    # is omitted from the descriptor config.
    if raw is None:
        raw = _bulk_mode_default(descriptor.identity.kind, field)
    if isinstance(raw, str) and raw in bulk_values:
        return field
    return None


def _bulk_mode_default(kind: str, field: str) -> str | None:
    """Best-effort default for a bulk mode field when the descriptor omits it.

    The handler's pydantic ``config_schema`` owns the real default; we only
    need the field's default to decide whether an omitted config still
    selects a bulk-streaming mode. Resolved from the handler class when
    importable, else None (treated as non-bulk — fail safe, never trap).
    """
    try:
        from ..data.sources.opensanctions import OpenSanctionsConfig
    except Exception:  # pragma: no cover - optional dep absent
        return None
    if kind == "opensanctions":
        return OpenSanctionsConfig.model_fields[field].default
    return None  # pragma: no cover


def bulk_highwater_advance(
    prior_offset: int,
    rows_traversed: int,
    *,
    reached_end: bool,
) -> int:
    """Compute the next bulk high-water-mark offset (pure, unit-testable).

    The offset is the count of rows ALREADY traversed in the current dataset
    snapshot — the handler skips this many rows on the next pull so each pull
    RESUMES where the prior one stopped instead of restarting from row 0.

      * Mid-snapshot (the pull was capped before exhausting the stream):
        advance by the rows this pull walked — ``prior + traversed``.
      * End-of-stream reached (a complete walk of the snapshot): reset to 0
        so the next pull re-walks the (refreshed) snapshot from the top.

    ``rows_traversed`` is clamped to ``>= 0`` defensively; a negative/garbage
    report can NOT rewind the high-water mark below where it already stood.
    """
    walked = max(0, int(rows_traversed))
    if reached_end:
        return 0
    return max(0, int(prior_offset)) + walked


def _unwrap_factory_value(raw: Any) -> Any:
    """Unwrap a descriptor property-factory shape (``{"raw": ..., ...}``).

    Descriptor config values arrive either bare or wrapped by the property
    factory; every actor-side read of a config field has to tolerate both
    (see :func:`_bulk_resume_field`, which does the same unwrap inline).
    """
    if isinstance(raw, dict) and "raw" in raw:
        return raw["raw"]
    return raw


def _positive_number(raw: Any) -> float | None:
    """Coerce ``raw`` to a POSITIVE float, or None when it is not usable.

    Rejects bools (``True`` is an int in Python and would silently mean "1
    second"), non-numerics and anything <= 0. None means "this source of the
    value said nothing usable" — the caller falls through to the next one.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0.0:  # NaN or non-positive
        return None
    return value


def _resolve_bound(
    *,
    source_id: str,
    config_value: Any,
    handler_value: Any,
    default: float,
    ceiling: float,
    label: str,
) -> float:
    """Resolve ONE per-source poll bound: descriptor → handler → default.

    Clamped to ``ceiling`` (never below the resolved value's own intent — a
    source is free to ask for LESS than the default, only more is capped). A
    present-but-unusable value logs and falls through to the next source
    rather than failing the poll: a bad tuning knob must degrade to the proven
    default, never take a source offline.
    """
    for origin, raw in (
        ("descriptor.config", _unwrap_factory_value(config_value)),
        ("handler", handler_value),
    ):
        if raw is None:
            continue
        value = _positive_number(raw)
        if value is None:
            logger.warning(
                "source_actor.poll_bound.invalid source=%s bound=%s origin=%s "
                "value=%r — ignoring (falling through to the default)",
                source_id, label, origin, raw,
            )
            continue
        if value > ceiling:
            logger.warning(
                "source_actor.poll_bound.clamped source=%s bound=%s origin=%s "
                "requested=%s ceiling=%s",
                source_id, label, origin, value, ceiling,
            )
            return ceiling
        return value
    return default


def resolve_poll_bounds(
    descriptor: SourceDescriptor, handler: Any,
) -> tuple[float, int]:
    """Per-source ``(wall_clock_budget_s, max_entries)`` for ONE pull (R5).

    Descriptor config wins over the handler's advertisement, which wins over
    the module defaults; both are clamped to the module ceilings. A source
    that declares neither gets exactly the historical bounds — this is a
    widening, never a change, for every kind but the ones that opt in.
    """
    cfg = descriptor.config or {}
    source_id = descriptor.identity.id

    def _handler_attr(name: str) -> Any:
        try:
            return getattr(handler, name, None)
        except Exception:  # pragma: no cover — a handler property that raises
            return None

    budget = _resolve_bound(
        source_id=source_id,
        config_value=cfg.get(_POLL_BUDGET_CONFIG_KEY),
        handler_value=_handler_attr(_POLL_BUDGET_CONFIG_KEY),
        default=_POLL_BUDGET_S,
        ceiling=_MAX_POLL_BUDGET_S,
        label=_POLL_BUDGET_CONFIG_KEY,
    )
    entries = _resolve_bound(
        source_id=source_id,
        config_value=cfg.get(_MAX_ENTRIES_CONFIG_KEY),
        handler_value=_handler_attr(_MAX_ENTRIES_CONFIG_KEY),
        default=float(_MAX_ENTRIES_PER_POLL),
        ceiling=float(_MAX_ENTRIES_CEILING),
        label=_MAX_ENTRIES_CONFIG_KEY,
    )
    return float(budget), max(1, int(entries))


#: B0-11 Fix-1 / The cursor policy (future clamp, look-back,
#: judged-entry ledger) lives in :mod:`.source_cursor`; these names stay
#: importable from here for existing callers.
_MAX_FUTURE_SKEW = CURSOR_FUTURE_TOLERANCE
_entry_logical_ts = entry_logical_ts


class _RawConfig(BaseModel):
    """Open BaseModel satisfying ``SourceContext.config`` (typed BaseModel).

    The source-first handlers read their config from the descriptor via the
    source factory, not from ``ctx.config``; this open model carries the raw
    descriptor config so the (frozen) SourceContext field is satisfied
    without a per-kind pydantic parse at the actor layer.
    """

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Dependency bundle + process-global registry (self-contained; mirrors the
# target pattern but does NOT touch dapr_actors._TARGET_DEPS).
# ---------------------------------------------------------------------------


class SourceDeps:
    """Constructor-time dependencies for a :class:`SourceCore` / actor.

    Plain object (not pydantic) so it can carry the asyncpg pool + callables
    without serialization fuss. Built by the host from a SourceDescriptor.
    """

    __slots__ = (
        "descriptor",
        "deps",
        "handler",
        "extractors",
        "upstream_client",
        "subscriptions_provider",
        "enrichment_stage",
    )

    def __init__(
        self,
        *,
        descriptor: SourceDescriptor,
        deps: StandardDeps,
        handler: Any | None = None,
        extractors: dict[str, MediaExtractor] | None = None,
        upstream_client: UpstreamClient | None = None,
        subscriptions_provider: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        enrichment_stage: Callable[[Any, Any], Awaitable[Any]] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.deps = deps
        # The baseline NLP enrichment chain (descriptor.pipeline.enrichment:
        # language_detect / geocode / ner_multilingual / classify), built +
        # StackRef-configured by the host (it owns the nlp/qdrant/embedding
        # clients). None → only tier-1 structured enrichment runs.
        self.enrichment_stage = enrichment_stage
        # A pre-built handler (push handlers are stateful + router-bound; the
        # host constructs one and keeps it). Poll handlers can be rebuilt per
        # pull from the factory, but keeping one is fine + cheaper.
        self.handler = handler
        self.extractors = extractors
        self.upstream_client = upstream_client
        # Returns the source's active authorized subscriptions (for the
        # subscriber-driven watchlist, §4.2.1). None → static watch set only.
        self.subscriptions_provider = subscriptions_provider


_SOURCE_DEPS: dict[str, SourceDeps] = {}
_SOURCE_DEPS_RESOLVER: Callable[[str], Awaitable[SourceDeps | None]] | None = None


def register_source_deps(actor_id: str, deps: SourceDeps) -> None:
    """Host hook: pre-register a source's deps in the process registry."""
    _SOURCE_DEPS[actor_id] = deps


def register_source_deps_resolver(
    resolver: Callable[[str], Awaitable[SourceDeps | None]],
) -> None:
    """Host hook: register the async fallback resolver (registry fetch)."""
    global _SOURCE_DEPS_RESOLVER
    _SOURCE_DEPS_RESOLVER = resolver


def clear_source_deps() -> None:
    """Test hook: drop the process-global source-deps registry."""
    _SOURCE_DEPS.clear()


async def resolve_source_deps(actor_id: str) -> SourceDeps | None:
    cached = _SOURCE_DEPS.get(actor_id)
    if cached is not None:
        return cached
    if _SOURCE_DEPS_RESOLVER is not None:
        resolved = await _SOURCE_DEPS_RESOLVER(actor_id)
        if resolved is not None:
            _SOURCE_DEPS[actor_id] = resolved
        return resolved
    return None


# ---------------------------------------------------------------------------
# SourceCore — the testable mechanism
# ---------------------------------------------------------------------------


class SourceCore:
    """Acquisition logic for one source, independent of the Dapr Actor base.

    The :class:`SourceActor` delegates to an instance of this; tests drive it
    directly against the dev rig (no sidecar). Holds no Dapr state itself —
    cursor + provisioning state live in a crash-safe :class:`FilterStateStore`
    (the Postgres ``actor_filter_state`` table).
    """

    def __init__(self, actor_id: str, sd: SourceDeps) -> None:
        self.actor_id = actor_id
        self.sd = sd
        self.descriptor = sd.descriptor
        self.deps = sd.deps
        self._extractors = sd.extractors or default_extractor_registry()
        # A-2 / D2 SEAM GUARD: ``media: "eager"`` REFUSES ACTIVATION unless a
        # real media-modality extractor is wired (the in-tree default registry
        # ships none — the former echo-caption stub is gone). Refusing here —
        # typed error, structured log, before any pull — beats refusing
        # per-signal: an eager source without real extraction must never run.
        if getattr(self.descriptor.pipeline, "media", "reference") == "eager":
            if not any(m in self._extractors for m in MEDIA_MODALITIES):
                logger.error(
                    "source_actor.eager_media.refused actor_id=%s source_id=%s "
                    "reason=no_real_extractor seam=eager-extraction "
                    "env=LEGBA_MEDIA_API_URL",
                    actor_id, self.descriptor.identity.id,
                )
                raise MediaEndpointNotConfiguredError(
                    f"source {self.descriptor.identity.id!r} declares media="
                    "'eager' but no real media extractor is wired — eager "
                    "extraction is a declared seam (configure "
                    "LEGBA_MEDIA_API_URL and register hosted extractors); "
                    "refusing activation"
                )
        # Source-side ingest dedupe (P-02, tiers 1+2) — built from the
        # descriptor's ``pipeline.ingestion_filters`` (the ``dedupe_tier_1`` /
        # ``dedupe_tier_2`` stages). Alias/canonical model: a raw row that
        # matches an existing one by canonical-URL (tier 1) or content hash
        # (tier 2) is KEPT and linked to the matched row's canonical via
        # signal_aliases + signals.canonical_signal_id — never collapsed. None
        # when the source declares no ingest dedupe tier (the periodic
        # cross_source_dedup analyst then owns dedup for that source).
        self._ingest_dedupe: IngestDedupe | None = ingest_dedupe_from_stages(
            getattr(self.descriptor.pipeline, "ingestion_filters", []) or [],
            produced_by=f"ingest_dedupe:{self.descriptor.identity.id}",
            owner_tenant=self.descriptor.scope.owner_tenant,
        )
        # W3 SRC-3: the ``keyword_gate`` ingestion stages, applied PRE-baseline.
        self._ingest_gate = SourceIngestGate(getattr(self.descriptor.pipeline, "ingestion_filters", []) or [])
        self._families = EMPTY_INDEX  # W10 TAX-2: refreshed per pull/emit (tags.tag_vocabulary_index)
        # N-L: records land through the sink registry, keyed by the declared
        # family (descriptor `sink.family`, default `document`).
        self._sinks = SinkSet(self, getattr(getattr(self.descriptor, "sink", None), "family", "document"))
        self._signal_sink = SignalSink(self)

    # -- context + state ---------------------------------------------------

    def _state_store(self) -> FilterStateStore:
        return FilterStateStore(
            self.deps.pg_pool,
            actor_id=self.actor_id,
            filter_id=f"source:{self.descriptor.identity.id}",
        )

    def _make_context(self) -> SourceContext:
        # The Signal is target-agnostic now (source-owned); SourceContext
        # still carries the legacy target_id/target_version fields (frozen
        # contract). We pass the source identity into both so handlers that
        # read ctx.source_id work, and the legacy fields are harmless (the
        # new Signal ignores them).
        ident = self.descriptor.identity
        return SourceContext(
            target_id=ident.id,
            target_version=ident.version,
            source_id=ident.id,
            config=_RawConfig(**(self.descriptor.config or {})),
            state_store=self._state_store(),
            secrets_resolve=self.deps.secrets_resolve,
            scope_geo=list(self.descriptor.scope.geo),
            scope_languages=list(self.descriptor.scope.languages),
            classification=bind_declared(self.descriptor.scope, families=getattr(self, "_families", EMPTY_INDEX)),
        )

    def _build_handler(self) -> Any:
        if self.sd.handler is not None:
            return self.sd.handler
        return build_source_handler(
            self.descriptor.identity.kind,
            self.descriptor.config,
            secrets_resolve=self.deps.secrets_resolve,
        )

    async def activation_check(self) -> None:
        """Run the handler's declared activation guard, if its kind has one.

        A kind whose handler class defines ``activation_check(ctx)`` (today
        only ``email_imap`` — SEAMS #63, no mailbox credential) refuses
        ACTIVATION here, loud and typed, before the actor goes ACTIVE or
        registers its poll reminder; every other kind is untouched (no handler
        is built for them on this path)."""
        if self.sd.handler is not None:
            cls: Any = type(self.sd.handler)
        else:
            cls = discover_source_kinds().get(self.descriptor.identity.kind)
        if getattr(cls, "activation_check", None) is None:
            return
        try:
            await self._build_handler().activation_check(self._make_context())
        except Exception as exc:
            logger.error(
                "source_actor.activation.refused actor_id=%s source_id=%s reason=%s err=%s",
                self.actor_id, self.descriptor.identity.id,
                getattr(exc, "reason", type(exc).__name__), exc,
            )
            raise

    def _upstream_client(self) -> UpstreamClient | None:
        if self.sd.upstream_client is not None:
            return self.sd.upstream_client
        # No client wired + provisioning enabled → the host must supply one.
        return None

    # -- baseline + write + publish ---------------------------------------

    async def _process_one(
        self, conn: asyncpg.Connection, ctx: SourceContext, raw: Signal,
        *, dedup_stats: dict[str, int] | None = None, hold: EnrichHold | None = None,
    ) -> "Signal | None":
        """Baseline → write ONE canonical signal. Returns the written signal
        (so the caller can publish it per-signal), or None if the baseline
        dropped it or the write was a dedup/conflict no-op. ``dedup_stats``, when
        supplied, is threaded to :func:`write_canonical_signal` so the poll path
        can count S-4 intra-source collapses (a collapse is a productive poll).
        ``hold`` (poll path only, W11-F103): a transient enrichment failure is
        recorded on it so the cursor holds before this entry, and its bypass
        reason skips the enrichment chain (written unenriched)."""
        if not await self._ingest_gate.admit(raw, ctx):  # keyword_gate: drop pre-enrichment
            return None
        # C-2: a claim row is stamped and its text cleaned BEFORE enrichment
        # (claim_hygiene); every other row: one dict lookup, untouched.
        tally(dedup_stats, prepare_claim_row(self.descriptor, raw))
        bypass = hold.bypass_reason() if hold is not None else None
        try:
            enriched = await asyncio.wait_for(
                run_baseline(
                    raw, ctx, media=self.descriptor.pipeline.media,
                    extractors=self._extractors,
                    enrichment_stage=None if bypass else self.sd.enrichment_stage,
                ),
                timeout=_ENRICH_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, NlpServiceUnavailable) as exc:
            if hold is None and isinstance(exc, NlpServiceUnavailable):
                raise  # push path: unchanged (no cursor to hold)
            # A single hung extractor/NLP call must not block the actor turn.
            # Skip this entry; the poll's cursor holds before it so the next
            # poll re-pulls it, and the rest of this poll bypasses the NLP chain.
            ctx.logger.warning(
                "source_actor.enrich.timeout actor_id=%s after=%.0fs err=%s",
                self.actor_id, _ENRICH_TIMEOUT_S, type(exc).__name__,
            )
            if hold is not None:
                hold.skip("timeout" if isinstance(exc, asyncio.TimeoutError) else "nlp_unavailable")
            return None
        if enriched is None:
            return None
        if bypass and hold is not None:
            hold.mark_unenriched(enriched, bypass)
        # N-L: tenant/licence stamps, the write, the S-4 collapse and the
        # ingest-dedupe alias link are the document sink (runtime/sinks/signal.py).
        return await self._signal_sink.write(conn, ctx, enriched, dedup_stats=dedup_stats)

    async def _publish(self, signals: list[Signal]) -> None:
        """Fan out written signals — the document sink's publish (sinks/signal.py)."""
        await self._signal_sink.publish(signals)

    # -- discovery path ----------------------------------------------------

    async def run_discovery_cycle(self) -> dict[str, Any]:
        """Delegate: one SOURCE-discovery cycle (P-13). Mechanism, contract
        and rationale live in :mod:`.source_discovery_dispatch` — extracted at
        the regrowth-gate seam. Errors propagate to :meth:`SourceActor.run`.
        """
        from .source_discovery_dispatch import run_discovery_cycle

        return await run_discovery_cycle(self)

    # -- poll path ---------------------------------------------------------

    async def pull_once(self) -> dict[str, Any]:
        """Pull the source, baseline + write each signal, publish. (poll path)

        Idempotent on restart via the persisted cursor (``since``). Returns a
        run summary.
        """
        self._families = await tag_vocabulary_index()
        ctx = self._make_context()
        handler = self._build_handler()
        self._ingest_gate.observe_handler(handler)  # the RSS keyword_* sugar's gate

        cursor_raw = await ctx.state_store.get("cursor") or {}
        # W11-F151 (mechanism: .source_cursor): a stored cursor later than now
        # is poison and healed on read; a dated-feed handler is handed
        # ``since - lookback`` and the ledger skips entries already judged.
        ledger = LookbackLedger(cursor_raw, lookback_of(handler))
        since = read_since(
            cursor_raw, source=self.descriptor.identity.id, dated=ledger.dated,
        )

        # B — bulk high-water-mark resume. For a dataset-streaming source the
        # ``since`` cursor can't paginate within one snapshot (all rows share a
        # coarse logical timestamp), so we carry a separate offset of rows
        # already traversed and hand it to the handler via the shared state
        # store; the handler skips that many rows and resumes mid-snapshot.
        bulk_field = _bulk_resume_field(self.descriptor)
        prior_offset = 0
        if bulk_field is not None:
            prior_offset = int(
                cursor_raw.get("bulk_offset", 0)
                if isinstance(cursor_raw, dict) else 0
            )
            # Publish the resume point for the handler to consume this pull, and
            # clear any stale traversal report from a prior pull.
            await ctx.state_store.set(BULK_RESUME_OFFSET_KEY, prior_offset)
            await ctx.state_store.set(BULK_TRAVERSED_KEY, None)

        written: list[Signal] = []
        # S-4: count intra-source exact-hash collapses this poll. A poll that
        # wrote 0 rows but collapsed >=1 duplicate DID get real content back from
        # the source, so it must NOT be recorded as a non-productive 'empty' poll
        # (which the liveness watchdog counts toward an empty-streak
        # degradation). B-1: the count is now RECORDED on the outcome row as
        # ``reserve_unchanged`` rather than only steering the rollup word — a
        # source whose every poll is unchanged re-serves is alive but frozen, and
        # that is the distinction the ledger could not express on 08-03.
        dedup_stats: dict[str, int] = {}
        errored: str | None = None
        capped = False
        # Logical position of the LAST entry we actually consumed this pull —
        # used to advance the cursor to where we STOPPED (not NOW) on a capped
        # pull, so a backlog is resumed rather than skipped (Fix A).
        last_processed_ts: datetime | None = None
        hold = EnrichHold(cursor_raw)  # Transient skips hold the cursor
        age_cap = FirstPollAgeCap(handler, first_poll=since is None)  # W11-F265
        order = WalkOrder()  # a capped newest-first walk holds the cursor
        # R5 — per-source bounds (descriptor config > handler advertisement >
        # the module defaults), each clamped to the module ceiling. Resolved
        # ONCE per poll, before the walk, so the whole pull is governed by one
        # pair of numbers that the log line below can report.
        poll_budget_s, max_entries = resolve_poll_bounds(self.descriptor, handler)
        deadline = time.monotonic() + poll_budget_s
        # N-L: fetch receipts (what this poll received, linked to each record)
        # and the non-document sinks — runtime/source_poll_records.py.
        receipts = ReceiptRecorder(source_id=self.descriptor.identity.id)
        posture = protocol_posture(self.descriptor, cursor_raw)  # fetch.http_version
        sink_counts: dict[str, Any] = {}
        sink_n = 0
        try:
            async with self.deps.pg_pool.acquire() as conn:
                count = 0
                async for raw in captured(handler.pull(ctx, ledger.handler_since(since)), receipts.log, posture):
                    link = receipts.link_for(raw, handler)
                    if not isinstance(raw, Signal):  # a typed record of another family
                        if sink_n >= _MAX_SINK_RECORDS_PER_POLL or time.monotonic() > deadline:
                            capped = True
                            break
                        sink_n += 1
                        route_record(self, raw, link, receipts)
                        continue
                    # A dated entry the look-back re-yields and the ledger has
                    # already judged costs nothing: not enriched, not counted.
                    ts = feed_ts(raw) if ledger.dated else entry_logical_ts(raw)
                    if ledger.judged(raw) or age_cap.too_old(raw):  # F265: counted, not written
                        continue
                    order.see(ts)
                    # Bound the poll by count AND wall time so it always finishes
                    # within Dapr's drain window and reaches the cursor-advance
                    # below — never trapped re-grinding a backlog (2026-06-08).
                    if count >= max_entries or time.monotonic() > deadline:
                        capped = True
                        break
                    count += 1
                    # Track the logical timestamp of every entry we CONSUME
                    # (not just the ones that survive dedup/baseline) — a capped
                    # pull made real forward progress through the backlog even
                    # when an entry was dropped, and must not re-grind it. An
                    # undated entry of a dated feed advances nothing (F151).
                    if ts is not None and (last_processed_ts is None or ts > last_processed_ts):
                        last_processed_ts = ts
                    hold.at(raw, ts or entry_logical_ts(raw), count - 1)
                    skipped_before = hold.skipped
                    sig = await self._process_one(
                        conn, ctx, raw, dedup_stats=dedup_stats, hold=hold,
                    )
                    if hold.skipped == skipped_before:  # judged (not a transient skip)
                        ledger.mark(raw)
                    if sig is not None:
                        written.append(sig)
                        # Publish immediately — fan-out then survives a later
                        # cap/error/drain instead of being lost at the end.
                        await self._publish([sig])
                        await receipts.link(conn, "signal", sig.signal_id, link)
        except asyncio.CancelledError:
            # A poll cut mid-walk (a roll's bounded drain cancels the
            # turns still running at LEGBA_RUNTIME_DRAIN_SECONDS) did NOT reach
            # the end of its walk: record it as capped so the cursor policy
            # holds where a capped walk holds (a newest-first walk keeps its
            # prior position) instead of advancing as if the walk completed.
            # The finally below still persists the cursor and the outcome row;
            # the cancellation then propagates.
            capped = True
            raise
        except Exception as exc:
            errored = str(exc)
            logger.warning(
                "source_actor.pull.error actor_id=%s err=%s", self.actor_id, exc,
            )
        finally:
            # P-5: items the handler itself dropped as too old (json_api's
            # lookback window) join the F265 count — poll row, summary, log.
            age_cap.absorb_handler_skips(handler)
            # Advance the cursor along REAL forward progress only — never to a
            # bare wall-clock NOW on a zero-yield pull. Cursor-advance policy:
            #   * hard fetch error  -> keep the prior position (retry the window).
            #   * >=1 entry processed (capped OR complete) -> advance to the
            #     LAST PROCESSED entry's logical timestamp, NOT NOW. NOW would
            #     skip the unprocessed backlog past the cap; the last-processed
            #     timestamp keeps the forward-progress anti-trap WITHOUT skipping
            #     live entries (the observed skipping behaviour).
            #   * ZERO entries processed (a genuinely empty/at-head/all-filtered
            #     pull) -> LEAVE THE CURSOR UNCHANGED (keep ``since``). The
            #     prior policy advanced ``since`` to NOW here, but a feed that
            #     misses every item one cycle (e.g. a transient parse/date bug
            #     that drops the whole window, or a Cloudflare 304 pin) then has
            #     its ``since`` march irreversibly forward and can NEVER catch up
            #     — a permanent silent stall (Fix B). Holding the cursor lets the
            #     next healthy pull re-see the same window; content-hash dedup
            #     downstream makes the re-emit on recovery a harmless no-op, so
            #     holding cannot cause a dup-storm.
            # First-ever pull (no prior cursor, since is None): there is no
            # window to protect, so a zero-yield pull seeds the cursor at NOW to
            # avoid re-walking from epoch on the next cycle.
            # A TRANSIENT skip (enrichment timeout) holds the advance
            # at max(since, min(last processed, earliest skipped - 1µs)) — see
            # source_enrich_hold; permanent drops still count as processed.
            # Never later than now; a dated handler's cursor never
            # moves backwards, and a capped walk that was not oldest-first
            # holds it (see source_cursor.advance / WalkOrder).
            cursor_ts = advance_cursor(
                errored=errored is not None, since=since,
                last_processed_ts=last_processed_ts, hold=hold, dated=ledger.dated,
                capped=capped, walk_ascending=order.ascending,
            )
            new_cursor = {
                "source_id": self.descriptor.identity.id,
                "last_pulled_at": cursor_ts.isoformat(),
                "rows_pulled": int(cursor_raw.get("rows_pulled", 0) if isinstance(cursor_raw, dict) else 0)
                + len(written),
                "last_error": errored,
                **self._ingest_gate.cursor_fields(cursor_raw),
                **enrichment_cursor_fields(self.descriptor.identity.id, cursor_raw),  # W5 SRC-4
                **hold.cursor_fields(errored=errored is not None),  # W11-F103
                **ledger.cursor_fields(cursor_ts),  # W11-F151
                **({"fetch_protocol_memory": posture.memory} if posture.memory else {}),
            }
            sink_counts = await finish_poll(self, receipts)
            dedup_stats["enrichment_skipped"] = hold.skipped
            dedup_stats["enrichment_bypassed"] = hold.bypassed
            # B — persist the bulk high-water mark. The advance DELTA is the
            # actor's OWN processed-count (``count``) — boundary-safe vs. the
            # handler's per-yield position across the cap suspension point —
            # while ``reached_end`` (did the handler drain the whole snapshot?)
            # comes from the handler's report when present, else is inferred
            # from whether WE capped. A full walk resets the offset to 0; a
            # mid-stream stop advances it. On a hard error keep the prior offset
            # to re-attempt the same window.
            if bulk_field is not None:
                bulk_offset = prior_offset
                if errored is None:
                    reached_end = await self._read_bulk_reached_end(
                        ctx, capped=capped,
                    )
                    rows, reached_end = hold.bulk_rows(count, reached_end)
                    bulk_offset = bulk_highwater_advance(
                        prior_offset, rows, reached_end=reached_end,
                    )
                new_cursor["bulk_offset"] = bulk_offset
            try:
                await ctx.state_store.set("cursor", new_cursor)
            except Exception:  # cursor persist must not mask the pull result
                logger.warning(
                    "source_actor.cursor.persist_failed actor_id=%s",
                    self.actor_id, exc_info=True,
                )
            # DQ-H5b (#88) + migration 0114 — record a provenance row for
            # EVERY poll so the cadence watchdog, entity_gc's auto-pause latch
            # and the UI can read what this poll actually did.
            #
            # The original code recorded only NON-productive polls, on the
            # premise that a productive one is self-evidencing via its signals
            # rows. That premise breaks every reader that walks a RUN of rows:
            # an absence cannot break a run, so a repaired source kept
            # presenting its old 'error' rows as the leading run and entity_gc
            # op-4 re-paused it while it was actively ingesting. A recorded
            # 'success' breaks the run at the first productive poll.
            #
            # S-4: a poll that wrote 0 rows but COLLAPSED >=1 intra-source
            # duplicate is ALSO productive (the source answered with content) —
            # it records 'success' with signals_written=0, never 'empty', so a
            # hazard feed healthily re-serving active events still cannot trip
            # the watchdog's empty-streak degradation. B-1 keeps that rollup
            # UNCHANGED and adds the count itself to the row, so a reader can
            # now tell a healthy hazard re-serve from a frozen feed without
            # re-deriving it from the signals table.
            await self._record_poll_outcome(
                ctx, handler,
                errored=errored,
                capped=capped,
                signals_written=len(written),
                reserve_unchanged=int(dedup_stats.get("reserve_unchanged") or 0),
                sink_counts=sink_counts,
                fetch_protocol=posture.outcome() if posture.eventful() else None,
                age_skipped=age_cap.outcome_count(),
                claim_hygiene=poll_claim_hygiene(dedup_stats),
            )

        if written or capped or errored or hold.skipped or sink_counts or age_cap.skipped:  # N-R: + observation-only polls
            logger.info(
                "source_actor.pull.done actor_id=%s written=%d capped=%s err=%s "
                "bulk_offset=%s budget_s=%.0f max_entries=%d enrich_skipped=%d "
                "enrich_bypassed=%d sinks=%s first_poll_age_skipped=%d",
                self.actor_id, len(written), capped, errored,
                new_cursor.get("bulk_offset"), poll_budget_s, max_entries,
                hold.skipped, hold.bypassed, _sink_summary(sink_counts), age_cap.skipped,
            )
        sink_written = sum(int(c.get("written", 0)) for c in sink_counts.values())
        return {
            **({"sink_counts": sink_counts} if sink_counts else {}),
            "outcome": "success" if written or sink_written else ("hard_fail" if errored else "noop"),
            "signals_written": len(written),
            "signal_ids": [str(s.signal_id) for s in written],
            "error": errored,
            "keyword_filtered": self._ingest_gate.last_poll_dropped,
            "enrichment_skipped": dedup_stats.get("enrichment_skipped", 0),
            "enrichment_bypassed": dedup_stats.get("enrichment_bypassed", 0),
            **({"first_poll_age_skipped": age_cap.skipped} if age_cap.skipped else {}),  # W11-F265
        }

    async def _record_poll_outcome(
        self,
        ctx: SourceContext,
        handler: Any,
        *,
        errored: str | None,
        capped: bool,
        signals_written: int = 0,
        reserve_unchanged: int = 0,
        sink_counts: dict[str, Any] | None = None,
        fetch_protocol: dict[str, Any] | None = None,
        age_skipped: int | None = None,
        claim_hygiene: dict[str, int] | None = None,
    ) -> None:
        """Persist this poll's provenance row (DQ-H5b #88; success — mig 0114).

        Best-effort: a failure here must NEVER mask the pull result. Reads the
        handler's own freshly-recorded health (``state`` + ``last_error``) when
        the handler exposes a ``health_state_key`` — that record is the ONLY
        place a handler-SWALLOWED 4xx / parse-fail / timeout is observable from
        the poll path (no exception escapes ``handler.pull`` for those cases),
        so it is what turns a bare "empty" into an honest
        "error / unhealthy — <reason>".

        Outcome precedence (PRODUCTIVITY WINS over the failure signals):

          * ``signals_written > 0`` or ``reserve_unchanged > 0`` → ``'success'``.
            A poll that produced is not a failing poll, whatever else it reported —
            counting it as an error is exactly how a repaired source got
            re-paused off its own recovery. Any ``errored`` / degraded health
            from a PARTIAL failure is still carried on the row (``error`` /
            ``health_state``) rather than dropped, so the detail survives
            without poisoning the run every reader walks.
          * else an escaped exception / degraded / unhealthy → ``'error'``.
          * else the keyword gate dropped >=1 entry → ``'gated'`` (0226: the
            feed delivered, all of it off-topic).
          * else → ``'empty'`` (unchanged: polled fine, nothing new).
        """
        health_state: str | None = None
        health_error: str | None = None
        newest_entry_ts: datetime | None = None
        # Only trust the handler health record when the pull ran to a NATURAL
        # conclusion. A capped pull can leave a STALE prior-pull health record
        # (we broke the async-for before the handler wrote this pull's health),
        # so we don't read it then — capped+0-written is recorded as plain
        # 'empty' with capped=True, which already says "made progress" (a
        # capped poll that DID write is 'success' on its own count, and needs
        # no health record to say so).
        if not capped:
            hkey = getattr(handler, "health_state_key", None)
            if hkey:
                try:
                    rec = await ctx.state_store.get(hkey)
                except Exception:
                    rec = None
                if isinstance(rec, dict):
                    state = rec.get("state")
                    if isinstance(state, str) and state:
                        health_state = state
                    last_err = rec.get("last_error")
                    if isinstance(last_err, str) and last_err:
                        health_error = last_err
                    # B0-12: the handler's newest-observed-entry timestamp
                    # (rss records it in detail on every parsed 200 / carries
                    # it across 304s) — the watchdog's quiet-vs-cursor-fault
                    # discriminator evidence. Defensive parse: bad/absent
                    # values degrade to NULL (→ the watchdog's pre-existing
                    # no-evidence behavior).
                    detail = rec.get("detail")
                    raw_ts = (
                        detail.get("newest_entry_ts")
                        if isinstance(detail, dict)
                        else None
                    )
                    if isinstance(raw_ts, str) and raw_ts:
                        try:
                            parsed = datetime.fromisoformat(raw_ts)
                            newest_entry_ts = (
                                parsed
                                if parsed.tzinfo
                                else parsed.replace(tzinfo=timezone.utc)
                            )
                        except ValueError:
                            newest_entry_ts = None

        written_n = max(0, int(signals_written))
        reserve_n = max(0, int(reserve_unchanged))
        gate_n = self._ingest_gate.last_poll_dropped  # None = not gated
        # N-L: a non-document sink's writes/unchanged count as this poll's
        # production for the rollup (the columns keep their signal meaning).
        sink_w = sum(int(c.get("written", 0)) for c in (sink_counts or {}).values())
        sink_u = sum(int(c.get("unchanged", 0)) for c in (sink_counts or {}).values())
        outcome = classify_poll_outcome(
            signals_written=written_n + sink_w, reserve_unchanged=reserve_n + sink_u,
            errored=errored, health_state=health_state, keyword_filtered=gate_n,
        )

        try:
            async with self.deps.pg_pool.acquire() as oc:
                await write_poll_outcome(
                    oc,
                    source_id=self.descriptor.identity.id,
                    source_version=self.descriptor.identity.version,
                    owner_tenant=self.descriptor.scope.owner_tenant,
                    outcome=outcome,
                    health_state=health_state,
                    capped=capped,
                    signals_written=written_n,
                    error=errored or health_error,
                    newest_entry_ts=newest_entry_ts,
                    reserve_unchanged=reserve_n,
                    keyword_filtered=gate_n,
                    sink_counts=sink_counts or None,
                    fetch_protocol=fetch_protocol,
                    age_skipped=age_skipped,
                    claim_hygiene=claim_hygiene,
                )
        except Exception:  # provenance write must never mask the pull result
            logger.warning(
                "source_actor.poll_outcome.persist_failed actor_id=%s",
                self.actor_id, exc_info=True,
            )

    async def _read_bulk_reached_end(
        self, ctx: SourceContext, *, capped: bool,
    ) -> bool:
        """Decide whether the bulk snapshot was FULLY walked this pull (B).

        ``reached_end`` drives the high-water reset: a full walk resets the
        offset to 0 (re-walk the refreshed snapshot next pull); a mid-stream
        stop keeps advancing it. Truth sources, in order:

          * If WE capped (hit the resolved entry cap / wall-clock budget —
            see :func:`resolve_poll_bounds`), it is NEVER end-of-stream — we
            stopped the handler mid-snapshot. The handler's post-loop report
            didn't even run.
          * Otherwise the handler returned on its own; its report
            (:data:`BULK_TRAVERSED_KEY`) distinguishes a true end-of-stream
            (``reached_end=True``) from the handler's OWN ``max_bulk_rows`` cap
            (``reached_end=False``). Absent a report we treat the uncapped
            return as a full walk (the common case — the handler drained).
        """
        if capped:
            return False
        report = await ctx.state_store.get(BULK_TRAVERSED_KEY)
        if isinstance(report, dict) and "reached_end" in report:
            return bool(report.get("reached_end"))
        return True

    # -- push path ---------------------------------------------------------

    def make_emit_callback(self) -> Callable[[Signal], Awaitable[None]]:
        """Build the ``emit_signal`` callback handed to a push handler.

        On each inbound event the handler calls this with one raw Signal; the
        callback runs the same baseline → write → publish path the poll branch
        uses. Each call is its own short transaction (a webhook POST is one
        event, not a batch).
        """
        async def _emit(raw: Any) -> None:
            if not isinstance(raw, Signal):  # N-L: a pushed record of another family
                await emit_record(self, raw)
                return
            self._families = await tag_vocabulary_index()
            ctx = self._make_context()
            async with self.deps.pg_pool.acquire() as conn:
                sig = await self._process_one(conn, ctx, raw)
            if sig is not None:
                await self._publish([sig])
                await link_inbound(self, "signal", sig.signal_id)  # N-L push receipt

        return _emit

    # -- provisioning (§4.2.1) --------------------------------------------

    async def reconcile_upstream(self) -> dict[str, Any]:
        """Reconcile the upstream watch set from active subscriptions. Idempotent."""
        prov = self.descriptor.provision
        if prov is None or not prov.enabled:
            return {"provisioned": False, "reason": "no_provision_block"}
        client = self._upstream_client()
        if client is None:
            logger.warning(
                "source_actor.provision.no_client actor_id=%s — provisioning "
                "declared but no UpstreamClient wired",
                self.actor_id,
            )
            return {"provisioned": False, "reason": "no_upstream_client"}
        subs: list[dict[str, Any]] = []
        if self.sd.subscriptions_provider is not None:
            subs = await self.sd.subscriptions_provider()
        static_params = list((prov.register_call or {}).get("static_watch_params", []))
        desired = desired_watch_set(prov, subscriptions=subs, static_params=static_params)
        ctx = self._make_context()
        result = await reconcile_provision(ctx, prov, desired=desired, client=client)
        return {
            "provisioned": True,
            "added": result.added,
            "removed": result.removed,
            "registered": result.registered,
            "pending": result.pending,
            "converged": result.converged,
        }

    async def deprovision_upstream(self) -> dict[str, Any]:
        prov = self.descriptor.provision
        if prov is None or not prov.enabled:
            return {"deprovisioned": False, "reason": "no_provision_block"}
        client = self._upstream_client()
        if client is None:
            return {"deprovisioned": False, "reason": "no_upstream_client"}
        ctx = self._make_context()
        result = await deprovision_all(ctx, prov, client=client)
        return {
            "deprovisioned": True,
            "removed": result.removed,
            "pending_removals": result.pending_removals,
            "converged": result.converged,
        }

    async def health(self) -> SourceHealth:
        ctx = self._make_context()
        handler = self._build_handler()
        try:
            return await handler.health_check(ctx)
        except Exception as exc:  # pragma: no cover - defensive
            return SourceHealth(state="unhealthy", last_error=str(exc))


# ---------------------------------------------------------------------------
# Dapr SourceActor wrapper
# ---------------------------------------------------------------------------

try:
    from dapr.actor import Actor, ActorInterface, Remindable, actormethod
    from datetime import timedelta

    from .dapr_cron import cron_to_reminder_timing

    _DAPR_AVAILABLE = True
except Exception:  # pragma: no cover - dapr optional in some envs
    _DAPR_AVAILABLE = False


if _DAPR_AVAILABLE:

    class SourceActorInterface(ActorInterface):
        """Wire-typed surface for SourceActor (ActorProxy clients)."""

        @actormethod(name="activate")
        async def activate(self) -> dict: ...

        @actormethod(name="pause")
        async def pause(self) -> dict: ...

        @actormethod(name="resume")
        async def resume(self) -> dict: ...

        @actormethod(name="retire")
        async def retire(self) -> dict: ...

        @actormethod(name="run")
        async def run(self, payload: dict) -> dict: ...

        @actormethod(name="reconcile")
        async def reconcile(self) -> dict: ...

        @actormethod(name="get_state")
        async def get_state(self) -> dict: ...

    class SourceActor(Actor, SourceActorInterface, Remindable):
        """Dapr-native SourceActor. Delegates acquisition to :class:`SourceCore`.

        Reminder: a poll source's ``cadence.schedule`` becomes one durable
        Dapr Reminder ``poll_<source_id>``; ``receive_reminder`` dispatches
        to ``run``. Push sources register no reminder — they wake via the
        inbound-webhook router (the host binds the handler's emit callback to
        :meth:`SourceCore.make_emit_callback` at deps-build time).
        """

        async def _core(self) -> SourceCore | None:
            # S-6: `resolve_source_deps` falls through to a registry fetch on a
            # cache miss, so it is the SourceActor's copy of the 08-01 hang
            # point. Bounded, and a timeout rejoins the existing `sd is None`
            # branch — the turn ends, the actor is not activated this pass, and
            # the reconciler's ENSURE_ACTIVE retries next resync. The
            # alternative is a held turn, which for a source means its poll
            # reminder and every fire behind it queue forever.
            sd = await bounded_turn_op_or(
                resolve_source_deps(self.id.id), None,
                op="source.resolve_deps", actor_id=self.id.id,
            )
            if sd is None:
                logger.warning(
                    "source_actor.no_deps actor_id=%s (host did not register "
                    "deps and resolver did not resolve, or the lookup blew its "
                    "turn budget)",
                    self.id.id,
                )
                return None
            return SourceCore(self.id.id, sd)

        async def _on_activate(self) -> None:
            logger.info("source_actor.activate actor_id=%s", self.id.id)

        async def _on_deactivate(self) -> None:
            await self._state_manager.save_state()

        async def _get_record(self) -> dict | None:
            ok, val = await self._state_manager.try_get_state("record")
            return val if ok else None

        async def _set_record(self, rec: dict) -> None:
            await self._state_manager.set_state("record", rec)
            await self._state_manager.save_state()

        async def receive_reminder(
            self, name, state, due_time, period, ttl=None,
        ) -> None:
            logger.info(
                "source_actor.reminder.fired actor_id=%s reminder=%s",
                self.id.id, name,
            )
            # A-1 belt-and-braces: a retired/paused source — or a stale
            # actor generation after a descriptor edit (the actor_id embeds
            # version[:16]) — must not keep polling upstream forever. Refuse
            # the fire and self-disarm provably-stale reminders.
            from .dapr_actors import reminder_guard_decision

            parts = self.id.id.split("::", 2)
            tail = parts[2] if len(parts) >= 3 else None
            rec = await self._get_record()
            sd = await resolve_source_deps(self.id.id)
            head = (
                sd.descriptor.identity.version if sd is not None else None
            )
            decision = reminder_guard_decision(
                record_lifecycle=(rec or {}).get("lifecycle"),
                own_tail=tail,
                head_version=head,
            )
            if decision == "unregister":
                logger.info(
                    "source_actor.reminder.stale actor_id=%s reminder=%s "
                    "— unregistering", self.id.id, name,
                )
                try:
                    await self.unregister_reminder(name)
                except Exception as exc:
                    logger.warning(
                        "source_actor.reminder.unregister_failed "
                        "actor_id=%s err=%s", self.id.id, exc,
                    )
                return
            if decision == "skip":
                logger.info(
                    "source_actor.reminder.skip actor_id=%s lifecycle=%s",
                    self.id.id, (rec or {}).get("lifecycle"),
                )
                return
            await self.run({"trigger_kind": "reminder"})

        async def activate(self) -> dict:
            core = await self._core()
            if core is None:
                return {"outcome": "noop", "reason": "no_deps"}
            await core.activation_check()  # SRC-8: a kind's guard refuses loud (SEAMS #63)
            sd = core.descriptor
            rec = await self._get_record() or {
                "actor_id": self.id.id,
                "descriptor_id": sd.identity.id,
                "descriptor_version": sd.identity.version,
                "acquisition": sd.acquisition,
                "lifecycle": DRAFT,
            }
            rec["lifecycle"] = ACTIVE
            await self._set_record(rec)

            # Provisioning first (upstream watch must exist before we receive).
            # S-6: this reaches a THIRD-PARTY upstream (a webhook subscription,
            # a watch registration). A remote host that accepts the connection
            # and never answers is the exact failure shape that wedged the
            # models host on 08-01 — listening, not completing — and here it
            # would hold this source's turn forever. Bounded; a timeout records
            # the miss and lets activation finish, because the reminder
            # registration below is the part that keeps the source alive and it
            # must not be hostage to a slow upstream. `reconcile_upstream` is
            # idempotent and re-runs on the next ENSURE_ACTIVE.
            prov_result = await bounded_turn_op_or(
                core.reconcile_upstream(),
                {"outcome": "deadline", "reason": "turn_budget_exceeded"},
                op="source.activate.reconcile_upstream", actor_id=self.id.id,
            )

            if sd.acquisition == "poll":
                schedule = sd.cadence.schedule if sd.cadence else None
                if schedule is not None:
                    expr = schedule.raw if hasattr(schedule, "raw") else str(schedule)
                    try:
                        due, per = cron_to_reminder_timing(expr)
                        await self.register_reminder(
                            name=f"poll_{sd.identity.id}",
                            state=b"{}",
                            due_time=due,
                            period=per,
                        )
                    except Exception as exc:
                        logger.warning(
                            "source_actor.reminder.invalid actor_id=%s expr=%r err=%s",
                            self.id.id, expr, exc,
                        )
            else:
                # push: the host binds the handler emit callback; nothing to
                # schedule here. The handler's own on_activate registered it
                # with the router at deps-build time.
                pass

            return {"lifecycle": ACTIVE, "acquisition": sd.acquisition, "provision": prov_result}

        async def pause(self) -> dict:
            rec = await self._get_record() or {}
            rec["lifecycle"] = PAUSED
            await self._set_record(rec)
            # A-1: a paused poll source must stop waking the scheduler. The
            # reminder name embeds the descriptor_id (segment 1 of our actor
            # id) so no deps resolution is needed. Best-effort — push sources
            # (no reminder) just no-op here.
            parts = self.id.id.split("::", 2)
            descriptor_id = parts[1] if len(parts) >= 2 else self.id.id
            try:
                await self.unregister_reminder(f"poll_{descriptor_id}")
                logger.info(
                    "source_actor.reminder.unregistered actor_id=%s reason=pause",
                    self.id.id,
                )
            except Exception as exc:
                logger.debug(
                    "source_actor.reminder.unregister_noop actor_id=%s err=%s",
                    self.id.id, exc,
                )
            return {"lifecycle": PAUSED}

        async def resume(self) -> dict:
            # A-1: pause() unregisters the poll reminder, so resume must do
            # a full activate() — which re-asserts the record, re-registers
            # the reminder, and re-reconciles upstream (all idempotent) —
            # not just flip the lifecycle field back.
            return await self.activate()

        async def retire(self) -> dict:
            core = await self._core()
            deprov = {}
            if core is not None:
                # S-6: same third-party exposure as activate()'s provisioning
                # leg, and worse to hold — a retire that never returns keeps a
                # superseded actor's turn occupied while the reconciler waits on
                # it. The reminder unregister below is the part that actually
                # stops the polling and runs regardless.
                deprov = await bounded_turn_op_or(
                    core.deprovision_upstream(),
                    {"outcome": "deadline", "reason": "turn_budget_exceeded"},
                    op="source.retire.deprovision_upstream",
                    actor_id=self.id.id,
                )
            # A-1: unregister by the PARSED descriptor_id (segment 1 of the
            # actor id — identical to sd.identity.id) so the reminder dies
            # even when deps resolution fails, e.g. the descriptor was
            # deleted from the registry. That failure mode previously left
            # a retired source polling upstream forever.
            parts = self.id.id.split("::", 2)
            descriptor_id = parts[1] if len(parts) >= 2 else self.id.id
            try:
                await self.unregister_reminder(f"poll_{descriptor_id}")
                logger.info(
                    "source_actor.reminder.unregistered actor_id=%s reason=retire",
                    self.id.id,
                )
            except Exception as exc:
                logger.debug(
                    "source_actor.reminder.unregister_noop actor_id=%s err=%s",
                    self.id.id, exc,
                )
            rec = await self._get_record() or {}
            rec["lifecycle"] = RETIRED
            await self._set_record(rec)
            return {"lifecycle": RETIRED, "deprovision": deprov}

        async def run(self, payload: dict | None = None) -> dict:
            rec = await self._get_record()
            if rec is None or rec.get("lifecycle") != ACTIVE:
                return {"outcome": "noop", "reason": "not_active"}
            core = await self._core()
            if core is None:
                return {"outcome": "hard_fail", "error": "no_deps"}
            # DISCOVERY branch — mirrors TargetActor.run's discriminator: a
            # descriptor carrying a `discovery` block materialises sources
            # instead of acquiring signals. It must precede the acquisition
            # check: a discovery template does not pull (its materialised
            # children do), so the `push_source_not_polled` / `pull_once` path
            # is not the work it wants.
            if core.descriptor.discovery is not None:
                try:
                    result = await core.run_discovery_cycle()
                except Exception as exc:
                    logger.exception(
                        "source_actor.discovery.failed actor_id=%s err=%s",
                        self.id.id, exc,
                    )
                    rec["last_run_at"] = _utcnow().isoformat()
                    rec["last_outcome"] = "hard_fail"
                    rec["last_error"] = str(exc)
                    await self._set_record(rec)
                    return {"outcome": "hard_fail", "error": str(exc)}
                rec["last_run_at"] = _utcnow().isoformat()
                rec["last_outcome"] = result["outcome"]
                rec["last_error"] = None
                await self._set_record(rec)
                return result
            if core.descriptor.acquisition != "poll":
                return {"outcome": "noop", "reason": "push_source_not_polled"}
            result = await core.pull_once()
            rec["last_run_at"] = _utcnow().isoformat()
            rec["last_outcome"] = result["outcome"]
            await self._set_record(rec)
            return result

        async def reconcile(self) -> dict:
            core = await self._core()
            if core is None:
                return {"outcome": "noop", "reason": "no_deps"}
            return await core.reconcile_upstream()

        async def get_state(self) -> dict:
            return await self._get_record() or {}

else:  # pragma: no cover
    SourceActor = None  # type: ignore[assignment]
    SourceActorInterface = None  # type: ignore[assignment]


__all__ = [
    "SourceCore",
    "SourceDeps",
    "SourceActor",
    "SourceActorInterface",
    "write_canonical_signal",
    "lookup_source_credibility",
    "register_source_deps",
    "register_source_deps_resolver",
    "resolve_source_deps",
    "clear_source_deps",
    "HttpUpstreamClient",
    "bulk_highwater_advance",
    "BULK_RESUME_OFFSET_KEY",
    "BULK_TRAVERSED_KEY",
]
