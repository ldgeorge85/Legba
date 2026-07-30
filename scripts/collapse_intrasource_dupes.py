#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collapse HISTORICAL intra-source exact content-hash duplicate signals (S-4).

The live S-4 ingest fix (:func:`legba.runtime.source_actor.write_canonical_signal`)
stops NEW intra-source duplicates: an incoming signal whose
``(source_id, content_hash)`` already exists in-window bumps the freshest
existing row's recency and skips the insert. Rows that landed BEFORE the fix
(~8,620 measured 2026-07-27 via ``scripts/report_intrasource_dupes.py``) are
still stored as separate byte-identical rows. This script is the one-shot
historical cleanup: for every ``(source_id, content_hash, owner_tenant)`` group
with more than one row it elects the NEWEST-fetched row as SURVIVOR (matching
the live bump's most-recent pick: ``ORDER BY fetched_at DESC, id DESC``),
re-points every in-database reference from the losing rows to the survivor,
and deletes the losers.

DRY-RUN BY DEFAULT — prints exact counts + a sample and changes nothing.
``--apply`` executes, batched (``--batch-groups`` groups per transaction, each
batch atomic). Idempotent: a re-run finds nothing left to collapse (groups kept
multi-row only by the retention keep-set are re-reported as held, never
re-deleted).

Reference classes to ``signals.id`` and how each is handled
-----------------------------------------------------------
The substrate has NO DB-level foreign key into ``signals`` (baseline 0001), so
every reference is explicit here — the same inventory ``signals_retention``
cleans, plus the lineage/value classes it deliberately dangles (dangling is the
D4 trade-off for a PURGE, where the content is gone anyway; in a COLLAPSE the
identical content SURVIVES under the survivor id, so we re-point instead):

* ``signal_entity_links.signal_id`` — RE-POINTED to the survivor via
  INSERT..SELECT ``ON CONFLICT (signal_id, entity_id, role) DO NOTHING`` then
  loser-row delete (the 0063/0076 entity-merge snapshot idiom).
* ``signal_aliases`` (``alias_signal_id``/``canonical_signal_id``) — both sides
  RE-POINTED (self-pairs dropped), then loser-touching rows deleted.
* ``signals.canonical_signal_id`` — rows pointing at a loser RE-POINTED to the
  survivor (a survivor that pointed at its own loser gets NULL, never a
  self-pointer).
* ``derived_from uuid[]`` lineage arrays on ``signals`` / ``facts`` /
  ``analyst_outputs`` / ``hypotheses`` / ``proposed_edges`` / ``situations`` /
  ``entity_profiles`` / ``nexuses`` and ``nexuses.source_signal_ids`` —
  loser ids REWRITTEN to the survivor id in place (first-occurrence order kept,
  survivor de-duplicated), so the recursive-CTE lineage walk keeps resolving.
* ``evidence_archive.signal_id`` (1:1 sidecar, no FK BY DESIGN — evidence
  outlives the row) — a loser's archive row is RE-POINTED to the survivor when
  the survivor has none; otherwise it is LEFT IN PLACE (evidence-outlives-row
  is the 0104 contract; nothing is deleted from the archive). A re-pointed
  archive row's ``object_ref`` is mirrored onto the survivor's
  ``signals.object_ref`` when that was empty (keeps the 0104 mirror invariant).
* ``trigger_state.seen_signal_ids`` (jsonb) — LEFT ALONE, safe by semantics:
  it is membership-only dedup memory for pending trigger fires; a deleted id
  simply never recurs, and rows are transient per-target state.
* OpenSearch corpus docs (``_id`` = signal id) and Qdrant embedding points —
  out-of-DB sidecar indexes; orphaned loser docs are TOLERATED (same posture as
  the ``signals_retention`` purge): every read path joins hits back to
  ``signals`` and drops missing rows, and the identical content remains
  reachable under the survivor's doc. ``--ids-out`` dumps the deleted ids as
  JSON for an optional later sidecar cleanup.

SAFETY RAILS
------------
* Distinct content is never touched: only groups sharing the exact
  ``(source_id, content_hash, owner_tenant)`` with ``content_hash <> ''``
  collapse — the empty-string "no hash" default is never a dedup key, and the
  same hash on a DIFFERENT source is genuine cross-source corroboration.
* Retention keep-set honored: a loser whose ``retention_class`` is
  ``retain_always`` / ``evidence_hold`` is NEVER deleted (counted
  ``held_skipped``) — mirrors the ``signals_retention`` exemption.
* Tenant-scoped grouping: rows never collapse across ``owner_tenant``.

Run in the registry container (runtime deps present) against the live Postgres:

  docker exec -e LEGBA_DATA_PG_DB=legba \\
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 \\
      -e PYTHONPATH=/app/src \\
      <registry-container> python3 /app/scripts/collapse_intrasource_dupes.py

Or on the host with the repo mounted:

  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/collapse_intrasource_dupes.py            # dry-run
  ... --apply                                                  # execute

Flags:
  --apply            execute (default: dry-run, read-only)
  --batch-groups N   duplicate groups per transaction (default 200)
  --limit-groups N   stop after N groups (default: all)
  --tenant T         restrict to one owner_tenant
  --source S         restrict to one source_id
  --sample N         sample groups printed in dry-run (default 10)
  --ids-out FILE     write the (would-be) deleted signal ids as JSON
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import asyncpg

#: Retention classes that are NEVER deleted regardless of duplication —
#: mirrors the ``signals_retention`` policy row's ``keep_classes`` (the C2
#: "one janitor" consolidation, migration 0109 /
#: ``deterministic_handlers._retention_sweep``).
KEEP_CLASSES = ("retain_always", "evidence_hold")

#: Every ``uuid[]`` column that can carry signal ids (lineage / value refs).
ARRAY_REFS: tuple[tuple[str, str], ...] = (
    ("signals", "derived_from"),
    ("facts", "derived_from"),
    ("analyst_outputs", "derived_from"),
    ("hypotheses", "derived_from"),
    ("proposed_edges", "derived_from"),
    ("situations", "derived_from"),
    ("entity_profiles", "derived_from"),
    ("nexuses", "derived_from"),
    ("nexuses", "source_signal_ids"),
)


async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


# Keyset-paginated duplicate-group scan. The composite key is the GROUP key, so
# apply-mode deletions never starve or loop the pagination (a group kept
# multi-row by held losers is passed once and never revisited in the same run).
_GROUPS_SQL = """
SELECT source_id, content_hash, owner_tenant, count(*) AS n
FROM signals
WHERE content_hash <> ''
  {tenant} {source}
  AND (source_id, content_hash, owner_tenant) > ({k1}::text, {k2}::text, {k3}::text)
GROUP BY source_id, content_hash, owner_tenant
HAVING count(*) > 1
ORDER BY source_id, content_hash, owner_tenant
LIMIT {lim}
"""

_ROWS_SQL = """
SELECT s.id, s.source_id, s.content_hash, s.owner_tenant, s.fetched_at,
       s.retention_class
FROM signals s
JOIN unnest($1::text[], $2::text[], $3::text[])
     AS g(source_id, content_hash, owner_tenant)
  ON s.source_id = g.source_id
 AND s.content_hash = g.content_hash
 AND s.owner_tenant = g.owner_tenant
ORDER BY s.source_id, s.content_hash, s.owner_tenant,
         s.fetched_at DESC, s.id DESC
"""

# Array re-point: rewrite loser ids to the survivor id inside a uuid[] column,
# preserving first-occurrence order and de-duplicating (loser + survivor both
# present collapses to one entry). Correlated subquery over the row's own array.
_ARRAY_REPOINT_SQL = """
UPDATE {table} SET {col} = (
    SELECT COALESCE(array_agg(newid ORDER BY first_ord), '{{}}'::uuid[])
    FROM (
        SELECT newid, min(ord) AS first_ord
        FROM (
            SELECT COALESCE(m.survivor_id, u.x) AS newid, u.ord
            FROM unnest({table}.{col}) WITH ORDINALITY AS u(x, ord)
            LEFT JOIN _collapse_map m ON m.loser_id = u.x
        ) rep
        GROUP BY newid
    ) dd
)
WHERE {col} && (SELECT array_agg(loser_id) FROM _collapse_map)
"""


def _count(status: str | None) -> int:
    """Trailing integer of an asyncpg command tag (``UPDATE 7`` / ``DELETE 3``)."""
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


def elect(rows: list[Any]) -> tuple[Any, list[Any], list[Any]]:
    """Split one group's rows into ``(survivor, losers, held)``.

    ``rows`` are one group's signals rows. Survivor = newest ``fetched_at``
    (ties broken by highest id — byte-identical to the live S-4 bump's
    ``ORDER BY fetched_at DESC, id DESC LIMIT 1`` pick). Losers in the
    retention keep-set are returned as ``held`` and never deleted.
    """
    ordered = sorted(rows, key=lambda r: (r["fetched_at"], str(r["id"])), reverse=True)
    survivor, rest = ordered[0], ordered[1:]
    losers = [r for r in rest if r["retention_class"] not in KEEP_CLASSES]
    held = [r for r in rest if r["retention_class"] in KEEP_CLASSES]
    return survivor, losers, held


async def _fetch_group_batch(
    conn: asyncpg.Connection,
    *,
    after: tuple[str, str, str] | None,
    batch_groups: int,
    tenant: str | None,
    source: str | None,
) -> list[Any]:
    params: list[Any] = []

    def _p(v: Any) -> str:
        params.append(v)
        return f"${len(params)}"

    tclause = f"AND owner_tenant = {_p(tenant)}" if tenant is not None else ""
    sclause = f"AND source_id = {_p(source)}" if source is not None else ""
    key = after if after is not None else ("", "", "")
    sql = _GROUPS_SQL.format(
        tenant=tclause, source=sclause,
        k1=_p(key[0]), k2=_p(key[1]), k3=_p(key[2]),
        lim=int(batch_groups),
    )
    return await conn.fetch(sql, *params)


async def _apply_batch(
    conn: asyncpg.Connection,
    mapping: dict[Any, Any],
    counters: dict[str, int],
) -> None:
    """Re-point + delete one batch of losers atomically.

    ``mapping`` is ``loser_id -> survivor_id``. All statements share ONE
    transaction so a loser is never deleted with a reference still pointing at
    it (mirrors the signals_retention child-first idiom).
    """
    async with conn.transaction():
        await conn.execute(
            "CREATE TEMP TABLE _collapse_map "
            "(loser_id uuid PRIMARY KEY, survivor_id uuid NOT NULL) "
            "ON COMMIT DROP"
        )
        await conn.copy_records_to_table(
            "_collapse_map", records=list(mapping.items()),
            columns=["loser_id", "survivor_id"],
        )

        # 1) signal_entity_links → survivor (dedup on the PK), then drop loser rows.
        counters["links_repointed"] += _count(await conn.execute(
            """
            INSERT INTO signal_entity_links
                (signal_id, entity_id, role, confidence,
                 analyst_id, analyst_version, run_id, created_at)
            SELECT m.survivor_id, l.entity_id, l.role, l.confidence,
                   l.analyst_id, l.analyst_version, l.run_id, l.created_at
            FROM signal_entity_links l
            JOIN _collapse_map m ON l.signal_id = m.loser_id
            ON CONFLICT (signal_id, entity_id, role) DO NOTHING
            """
        ))
        counters["links_deleted"] += _count(await conn.execute(
            "DELETE FROM signal_entity_links "
            "WHERE signal_id IN (SELECT loser_id FROM _collapse_map)"
        ))

        # 2) signal_aliases → both sides re-pointed (no self-pairs), losers dropped.
        counters["aliases_repointed"] += _count(await conn.execute(
            """
            INSERT INTO signal_aliases
                (alias_signal_id, canonical_signal_id, reason, score,
                 produced_by, produced_at)
            SELECT COALESCE(ma.survivor_id, a.alias_signal_id),
                   COALESCE(mc.survivor_id, a.canonical_signal_id),
                   a.reason, a.score, a.produced_by, a.produced_at
            FROM signal_aliases a
            LEFT JOIN _collapse_map ma ON ma.loser_id = a.alias_signal_id
            LEFT JOIN _collapse_map mc ON mc.loser_id = a.canonical_signal_id
            WHERE (ma.loser_id IS NOT NULL OR mc.loser_id IS NOT NULL)
              AND COALESCE(ma.survivor_id, a.alias_signal_id)
                  <> COALESCE(mc.survivor_id, a.canonical_signal_id)
            ON CONFLICT (alias_signal_id, canonical_signal_id) DO NOTHING
            """
        ))
        counters["aliases_deleted"] += _count(await conn.execute(
            """
            DELETE FROM signal_aliases
            WHERE alias_signal_id IN (SELECT loser_id FROM _collapse_map)
               OR canonical_signal_id IN (SELECT loser_id FROM _collapse_map)
            """
        ))

        # 3) signals.canonical_signal_id → survivor (self-pointer becomes NULL).
        counters["canonical_repointed"] += _count(await conn.execute(
            """
            UPDATE signals s
               SET canonical_signal_id = CASE WHEN s.id = m.survivor_id
                                              THEN NULL ELSE m.survivor_id END,
                   updated_at = NOW()
              FROM _collapse_map m
             WHERE s.canonical_signal_id = m.loser_id
            """
        ))

        # 4) uuid[] lineage / value arrays.
        for table, col in ARRAY_REFS:
            n = _count(await conn.execute(
                _ARRAY_REPOINT_SQL.format(table=table, col=col)
            ))
            counters[f"array_{table}_{col}"] += n
            counters["array_rows_repointed"] += n

        # 5) evidence_archive sidecar (1:1, no FK by design). Re-point a loser's
        # row onto the survivor only when the survivor has none; prefer the
        # archived-status row. Everything else stays (evidence outlives rows).
        counters["archive_repointed"] += _count(await conn.execute(
            """
            WITH cand AS (
                SELECT ea.signal_id AS loser_id, m.survivor_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.survivor_id
                           ORDER BY (ea.status = 'archived') DESC,
                                    ea.archived_at DESC NULLS LAST,
                                    ea.signal_id
                       ) AS rn
                FROM evidence_archive ea
                JOIN _collapse_map m ON m.loser_id = ea.signal_id
                WHERE NOT EXISTS (SELECT 1 FROM evidence_archive s
                                  WHERE s.signal_id = m.survivor_id)
            )
            UPDATE evidence_archive ea
               SET signal_id = cand.survivor_id, updated_at = NOW()
              FROM cand
             WHERE ea.signal_id = cand.loser_id AND cand.rn = 1
            """
        ))
        # Mirror the re-pointed archive's content address onto the survivor row
        # when it had none (0104 mirror invariant: signals.object_ref = cas:…).
        counters["object_ref_mirrored"] += _count(await conn.execute(
            """
            UPDATE signals s
               SET object_ref = ea.object_ref, updated_at = NOW()
              FROM evidence_archive ea
             WHERE ea.signal_id = s.id
               AND s.id IN (SELECT DISTINCT survivor_id FROM _collapse_map)
               AND COALESCE(s.object_ref, '') = ''
               AND COALESCE(ea.object_ref, '') <> ''
            """
        ))
        counters["archive_left_in_place"] += int(await conn.fetchval(
            "SELECT count(*) FROM evidence_archive "
            "WHERE signal_id IN (SELECT loser_id FROM _collapse_map)"
        ) or 0)

        # 6) the losers themselves.
        counters["signals_deleted"] += _count(await conn.execute(
            "DELETE FROM signals WHERE id IN (SELECT loser_id FROM _collapse_map)"
        ))


async def _dry_run_reference_counts(
    conn: asyncpg.Connection, loser_ids: list[Any],
) -> dict[str, int]:
    """Exact per-class counts of rows currently referencing the losers."""
    out: dict[str, int] = {}
    out["links_rows"] = int(await conn.fetchval(
        "SELECT count(*) FROM signal_entity_links WHERE signal_id = ANY($1::uuid[])",
        loser_ids) or 0)
    out["alias_rows"] = int(await conn.fetchval(
        "SELECT count(*) FROM signal_aliases WHERE alias_signal_id = ANY($1::uuid[]) "
        "OR canonical_signal_id = ANY($1::uuid[])", loser_ids) or 0)
    out["canonical_rows"] = int(await conn.fetchval(
        "SELECT count(*) FROM signals WHERE canonical_signal_id = ANY($1::uuid[])",
        loser_ids) or 0)
    total_arrays = 0
    for table, col in ARRAY_REFS:
        n = int(await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE {col} && $1::uuid[]",
            loser_ids) or 0)
        out[f"array_{table}_{col}"] = n
        total_arrays += n
    out["array_rows"] = total_arrays
    out["archive_rows"] = int(await conn.fetchval(
        "SELECT count(*) FROM evidence_archive WHERE signal_id = ANY($1::uuid[])",
        loser_ids) or 0)
    return out


async def run(
    conn: asyncpg.Connection,
    *,
    apply: bool = False,
    batch_groups: int = 200,
    limit_groups: int | None = None,
    tenant: str | None = None,
    source: str | None = None,
    sample: int = 10,
    quiet: bool = False,
) -> dict[str, Any]:
    """Scan (and with ``apply=True`` collapse) all duplicate groups.

    Returns the counters dict (also printed unless ``quiet``).
    """
    def _say(msg: str) -> None:
        if not quiet:
            print(msg)

    counters: dict[str, int] = {
        "groups_seen": 0, "held_skipped": 0, "signals_deleted": 0,
        "links_repointed": 0, "links_deleted": 0,
        "aliases_repointed": 0, "aliases_deleted": 0,
        "canonical_repointed": 0, "array_rows_repointed": 0,
        "archive_repointed": 0, "archive_left_in_place": 0,
        "object_ref_mirrored": 0,
    }
    for table, col in ARRAY_REFS:
        counters[f"array_{table}_{col}"] = 0

    samples: list[dict[str, Any]] = []
    dry_loser_ids: list[Any] = []
    deleted_ids: list[str] = []
    after: tuple[str, str, str] | None = None
    done = False

    while not done:
        want = batch_groups
        if limit_groups is not None:
            want = min(want, limit_groups - counters["groups_seen"])
            if want <= 0:
                break
        groups = await _fetch_group_batch(
            conn, after=after, batch_groups=want, tenant=tenant, source=source,
        )
        if not groups:
            break
        after = (
            groups[-1]["source_id"], groups[-1]["content_hash"],
            groups[-1]["owner_tenant"],
        )
        rows = await conn.fetch(
            _ROWS_SQL,
            [g["source_id"] for g in groups],
            [g["content_hash"] for g in groups],
            [g["owner_tenant"] for g in groups],
        )
        by_group: dict[tuple[str, str, str], list[Any]] = {}
        for r in rows:
            by_group.setdefault(
                (r["source_id"], r["content_hash"], r["owner_tenant"]), []
            ).append(r)

        mapping: dict[Any, Any] = {}
        for key, grp_rows in by_group.items():
            if len(grp_rows) < 2:
                continue  # raced away / already collapsed
            counters["groups_seen"] += 1
            survivor, losers, held = elect(grp_rows)
            counters["held_skipped"] += len(held)
            for loser in losers:
                mapping[loser["id"]] = survivor["id"]
            if len(samples) < sample and losers:
                samples.append({
                    "source_id": key[0],
                    "content_hash": key[1][:16],
                    "owner_tenant": key[2],
                    "rows": len(grp_rows),
                    "survivor": str(survivor["id"]),
                    "survivor_fetched_at": str(survivor["fetched_at"]),
                    "losers": [str(x["id"]) for x in losers],
                    "held": [str(x["id"]) for x in held],
                })

        if mapping:
            if apply:
                await _apply_batch(conn, mapping, counters)
                deleted_ids.extend(str(k) for k in mapping)
                _say(
                    f"  batch: {len(mapping)} loser(s) collapsed "
                    f"(groups so far {counters['groups_seen']}, "
                    f"deleted so far {counters['signals_deleted']})"
                )
            else:
                dry_loser_ids.extend(mapping.keys())
                deleted_ids.extend(str(k) for k in mapping)

    result: dict[str, Any] = dict(counters)
    result["samples"] = samples
    result["deleted_ids"] = deleted_ids

    if not apply:
        result["would_delete"] = len(dry_loser_ids)
        refs = (
            await _dry_run_reference_counts(conn, dry_loser_ids)
            if dry_loser_ids else {}
        )
        result["reference_rows"] = refs
        _say("=" * 78)
        _say(" DRY RUN — no rows changed. Re-run with --apply to execute.")
        _say("=" * 78)
        _say(f" duplicate groups        : {counters['groups_seen']:>8,}")
        _say(f" loser rows to DELETE    : {len(dry_loser_ids):>8,}")
        _say(f" held (keep-set) skipped : {counters['held_skipped']:>8,}")
        if refs:
            _say(" reference rows to re-point before delete:")
            _say(f"   signal_entity_links rows : {refs['links_rows']:>8,}")
            _say(f"   signal_aliases rows      : {refs['alias_rows']:>8,}")
            _say(f"   canonical_signal_id rows : {refs['canonical_rows']:>8,}")
            _say(f"   uuid[] lineage rows      : {refs['array_rows']:>8,}")
            for table, col in ARRAY_REFS:
                n = refs.get(f"array_{table}_{col}", 0)
                if n:
                    _say(f"     - {table}.{col:<18}: {n:>8,}")
            _say(f"   evidence_archive rows    : {refs['archive_rows']:>8,}")
        if samples:
            _say("-" * 78)
            _say(f" sample ({len(samples)} of {counters['groups_seen']} groups):")
            for s in samples:
                _say(
                    f"  {s['source_id'][:38]:<38} hash={s['content_hash']} "
                    f"rows={s['rows']} tenant={s['owner_tenant']}"
                )
                _say(
                    f"    survivor {s['survivor']} "
                    f"(fetched {s['survivor_fetched_at']})"
                )
                _say(f"    losers   {', '.join(s['losers'])}")
                if s["held"]:
                    _say(f"    held     {', '.join(s['held'])} (keep-set, kept)")
        _say("=" * 78)
    else:
        _say("=" * 78)
        _say(
            "SUMMARY "
            f"groups={counters['groups_seen']} "
            f"deleted={counters['signals_deleted']} "
            f"held_skipped={counters['held_skipped']} "
            f"links_repointed={counters['links_repointed']} "
            f"links_deleted={counters['links_deleted']} "
            f"aliases_repointed={counters['aliases_repointed']} "
            f"aliases_deleted={counters['aliases_deleted']} "
            f"canonical_repointed={counters['canonical_repointed']} "
            f"arrays_repointed={counters['array_rows_repointed']} "
            f"archive_repointed={counters['archive_repointed']} "
            f"archive_left={counters['archive_left_in_place']} "
            f"object_ref_mirrored={counters['object_ref_mirrored']}"
        )
        _say("=" * 78)
    return result


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Collapse historical intra-source exact content-hash "
        "duplicate signals to their newest-fetched survivor "
        "(dry-run by default).",
    )
    ap.add_argument("--apply", action="store_true",
                    help="execute (default: dry-run, read-only)")
    ap.add_argument("--batch-groups", type=int, default=200)
    ap.add_argument("--limit-groups", type=int, default=None)
    ap.add_argument("--tenant", type=str, default=None)
    ap.add_argument("--source", type=str, default=None)
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--ids-out", type=str, default=None,
                    help="write the (would-be) deleted signal ids as JSON")
    args = ap.parse_args()

    conn = await _connect_pg()
    try:
        result = await run(
            conn,
            apply=args.apply,
            batch_groups=args.batch_groups,
            limit_groups=args.limit_groups,
            tenant=args.tenant,
            source=args.source,
            sample=args.sample,
        )
    finally:
        await conn.close()

    if args.ids_out:
        with open(args.ids_out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "applied": bool(args.apply),
                    "deleted_signal_ids": result["deleted_ids"],
                },
                fh, indent=2,
            )
        print(f" ids written: {args.ids_out} ({len(result['deleted_ids'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
