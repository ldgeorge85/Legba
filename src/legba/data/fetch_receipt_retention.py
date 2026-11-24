# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention for fetch-receipt BYTES (N-L) — what may be pruned, and the pruner.

Every poll keeps the response it received (``fetch_versions``, migration
0245). Most of those bytes are never cited by anything; after
``unreferenced_after_days`` they are pruned. What is pruned is the BYTES only:
the version row (address, sha256, size, status, content type, poll times,
fetch count), the record links and any extracted page text stay, and a
``fetch_version_tombstones`` row says what went, when, why and under which
policy version. Referenced bytes are never pruned — they age into the cold
tier through ``scripts/archive_tier.py`` instead (``archive_tier``).

THE "REFERENCED" RULE (policy ``fetch-receipts-retention/1``). A version is
referenced when ANY of these holds — and ``derived_from`` is deliberately NOT
one of them (it lists a run's INPUTS, not what an output cites):

1. a finding/output cites the bytes directly: an ``analyst_outputs``
   ``data.data.citations`` entry whose ``fetch_sha256`` is the version's sha256
   (series_change citations carry it);
2. a record derived from the version (``fetch_version_records``) is cited by a
   real citation edge:
   * ``analyst_outputs.data.data.citations[*]`` — ``signal_id``, or
     ``ref_id`` with ``ref_kind`` signal / observation (findings,
     compositions, situation updates);
   * ``consult_turns.cited_refs[*].id`` (consult answers);
   * ``inquiry_ledger.cited_refs[*]``;
   * ``journal_entries`` / ``journal_proposals`` ``.cited_substrate_refs``;
   * ``claim_contentions.ref_signal_ids``;
   * ``forecast_resolutions`` / ``forecast_resolution_proposals`` /
     ``scorecard_entries`` ``.evidence_refs`` (the sealed ledgers' evidence);
3. a derived signal is HELD: ``retention_class`` ``evidence_hold`` /
   ``retain_always``, or it has an ``evidence_archive`` row;
4. the bytes are SHARED with something kept: the same sha256 is an
   ``evidence_archive`` object or media object, has a cold copy
   (``evidence_archive_cold``), is a ``signals.object_ref``, a history
   ``observations.sha256``, or belongs to another version that is not itself
   prunable now (one content address, many rows: the file goes only when no
   row needs it).

Measured read-only on 2026-11-13 under rule 2 (the citation half): of the
90,631 pages the evidence archiver archived in the last 60 days, 35,861
(39.6 %) are cited by a real citation edge; of the 172,779 signals fetched in
the last 60 days, 38,803 (22.5 %) are cited, and 94,954 (55.0 %) are cited or
held (rule 3).

I/O IS BOUNDED: ``batch_size`` versions per batch, ``max_batches`` per run;
each batch costs one scan of the citation edges restricted to that batch's
record ids. Dry-run by default; ``apply`` unlinks and records. Idempotent: a
pruned version is never selected again, and a tombstone is written once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .archive import cas_path

__all__ = ["POLICY_VERSION", "RetentionPolicy", "prune_unreferenced", "REFERENCED_SQL"]

POLICY_VERSION = "fetch-receipts-retention/1"


@dataclass(frozen=True)
class RetentionPolicy:
    unreferenced_after_days: int = 30
    batch_size: int = 500
    max_batches: int = 20

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RetentionPolicy":
        e = os.environ if env is None else env

        def _pos(name: str, default: int) -> int:
            try:
                v = int(e.get(name, "") or default)
            except ValueError:
                return default
            return v if v > 0 else default

        return cls(
            unreferenced_after_days=_pos("LEGBA_FETCH_RECEIPT_UNREFERENCED_AFTER_DAYS", 30),
            batch_size=_pos("LEGBA_FETCH_RECEIPT_PRUNE_BATCH", 500),
            max_batches=_pos("LEGBA_FETCH_RECEIPT_PRUNE_MAX_BATCHES", 20),
        )


#: Candidates: held bytes, last seen before the cutoff, not yet pruned, oldest
#: first, after the id cursor. $1 = days, $2 = limit, $3 = after id.
CANDIDATES_SQL = """
SELECT id, sha256, size_bytes, stored_size, last_seen_at
  FROM fetch_versions
 WHERE object_ref IS NOT NULL
   AND bytes_pruned_at IS NULL
   AND last_seen_at < now() - make_interval(days => $1)
   AND id > $3
 ORDER BY id
 LIMIT $2
"""

#: Which of the candidate version ids ($1) are REFERENCED (rules 1-4). $2 =
#: days (rule 4's "another version not prunable now").
REFERENCED_SQL = """
WITH cand AS (
    SELECT v.id, v.sha256 FROM fetch_versions v WHERE v.id = ANY($1::bigint[])
), recs AS (
    SELECT r.version_id, r.record_kind, r.record_id, r.record_id::text AS rid
      FROM fetch_version_records r WHERE r.version_id = ANY($1::bigint[])
), cites AS (
    SELECT coalesce(c->>'signal_id',
                    CASE WHEN c->>'ref_kind' IN ('signal', 'observation') THEN c->>'ref_id' END) AS rid,
           c->>'fetch_sha256' AS sha
      FROM analyst_outputs o,
           jsonb_array_elements(CASE WHEN jsonb_typeof(o.data->'data'->'citations') = 'array'
                                     THEN o.data->'data'->'citations' ELSE '[]'::jsonb END) c
     WHERE (c->>'signal_id') IN (SELECT rid FROM recs)
        OR (c->>'ref_id') IN (SELECT rid FROM recs)
        OR (c->>'fetch_sha256') IN (SELECT sha256 FROM cand)
), edge_ids AS (
    SELECT rid FROM cites WHERE rid IS NOT NULL
    UNION SELECT c->>'id' FROM consult_turns t,
           jsonb_array_elements(CASE WHEN jsonb_typeof(t.cited_refs) = 'array' THEN t.cited_refs ELSE '[]'::jsonb END) c
     WHERE jsonb_typeof(c) = 'object' AND (c->>'id') IN (SELECT rid FROM recs)
    UNION SELECT x FROM inquiry_ledger i,
           jsonb_array_elements_text(CASE WHEN jsonb_typeof(i.cited_refs) = 'array' THEN i.cited_refs ELSE '[]'::jsonb END) x
     WHERE x IN (SELECT rid FROM recs)
    UNION SELECT u::text FROM journal_entries, unnest(cited_substrate_refs) u WHERE u::text IN (SELECT rid FROM recs)
    UNION SELECT u::text FROM journal_proposals, unnest(cited_substrate_refs) u WHERE u::text IN (SELECT rid FROM recs)
    UNION SELECT u::text FROM claim_contentions, unnest(ref_signal_ids) u WHERE u::text IN (SELECT rid FROM recs)
    UNION SELECT u::text FROM forecast_resolutions, unnest(evidence_refs) u WHERE u::text IN (SELECT rid FROM recs)
    UNION SELECT u::text FROM forecast_resolution_proposals, unnest(evidence_refs) u WHERE u::text IN (SELECT rid FROM recs)
    UNION SELECT u::text FROM scorecard_entries, unnest(evidence_refs) u WHERE u::text IN (SELECT rid FROM recs)
)
SELECT DISTINCT cand.id
  FROM cand
 WHERE EXISTS (SELECT 1 FROM cites WHERE cites.sha = cand.sha256)                          -- rule 1
    OR EXISTS (SELECT 1 FROM recs JOIN edge_ids e ON e.rid = recs.rid
                WHERE recs.version_id = cand.id)                                            -- rule 2
    OR EXISTS (SELECT 1 FROM recs JOIN signals s ON recs.record_kind = 'signal' AND s.id = recs.record_id
                WHERE recs.version_id = cand.id
                  AND (s.retention_class IN ('evidence_hold', 'retain_always')
                       OR EXISTS (SELECT 1 FROM evidence_archive ea WHERE ea.signal_id = s.id)))  -- rule 3
    OR EXISTS (SELECT 1 FROM evidence_archive ea
                WHERE ea.sha256 = cand.sha256 OR ea.media_sha256 = cand.sha256)             -- rule 4
    OR EXISTS (SELECT 1 FROM evidence_archive_cold k WHERE k.sha256 = cand.sha256)
    OR EXISTS (SELECT 1 FROM signals s2 WHERE s2.object_ref = 'cas:sha256/' || cand.sha256)
    OR EXISTS (SELECT 1 FROM observations h WHERE h.sha256 = cand.sha256 AND h.origin_class <> 'live')
    OR EXISTS (SELECT 1 FROM fetch_versions o2
                WHERE o2.sha256 = cand.sha256 AND o2.id <> cand.id AND o2.object_ref IS NOT NULL
                  AND o2.bytes_pruned_at IS NULL
                  AND (o2.last_seen_at >= now() - make_interval(days => $2)
                       OR NOT (o2.id = ANY($1::bigint[]))))
"""

_TOMBSTONE_SQL = """
INSERT INTO fetch_version_tombstones (version_id, sha256, stored_size, reason, policy_version)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (version_id) DO NOTHING
"""

_MARK_PRUNED_SQL = "UPDATE fetch_versions SET bytes_pruned_at = now() WHERE id = $1 AND bytes_pruned_at IS NULL"


@dataclass
class PruneReport:
    policy_version: str = POLICY_VERSION
    applied: bool = False
    examined: int = 0
    referenced: int = 0
    prunable: int = 0
    pruned: int = 0
    bytes_freed: int = 0
    missing_files: int = 0
    batches: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)


async def prune_unreferenced(conn: Any, root: Path, policy: RetentionPolicy, *, apply: bool) -> PruneReport:
    """One bounded pass. Dry-run unless ``apply``."""
    report = PruneReport(applied=apply)
    reason = f"unreferenced for {policy.unreferenced_after_days}d"
    after = 0
    for _ in range(policy.max_batches):
        rows = await conn.fetch(CANDIDATES_SQL, policy.unreferenced_after_days, policy.batch_size, after)
        if not rows:
            break
        report.batches += 1
        after = rows[-1]["id"]
        ids = [r["id"] for r in rows]
        report.examined += len(ids)
        held = {r["id"] for r in await conn.fetch(REFERENCED_SQL, ids, policy.unreferenced_after_days)}
        report.referenced += len(held)
        # One content address, several rows in this batch: a held row keeps
        # the file for every row sharing its sha256.
        held_shas = {r["sha256"] for r in rows if r["id"] in held}
        for r in rows:
            if r["id"] in held or r["sha256"] in held_shas:
                continue
            report.prunable += 1
            path = cas_path(root, r["sha256"])
            size = path.stat().st_size if path.exists() else 0
            if len(report.sample) < 10:
                report.sample.append({"version_id": r["id"], "sha256": r["sha256"], "stored_bytes": size})
            if not apply:
                report.bytes_freed += size
                continue
            async with conn.transaction():
                await conn.execute(_TOMBSTONE_SQL, r["id"], r["sha256"], size or r["stored_size"],
                                   reason, POLICY_VERSION)
                await conn.execute(_MARK_PRUNED_SQL, r["id"])
                # Unlink last, inside the transaction: if it fails the rows roll back.
                try:
                    path.unlink()
                except FileNotFoundError:
                    report.missing_files += 1
            report.pruned += 1
            report.bytes_freed += size
        if len(rows) < policy.batch_size:
            break
    return report
