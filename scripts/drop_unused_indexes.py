#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drop (or re-create) the unused indexes of migration 0264 out of
transaction — DRY RUN by default.

The out-of-transaction half of 0264 (the create_read_latency_indexes
pattern, reversed). 0264 drops each index only on a small table; on the live
large tables (signals, entity_profiles, analyst_traces, entity_edges,
governor_events) the drop is this script's job, because the runner holds
every migration inside one transaction and DROP INDEX CONCURRENTLY cannot run
there.

Modes:

  (no flags)    DRY RUN. Each index: present (with its scan count) / absent.
  --drop        Re-checks idx_scan on the day: an index that HAS been scanned
                since the report is SKIPPED and named (exit 1). Otherwise DROP
                INDEX CONCURRENTLY IF EXISTS, one at a time, autocommit.
  --recreate    THE REVERSE: CREATE INDEX CONCURRENTLY IF NOT EXISTS from the
                original definition below (pg_get_indexdef on live,
                2026-11-14). An INVALID leftover is dropped and rebuilt.
                Pass --only NAME[,NAME] to restore some.

Usage (inside the runtime container, DB env from the container)::

      python3 scripts/drop_unused_indexes.py             # dry run
      python3 scripts/drop_unused_indexes.py --drop      # a few seconds
      python3 scripts/drop_unused_indexes.py --recreate  # minutes (the GINs)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any

#: name -> (table, definition after the index name), the live pg_get_indexdef
#: of 2026-11-14. Pinned to 0264's DROP list by
#: tests/data_pkg/test_schema_hygiene.py.
INDEXES: dict[str, tuple[str, str]] = {
    "action_pack_descriptors_schema_idx": ("action_pack_descriptors", "ON public.action_pack_descriptors USING btree (schema_uri)"),
    "idx_asd_target_emitted": ("alert_sink_deliveries", "ON public.alert_sink_deliveries USING btree (target_id, attempted_at DESC)"),
    "analyst_traces_input_gin": ("analyst_traces", "ON public.analyst_traces USING gin (input_row_refs)"),
    "budget_ledger_cost_estimate_idx": ("budget_ledger", "ON public.budget_ledger USING btree (bucket DESC, cost_estimate_usd DESC) WHERE (cost_estimate_usd > (0)::numeric)"),
    "collection_descriptors_schema_idx": ("collection_descriptors", "ON public.collection_descriptors USING btree (schema_uri)"),
    "idx_collection_requirements_open": ("collection_requirements", "ON public.collection_requirements USING btree (priority_rank, created_at DESC) WHERE (status = ANY (ARRAY['proposed'::text, 'reviewed'::text]))"),
    "idx_correctness_labels_unit": ("correctness_labels", "ON public.correctness_labels USING btree (unit_analyst_id)"),
    "desk_baselines_deviation_idx": ("desk_baselines", "ON public.desk_baselines USING btree (deviation, abs(deviation_sigma) DESC) WHERE (deviation <> 'within'::text)"),
    "discovery_state_descriptor_idx": ("discovery_state", "ON public.discovery_state USING btree (descriptor_id)"),
    "discovery_state_discovery_idx": ("discovery_state", "ON public.discovery_state USING btree (discovery_id)"),
    "idx_entity_edges_decay": ("entity_edges", "ON public.entity_edges USING btree (last_seen_at) WHERE ((valid_until IS NULL) AND (superseded_by IS NULL))"),
    "idx_entity_edges_evidence": ("entity_edges", "ON public.entity_edges USING gin (source_signal_ids)"),
    "idx_entity_edges_unresolved_reason": ("entity_edges_unresolved", "ON public.entity_edges_unresolved USING btree (reason, created_at DESC)"),
    "idx_entity_profiles_analyst_id": ("entity_profiles", "ON public.entity_profiles USING btree (analyst_id) WHERE (analyst_id IS NOT NULL)"),
    "idx_entity_profiles_block_key": ("entity_profiles", "ON public.entity_profiles USING btree (entity_block_key(canonical_name)) WHERE (COALESCE((data ->> 'gc_status'::text), ''::text) <> ALL (ARRAY['merged'::text, 'junk'::text]))"),
    "idx_entity_profiles_derived_from": ("entity_profiles", "ON public.entity_profiles USING gin (derived_from)"),
    "idx_entity_profiles_name_trgm": ("entity_profiles", "ON public.entity_profiles USING gin (lower(canonical_name) gin_trgm_ops) WHERE (COALESCE((data ->> 'gc_status'::text), ''::text) <> ALL (ARRAY['merged'::text, 'junk'::text]))"),
    "idx_entity_profiles_target_id": ("entity_profiles", "ON public.entity_profiles USING btree (target_id) WHERE (target_id IS NOT NULL)"),
    "idx_events_asof": ("events", "ON public.events USING btree (valid_from, valid_until)"),
    "governor_events_account_idx": ("governor_events", "ON public.governor_events USING btree (budget_account, occurred_at DESC)"),
    "idx_hypotheses_analyst_id": ("hypotheses", "ON public.hypotheses USING btree (analyst_id) WHERE (analyst_id IS NOT NULL)"),
    "narrative_echo_edges_systematic_idx": ("narrative_echo_edges", "ON public.narrative_echo_edges USING btree (echo_ratio DESC, co_carried DESC) WHERE systematic"),
    "narratives_lead_source_idx": ("narratives", "ON public.narratives USING btree (lead_source_id) WHERE (lead_source_id IS NOT NULL)"),
    "idx_retention_policies_enabled": ("retention_policies", "ON public.retention_policies USING btree (enabled) WHERE enabled"),
    "idx_review_flags_open": ("review_flags", "ON public.review_flags USING btree (created_at DESC) WHERE (closed_at IS NULL)"),
    "idx_seed_batches_source": ("seed_batches", "ON public.seed_batches USING btree (source, imported_at DESC)"),
    "signals_entity_classes_gin": ("signals", "ON public.signals USING gin (entity_classes)"),
    "idx_situations_analyst_id": ("situations", "ON public.situations USING btree (analyst_id) WHERE (analyst_id IS NOT NULL)"),
    "idx_source_credibility_last_updated": ("source_credibility", "ON public.source_credibility USING btree (last_updated DESC)"),
    "idx_source_credibility_score": ("source_credibility", "ON public.source_credibility USING btree (score)"),
    "idx_source_layers_source": ("source_layers", "ON public.source_layers USING btree (source_id)"),
    "stack_credentials_created_idx": ("stack_credentials", "ON public.stack_credentials USING btree (created_at DESC)"),
}


async def index_state(conn: Any) -> dict[str, tuple[str, int | None]]:
    """``{name: ("valid"|"invalid"|"absent", idx_scan|None)}``."""
    rows = await conn.fetch(
        "SELECT c.relname, i.indisvalid, s.idx_scan FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
        "LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid "
        "WHERE c.relname = ANY($1::text[])",
        list(INDEXES))
    found = {
        r["relname"]: ("valid" if r["indisvalid"] else "invalid",
                       int(r["idx_scan"]) if r["idx_scan"] is not None else None)
        for r in rows
    }
    return {name: found.get(name, ("absent", None)) for name in INDEXES}


async def drop_indexes(conn: Any, names: list[str], *, log=print) -> list[str]:
    """DROP CONCURRENTLY each index still unscanned; return the skipped
    names (scanned since the report). ``conn`` must NOT be in a transaction."""
    await conn.execute("SET lock_timeout = '10s'")
    state = await index_state(conn)
    skipped: list[str] = []
    for name in names:
        valid, scans = state[name]
        if valid == "absent":
            log(f"{name}: already absent")
            continue
        if scans:
            log(f"{name}: SKIPPED — scanned {scans} times since the report; re-judge it")
            skipped.append(name)
            continue
        t0 = time.monotonic()
        await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
        log(f"{name}: dropped in {time.monotonic() - t0:.1f} s")
    return skipped


async def recreate_indexes(conn: Any, names: list[str], *, log=print) -> dict[str, str]:
    """The reverse: CREATE INDEX CONCURRENTLY IF NOT EXISTS from the saved
    definition. ``conn`` must NOT be in a transaction."""
    await conn.execute("SET lock_timeout = '10s'")
    for name in names:
        valid, _ = (await index_state(conn))[name]
        if valid == "invalid":
            log(f"{name}: INVALID leftover — dropping CONCURRENTLY first")
            await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
            valid = "absent"
        if valid == "absent":
            t0 = time.monotonic()
            await conn.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {INDEXES[name][1]}")
            log(f"{name}: re-created in {time.monotonic() - t0:.1f} s")
        else:
            log(f"{name}: already present and valid")
    return {n: s for n, (s, _) in (await index_state(conn)).items() if n in names}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--drop", action="store_true")
    mode.add_argument("--recreate", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated index names")
    args = ap.parse_args()
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(INDEXES)
    unknown = [n for n in names if n not in INDEXES]
    if unknown:
        print(f"not in 0264's list: {unknown}", file=sys.stderr)
        return 2
    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        if args.drop:
            skipped = await drop_indexes(conn, names)
            return 1 if skipped else 0
        if args.recreate:
            states = await recreate_indexes(conn, names)
            print(f"indexes: {states}")
            return 0 if all(s == "valid" for s in states.values()) else 1
        for name, (valid, scans) in (await index_state(conn)).items():
            if name in names:
                print(f"{name} on {INDEXES[name][0]}: {valid}"
                      + (f", {scans} scans" if scans is not None else ""))
        print("dry run (pass --drop, or --recreate for the reverse)")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
