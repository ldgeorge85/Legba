#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Supply-chain pack — READ-ONLY lane preflight (plan §6.1 step 1).

Measures, per Tier-A desk, exactly what ``_read_substrate_slice``
(``src/legba/runtime/actor_substrate_slice.py:164-445``) would face on this
desk's next fire, and reports it against the 360-row SQL pre-filter that runs
BEFORE the desk's ``scope.predicate`` does. This is the gate on activating the
six Tier-A desks: the plan's single most important design rule
(planning/SUPPLY_CHAIN_PACK_PLAN_2026-07-29.md §0.2) is that every desk must
carry a SQL-pushdown discriminator sized so that (window AND pushdown) lands
under the cap — the free-text predicate is a REFINER, never the selector.

METHODOLOGY (mirrors the slice reader clause-for-clause, SELECT only):

    window       fetched_at > NOW() - INTERVAL '<time_window> hours'
    backfill     (payload->>'event_class') IS DISTINCT FROM 'backfill'
    canonical    (canonical_signal_id IS NULL OR canonical_signal_id = id)
    pushdown     [source_id = ANY($n)]   when the target pins explicit ids
                 [geo && $n::text[]]     when scope.geo is non-empty
    fetch_limit  max(200, LEGBA_SLICE_ROW_CAP * 3)          == 360 by default
    residual     filter_rows_by_residual(scope.predicate, rows)  (POST-query)
    row_cap      LEGBA_SLICE_ROW_CAP                        == 120 by default

Per desk it reports:

  * ``lane_rows``      — rows the pushdown alone admits in the window. THE GATE:
    if this exceeds ``fetch_limit`` the newest-N cut silently drops evidence.
  * ``hits_full``      — predicate hits across the WHOLE window (no LIMIT).
  * ``hits_capped``    — predicate hits inside the newest ``fetch_limit`` rows,
    i.e. what the desk would actually see.
  * ``lost``           — hits_full - hits_capped: the evidence the pre-filter
    would silently drop. MUST be 0 for an activatable desk.
  * ``slice_rows``     — min(hits_capped, row_cap): the rows the unit reads.
  * ``top_source``     — the highest-contributing source_id and its share of
    ``lane_rows``. Flagged over ~30% (plan §0.3: the per-source diversity cap
    `_diversify_by_source` sits on the ``elif`` branch and does NOT run when a
    ``scope.predicate`` is present, so a predicate-bearing desk has ZERO
    firehose protection).

Also asserts the plan's hard geo rule: NO desk may carry ``US`` in scope.geo.

CAVEAT, stated because it is real: ``filter_rows_by_residual`` fails CLOSED per
row — a row whose residual breaches the engine's 5 ms wall-clock budget is
DROPPED and logged ("slice residual eval failed (row dropped)"). A handful of
very long signal bodies hit that budget on every run. Both ``hits_full`` and
``hits_capped`` are measured through the same evaluator, so the ``lost`` delta is
unaffected, but the absolute hit counts are a floor, not an exact count. The
production slice reader behaves identically, so the numbers describe what a desk
would really see.

Reads the desk definitions straight out of the descriptor YAML files, so the
numbers always describe the descriptors as committed, never a stale copy.

READ-ONLY: every statement is a SELECT. Nothing is registered, written, or
activated. Tier-B desks are measured too, as information — their activation
gates are collection-volume gates written into their own descriptor headers, not
this cap.

Run via the test image against the live DB:
  docker run --rm --network host --entrypoint python3 -v $PWD:/app -w /app \\
    -e PYTHONPATH=/app/src:/install/lib/python3.11/site-packages \\
    -e LEGBA_DATA_PG_HOST=127.0.0.1 -e LEGBA_DATA_PG_USER=legba \\
    -e LEGBA_DATA_PG_PASSWORD=legba -e LEGBA_DATA_PG_DB=legba \\
    legba/legba-test:latest scripts/preflight_supply_chain_lanes.py

Options:
  --window-hours N   override the measured window (default 24 — the
                     disruption_status unit's declared time_window)
  --all              include the 4 Tier-B desks in the report
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from typing import Any

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from legba.data.nats import SIGNALS_EXCLUDE_BACKFILL_SQL  # noqa: E402
from legba.data.postgres import PostgresStore  # noqa: E402
from legba.data.schemas.target import TargetDescriptor  # noqa: E402
from legba.runtime.actor_substrate_slice import _slice_row_cap  # noqa: E402
from legba.runtime.subscription.filter import filter_rows_by_residual  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

# The unit's declared window (descriptors/analyst_disruption_status.yaml:
# subscription.targets.time_window: 24h). Overridable with --window-hours to
# re-prove why 72h does NOT fit.
DEFAULT_WINDOW_HOURS = 24

# Tier A = the 6 desks the plan activates at launch; Tier B = the 4 declared-but-
# draft desks whose activation waits on a source slot (§1.2).
TIER_A_FILES = [
    "target_lane_hormuz.yaml",
    "target_lane_red_sea.yaml",
    "target_lane_malacca_south_china_sea.yaml",
    "target_lane_black_sea.yaml",
    "target_flow_semiconductors.yaml",
    "target_flow_energy_shipping.yaml",
]
TIER_B_FILES = [
    "target_lane_panama.yaml",
    "target_flow_critical_minerals.yaml",
    "target_flow_container_freight.yaml",
    "target_lane_baltic_north_sea.yaml",
]

# Single-source concentration the plan flags as unhealthy for a predicate-bearing
# desk (no diversity cap runs on that branch).
CONCENTRATION_FLAG = 0.30

# The columns the residual ctx reads — the slice reader's SELECT list, so the
# predicate is never fed nulls (actor_substrate_slice.py:307-311).
_SLICE_COLUMNS = (
    "id, source_id, source_version, canonical_url, payload, language, geo, "
    "tags, fetched_at, derived_from, entity_classes, source_credibility, "
    "modality, salience"
)


def _fetch_limit(row_cap: int) -> int:
    """The slice reader's over-fetch: max(200, row_cap * 3) on the predicate /
    diversity paths (actor_substrate_slice.py:305)."""
    return max(200, row_cap * 3)


def _load(name: str) -> TargetDescriptor:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return TargetDescriptor.model_validate(body, strict=False)


def _pushdown(desc: TargetDescriptor) -> tuple[list[str], list[str]]:
    """(explicit source_ids, scope.geo) — exactly what the slice reader reads off
    the target body (actor_substrate_slice.py:240-256)."""
    source_ids = [s.source_id for s in desc.sources if s.source_id]
    geo = [g for g in (getattr(desc.scope, "geo", None) or []) if g]
    return source_ids, geo


def _where(source_ids: list[str], geo: list[str], window_hours: int) -> tuple[str, list[Any]]:
    clauses = [
        f"fetched_at > NOW() - INTERVAL '{window_hours} hours'",
        SIGNALS_EXCLUDE_BACKFILL_SQL,
        "(canonical_signal_id IS NULL OR canonical_signal_id = id)",
    ]
    params: list[Any] = []
    if source_ids:
        params.append(source_ids)
        clauses.append(f"source_id = ANY(${len(params)})")
    if geo:
        params.append(geo)
        clauses.append(f"geo && ${len(params)}::text[]")
    return "WHERE " + " AND ".join(clauses), params


async def _measure(
    conn, desc: TargetDescriptor, *, window_hours: int, row_cap: int
) -> dict[str, Any]:
    source_ids, geo = _pushdown(desc)
    predicate = desc.scope.predicate or ""
    where, params = _where(source_ids, geo, window_hours)
    limit = _fetch_limit(row_cap)

    lane_rows = await conn.fetchval(f"SELECT count(*) FROM signals {where}", *params)

    # Source concentration over the pushdown rows (before the predicate runs).
    top_source, top_count = None, 0
    conc_rows = await conn.fetch(
        f"SELECT source_id, count(*) AS n FROM signals {where} "
        f"GROUP BY source_id ORDER BY n DESC LIMIT 1",
        *params,
    )
    if conc_rows:
        top_source = conc_rows[0]["source_id"]
        top_count = int(conc_rows[0]["n"])

    # hits_full: the predicate over the WHOLE window (no LIMIT) — the evidence
    # that EXISTS. hits_capped: the predicate over the newest fetch_limit rows —
    # the evidence the desk would actually SEE.
    full = await conn.fetch(
        f"SELECT {_SLICE_COLUMNS} FROM signals {where} ORDER BY fetched_at DESC",
        *params,
    )
    capped = full[:limit]
    hits_full = filter_rows_by_residual(predicate, [dict(r) for r in full]) if predicate else full
    hits_capped = (
        filter_rows_by_residual(predicate, [dict(r) for r in capped]) if predicate else capped
    )

    return {
        "desk_id": desc.identity.id,
        "state": desc.identity.state.value,
        "geo": geo,
        "source_pin": source_ids,
        "lane_rows": int(lane_rows or 0),
        "fetch_limit": limit,
        "over_cap": int(lane_rows or 0) > limit,
        "hits_full": len(hits_full),
        "hits_capped": len(hits_capped),
        "lost": len(hits_full) - len(hits_capped),
        "slice_rows": min(len(hits_capped), row_cap),
        "top_source": top_source,
        "top_share": (top_count / lane_rows) if lane_rows else 0.0,
        "has_us": "US" in geo,
    }


def _print(title: str, rows: list[dict[str, Any]], *, row_cap: int) -> int:
    """Print one tier's table. Returns the number of FLAGGED desks."""
    print(f"\n{title}")
    print(
        f"  {'desk':30s} {'push':>10s} {'rows':>6s} {'cap':>5s} "
        f"{'hits':>5s} {'seen':>5s} {'lost':>5s} {'slice':>6s}  top-source (share)"
    )
    flagged = 0
    for r in rows:
        push = f"geo{len(r['geo'])}" if r["geo"] else (
            f"src{len(r['source_pin'])}" if r["source_pin"] else "NONE"
        )
        mark = " "
        notes: list[str] = []
        if r["has_us"]:
            notes.append("US IN GEO (plan §0.3 hard NO)")
        if r["over_cap"]:
            notes.append(f"OVER THE {r['fetch_limit']}-ROW CAP")
        if r["lost"]:
            notes.append(f"{r['lost']} predicate hits DROPPED by the pre-filter")
        if r["top_share"] > CONCENTRATION_FLAG:
            notes.append(f"single-source concentration {r['top_share']:.0%}")
        if not r["geo"] and not r["source_pin"]:
            notes.append("NO PUSHDOWN — reads the tenant-wide pool")
        if notes:
            mark = "!"
            flagged += 1
        share = f"{r['top_share']:.0%}" if r["lane_rows"] else "-"
        print(
            f"{mark} {r['desk_id']:30s} {push:>10s} {r['lane_rows']:>6d} "
            f"{r['fetch_limit']:>5d} {r['hits_full']:>5d} {r['hits_capped']:>5d} "
            f"{r['lost']:>5d} {r['slice_rows']:>6d}  {r['top_source'] or '-'} ({share})"
        )
        for n in notes:
            print(f"      -> {n}")
    return flagged


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supply-chain lane preflight (read-only).")
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--all", action="store_true", help="also measure the 4 Tier-B desks")
    args = parser.parse_args(argv)

    row_cap = _slice_row_cap()
    limit = _fetch_limit(row_cap)
    print(
        f"Supply-chain lane preflight — window {args.window_hours}h, "
        f"LEGBA_SLICE_ROW_CAP={row_cap}, fetch_limit={limit} "
        f"(read-only; SELECT statements only)"
    )
    print(
        "  rows = pushdown-only count in the window (THE GATE vs cap); "
        "hits = predicate hits over the full window; seen = hits inside the "
        "newest `cap` rows; lost = hits the pre-filter drops; slice = rows the unit reads"
    )

    pg = PostgresStore.from_env()
    await pg.connect()
    try:
        async with pg.acquire() as conn:
            tier_a = [
                await _measure(conn, _load(f), window_hours=args.window_hours, row_cap=row_cap)
                for f in TIER_A_FILES
            ]
            tier_b = (
                [
                    await _measure(conn, _load(f), window_hours=args.window_hours, row_cap=row_cap)
                    for f in TIER_B_FILES
                ]
                if args.all
                else []
            )
            total = await conn.fetchval(
                f"SELECT count(*) FROM signals "
                f"WHERE fetched_at > NOW() - INTERVAL '{args.window_hours} hours' "
                f"AND {SIGNALS_EXCLUDE_BACKFILL_SQL} "
                f"AND (canonical_signal_id IS NULL OR canonical_signal_id = id)"
            )
    finally:
        await pg.close()

    flagged = _print("TIER A — the activation gate (every desk must be clean):", tier_a,
                     row_cap=row_cap)
    if tier_b:
        _print("TIER B — informational (activation is gated on a source slot, not this cap):",
               tier_b, row_cap=row_cap)
    print(
        f"\nAll canonical signals in the window: {total} "
        f"(a NO-pushdown thematic desk would sample {limit}/{total} = "
        f"{(limit / total) if total else 0:.0%} of it, recency-biased)"
    )
    if flagged:
        print(
            f"\nFAIL: {flagged} Tier-A desk(s) flagged. Trim the geo set (or the "
            f"window) and re-run BEFORE activating — a mis-sized desk's findings "
            f"rest on a silently truncated slice (plan §6.5 kill criterion 4)."
        )
        return 1
    print("\nPASS: every Tier-A desk is inside the pre-filter cap with no dropped hits.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
