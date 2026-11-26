#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repair desk × unit heads left wrong before the head-integrity fix (lane N-A, one-off).

WHY. Until the head-integrity fix two write-path defects left the head chain wrong
(``legba.data.head_integrity`` has the full account, 2026-11-11):

  * a read whose JSON failed to parse (``unstructured``/``coerce_failed``, conf
    0.30) superseded the verified head — CD escalation ``fae8b3ca`` closed
    ``1423491b``, UA escalation ``7f6280e0`` closed ``6fb1cfff``;
  * a read with no derivable situation signature never clustered and stayed
    open beside the chain — CD escalation ``bb2ce5fc`` (11-03), 80 keys with
    more than one open row on 11-11.

The code now prevents both; this script repairs the rows that already stand.

WHAT IT DOES, per ``(analyst_id, target_id)`` key of a situation-bearing desk
stream (some row of the key carries a derived ``sig:`` signature; composition /
meta producers excluded by the registry-derived set; explicit ``sit:`` rows are
never touched). The candidates are the key's LIVE rows — open, or closed BY a
failed parse — and the head is the newest structured one
(``head_integrity.pick_head``, the rule the write path and the gathers use):

  (a) BACKUP — every row about to change is copied (id, key, superseded_by,
      superseded_at, the action) into ``analyst_outputs_head_repair_<YYYYMMDD>``
      before anything is written;
  (b) CLOSE ORPHANS — every other OPEN row of the key, a failed-parse row
      included, gets ``superseded_by = <head>`` plus a ``finding_supersessions``
      audit edge (reason ``head_key_fold`` / ``parse_failed_not_a_head``); its
      ``situation_signature`` is not touched;
  (c) RESTORE — a head that a failed parse closed is reopened
      (``superseded_by``/``superseded_at`` cleared). The old audit edge stays:
      the edge table is append-only history.

NOTHING IS DELETED. One transaction. DRY-RUN BY DEFAULT — prints the counts and
the actions on the named 11-11 ids, writes nothing; ``--apply`` writes.
Idempotent: after an apply every key has exactly one live row, so a second run
plans nothing (and the backup insert is ``ON CONFLICT DO NOTHING``).

Run inside the live runtime container after the release that ships the fix:

    docker exec -i legba-legba-runtime-dapr-1 python3 - < scripts/repair_desk_heads_once.py
    docker exec -i legba-legba-runtime-dapr-1 python3 - --apply < scripts/repair_desk_heads_once.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from legba.data import head_integrity as hi

#: The 11-11 ids the sweep named — printed so the operator can see their fate.
NAMED_IDS = ("bb2ce5fc", "fae8b3ca", "1423491b", "7f6280e0", "6fb1cfff")

#: The candidates: every LIVE desk row (open, or closed BY a failed parse),
#: with whether its key is situation-bearing. One statement.
CANDIDATES_SQL = f"""
WITH bearing AS (
    SELECT DISTINCT b.analyst_id, b.target_id
      FROM analyst_outputs b
     WHERE b.kind = 'finding'
       AND b.situation_signature LIKE 'sig:%'
       AND b.analyst_id IS NOT NULL AND b.target_id IS NOT NULL
)
SELECT f.id::text AS id, f.analyst_id, f.target_id, f.produced_at,
       f.superseded_by::text AS superseded_by, f.superseded_at,
       f.situation_signature, f.data -> 'tags' AS tags,
       (bk.analyst_id IS NOT NULL) AS key_bearing
  FROM analyst_outputs f
  LEFT JOIN bearing bk ON bk.analyst_id = f.analyst_id AND bk.target_id = f.target_id
 WHERE f.kind = 'finding'
   AND f.analyst_id IS NOT NULL AND f.target_id IS NOT NULL
   AND f.analyst_id <> ALL($1::text[])
   AND {hi.live_head_predicate("f")}
"""

_BACKUP_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id uuid PRIMARY KEY,
    analyst_id text,
    target_id text,
    superseded_by uuid,
    superseded_at timestamptz,
    action text NOT NULL,
    new_superseded_by uuid,
    backed_up_at timestamptz NOT NULL DEFAULT now()
)
"""

_BACKUP_INSERT = """
INSERT INTO {table} (id, analyst_id, target_id, superseded_by, superseded_at, action, new_superseded_by)
SELECT o.id, o.analyst_id, o.target_id, o.superseded_by, o.superseded_at, x.action, x.new_sup
  FROM unnest($1::uuid[], $2::text[], $3::uuid[]) AS x(id, action, new_sup)
  JOIN analyst_outputs o ON o.id = x.id
ON CONFLICT (id) DO NOTHING
"""

_CLOSE_SQL = """
UPDATE analyst_outputs SET superseded_by = $2, superseded_at = now()
 WHERE id = $1 AND superseded_by IS NULL
RETURNING id
"""

_EDGE_SQL = """
INSERT INTO finding_supersessions
    (superseded_finding_id, superseding_finding_id, situation_signature, reason, score, produced_by)
VALUES ($1, $2, $3, $4, 1.0, 'head_repair_once')
ON CONFLICT (superseded_finding_id, superseding_finding_id) DO NOTHING
"""

_RESTORE_SQL = """
UPDATE analyst_outputs SET superseded_by = NULL, superseded_at = NULL
 WHERE id = $1 AND superseded_by = $2
RETURNING id
"""


def _tags(raw: Any) -> list[str]:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(t) for t in raw] if isinstance(raw, list) else []


def plan_repair(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure: the closes and restores for the candidate rows (``CANDIDATES_SQL``).

    ``closes`` — ``FoldLink`` for every OPEN non-head row of a bearing key;
    ``restores`` — ``(head_id, failed_superseder_id)`` for a head a failed parse
    closed."""
    shaped: list[dict[str, Any]] = []
    for r in rows:
        sig = str(r.get("situation_signature") or "")
        shaped.append({
            "id": str(r["id"]),
            "analyst_id": r.get("analyst_id"),
            "target_id": r.get("target_id"),
            "produced_at": r.get("produced_at"),
            "superseded_by": r.get("superseded_by"),
            "tags": _tags(r.get("tags")),
            # The fold's bearing test reads this field: an explicit situation
            # keeps its own key; otherwise the KEY's bearing decides.
            "_repair_sig": sig if sig.startswith("sit:") else ("sig:" if r.get("key_bearing") else ""),
        })
    by_id = {r["id"]: r for r in shaped}
    links = hi.plan_key_fold(shaped, signature_key="_repair_sig")
    closes = [lk for lk in links if by_id[lk.superseded_id]["superseded_by"] is None]
    heads = {lk.superseding_id for lk in links}
    # A live row that is the only one of its key needs no close, but may still
    # be a head a failed parse closed.
    keys = Counter(hi.head_key(r) for r in shaped if not r["_repair_sig"].startswith("sit:"))
    for r in shaped:
        if r["_repair_sig"] == "sig:" and keys[hi.head_key(r)] == 1:
            heads.add(r["id"])
    restores = [
        (hid, str(by_id[hid]["superseded_by"]))
        for hid in sorted(heads)
        if by_id[hid]["superseded_by"] is not None and not hi.is_failed_parse(by_id[hid])
    ]
    multi = sum(
        1 for k, n in Counter(
            hi.head_key(r) for r in shaped
            if r["superseded_by"] is None and r["_repair_sig"] == "sig:"
        ).items() if n > 1
    )
    return {"closes": closes, "restores": restores, "keys_multi_open": multi,
            "candidates": len(shaped)}


async def repair_once(conn: Any, *, exclude: Sequence[str], apply: bool,
                      today: datetime | None = None) -> dict[str, Any]:
    rows = await conn.fetch(CANDIDATES_SQL, sorted(exclude))
    plan = plan_repair([dict(r) for r in rows])
    closes, restores = plan["closes"], plan["restores"]
    named = {}
    for lk in closes:
        if lk.superseded_id[:8] in NAMED_IDS:
            named[lk.superseded_id[:8]] = f"close -> {lk.superseding_id[:8]} ({lk.reason})"
    for hid, sup in restores:
        if hid[:8] in NAMED_IDS:
            named[hid[:8]] = f"restore (was closed by failed parse {sup[:8]})"
    out: dict[str, Any] = {
        "mode": "apply" if apply else "dry-run",
        "candidates": plan["candidates"],
        "keys_with_more_than_one_open_row": plan["keys_multi_open"],
        "close_orphans": sum(1 for lk in closes if lk.reason == hi.REASON_KEY_FOLD),
        "close_parse_failed": sum(1 for lk in closes if lk.reason == hi.REASON_PARSE_FAILED),
        "restore_heads": len(restores),
        "named": named,
    }
    if not apply or not (closes or restores):
        return out
    table = "analyst_outputs_head_repair_" + (today or datetime.now(timezone.utc)).strftime("%Y%m%d")
    async with conn.transaction():
        await conn.execute(_BACKUP_DDL.format(table=table))
        ids = [lk.superseded_id for lk in closes] + [h for h, _ in restores]
        actions = [f"close:{lk.reason}" for lk in closes] + ["restore"] * len(restores)
        new_sup = [lk.superseding_id for lk in closes] + [None] * len(restores)
        await conn.execute(_BACKUP_INSERT.format(table=table), ids, actions, new_sup)
        closed = restored = 0
        for hid, sup in restores:
            restored += await conn.fetchval(_RESTORE_SQL, hid, sup) is not None
        for lk in closes:
            if await conn.fetchval(_CLOSE_SQL, lk.superseded_id, lk.superseding_id) is None:
                continue
            closed += 1
            await conn.execute(_EDGE_SQL, lk.superseded_id, lk.superseding_id,
                               f"head:{lk.key[0]}:{lk.key[1]}", lk.reason)
    out.update({"backup_table": table, "closed": closed, "restored": restored})
    return out


async def _main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the repair (default: dry-run)")
    args = ap.parse_args(list(argv))

    from legba.data.analysts.deterministic_handlers.finding_supersession import (
        resolve_composition_analyst_ids,
    )
    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore

    pg = PostgresStore(PostgresConfig.from_env())
    await pg.connect()
    try:
        async with pg.pool.acquire() as conn:
            exclude = await resolve_composition_analyst_ids(conn)
            result = await repair_once(conn, exclude=sorted(exclude), apply=args.apply)
    finally:
        await pg.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
