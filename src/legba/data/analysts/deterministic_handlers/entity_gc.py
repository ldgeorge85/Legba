# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``entity_gc`` sub-handler — L-203 migration of ``legba.maintenance.entity_gc``.

Entity garbage-collection family. No LLM. Seven operations:

  1. Mark entities with no signal_entity_links in 30d as ``gc_status=dormant``.
  2. Flag name-similar entity pairs (trigram similarity > 0.6) with
     co-occurring signals as ``duplicate_candidate``.
  3. Delete orphan signal_entity_links.
  4. Auto-pause sources with > 20 consecutive failed polls. The failure
     signal is the contiguous leading run of ``outcome='error'`` rows in
     ``source_poll_outcomes`` per ACTIVE ``source_descriptors`` head — there is
     NO ``sources`` table / ``consecutive_failures`` column (the original query
     hit a non-existent ``sources`` relation and logged
     ``source_pause_failed err=relation "sources" does not exist`` on EVERY run,
     D2). Pausing flips the head descriptor's lifecycle ``state`` 'active'→
     'paused' (mirroring ``discovered_materializer._pause_discovery``) and
     records the reason into ``body->>auto_paused_*``; the runtime actor loop
     observes the state change and stops polling.

     A source that is CURRENTLY PRODUCING is never paused by this leg. Two
     mechanisms, both in ``_consecutive_error_streaks``: a productive poll now
     writes an ``outcome='success'`` row (migration 0114) that BREAKS the
     leading error run, and — independently of the ledger — a source whose
     newest signal LANDED within ``_SOURCE_RECENT_SIGNAL_HOURS`` is skipped
     outright. The second exists because the ledger's pre-0114 rows are
     structurally wrong: productive polls wrote nothing, so a repaired source
     kept presenting a frozen historical error run and was re-latched off it
     (gdelt.files 2026-07-27, paused two minutes after its fix deployed;
     ukrinform / nasa.eonet before it).
  5. Quarantine orphan ``proposed_edges`` (D25) — pending edges whose
     ``source_entity`` / ``target_entity`` has no matching
     ``entity_profiles.canonical_name`` (the exact drift ``integrity_sweep``
     COUNTS as ``orphan_proposed_edges_source`` / ``orphan_proposed_edges_target``
     but never acts on). They can never promote into a CoOccursWith nexus
     (governance keys on canonical entities), so they accrete as permanently
     ``pending`` rows the sweep re-counts forever (406/678 and rising). We flip
     them to ``status='orphaned'`` — non-destructive, removes them from the
     governance ``status='pending'`` work-set, and clears the rising flag.
  6. (E5) Compact merged-entity edges — see the section banner near
     ``_compact_merged_edges`` below.
  7. Re-probe + auto-unpause sources that operation 4 auto-paused >= 24h ago
     (the "auto-unpause re-probe" queued hardening, MASTER_PLAN 2026-07-10
     ~L319). Operation 4's counting bug is already fixed (bound by the
     source's last PRODUCED signal, not raw poll-outcome rows — see
     ``_consecutive_error_streaks``'s ``last_signal`` docstring / T-4a), but a
     source auto-paused on a transient blip (the ukrinform / nasa.eonet
     cases: 8 days and unbounded lost to a 1-day upstream hiccup) had no way
     back except a manual repair — the latch never re-probed. This leg is the
     complementary self-heal: build the REAL source handler for each eligible
     paused row (the same ``build_source_handler`` factory + config-unwrap
     the production poll path already uses) and run its own cheap
     ``health_check`` — one bounded HTTP request per source, through the
     SSRF-guarded transport every source already fetches through
     (``legba.data.sources._egress``). A ``healthy`` result auto-unpauses;
     ``degraded`` / ``unhealthy`` / a probe exception leaves the source
     paused (re-tried again next tick — cheap + idempotent). Only rows
     carrying the ``auto_paused_*`` markers are eligible — an
     operator-paused or retired source is NEVER touched (see
     ``_reprobe_paused_sources``).

Output ``data`` keys:
    dormant_entities        int
    duplicate_flags         int
    orphan_edges            int
    sources_paused          int
    orphan_proposed_edges   int
    compacted_edges         int
    sources_unpaused        int
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from ...provenance.models import FindingPayload
from ...sources._contract import InMemoryStateStore, SourceContext
from ....runtime.analyst_method import AnalystMethodResult
from ....runtime.source_factory import build_source_handler

logger = logging.getLogger(__name__)


class _RawConfig(BaseModel):
    """Open BaseModel satisfying ``SourceContext.config`` (typed BaseModel).

    Local equivalent of ``legba.runtime.source_actor._RawConfig`` — that
    class is private (not in ``source_actor.__all__``) and importing it here
    would also pull a data->runtime dependency for a single one-line shape.
    Carries the raw (property-factory-WRAPPED) descriptor config UNCHANGED,
    exactly what the production runtime hands every source handler's
    ``ctx.config`` — most handlers ignore it (they read their own typed
    ``self._config``, set by ``build_source_handler`` at construction), but
    ACLED and UCDP re-parse ``ctx.config`` themselves inside ``health_check``
    and documented-expect this exact raw-passthrough shape."""

    model_config = ConfigDict(extra="allow")


_DORMANT_DAYS = 30
_DUP_TRIGRAM_THRESHOLD = 0.6
_DUP_MAX_PAIRS = 50
_SOURCE_FAILURE_THRESHOLD = 20
# How many recent poll-outcome rows per source the error-streak read pulls back.
# Must exceed _SOURCE_FAILURE_THRESHOLD so a qualifying leading error-run is
# never truncated by the LIMIT. Since migration 0114 a PRODUCTIVE poll writes an
# ``outcome='success'`` row, so the window holds successes too — which is the
# point: a success is what BREAKS the leading error run at its first occurrence.
_SOURCE_STREAK_WINDOW = max(_SOURCE_FAILURE_THRESHOLD + 5, 25)

# --- Op 4: the currently-producing guard -----------------------------------
# A source that landed a signal row in the substrate within this window is
# PRODUCING, and a producing source is not a failing source — it is never
# auto-paused, whatever its historical poll-outcome rows say.
#
# This is defence in depth, deliberately independent of the outcome ledger.
# Recording success (migration 0114) prevents NEW fossil error-runs, but the
# rows already on disk stay: gdelt.files carries ~102 leading 'error' rows from
# a four-day outage that was repaired. This guard neutralises them without
# anyone hand-deleting data — the repair that operators previously performed by
# hand, three times over, made structural.
#
# 48h matches the "firing" floor the system-status route already uses for the
# same question (v3_api.SourceFiringRow: "produced a signal within the last 48h
# → firing, regardless of any recent empty/error poll rows"), so the platform
# holds ONE definition of currently-producing. It is keyed on
# ``signals.created_at`` — when the row LANDED in the substrate — not
# ``fetched_at``, which is handler-supplied and can be back-dated by a bulk /
# archive loader (the same reason the status route keys on created_at).
_SOURCE_RECENT_SIGNAL_HOURS = 48

# --- Op 7: auto-unpause re-probe -------------------------------------------
# Eligibility floor — a source must have sat auto-paused for at least this
# long before it is even considered for a re-probe. This is deliberately NOT
# "probe every paused source every tick": a source that failed 20 consecutive
# polls a minute ago is almost certainly still down; re-probing immediately
# just burns a request for no gain. 24h matches the MASTER_PLAN spec ("hourly
# HEAD after 24h auto-paused") and comfortably exceeds any sane polling
# cadence (the fastest first-party cadence is minutes, not hours), so a
# healthy-again source is never stuck waiting more than one entity_gc cadence
# cycle (6h) beyond the 24h floor before its first re-probe opportunity.
_REPROBE_MIN_PAUSED_AGE = timedelta(hours=24)
# Cap on how many eligible sources get re-probed in a single entity_gc run.
# entity_gc's own cadence (every 6h, see analyst_entity_gc.yaml) already
# satisfies "hourly-or-better" for any source past the 24h floor (~4 probe
# opportunities per day once eligible); the cap exists so a future spike in
# simultaneously-auto-paused sources can't turn one GC tick into a serial
# fan-out of dozens of live HTTP probes. Re-probing is idempotent and cheap
# (one bounded request per source via that handler's own health_check), so a
# source that misses the cap this tick is simply picked up on the next one.
_REPROBE_MAX_PER_RUN = 10
# CONTENT-freshness window (2026-07-23 night diagnostics rider — the
# voa.africa counter-example: HTTP 200 + valid RSS + a ticking
# ``<lastBuildDate>`` while every actual item sat frozen for 16 months). A
# re-probe that gates on HTTP status alone would have resurrected voa.africa
# the moment its upstream 403 block lifted — then, because it has ZERO prior
# signals/cursor, its first post-unpause poll would have ingested all 20
# sixteen-month-old items as if they were fresh (the stale-backfill
# poisoning path: rss.py's since-filter only applies when a cursor already
# exists; source_actor.py's cursor-persistence). "Fresh" therefore requires
# the newest item to be within this window of NOW — 14 days comfortably
# covers every legitimate first-party cadence (the slowest scheduled polls
# are still sub-daily) while catching a content-dead syndication layer that
# merely stopped publishing weeks-to-months ago. Deliberately conservative:
# the false-negative cost (a source that IS back stays paused one more cycle,
# caught by the next automatic re-probe OR a manual operator look) is far
# cheaper than the false-positive cost (fossil-content ingestion poisoning
# the substrate with year-old "signals").
_REPROBE_FRESHNESS_WINDOW = timedelta(days=14)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


async def _mark_dormant(pool: Any) -> int:
    dormant = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ep.id
            FROM entity_profiles ep
            WHERE NOT EXISTS (
                SELECT 1 FROM signal_entity_links sel
                WHERE sel.entity_id = ep.id
                  AND sel.created_at > NOW() - INTERVAL '%d days'
            )
            AND ep.created_at < NOW() - INTERVAL '%d days'
            AND COALESCE(ep.data->>'gc_status', 'active') != 'dormant'
            """ % (_DORMANT_DAYS, _DORMANT_DAYS)
        )
        for row in rows:
            await conn.execute(
                """
                UPDATE entity_profiles SET
                    data = jsonb_set(
                        COALESCE(data, '{}'::jsonb),
                        '{gc_status}',
                        '"dormant"'
                    ),
                    updated_at = NOW()
                WHERE id = $1
                """,
                row["id"],
            )
            dormant += 1
    return dormant


async def _flag_duplicates(pool: Any) -> int:
    flagged = 0
    async with pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        except Exception:
            logger.debug("entity_gc.pg_trgm_unavailable")
            return 0
        rows = await conn.fetch(
            """
            SELECT DISTINCT
                a.id AS id_a, a.canonical_name AS name_a,
                b.id AS id_b, b.canonical_name AS name_b,
                similarity(LOWER(a.canonical_name), LOWER(b.canonical_name)) AS sim
            FROM entity_profiles a
            JOIN entity_profiles b ON a.id < b.id
            WHERE similarity(LOWER(a.canonical_name), LOWER(b.canonical_name)) > $1
              AND a.entity_type = b.entity_type
              AND COALESCE(a.data->>'gc_status', 'active') != 'dormant'
              AND COALESCE(b.data->>'gc_status', 'active') != 'dormant'
              AND COALESCE(a.data->>'duplicate_candidate', 'false') != 'true'
            LIMIT $2
            """,
            _DUP_TRIGRAM_THRESHOLD, _DUP_MAX_PAIRS,
        )
        for row in rows:
            cooc = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                    SELECT sel_a.signal_id
                    FROM signal_entity_links sel_a
                    JOIN signal_entity_links sel_b
                      ON sel_a.signal_id = sel_b.signal_id
                    WHERE sel_a.entity_id = $1
                      AND sel_b.entity_id = $2
                    LIMIT 1
                ) sub
                """,
                row["id_a"], row["id_b"],
            )
            if cooc and cooc > 0:
                await conn.execute(
                    """
                    UPDATE entity_profiles SET
                        data = jsonb_set(
                            jsonb_set(
                                COALESCE(data, '{}'::jsonb),
                                '{duplicate_candidate}',
                                '"true"'
                            ),
                            '{duplicate_of}',
                            to_jsonb($2::text)
                        ),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    row["id_b"], str(row["id_a"]),
                )
                flagged += 1
    return flagged


async def _clean_orphan_edges(pool: Any) -> int:
    removed = 0
    async with pool.acquire() as conn:
        for sql in (
            """
            DELETE FROM signal_entity_links sel
            WHERE NOT EXISTS (
                SELECT 1 FROM entity_profiles ep
                WHERE ep.id = sel.entity_id
            )
            """,
            """
            DELETE FROM signal_entity_links sel
            WHERE EXISTS (
                SELECT 1 FROM entity_profiles ep
                WHERE ep.id = sel.entity_id
                  AND ep.data->>'gc_status' = 'merged'
            )
            """,
        ):
            result = await conn.execute(sql)
            removed += int(result.split()[-1]) if result else 0
    return removed


async def _quarantine_orphan_proposed_edges(pool: Any) -> int:
    """D25 — flip orphan ``pending`` proposed_edges to ``status='orphaned'``.

    An orphan edge is one whose ``source_entity`` OR ``target_entity`` has no
    matching ``entity_profiles.canonical_name`` — exactly the rows
    ``integrity_sweep`` COUNTS (``orphan_proposed_edges_source`` /
    ``orphan_proposed_edges_target``). Such an edge can never be promoted by
    ``proposed_edge_governance`` (which keys on canonical entities), so it sits
    ``pending`` forever and the sweep re-counts it every hour.

    We only touch rows still ``status='pending'`` so we never disturb already-
    promoted/rejected/orphaned edges or the supersession history. ``orphaned``
    is a NEW terminal status outside the governance ``pending`` work-set —
    non-destructive (the row is retained for audit, not deleted).

    W3-A — THE TOMBSTONE HOLE. The existence probe used to be a bare
    ``ep.canonical_name = pe.source_entity``, which is satisfied by a MERGED
    TOMBSTONE: an endpoint naming an entity the GC merged away still "existed",
    so the row was neither quarantined nor (before
    :func:`_repoint_proposed_edges`) re-pointed, and it sat ``pending`` forever
    while the sweep re-counted it every hour.

    The probe now asks the question the caller actually means — "does this name
    reach a LIVE entity?" — by chasing ``resolve_entity`` (0086, cycle-safe) to
    the terminal survivor and requiring THAT to be un-merged. Note what this
    deliberately does NOT do: an endpoint naming a tombstone whose keeper is
    live still resolves, so it is NOT quarantined. Those rows belong to the
    re-point path, which preserves the candidate; quarantining them would
    destroy a promotable edge to fix a bookkeeping error. Only a name that
    reaches no live entity at all — unknown, or a degenerate chain that
    dead-ends in another tombstone — is orphaned."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE proposed_edges pe
               SET status = 'orphaned', reviewed_at = now()
             WHERE pe.status = 'pending'
               AND (
                   NOT EXISTS (
                       SELECT 1 FROM entity_profiles ep
                       JOIN entity_profiles k
                         ON k.id = public.resolve_entity(ep.id)
                       WHERE ep.canonical_name = pe.source_entity
                         AND k.merged_into IS NULL
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM entity_profiles ep
                       JOIN entity_profiles k
                         ON k.id = public.resolve_entity(ep.id)
                       WHERE ep.canonical_name = pe.target_entity
                         AND k.merged_into IS NULL
                   )
               )
            """
        )
    return int(result.split()[-1]) if result else 0


def _consecutive_error_streaks(
    rows: Any,
    *,
    threshold: int,
    now: datetime | None = None,
    recent_signal_hours: int = _SOURCE_RECENT_SIGNAL_HOURS,
) -> list[tuple[str, int]]:
    """Pure per-source consecutive-``error``-poll decision — no DB, unit-testable.

    ``rows``: iterable of mappings carrying ``source_id``, ``outcome``
    (``'error'`` | ``'empty'`` | ``'success'``), ``occurred_at`` (tz-aware
    datetime) and (optionally) ``last_signal`` (tz-aware datetime — the source's
    newest produced signal) and ``last_ingest`` (tz-aware datetime — when that
    source's newest signal row LANDED in the substrate), already grouped per
    source and ordered NEWEST-FIRST (the SQL guarantees this, mirroring the
    liveness watchdog's empty-streak read). For each source, count the contiguous
    LEADING run of ``outcome='error'`` rows; the run breaks on the first non-error
    row — an ``'empty'`` outcome or, since migration 0114, a ``'success'`` one.
    Returns ``(source_id, streak_len)`` for every source whose leading error run
    is ``>= threshold``.

    THREE independent things stop a source being latched, in the order they are
    applied. Each exists because a previous single mechanism proved insufficient:

    1. **Currently-producing guard** (``last_ingest`` within
       ``recent_signal_hours`` of ``now``): the source is producing RIGHT NOW,
       so it is not failing, and NOTHING in its poll history can pause it. This
       is the only rule that does not depend on the outcome ledger being
       correct, which is why it is first: the ledger's historical rows are known
       to be wrong (before migration 0114 a productive poll wrote no row at all,
       so a repaired source's leading run stayed frozen at its old failures —
       gdelt.files 2026-07-27 was auto-paused two minutes after its fix landed,
       reading a streak that was already history, and its ~102 error rows are
       still the leading run today). ``last_ingest`` is substrate landing time
       (``signals.created_at``), NOT the handler-supplied ``fetched_at``, so a
       bulk/archive loader that back-dates its rows cannot defeat it.

    2. **A 'success' row breaks the run** (migration 0114): the first productive
       poll after a repair writes ``outcome='success'``, which is a non-error
       row, which ends the leading run for good. This is the root-cause fix —
       guard (1) neutralises the fossil rows already on disk, this stops new
       ones forming.

    3. **Stale-error bound** (T-4a — mirrors
       ``liveness_watchdog._evaluate_empty_streaks``): an error poll at or before
       ``last_signal`` is stale evidence and stops the count. Retained: it still
       covers every pre-0114 row, and it catches the case where a source
       produced longer ago than ``recent_signal_hours`` yet its error rows are
       older still.

    When ``last_signal`` / ``last_ingest`` are absent (older callers / a source
    that has never produced), behavior is unchanged: the contiguous error run
    alone decides — a source that keeps ERRORING and never produced is exactly
    what auto-pause is for."""
    if threshold <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(hours=max(0, int(recent_signal_hours)))
    by_source: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in rows:
        sid = r.get("source_id")
        if not sid:
            continue
        if sid not in by_source:
            by_source[sid] = []
            order.append(sid)
        by_source[sid].append(r)
    failing: list[tuple[str, int]] = []
    for sid in order:
        outcomes = by_source[sid]
        # All rows carry the same per-source last_signal; read it off the first.
        last_signal = outcomes[0].get("last_signal") if outcomes else None
        # Guard 1 — currently producing ⇒ never paused, whatever the history.
        last_ingest = outcomes[0].get("last_ingest") if outcomes else None
        if isinstance(last_ingest, datetime) and last_ingest >= recent_cutoff:
            continue
        streak = 0
        for r in outcomes:
            if (r.get("outcome") or "") != "error":
                break
            occurred = r.get("occurred_at")
            # Stale-error bound: an error at/before the newest produced signal is
            # not part of the current run (the source produced after it) — stop.
            if (
                last_signal is not None
                and occurred is not None
                and occurred <= last_signal
            ):
                break
            streak += 1
        if streak >= threshold:
            failing.append((sid, streak))
    return failing


async def _pause_failing_sources(pool: Any) -> int:
    """Auto-pause ACTIVE sources with a leading run of >= threshold ``error``
    polls.

    There is NO ``sources`` table — the original query hit a non-existent
    ``sources`` relation (D2). The failure signal lives in
    ``source_poll_outcomes`` (one row per poll, ``outcome`` in ``'empty'`` /
    ``'error'`` / ``'success'``); a source is a descriptor in
    ``source_descriptors`` (head row keyed on ``descriptor_id`` + ``is_head``,
    lifecycle in ``state``, metadata in ``body`` jsonb). We read the last
    ``_SOURCE_STREAK_WINDOW`` poll-outcomes per ACTIVE head source, compute the
    contiguous leading ``error`` run in pure Python, and flip ``state``
    'active'→'paused' (recording the reason in ``body``) for any source over
    threshold.

    Two per-source recency columns ride along, and they are NOT the same fact:
    ``last_signal`` is ``max(signals.fetched_at)`` (handler-supplied logical
    time — the stale-error bound), while ``last_ingest`` is
    ``max(signals.created_at)`` (when the row landed in the substrate — the
    currently-producing guard). A bulk/archive loader can back-date
    ``fetched_at``; it cannot back-date ``created_at``. See
    :func:`_consecutive_error_streaks`."""
    paused = 0
    window = int(_SOURCE_STREAK_WINDOW)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT po.source_id   AS source_id,
                   po.outcome     AS outcome,
                   po.occurred_at AS occurred_at,
                   (SELECT max(s.fetched_at)
                      FROM signals s
                     WHERE s.source_id = d.descriptor_id) AS last_signal,
                   (SELECT max(s.created_at)
                      FROM signals s
                     WHERE s.source_id = d.descriptor_id) AS last_ingest
            FROM source_descriptors d
            JOIN LATERAL (
                SELECT source_id, outcome, occurred_at
                FROM source_poll_outcomes
                WHERE source_id = d.descriptor_id
                ORDER BY occurred_at DESC
                LIMIT {window}
            ) po ON TRUE
            WHERE d.is_head AND d.state = 'active'
            ORDER BY po.source_id, po.occurred_at DESC
            """
        )
        for source_id, streak in _consecutive_error_streaks(
            [dict(r) for r in rows], threshold=_SOURCE_FAILURE_THRESHOLD
        ):
            reason = (
                f"Exceeded {_SOURCE_FAILURE_THRESHOLD} consecutive failed polls "
                f"({streak} error outcomes)"
            )
            await conn.execute(
                """
                UPDATE source_descriptors SET
                    body = jsonb_set(
                        jsonb_set(
                            COALESCE(body, '{}'::jsonb),
                            '{auto_paused_at}',
                            to_jsonb($2::text)
                        ),
                        '{auto_paused_reason}',
                        to_jsonb($3::text)
                    ),
                    state = 'paused'
                WHERE descriptor_id = $1 AND is_head
                """,
                source_id,
                datetime.now(timezone.utc).isoformat(),
                reason,
            )
            paused += 1
    return paused


async def _fetch_reprobe_candidates(pool: Any, *, min_age: Any, limit: int) -> list[dict[str, Any]]:
    """Read auto-paused head descriptors eligible for a re-probe.

    Eligibility is narrow and deliberate — only rows carrying BOTH markers
    op-4 writes (``auto_paused_at`` present, ``auto_paused_reason`` present)
    qualify, so an operator-paused or retired source (which never gets those
    keys) is never touched by this leg. ``auto_paused_at`` must be at least
    ``min_age`` old — a source paused 5 minutes ago is not re-probed on the
    very next tick. Oldest-paused-first ordering + the caller's ``limit``
    means a backlog of eligible sources drains fairly across runs rather than
    the same head-of-list rows starving the tail forever."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT descriptor_id,
                   kind,
                   body AS body,
                   (body->>'auto_paused_at')::timestamptz AS auto_paused_at
            FROM source_descriptors
            WHERE is_head
              AND state = 'paused'
              AND body ? 'auto_paused_at'
              AND body ? 'auto_paused_reason'
              AND (body->>'auto_paused_at')::timestamptz <= (now() - $1::interval)
            ORDER BY (body->>'auto_paused_at')::timestamptz ASC
            LIMIT $2
            """,
            min_age,
            limit,
        )
    return [dict(r) for r in rows]


def _parse_signal_published_at(sig: Any) -> datetime | None:
    """Best-effort extraction of a yielded ``Signal``'s own content date.

    ``payload["published_at"]`` (an ISO-8601 string or ``None``) is the
    near-universal convention every first-party source handler writes onto
    each ``Signal`` it yields (rss / json_api / telegram / discord /
    mediacloud / acled / ucdp / opensanctions / gdelt / common_crawl /
    intelmq / firecrawl — grep-verified). This is the item's OWN date (an RSS
    entry's ``<pubDate>``, a telegram message's ``date``, ...) — NEVER a
    feed/response-level field like RSS's ``<lastBuildDate>`` or an HTTP
    ``Last-Modified`` header, both of which a dead syndication layer can keep
    ticking "today" while every actual item is frozen (the voa.africa case:
    HTTP 200, valid RSS 2.0, ``<lastBuildDate>`` claims today, all 20
    ``<pubDate>``s frozen at 2025-03). A missing / unparseable value returns
    ``None`` — the caller treats that as "not fresh", never as "fresh"."""
    payload = getattr(sig, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("published_at")
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _probe_content_freshness(
    *,
    handler: Any,
    ctx: SourceContext,
    auto_paused_at: datetime | None,
    max_probe_signals: int = 50,
) -> datetime | None:
    """Pull from the handler for real and return the newest item date seen.

    Rider (2026-07-23 night diagnostics, voa.africa): an HTTP-status-only
    gate is provably wrong — voa.africa answers HTTP 200 with a well-formed,
    current-looking RSS document (``<lastBuildDate>`` ticks "today") while
    its actual content (every ``<pubDate>``) has been frozen for 16 months.
    ``health_check()`` alone (status/connectivity) would have resurrected it.

    This calls the SAME ``pull()`` the production poll loop drives — the one
    code path that actually parses item-level content — against a THROWAWAY
    ``InMemoryStateStore`` (never persisted back to the real cursor; any
    ``state_store.set()`` the handler does inside ``pull()`` vanishes with
    this object) and a bare ``since=None`` (no since-filtering — we want
    whatever the endpoint currently serves, not a pre-filtered slice, so a
    fossil feed's fossil items are actually visible to inspect rather than
    silently dropped by the handler's own recency filter). Reads at most
    ``max_probe_signals`` from the generator before closing it early — this
    is still a BOUNDED read (a feed typically returns 20-50 items total; we
    never drain an unbounded/paginated source like telegram's catch-up walk).

    Returns the MAX parsed ``published_at`` across every signal seen (via
    :func:`_parse_signal_published_at`), or ``None`` when nothing yielded or
    nothing yielded a parseable date — the caller treats ``None`` exactly
    like "not fresh"."""
    newest: datetime | None = None
    seen = 0
    agen = handler.pull(ctx, since=None)
    try:
        async for sig in agen:
            seen += 1
            dt = _parse_signal_published_at(sig)
            if dt is not None and (newest is None or dt > newest):
                newest = dt
            if seen >= max_probe_signals:
                break
    finally:
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
    return newest


async def _probe_source_health(
    *, descriptor_id: str, kind: str, body: Mapping[str, Any], deps: Any,
) -> dict[str, Any]:
    """Run ONE cheap content-freshness probe for a paused source's own kind.

    Mirrors the production poll path's construction step (``source_actor.
    SourceActor._make_context`` / ``_build_handler``) at the level this
    deterministic leg can reach without a live actor:

      * ``handler`` — built via the SAME ``build_source_handler`` factory the
        runtime's poll loop uses. It unwraps the descriptor's property-factory
        config shapes internally and threads ``secrets_resolve`` for the
        kinds that need vault credentials (telegram / mediacloud / acled /
        opensanctions / firecrawl / discord); most handlers then read their
        OWN typed, already-unwrapped ``self._config`` at probe time.
      * ``ctx.config`` — a couple of kinds (ACLED, UCDP) don't cache a typed
        config at construction and instead re-parse ``ctx.config`` inside
        their own handler methods, documented on both as expecting "the
        runtime's raw passthrough" — the property-factory-WRAPPED dict, one
        open model wrapping ``body['config']`` UNCHANGED (mirrors
        ``source_actor._RawConfig`` exactly; a local equivalent, not an
        import, to avoid a data->runtime import edge for one open BaseModel).

    Then it runs the CONTENT-level probe (:func:`_probe_content_freshness`,
    ``pull()`` against a throwaway state store) rather than the shallow
    ``health_check()`` — an HTTP-status-only gate is the exact class of bug
    the voa.africa counter-example demonstrated (200 + valid feed + every
    item 16 months stale). Every kind's ``pull()`` still runs through the
    SSRF-guarded transport (``legba.data.sources._egress.
    guarded_async_client``) every source fetches through — this function does
    not reimplement a fetcher; it invokes the one the source's own handler
    already carries, then reads the CONTENT the handler parsed out of it.

    Returns ``{"fresh": bool, "newest_item_at": datetime | None,
    "reason": str}``. ``fresh`` requires ALL of: the pull raised no
    exception, at least one signal carried a parseable ``published_at``, that
    date is newer than ``auto_paused_at`` (proves the source moved AFTER it
    went dark — not just "slightly less ancient"), AND within
    ``_REPROBE_FRESHNESS_WINDOW`` of now (proves it is not merely old-but-
    newer-than-pause). Any failure to prove freshness — including "the probe
    itself failed" — resolves to ``fresh=False``: the false-negative cost is
    a later manual look; the false-positive cost is fossil-content ingestion
    (an unpaused, cursor-less source's FIRST poll would ingest every stale
    item it just proved exists, per the voa.africa stale-backfill mechanics
    traced to ``rss.py`` / ``source_actor.py``)."""
    raw_config: dict[str, Any] = dict((body or {}).get("config") or {})
    secrets_resolve = getattr(deps, "secrets_resolve", None)
    auto_paused_at = body.get("auto_paused_at") if isinstance(body, Mapping) else None
    if isinstance(auto_paused_at, str):
        try:
            auto_paused_at = datetime.fromisoformat(auto_paused_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            auto_paused_at = None
    if isinstance(auto_paused_at, datetime) and auto_paused_at.tzinfo is None:
        auto_paused_at = auto_paused_at.replace(tzinfo=timezone.utc)
    if not isinstance(auto_paused_at, datetime):
        auto_paused_at = None

    try:
        handler = build_source_handler(
            kind, raw_config, secrets_resolve=secrets_resolve,
        )
        ctx = SourceContext(
            target_id=descriptor_id,
            target_version=str(((body or {}).get("identity") or {}).get("version", "")),
            source_id=descriptor_id,
            config=_RawConfig(**raw_config),
            state_store=InMemoryStateStore(),
            secrets_resolve=secrets_resolve,
        )
        newest = await _probe_content_freshness(
            handler=handler, ctx=ctx, auto_paused_at=auto_paused_at,
        )
    except Exception as exc:  # noqa: BLE001 — a failed probe just stays paused
        logger.info(
            "entity_gc.reprobe.probe_failed descriptor_id=%s kind=%s err=%r",
            descriptor_id, kind, exc,
        )
        return {"fresh": False, "newest_item_at": None, "reason": f"probe_error: {exc!r}"}

    if newest is None:
        return {
            "fresh": False, "newest_item_at": None,
            "reason": "no_parseable_item_dates",
        }
    now = datetime.now(timezone.utc)
    within_window = newest >= now - _REPROBE_FRESHNESS_WINDOW
    newer_than_pause = auto_paused_at is None or newest > auto_paused_at
    if within_window and newer_than_pause:
        return {"fresh": True, "newest_item_at": newest, "reason": "fresh"}
    reason = (
        "stale_content"
        if not within_window
        else "not_newer_than_auto_pause"
    )
    return {"fresh": False, "newest_item_at": newest, "reason": reason}


async def _reprobe_paused_sources(pool: Any, deps: Any) -> int:
    """Op 7 — re-probe + auto-unpause sources op-4 auto-paused >= 24h ago.

    Reads eligible candidates (see ``_fetch_reprobe_candidates``), probes each
    for CONTENT FRESHNESS (see ``_probe_source_health`` — gates on the
    newest item's own date, never on HTTP status alone; see the voa.africa
    counter-example in its docstring), and for every fresh result mirrors the
    op-4 pause write IN REVERSE — the exact same out-of-band mutation shape
    (a direct ``state`` column flip + a ``body`` jsonb key strip on the head
    row), never an API PUT. This is load-bearing: descriptor state is
    normally carried via content-hash, but op-4's pause write bypasses that
    (direct row-state write + body-field write, not a re-registration), so an
    API PUT would either no-op against the stale hash or head-shift back to
    the pre-pause version and silently UNDO the unpause. The 07-23 manual
    repair (`UPDATE source_descriptors SET state='active', body=(body-
    'auto_paused_at')-'auto_paused_reason' WHERE descriptor_id=... AND
    is_head`) is exactly this shape — this function automates it."""
    unpaused = 0
    candidates = await _fetch_reprobe_candidates(
        pool, min_age=_REPROBE_MIN_PAUSED_AGE, limit=_REPROBE_MAX_PER_RUN,
    )
    for row in candidates:
        descriptor_id = row["descriptor_id"]
        kind = row["kind"]
        body = row.get("body") or {}
        probe = await _probe_source_health(
            descriptor_id=descriptor_id, kind=kind, body=body, deps=deps,
        )
        if not probe["fresh"]:
            # Newest-item date logged even on a stay-paused verdict — an
            # operator reading this line sees WHY (per the coordinator rider):
            # a frozen-content source shows its fossil date every re-probe
            # tick, distinguishing "still genuinely down" from "content dead".
            logger.info(
                "entity_gc.reprobe.still_down descriptor_id=%s kind=%s "
                "reason=%s newest_item_at=%s paused_since=%s",
                descriptor_id, kind, probe["reason"], probe["newest_item_at"],
                row.get("auto_paused_at"),
            )
            continue
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE source_descriptors SET
                    body = (body - 'auto_paused_at') - 'auto_paused_reason',
                    state = 'active'
                WHERE descriptor_id = $1
                  AND is_head
                  AND state = 'paused'
                  AND body ? 'auto_paused_at'
                """,
                descriptor_id,
            )
        # A guarded UPDATE — the WHERE re-checks state/markers at write time
        # so a concurrent operator action (manual pause/retire) between the
        # read above and this write can never be clobbered by a stale probe.
        if result and result.split()[-1] == "1":
            unpaused += 1
            logger.info(
                "entity_gc.reprobe.auto_unpaused descriptor_id=%s kind=%s "
                "newest_item_at=%s paused_since=%s",
                descriptor_id, kind, probe["newest_item_at"],
                row.get("auto_paused_at"),
            )
    return unpaused


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# E5 — compaction: re-point a merged loser's OPEN nexus/fact endpoints onto the
# keeper's canonical_name. merge_pair (entity_researcher) only sets merged_into
# + folds aliases; the graph edges still carry the loser surface. This bounded,
# idempotent sweep re-points them (table-only — the AGE graph is not a load-
# bearing consumer) so structural_balance / graph_mining / the read port see a
# consistent graph. Collisions with an existing open keeper triple are CLOSED
# (superseded), never a unique-index violation. It GATES flipping the researcher
# to apply-mode.
# ---------------------------------------------------------------------------


async def _repoint_nexuses(conn: Any, loser: str, keeper: str) -> int:
    """Re-point OPEN nexus endpoints from ``loser`` surface to ``keeper``. A
    re-pointed self-loop (subject==object) is closed; a collision with an
    existing open keeper triple closes the loser-derived row (superseded_by the
    survivor) instead of violating idx_nexuses_triple_open. Returns rows touched."""
    rows = await conn.fetch(
        """
        SELECT id, subject, object, intermediary, rel_type
          FROM nexuses
         WHERE valid_until IS NULL AND superseded_by IS NULL
           AND (lower(subject) = lower($1)
                OR lower(object) = lower($1)
                OR lower(COALESCE(intermediary, '')) = lower($1))
        """,
        loser,
    )
    lo = loser.lower()
    touched = 0
    for row in rows:
        new_s = keeper if (row["subject"] or "").lower() == lo else row["subject"]
        new_o = keeper if (row["object"] or "").lower() == lo else row["object"]
        new_i = keeper if (row["intermediary"] or "").lower() == lo else row["intermediary"]
        if new_s and new_o and new_s.lower() == new_o.lower():
            # a re-pointed self-loop has no meaning — close it (no survivor).
            await conn.execute(
                "UPDATE nexuses SET valid_until = now(), updated_at = now() "
                "WHERE id = $1", row["id"])
            touched += 1
            continue
        collide = await conn.fetchval(
            """
            SELECT id FROM nexuses
             WHERE valid_until IS NULL AND superseded_by IS NULL AND id <> $5
               AND lower(subject) = lower($1)
               AND lower(COALESCE(intermediary, '')) = lower(COALESCE($2, ''))
               AND lower(object) = lower($3)
               AND lower(rel_type) = lower($4)
             LIMIT 1
            """,
            new_s, new_i, new_o, row["rel_type"], row["id"],
        )
        if collide is not None:
            await conn.execute(
                "UPDATE nexuses SET valid_until = now(), superseded_by = $2, "
                "updated_at = now() WHERE id = $1", row["id"], collide)
        else:
            await conn.execute(
                "UPDATE nexuses SET subject = $2, object = $3, intermediary = $4, "
                "updated_at = now() WHERE id = $1", row["id"], new_s, new_o, new_i)
        touched += 1
    return touched


#: Terminal status for a candidate whose endpoints, once re-pointed onto their
#: merge keeper, turn out to name an edge another candidate already carries.
#: A NEW terminal status outside the governance ``pending`` work-set, added the
#: same way ``orphaned`` was: the row is retained for audit, never deleted, and
#: its evidence is folded onto the survivor first so nothing is lost.
PROPOSED_EDGE_MERGED_STATUS = "merged"


async def _repoint_proposed_edges(conn: Any, loser: str, keeper: str) -> int:
    """Re-point `proposed_edges` endpoints from ``loser`` onto ``keeper``.

    THE GAP THIS CLOSES. ``_compact_merged_edges`` has always re-pointed
    `nexuses` and `facts` and has NEVER touched `proposed_edges`, so every
    candidate naming a merged loser keeps naming a tombstone for good. Measured
    live 2026-08-03: 13,222 rows name a tombstone, 5,844 still ``pending``.

    The cost is not cosmetic. The candidate queue is keyed on
    ``uq_proposed_edges_triple`` (lower(source), lower(target), rel_type), so
    the same real pair sits in the queue TWICE — once under the loser surface
    and once under the keeper's — and both get accrued, qualified and typed.
    3,268 of those rows collide with a keeper-named row that already exists;
    that duplication is the re-spend, and folding it is the point of this
    function.

    Three outcomes per row, mirroring ``_repoint_nexuses``:
      * a re-pointed SELF-LOOP (both endpoints land on the keeper) is
        ``rejected`` — the same verdict ``proposed_edge_governance`` gives a
        self-referential candidate, so the queue and the promoter agree;
      * a COLLISION with an existing keeper-named candidate folds this row's
        evidence onto that survivor (confidence maxed, lineage unioned, the
        stronger status kept) and marks this row ``merged``. Folding before
        marking is what keeps a citation from being dropped;
      * otherwise the endpoints are re-pointed in place.

    Returns rows touched.
    """
    rows = await conn.fetch(
        """
        SELECT id, source_entity, target_entity, relationship_type, status,
               confidence, evidence_text, derived_from
          FROM proposed_edges
         WHERE lower(source_entity) = lower($1)
            OR lower(target_entity) = lower($1)
        """,
        loser,
    )
    lo = loser.lower()
    touched = 0
    for row in rows:
        new_s = keeper if (row["source_entity"] or "").lower() == lo \
            else row["source_entity"]
        new_t = keeper if (row["target_entity"] or "").lower() == lo \
            else row["target_entity"]
        if new_s and new_t and new_s.lower() == new_t.lower():
            await conn.execute(
                "UPDATE proposed_edges SET status = 'rejected', "
                "reviewed_at = now() WHERE id = $1 AND status <> 'rejected'",
                row["id"])
            touched += 1
            continue
        collide = await conn.fetchrow(
            """
            SELECT id, status, confidence FROM proposed_edges
             WHERE lower(source_entity) = lower($1)
               AND lower(target_entity) = lower($2)
               AND relationship_type = $3
               AND id <> $4
             LIMIT 1
            """,
            new_s, new_t, row["relationship_type"], row["id"],
        )
        if collide is not None:
            # Fold FIRST — the evidence has to reach the survivor before this
            # row leaves the work-set, or the merge silently costs a citation.
            # `pending` is the only status the governance sweep still works, so
            # it wins over any terminal one: an edge one of whose two rows is
            # still in play stays in play.
            await conn.execute(
                """
                UPDATE proposed_edges
                   SET confidence   = GREATEST(confidence, $2),
                       derived_from = COALESCE((SELECT array_agg(DISTINCT e)
                                       FROM unnest(derived_from || $3::uuid[]) e),
                                      '{}'::uuid[]),
                       evidence_text = CASE
                           WHEN evidence_text = '' THEN $4 ELSE evidence_text END,
                       status = CASE WHEN status = 'pending' OR $5 = 'pending'
                                     THEN 'pending' ELSE status END
                 WHERE id = $1
                """,
                collide["id"], float(row["confidence"] or 0.0),
                list(row["derived_from"] or []), row["evidence_text"] or "",
                row["status"],
            )
            await conn.execute(
                "UPDATE proposed_edges SET status = $2, reviewed_at = now() "
                "WHERE id = $1",
                row["id"], PROPOSED_EDGE_MERGED_STATUS)
        else:
            await conn.execute(
                "UPDATE proposed_edges SET source_entity = $2, "
                "target_entity = $3 WHERE id = $1",
                row["id"], new_s, new_t)
        touched += 1
    return touched


async def _repoint_facts(conn: Any, loser: str, keeper: str) -> int:
    """Re-point OPEN facts.subject from ``loser`` to ``keeper`` (the entity
    endpoint; ``value`` is often a literal and is left for a later pass). A
    collision on the open (subject,predicate,value,valid_from) index closes the
    loser fact. Returns rows touched."""
    rows = await conn.fetch(
        """
        SELECT id, predicate, value, valid_from FROM facts
         WHERE valid_until IS NULL AND superseded_by IS NULL
           AND lower(subject) = lower($1)
        """,
        loser,
    )
    touched = 0
    for row in rows:
        collide = await conn.fetchval(
            """
            SELECT id FROM facts
             WHERE valid_until IS NULL AND superseded_by IS NULL AND id <> $5
               AND lower(subject) = lower($1)
               AND lower(predicate) = lower($2)
               AND lower(COALESCE(value, '')) = lower(COALESCE($3, ''))
               AND COALESCE(valid_from, '1970-01-01 00:00:00+00'::timestamptz)
                   = COALESCE($4, '1970-01-01 00:00:00+00'::timestamptz)
             LIMIT 1
            """,
            keeper, row["predicate"], row["value"], row["valid_from"], row["id"],
        )
        if collide is not None:
            await conn.execute(
                "UPDATE facts SET valid_until = now(), superseded_by = $2, "
                "updated_at = now() WHERE id = $1", row["id"], collide)
        else:
            await conn.execute(
                "UPDATE facts SET subject = $2, updated_at = now() WHERE id = $1",
                row["id"], keeper)
        touched += 1
    return touched


async def _compact_merged_edges(pool: Any, batch_limit: int = 200) -> int:
    """For merged-loser tombstones not yet compacted, re-point their OPEN nexus/
    fact endpoints onto the keeper canonical_name, then mark the loser compacted
    (``data.merge.compacted_at`` — the forward-progress gate). Bounded per run;
    idempotent; degrade-not-break per loser (one bad loser can't sink the sweep)."""
    total = 0
    async with pool.acquire() as conn:
        losers = await conn.fetch(
            """
            -- Resolve to the TERMINAL survivor, not the immediate merged_into
            -- parent (adversarial-review HIGH): in a chain L->K->K2 (K itself
            -- later merged), the direct parent K is a tombstone — re-pointing
            -- L's edges onto K's dead surface would strand them. resolve_entity
            -- (0086, cycle-safe) chases to the live keeper. The k.merged_into IS
            -- NULL guard defers a loser whose terminal is (degenerately) still a
            -- tombstone, so it settles after the chain collapses (never strands).
            SELECT l.id, l.canonical_name AS loser, k.canonical_name AS keeper
              FROM entity_profiles l
              JOIN entity_profiles k ON k.id = public.resolve_entity(l.merged_into)
             WHERE l.merged_into IS NOT NULL
               AND k.merged_into IS NULL
               AND NOT (COALESCE(l.data->'merge', '{}'::jsonb) ? 'compacted_at')
             LIMIT $1
            """,
            int(batch_limit),
        )
        for r in losers:
            loser, keeper = str(r["loser"] or ""), str(r["keeper"] or "")
            try:
                async with conn.transaction():
                    if loser and keeper and loser.lower() != keeper.lower():
                        total += await _repoint_nexuses(conn, loser, keeper)
                        total += await _repoint_facts(conn, loser, keeper)
                        # The third name-keyed edge surface. Omitted since this
                        # sweep was written, which is why 13,222 candidates
                        # still name a tombstone and 3,268 real pairs sit in
                        # the queue twice.
                        total += await _repoint_proposed_edges(
                            conn, loser, keeper)
                    await conn.execute(
                        """
                        UPDATE entity_profiles
                           SET data = jsonb_set(
                                 jsonb_set(COALESCE(data, '{}'::jsonb), '{merge}',
                                           COALESCE(data->'merge', '{}'::jsonb), true),
                                 '{merge,compacted_at}', to_jsonb(now()::text), true),
                               updated_at = now()
                         WHERE id = $1
                        """,
                        r["id"],
                    )
            except Exception as exc:  # pragma: no cover - degrade-not-break
                logger.warning("entity_gc.compact_failed loser=%r err=%s", loser, exc)
    return total


def _build_finding(
    *,
    dormant_entities: int,
    duplicate_flags: int,
    orphan_edges: int,
    sources_paused: int,
    orphan_proposed_edges: int = 0,
    compacted_edges: int = 0,
    sources_unpaused: int = 0,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Entity GC: {dormant_entities} dormant, {duplicate_flags} duplicates, "
        f"{orphan_edges} orphan edges, {sources_paused} sources paused, "
        f"{orphan_proposed_edges} orphan proposed-edges quarantined, "
        f"{compacted_edges} merged-edge re-points, "
        f"{sources_unpaused} sources re-probed fresh + auto-unpaused"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"dormant_entities={dormant_entities}",
        f"duplicate_flags={duplicate_flags}",
        f"orphan_edges={orphan_edges}",
        f"sources_paused={sources_paused}",
        f"orphan_proposed_edges={orphan_proposed_edges}",
        f"compacted_edges={compacted_edges}",
        f"sources_unpaused={sources_unpaused}",
    ])
    tags = ["deterministic", "entity_gc"]
    if (
        dormant_entities
        or duplicate_flags
        or orphan_edges
        or sources_paused
        or orphan_proposed_edges
        or compacted_edges
        or sources_unpaused
    ):
        tags.append("gc_actions_taken")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "entity_gc",
            "dormant_entities": dormant_entities,
            "duplicate_flags": duplicate_flags,
            "orphan_edges": orphan_edges,
            "sources_paused": sources_paused,
            "orphan_proposed_edges": orphan_proposed_edges,
            "compacted_edges": compacted_edges,
            "sources_unpaused": sources_unpaused,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring."""
    dormant = 0
    duplicates = 0
    orphans = 0
    paused = 0
    orphan_proposed = 0
    compacted = 0
    unpaused = 0

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        run_dormant = bool(options.get("run_dormant", True))
        run_duplicates = bool(options.get("run_duplicates", True))
        run_orphans = bool(options.get("run_orphans", True))
        run_pause = bool(options.get("run_source_pause", True))
        run_orphan_edges = bool(options.get("run_orphan_proposed_edges", True))
        run_compaction = bool(options.get("run_compaction", True))
        run_reprobe = bool(options.get("run_source_reprobe", True))
        if run_dormant:
            try:
                dormant = await _mark_dormant(pool)
            except Exception as exc:
                logger.warning("entity_gc.dormant_failed err=%s", exc)
        if run_duplicates:
            try:
                duplicates = await _flag_duplicates(pool)
            except Exception as exc:
                logger.warning("entity_gc.duplicates_failed err=%s", exc)
        if run_orphans:
            try:
                orphans = await _clean_orphan_edges(pool)
            except Exception as exc:
                logger.warning("entity_gc.orphans_failed err=%s", exc)
        if run_pause:
            try:
                paused = await _pause_failing_sources(pool)
            except Exception as exc:
                logger.warning("entity_gc.source_pause_failed err=%s", exc)
        if run_orphan_edges:
            try:
                orphan_proposed = await _quarantine_orphan_proposed_edges(pool)
            except Exception as exc:
                logger.warning("entity_gc.orphan_proposed_edges_failed err=%s", exc)
        if run_compaction:
            try:
                compacted = await _compact_merged_edges(pool)
            except Exception as exc:
                logger.warning("entity_gc.compaction_failed err=%s", exc)
        if run_reprobe:
            try:
                unpaused = await _reprobe_paused_sources(pool, deps)
            except Exception as exc:
                logger.warning("entity_gc.source_reprobe_failed err=%s", exc)

    finding = _build_finding(
        dormant_entities=dormant,
        duplicate_flags=duplicates,
        orphan_edges=orphans,
        sources_paused=paused,
        orphan_proposed_edges=orphan_proposed,
        compacted_edges=compacted,
        sources_unpaused=unpaused,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
