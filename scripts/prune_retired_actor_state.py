#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prune superseded RETIRED ``actor_state`` rows — DRY RUN by default.

WHY
---
The actor id embeds the descriptor's content hash, so every descriptor edit
mints a new actor and leaves the old row ``retired`` for good; nothing ever
removed one (901 on live 2026-11-14: 454 analyst, 280 source, 167 target).
They are not harmless: the reminder-GC sweep re-examines every retired row on
every reconcile (``runtime/reminder_gc.py``, W11-F268), and the actor roster
reads past them.

WHAT IS REMOVED — a row only when EVERY one of these holds
-----------------------------------------------------------
  1. ``lifecycle = 'retired'`` (re-checked in the DELETE itself);
  2. superseded by a newer head: its descriptor has a head (``is_head`` in the
     kind's descriptor table) whose version is NOT this row's version, AND a
     non-retired ``actor_state`` row runs that head version (so the
     descriptor stays represented by a live actor);
  3. no reminder: for each reminder name the actor would have registered
     (``reminder_gc.orphan_reminder_names``), the daprd sidecar answers GET
     404 (or an empty 200, the same rule as the reminder GC). Any other
     answer — present, an error, an unreachable sidecar — keeps the row;
  4. untouched for ``--older-than-days`` (default 30): ``updated_at`` is older
     (re-checked in the DELETE: a row written since the plan is kept).

Every other retired row is kept and the report says which rule kept it.

Modes:

  (no flags)  DRY RUN. One read-only transaction (and the sidecar GETs);
              prints the counts by kind and by reason, and with ``--list``
              every candidate with its reasons. Writes nothing.
  --apply     First writes every candidate row, whole (``row_to_json``), to
              ``--backup`` (JSON lines; default
              ``./actor_state_pruned_<UTC stamp>.jsonl``), fsyncs it, then
              deletes exactly those rows in one transaction, each still
              retired, still superseded, and with the ``updated_at`` that was
              backed up. Prints how many were deleted.

Usage (inside a container with the DB env, e.g. the runtime)::

      python3 scripts/prune_retired_actor_state.py                  # dry run
      python3 scripts/prune_retired_actor_state.py --list           # + rows
      python3 scripts/prune_retired_actor_state.py --apply --backup /path/x.jsonl

Restore: each backup line is one row's columns; re-insert with
``INSERT INTO public.actor_state SELECT * FROM json_populate_record(NULL::public.actor_state, $1::json)``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

#: Days a retired row must be untouched before it may go.
DEFAULT_OLDER_THAN_DAYS = 30

#: Every retired row, with its descriptor head and the live actor (if any)
#: that runs that head. $1 = days.
PLAN_SQL = """
WITH heads AS (
    SELECT 'analyst'::text AS actor_kind, descriptor_id, version
      FROM public.analyst_descriptors WHERE is_head
    UNION ALL
    SELECT 'source', descriptor_id, version
      FROM public.source_descriptors WHERE is_head
    UNION ALL
    SELECT 'target', descriptor_id, version
      FROM public.target_descriptors WHERE is_head
)
SELECT a.actor_id, a.actor_kind, a.descriptor_id, a.descriptor_version,
       a.lifecycle, a.updated_at, a.source_cursors,
       h.version AS head_version,
       live.actor_id AS live_actor_id,
       (a.updated_at < now() - make_interval(days => $1::int)) AS old_enough
  FROM public.actor_state a
  LEFT JOIN heads h
    ON h.actor_kind = a.actor_kind AND h.descriptor_id = a.descriptor_id
  LEFT JOIN LATERAL (
        SELECT b.actor_id
          FROM public.actor_state b
         WHERE b.actor_kind = a.actor_kind
           AND b.descriptor_id = a.descriptor_id
           AND b.descriptor_version = h.version
           AND b.lifecycle <> 'retired'
         ORDER BY b.updated_at DESC, b.actor_id
         LIMIT 1
  ) live ON TRUE
 WHERE a.lifecycle = 'retired'
 ORDER BY a.actor_kind, a.updated_at, a.actor_id
"""

#: The whole rows to back up, by id.
BACKUP_SQL = """
SELECT row_to_json(a)::text AS row_json
  FROM public.actor_state a
 WHERE a.actor_id = ANY($1::text[])
 ORDER BY a.actor_id
"""

#: Delete exactly the planned rows, each only if still retired, still at the
#: backed-up ``updated_at``, and still superseded by a live head actor.
#: $1 = ids, $2 = their updated_at (same order).
DELETE_SQL = """
DELETE FROM public.actor_state a
 USING unnest($1::text[], $2::timestamptz[]) AS k(actor_id, updated_at)
 WHERE a.actor_id = k.actor_id
   AND a.updated_at = k.updated_at
   AND a.lifecycle = 'retired'
   AND EXISTS (
        SELECT 1 FROM public.actor_state b
         WHERE b.actor_kind = a.actor_kind
           AND b.descriptor_id = a.descriptor_id
           AND b.descriptor_version <> a.descriptor_version
           AND b.lifecycle <> 'retired'
   )
RETURNING a.actor_id
"""

# Reasons a retired row is KEPT (first failing rule wins, in this order).
KEEP_RECENT = "updated within the window"
KEEP_NO_HEAD = "descriptor has no head"
KEEP_IS_HEAD = "row runs the head version"
KEEP_NO_LIVE = "no live actor runs the head"
KEEP_UNKNOWN_KIND = "kind has no reminder mapping"
KEEP_REMINDER = "a reminder is registered"
KEEP_REMINDER_UNKNOWN = "reminder state unknown (sidecar)"

#: ``(actor_type, actor_id, reminder_name) -> True present / False absent``;
#: raising means unknown (the row is kept).
ReminderProbe = Callable[[str, str, str], Awaitable[bool]]


@dataclass
class Verdict:
    actor_id: str
    actor_kind: str
    descriptor_id: str
    updated_at: datetime
    prune: bool
    reasons: list[str] = field(default_factory=list)


def _cursor_keys(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return list(raw.keys()) if isinstance(raw, dict) else []


def reminder_names(row: Any) -> list[str] | None:
    """The reminder names this actor would have registered; None for a kind
    the reminder GC does not map (such a row is kept)."""
    from legba.runtime.reminder_gc import _ACTOR_TYPE_BY_KIND, orphan_reminder_names
    from legba.runtime.state import ActorStateRecord, SourceCursor

    if row["actor_kind"] not in _ACTOR_TYPE_BY_KIND:
        return None
    rec = ActorStateRecord(
        actor_id=row["actor_id"], actor_kind=row["actor_kind"],
        descriptor_id=row["descriptor_id"], descriptor_version=row["descriptor_version"],
        lifecycle=row["lifecycle"],
        source_cursors={k: SourceCursor(source_id=k) for k in _cursor_keys(row["source_cursors"])},
    )
    return orphan_reminder_names(rec)


async def judge(row: Any, probe: ReminderProbe) -> Verdict:
    """Apply the four rules to one retired row (the reminder probe runs only
    for a row every other rule already lets go)."""
    v = Verdict(row["actor_id"], row["actor_kind"], row["descriptor_id"], row["updated_at"], False)
    if not row["old_enough"]:
        v.reasons.append(KEEP_RECENT)
    if row["head_version"] is None:
        v.reasons.append(KEEP_NO_HEAD)
    elif row["head_version"] == row["descriptor_version"]:
        v.reasons.append(KEEP_IS_HEAD)
    elif row["live_actor_id"] is None:
        v.reasons.append(KEEP_NO_LIVE)
    if v.reasons:
        return v
    names = reminder_names(row)
    if names is None:
        v.reasons.append(KEEP_UNKNOWN_KIND)
        return v
    from legba.runtime.reminder_gc import _ACTOR_TYPE_BY_KIND

    actor_type = _ACTOR_TYPE_BY_KIND[row["actor_kind"]]
    for name in names:
        try:
            present = await probe(actor_type, row["actor_id"], name)
        except Exception:  # noqa: BLE001 - unknown keeps the row
            v.reasons.append(KEEP_REMINDER_UNKNOWN)
            return v
        if present:
            v.reasons.append(KEEP_REMINDER)
            return v
    v.prune = True
    v.reasons = [
        "retired",
        f"superseded: head {str(row['head_version'])[:16]} runs as {row['live_actor_id']}",
        f"no reminder ({len(names)} checked)" if names else "no reminder (registers none)",
        "older than the window",
    ]
    return v


def build_sidecar_probe(*, sidecar_url: str | None = None, timeout_s: float = 10.0) -> ReminderProbe:
    """GET only: 404 (or an empty 200) = absent, 200 with a body = present,
    anything else raises (unknown). Never DELETEs, never activates."""
    from legba.runtime.reminder_gc import _reminder_body_present, dapr_sidecar_url

    base = (sidecar_url or dapr_sidecar_url()).rstrip("/")

    async def _probe(actor_type: str, actor_id: str, name: str) -> bool:
        import httpx

        url = f"{base}/v1.0/actors/{actor_type}/{actor_id}/reminders/{name}"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise RuntimeError(f"sidecar GET {resp.status_code} for {actor_id}/{name}")
        return _reminder_body_present(resp)

    return _probe


async def fetch_retired(conn: Any, *, older_than_days: int) -> list[Any]:
    """Every retired row with its head and live-head actor (one read)."""
    return await conn.fetch(PLAN_SQL, int(older_than_days))


async def plan(rows: list[Any], probe: ReminderProbe) -> list[Verdict]:
    """Judge each fetched row (the sidecar probes run here, after the read's
    transaction has closed)."""
    return [await judge(r, probe) for r in rows]


async def backup_rows(conn: Any, verdicts: list[Verdict], path: str) -> int:
    """Write the candidates' whole rows to ``path`` (JSON lines), fsynced.
    Refuses to overwrite an existing file."""
    ids = [v.actor_id for v in verdicts if v.prune]
    rows = await conn.fetch(BACKUP_SQL, ids)
    with open(path, "x", encoding="utf-8") as fh:
        for r in rows:
            fh.write(r["row_json"] + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return len(rows)


async def apply(conn: Any, verdicts: list[Verdict], backup_path: str) -> tuple[int, int]:
    """Back up, then delete the planned rows in one transaction. Returns
    ``(backed_up, deleted)``. Nothing is deleted unless every candidate was
    written to the backup."""
    todo = [v for v in verdicts if v.prune]
    if not todo:
        return 0, 0
    backed = await backup_rows(conn, verdicts, backup_path)
    if backed != len(todo):
        raise RuntimeError(f"backup wrote {backed} of {len(todo)} rows; nothing deleted")
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '10s'")
        await conn.execute("SET LOCAL statement_timeout = '60s'")
        gone = await conn.fetch(
            DELETE_SQL, [v.actor_id for v in todo], [v.updated_at for v in todo],
        )
    return backed, len(gone)


def report(verdicts: list[Verdict], *, older_than_days: int, list_rows: bool = False) -> str:
    out: list[str] = []
    total = Counter(v.actor_kind for v in verdicts)
    prune = Counter(v.actor_kind for v in verdicts if v.prune)
    out.append(f"retired rows: {len(verdicts)} ({dict(sorted(total.items()))})")
    out.append(
        f"would remove (retired, superseded by a live head, no reminder, "
        f"untouched > {older_than_days} d): {sum(prune.values())} ({dict(sorted(prune.items()))})"
    )
    kept = Counter((v.actor_kind, v.reasons[0]) for v in verdicts if not v.prune)
    if kept:
        out.append("kept, by first reason:")
        for (kind, why), n in sorted(kept.items()):
            out.append(f"  {kind:8s} {why}: {n}")
    if list_rows:
        for v in verdicts:
            if v.prune:
                out.append(f"  - {v.actor_id} updated {v.updated_at.isoformat()} :: {'; '.join(v.reasons)}")
    return "\n".join(out)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--older-than-days", type=int, default=DEFAULT_OLDER_THAN_DAYS)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sidecar-url", default=None)
    args = ap.parse_args(argv)
    if args.older_than_days < 1:
        ap.error("--older-than-days must be >= 1")
    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
        timeout=10, command_timeout=90,
        server_settings={"application_name": "prune_retired_actor_state"},
    )
    probe = build_sidecar_probe(sidecar_url=args.sidecar_url)
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL statement_timeout = '60s'")
            rows = await fetch_retired(conn, older_than_days=args.older_than_days)
        verdicts = await plan(rows, probe)
        print(report(verdicts, older_than_days=args.older_than_days, list_rows=args.list))
        if not args.apply:
            print("dry run: nothing written (pass --apply to back up and delete)")
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = args.backup or f"actor_state_pruned_{stamp}.jsonl"
        backed, deleted = await apply(conn, verdicts, path)
        print(f"backed up {backed} rows to {path}; deleted {deleted}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
