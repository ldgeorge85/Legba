# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DOCUMENT sink — today's signal write path, moved out of the actor.

N-L step 0 (planning/reviews/DESIGN_2026-11-13/09 §D.3): everything that used
to sit between "the baseline produced an enriched :class:`Signal`" and "the
signal is on the bus" lived inside ``runtime/source_actor.py`` — the canonical
INSERT, the S-4 intra-source collapse, the credibility backfill, the tenant
and licence stamps, the ingest-dedupe alias link and the per-signal NATS
publish. It is moved here VERBATIM (the functions below are the same bytes
that sat in the actor at ``9a59a0da``; the class methods are the tail of
``SourceCore._process_one`` and ``SourceCore._publish``), so the document
path writes and publishes exactly what it did. The equivalence is proven by
``tests/runtime/test_signal_sink_equivalence.py``, which runs the frozen
pre-move actor and this one over the same live-shaped fixtures.

The logger keeps the actor's name on purpose: every log line this path emits
was ``legba.runtime.source_actor`` and log routing must not change with a
file move.

Public names stay importable from :mod:`legba.runtime.source_actor` (it
re-exports them), so no caller had to change.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import asyncpg

from ...data._url_canon import stored_canonical_url_hash
from ...data.claim_support import CLAIM_PAYLOAD_KEY, claim_stamp_for, strip_claim_media
from ...data.nats import signal_subject
from ...data.source_classification import signal_data
from ...data.tags import BoundTag
from ...data.filters.source_credibility import (
    _extract_signal_host,
    extract_lookup_hosts,
)
from ...data.sources._contract import Signal, SourceContext
from ...shared.url_redaction import redact_presigned_in
from ..page_landing import is_page_record
from . import register_sink

logger = logging.getLogger("legba.runtime.source_actor")


# S-4 intra-source exact-hash dedup. A source that re-lists an unchanged item
# every poll (hazard feeds re-serve active events; NWS/NASA/USGS re-publish)
# used to spawn a fresh signals row per poll — 41% of a 7-day window's rows were
# exact content-hash duplicates, ALL intra-source (the "M 5.5 Chupaca, Peru"
# quake was stored 194x). content_hash is a DETERMINISTIC hash of content
# (source-set, or the baseline backstop over canonical_url + title — never the
# fetch time), so an exact (source_id, content_hash) match is byte-identical
# content: safe to collapse. Before inserting we look for an existing
# same-(source_id, content_hash) row within a lookback window and, on a hit,
# record the re-serve on that row and SKIP the insert instead of appending a
# redundant row. This is INTRA-source only (the lookup pins source_id); a
# cross-source same-hash dup is still KEPT and alias-linked by ingest_dedupe /
# the periodic cross_source_dedup analyst.
# Default ON — the collapse is provably lossless for an exact hash; set
# LEGBA_INTRASOURCE_DEDUP=0 to disable (fall back to keep-all + alias-link).
#
# B-1 (2026-08-03) — WHAT THE COLLAPSE BUMPS. It used to advance the surviving
# row's ``fetched_at``, on the reading "recency stays fresh — we still see this".
# That reading cost us the 08-03 freshness incident (MASTER_PLAN §32.7): when the
# 5 rsshub AP-topic feeds froze upstream and re-served the same 39-item snapshot
# every poll for six days, 201 signals reported ``fetched_at = today`` the whole
# time, and because the substrate slice WINDOWS + ORDERS on ``fetched_at`` a
# 07-28 snapshot topped every 72h slice for five country desks and the journal.
#
# ``fetched_at`` now means exactly what every reader already assumed: the fetch
# that first delivered THIS content. An exact content_hash match is, by
# construction, identical content — nothing was fetched, so nothing is bumped.
# The re-serve is recorded on ``last_seen_at`` (migration 0140) and counted per
# poll as ``reserve_unchanged``.
#
# The load-bearing converse still holds WITHOUT special-casing: genuinely-updated
# content (an EONET/GDACS event whose feature JSON changed, a re-published RSS
# item with an edited body) hashes DIFFERENTLY, so it never reaches this path at
# all — it inserts a new row carrying its own honest, fresh ``fetched_at``. The
# "updates must still look fresh" semantic is a property of content-addressed
# hashing, not of the bump.
_INTRASOURCE_DEDUP_ENV = "LEGBA_INTRASOURCE_DEDUP"
_INTRASOURCE_DEDUP_WINDOW_ENV = "LEGBA_INTRASOURCE_DEDUP_WINDOW_HOURS"
_DEFAULT_INTRASOURCE_DEDUP_WINDOW_HOURS = 168  # 7 days


def _intrasource_dedup_enabled() -> bool:
    """Whether S-4 intra-source exact-hash collapse is active (default ON)."""
    return os.getenv(_INTRASOURCE_DEDUP_ENV, "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _intrasource_dedup_window_hours() -> int:
    """Lookback window (hours) for the (source_id, content_hash) existence check.

    A re-emission WITHIN this window collapses onto the existing row; a
    re-appearance after a longer silence lands a fresh row (treated as a new
    occurrence). The window is measured from ``last_seen_at`` (B-1), so an item
    that keeps re-emitting never falls out of its own window — the bound only
    governs re-appearance after a real gap. Measuring it from ``fetched_at``
    instead would, now that a re-serve no longer bumps ``fetched_at``, drop a
    continuously-re-served item out of its own window after 7 days and insert a
    duplicate carrying a fresh timestamp: the 08-03 freshness lie, on a weekly
    period. Defaults to 168h (7d); override via
    LEGBA_INTRASOURCE_DEDUP_WINDOW_HOURS (a non-positive/garbage value degrades
    to the default).
    """
    raw = os.getenv(_INTRASOURCE_DEDUP_WINDOW_ENV, "").strip()
    if not raw:
        return _DEFAULT_INTRASOURCE_DEDUP_WINDOW_HOURS
    try:
        value = int(raw)
    except ValueError:  # pragma: no cover — defensive
        return _DEFAULT_INTRASOURCE_DEDUP_WINDOW_HOURS
    return value if value > 0 else _DEFAULT_INTRASOURCE_DEDUP_WINDOW_HOURS

# ---------------------------------------------------------------------------
# Canonical signal write (NEW source-first schema — NOT write_target_signal).
# ---------------------------------------------------------------------------


# B-1: ``last_seen_at`` is stamped from the SAME ``fetched_at`` this insert
# carries — at first write the two agree ("fetched now, seen now"). They diverge
# only when the source later re-serves this exact content: the S-4 collapse
# advances ``last_seen_at`` and leaves ``fetched_at`` pinned to the fetch that
# actually delivered the content (migration 0140).
_INSERT_SIGNAL = """
INSERT INTO signals (
    id, source_id, source_version, produced_by_id, produced_by_kind,
    fetched_at, owner_tenant, modality, mime_type, media_ref, embedding_ref,
    retention_class, media_ref_expires_at, object_ref,
    payload, canonical_url, language_hint, raw_provenance,
    language, geo, tags, entity_classes, source_credibility,
    content_hash, canonical_signal_id, derived_from, schema_uri,
    access_class, last_seen_at, data, canonical_url_hash
)
VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10, $11,
    $12, $13, $14,
    $15::jsonb, $16, $17, $18::jsonb,
    $19, $20::text[], $21::text[], $22::text[], $23,
    $24, $25, $26::uuid[], $27,
    $28, $6, $29::jsonb, $30
)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""


# S-4: atomic find-and-record for an intra-source exact-hash duplicate. Picks the
# MOST-RECENT existing row sharing this (source_id, content_hash, owner_tenant)
# within the lookback window and advances its last_seen_at (never backwards — via
# GREATEST) + updated_at. Recording on the MOST-RECENT row — deliberately NOT the
# earliest — keeps the "earliest fetched_at = canonical" ordering that
# ingest_dedupe and the cross_source_dedup analyst pick deterministically stable.
# content_hash <> '' skips the schema-default "no hash" rows (empty string is
# never a dedup key). The window bound is inlined (int-validated, no user input)
# mirroring the coalescer's INTERVAL pattern. The equality on (source_id,
# content_hash) rides the existing signals_content_hash_idx + signals_source_idx
# (content_hash is a sha256 → the per-hash candidate set is tiny), so no new
# index is needed.
#
# B-1: ``fetched_at`` is NOT touched. An exact content_hash match is identical
# content — no fetch of anything new happened, so the row's fetch time must not
# move (the 08-03 freshness lie). ``last_seen_at`` carries the re-serve, and the
# window + the candidate ORDER BY key off it (COALESCE for pre-0140 rows, which
# have never recorded a re-serve and so are correctly read at their fetched_at).
def _bump_intrasource_sql(window_hours: int) -> str:
    return f"""
    UPDATE signals
       SET last_seen_at = GREATEST(COALESCE(last_seen_at, fetched_at), $4),
           updated_at = NOW()
     WHERE id = (
         SELECT id FROM signals
          WHERE source_id = $1
            AND content_hash = $2
            AND content_hash <> ''
            AND owner_tenant = $3
            AND COALESCE(last_seen_at, fetched_at)
                > NOW() - INTERVAL '{int(window_hours)} hours'
          ORDER BY COALESCE(last_seen_at, fetched_at) DESC, id DESC
          LIMIT 1
     )
    RETURNING id
    """


async def _bump_intrasource_duplicate(
    conn: asyncpg.Connection,
    signal: Signal,
    *,
    owner_tenant: str,
    window_hours: int,
) -> Any | None:
    """Record a re-serve on the freshest existing same-(source_id, content_hash) row.

    Returns the recorded row id on a hit (the caller then SKIPS the insert), or
    None when no in-window duplicate exists (the caller inserts normally). The
    read+write is a SINGLE atomic UPDATE ... RETURNING, so there is no
    check-then-insert race within the connection (and a source is a single Dapr
    actor — its polls are turn-serialized, so no cross-poll race either). The
    caller guards ``content_hash`` non-empty before calling.

    B-1: what advances is ``last_seen_at`` ("the source re-served this"), never
    ``fetched_at`` ("we fetched this content"). See the module note above S-4.
    """
    return await conn.fetchval(
        _bump_intrasource_sql(window_hours),
        signal.source_id,
        signal.content_hash,
        owner_tenant,
        signal.fetched_at,
    )


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
    access_class: str = "restricted",
    dedup_stats: dict[str, int] | None = None,
    classification: tuple[BoundTag, ...] = (),
) -> Any | None:
    """Insert ONE canonical, target-agnostic signal into the new ``signals``
    table. Stamps provenance (source-origin) + tenant from the descriptor.

    ``access_class``: Wave-E — the source descriptor's ``scope.access_class``
    (data/provenance/access.py's closed vocabulary), stamped onto the row's
    ``access_class`` column (migration 0216). Defaults fail-closed to
    ``"restricted"`` for callers that don't resolve a descriptor (e.g. the
    seed/manual-batch path); the real source-actor ingest path always passes
    the descriptor's own value.

    Returns the inserted row id, or ``None`` if the write was a no-op — either a
    row with that id already existed (the uuid4-id ON CONFLICT backstop) OR (S-4)
    an intra-source exact-hash duplicate already landed within the lookback
    window, in which case the existing row's recency (``fetched_at``) is bumped
    and NO new row is inserted. The caller treats ``None`` as "nothing written,
    nothing to fan out".

    ``dedup_stats``: when supplied, its ``"reserve_unchanged"`` counter is
    incremented on each S-4 collapse. The poll path passes this so it can tell
    "wrote nothing because the source re-served content we already hold, byte for
    byte" (source alive, content NOT moving) apart from "wrote nothing because the
    feed was empty" (the liveness watchdog's stall signal). B-1 named the counter
    for what it measures — an unchanged re-serve — because that is the serve-stale
    evidence the 08-03 incident had nowhere to live. See
    :data:`_INTRASOURCE_DEDUP_ENV`.

    ``classification``: W10 TAX-2 — the source's declared classification
    (``SourceContext.classification``). The row's ``data`` carries
    ``classifications`` = those entries + the signal's own namespaced tags,
    bound (``source_classification.signal_data``); nothing to stamp ⇒ NULL.
    """
    # FIX P2-3: backfill source_credibility from the registry-scored host table
    # when the in-flight signal doesn't already carry a score (i.e. the
    # source_credibility pipeline filter wasn't bound for this descriptor). The
    # lookup degrades to None (column stays NULL) for unknown hosts.
    source_credibility = signal.source_credibility
    if source_credibility is None:
        source_credibility = await lookup_source_credibility(conn, signal)

    # S-4: intra-source exact-hash collapse (pre-insert). An empty content_hash
    # is the schema default ("no hash") and is never a dedup key. Best-effort —
    # any error here must NOT lose the write, so we log and fall through to the
    # normal insert (a duplicate row is a lesser evil than a dropped signal).
    if signal.content_hash and _intrasource_dedup_enabled():
        resolved_tenant = owner_tenant or signal.owner_tenant
        try:
            bumped = await _bump_intrasource_duplicate(
                conn, signal,
                owner_tenant=resolved_tenant,
                window_hours=_intrasource_dedup_window_hours(),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "source_actor.intrasource_dedup.failed source=%s hash=%s "
                "err=%s (falling through to insert)",
                signal.source_id, (signal.content_hash or "")[:16], exc,
            )
            bumped = None
        if bumped is not None:
            if dedup_stats is not None:
                dedup_stats["reserve_unchanged"] = (
                    dedup_stats.get("reserve_unchanged", 0) + 1
                )
            logger.debug(
                "source_actor.intrasource_dedup.collapsed source=%s hash=%s "
                "row=%s (unchanged re-serve: last_seen_at advanced, fetched_at "
                "unchanged, insert skipped)",
                signal.source_id, signal.content_hash[:16], bumped,
            )
            return None

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
        json.dumps(redact_presigned_in(signal.raw_provenance), default=str),  # W11-F59
        signal.language,
        list(signal.geo),
        list(signal.tags),
        list(signal.entity_classes),
        source_credibility,
        signal.content_hash,
        signal.canonical_signal_id,
        list(signal.derived_from),
        signal.schema_uri,
        access_class,
        _json_or_none(await signal_data(signal.tags, classification)),
        # N-S (migration 0247): the stored tier-1 dedupe key, a pure function
        # of the canonical_url argument above — ingest dedupe's lookup is an
        # indexed equality on it instead of a scan of every stored URL.
        stored_canonical_url_hash(signal.canonical_url),
    )
    return row["id"] if row else None


def _json_or_none(bag: dict[str, Any] | None) -> str | None:
    return None if bag is None else json.dumps(bag)



# ---------------------------------------------------------------------------
# SignalSink — the tail of SourceCore._process_one + SourceCore._publish
# ---------------------------------------------------------------------------


class SignalSink:
    """The ``document`` family: one enriched :class:`Signal` → one ``signals``
    row (or an S-4 collapse), its ingest-dedupe alias link, and one message on
    ``legba.signals.<tenant>.<source>.<modality>.raw``.

    It reads the owning core's descriptor, deps and ingest-dedupe engine
    THROUGH the core (never a copy taken at construction), so anything that
    replaces those on the core — a test, a re-activation — is seen here
    exactly as the inline code saw it.
    """

    family = "document"
    record_types: tuple[type, ...] = (Signal,)

    def __init__(self, core: Any) -> None:
        self._core = core

    async def write(
        self, conn: asyncpg.Connection, ctx: SourceContext, enriched: Signal,
        *, dedup_stats: dict[str, int] | None = None,
    ) -> "Signal | None":
        """Stamp, write and alias-link ONE enriched signal. Returns it when a
        row was written (the caller publishes it), ``None`` on a collapse or
        an id conflict."""
        core = self._core
        descriptor = core.descriptor
        # I-2: a fetched PAGE first meets the document already held under its
        # URL — collapse into it, or fill a summary-only one in — and gets its
        # host credibility/licence (runtime/page_landing.py). Other sources:
        # one dict lookup, no SQL.
        if is_page_record(enriched):
            from ..page_landing import land  # noqa: PLC0415

            try:
                landing = await land(conn, enriched, owner_tenant=descriptor.scope.owner_tenant
                                     or enriched.owner_tenant)
            except Exception:
                logger.warning("source_actor.page_landing.failed actor_id=%s signal=%s (writing as new)",
                               core.actor_id, enriched.signal_id, exc_info=True)
                landing = None
            notes = getattr(core, "page_landings", None)
            if landing is not None and isinstance(notes, list):
                notes.append(landing)
            if landing is not None and landing.action != "new":
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
        if descriptor.scope.owner_tenant:
            enriched.owner_tenant = descriptor.scope.owner_tenant
        # LIC-2 stamp (SourceScope → signal): copy the descriptor's declared
        # license_class onto the payload so the OpenSearch corpus facet + the
        # P2-2 evidence-archiver retention gate read a per-signal value. A
        # payload that already carries one (manual-batch provenance, a
        # source-handler override) WINS — the scope value is the source-level
        # default, not a clobber. Unset scope → no key (honest absence).
        scope_license = getattr(descriptor.scope, "license_class", None)
        if scope_license and "license_class" not in enriched.payload:
            enriched.payload["license_class"] = scope_license
        # C-1 claim stamp (SourceScope → signal): a ``source_class: claim``
        # source's row carries ``payload.claim`` = {source_class, claimant,
        # claimant_from, verb} — the claimant from the feed's declared
        # structured field, else the declared claimant; never a model's. The
        # stamp is the SOURCE's verdict and always overwrites: a feed cannot
        # declare its own rows claims or not-claims. Text only: no media
        # reference survives on a claim row. C-2: a channel the descriptor
        # marks ``claim`` (Telegram ``config.classes``) is stamped the same way;
        # its text was cleaned before enrichment (data/claim_hygiene.py).
        stamp = claim_stamp_for(descriptor, enriched.payload)
        if stamp is not None:
            enriched.payload[CLAIM_PAYLOAD_KEY] = stamp
            strip_claim_media(enriched)
        elif isinstance(enriched.payload.get(CLAIM_PAYLOAD_KEY), dict):
            enriched.payload.pop(CLAIM_PAYLOAD_KEY, None)
        written_id = await write_canonical_signal(
            conn,
            enriched,
            source_version=descriptor.identity.version,
            owner_tenant=descriptor.scope.owner_tenant,
            access_class=descriptor.scope.access_class,
            dedup_stats=dedup_stats,
            classification=getattr(ctx, "classification", ()),
        )
        if written_id is None:
            # No row written — either the uuid4-id ON CONFLICT backstop or (S-4)
            # an intra-source exact-hash duplicate whose recency was bumped in
            # place. Nothing to fan out either way (a re-emission of identical
            # content must not re-fire triggers / inflate firing counts).
            return None

        # Source-side ingest dedupe (P-02, tiers 1+2). Run AFTER the insert so
        # the alias link references an existing row, in the SAME connection so
        # the link lands in the same short transaction. Alias/canonical: the
        # raw row is already written + kept; if it matches an existing signal we
        # set its canonical_signal_id + write signal_aliases — never collapse.
        # A canonical_only subscriber then receives only the canonical of the
        # set. Best-effort: a dedupe-link error must not lose the (already
        # written) signal — it stays raw and the periodic analyst links it.
        ingest_dedupe = core._ingest_dedupe
        if ingest_dedupe is not None:
            try:
                result = await ingest_dedupe.apply(conn, enriched)
                if result.is_duplicate:
                    ctx.logger.info(
                        "source_actor.ingest_dedupe.linked actor_id=%s "
                        "alias=%s canonical=%s tier=%d reason=%s",
                        core.actor_id, enriched.signal_id,
                        result.canonical_signal_id, result.tier, result.reason,
                    )
            except Exception as exc:
                ctx.logger.warning(
                    "source_actor.ingest_dedupe.failed actor_id=%s signal=%s "
                    "err=%s (row kept raw; periodic dedup analyst will link it)",
                    core.actor_id, enriched.signal_id, exc,
                )
        return enriched

    async def publish(self, signals: list[Signal]) -> None:
        """Fan out each written signal onto the shared raw-pool stream.

        L-205 / PIVOT §6.1: acquisition publishes ONE message per signal to
        ``legba.signals.<tenant>.<source>.<modality>.<event_class>`` (the
        coarse subject the ``legba_signals`` JetStream stream captures and the
        per-target subscription consumers + trigger engine subject-filter on).
        The earlier coarse ``source.<id>.signals`` subject hit no stream
        (``no response from stream``) and never reached the fan-out plane.
        """
        core = self._core
        await self._publish(signals)
        # I-1: references in these documents become ingestion requests, for a
        # source that declares a follow policy, with LEGBA_INTAKE_FOLLOW on
        # (both absent today: no SQL, no work). Never costs the signal.
        if signals and getattr(core.descriptor, "follow", None) is not None:
            from ..intake_follow import follow_written  # noqa: PLC0415

            try:
                await follow_written(core, signals)
            except Exception:
                logger.warning("source_actor.follow.failed actor_id=%s", core.actor_id, exc_info=True)

    async def _publish(self, signals: list[Signal]) -> None:
        core = self._core
        if core.deps.nats_publish is None or not signals:
            return
        tenant = core.descriptor.scope.owner_tenant
        source_id = core.descriptor.identity.id
        published = 0
        for sig in signals:
            subject = signal_subject(
                tenant=tenant,
                source_id=source_id,
                modality=getattr(sig, "modality", "text") or "text",
                event_class="raw",
            )
            try:
                await core.deps.nats_publish(
                    subject, sig.model_dump_json().encode("utf-8"),
                )
                published += 1
            except Exception as exc:  # publish failure must not lose the write
                logger.warning(
                    "source_actor.publish.failed actor_id=%s subject=%s err=%s",
                    core.actor_id, subject, exc,
                )
        if published:
            logger.info(
                "source_actor.published actor_id=%s count=%d subject_root=legba.signals.%s.%s",
                core.actor_id, published, tenant, source_id,
            )


register_sink("document", SignalSink)


__all__ = [
    "SignalSink",
    "lookup_source_credibility",
    "write_canonical_signal",
]
