# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DRIFT RE-CHECK batch — producer (c) of the intake.

The platform cannot watch every page it ingested. It re-checks the ones that
matter — CITED pages (a signal with an evidence-archive row, i.e. one a
finding cited) — in a routine batch with a declared cadence and cap: the
newest held version of each such page address, oldest first, once it is
older than ``min_age``. Each becomes a ``stored_version`` request (reason
``drift_recheck``, budget class ``drift``, sink ``receipt``) under the
declared page pipeline. The drain re-fetches the address; the receipt writer
keeps the new version and writes the plain drift row when the extracted text
changed (or ``gone`` when the page stopped answering). Classification of the
drift, flags on dependants and the panel are the drift follow-up's, not this
batch's.

Switch and cadence (all off / declared here, env-overridable):

* ``LEGBA_INTAKE_DRIFT_RECHECK`` — off by default;
* ``LEGBA_INTAKE_DRIFT_PIPELINE`` — the page pipeline (a source descriptor
  whose kind takes an address). Unset = the instance's default page pipeline
  ``source.page.default`` (I-2); ``none`` = the batch does nothing, says so
  once;
* ``LEGBA_INTAKE_DRIFT_CADENCE_H`` (24) — at most one batch per cadence. The
  last batch's time is the newest request this batch submitted (the table is
  the state; nothing else is kept);
* ``LEGBA_INTAKE_DRIFT_CAP`` (50) — requests per batch;
* ``LEGBA_INTAKE_DRIFT_MIN_AGE_H`` (168) — a page is re-checked at most
  weekly (and less often once it has proved stable — the back-off below);
* ``LEGBA_INTAKE_DRIFT_HEAD_DAYS`` (14) — how recent an open head must be for
  its citations to put a page first.

The SELECTION is :data:`_CANDIDATES_SQL`: the same cap, ordered by
what relies on the page (open heads first, then the sealed ledgers' evidence
and what was published outward), stable pages backed off, and hosts the
licence ledger refuses never re-checked.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from ..data.intake_contract import DEFAULT_PAGE_PIPELINE

logger = logging.getLogger(__name__)

__all__ = ["DRIFT_REQUESTER", "drift_batch_if_due", "drift_recheck_enabled", "submit_drift_batch"]

DRIFT_REQUESTER = "intake:drift_recheck"
_ON = frozenset({"1", "on", "true", "yes"})
_warned_no_pipeline = False
#: This process's last batch (the table holds it across restarts; this stops
#: an empty batch from re-running its candidate query every tick).
_last_batch_at: datetime | None = None


def drift_recheck_enabled() -> bool:
    return os.environ.get("LEGBA_INTAKE_DRIFT_RECHECK", "").strip().lower() in _ON


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


#: The selection. Same budget (``cap`` per batch, one batch per
#: cadence), smarter order:
#:
#: * candidates — the newest page/archiver version of each address a CITED
#:   signal carries (an evidence-archive row; the 0247 canonical-URL hash),
#:   not already queued, last seen before the minimum age;
#: * BACK-OFF — a page whose re-checks keep finding the same text waits
#:   longer: ``min_age * 2^k`` with k = the done re-checks since the address
#:   last drifted that observed no change (capped at 3: 1, 2, 4, 8 weeks at
#:   the default weekly age);
#: * NEVER a host the licence ledger refuses (``source_credibility``
#:   ``license_class`` in the archiver's forbid-retention set, exact host or
#:   any parent domain);
#: * PRIORITY — cited by a currently-open head (an ``analyst_outputs`` row not
#:   superseded, produced inside ``LEGBA_INTAKE_DRIFT_HEAD_DAYS`` (14), whose
#:   citations name one of the page's signals) scores 4; evidence of the
#:   sealed ledgers (``forecast_resolutions`` / proposals /
#:   ``scorecard_entries`` ``evidence_refs``) 2; published outward (an open
#:   journal entry's ``cited_substrate_refs``, a consult answer's
#:   ``cited_refs``) 2. Highest score first, then oldest.
#:
#: $1 = now, $2 = cap, $3 = min age (hours), $4 = forbidden licence classes,
#: $5 = open-head window (days).
_CANDIDATES_SQL = """
WITH newest AS (
    SELECT DISTINCT ON (v.address) v.id, v.address, v.url, v.last_seen_at
      FROM fetch_versions v
     WHERE v.receipt_kind = 'fetch' AND v.role IN ('page', 'archiver')
     ORDER BY v.address, v.last_seen_at DESC, v.id DESC
), cand AS MATERIALIZED (
    SELECT n.id, n.address, n.url, n.last_seen_at,
           lower(substring(n.address from '^[A-Za-z][A-Za-z0-9+.-]*://([^/:?#]+)')) AS host,
           (SELECT array_agg(s.id) FROM signals s
             WHERE s.canonical_url_hash = encode(sha256(convert_to(n.address, 'UTF8')), 'hex')
               AND EXISTS (SELECT 1 FROM evidence_archive ea WHERE ea.signal_id = s.id)) AS sids
      FROM newest n
     WHERE n.last_seen_at < $1::timestamptz - $3::float8 * interval '1 hour'
       AND NOT EXISTS (
            SELECT 1 FROM ingestion_requests r
             WHERE r.version_id = n.id AND r.reason = 'drift_recheck'
               AND r.state IN ('queued', 'claimed'))
), eligible AS MATERIALIZED (
    SELECT c.*,
           (SELECT count(*) FROM ingestion_requests r
             WHERE r.address_canonical = c.address AND r.reason = 'drift_recheck' AND r.state = 'done'
               AND coalesce((r.result->>'drift_observed')::int, 0) = 0
               AND NOT coalesce((r.result->>'gone')::boolean, false)
               AND r.finished_at > coalesce(
                     (SELECT max(d.observed_at) FROM fetch_drift_observations d WHERE d.address = c.address),
                     '-infinity'::timestamptz)) AS stable_checks
      FROM cand c
     WHERE c.sids IS NOT NULL
       AND NOT EXISTS (
            SELECT 1 FROM source_credibility sc
             WHERE sc.license_class = ANY($4::text[])
               AND (c.host = sc.source_host OR c.host LIKE '%.' || sc.source_host))
), due AS MATERIALIZED (
    SELECT e.*, u.sid
      FROM eligible e, unnest(e.sids) AS u(sid)
     WHERE e.last_seen_at < $1::timestamptz - $3::float8 * power(2, least(e.stable_checks, 3)) * interval '1 hour'
), open_cited AS MATERIALIZED (
    -- One pass over the open heads of the window: the signal ids they cite.
    SELECT DISTINCT coalesce(x->>'signal_id', x->>'ref_id') AS sid
      FROM analyst_outputs o,
           jsonb_array_elements(CASE WHEN jsonb_typeof(o.data->'data'->'citations') = 'array'
                                     THEN o.data->'data'->'citations' ELSE '[]'::jsonb END) x
     WHERE o.produced_at >= $1::timestamptz - $5::float8 * interval '1 day'
       AND o.superseded_by IS NULL AND jsonb_typeof(x) = 'object'
       AND coalesce(x->>'signal_id', x->>'ref_id') IS NOT NULL
), ledger_cited AS MATERIALIZED (
    SELECT DISTINCT u AS sid FROM forecast_resolutions f, unnest(f.evidence_refs) u
    UNION SELECT u FROM forecast_resolution_proposals f, unnest(f.evidence_refs) u
    UNION SELECT u FROM scorecard_entries f, unnest(f.evidence_refs) u
), published_cited AS MATERIALIZED (
    SELECT DISTINCT u::text AS sid FROM journal_entries j, unnest(j.cited_substrate_refs) u
     WHERE j.valid_until IS NULL
    UNION
    SELECT x->>'id' FROM consult_turns t,
           jsonb_array_elements(CASE WHEN jsonb_typeof(t.cited_refs) = 'array'
                                     THEN t.cited_refs ELSE '[]'::jsonb END) x
     WHERE jsonb_typeof(x) = 'object' AND x ? 'id'
), scored AS (
    SELECT d.id, d.address, d.url, d.last_seen_at, d.stable_checks,
           bool_or(d.sid::text IN (SELECT sid FROM open_cited)) AS open_head,
           bool_or(d.sid::text IN (SELECT sid FROM ledger_cited)) AS forecast,
           bool_or(d.sid::text IN (SELECT sid FROM published_cited)) AS published
      FROM due d
     GROUP BY d.id, d.address, d.url, d.last_seen_at, d.stable_checks
)
SELECT id, address, url, last_seen_at, open_head, forecast, published, stable_checks
  FROM scored
 ORDER BY (CASE WHEN open_head THEN 4 ELSE 0 END + CASE WHEN forecast THEN 2 ELSE 0 END
           + CASE WHEN published THEN 2 ELSE 0 END) DESC, last_seen_at
 LIMIT $2
"""


def _forbidden_license_classes() -> list[str]:
    from ..data.research_evidence import forbidden_license_classes  # noqa: PLC0415

    return sorted(forbidden_license_classes())


async def submit_drift_batch(
    pool: Any, *, pipeline: str, cap: int, min_age_h: float, now: datetime | None = None,
    head_days: float | None = None,
) -> dict[str, int]:
    """Submit up to ``cap`` re-check requests. Returns counts by outcome and
    by the reason a page was put first."""
    from ..data.intake_store import SubmitRequest, submit_request  # noqa: PLC0415

    now = now or datetime.now(tz=timezone.utc)
    head_days = _num("LEGBA_INTAKE_DRIFT_HEAD_DAYS", 14.0) if head_days is None else head_days
    async with pool.acquire() as conn:
        rows = await conn.fetch(_CANDIDATES_SQL, now, int(cap), float(min_age_h),
                                _forbidden_license_classes(), float(head_days))
    counts: dict[str, int] = {"candidates": len(rows)}
    for row in rows:
        for why in ("open_head", "forecast", "published"):
            if row[why]:
                counts[f"why_{why}"] = counts.get(f"why_{why}", 0) + 1
        if row["stable_checks"]:
            counts["backed_off_but_due"] = counts.get("backed_off_but_due", 0) + 1
        res = await submit_request(pool, SubmitRequest(
            target_kind="stored_version", version_id=int(row["id"]),
            pipeline_source_id=pipeline, sink="receipt", reason="drift_recheck",
            requested_by=DRIFT_REQUESTER,
            priority=40 if row["open_head"] else 30 if (row["forecast"] or row["published"]) else 20,
        ), now=now)
        key = res.refusal_reason or res.state
        counts[key] = counts.get(key, 0) + 1
    return counts


async def drift_batch_if_due(pool: Any, *, now: datetime | None = None) -> bool:
    """Run one batch when the cadence has elapsed since the last one. True
    when a batch ran."""
    global _warned_no_pipeline, _last_batch_at
    # I-2: unset = the instance's default page pipeline; "none" = no re-checks.
    pipeline = os.environ.get("LEGBA_INTAKE_DRIFT_PIPELINE", "").strip() or DEFAULT_PAGE_PIPELINE
    if pipeline.lower() in ("none", "off"):
        if not _warned_no_pipeline:
            logger.warning("intake.drift.no_pipeline LEGBA_INTAKE_DRIFT_PIPELINE=%s — no re-checks", pipeline)
            _warned_no_pipeline = True
        return False
    now = now or datetime.now(tz=timezone.utc)
    cadence = timedelta(hours=_num("LEGBA_INTAKE_DRIFT_CADENCE_H", 24.0))
    async with pool.acquire() as conn:
        last = await conn.fetchval(
            "SELECT max(created_at) FROM ingestion_requests WHERE requested_by = $1", DRIFT_REQUESTER,
        )
    if _last_batch_at is not None and (last is None or _last_batch_at > last):
        last = _last_batch_at
    if last is not None and now - last < cadence:
        return False
    _last_batch_at = now
    counts = await submit_drift_batch(
        pool, pipeline=pipeline, cap=int(_num("LEGBA_INTAKE_DRIFT_CAP", 50)),
        min_age_h=_num("LEGBA_INTAKE_DRIFT_MIN_AGE_H", 168.0), now=now,
    )
    logger.info("intake.drift.batch pipeline=%s counts=%s", pipeline, counts)
    return True
