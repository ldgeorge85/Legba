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

Design note — the **mechanism** is the deliverable; the handler *library* is
incremental. The core logic lives in :class:`SourceCore` (a plain class,
directly testable against the dev rig without a Dapr sidecar). The thin
:class:`SourceActor` Dapr wrapper delegates to it so the production path and
the tested path are the same code. This module never edits the existing
``dapr_actors.py`` target/analyst actors (those are RED + out of scope).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import asyncpg
from pydantic import BaseModel, ConfigDict

from ..data.nats import signal_subject
from ..data.schemas.source import SourceDescriptor
from ..data.filters.ingest_dedupe import IngestDedupe, ingest_dedupe_from_stages
from ..data.filters.source_credibility import (
    _extract_signal_host,
    extract_lookup_hosts,
)
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
from .deps import StandardDeps
from .source_factory import build_source_handler
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
_MAX_ENTRIES_PER_POLL = 100      # hard count cap per pull
_POLL_BUDGET_S = 30.0            # wall-time budget per pull
_ENRICH_TIMEOUT_S = 12.0         # per-entry baseline/enrichment timeout

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


def _entry_logical_ts(sig: Signal) -> datetime:
    """The logical timestamp of one processed entry, for cursor advance.

    Prefers a handler-provided logical timestamp on the payload (RSS /
    json_api stamp ``_published_at_dt``; OpenSanctions stamps
    ``_last_seen_dt``) so the cursor advances along the source's OWN ordering.
    Falls back to the Signal's ``fetched_at`` (always present) for handlers
    that surface no logical timestamp. Always returns a tz-aware UTC datetime.
    """
    for key in ("_published_at_dt", "_last_seen_dt", "_event_dt"):
        val = sig.payload.get(key)
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    fa = sig.fetched_at
    return fa if fa.tzinfo else fa.replace(tzinfo=timezone.utc)


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
# Canonical signal write (NEW source-first schema — NOT write_target_signal).
# ---------------------------------------------------------------------------


_INSERT_SIGNAL = """
INSERT INTO signals (
    id, source_id, source_version, produced_by_id, produced_by_kind,
    fetched_at, owner_tenant, modality, mime_type, media_ref, embedding_ref,
    retention_class, media_ref_expires_at, object_ref,
    payload, canonical_url, language_hint, raw_provenance,
    language, geo, tags, entity_classes, source_credibility,
    content_hash, canonical_signal_id, derived_from, schema_uri
)
VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10, $11,
    $12, $13, $14,
    $15::jsonb, $16, $17, $18::jsonb,
    $19, $20::text[], $21::text[], $22::text[], $23,
    $24, $25, $26::uuid[], $27
)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""


async def lookup_source_credibility(
    conn: asyncpg.Connection, signal: Signal,
) -> float | None:
    """Resolve a signal's host credibility from the ``source_credibility`` table.

    FIX P2-3: ``signals.source_credibility`` was 100% NULL because the
    ``source_credibility`` PIPELINE FILTER only runs when a source descriptor
    binds the ``"source_credibility"`` kind — and the live descriptors don't.
    The credibility TABLE is scored for ~65 hosts, so we resolve the score at
    the canonical WRITE path instead: extract the host from the signal's
    URL/payload, probe the table (exact host + progressively-trimmed subdomains,
    first hit wins — reusing the filter module's host helpers so producer +
    filter agree), and return the score. Degrades to ``None`` (leaves the column
    NULL) on no host, no table match, or any DB error — never raises.
    """
    host = _extract_signal_host(signal)
    if host is None:
        return None
    candidates = extract_lookup_hosts(host)
    if not candidates:
        return None
    try:
        rows = await conn.fetch(
            """
            SELECT source_host, score
              FROM source_credibility
             WHERE source_host = ANY($1::text[])
            """,
            candidates,
        )
    except Exception as exc:  # pragma: no cover - best-effort lookup
        logger.warning(
            "source_actor.credibility.lookup_failed host=%s err=%s", host, exc,
        )
        return None
    if not rows:
        return None
    # "First match in candidates order" — Postgres has no natural ordering for
    # the input array, so resolve the most-specific host client-side.
    by_host = {r["source_host"]: r for r in rows}
    for candidate in candidates:
        row = by_host.get(candidate)
        if row is not None and row["score"] is not None:
            return float(row["score"])
    return None


async def write_canonical_signal(
    conn: asyncpg.Connection,
    signal: Signal,
    *,
    source_version: str,
    owner_tenant: str,
) -> Any | None:
    """Insert ONE canonical, target-agnostic signal into the new ``signals``
    table. Stamps provenance (source-origin) + tenant from the descriptor.

    Returns the inserted row id, or ``None`` if a row with that id already
    existed (idempotent — a re-fired reminder that re-pulls the same payload
    won't double-insert when the handler reuses a deterministic id; the
    default uuid4 ids are unique so this is a backstop, real dedup is P-09).
    """
    # FIX P2-3: backfill source_credibility from the registry-scored host table
    # when the in-flight signal doesn't already carry a score (i.e. the
    # source_credibility pipeline filter wasn't bound for this descriptor). The
    # lookup degrades to None (column stays NULL) for unknown hosts.
    source_credibility = signal.source_credibility
    if source_credibility is None:
        source_credibility = await lookup_source_credibility(conn, signal)

    row = await conn.fetchrow(
        _INSERT_SIGNAL,
        signal.signal_id,
        signal.source_id,
        source_version or signal.source_version,
        signal.produced_by_id,
        signal.produced_by_kind,
        signal.fetched_at,
        owner_tenant or signal.owner_tenant,
        signal.modality,
        signal.mime_type,
        signal.media_ref,
        signal.embedding_ref,
        signal.retention_class,
        signal.media_ref_expires_at,
        signal.object_ref,
        json.dumps(signal.payload, default=str),
        signal.canonical_url,
        signal.language_hint,
        json.dumps(signal.raw_provenance, default=str),
        signal.language,
        list(signal.geo),
        list(signal.tags),
        list(signal.entity_classes),
        source_credibility,
        signal.content_hash,
        signal.canonical_signal_id,
        list(signal.derived_from),
        signal.schema_uri,
    )
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# Non-productive poll provenance (DQ-H5b #88).
# ---------------------------------------------------------------------------

_INSERT_POLL_OUTCOME = """
INSERT INTO public.source_poll_outcomes (
    source_id, source_version, owner_tenant,
    outcome, health_state, capped, signals_written, error
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

# Cap the stored error string so a verbose traceback can't bloat the row.
_POLL_OUTCOME_ERROR_MAX = 2000


async def write_poll_outcome(
    conn: asyncpg.Connection,
    *,
    source_id: str,
    source_version: str | None,
    owner_tenant: str,
    outcome: str,                 # 'empty' | 'error'
    health_state: str | None,     # 'healthy' | 'degraded' | 'unhealthy' | None
    capped: bool,
    signals_written: int,
    error: str | None,
) -> None:
    """Append a provenance row for a NON-productive poll (DQ-H5b).

    Recorded only when a poll wrote ZERO signals — a productive poll is
    self-evidencing via its ``signals`` rows. ``outcome`` is the coarse rollup
    the cadence watchdog keys on ('error' when the poll failed — an escaped
    exception OR a handler-swallowed 4xx/parse-fail/timeout surfaced via
    ``health_state`` — else 'empty' for a genuine HTTP-200-but-0-items feed).
    """
    await conn.execute(
        _INSERT_POLL_OUTCOME,
        source_id,
        source_version,
        owner_tenant or "default",
        outcome,
        health_state,
        capped,
        int(signals_written),
        error[:_POLL_OUTCOME_ERROR_MAX] if error else None,
    )


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
        )

    def _build_handler(self) -> Any:
        if self.sd.handler is not None:
            return self.sd.handler
        return build_source_handler(
            self.descriptor.identity.kind,
            self.descriptor.config,
            secrets_resolve=self.deps.secrets_resolve,
        )

    def _upstream_client(self) -> UpstreamClient | None:
        if self.sd.upstream_client is not None:
            return self.sd.upstream_client
        # No client wired + provisioning enabled → the host must supply one.
        return None

    # -- baseline + write + publish ---------------------------------------

    async def _process_one(
        self, conn: asyncpg.Connection, ctx: SourceContext, raw: Signal,
    ) -> "Signal | None":
        """Baseline → write ONE canonical signal. Returns the written signal
        (so the caller can publish it per-signal), or None if the baseline
        dropped it or the write was a dedup/conflict no-op."""
        try:
            enriched = await asyncio.wait_for(
                run_baseline(
                    raw, ctx, media=self.descriptor.pipeline.media,
                    extractors=self._extractors,
                    enrichment_stage=self.sd.enrichment_stage,
                ),
                timeout=_ENRICH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            # A single hung extractor/NLP call must not block the actor turn.
            # Skip this entry; the next poll re-pulls it (dedup makes it cheap).
            ctx.logger.warning(
                "source_actor.enrich.timeout actor_id=%s after=%.0fs",
                self.actor_id, _ENRICH_TIMEOUT_S,
            )
            return None
        if enriched is None:
            return None
        # Stamp the source's tenant onto the in-memory signal BEFORE write +
        # fan-out. write_canonical_signal pins owner_tenant on the DB row via a
        # param, but the published envelope is this object's model_dump_json();
        # if the object keeps its model default ("default") the published
        # owner_tenant disagrees with the DB row, the subject token, and the
        # subscription binding. The reactive matcher (subscription.filter.matches)
        # re-checks signal.owner_tenant against the binding's owner_tenant and
        # rejects on mismatch — so an unstamped envelope silently disables ALL
        # reactive triggering (the batch/cadence path reads the DB row, so it
        # was unaffected; only the per-signal fan-out broke). Mirror
        # write_canonical_signal's `owner_tenant or signal.owner_tenant` fallback.
        if self.descriptor.scope.owner_tenant:
            enriched.owner_tenant = self.descriptor.scope.owner_tenant
        written_id = await write_canonical_signal(
            conn,
            enriched,
            source_version=self.descriptor.identity.version,
            owner_tenant=self.descriptor.scope.owner_tenant,
        )
        if written_id is None:
            # ON CONFLICT DO NOTHING (id collision) — nothing written, nothing
            # to fan out.
            return None

        # Source-side ingest dedupe (P-02, tiers 1+2). Run AFTER the insert so
        # the alias link references an existing row, in the SAME connection so
        # the link lands in the same short transaction. Alias/canonical: the
        # raw row is already written + kept; if it matches an existing signal we
        # set its canonical_signal_id + write signal_aliases — never collapse.
        # A canonical_only subscriber then receives only the canonical of the
        # set. Best-effort: a dedupe-link error must not lose the (already
        # written) signal — it stays raw and the periodic analyst links it.
        if self._ingest_dedupe is not None:
            try:
                result = await self._ingest_dedupe.apply(conn, enriched)
                if result.is_duplicate:
                    ctx.logger.info(
                        "source_actor.ingest_dedupe.linked actor_id=%s "
                        "alias=%s canonical=%s tier=%d reason=%s",
                        self.actor_id, enriched.signal_id,
                        result.canonical_signal_id, result.tier, result.reason,
                    )
            except Exception as exc:
                ctx.logger.warning(
                    "source_actor.ingest_dedupe.failed actor_id=%s signal=%s "
                    "err=%s (row kept raw; periodic dedup analyst will link it)",
                    self.actor_id, enriched.signal_id, exc,
                )
        return enriched

    async def _publish(self, signals: list[Signal]) -> None:
        """Fan out each written signal onto the shared raw-pool stream.

        L-205 / PIVOT §6.1: acquisition publishes ONE message per signal to
        ``legba.signals.<tenant>.<source>.<modality>.<event_class>`` (the
        coarse subject the ``legba_signals`` JetStream stream captures and the
        per-target subscription consumers + trigger engine subject-filter on).
        The earlier coarse ``source.<id>.signals`` subject hit no stream
        (``no response from stream``) and never reached the fan-out plane.
        """
        if self.deps.nats_publish is None or not signals:
            return
        tenant = self.descriptor.scope.owner_tenant
        source_id = self.descriptor.identity.id
        published = 0
        for sig in signals:
            subject = signal_subject(
                tenant=tenant,
                source_id=source_id,
                modality=getattr(sig, "modality", "text") or "text",
                event_class="raw",
            )
            try:
                await self.deps.nats_publish(
                    subject, sig.model_dump_json().encode("utf-8"),
                )
                published += 1
            except Exception as exc:  # publish failure must not lose the write
                logger.warning(
                    "source_actor.publish.failed actor_id=%s subject=%s err=%s",
                    self.actor_id, subject, exc,
                )
        if published:
            logger.info(
                "source_actor.published actor_id=%s count=%d subject_root=legba.signals.%s.%s",
                self.actor_id, published, tenant, source_id,
            )

    # -- poll path ---------------------------------------------------------

    async def pull_once(self) -> dict[str, Any]:
        """Pull the source, baseline + write each signal, publish. (poll path)

        Idempotent on restart via the persisted cursor (``since``). Returns a
        run summary.
        """
        ctx = self._make_context()
        handler = self._build_handler()

        cursor_raw = await ctx.state_store.get("cursor") or {}
        since = None
        since_iso = cursor_raw.get("last_pulled_at") if isinstance(cursor_raw, dict) else None
        if isinstance(since_iso, str) and since_iso:
            try:
                since = datetime.fromisoformat(since_iso)
            except ValueError:
                since = None

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
        errored: str | None = None
        capped = False
        # Logical position of the LAST entry we actually consumed this pull —
        # used to advance the cursor to where we STOPPED (not NOW) on a capped
        # pull, so a backlog is resumed rather than skipped (Fix A).
        last_processed_ts: datetime | None = None
        deadline = time.monotonic() + _POLL_BUDGET_S
        try:
            async with self.deps.pg_pool.acquire() as conn:
                count = 0
                async for raw in handler.pull(ctx, since):
                    # Bound the poll by count AND wall time so it always finishes
                    # within Dapr's drain window and reaches the cursor-advance
                    # below — never trapped re-grinding a backlog (2026-06-08).
                    if count >= _MAX_ENTRIES_PER_POLL or time.monotonic() > deadline:
                        capped = True
                        break
                    count += 1
                    # Track the logical timestamp of every entry we CONSUME
                    # (not just the ones that survive dedup/baseline) — a capped
                    # pull made real forward progress through the backlog even
                    # when an entry was dropped, and must not re-grind it.
                    ts = _entry_logical_ts(raw)
                    if last_processed_ts is None or ts > last_processed_ts:
                        last_processed_ts = ts
                    sig = await self._process_one(conn, ctx, raw)
                    if sig is not None:
                        written.append(sig)
                        # Publish immediately — fan-out then survives a later
                        # cap/error/drain instead of being lost at the end.
                        await self._publish([sig])
        except Exception as exc:
            errored = str(exc)
            logger.warning(
                "source_actor.pull.error actor_id=%s err=%s", self.actor_id, exc,
            )
        finally:
            # Advance the cursor along REAL forward progress only — never to a
            # bare wall-clock NOW on a zero-yield pull. Cursor-advance policy:
            #   * hard fetch error  -> keep the prior position (retry the window).
            #   * >=1 entry processed (capped OR complete) -> advance to the
            #     LAST PROCESSED entry's logical timestamp, NOT NOW. NOW would
            #     skip the unprocessed backlog past the cap; the last-processed
            #     timestamp keeps the forward-progress anti-trap WITHOUT skipping
            #     live entries (the observed skipping behaviour).
            #   * ZERO entries processed (a genuinely empty/at-head/all-filtered
            #     pull) -> LEAVE THE CURSOR UNCHANGED (keep ``since_iso``). The
            #     prior policy advanced ``since`` to NOW here, but a feed that
            #     misses every item one cycle (e.g. a transient parse/date bug
            #     that drops the whole window, or a Cloudflare 304 pin) then has
            #     its ``since`` march irreversibly forward and can NEVER catch up
            #     — a permanent silent stall (Fix B). Holding the cursor lets the
            #     next healthy pull re-see the same window; content-hash dedup
            #     downstream makes the re-emit on recovery a harmless no-op, so
            #     holding cannot cause a dup-storm.
            # First-ever pull (no prior cursor, since_iso is None): there is no
            # window to protect, so a zero-yield pull seeds the cursor at NOW to
            # avoid re-walking from epoch on the next cycle.
            if errored is not None:
                last_pulled_at = since_iso or _utcnow().isoformat()
            elif last_processed_ts is not None:
                last_pulled_at = last_processed_ts.isoformat()
            elif since_iso:
                last_pulled_at = since_iso
            else:
                last_pulled_at = _utcnow().isoformat()
            new_cursor = {
                "source_id": self.descriptor.identity.id,
                "last_pulled_at": last_pulled_at,
                "rows_pulled": int(cursor_raw.get("rows_pulled", 0) if isinstance(cursor_raw, dict) else 0)
                + len(written),
                "last_error": errored,
            }
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
                    bulk_offset = bulk_highwater_advance(
                        prior_offset, count, reached_end=reached_end,
                    )
                new_cursor["bulk_offset"] = bulk_offset
            try:
                await ctx.state_store.set("cursor", new_cursor)
            except Exception:  # cursor persist must not mask the pull result
                logger.warning(
                    "source_actor.cursor.persist_failed actor_id=%s",
                    self.actor_id, exc_info=True,
                )
            # DQ-H5b (#88) — record a provenance row for a NON-productive poll
            # (empty / error) so the cadence watchdog + UI can surface WHY this
            # source is silent. A poll that wrote >=1 signal is self-evidencing
            # via its signals rows, so it is intentionally NOT logged here.
            if not written:
                await self._record_poll_outcome(
                    ctx, handler, errored=errored, capped=capped,
                )

        if written or capped or errored:
            logger.info(
                "source_actor.pull.done actor_id=%s written=%d capped=%s err=%s "
                "bulk_offset=%s",
                self.actor_id, len(written), capped, errored,
                new_cursor.get("bulk_offset"),
            )
        return {
            "outcome": "success" if written else ("hard_fail" if errored else "noop"),
            "signals_written": len(written),
            "signal_ids": [str(s.signal_id) for s in written],
            "error": errored,
        }

    async def _record_poll_outcome(
        self,
        ctx: SourceContext,
        handler: Any,
        *,
        errored: str | None,
        capped: bool,
    ) -> None:
        """Persist a non-productive-poll provenance row (DQ-H5b #88).

        Best-effort: a failure here must NEVER mask the pull result. Reads the
        handler's own freshly-recorded health (``state`` + ``last_error``) when
        the handler exposes a ``health_state_key`` — that record is the ONLY
        place a handler-SWALLOWED 4xx / parse-fail / timeout is observable from
        the poll path (no exception escapes ``handler.pull`` for those cases),
        so it is what turns a bare "empty" into an honest
        "error / unhealthy — <reason>".
        """
        health_state: str | None = None
        health_error: str | None = None
        # Only trust the handler health record when the pull ran to a NATURAL
        # conclusion. A capped pull can leave a STALE prior-pull health record
        # (we broke the async-for before the handler wrote this pull's health),
        # so we don't read it then — capped+0-written is recorded as plain
        # 'empty' with capped=True, which already says "made progress".
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

        if errored is not None or health_state in ("degraded", "unhealthy"):
            outcome = "error"
        else:
            outcome = "empty"

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
                    signals_written=0,
                    error=errored or health_error,
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

          * If WE capped (hit ``_MAX_ENTRIES_PER_POLL`` / the wall-clock
            budget), it is NEVER end-of-stream — we stopped the handler
            mid-snapshot. The handler's post-loop report didn't even run.
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
        async def _emit(raw: Signal) -> None:
            ctx = self._make_context()
            async with self.deps.pg_pool.acquire() as conn:
                sig = await self._process_one(conn, ctx, raw)
            if sig is not None:
                await self._publish([sig])

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
            sd = await resolve_source_deps(self.id.id)
            if sd is None:
                logger.warning(
                    "source_actor.no_deps actor_id=%s (host did not register "
                    "deps and resolver did not resolve)",
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
            prov_result = await core.reconcile_upstream()

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
                deprov = await core.deprovision_upstream()
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
