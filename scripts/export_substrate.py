#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""export_substrate.py — instance → bundle exporter (seeding flavor a).

Exports the KNOWLEDGE LAYER of a running Legba instance — ``entity_profiles``,
``facts``, ``nexuses`` — to a portable JSONL bundle that ``seed_import.py``
re-homes into another (fresh or topped-up) instance. This is the
clone / migrate / branch path; flavor b (``scripts/seed.py``) is the
curated-authoritative path.

WHY a bundle, not ``pg_dump``: the bundle refers to rows by NATURAL KEY — an
entity by ``(lower(canonical_name), entity_class)``; a fact by its open
temporal triple ``(subject, predicate, value, valid_from)``; a nexus by
``(subject, intermediary, object, rel_type, valid_from)`` — NOT by instance
row id. So the importer resolves/creates entities on the *target*, regenerates
ids, and re-links via natural keys — the bundle re-homes cleanly across
instances with different id spaces. The AGE graph is NOT exported (no cypher
dump); the importer rebuilds it from the re-homed rows.

By default only the OPEN ("what holds now") rows are exported
(``valid_until IS NULL AND superseded_by IS NULL``) — a fresh instance wants
the current world, not the full supersession history. ``--include-closed``
exports every row.

Filters (all optional, AND-combined):
  --source-type seed,ingestion,agent,backfill   # facts/nexuses source_type
  --since YYYY-MM-DD                             # valid_from >= date
  --target  <target_id>                          # provenance target_id

Usage:
    python3 scripts/export_substrate.py --out /tmp/bundle
    python3 scripts/export_substrate.py --out /tmp/bundle --source-type seed
    python3 scripts/export_substrate.py --out /tmp/bundle --include-closed

Output (``--out DIR``): ``entities.jsonl`` / ``facts.jsonl`` / ``nexuses.jsonl``
+ ``manifest.json`` (schema version, source instance, time, counts, filters).
Connection: ``PostgresConfig.from_env()`` (the runtime's LEGBA_* env).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

import asyncpg

from legba.data.config import PostgresConfig

#: Bundle schema version — bumped if the JSONL row shape changes. The importer
#: refuses an unknown MAJOR.
BUNDLE_SCHEMA_VERSION = "legba/substrate-bundle/1-0-0"

_VALID_SOURCE_TYPES = {"seed", "ingestion", "agent", "backfill"}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "hex") and hasattr(value, "int"):  # UUID
        return str(value)
    return str(value)


def _dump_line(row: dict[str, Any]) -> str:
    return json.dumps(row, default=_json_default, ensure_ascii=False, sort_keys=True)


def _as_obj(value: Any) -> dict[str, Any]:
    """Normalise a jsonb column to a dict.

    We read via a plain ``asyncpg.connect`` (no per-connection jsonb codec —
    that lives on ``PostgresStore``'s pool), so a ``jsonb`` column comes back
    as a JSON *string*. Decode it to a dict so the bundle carries a real nested
    object (and the importer's ``FactPayload``/``NexusPayload`` — which expect a
    dict — accept it). A dict (codec present) or NULL passes through to ``{}``.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {"_value": decoded}
        except (ValueError, TypeError):
            return {}
    return {}


def _open_clause(include_closed: bool, alias: str = "") -> str:
    """SQL predicate selecting the OPEN rows unless ``include_closed``."""
    if include_closed:
        return "TRUE"
    a = f"{alias}." if alias else ""
    return f"{a}valid_until IS NULL AND {a}superseded_by IS NULL"


def _filters(
    *,
    source_types: list[str] | None,
    since: datetime | None,
    target: str | None,
    next_param: int,
) -> tuple[str, list[Any]]:
    """Build the shared (source_type / since / target) WHERE fragment."""
    clauses: list[str] = []
    params: list[Any] = []
    n = next_param
    if source_types:
        clauses.append(f"source_type = ANY(${n})")
        params.append(source_types)
        n += 1
    if since is not None:
        clauses.append(f"valid_from >= ${n}")
        params.append(since)
        n += 1
    if target:
        clauses.append(f"target_id = ${n}")
        params.append(target)
        n += 1
    frag = (" AND " + " AND ".join(clauses)) if clauses else ""
    return frag, params


async def _export_entities(
    conn: asyncpg.Connection, out: Path, *, target: str | None
) -> int:
    """Export entity_profiles, keyed by (canonical_name, entity_class).

    Entities carry no temporal lifecycle, so ``include_closed`` does not apply.
    The ``--target`` filter is honored when set (entity provenance target_id).
    """
    out.mkdir(parents=True, exist_ok=True)
    where = "WHERE TRUE"
    params: list[Any] = []
    if target:
        where += " AND target_id = $1"
        params.append(target)
    rows = await conn.fetch(
        f"""
        SELECT canonical_name, entity_type, entity_class, data,
               geo_lat, geo_lon, geo_country, geo_region, completeness_score
        FROM entity_profiles
        {where}
        ORDER BY lower(canonical_name), entity_class
        """,
        *params,
    )
    path = out / "entities.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                _dump_line(
                    {
                        "natural_key": {
                            "canonical_name": r["canonical_name"],
                            "entity_class": r["entity_class"],
                        },
                        "entity_type": r["entity_type"],
                        "data": _as_obj(r["data"]),
                        "geo_lat": r["geo_lat"],
                        "geo_lon": r["geo_lon"],
                        "geo_country": r["geo_country"],
                        "geo_region": r["geo_region"],
                        "completeness_score": r["completeness_score"],
                    }
                )
                + "\n"
            )
    return len(rows)


async def _export_facts(
    conn: asyncpg.Connection,
    out: Path,
    *,
    include_closed: bool,
    source_types: list[str] | None,
    since: datetime | None,
    target: str | None,
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    frag, params = _filters(
        source_types=source_types, since=since, target=target, next_param=1
    )
    rows = await conn.fetch(
        f"""
        SELECT subject, predicate, value, confidence, source_type,
               valid_from, valid_until, geo_lat, geo_lon, data
        FROM facts
        WHERE {_open_clause(include_closed)}{frag}
        ORDER BY subject, predicate, value, valid_from
        """,
        *params,
    )
    path = out / "facts.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                _dump_line(
                    {
                        "natural_key": {
                            "subject": r["subject"],
                            "predicate": r["predicate"],
                            "value": r["value"],
                            "valid_from": r["valid_from"],
                        },
                        "confidence": r["confidence"],
                        "source_type": r["source_type"],
                        "valid_from": r["valid_from"],
                        "valid_until": r["valid_until"],
                        "geo_lat": r["geo_lat"],
                        "geo_lon": r["geo_lon"],
                        "data": _as_obj(r["data"]),
                    }
                )
                + "\n"
            )
    return len(rows)


async def _export_nexuses(
    conn: asyncpg.Connection,
    out: Path,
    *,
    include_closed: bool,
    source_types: list[str] | None,
    since: datetime | None,
    target: str | None,
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    frag, params = _filters(
        source_types=source_types, since=since, target=target, next_param=1
    )
    rows = await conn.fetch(
        f"""
        SELECT subject, intermediary, object, rel_type, label, polarity,
               intent, channel, confidence, source_type,
               valid_from, valid_until, data
        FROM nexuses
        WHERE {_open_clause(include_closed)}{frag}
        ORDER BY subject, rel_type, object, valid_from
        """,
        *params,
    )
    path = out / "nexuses.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                _dump_line(
                    {
                        "natural_key": {
                            "subject": r["subject"],
                            "intermediary": r["intermediary"],
                            "object": r["object"],
                            "rel_type": r["rel_type"],
                            "valid_from": r["valid_from"],
                        },
                        "label": r["label"],
                        "polarity": r["polarity"],
                        "intent": r["intent"],
                        "channel": r["channel"],
                        "confidence": r["confidence"],
                        "source_type": r["source_type"],
                        "valid_from": r["valid_from"],
                        "valid_until": r["valid_until"],
                        "data": _as_obj(r["data"]),
                    }
                )
                + "\n"
            )
    return len(rows)


async def _run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    source_types = None
    if args.source_type:
        source_types = [s.strip() for s in args.source_type.split(",") if s.strip()]
        bad = [s for s in source_types if s not in _VALID_SOURCE_TYPES]
        if bad:
            print(
                f"error: unknown --source-type {bad!r}; "
                f"valid: {sorted(_VALID_SOURCE_TYPES)}",
                file=sys.stderr,
            )
            return 2

    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    cfg = PostgresConfig.from_env()
    conn = await asyncpg.connect(cfg.dsn)
    try:
        n_entities = await _export_entities(conn, out, target=args.target)
        n_facts = await _export_facts(
            conn,
            out,
            include_closed=args.include_closed,
            source_types=source_types,
            since=since,
            target=args.target,
        )
        n_nexuses = await _export_nexuses(
            conn,
            out,
            include_closed=args.include_closed,
            source_types=source_types,
            since=since,
            target=args.target,
        )
    finally:
        await conn.close()

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source_instance": socket.gethostname(),
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "counts": {
            "entities": n_entities,
            "facts": n_facts,
            "nexuses": n_nexuses,
        },
        "filters": {
            "include_closed": args.include_closed,
            "source_type": source_types,
            "since": args.since,
            "target": args.target,
        },
        "files": ["entities.jsonl", "facts.jsonl", "nexuses.jsonl"],
        "ref_strategy": "natural_key",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Legba substrate bundle exporter (flavor a).")
    p.add_argument("--out", required=True, help="output bundle directory")
    p.add_argument(
        "--include-closed",
        action="store_true",
        help="export closed/superseded rows too (default: open rows only)",
    )
    p.add_argument(
        "--source-type",
        help="comma-separated facts/nexuses source_type filter (seed,ingestion,agent,backfill)",
    )
    p.add_argument("--since", help="valid_from >= YYYY-MM-DD")
    p.add_argument("--target", help="provenance target_id filter")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())
