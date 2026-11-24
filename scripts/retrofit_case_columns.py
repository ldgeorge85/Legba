#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""retrofit_case_columns.py — the case-bearing columns of the existing rows, bound to registered values (W9 TAX-4).

The producers bind these columns at write time (``legba.data.case_columns``);
this tool brings the rows written BEFORE that to the same contract:

  situations.category / events.category
      a registered classification ``<family>:<value>`` or ``unclassified``; a
      value that is a registered SUBJECT id (a ``target_descriptors`` row —
      the desk ids ``country_g20_us``, ``lane_hormuz`` …) moves to
      ``data.subject_id``; the producer's value is kept as
      ``data.category_raw`` whenever the column no longer holds it.
  events.event_type
      the ``event_type`` family (CHECK-bound; migration 0230) or
      ``unclassified``.
  entity_profiles.entity_type
      the ``entity_class`` family or ``unclassified`` (never the catch-all
      ``entity``).

Every row also gets TAG-1's ``data.classifications`` for its bound value (set,
not appended). The vocabulary is read from ``vocabulary_entries`` through the
registry's own ``VocabularyCache`` (a family the registry would leave unloaded
is unloaded here too, and falls back to its literal).

The binding is a pure function of the producer's value (``category_raw`` when
present, else the column), so a second pass over a retrofitted row computes
exactly what it holds and writes nothing: the tool is idempotent.

MODES
  --dry-run (the DEFAULT; read-only): one pass per table in keyset batches of
    ``--batch-size`` (default 1,000) on the primary key — never one long
    statement. Prints, per column, the distinct values and rows that are
    registered / subject-moved / unclassified, the rows a ``--apply`` would
    write, and the top 40 values that bind ``unclassified``.
    ``--json PATH`` writes the full census.
  --apply: one UPDATE per batch of 1,000, each batch its own transaction,
    with a RESUME CURSOR (``--cursor-file``, default
    ``./retrofit_case_columns.cursor.json``) rewritten after every committed
    batch, per table. A re-run resumes after the last committed id;
    ``--restart`` ignores the cursor. A row whose case column changed between
    the read and the write (a producer upserted it) is skipped by the UPDATE's
    guard and re-read on the next pass. ``updated_at`` is NOT touched — this
    is a re-labelling, not a new observation (the open-event read orders on
    it). THE APPLY IS THE OPERATOR'S ACT.

``--table`` limits either mode to one table; ``--analyst-id`` to one producer
(a preview, and the test seam).

USAGE
    python3 scripts/retrofit_case_columns.py                    # dry-run census
    python3 scripts/retrofit_case_columns.py --json /tmp/case_census.json
    python3 scripts/retrofit_case_columns.py --apply             # operator act; resumable
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
from pathlib import Path
from typing import Any
from uuid import UUID

from legba.data import case_columns as cc
from legba.data.tags import CLASSIFICATIONS_KEY, VocabularyIndex, stamp_classifications

BATCH_SIZE = 1000
TOP_N = 40
DEFAULT_CURSOR_FILE = "retrofit_case_columns.cursor.json"
_UUID_MIN = UUID(int=0)

TABLES: tuple[str, ...] = ("situations", "events", "entity_profiles")

#: The case columns per table, in report order.
COLUMNS: dict[str, tuple[str, ...]] = {
    "situations": ("category",),
    "events": ("category", "event_type"),
    "entity_profiles": ("entity_type",),
}

#: Per-row census statuses.
REGISTERED = "registered"
SUBJECT_MOVED = "subject_moved"
UNCLASSIFIED = "unclassified"
STATUSES: tuple[str, ...] = (REGISTERED, SUBJECT_MOVED, UNCLASSIFIED)

_CATEGORY_COLS = (
    "id, category, data->>'category_raw' AS category_raw, "
    "data->>'subject_id' AS subject_id, data->'tags' AS tags, "
    "data->>'event_class' AS event_class, "
    "data->'classifications' AS classifications"
)
_SELECT: dict[str, str] = {
    "situations": f"SELECT {_CATEGORY_COLS} FROM situations",
    "events": f"SELECT {_CATEGORY_COLS}, event_type FROM events",
    "entity_profiles": (
        "SELECT id, entity_type, entity_class, "
        "data->'classifications' AS classifications FROM entity_profiles"
    ),
}
#: The one read both modes use, per table: a keyset page on the primary key.
BATCH_SQL: dict[str, str] = {
    t: f"{sel}\n WHERE id > $1\n ORDER BY id\n LIMIT $2" for t, sel in _SELECT.items()
}
#: The same page scoped to one producer (``--analyst-id``; the test seam).
BATCH_SQL_ONE_ANALYST: dict[str, str] = {
    t: f"{sel}\n WHERE id > $1\n   AND analyst_id = $3\n ORDER BY id\n LIMIT $2"
    for t, sel in _SELECT.items()
}

#: One UPDATE per batch. Set (not append): the binding's data keys replace
#: any stale ones; the guard skips a row a producer rewrote since the read.
APPLY_SQL: dict[str, str] = {
    "situations": """
        UPDATE situations AS s
           SET category = v.category,
               data = (s.data - 'subject_id' - 'category_raw') || v.patch
          FROM unnest($1::uuid[], $2::text[], $3::jsonb[], $4::text[])
               AS v(id, category, patch, was)
         WHERE s.id = v.id
           AND s.category = v.was
    """,
    "events": """
        UPDATE events AS s
           SET category = v.category,
               event_type = v.event_type,
               data = (s.data - 'subject_id' - 'category_raw') || v.patch
          FROM unnest($1::uuid[], $2::text[], $3::jsonb[], $4::text[], $5::text[])
               AS v(id, category, patch, was, event_type)
         WHERE s.id = v.id
           AND s.category = v.was
    """,
    "entity_profiles": """
        UPDATE entity_profiles AS s
           SET entity_type = v.entity_type,
               data = s.data || v.patch
          FROM unnest($1::uuid[], $2::text[], $3::jsonb[], $4::text[])
               AS v(id, entity_type, patch, was)
         WHERE s.id = v.id
           AND s.entity_type = v.was
    """,
}


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# The per-row plan (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnOutcome:
    column: str
    raw: str          # the producer's value
    value: str        # what the column holds after the retrofit
    status: str       # REGISTERED / SUBJECT_MOVED / UNCLASSIFIED


@dataclass(frozen=True)
class RowPlan:
    id: UUID
    was: str                         # the case column as read (the guard)
    outcomes: tuple[ColumnOutcome, ...]
    patch: dict[str, Any]            # data keys to set
    changed: bool

    def value(self, column: str) -> str:
        return next(o.value for o in self.outcomes if o.column == column)


def _category_status(b: cc.CategoryBinding) -> str:
    if b.subject_id:
        return SUBJECT_MOVED
    return REGISTERED if b.category != cc.UNCLASSIFIED else UNCLASSIFIED


def plan_row(
    table: str, row: Any, *, families: VocabularyIndex, subjects: frozenset[str],
) -> RowPlan:
    """What the retrofit writes for one row — the producer's binding, applied
    to the value the producer wrote."""
    if table == "entity_profiles":
        raw = row["entity_type"] or ""
        value = cc.bind_entity_type(raw, families=families)
        status = REGISTERED if value != cc.UNCLASSIFIED else UNCLASSIFIED
        want = stamp_classifications(
            table, {"entity_class": row["entity_class"]}, {}, families=families,
        )[CLASSIFICATIONS_KEY]
        changed = value != raw or _json(row["classifications"]) != want
        return RowPlan(
            row["id"], raw, (ColumnOutcome("entity_type", raw, value, status),),
            {CLASSIFICATIONS_KEY: want}, changed,
        )
    current = row["category"] or ""
    raw = row["category_raw"] if row["category_raw"] is not None else current
    b = cc.bind_category(raw, families=families, subjects=subjects, hint=row["event_class"])
    tags = _json(row["tags"])
    stamped = cc.stamp_category_data(
        table, b, {"tags": tags} if isinstance(tags, list) else {}, families=families,
    )
    stamped.pop("tags", None)
    outcomes = [ColumnOutcome("category", raw, b.category, _category_status(b))]
    changed = (
        b.category != current
        or b.subject_id != row["subject_id"]
        or b.data_fields().get(cc.RAW_KEY) != row["category_raw"]
        or _json(row["classifications"]) != stamped[CLASSIFICATIONS_KEY]
    )
    if table == "events":
        et_raw = row["event_type"] or ""
        et = cc.bind_event_type(et_raw, families=families)
        outcomes.append(ColumnOutcome(
            "event_type", et_raw, et, REGISTERED if et != cc.UNCLASSIFIED else UNCLASSIFIED,
        ))
        changed = changed or et != et_raw
    return RowPlan(row["id"], current, tuple(outcomes), stamped, changed)


async def plan_batch(
    conn: Any, table: str, rows: list[Any], *, families: VocabularyIndex,
) -> list[RowPlan]:
    subjects: frozenset[str] = frozenset()
    if table != "entity_profiles":
        raws = [r["category_raw"] if r["category_raw"] is not None else r["category"] for r in rows]
        subjects = await cc.registered_subjects(conn, raws)
    return [plan_row(table, r, families=families, subjects=subjects) for r in rows]


async def iter_batches(
    conn: Any, table: str, *, batch_size: int = BATCH_SIZE,
    after_id: UUID | None = None, analyst_id: str | None = None,
):
    """Yield successive keyset batches of one table's rows."""
    cursor = after_id or _UUID_MIN
    while True:
        if analyst_id is None:
            rows = await conn.fetch(BATCH_SQL[table], cursor, batch_size)
        else:
            rows = await conn.fetch(BATCH_SQL_ONE_ANALYST[table], cursor, batch_size, analyst_id)
        if not rows:
            return
        yield rows
        cursor = rows[-1]["id"]
        if len(rows) < batch_size:
            return


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


@dataclass
class ColumnCensus:
    rows: int = 0
    by_raw: dict[str, Counter] = field(default_factory=dict)   # raw → Counter(status)

    def add(self, o: ColumnOutcome) -> None:
        self.rows += 1
        self.by_raw.setdefault(o.raw, Counter())[o.status] += 1


@dataclass
class Census:
    rows_scanned: Counter = field(default_factory=Counter)      # table → n
    rows_to_write: Counter = field(default_factory=Counter)     # table → n
    columns: dict[str, ColumnCensus] = field(default_factory=dict)  # "t.col" → census

    def add(self, table: str, plan: RowPlan) -> None:
        self.rows_scanned[table] += 1
        self.rows_to_write[table] += int(plan.changed)
        for o in plan.outcomes:
            self.columns.setdefault(f"{table}.{o.column}", ColumnCensus()).add(o)


async def take_census(
    conn: Any, families: VocabularyIndex, *, tables: tuple[str, ...] = TABLES,
    batch_size: int = BATCH_SIZE, analyst_id: str | None = None,
) -> Census:
    census = Census()
    for table in tables:
        census.columns.update({
            f"{table}.{c}": census.columns.get(f"{table}.{c}", ColumnCensus())
            for c in COLUMNS[table]
        })
        async for rows in iter_batches(conn, table, batch_size=batch_size, analyst_id=analyst_id):
            for plan in await plan_batch(conn, table, rows, families=families):
                census.add(table, plan)
    return census


def summarize(census: Census, families: VocabularyIndex, *, top: int = TOP_N) -> dict[str, Any]:
    """The dry-run report: per column, distinct values and rows per status,
    and the top unmapped values."""
    columns: dict[str, Any] = {}
    for key, col in census.columns.items():
        by_status = {s: {"values": 0, "rows": 0} for s in STATUSES}
        unmapped: list[tuple[str, int]] = []
        for raw, counts in col.by_raw.items():
            for status, n in counts.items():
                by_status[status]["values"] += 1
                by_status[status]["rows"] += n
            n_unc = counts.get(UNCLASSIFIED, 0)
            if n_unc and raw != cc.UNCLASSIFIED:
                unmapped.append((raw, n_unc))
        unmapped.sort(key=lambda t: (-t[1], t[0]))
        top_values = sorted(
            ((raw, sum(c.values()), c.most_common(1)[0][0]) for raw, c in col.by_raw.items()),
            key=lambda t: (-t[1], t[0]),
        )
        columns[key] = {
            "rows": col.rows,
            "distinct_values": len(col.by_raw),
            "by_status": by_status,
            "top_unmapped": [{"raw": r, "rows": n} for r, n in unmapped[:top]],
            "unmapped_total": len(unmapped),
            "top_values": [
                {"raw": r, "rows": n, "status": st} for r, n, st in top_values[:top]
            ],
        }
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "rows_scanned": dict(census.rows_scanned),
        "rows_to_write": dict(census.rows_to_write),
        "vocabulary": {
            "families_loaded": sorted(f for f in families.values if families.family_loaded(f)),
            "families_unloaded": sorted(families.unloaded),
        },
        "columns": columns,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# case-column retrofit census (dry-run, read-only) as of {report['as_of']}",
        f"vocabulary loaded: {', '.join(report['vocabulary']['families_loaded']) or '—'}; "
        f"unloaded: {', '.join(report['vocabulary']['families_unloaded']) or '—'}",
    ]
    for table, n in report["rows_scanned"].items():
        lines.append(
            f"{table}: rows scanned {n:,}; an --apply would write "
            f"{report['rows_to_write'].get(table, 0):,}"
        )
    for key, col in report["columns"].items():
        lines += ["", f"## {key} — {col['rows']:,} rows, {col['distinct_values']:,} distinct values",
                  "  status          values /       rows"]
        for st, slot in col["by_status"].items():
            lines.append(f"  {st:<14} {slot['values']:>7,} / {slot['rows']:>10,}")
        lines.append(f"  top {len(col['top_unmapped'])} unmapped (of {col['unmapped_total']:,}):")
        lines += [f"    {r['rows']:>8,}  {r['raw']!r}" for r in col["top_unmapped"]]
        if not col["top_unmapped"]:
            lines.append("    — none")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


def _read_cursor(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _write_cursor(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


async def _write_batch(conn: Any, table: str, plans: list[RowPlan]) -> int:
    todo = [p for p in plans if p.changed]
    if not todo:
        return 0
    ids = [p.id for p in todo]
    patches = [json.dumps(p.patch) for p in todo]
    was = [p.was for p in todo]
    if table == "entity_profiles":
        args = (ids, [p.value("entity_type") for p in todo], patches, was)
    elif table == "events":
        args = (ids, [p.value("category") for p in todo], patches, was,
                [p.value("event_type") for p in todo])
    else:
        args = (ids, [p.value("category") for p in todo], patches, was)
    status = await conn.execute(APPLY_SQL[table], *args)
    return int(status.split()[-1]) if isinstance(status, str) else len(todo)


async def apply(
    conn: Any,
    families: VocabularyIndex,
    *,
    cursor_path: Path,
    tables: tuple[str, ...] = TABLES,
    batch_size: int = BATCH_SIZE,
    restart: bool = False,
    max_batches: int | None = None,
    analyst_id: str | None = None,
) -> dict[str, Any]:
    """Write the retrofit batch by batch, resumably, table by table."""
    prior = None if restart else _read_cursor(cursor_path)
    if prior and prior.get("analyst_id") != analyst_id:
        raise SystemExit(
            f"cursor {cursor_path} was written for analyst_id={prior.get('analyst_id')!r}; "
            f"pass --restart or the same --analyst-id"
        )
    state: dict[str, Any] = {"analyst_id": analyst_id, "tables": {}, "done": False}
    if prior:
        state["tables"] = dict(prior.get("tables") or {})
    done_this_run = 0
    for table in tables:
        t = state["tables"].setdefault(table, {
            "last_id": None, "batches": 0, "rows_scanned": 0, "rows_written": 0, "done": False,
        })
        if t["done"]:
            continue
        after = UUID(t["last_id"]) if t["last_id"] else None
        async for rows in iter_batches(
            conn, table, batch_size=batch_size, after_id=after, analyst_id=analyst_id,
        ):
            plans = await plan_batch(conn, table, rows, families=families)
            async with conn.transaction():
                written = await _write_batch(conn, table, plans)
            t["last_id"] = str(rows[-1]["id"])
            t["batches"] += 1
            t["rows_scanned"] += len(rows)
            t["rows_written"] += written
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_cursor(cursor_path, state)
            done_this_run += 1
            if max_batches is not None and done_this_run >= max_batches:
                return state
        t["done"] = True
        _write_cursor(cursor_path, state)
    state["done"] = all(state["tables"].get(t, {}).get("done") for t in tables)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_cursor(cursor_path, state)
    return state


async def load_families(store: Any) -> VocabularyIndex:
    """The vocabulary as the registry would load it (per-family degrade)."""
    from legba.data.registry.vocabulary_cache import VocabularyCache

    cache = VocabularyCache(store)
    await cache.refresh()
    return VocabularyIndex.from_cache(
        cache, loaded_at=datetime.now(timezone.utc).timestamp(),
    )


async def _main_async(args: argparse.Namespace) -> int:
    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore

    tables = (args.table,) if args.table else TABLES
    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    try:
        families = await load_families(store)
        async with store.acquire() as conn:
            if args.apply:
                state = await apply(
                    conn, families, cursor_path=Path(args.cursor_file), tables=tables,
                    batch_size=args.batch_size, restart=args.restart,
                    max_batches=args.max_batches, analyst_id=args.analyst_id,
                )
                print(json.dumps(state, indent=2, sort_keys=True))
                return 0
            await conn.execute("SET statement_timeout = '120s'")
            census = await take_census(
                conn, families, tables=tables, batch_size=args.batch_size,
                analyst_id=args.analyst_id,
            )
        report = summarize(census, families, top=args.top)
        print(render(report))
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(f"\n# full census written to {args.json}")
        return 0
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only census (the default)")
    mode.add_argument("--apply", action="store_true", help="write the retrofit (operator act)")
    p.add_argument("--table", choices=TABLES, default=None, help="limit to one table")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--top", type=int, default=TOP_N)
    p.add_argument("--json", help="write the full dry-run census to this path")
    p.add_argument("--cursor-file", default=DEFAULT_CURSOR_FILE)
    p.add_argument("--restart", action="store_true", help="ignore an existing cursor")
    p.add_argument("--max-batches", type=int, default=None)
    p.add_argument("--analyst-id", default=None, help="limit to one producer")
    args = p.parse_args(argv)
    if args.batch_size < 1:
        p.error("--batch-size must be >= 1")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
