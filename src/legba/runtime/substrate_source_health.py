# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-poll HEALTH — the ``get_source_health`` read, in its own module.

Split out of ``substrate_query_port.py`` when that
module crossed its size ceiling; the port keeps a thin delegating method with
the same signature, exactly as ``substrate_frame_reads`` carries
``list_situations`` / ``get_timeline`` / ``query_events``. Nothing here reaches
back into the port (one-way import), so the two cannot cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import source_staleness as _staleness
from ..data.intake_visibility import intake_counts
from .enrichment_timing import fetch_enrichment_last_poll

logger = logging.getLogger(__name__)

__all__ = ["get_source_health", "list_sources", "_FEED_EMPTY_SLACK"]

#: Mirrors ``substrate_query_port._MAX_ROW_LIMIT`` (the port's row cap on every
#: read); spelled here rather than imported because this module must never
#: import the port.
_MAX_ROW_LIMIT = 200

#: The body-extraction counters cover archive rows touched in this
#: many days (the rate is a current reading, and the window bounds the scan).
BODY_EXTRACTION_WINDOW_DAYS = 30

# get_source_health (CONSULT_REVIEW_2026-09-28 B.9): a source's newest
# OBSERVED upstream entry (``source_poll_outcomes.newest_entry_ts``) and its
# newest INGESTED signal (``signals.fetched_at``, via ``last_signal_at``) are
# stamped by different clocks (the upstream feed's own entry timestamps vs
# our fetch time), so an honestly-empty poll can land ``newest_entry_ts`` a
# hair after ``last_signal_at`` from clock skew / timestamp granularity
# alone. A small slack keeps that from misclassifying a quiet feed as a
# cursor fault.
_FEED_EMPTY_SLACK = _staleness.FEED_EMPTY_SLACK


async def get_source_health(
    pool: Any,
    *,
    silent_only: bool = False,
    silent_hours: int = 48,
    limit: int = _MAX_ROW_LIMIT,
    include_body_extraction: bool = True,
) -> dict[str, Any]:
    """Source-poll HEALTH — which feeds are quiet or erroring.

    Joins each head source descriptor to its most-recent ``signals`` time and
    its most-recent ``source_poll_outcomes`` row (migration 0046 + 0114: one
    row per poll, 'success' | 'gated' | 'empty' | 'error'; 'gated' =
    the keyword gate dropped every entry, migration 0226). Lets the journal tell "no
    coverage on X" apart from "a quiet feed" and narrate the platform's
    intake honestly. ``refs`` is empty (source rows are descriptors, not
    chip-linked substrate rows).

    The ``erroring`` aggregate below counts sources whose NEWEST outcome row
    is 'error'. Before 0114 that over-counted structurally: a productive
    poll wrote no row, so a source that failed once and then recovered kept
    reporting its old error as the newest outcome forever. Now the first
    productive poll writes 'success' and the count self-corrects.

    HEAD COVERAGE (W2-T6 / M4): the old ``LIMIT 40`` default scanned 40
    of ~48 active heads, so the row list (and its silent/error tallies)
    silently undercounted. The default now covers the whole fleet
    (clamped at ``_MAX_ROW_LIMIT``); rows were already staleness-ordered
    (silent-first), so any explicit smaller limit only sheds the
    freshest feeds — and the response states ``scanned`` /
    ``truncated`` explicitly. The H-1 ``summary`` block remains the
    denominator-honest whole-fleet aggregate.

    SILENT != BROKEN (CONSULT_REVIEW_2026-09-28 B.9): the flat
    ``silent_hours >= silent_hours`` cutoff alone cannot tell a genuinely
    quiet feed from a stalled/erroring one — on 2026-09-27 it reported
    25 reference feeds "silent 50-80h" that had all polled cleanly with
    outcome='empty' and nothing newer upstream. Each row now also carries
    ``newest_entry_ts`` (the poll's newest OBSERVED upstream entry) and
    ``feed_has_no_new_items`` (outcome='empty' and that observation is no
    newer than the source's last ingested signal, plus a small clock-skew
    slack); ``summary`` splits the whole-fleet ``active_stalled`` count
    into ``silent_feed_empty`` (honestly quiet) and
    ``silent_poll_error_or_none`` (erroring or no poll evidence at all —
    the actually-broken remainder).

    STALENESS (W3 SRC-2): each row also carries ``staleness`` —
    ``{state, factor, reason, ...}`` from :mod:`.source_staleness`, the ONE
    place "no new items for N× the feed's expected interval" is decided
    (``fresh`` / ``quiet`` / ``stale`` / ``dead``; ``unknown`` when nothing
    can be measured — never a 0). ``feed_has_no_new_items`` is an INPUT to it
    (it tells a quiet feed from an ingestion fault), and the liveness
    watchdog's tripwire reads these same rows, so there is no parallel truth.
    ``summary.staleness`` is the whole-scan histogram by state.

    BODY EXTRACTION: each row also carries ``body_extraction`` —
    ``{fetched, challenge_blocked, boilerplate_rejected, body_stored,
    body_extraction_rate}`` aggregated from the ``evidence_archive`` sidecar
    for that feed's signals, the same four counters the archiver's per-run
    receipt tallies, over archive rows touched in the last
    ``BODY_EXTRACTION_WINDOW_DAYS`` days (one grouped pass, not a per-feed
    lateral: the lateral form measured 3.6 s warm / 9 s cold against 0.6 s
    without it on the live table, 2026-11-05; the grouped, windowed pass
    adds ~0.2 s). ``include_body_extraction=False`` skips the aggregate and
    leaves ``body_extraction`` None — the staleness watchdog's tripwire
    never reads it. The rate is ``body_stored / fetched`` — the fraction of
    fetch attempts that landed a real stored body — and is ``None`` on an
    unfetched feed, never a 0 pretending to be a measurement. This is how
    "a publisher is challenging us" shows up BEFORE anyone reads a log:
    Times of Israel / Jerusalem Post sat at ~100% challenge_blocked / 0
    body_stored for 30 days (the defect this lane fixes).
    """
    clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
    if include_body_extraction:
        # The per-feed body-extraction counters, read off the SAME
        # evidence_archive sidecar the archiver's run receipt tallies (one
        # vocabulary, one place to audit). 'failed' rows sit in the fetched
        # denominator because a fetch WAS attempted; an archive row whose
        # last_error names the challenge tell was a BLOCK, and one naming
        # rejected_boilerplate was a boilerplate rejection — the closed
        # status CHECK has no word for either, so they are read off the
        # recorded error.
        be_cte = (
            "WITH be AS ( "
            "  SELECT es.source_id, "
            "    count(*) FILTER (WHERE ea.status IN "
            "      ('archived', 'blocked_challenge', 'failed', 'skipped_size')) AS fetched, "
            "    count(*) FILTER (WHERE ea.status = 'blocked_challenge' "
            "      OR (ea.status = 'failed' AND ea.last_error LIKE "
            "          'blocked_by_challenge:%')) AS challenge_blocked, "
            "    count(*) FILTER (WHERE ea.last_error LIKE "
            "      'rejected_boilerplate:%') AS boilerplate_rejected, "
            "    count(*) FILTER (WHERE ea.status = 'archived') AS body_stored "
            "  FROM evidence_archive ea "
            "  JOIN signals es ON es.id = ea.signal_id "
            f"  WHERE ea.updated_at >= now() - interval '{int(BODY_EXTRACTION_WINDOW_DAYS)} days' "
            "  GROUP BY es.source_id "
            ") "
        )
        be_cols = (
            "       be.fetched AS be_fetched, "
            "       be.challenge_blocked AS be_challenge_blocked, "
            "       be.boilerplate_rejected AS be_boilerplate_rejected, "
            "       be.body_stored AS be_body_stored "
        )
        be_join = "LEFT JOIN be ON be.source_id = s.descriptor_id "
    else:
        be_cte = ""
        be_cols = (
            "       NULL::bigint AS be_fetched, "
            "       NULL::bigint AS be_challenge_blocked, "
            "       NULL::bigint AS be_boilerplate_rejected, "
            "       NULL::bigint AS be_body_stored "
        )
        be_join = ""
    sql = (
        be_cte
        + "SELECT s.descriptor_id AS source_id, "
        "       s.body->'identity'->>'name' AS name, s.state, "
        "       sig.last_signal_at, po.outcome AS last_poll_outcome, "
        "       po.health_state AS last_health_state, "
        "       po.occurred_at AS last_poll_at, "
        "       po.newest_entry_ts AS last_poll_newest_entry_ts, "
        "       po.keyword_filtered AS last_poll_keyword_filtered, "
        "       ok.last_ok_poll_at, "
        "       s.body->'cadence'->'schedule'->>'raw' AS cadence_raw, "
        + be_cols
        + "FROM source_descriptors s "
        "LEFT JOIN LATERAL ( "
        "  SELECT max(fetched_at) AS last_signal_at FROM signals "
        "  WHERE source_id = s.descriptor_id "
        ") sig ON TRUE "
        "LEFT JOIN LATERAL ( "
        "  SELECT outcome, health_state, occurred_at, newest_entry_ts, "
        "         keyword_filtered "
        "  FROM source_poll_outcomes "
        "  WHERE source_id = s.descriptor_id "
        "  ORDER BY occurred_at DESC LIMIT 1 "
        ") po ON TRUE "
        "LEFT JOIN LATERAL ( "
        "  SELECT max(occurred_at) AS last_ok_poll_at "
        "  FROM source_poll_outcomes "
        "  WHERE source_id = s.descriptor_id AND outcome <> 'error' "
        ") ok ON TRUE "
        + be_join
        + "WHERE s.is_head = TRUE AND s.state = 'active' "
        "ORDER BY sig.last_signal_at ASC NULLS FIRST "
        f"LIMIT {clamped_limit}"
    )
    now = datetime.now(timezone.utc)
    silent_cutoff = now - timedelta(hours=silent_hours)
    async with pool.acquire() as conn:
        records = await conn.fetch(sql)
        # H-1 (MASTER_PLAN 2026-07-13, DENOMINATOR HONESTY): accurate aggregate
        # counts across ALL wired head sources — NOT derived from `rows` (which
        # lists only ACTIVE sources, capped at `limit`, so its silent/error
        # tallies undercount and it can never see paused/retired). The journal's
        # "all active feeds fresh" hid the denominator (a critique found the
        # honest shape is ~37 fresh / 48 active / 58 wired). These give the
        # journal the true "N fresh / M active / K wired" + the paused/retired
        # roster to name (apparatus quiet, not world silence).
        state_records = await conn.fetch(
            "SELECT state, count(*) AS n FROM source_descriptors "
            "WHERE is_head = TRUE GROUP BY state"
        )
        active_agg = await conn.fetchrow(
            "SELECT count(*) AS active_total, "
            "  count(*) FILTER (WHERE sig.last_signal_at IS NOT NULL "
            "    AND sig.last_signal_at >= $1) AS fresh, "
            "  count(*) FILTER (WHERE sig.last_signal_at IS NULL "
            "    OR sig.last_signal_at < $1) AS stalled, "
            "  count(*) FILTER (WHERE po.outcome = 'error') AS erroring, "
            # B.9: of the stalled (silent) heads, how many are honestly
            # empty (polled fine, upstream simply has nothing newer than
            # what we already ingested) vs erroring/never-polled — the
            # 09-27 25-feed misread conflated the two.
            "  count(*) FILTER (WHERE (sig.last_signal_at IS NULL "
            "      OR sig.last_signal_at < $1) "
            "    AND po.outcome = 'empty' AND po.newest_entry_ts IS NOT NULL "
            "    AND sig.last_signal_at IS NOT NULL "
            "    AND po.newest_entry_ts <= sig.last_signal_at + $2"
            "  ) AS silent_feed_empty, "
            # 0226: of the stalled heads, how many last polled GATED -- the
            # feed delivered and the keyword gate dropped every entry. The
            # fetch path works; this is not an erroring/never-polled source.
            "  count(*) FILTER (WHERE (sig.last_signal_at IS NULL "
            "      OR sig.last_signal_at < $1) "
            "    AND po.outcome = 'gated') AS silent_feed_gated "
            "FROM source_descriptors s "
            "LEFT JOIN LATERAL (SELECT max(fetched_at) AS last_signal_at FROM signals "
            "  WHERE source_id = s.descriptor_id) sig ON TRUE "
            "LEFT JOIN LATERAL (SELECT outcome, newest_entry_ts FROM source_poll_outcomes "
            "  WHERE source_id = s.descriptor_id ORDER BY occurred_at DESC LIMIT 1) po ON TRUE "
            "WHERE s.is_head = TRUE AND s.state = 'active'",
            silent_cutoff,
            _FEED_EMPTY_SLACK,
        )
        non_active_records = await conn.fetch(
            "SELECT s.descriptor_id AS source_id, "
            "  s.body->'identity'->>'name' AS name, s.state "
            "FROM source_descriptors s "
            "WHERE s.is_head = TRUE AND s.state <> 'active' "
            "ORDER BY s.state, name "
            f"LIMIT {clamped_limit}"
        )
        # I-1: the ingestion intake's queue beside the poll health (None when
        # migration 0249 is not applied or the read fails — never zeros).
        try:
            intake = await intake_counts(conn)
        except Exception:  # pragma: no cover -- degrade to "not there"
            logger.warning("source_health.intake_counts_failed", exc_info=True)
            intake = None
    profiles = await _staleness.fetch_source_profiles(pool)
    # W5 SRC-4: the last enriched poll's per-stage latency, off each source's
    # cursor row. A source absent from the map (or an unreadable ledger) is NOT
    # MEASURED -- the row carries None, never a 0.
    try:
        enrichment = await fetch_enrichment_last_poll(
            pool, [str(r["source_id"]) for r in records],
        )
    except Exception:  # pragma: no cover -- degrade to "not measured"
        enrichment = {}
    rows: list[dict[str, Any]] = []
    silent_count = 0
    error_count = 0
    staleness_hist = {state: 0 for state in _staleness.STATES}
    for r in records:
        lsa = r["last_signal_at"]
        newest_entry_ts = r["last_poll_newest_entry_ts"]
        silent_h: float | None = None
        if isinstance(lsa, datetime):
            silent_h = round((now - lsa).total_seconds() / 3600.0, 1)
        is_silent = silent_h is None or silent_h >= silent_hours
        # B.9: a poll that came back 'empty' AND whose newest OBSERVED
        # upstream entry is no newer than what we already ingested (plus
        # a small slack for clock skew) is an honestly-quiet feed, not a
        # broken one — the 09-27 "25 reference feeds silent 50-80h" read
        # was every one of these, misreported as a coverage gap.
        # W3 SRC-2: that verdict is an INPUT to the staleness state (it tells a
        # quiet feed from an ingestion fault), computed once in
        # ``source_staleness`` and read back here — not a parallel truth.
        staleness = _staleness.staleness_for(
            last_signal_at=lsa,
            cadence_raw=r["cadence_raw"],
            profile=profiles.get(r["source_id"]),
            last_poll_outcome=r["last_poll_outcome"],
            newest_entry_ts=newest_entry_ts,
            last_ok_poll_at=r["last_ok_poll_at"],
            now=now,
        )
        feed_has_no_new_items = bool(staleness["upstream_quiet"])
        staleness_hist[staleness["state"]] += 1
        if r["last_poll_outcome"] == "error":
            error_count += 1
        if is_silent:
            silent_count += 1
        if silent_only and not is_silent:
            continue
        # The per-feed body-extraction read (the evidence_archive
        # sidecar aggregate joined above). ``body_extraction_rate`` is
        # body_stored / fetched with the house convention: None on an empty
        # denominator — never a 0 pretending to be a measurement.
        body_extraction: dict[str, Any] | None = None
        if include_body_extraction:
            be_fetched = int(r["be_fetched"] or 0)
            body_extraction = {
                "fetched": be_fetched,
                "challenge_blocked": int(r["be_challenge_blocked"] or 0),
                "boilerplate_rejected": int(r["be_boilerplate_rejected"] or 0),
                "body_stored": int(r["be_body_stored"] or 0),
                "body_extraction_rate": (
                    int(r["be_body_stored"] or 0) / be_fetched if be_fetched else None
                ),
                "window_days": int(BODY_EXTRACTION_WINDOW_DAYS),
            }
        rows.append({
            "source_id": r["source_id"],
            "name": r["name"],
            "state": r["state"],
            "last_signal_at": lsa.isoformat() if isinstance(lsa, datetime) else None,
            "silent_hours": silent_h,
            "last_poll_outcome": r["last_poll_outcome"],
            "last_health_state": r["last_health_state"],
            "body_extraction": body_extraction,
            # 0226: entries the keyword gate dropped on the last poll; None =
            # not gated / unknown (never a 0).
            "keyword_filtered": r["last_poll_keyword_filtered"],
            "newest_entry_ts": (
                newest_entry_ts.isoformat()
                if isinstance(newest_entry_ts, datetime)
                else None
            ),
            "feed_has_no_new_items": feed_has_no_new_items,
            "staleness": staleness,
            "enrichment_last_poll": enrichment.get(str(r["source_id"])),
        })
    by_state = {str(r["state"]): int(r["n"]) for r in state_records}
    active_stalled = int(active_agg["stalled"]) if active_agg else 0
    silent_feed_empty = int(active_agg["silent_feed_empty"]) if active_agg else 0
    silent_feed_gated = int(active_agg["silent_feed_gated"]) if active_agg else 0
    summary = {
        "total_wired": sum(by_state.values()),
        "by_state": by_state,
        "active_total": int(active_agg["active_total"]) if active_agg else 0,
        "active_fresh": int(active_agg["fresh"]) if active_agg else 0,
        "active_stalled": active_stalled,
        "active_erroring": int(active_agg["erroring"]) if active_agg else 0,
        # B.9 split of the whole-fleet "stalled" (silent) bucket: fed
        # honestly nothing new (outcome='empty', newest_entry_ts backs
        # it up) vs actually broken/unknown (error, or no poll evidence
        # at all). Denominator-honest (whole active fleet, not row-capped).
        "silent_feed_empty": silent_feed_empty,
        # 0226: stalled but last polled 'gated' (delivered, all dropped by the
        # keyword gate) -- productive for the fetch path, so NOT counted in the
        # error-or-none remainder below.
        "silent_feed_gated": silent_feed_gated,
        "silent_poll_error_or_none": active_stalled - silent_feed_empty - silent_feed_gated,
        "silent_threshold_hours": silent_hours,
        # W3 SRC-2: the whole-scan histogram by staleness state (over the
        # ``scanned`` rows, before any silent_only filter).
        "staleness": staleness_hist,
    }
    non_active = [
        {"source_id": r["source_id"], "name": r["name"], "state": r["state"]}
        for r in non_active_records
    ]
    # KEY ORDER IS LOAD-BEARING (2026-09-25, the lens "three active feeds"
    # fabrication). The GATHER preamble renders a non-signal tool result as
    # ``json.dumps(result)[:600]`` (inline_target's ``tool_summaries``): with
    # ``rows`` first, those 600 characters were two rows cut mid-JSON and the
    # ``summary`` at the tail never reached the narrator — every lens then
    # spoke the rows it saw as the fleet ("active_total = 3 … total_wired =
    # 3"; the honesty phase stamped ``source_health_fabricated`` on each).
    # The NARRATE loop's ``_bounded_tool_json`` (4,000 chars) sheds whole
    # rows and keeps the other keys, so it was never the site. The
    # denominator-honest aggregate goes FIRST so ANY prefix keeps it; the
    # row list and the non-active roster are what a bound sheds.
    return {
        # H-1 denominator-honest aggregates (whole-fleet, not row-capped).
        "summary": summary,
        "count": len(rows),
        # Coverage honesty (W2-T6): how many ACTIVE heads the row scan
        # actually covered vs the fleet; rows are staleness-ordered so a
        # truncated scan only sheds the freshest feeds.
        "scanned": len(records),
        "truncated": len(records) < summary["active_total"],
        # Capped/active-only view (back-compat). Prefer `summary` for the truth.
        "silent_count": silent_count,
        "error_count": error_count,
        "silent_threshold_hours": silent_hours,
        "refs": [],
        "rows": rows,
        "non_active": non_active,
        # I-1: queue depth by state/reason, refusals by reason, per-host
        # rates and the oldest queued age (data/intake_visibility.py).
        "intake": intake,
    }


#: The one-line reading of ``last_poll_outcome`` the consult gets in
#: the ``summary`` and on every 'empty' row. The 2026-11-05 consult dig: 59 of
#: 116 healthy sources show 'empty' at any instant (it is the LATEST poll only:
#: HTTP fine, nothing new), and a consult that read it as "dead" reported a
#: working fleet as down.
POLL_OUTCOME_GUIDE = (
    "last_poll_outcome is the LATEST poll only: 'empty' = the poll succeeded "
    "and found no NEW items (a quiet feed, normal for a slow publisher, NOT "
    "dead); 'success' = it wrote or re-served items; 'error' = the fetch "
    "failed; 'gated' = the feed delivered and the keyword gate dropped every "
    "entry. Liveness is the `staleness` verdict (fresh|quiet|stale|dead|unknown "
    "against the feed's own cadence) plus `last_success_at`; for the full "
    "per-source reasoning call get_source_health."
)
_EMPTY_ROW_NOTE = "no new items on the latest poll (not dead)"

#: How many non-healthy sources the ``summary.attention`` roster names, so a
#: trimmed row list can never hide the sources that need a look.
_ATTENTION_CAP = 20


async def list_sources(
    pool: Any,
    *,
    active_only: bool = True,
    silent_only: bool = False,
    silent_hours: int = 48,
    known_at: datetime | None = None,
) -> dict[str, Any]:
    """The ingest sources and their freshness/coverage (the ``list_sources`` read).

    Joins each head source descriptor to its newest signal time, its latest
    ``source_poll_outcomes`` row and its last SUCCESS poll, and stamps each row
    with the ``staleness`` verdict from :mod:`.source_staleness` -- the same
    verdict ``get_source_health`` and the liveness watchdog use, so no parallel
    truth. ``silent_only`` keeps only sources silent over ``silent_hours``.

    ORDER: freshest first by default. The old ``last_signal_at ASC
    NULLS FIRST`` put the stalest rows at the front, and the consult loop's
    bounded tool JSON sheds whole TRAILING rows, so a trimmed answer was by
    construction the 37 stalest of 116 and was read as the fleet. With
    ``silent_only`` the order flips to stalest first (never-delivered first):
    the caller asked for the silent ones, so the worst lead. Either way the
    ``summary`` block comes FIRST in the result and is whole-fleet (not
    row-trimmed): counts by ``staleness`` and by ``last_poll_outcome``, and an
    ``attention`` roster of every stale / dead / unknown / erroring source, so
    no prefix of the result can hide a broken one.
    """
    # E0 (evaluation harness only): ``known_at`` reads the fleet as it stood
    # at K — newest signal among rows created before K, polls before K,
    # staleness judged at K. Absent, every fragment is "" and the statement is
    # byte-identical. The roster and cadence profiles are today's (declared in
    # ``data.raw_reader_known_at``).
    sig_cut = po_cut = ok_cut = ""
    params: list[Any] = []
    if known_at is not None:
        # Imported here, not at module top: this leaf stays slim (no other
        # legba.runtime module on a plain import; test_source_staleness).
        from . import substrate_known_at as _known_at

        params.append(known_at)
        sig_cut = f"AND {_known_at.recorded_before_sql(_known_at.RECORD_ANCHOR, 1)} "
        po_cut = f"AND {_known_at.recorded_before_sql('occurred_at', 1)} "
        ok_cut = f"AND {_known_at.recorded_before_sql('o.occurred_at', 1)} "
    clauses: list[str] = ["s.is_head = TRUE"]
    if active_only:
        clauses.append("s.state = 'active'")
    order = (
        "sig.last_signal_at ASC NULLS FIRST" if silent_only
        else "sig.last_signal_at DESC NULLS LAST"
    )
    sql = (
        "SELECT s.descriptor_id AS source_id, "
        "       s.body->'identity'->>'name' AS name, s.state, "
        "       sig.last_signal_at, po.outcome AS last_poll_outcome, "
        "       po.occurred_at AS last_poll_at, "
        "       po.newest_entry_ts AS last_poll_newest_entry_ts, "
        "       ok.last_ok_poll_at, ok.last_success_at, "
        "       s.body->'cadence'->'schedule'->>'raw' AS cadence_raw "
        "FROM source_descriptors s "
        "LEFT JOIN LATERAL ( "
        "  SELECT max(fetched_at) AS last_signal_at FROM signals "
        "  WHERE source_id = s.descriptor_id "
        f"{sig_cut}"
        ") sig ON TRUE "
        "LEFT JOIN LATERAL ( "
        "  SELECT outcome, occurred_at, newest_entry_ts "
        "  FROM source_poll_outcomes "
        f"  WHERE source_id = s.descriptor_id {po_cut}ORDER BY occurred_at DESC LIMIT 1 "
        ") po ON TRUE "
        # Two newest-first LIMIT 1 probes on source_poll_outcomes_source_time_idx
        # (source_id, occurred_at DESC), not a FILTERed max() over every poll a
        # source ever had: same answer, live EXPLAIN ANALYZE 2026-11-05 (116
        # sources, 219k poll rows) 1.02 s -> 0.62 s, the rest is the signals
        # lateral the read always had.
        "LEFT JOIN LATERAL ( SELECT "
        "  (SELECT o.occurred_at FROM source_poll_outcomes o "
        "    WHERE o.source_id = s.descriptor_id AND o.outcome <> 'error' "
        f"    {ok_cut}ORDER BY o.occurred_at DESC LIMIT 1) AS last_ok_poll_at, "
        "  (SELECT o.occurred_at FROM source_poll_outcomes o "
        "    WHERE o.source_id = s.descriptor_id AND o.outcome = 'success' "
        f"    {ok_cut}ORDER BY o.occurred_at DESC LIMIT 1) AS last_success_at "
        ") ok ON TRUE "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY {order}, s.descriptor_id"
    )
    async with pool.acquire() as conn:
        records = await conn.fetch(sql, *params)
    profiles = await _staleness.fetch_source_profiles(pool)

    now = known_at if known_at is not None else datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    staleness_hist = {state: 0 for state in _staleness.STATES}
    outcome_hist: dict[str, int] = {}
    attention: list[dict[str, Any]] = []
    silent_count = 0
    for r in records:
        lsa = r["last_signal_at"]
        silent_h: float | None = None
        if isinstance(lsa, datetime):
            silent_h = round((now - lsa).total_seconds() / 3600.0, 1)
        is_silent = silent_h is None or silent_h >= silent_hours
        verdict = _staleness.staleness_for(
            last_signal_at=lsa,
            cadence_raw=r["cadence_raw"],
            profile=profiles.get(r["source_id"]),
            last_poll_outcome=r["last_poll_outcome"],
            newest_entry_ts=r["last_poll_newest_entry_ts"],
            last_ok_poll_at=r["last_ok_poll_at"],
            now=now,
        )
        state = verdict["state"]
        outcome = r["last_poll_outcome"]
        staleness_hist[state] += 1
        okey = str(outcome) if outcome else "never_polled"
        outcome_hist[okey] = outcome_hist.get(okey, 0) + 1
        if is_silent:
            silent_count += 1
        if state in ("stale", "dead", "unknown") or outcome == "error":
            attention.append({
                "source_id": r["source_id"],
                "staleness": state,
                "last_poll_outcome": outcome,
            })
        if silent_only and not is_silent:
            continue
        lsp, lok = r["last_poll_at"], r["last_success_at"]
        row: dict[str, Any] = {
            "source_id": r["source_id"],
            "name": r["name"],
            "state": r["state"],
            "last_signal_at": lsa.isoformat() if isinstance(lsa, datetime) else None,
            "silent_hours": silent_h,
            "last_poll_outcome": outcome,
            "last_poll_at": lsp.isoformat() if isinstance(lsp, datetime) else None,
            "last_success_at": lok.isoformat() if isinstance(lok, datetime) else None,
            "staleness": state,
        }
        if outcome == "empty":
            row["last_poll_note"] = _EMPTY_ROW_NOTE
        rows.append(row)

    # KEY ORDER IS LOAD-BEARING (same reason as get_source_health): the bounded
    # tool JSON sheds whole trailing rows and keeps what comes first, so the
    # whole-fleet summary leads and the row list is what a bound sheds.
    return {
        "summary": {
            "sources_scanned": len(records),
            "by_staleness": staleness_hist,
            "by_last_poll_outcome": outcome_hist,
            "silent_over_threshold": silent_count,
            "silent_threshold_hours": silent_hours,
            "attention": attention[:_ATTENTION_CAP],
            "attention_total": len(attention),
            "reading_guide": POLL_OUTCOME_GUIDE,
        },
        "rows": rows,
        "refs": [],
        "count": len(rows),
        **({"known_at_cut": _known_at.cut_stamp(
            known_at, dropped=0,
            how="signals.created_at / poll occurred_at < known_at; judged at known_at",
        )} if known_at is not None else {}),
    }
