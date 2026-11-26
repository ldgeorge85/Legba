#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Correct the journal rows written without evidence since the 10-23 cutover (lane N-G, one-off).

WHY. From 10-24 about half the journal-family runs collapsed at the field-notes
handoff and NARRATE wrote from nothing: invented leads, invented ``[[ref:]]``
uuids (``legba.data.journal_evidence`` has the account). The publish gate now
withholds such drafts; this script withdraws the ones already published.

WHAT IT DOES. Every OPEN journal-family row (``journal_evidence.GATED_ENTRY_KINDS``)
produced since ``--since`` (default 2026-10-24) is put through the SAME test the
publish gate applies (``evaluate_publish_gate``: control text, zero refs, or fewer
than 80 % of cited refs resolving in ``REF_TABLES``). Rows already carrying
``evidence_unavailable`` (declared by the gate) are skipped. Refs are the
structured column UNION every inline citation form in the body
(``collect_refs``), and lens / inquiry / crossroads rows are judged
with their entry kind (zero refs withholds them only as a stub / tool call).
A short-id ref (8-hex prefix) counts as resolved only when its prefix
matches exactly one stored row created before the citing row.

  * DRY-RUN (default): prints every failing row with its resolve counts, and the
    totals by tier. Writes nothing.
  * ``--apply``: in ONE transaction, (a) copies every affected row in full into
    ``journal_entries_correction_backup_<YYYYMMDD>``, (b) writes ONE
    ``correction`` entry citing them with a reason each (keyed
    ``--key``; an existing correction with that key is reused), (c) closes each
    row with ``superseded_by`` = the correction + ``valid_until`` (content
    untouched). Idempotent: closed rows are not open, so a second run plans
    nothing.

Run inside the runtime container after the release that ships it (dry-run first):

    docker exec -i legba-legba-runtime-dapr-1 python3 - < scripts/journal_correct_once.py
    docker exec -i legba-legba-runtime-dapr-1 python3 - --apply < scripts/journal_correct_once.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from legba.data import journal_evidence as je

DEFAULT_SINCE = "2026-10-24T00:00:00+00:00"
DEFAULT_KEY = "evidence_gate_2026-11"
REASON = (
    "These rows were written after the field-notes handoff failed: their cited "
    "refs do not resolve to stored evidence (or they carry model control text), "
    "so their content is absent from the corpus."
)

CANDIDATES_SQL = """
SELECT id::text AS id, entry_kind, analyst_id, produced_at, period_start, period_end,
       body, cited_substrate_refs::text[] AS refs
  FROM journal_entries
 WHERE entry_kind = ANY($1::text[])
   AND produced_at >= $2
   AND valid_until IS NULL AND superseded_by IS NULL
   AND NOT ($3 = ANY(honesty_flags))
 ORDER BY produced_at
"""

_BACKUP_DDL = (
    "CREATE TABLE IF NOT EXISTS {table} "
    "(LIKE journal_entries INCLUDING DEFAULTS, backed_up_at timestamptz NOT NULL DEFAULT now())"
)
_BACKUP_INSERT = (
    "INSERT INTO {table} SELECT j.*, now() FROM journal_entries j "
    "WHERE j.id = ANY($1::uuid[]) AND NOT EXISTS (SELECT 1 FROM {table} b WHERE b.id = j.id)"
)


def plan_corrections(
    rows: Sequence[Mapping[str, Any]], resolved: set[str] | None,
) -> list[dict[str, Any]]:
    """Pure: the rows that fail the publish-gate test, each with its counts."""
    out: list[dict[str, Any]] = []
    for r in rows:
        refs = je.collect_refs(r.get("refs"), r.get("body"))
        # N-I: a short-id prefix resolves only to a row older than the citing row.
        row_resolved = (
            resolved.as_of(r.get("produced_at")) if isinstance(resolved, je.ResolvedRefs)
            else resolved
        )
        v = je.evaluate_publish_gate(
            body=r.get("body"), refs=refs, resolved=row_resolved, entry_kind=r.get("entry_kind"),
        )
        if not v.withhold:
            continue
        out.append({
            "id": str(r["id"]),
            "entry_kind": r.get("entry_kind"),
            "analyst_id": r.get("analyst_id"),
            "produced_at": r.get("produced_at"),
            "period_start": r.get("period_start"),
            "period_end": r.get("period_end"),
            "reason": v.reason,
            "refs_total": v.refs_total,
            "refs_resolved": v.refs_resolved,
            "refs_resolved_by_prefix": v.refs_resolved_by_prefix,
            "detail": je.verdict_detail(v),
        })
    return out


def summarize(plan: Sequence[Mapping[str, Any]], candidates: int) -> dict[str, Any]:
    return {
        "candidates": candidates,
        "would_correct": len(plan),
        "by_tier": dict(sorted(Counter(p["entry_kind"] for p in plan).items())),
        "by_analyst": dict(sorted(Counter(p["analyst_id"] for p in plan).items())),
        "by_reason": dict(sorted(Counter(p["reason"] for p in plan).items())),
    }


async def correct_once(
    conn: Any, *, since: datetime, key: str, apply: bool, today: datetime | None = None,
) -> dict[str, Any]:
    rows = [dict(r) for r in await conn.fetch(
        CANDIDATES_SQL, sorted(je.GATED_ENTRY_KINDS), since, je.EVIDENCE_UNAVAILABLE_FLAG,
    )]
    all_refs = {x for r in rows for x in je.collect_refs(r.get("refs"), r.get("body"))}
    resolved = await je.resolve_ref_existence(conn, sorted(all_refs))
    if resolved is None:
        raise SystemExit("ref resolution unavailable — refusing to plan a correction")
    plan = plan_corrections(rows, resolved)
    out: dict[str, Any] = {"mode": "apply" if apply else "dry-run", **summarize(plan, len(rows))}
    out["rows"] = [
        f"{p['entry_kind']:<13} {p['analyst_id'] or '-':<22} {str(p['produced_at'])[:16]} "
        f"{p['id'][:8]} {p['reason']:<15} {p['refs_resolved']}/{p['refs_total']}"
        for p in plan
    ]
    if not apply or not plan:
        return out
    from legba.data.provenance.journal_correction import (
        close_rows_by_correction,
        write_correction_entry,
    )

    table = "journal_entries_correction_backup_" + (
        today or datetime.now(timezone.utc)
    ).strftime("%Y%m%d")
    ids = [p["id"] for p in plan]
    async with conn.transaction():
        await conn.execute(_BACKUP_DDL.format(table=table))
        await conn.execute(_BACKUP_INSERT.format(table=table), ids)
        cid, created = await write_correction_entry(
            conn, correction_key=key, corrects=plan, reason=REASON,
        )
        closed = await close_rows_by_correction(conn, correction_id=cid, ids=ids)
    out.update({
        "backup_table": table, "correction_id": str(cid),
        "correction_created": created, "closed": len(closed),
    })
    return out


async def _main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the correction (default: dry-run)")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="ISO timestamp lower bound")
    ap.add_argument("--key", default=DEFAULT_KEY, help="idempotency key of the correction row")
    args = ap.parse_args(list(argv))
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore

    pg = PostgresStore(PostgresConfig.from_env())
    await pg.connect()
    try:
        async with pg.pool.acquire() as conn:
            result = await correct_once(conn, since=since, key=args.key, apply=args.apply)
    finally:
        await pg.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
