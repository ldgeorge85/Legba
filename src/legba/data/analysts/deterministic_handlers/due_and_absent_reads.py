# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reads behind ``due_and_absent`` (lane E-1): every SQL statement the
detector runs, and the async helpers that run them. Read-only, no model.

Split from :mod:`.due_and_absent` so the rule (pure: classification, the
ledger diff, the wording) stays readable apart from the I/O.

Every read takes ``prior_ids``: the expectation rows the previous finding
reported as due and absent. They are re-read whatever their state now, so a
row that was resolved late or voided since is seen and reported once, never
silently dropped.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

#: The source-health episode kinds the liveness watchdog and the staleness
#: tripwire raise (docs/ALERTING.md, "source health"). The detector reads
#: them; it never raises them.
WATCHDOG_SOURCE_KINDS: tuple[str, ...] = (
    "source_cadence_stall", "source_degraded",
    "source_staleness_stale", "source_staleness_dead",
)

#: The newest prior due_and_absent finding (its ledger). Rides
#: idx_analyst_outputs_analyst_id; the detector writes at most one row a day.
PRIOR_SQL = """
SELECT data
  FROM analyst_outputs
 WHERE analyst_id = $1
   AND kind = 'finding'
 ORDER BY produced_at DESC
 LIMIT 1
"""

#: (a) sealed exogenous-ledger questions past due + grace with no active
#: resolution, plus the previous finding's rows. The newest proposal carries
#: what the resolver searched; the first DECISIVE proposal (yes/no/void)
#: counts as a proposed resolution. $1 now, $2 prior ids, $3 limit.
FORECAST_QUESTIONS_SQL = """
SELECT q.id::text AS id, q.slug, q.question, q.resolver_kind, q.status,
       q.due_at, q.grace_hours, q.window_start, q.window_end,
       r.outcome AS resolution_outcome, r.resolved_at,
       p.id::text AS proposal_id, p.outcome AS proposal_outcome,
       p.recorded_at AS proposal_at, p.searched AS proposal_searched,
       (SELECT min(d.recorded_at) FROM forecast_resolution_records d
         WHERE d.question_id = q.id AND d.record_kind = 'proposal'
           AND d.outcome IN ('yes', 'no', 'void')) AS decisive_proposal_at,
       (SELECT min(fp.proposed_at) FROM forecast_resolution_proposals fp
         WHERE fp.question_id = q.id AND fp.status IN ('proposed', 'accepted'))
           AS legacy_proposal_at
  FROM forecast_questions q
  LEFT JOIN forecast_resolutions r ON r.question_id = q.id AND r.active
  LEFT JOIN LATERAL (
        SELECT id, outcome, recorded_at, searched
          FROM forecast_resolution_records
         WHERE question_id = q.id AND record_kind = 'proposal'
         ORDER BY recorded_at DESC
         LIMIT 1) p ON TRUE
 WHERE q.sealed_at IS NOT NULL
   AND ((q.status = 'open'
         AND q.due_at + make_interval(hours => q.grace_hours) < $1)
        OR q.id = ANY($2::uuid[]))
 ORDER BY q.due_at, q.id
 LIMIT $3
"""

#: (a) the acute hazard pilot's forecasts whose window closed more than the
#: grace ago with no graded outcome (a voided row is withdrawn, never due),
#: plus the previous finding's rows. $1 cutoff (now - grace), $2 prior ids,
#: $3 limit.
ACUTE_FORECASTS_SQL = """
SELECT id::text AS id, region, event_class, window_start, window_end,
       resolved_outcome, resolved_by, resolved_at
  FROM acute_forecasts
 WHERE (resolved_outcome IS NULL
        AND window_end < $1
        AND (resolved_by IS NULL OR resolved_by NOT LIKE 'voided:%'))
    OR id = ANY($2::uuid[])
 ORDER BY window_end, id
 LIMIT $3
"""

#: (b) the calendar's rows whose occurrence (end, or start for a point event)
#: plus the tolerance passed inside the look-back, plus the previous finding's
#: rows. $1 now, $2 tolerance hours, $3 look-back floor, $4 prior ids, $5 limit.
SCHEDULED_EVENTS_SQL = """
SELECT id::text AS id, title, starts_at, ends_at, scope, subject_keys,
       confidence, retired_at
  FROM scheduled_events
 WHERE (retired_at IS NULL
        AND COALESCE(ends_at, starts_at) + make_interval(hours => $2) < $1
        AND COALESCE(ends_at, starts_at) + make_interval(hours => $2) >= $3)
    OR id = ANY($4::uuid[])
 ORDER BY starts_at, id
 LIMIT $5
"""

#: (b) the CANDIDATE signals for every calendar row at once: canonical
#: signals collected in the window whose folded title + summary contains any
#: declared key as a plain substring ($3, LIKE patterns). ONE scan of the
#: window whatever the number of rows; the folded text is built once per
#: signal (the OFFSET 0 lateral is not pulled up). The word-bounded per-row
#: key count runs in Python over the candidates (:func:`first_matches`).
#: Measured on live 2026-11-14: Postgres regexes over the text cost ~20 s for
#: ten days of signals; substring LIKE plus Python word bounds is bounded by
#: the payload detoast (~0.3 s per day). $1 floor, $2 until, $3 patterns,
#: $4 the candidate cap.
SCHEDULED_CANDIDATES_SQL = """
SELECT s.id::text AS id, s.source_id, s.fetched_at, t.txt
  FROM signals s
  CROSS JOIN LATERAL (
        SELECT lower(COALESCE(s.payload->>'title', '') || ' '
                     || COALESCE(s.payload->>'summary', '')) AS txt
        OFFSET 0) t
 WHERE s.fetched_at >= $1 AND s.fetched_at <= $2
   AND s.canonical_signal_id IS NULL
   AND t.txt LIKE ANY ($3::text[])
 ORDER BY s.fetched_at, s.id
 LIMIT $4
"""

#: The most candidate signals one run reads; reaching it makes the calendar
#: read UNMEASURED (a capped scan could miss the match that clears a row).
MAX_CANDIDATES = 20000


class CandidateCapReached(RuntimeError):
    """The candidate scan hit :data:`MAX_CANDIDATES`."""

#: (b) the inquiry ledger's open rows whose ``resolves_by`` passed, plus the
#: previous finding's rows. $1 now, $2 prior ids, $3 limit.
INQUIRY_SQL = """
SELECT id::text AS id, descriptor_id, kind, left(text, 300) AS text, status,
       resolves_by, closed_at, dispatched_to
  FROM inquiry_ledger
 WHERE (status = 'open' AND resolves_by IS NOT NULL AND resolves_by < $1)
    OR id = ANY($2::uuid[])
 ORDER BY resolves_by, id
 LIMIT $3
"""

#: (c) every LIVE series (observations origin 'live', migration 0246): its
#: declared period, its newest period start and that row's id.
SERIES_SQL = """
SELECT o.source_id, o.series_id, o.period,
       max(o.valid_from) AS newest,
       (array_agg(o.id::text ORDER BY o.valid_from DESC, o.record_time DESC))[1] AS newest_id,
       count(DISTINCT o.subject) AS subjects
  FROM observations o
 WHERE o.origin_class = 'live'
 GROUP BY o.source_id, o.series_id, o.period
 ORDER BY o.source_id, o.series_id
 LIMIT $1
"""

#: (c) each series source's head: its lifecycle state and declared lag.
SERIES_SOURCES_SQL = """
SELECT descriptor_id, state, body #>> '{config,lag_days,raw}' AS lag_days
  FROM source_descriptors
 WHERE is_head AND descriptor_id = ANY($1::text[])
"""

#: The sources that are active now (the collection a row with no named
#: source would have been carried by).
ACTIVE_SOURCES_SQL = """
SELECT descriptor_id
  FROM source_descriptors
 WHERE is_head AND state = 'active' AND descriptor_id LIKE 'source.%'
"""

#: Poll outcomes per source inside a window (migration 0046/0114/0226).
POLL_HEALTH_SQL = """
SELECT source_id,
       count(*)::int AS polls,
       count(*) FILTER (WHERE outcome = 'error')::int AS errors,
       count(*) FILTER (WHERE outcome IN ('success', 'empty', 'gated'))::int AS clean,
       COALESCE(sum(signals_written) FILTER (WHERE outcome = 'success'), 0)::int AS written
  FROM source_poll_outcomes
 WHERE source_id = ANY($1::text[]) AND occurred_at >= $2 AND occurred_at <= $3
 GROUP BY source_id
"""

#: Watchdog source-health episodes open at any point inside the window.
WATCHDOG_SQL = """
SELECT subject_id, array_agg(DISTINCT kind ORDER BY kind) AS kinds
  FROM alerts
 WHERE kind = ANY($1::text[]) AND subject_id = ANY($2::text[])
   AND opened_at <= $4 AND (closed_at IS NULL OR closed_at >= $3)
 GROUP BY subject_id
"""

#: How many outputs an analyst wrote since a moment (an inquiry's carrier).
ANALYST_OUTPUTS_SINCE_SQL = """
SELECT count(*)::int AS n
  FROM analyst_outputs
 WHERE analyst_id = $1 AND produced_at >= $2
"""


def _ts(value: Any) -> datetime:
    """A timestamp as an aware datetime (an ISO string is parsed; naive = UTC)."""
    v = datetime.fromisoformat(value) if isinstance(value, str) else value
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


def fold_keys(keys: Sequence[str]) -> list[str]:
    """Declared subject keys folded: whitespace collapsed, lower-cased,
    empties dropped, order kept, duplicates removed (pure)."""
    out: list[str] = []
    for raw in keys:
        k = " ".join(str(raw or "").split()).lower()
        if k and k not in out:
            out.append(k)
    return out


def like_patterns(keys: Sequence[str]) -> list[str]:
    """``%key%`` LIKE patterns (``\\``, ``%`` and ``_`` escaped) for the
    candidate scan — a superset of the word-bounded match (pure)."""
    return sorted({"%" + re.sub(r"([\\%_])", r"\\\1", k) + "%" for k in fold_keys(keys)})


def key_regex(key: str) -> re.Pattern[str]:
    """A folded key as a whole-word literal (Unicode word boundaries)."""
    return re.compile(r"(?<!\w)" + re.escape(key) + r"(?!\w)")


def key_hits(text: str, keys: Sequence[str]) -> int:
    """How many distinct folded keys occur in ``text`` as whole words (pure)."""
    low = str(text or "").lower()
    return sum(1 for k in fold_keys(keys) if key_regex(k).search(low))


async def fetch(conn: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch(sql, *args)]


async def first_matches(conn: Any, events: Sequence[Mapping[str, Any]], *, floor: datetime,
                        until: datetime, min_hits: int) -> dict[str, dict[str, Any]]:
    """``{event id: earliest matching signal}`` for calendar rows that declare
    subject keys. Each row is searched from its own start (never before
    ``floor``) to ``until``; a row with no match is absent from the map."""
    wanted = [(str(ev["id"]), ev["starts_at"], fold_keys(ev.get("subject_keys") or []))
              for ev in events]
    wanted = [w for w in wanted if w[2]]
    if not wanted:
        return {}
    patterns = like_patterns([k for _, _, keys in wanted for k in keys])
    rows = await conn.fetch(SCHEDULED_CANDIDATES_SQL, floor, until, patterns, MAX_CANDIDATES)
    if len(rows) >= MAX_CANDIDATES:
        raise CandidateCapReached(f"candidate scan reached {MAX_CANDIDATES} rows")
    return match_candidates(wanted, [dict(r) for r in rows], min_hits=min_hits)


def match_candidates(wanted: Sequence[tuple[str, Any, Sequence[str]]],
                     candidates: Sequence[Mapping[str, Any]], *,
                     min_hits: int) -> dict[str, dict[str, Any]]:
    """Per calendar row, the earliest candidate at or after its start that
    carries ``min(min_hits, #keys)`` of its keys as whole words (pure)."""
    ordered = sorted(({**c, "fetched_at": _ts(c["fetched_at"])} for c in candidates),
                     key=lambda c: (c["fetched_at"], str(c["id"])))
    out: dict[str, dict[str, Any]] = {}
    for ev_id, start, keys in wanted:
        need = min(min_hits, len(keys))
        regs = [key_regex(k) for k in keys]
        for c in ordered:
            if c["fetched_at"] < _ts(start):
                continue
            txt = str(c.get("txt") or "")
            if sum(1 for r in regs if r.search(txt)) >= need:
                out[ev_id] = {"id": str(c["id"]), "source_id": c.get("source_id"),
                              "fetched_at": c["fetched_at"]}
                break
    return out


async def active_sources(conn: Any) -> list[str]:
    return [str(r["descriptor_id"]) for r in await conn.fetch(ACTIVE_SOURCES_SQL)]


def poll_class(stats: Mapping[str, Any] | None) -> str:
    """The window's poll class for one source (pure).

    ``silent``: no poll recorded; ``failing``: polls, none clean;
    ``partly_failing``: some errored, some clean; ``polling_clean``: every
    poll clean (success / empty / gated)."""
    if not stats or int(stats.get("polls") or 0) == 0:
        return "silent"
    errors, clean = int(stats.get("errors") or 0), int(stats.get("clean") or 0)
    if clean == 0:
        return "failing"
    if errors > 0:
        return "partly_failing"
    return "polling_clean"


async def source_health(conn: Any, sources: Sequence[str], since: datetime,
                        until: datetime) -> dict[str, dict[str, Any]]:
    """Per source: ``{class, polls, errors, clean, written, watchdog}``."""
    ids = sorted({str(s) for s in sources if s})
    if not ids:
        return {}
    stats = {str(r["source_id"]): dict(r) for r in await conn.fetch(POLL_HEALTH_SQL, ids, since, until)}
    dog = {str(r["subject_id"]): list(r["kinds"] or [])
           for r in await conn.fetch(WATCHDOG_SQL, list(WATCHDOG_SOURCE_KINDS), ids, since, until)}
    out: dict[str, dict[str, Any]] = {}
    for sid in ids:
        st = stats.get(sid) or {}
        out[sid] = {
            "class": poll_class(st),
            "polls": int(st.get("polls") or 0), "errors": int(st.get("errors") or 0),
            "clean": int(st.get("clean") or 0), "written": int(st.get("written") or 0),
            "watchdog": dog.get(sid, []),
        }
    return out


async def analyst_outputs_since(conn: Any, analyst_id: str, since: datetime) -> int:
    row = await conn.fetchrow(ANALYST_OUTPUTS_SINCE_SQL, analyst_id, since)
    return int(row["n"]) if row is not None else 0


__all__ = [
    "ACUTE_FORECASTS_SQL",
    "FORECAST_QUESTIONS_SQL",
    "INQUIRY_SQL",
    "MAX_CANDIDATES",
    "PRIOR_SQL",
    "SCHEDULED_CANDIDATES_SQL",
    "SCHEDULED_EVENTS_SQL",
    "SERIES_SOURCES_SQL",
    "SERIES_SQL",
    "WATCHDOG_SOURCE_KINDS",
    "CandidateCapReached",
    "active_sources",
    "analyst_outputs_since",
    "fetch",
    "first_matches",
    "fold_keys",
    "key_hits",
    "key_regex",
    "like_patterns",
    "match_candidates",
    "poll_class",
    "source_health",
]
