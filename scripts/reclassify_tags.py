#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""reclassify_tags.py — the raw `signals.tags` values, bound to the registered vocabulary (W7 TAG-1).

`signals.tags` holds ~17k distinct raw strings — publisher RSS categories, not
a taxonomy. This script binds each one with the platform's single rule
(`legba.data.tags.bind_tag`) against the vocabulary as registered NOW (read
from `vocabulary_entries` through the registry's own `VocabularyCache`, so a
family the registry would leave unloaded is unloaded here too):

  * `<family>:<value>` of a loaded family with a known value → `bound`;
  * a family-shaped prefix that is unloaded, or a value it does not know →
    `unclassified` (the raw string kept);
  * a bare value → `src:<value>`, `legacy_src` — provenance, not a class.

MODES
  --dry-run (the DEFAULT; read-only): one pass over `signals` in keyset batches
    of `--batch-size` (default 1,000) on the primary key — never one long
    statement. Prints the distinct-value and occurrence counts per family and
    status, the top 40 `unclassified` values, the top 40 `legacy_src` values,
    and the CROSSWALK CANDIDATES: non-bound values whose normalized form
    (case-folded, non-alphanumerics removed) equals a registered value or
    alias in some loaded family — the rows a profile's `<profile>.crosswalk`
    family (DEC-12) would be seeded from. `--json PATH` writes the full census.
  --apply: writes `signals.data.classifications` for every TAGGED row (rows
    with no tags stay `data IS NULL`) as the UNION of the row's EXISTING
    entries and the binding of its raw tags (`merged_classifications`):
    an entry the insert-time stamper wrote that no raw tag yields
    — the source's declared `src:` scope entries — is kept in place, an entry
    a raw tag yields is re-bound against the vocabulary as registered now, and
    the remaining raw-tag bindings follow in tag order; one entry per
    `(family, value)`. Both stampers converge on ONE shape. One UPDATE per batch of 1,000, each
    batch its own transaction, with a RESUME CURSOR (`--cursor-file`, default
    `./reclassify_tags.cursor.json`) rewritten after every committed batch. A
    re-run resumes after the last committed id; `--restart` ignores the
    cursor. A crash between a commit and the cursor write re-does one batch —
    the write is idempotent (a second apply over its own output is a no-op).
    `signals.tags` is never rewritten.

`--source-id` limits either mode to one source (a preview, and the test seam).

USAGE
    python3 scripts/reclassify_tags.py                     # dry-run census
    python3 scripts/reclassify_tags.py --json /tmp/tags_census.json
    python3 scripts/reclassify_tags.py --apply              # operator act; resumable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from legba.data.tags import (
    STATUS_BOUND,
    STATUS_LEGACY_SRC,
    STATUS_UNCLASSIFIED,
    VocabularyIndex,
    bind_tag,
    classifications,
)

BATCH_SIZE = 1000
TOP_N = 40
DEFAULT_CURSOR_FILE = "reclassify_tags.cursor.json"
_UUID_MIN = UUID(int=0)

#: The one read both modes use: a keyset page on the primary key.
BATCH_SQL = """
    SELECT id, tags, data -> 'classifications' AS classifications
      FROM signals
     WHERE id > $1
     ORDER BY id
     LIMIT $2
"""
BATCH_SQL_ONE_SOURCE = """
    SELECT id, tags, data -> 'classifications' AS classifications
      FROM signals
     WHERE id > $1
       AND source_id = $3
     ORDER BY id
     LIMIT $2
"""
#: One UPDATE per batch: set the key to the merged list (`merged_classifications`).
APPLY_SQL = """
    UPDATE signals AS s
       SET data = COALESCE(s.data, '{}'::jsonb)
                  || jsonb_build_object('classifications', v.c)
      FROM unnest($1::uuid[], $2::jsonb[]) AS v(id, c)
     WHERE s.id = v.id
"""

_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize(value: str) -> str:
    """The crosswalk comparison form: case-folded, non-alphanumerics removed
    (`Armed Group`, `armed_group`, `ArmedGroup` → `armedgroup`)."""
    return _NORM_RE.sub("", value.casefold())


async def iter_batches(
    conn: Any, *, batch_size: int = BATCH_SIZE, after_id: UUID | None = None,
    source_id: str | None = None,
):
    """Yield successive keyset batches of (id, tags) records."""
    cursor = after_id or _UUID_MIN
    while True:
        if source_id is None:
            rows = await conn.fetch(BATCH_SQL, cursor, batch_size)
        else:
            rows = await conn.fetch(BATCH_SQL_ONE_SOURCE, cursor, batch_size, source_id)
        if not rows:
            return
        yield rows
        cursor = rows[-1]["id"]
        if len(rows) < batch_size:
            return


@dataclass
class Census:
    rows_scanned: int = 0
    rows_tagged: int = 0
    occurrences: Counter = field(default_factory=Counter)  # raw → count

    def add(self, tags: Any) -> None:
        self.rows_scanned += 1
        tags = [t for t in (tags or []) if isinstance(t, str)]
        if tags:
            self.rows_tagged += 1
            self.occurrences.update(tags)


async def take_census(
    conn: Any, *, batch_size: int = BATCH_SIZE, source_id: str | None = None,
) -> Census:
    census = Census()
    async for rows in iter_batches(conn, batch_size=batch_size, source_id=source_id):
        for r in rows:
            census.add(r["tags"])
    return census


def _crosswalk_targets(families: VocabularyIndex) -> dict[str, list[str]]:
    """normalized form → ['<family>:<canonical>', ...] over every loaded
    family's values and aliases."""
    out: dict[str, set[str]] = {}
    for fam, values in families.values.items():
        if not families.family_loaded(fam):
            continue
        for v in values:
            out.setdefault(normalize(v), set()).add(f"{fam}:{v}")
        for alias, canon in families.aliases.get(fam, {}).items():
            out.setdefault(normalize(alias), set()).add(f"{fam}:{canon}")
    return {k: sorted(v) for k, v in out.items() if k}


def summarize(census: Census, families: VocabularyIndex, *, top: int = TOP_N) -> dict[str, Any]:
    """The dry-run report: counts per (family, status), the top unclassified
    and legacy values, and the crosswalk candidates."""
    by_key: dict[tuple[str, str], dict[str, int]] = {}
    unclassified: list[tuple[str, int, str]] = []
    legacy: list[tuple[str, int]] = []
    candidates: list[dict[str, Any]] = []
    targets = _crosswalk_targets(families)
    for raw, n in census.occurrences.items():
        b = bind_tag(raw, families=families)
        slot = by_key.setdefault((b.family or "(empty)", b.status), {"values": 0, "occurrences": 0})
        slot["values"] += 1
        slot["occurrences"] += n
        if b.status == STATUS_UNCLASSIFIED:
            unclassified.append((raw, n, b.family))
        elif b.status == STATUS_LEGACY_SRC:
            legacy.append((raw, n))
        if b.status != STATUS_BOUND:
            hits = targets.get(normalize(b.value))
            if hits:
                candidates.append({"raw": raw, "occurrences": n, "candidates": hits})
    by_status: dict[str, dict[str, int]] = {}
    for (_fam, status), slot in by_key.items():
        agg = by_status.setdefault(status, {"values": 0, "occurrences": 0})
        agg["values"] += slot["values"]
        agg["occurrences"] += slot["occurrences"]
    unclassified.sort(key=lambda t: (-t[1], t[0]))
    legacy.sort(key=lambda t: (-t[1], t[0]))
    candidates.sort(key=lambda c: (-c["occurrences"], c["raw"]))
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "rows_scanned": census.rows_scanned,
        "rows_tagged": census.rows_tagged,
        "distinct_values": len(census.occurrences),
        "tag_occurrences": sum(census.occurrences.values()),
        "vocabulary": {
            "families_loaded": sorted(f for f in families.values if families.family_loaded(f)),
            "families_unloaded": sorted(families.unloaded),
        },
        "by_status": dict(sorted(by_status.items())),
        "by_family_status": [
            {"family": fam, "status": st, **slot}
            for (fam, st), slot in sorted(by_key.items(), key=lambda kv: (-kv[1]["occurrences"], kv[0]))
        ],
        "top_unclassified": [
            {"raw": r, "occurrences": n, "family": f} for r, n, f in unclassified[:top]
        ],
        "top_legacy_src": [{"raw": r, "occurrences": n} for r, n in legacy[:top]],
        "crosswalk_candidates": candidates[:top],
        "crosswalk_candidates_total": len(candidates),
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# tag reclassification census (dry-run, read-only) as of {report['as_of']}",
        f"rows scanned {report['rows_scanned']:,}; tagged {report['rows_tagged']:,}; "
        f"distinct raw values {report['distinct_values']:,}; "
        f"tag occurrences {report['tag_occurrences']:,}",
        f"vocabulary loaded: {', '.join(report['vocabulary']['families_loaded']) or '—'}; "
        f"unloaded: {', '.join(report['vocabulary']['families_unloaded']) or '—'}",
        "",
        "## by status (distinct values / occurrences)",
    ]
    for st, slot in report["by_status"].items():
        lines.append(f"  {st:<13} {slot['values']:>8,} / {slot['occurrences']:>10,}")
    lines += ["", "## by family and status (distinct values / occurrences)"]
    for row in report["by_family_status"]:
        lines.append(
            f"  {row['family']:<32} {row['status']:<13} "
            f"{row['values']:>8,} / {row['occurrences']:>10,}"
        )
    lines += ["", f"## top {len(report['top_unclassified'])} unclassified (occurrences)"]
    lines += [f"  {r['occurrences']:>8,}  [{r['family']}] {r['raw']!r}" for r in report["top_unclassified"]]
    if not report["top_unclassified"]:
        lines.append("  — none")
    lines += ["", f"## top {len(report['top_legacy_src'])} legacy_src (occurrences)"]
    lines += [f"  {r['occurrences']:>8,}  {r['raw']!r}" for r in report["top_legacy_src"]]
    lines += [
        "",
        f"## crosswalk candidates (top {len(report['crosswalk_candidates'])} of "
        f"{report['crosswalk_candidates_total']:,})",
    ]
    lines += [
        f"  {c['occurrences']:>8,}  {c['raw']!r} → {', '.join(c['candidates'])}"
        for c in report["crosswalk_candidates"]
    ]
    if not report["crosswalk_candidates"]:
        lines.append("  — none")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


def _existing_entries(raw: Any) -> list[dict[str, Any]]:
    """The row's current ``data.classifications`` entries (well-formed only)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return [
        e for e in raw
        if isinstance(e, dict) and isinstance(e.get("family"), str) and e.get("family")
        and isinstance(e.get("value"), str)
    ]


def merged_classifications(
    existing: Any, tags: list[str], *, families: VocabularyIndex,
) -> list[dict[str, Any]]:
    """The ONE ``data.classifications`` shape both stampers converge on.

    The row's existing entries first, in their order: one a raw tag also yields
    is replaced by that tag's binding NOW (its status refreshed), one no raw
    tag yields (a TAX-2 ``src:`` scope entry, a declared classification) is
    kept verbatim. Then every remaining raw-tag binding, in tag order. One
    entry per ``(family, value)``. Idempotent over its own output.
    """
    fresh = classifications(tags, families=families)
    by_key = {(c["family"], c["value"]): c for c in fresh}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in [*_existing_entries(existing), *fresh]:
        key = (entry["family"], entry["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append(by_key.get(key, entry))
    return out


def _read_cursor(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _write_cursor(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


async def apply(
    conn: Any,
    families: VocabularyIndex,
    *,
    cursor_path: Path,
    batch_size: int = BATCH_SIZE,
    restart: bool = False,
    max_batches: int | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Write `signals.data.classifications` batch by batch, resumably."""
    prior = None if restart else _read_cursor(cursor_path)
    if prior and prior.get("source_id") != source_id:
        raise SystemExit(
            f"cursor {cursor_path} was written for source_id={prior.get('source_id')!r}; "
            f"pass --restart or the same --source-id"
        )
    state: dict[str, Any] = {
        "last_id": None, "batches": 0, "rows_scanned": 0, "rows_written": 0,
        "source_id": source_id, "done": False,
    }
    if prior:
        state.update({k: prior[k] for k in state if k in prior})
        state["done"] = False
    after = UUID(state["last_id"]) if state["last_id"] else None
    done_this_run = 0
    async for rows in iter_batches(conn, batch_size=batch_size, after_id=after, source_id=source_id):
        ids: list[UUID] = []
        payloads: list[str] = []
        for r in rows:
            tags = [t for t in (r["tags"] or []) if isinstance(t, str)]
            if not tags:
                continue
            ids.append(r["id"])
            payloads.append(json.dumps(
                merged_classifications(r["classifications"], tags, families=families)
            ))
        async with conn.transaction():
            if ids:
                await conn.execute(APPLY_SQL, ids, payloads)
        state["last_id"] = str(rows[-1]["id"])
        state["batches"] += 1
        state["rows_scanned"] += len(rows)
        state["rows_written"] += len(ids)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_cursor(cursor_path, state)
        done_this_run += 1
        if max_batches is not None and done_this_run >= max_batches:
            return state
    state["done"] = True
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

    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    try:
        families = await load_families(store)
        async with store.acquire() as conn:
            if args.apply:
                state = await apply(
                    conn, families, cursor_path=Path(args.cursor_file),
                    batch_size=args.batch_size, restart=args.restart,
                    max_batches=args.max_batches, source_id=args.source_id,
                )
                print(json.dumps(state, indent=2, sort_keys=True))
                return 0
            await conn.execute("SET statement_timeout = '120s'")
            census = await take_census(conn, batch_size=args.batch_size, source_id=args.source_id)
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
    mode.add_argument("--apply", action="store_true", help="write signals.data.classifications")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--top", type=int, default=TOP_N)
    p.add_argument("--json", help="write the full dry-run census to this path")
    p.add_argument("--cursor-file", default=DEFAULT_CURSOR_FILE)
    p.add_argument("--restart", action="store_true", help="ignore an existing cursor")
    p.add_argument("--max-batches", type=int, default=None)
    p.add_argument("--source-id", default=None, help="limit to one source")
    args = p.parse_args(argv)
    if args.batch_size < 1:
        p.error("--batch-size must be >= 1")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
