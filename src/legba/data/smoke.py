# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.smoke — sanity verification per L-001 brief item 7.

After migrations land, this module:
  1. Verifies all expected tables exist with their key columns.
  2. Verifies AGE vocabulary (9 vertex + 14 edge labels) loaded.
  3. Inserts a sample target descriptor row + reads it back.
  4. Inserts a sample stack component row + reads it back.
  5. Inserts a sample analyst_trace row + reads it back via provenance query.
  6. Reports row counts + table sizes per L-091's audit pattern.

Use programmatically (`run_smoke(...)`) or via `python -m legba.data.smoke`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import asyncpg

from .config import DataConfig, PostgresConfig
from .postgres import PostgresStore
from .provenance import (
    compute_receipt_hash,
    is_valid_schema_uri,
    sha256_canonical,
)
from .vocabulary import ENTITY_CLASSES, RELATIONSHIP_TYPES, vertex_label

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Expected substrate shape (kept in sync with migrations 0001–0014)
# ---------------------------------------------------------------------------

EXPECTED_TABLES: tuple[str, ...] = (
    "legba_data_migrations",
    # Core — source-first pivot (migration 0024) DROPPED the legacy `sources`
    # and `predictions` tables; signals are source-owned + modality-first.
    # Migration 0030 dropped the dead 0002 legacy set (events + its link
    # tables, goals/watchlist/watch_triggers, discovered_urls, users).
    "signals", "entity_profiles", "entity_profile_versions",
    "signal_entity_links",
    "situations", "hypotheses",
    "proposed_edges", "facts",
    # PIECE A — reified typed relationship (migration 0033).
    "nexuses",
    # Runtime
    "analyst_traces", "analyst_critiques", "budget_ledger", "graph_metrics",
    # Registry
    "target_descriptors", "analyst_descriptors", "wiring_descriptors",
    "vocabulary_entries", "stack_components", "conversion_webhooks",
    "stack_credentials",
    # DLQ + audit
    "descriptor_dead_letter", "output_dead_letter",
    "descriptor_audit_log", "audit_checkpoints",
    # Filter / enrichment registries (L-152 + later filter-kind tables)
    "source_credibility",
    # Per-poll provenance (DQ-H5b #88, migration 0046; 'success' outcome 0114).
    "source_poll_outcomes",
    # UI panel registrations (L-192).
    "ui_panel_registrations",
    # P5-6 Watchlist v2 (migration 0105) — operator-defined standing watches.
    # RE-LANDS the legacy `watchlist` NAME first-class (same shape as the
    # `nexuses` re-landing), so it moved here out of RETIRED_TABLES.
    "watchlist",
)

# Tables that should NOT exist (retired per L-090 §4.3 + source-first pivot
# migration 0024, which DROPPED the legacy `sources` and `predictions`
# tables, + migration 0030, which dropped the dead 0002 legacy set).
RETIRED_TABLES: tuple[str, ...] = (
    "notifications", "operator_corrections", "modifications",
    "signals_staging", "situation_signals",
    # NB: `nexuses` was retired pre-pivot but is RE-LANDED first-class by PIECE A
    # (migration 0033) — it now lives in EXPECTED_TABLES above.
    "sources", "predictions",
    # 0030 (C-3) — dead legacy event loop + steering tables.
    # NB: `watchlist` was retired with that set but is RE-LANDED first-class
    # by P5-6 Watchlist v2 (migration 0105, operator-defined standing
    # watches) — the same re-landing shape as `nexuses` above, so it left
    # this list.
    "events", "signal_event_links", "event_entity_links", "situation_events",
    "goals", "watch_triggers", "discovered_urls", "users",
)

# Required provenance columns per L-107 §1 on analyst-produced rows.
PROVENANCE_COLUMNS: tuple[str, ...] = (
    "target_id", "target_version", "analyst_id", "analyst_version",
    "produced_at", "derived_from", "schema_uri", "run_id",
)

# Source-first pivot (migration 0024): `signals` are source-owned +
# target-agnostic, so they carry the source-lineage provenance columns
# (source_id/produced_by_*/fetched_at) rather than the analyst/target set.
SIGNAL_PROVENANCE_COLUMNS: tuple[str, ...] = (
    "source_id", "source_version", "produced_by_id", "produced_by_kind",
    "fetched_at", "derived_from", "schema_uri",
)

# Analyst-produced tables that carry the universal `PROVENANCE_COLUMNS` set.
# `signals` is checked separately against `SIGNAL_PROVENANCE_COLUMNS`;
# `predictions` was dropped by migration 0024; `events`/`goals`/`watchlist`
# were dropped by migration 0030 (dead legacy set).
PROVENANCE_TABLES: tuple[str, ...] = (
    "entity_profiles", "situations", "hypotheses",
    "facts", "proposed_edges", "nexuses",
)


@dataclass
class SmokeResult:
    ok: bool
    tables_present: list[str] = field(default_factory=list)
    tables_missing: list[str] = field(default_factory=list)
    tables_unexpected_retired: list[str] = field(default_factory=list)
    provenance_table_failures: dict[str, list[str]] = field(default_factory=dict)
    age_vertex_labels: list[str] = field(default_factory=list)
    age_edge_labels: list[str] = field(default_factory=list)
    vertex_label_diff: dict[str, list[str]] = field(default_factory=dict)
    edge_label_diff: dict[str, list[str]] = field(default_factory=dict)
    sample_target_id: str | None = None
    sample_stack_component_id: str | None = None
    sample_trace_run_id: str | None = None
    lineage_query_ok: bool = False
    table_stats: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_smoke(pg: PostgresConfig | None = None) -> SmokeResult:
    pg = pg or PostgresConfig.from_env()
    result = SmokeResult(ok=True)

    store = PostgresStore(pg)
    await store.connect()
    try:
        # 1. Table presence
        present = set(await store.list_tables())
        for t in EXPECTED_TABLES:
            if t in present:
                result.tables_present.append(t)
            else:
                result.tables_missing.append(t)
                result.ok = False

        for t in RETIRED_TABLES:
            if t in present:
                result.tables_unexpected_retired.append(t)
                result.ok = False

        # 2. Provenance columns on key tables
        for t in PROVENANCE_TABLES:
            if t not in present:
                continue
            cols = await store.table_columns(t)
            col_names = {c["column_name"] for c in cols}
            missing = [c for c in PROVENANCE_COLUMNS if c not in col_names]
            if missing:
                result.provenance_table_failures[t] = missing
                result.ok = False

        # `signals` carries the source-first provenance set (migration 0024).
        if "signals" in present:
            cols = await store.table_columns("signals")
            col_names = {c["column_name"] for c in cols}
            missing = [
                c for c in SIGNAL_PROVENANCE_COLUMNS if c not in col_names
            ]
            if missing:
                result.provenance_table_failures["signals"] = missing
                result.ok = False

        # 3. AGE labels
        try:
            labels = await store.graph_labels()
        except Exception as exc:
            result.errors.append(f"AGE label query failed: {exc}")
            result.ok = False
            labels = {"vertex": [], "edge": []}

        result.age_vertex_labels = sorted(labels.get("vertex", []))
        result.age_edge_labels = sorted(labels.get("edge", []))

        expected_vertices = {vertex_label(ec) for ec in ENTITY_CLASSES}
        expected_edges = set(RELATIONSHIP_TYPES)

        present_v = set(result.age_vertex_labels)
        present_e = set(result.age_edge_labels)
        missing_v = sorted(expected_vertices - present_v)
        missing_e = sorted(expected_edges - present_e)
        unexpected_v = sorted(present_v - expected_vertices - {"_ag_label_vertex", "_ag_label_edge"})
        unexpected_e = sorted(present_e - expected_edges - {"_ag_label_vertex", "_ag_label_edge"})

        if missing_v:
            result.vertex_label_diff["missing"] = missing_v
            result.ok = False
        if missing_e:
            result.edge_label_diff["missing"] = missing_e
            result.ok = False
        if unexpected_v:
            result.vertex_label_diff["unexpected"] = unexpected_v
        if unexpected_e:
            result.edge_label_diff["unexpected"] = unexpected_e

        # 4. Sample target descriptor round-trip
        target_id = f"smoke_test_target_{uuid4().hex[:8]}"
        target_version = sha256_canonical({"smoke": "target", "id": target_id})[:32]
        target_body = {
            "identity": {
                "id": target_id, "name": "Smoke Test Target",
                "schema_uri": "legba/target/2.0.0", "version": target_version,
                "abstraction_level": "L1", "state": "draft",
                "owner": "smoke@local",
                "created": datetime.now(tz=timezone.utc).isoformat(),
            },
            "scope": {
                "geo": ["BR"], "languages": ["en"],
                "entity_classes": ["entity", "country"],
                "time_horizon_days": 90,
            },
            "sources": [], "outputs": [], "coordination": {},
        }
        async with store.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO target_descriptors
                        (descriptor_id, version, schema_uri, is_head,
                         abstraction_level, state, owner, name, body, created_at)
                    VALUES ($1, $2, 'legba/target/2.0.0', TRUE, 'L1', 'draft',
                            'smoke@local', 'Smoke Test Target', $3::jsonb, NOW())
                    """,
                    target_id, target_version, json.dumps(target_body),
                )
                row = await conn.fetchrow(
                    "SELECT body FROM target_descriptors WHERE descriptor_id = $1",
                    target_id,
                )
                body = row["body"] if row else None
                if isinstance(body, str):  # codec-less connection fallback
                    body = json.loads(body)
                if isinstance(body, dict) and body.get("identity", {}).get("id") == target_id:
                    result.sample_target_id = target_id
                else:
                    result.errors.append("target descriptor round-trip failed")
                    result.ok = False
            except Exception as exc:
                result.errors.append(f"target descriptor insert failed: {exc}")
                result.ok = False

        # 5. Sample stack component round-trip
        component_id = f"pg.cluster_smoke_{uuid4().hex[:8]}"
        component_version = sha256_canonical({"smoke": "stack", "id": component_id})[:32]
        component_body = {
            "id": component_id, "name": "Smoke PG Cluster",
            "schema_uri": "legba/stack/postgres/1.0.0",
            "version": component_version, "state": "draft", "owner": "smoke@local",
            "config": {
                "host": {"factory_kind": "text", "raw": "localhost"},
                "database": {"factory_kind": "text", "raw": "legba"},
                "user": {"factory_kind": "text", "raw": "legba"},
                "password": {"factory_kind": "secret", "raw": "creds.smoke.pg"},
            },
        }
        async with store.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO stack_components
                        (component_id, version, schema_uri, kind, is_head,
                         state, owner, name, body, created_at)
                    VALUES ($1, $2, 'legba/stack/postgres/1.0.0', 'postgres', TRUE,
                            'draft', 'smoke@local', 'Smoke PG Cluster', $3::jsonb, NOW())
                    """,
                    component_id, component_version, json.dumps(component_body),
                )
                row = await conn.fetchrow(
                    "SELECT body FROM stack_components WHERE component_id = $1",
                    component_id,
                )
                body = row["body"] if row else None
                if isinstance(body, str):  # codec-less connection fallback
                    body = json.loads(body)
                if isinstance(body, dict) and body.get("id") == component_id:
                    result.sample_stack_component_id = component_id
                else:
                    result.errors.append("stack component round-trip failed")
                    result.ok = False
            except Exception as exc:
                result.errors.append(f"stack component insert failed: {exc}")
                result.ok = False

        # 6. Sample analyst_trace + provenance query
        run_id = uuid4()
        analyst_id = "smoke_analyst"
        analyst_version = sha256_canonical({"smoke": "analyst"})[:32]
        run_started = datetime.now(tz=timezone.utc)
        receipt_hash = compute_receipt_hash(
            run_id=run_id,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            input_row_refs=[],
            prompt_module_hash=None,
            prompt_rendered=None,
            output_row_refs=[],
            output_payload={"smoke": True},
            run_ended_at=run_started,
        )
        async with store.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO analyst_traces
                        (run_id, analyst_id, analyst_version, target_id,
                         cadence_trigger, input_row_refs, intermediate_steps,
                         llm_calls, tool_calls, output_row_refs, output_payload,
                         status, run_started_at, run_ended_at, receipt_hash)
                    VALUES ($1, $2, $3, NULL, 'manual', '{}'::UUID[], '[]'::jsonb,
                            '[]'::jsonb, '[]'::jsonb, '{}'::UUID[], $4::jsonb,
                            'success', $5, $5, $6)
                    """,
                    run_id, analyst_id, analyst_version,
                    json.dumps({"smoke": True}), run_started, receipt_hash,
                )
                row = await conn.fetchrow(
                    """
                    SELECT run_id, analyst_id, status, receipt_hash
                    FROM analyst_traces WHERE analyst_id = $1
                    ORDER BY run_started_at DESC LIMIT 1
                    """,
                    analyst_id,
                )
                if row and str(row["run_id"]) == str(run_id):
                    result.sample_trace_run_id = str(run_id)
                else:
                    result.errors.append("analyst_trace round-trip failed")
                    result.ok = False

                # Provenance round-trip — write a source-first signal row and
                # verify it reads back. Source-first pivot (migration 0024):
                # signals are source-owned + target-agnostic, so the row
                # carries source-lineage provenance (source_id/produced_by_*/
                # fetched_at) rather than the analyst/target columns.
                signal_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO signals
                        (id, source_id, source_version, produced_by_kind,
                         fetched_at, modality, payload, content_hash,
                         derived_from, schema_uri)
                    VALUES ($1, $2, '', 'source', NOW(), 'text', $3::jsonb,
                            $4, '{}'::UUID[], $5)
                    """,
                    signal_id, "smoke_source",
                    json.dumps({"smoke": True}),
                    f"smoke_{signal_id.hex}",
                    "iglu:legba/signal/jsonschema/3-0-0",
                )
                row = await conn.fetchrow(
                    "SELECT id, source_id FROM signals WHERE id = $1",
                    signal_id,
                )
                if row and row["source_id"] == "smoke_source":
                    result.lineage_query_ok = True
                else:
                    result.errors.append("provenance round-trip on signals failed")
                    result.ok = False
            except Exception as exc:
                result.errors.append(f"analyst_trace round-trip failed: {exc}")
                result.ok = False

        # 7. Table sizes / row counts (L-091 audit pattern)
        result.table_stats = await _table_stats(store)

    finally:
        await store.close()

    return result


async def _table_stats(store: PostgresStore) -> list[dict[str, Any]]:
    """Return [{table, size_bytes, size_pretty, row_count}, ...]."""
    async with store.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.relname AS table,
                pg_total_relation_size(c.oid) AS size_bytes,
                pg_size_pretty(pg_total_relation_size(c.oid)) AS size_pretty,
                c.reltuples::bigint AS approx_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC
            """
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # Real row count (autovacuum stats stale on freshly seeded clusters).
            real_count = await conn.fetchval(
                f"SELECT count(*) FROM public.{d['table']}"
            )
            d["row_count"] = int(real_count or 0)
            out.append(d)
        return out


if __name__ == "__main__":  # pragma: no cover — manual invocation
    parser = argparse.ArgumentParser(description="Run legba.data smoke test.")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = asyncio.run(run_smoke())
    if args.json:
        print(result.to_json())
    else:
        print(f"OK: {result.ok}")
        print(f"tables present: {len(result.tables_present)}/{len(EXPECTED_TABLES)}")
        if result.tables_missing:
            print(f"tables missing:  {result.tables_missing}")
        if result.tables_unexpected_retired:
            print(f"retired-but-present: {result.tables_unexpected_retired}")
        if result.provenance_table_failures:
            print(f"provenance failures: {result.provenance_table_failures}")
        print(
            f"AGE: {len(result.age_vertex_labels)} vertex / "
            f"{len(result.age_edge_labels)} edge labels"
        )
        if result.vertex_label_diff:
            print(f"vertex diff: {result.vertex_label_diff}")
        if result.edge_label_diff:
            print(f"edge diff:   {result.edge_label_diff}")
        print(f"sample target id:     {result.sample_target_id}")
        print(f"sample stack id:      {result.sample_stack_component_id}")
        print(f"sample trace run_id:  {result.sample_trace_run_id}")
        print(f"lineage query ok:     {result.lineage_query_ok}")
        if result.errors:
            print("errors:")
            for e in result.errors:
                print(f"  - {e}")
        print("table stats:")
        for r in result.table_stats:
            print(f"  {r['table']:30s}  {r['size_pretty']:>10s}  rows={r['row_count']}")
    raise SystemExit(0 if result.ok else 1)
