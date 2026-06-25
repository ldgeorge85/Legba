#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""seed_import.py — bundle → instance importer (seeding flavor a).

Re-homes a substrate bundle produced by ``scripts/export_substrate.py`` into
THIS instance (clone / migrate / branch). The bundle refers to rows by NATURAL
KEY, so the importer:

  1. resolves/creates every ``entity_profiles`` row on the TARGET by
     ``(lower(canonical_name), entity_class)`` — reusing the seed driver's
     ``_resolve_entity`` (the same ``ON CONFLICT`` upsert ongoing entity
     resolution uses), so no duplicate entities are ever spawned;
  2. re-homes each fact / nexus by writing it through the existing
     ``write_fact`` / ``write_nexus`` paths (so Piece-B/A temporal supersession
     + validation apply), stamped ``source_type`` (the imported row's own
     source_type, defaulting to ``'seed'``) + a single ``seed_batches`` row for
     the whole import;
  3. rebuilds the AGE graph FROM THE RE-HOMED ROWS (not from an exported cypher
     dump): each fact whose endpoints classify to a graph vertex is MERGEd as
     an edge via the existing ``upsert_fact_edge`` helper. The nexus-consuming
     analysts (structural_balance / graph_mining) read the ``nexuses`` TABLE
     directly, so the table re-home already restores the signed graph; the AGE
     rebuild is the optional fact-derived edge layer.

IDEMPOTENCY: a re-import of the same bundle adds NO duplicate OPEN triples —
``write_fact`` / ``write_nexus`` upsert on the open temporal-triple uniqueness,
and ``_resolve_entity`` dedupes entities. (A fresh ``seed_batches`` ledger row
is recorded each run — the batch ledger is an audit of imports.)

Usage:
    python3 scripts/seed_import.py --bundle /tmp/bundle
    python3 scripts/seed_import.py --bundle /tmp/bundle --dry-run
    python3 scripts/seed_import.py --bundle /tmp/bundle --source-type imported
    python3 scripts/seed_import.py --bundle /tmp/bundle --no-graph

Connection: ``PostgresConfig.from_env()``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

import asyncpg

from legba.data.config import PostgresConfig
from legba.data.filters._fact_graph import upsert_fact_edge, vertex_label_for_class
from legba.data.provenance import (
    AnalystContext,
    FactPayload,
    NexusPayload,
    write_fact,
    write_nexus,
)
from legba.data.seed._driver import _resolve_entity

#: Bundle schema this importer understands (MAJOR must match the exporter).
SUPPORTED_BUNDLE_MAJOR = "1"


def _parse_dt(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _import_ctx() -> AnalystContext:
    """Synthetic provenance context for imported rows (mirrors the seed driver)."""
    return AnalystContext(
        analyst_id="seed.import",
        analyst_version="import",
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )


async def _rehome(
    pool: asyncpg.Pool,
    bundle: Path,
    *,
    source_type_override: str | None,
    rebuild_graph: bool,
    manifest: dict[str, Any],
) -> dict[str, int]:
    """Resolve entities → write facts/nexuses → record the batch → rebuild AGE."""
    entities = _read_jsonl(bundle / "entities.jsonl")
    facts = _read_jsonl(bundle / "facts.jsonl")
    nexuses = _read_jsonl(bundle / "nexuses.jsonl")

    counts = {
        "entities": 0,
        "facts": 0,
        "nexuses": 0,
        "graph_edges": 0,
        "skipped": 0,
    }
    actx = _import_ctx()
    # canonical_name.lower() -> entity_class (for the AGE rebuild vertex labels).
    entity_class_by_name: dict[str, str] = {}

    async with pool.acquire() as conn:
        batch_id = await conn.fetchval(
            """
            INSERT INTO seed_batches (source, kind, source_type, manifest)
            VALUES ('seed_import', 'instance_bundle', $1, $2::jsonb)
            RETURNING id
            """,
            source_type_override or "seed",
            json.dumps(
                {
                    "imported_at": datetime.now(tz=timezone.utc).isoformat(),
                    "bundle_manifest": manifest,
                }
            ),
        )

        # 1) Entities — resolve/create on the target by natural key.
        for ent in entities:
            nk = ent.get("natural_key") or {}
            name = (nk.get("canonical_name") or "").strip()
            cls = (nk.get("entity_class") or "entity").strip()
            if not name:
                counts["skipped"] += 1
                continue
            try:
                await _resolve_entity(
                    conn,
                    canonical_name=name,
                    entity_class=cls,
                    geo_lat=ent.get("geo_lat"),
                    geo_lon=ent.get("geo_lon"),
                    geo_country=ent.get("geo_country"),
                    data=ent.get("data") or {},
                )
                entity_class_by_name[name.lower()] = cls
                counts["entities"] += 1
            except Exception as exc:  # degrade-not-drop
                counts["skipped"] += 1
                print(f"  entity {name!r} skipped: {exc}", file=sys.stderr)

        # 2) Facts — re-home each by writing through write_fact.
        for f in facts:
            nk = f.get("natural_key") or {}
            subject = (nk.get("subject") or "").strip()
            predicate = (nk.get("predicate") or "").strip()
            value = (nk.get("value") or "").strip()
            if not (subject and predicate and value):
                counts["skipped"] += 1
                continue
            stype = source_type_override or f.get("source_type") or "seed"
            # Ensure both endpoints resolve (bundle may carry a fact whose
            # endpoints were not all in entities.jsonl, e.g. filtered export).
            for name in (subject, value):
                if name.lower() not in entity_class_by_name:
                    try:
                        await _resolve_entity(conn, canonical_name=name)
                        entity_class_by_name[name.lower()] = "entity"
                    except Exception:
                        pass
            try:
                out, dlq = await write_fact(
                    conn,
                    analyst_ctx=actx,
                    payload=FactPayload(
                        subject=subject,
                        predicate=predicate,
                        value=value,
                        confidence=float(f.get("confidence", 1.0) or 1.0),
                        source_type=stype,
                        valid_from=_parse_dt(f.get("valid_from")),
                        geo_lat=f.get("geo_lat"),
                        geo_lon=f.get("geo_lon"),
                        data=f.get("data") or {},
                    ),
                    derived_from=[],
                    source_type=stype,
                    seed_batch_id=batch_id,
                )
                if dlq is not None or out is None:
                    counts["skipped"] += 1
                    continue
                counts["facts"] += 1
            except Exception as exc:  # degrade-not-drop
                counts["skipped"] += 1
                print(
                    f"  fact ({subject}|{predicate}|{value}) skipped: {exc}",
                    file=sys.stderr,
                )
                continue

            # 3a) AGE rebuild: project the fact-derived edge from the re-homed
            #     row (best-effort; never fails the import).
            if rebuild_graph:
                subj_cls = entity_class_by_name.get(subject.lower())
                val_cls = entity_class_by_name.get(value.lower())
                if (
                    vertex_label_for_class(subj_cls)
                    and vertex_label_for_class(val_cls)
                    and out is not None
                ):
                    try:
                        emitted = await _emit_fact_edge(
                            pool,
                            subject=subject,
                            subject_class=subj_cls,
                            predicate=predicate,
                            value=value,
                            value_class=val_cls,
                            fact_id=str(out.id),
                        )
                        if emitted:
                            counts["graph_edges"] += 1
                    except Exception:
                        pass

        # 4) Nexuses — re-home each by writing through write_nexus.
        for n in nexuses:
            nk = n.get("natural_key") or {}
            subject = (nk.get("subject") or "").strip()
            obj = (nk.get("object") or "").strip()
            rel_type = (nk.get("rel_type") or "").strip()
            intermediary = (nk.get("intermediary") or None)
            if not (subject and obj and rel_type):
                counts["skipped"] += 1
                continue
            stype = source_type_override or n.get("source_type") or "seed"
            for name in (subject, obj, intermediary):
                if name and name.lower() not in entity_class_by_name:
                    try:
                        await _resolve_entity(conn, canonical_name=name)
                        entity_class_by_name[name.lower()] = "entity"
                    except Exception:
                        pass
            try:
                out, dlq = await write_nexus(
                    conn,
                    analyst_ctx=actx,
                    payload=NexusPayload(
                        subject=subject,
                        intermediary=intermediary,
                        object=obj,
                        rel_type=rel_type,
                        label=n.get("label", "") or "",
                        polarity=int(n.get("polarity", 0) or 0),
                        intent=n.get("intent", "") or "",
                        channel=n.get("channel", "direct") or "direct",
                        confidence=float(n.get("confidence", 1.0) or 1.0),
                        valid_from=_parse_dt(n.get("valid_from")),
                        data=n.get("data") or {},
                    ),
                    derived_from=[],
                    source_type=stype,
                    seed_batch_id=batch_id,
                )
                if dlq is not None or out is None:
                    counts["skipped"] += 1
                    continue
                counts["nexuses"] += 1
            except Exception as exc:  # degrade-not-drop
                counts["skipped"] += 1
                print(
                    f"  nexus ({subject}|{rel_type}|{obj}) skipped: {exc}",
                    file=sys.stderr,
                )

        await conn.execute(
            "UPDATE seed_batches SET counts = $2::jsonb WHERE id = $1",
            batch_id,
            json.dumps(counts),
        )
    counts["seed_batch_id"] = str(batch_id)  # type: ignore[assignment]
    return counts


async def _emit_fact_edge(
    pool: asyncpg.Pool,
    *,
    subject: str,
    subject_class: str | None,
    predicate: str,
    value: str,
    value_class: str | None,
    fact_id: str,
) -> bool:
    """Project one fact-derived AGE edge via the existing helper.

    ``upsert_fact_edge`` calls ``store.cypher(query, cols=…, graph_name=…)``;
    we hand it a tiny shim that runs the AGE cypher on a pooled connection that
    has the agtype codec loaded. Best-effort — a graph error never fails the
    import (the fact row has already persisted).
    """

    class _Shim:
        async def cypher(self, query: str, *, cols: str, graph_name: str) -> Any:
            async with pool.acquire() as conn:
                await conn.execute("LOAD 'age'")
                await conn.execute('SET search_path = ag_catalog, "$user", public')
                stmt = f"SELECT * FROM cypher('{graph_name}', $$ {query} $$) AS ({cols})"
                return await conn.fetch(stmt)

    return await upsert_fact_edge(
        _Shim(),
        subject=subject,
        subject_class=subject_class,
        predicate=predicate,
        value=value,
        value_class=value_class,
        fact_id=fact_id,
        graph="legba_graph",
    )


def _validate_manifest(bundle: Path) -> dict[str, Any]:
    mpath = bundle / "manifest.json"
    if not mpath.exists():
        raise SystemExit(f"error: no manifest.json in {bundle}")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    schema = str(manifest.get("schema_version", ""))
    major = schema.rsplit("/", 1)[-1].split("-", 1)[0] if "-" in schema else ""
    if major and major != SUPPORTED_BUNDLE_MAJOR:
        raise SystemExit(
            f"error: bundle schema {schema!r} major {major!r} != supported "
            f"{SUPPORTED_BUNDLE_MAJOR!r}"
        )
    return manifest


async def _run(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle)
    manifest = _validate_manifest(bundle)

    if args.dry_run:
        # Count what WOULD be imported; touch nothing.
        report = {
            "dry_run": True,
            "bundle": str(bundle),
            "would_import": {
                "entities": len(_read_jsonl(bundle / "entities.jsonl")),
                "facts": len(_read_jsonl(bundle / "facts.jsonl")),
                "nexuses": len(_read_jsonl(bundle / "nexuses.jsonl")),
            },
            "manifest": manifest,
        }
        print(json.dumps(report, indent=2, default=str))
        return 0

    cfg = PostgresConfig.from_env()
    pool = await asyncpg.create_pool(cfg.dsn, min_size=1, max_size=4)
    try:
        counts = await _rehome(
            pool,
            bundle,
            source_type_override=args.source_type,
            rebuild_graph=not args.no_graph,
            manifest=manifest,
        )
    finally:
        await pool.close()
    print(json.dumps({"dry_run": False, "imported": counts}, indent=2, default=str))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Legba substrate bundle importer (flavor a).")
    p.add_argument("--bundle", required=True, help="bundle directory to import")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="read + count the bundle; write nothing",
    )
    p.add_argument(
        "--source-type",
        help="override source_type stamped on every imported row "
        "(default: each row's own source_type, else 'seed')",
    )
    p.add_argument(
        "--no-graph",
        action="store_true",
        help="skip the AGE fact-edge rebuild (rows are still re-homed)",
    )
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())
