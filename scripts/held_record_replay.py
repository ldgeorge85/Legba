#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay the desk shadow records through the held-family writer (lane DM-C).

DRY RUN BY DEFAULT: reads the shadow records and plans each through the
writer's rules (``legba.data.held_record``), simulating the matching rule
across records in order, and reports — assertions by class and status,
versions, restatement links, rows not held and why, rows not written and why,
matching-rule firings, support links with and without bytes. Nothing is
written anywhere.

Sources (one):
  --dsn DSN            read the ledger (``desk_rows_shadow``) over a READ-ONLY
                       session, and the writer's lookups (receipt pins, the
                       proposing seat, the prior read, surface subjects) from
                       the same database, SELECTs only.
  --from-jsonl FILE    shadow rows exported as JSON lines (id, run_id,
                       analyst_id, target_id, occurred_at, contract_version,
                       gate_version, record); ``--lookups-json FILE`` adds the
                       lookups as exported rows (pin_rows, outputs,
                       alias_keys, run_handed).

  --analyst ID         (repeatable) default: energy_security
  --since ISO          only records at or after this time

APPLY (never against the live database): ``--apply --target-dsn DSN`` writes
the records into that database as a REPLAY batch (``origin = 'replay'``, batch
``--batch``, removable with ``legba.held_record_rollback``) with no lookups
(pins, proposer and prior reads are counted as unresolved: a fresh database
has none of them). ``--fresh-db`` creates a new database on the target server,
applies every migration, writes, reports what the tables hold (and the chain
and cache verification), and drops it. A target database named ``legba`` is
refused.

    python3 scripts/held_record_replay.py --from-jsonl rows.jsonl --lookups-json lk.json
    python3 scripts/held_record_replay.py --from-jsonl rows.jsonl --apply \\
        --target-dsn postgresql://legba:legba@127.0.0.1:5432/postgres --fresh-db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.held_record import HELD, NOT_HELD, plan_shadow_record, type_map  # noqa: E402
from legba.data.held_record_feed import row_as_shadow  # noqa: E402

_SOURCE_SQL = """
SELECT id, run_id, analyst_id, target_id, occurred_at, contract_version, gate_version, record
  FROM desk_rows_shadow
 WHERE analyst_id = ANY($1::text[]) AND status = 'written'
   AND ($2::timestamptz IS NULL OR occurred_at >= $2)
 ORDER BY occurred_at, id
"""


# ---------------------------------------------------------------------------
# Lookups from an export (the dry run's file mode)
# ---------------------------------------------------------------------------


class FileLookups:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self.pin_rows = list(data.get("pin_rows") or [])
        self.outputs = {(str(o["run_id"]), str(o["analyst_id"])): o.get("analyst_version")
                        for o in data.get("outputs") or []}
        self.alias = list(data.get("alias_keys") or [])
        self.handed = list(data.get("run_handed") or [])

    async def pins(self, conn: Any, signal_ids: Sequence[str]) -> Mapping[str, Any]:
        from legba.data.byte_path import classify
        from legba.data.citation_rows_store import _RECEIVED_ORDER, _Pin

        wanted = set(signal_ids)
        by: dict[str, list[dict[str, Any]]] = {}
        for r in self.pin_rows:
            if str(r["record_id"]) in wanted:
                by.setdefault(str(r["record_id"]), []).append(r)
        out = {}
        for key, rows in by.items():
            st = classify(key, rows)
            vid, role = st.version_id, st.link_role
            if vid is None:
                first = min(rows, key=lambda r: (_RECEIVED_ORDER.get(str(r["role"]), 9), int(r["version_id"])))
                vid, role = int(first["version_id"]), str(first["role"])
            row = next(r for r in rows if int(r["version_id"]) == vid)
            out[key] = _Pin(vid, str(role), st.label, row["sha256"], row["text_sha256"])
        return out

    async def proposer(self, conn: Any, run_id: str | None, analyst_id: str | None) -> str | None:
        from legba.data.principals import seat_handle

        v = self.outputs.get((str(run_id), str(analyst_id)))
        return seat_handle(analyst_id, v) if v else None

    async def prior_read(self, conn: Any, run_id: str | None, ordinal: int | None) -> str | None:
        for h in self.handed:
            if str(h["run_id"]) == str(run_id) and h.get("target_kind") == "finding":
                for i, o in enumerate(h.get("ordinals") or []):
                    if o == ordinal:
                        return str(h["ids"][i])
        return None

    async def surface_entities(self, conn: Any, names: Sequence[str]) -> Mapping[str, str]:
        from legba.data.held_record_store import _surface_norm

        out = {}
        for n in names:
            ids = {str(a["entity_id"]) for a in self.alias if a["key"] == _surface_norm(n)}
            if len(ids) == 1:
                out[n] = ids.pop()
        return out


# ---------------------------------------------------------------------------
# The dry run: the writer's rules and the matching rule, simulated in order
# ---------------------------------------------------------------------------


async def simulate(rows: Sequence[Mapping[str, Any]], lookups: Any, conn: Any = None) -> dict[str, Any]:
    from legba.data.held_record_store import _resolve_surfaces, _surface_names

    types = type_map()
    held: dict[str, dict[str, Any]] = {}       # claim key -> {status, window, class}
    c: Counter = Counter()
    by_class: Counter = Counter()
    by_class_status: Counter = Counter()
    not_held: Counter = Counter()
    not_written: Counter = Counter()
    refused: Counter = Counter()
    bytes_labels: Counter = Counter()
    loose: Counter = Counter()
    loose_index: dict[tuple[str, str], set[int]] = {}
    for row in rows:
        shadow = row_as_shadow(row)
        plan = plan_shadow_record(shadow, types=types)
        c["shadow_records"] += 1
        c["surface_resolved"] += _resolve_surfaces(plan, await lookups.surface_entities(conn, _surface_names(shadow)),
                                                   types)
        pins = await lookups.pins(conn, sorted({s for r in plan.records for s, _, _ in r.evidence}))
        c["proposer_named"] += int(await lookups.proposer(conn, plan.run_id, plan.analyst_id) is not None)
        not_written.update(plan.not_written)
        refused.update(plan.refused)
        c["rows_planned"] += len(plan.records)
        for rec in plan.records:
            # a LOOSER rule (same type and subject, any text), measured, not used
            lk = (rec.type_id, rec.subject.key)
            if rec.claim_key not in held and lk in loose_index:
                earlier = loose_index[lk]
                loose["in_same_record" if plan.shadow_id in earlier else "across_records"] += 1
            loose_index.setdefault(lk, set()).add(plan.shadow_id)
            prior = held.get(rec.claim_key)
            if rec.class_ == "attributed":
                c["attributed_speaker_found"] += int(rec.speaker_text is not None)
            if prior is None:
                held[rec.claim_key] = {"status": rec.status, "window": (rec.valid_from, rec.valid_to)}
                c["assertions"] += 1
                c["versions"] += 1
                by_class[rec.class_] += 1
                by_class_status[f"{rec.class_}:{rec.status}"] += 1
                if rec.status != HELD:
                    not_held[rec.reason_code] += 1
                _evidence(rec, pins, c, bytes_labels)
                continue
            c["match_fired"] += 1
            c["match_fired_in_record" if rec.same_as_row else "match_fired_across_records"] += 1
            if rec.class_ == "absence" and prior["window"] != (rec.valid_from, rec.valid_to):
                c["match_new_version"] += 1
                c["versions"] += 1
                prior.update(status=HELD, window=(rec.valid_from, rec.valid_to))
                continue
            c["restatement_links_match"] += 1
            if prior["status"] == NOT_HELD and rec.status == HELD:
                c["match_now_held"] += 1
                prior["status"] = HELD
                _evidence(rec, pins, c, bytes_labels)
        for link in plan.links:
            if await lookups.prior_read(conn, plan.run_id, link.ref) is None:
                c["restatement_prior_read_unresolved"] += 1
            else:
                c["restatement_links_prior_read"] += 1
    return {"counts": dict(c), "assertions_by_class": dict(by_class), "by_class_status": dict(by_class_status),
            "not_held_reasons": dict(not_held), "not_written": dict(not_written), "refused": dict(refused),
            "bytes_labels": dict(bytes_labels), "looser_rule_would_fire": dict(loose),
            "final_status": dict(Counter(v["status"] for v in held.values()))}


def _evidence(rec: Any, pins: Mapping[str, Any], c: Counter, labels: Counter) -> None:
    for sid, _, _ in rec.evidence:
        pin = pins.get(sid)
        c["evidence_links"] += 1
        c["evidence_with_bytes" if pin else "evidence_without_bytes"] += 1
        if pin:
            labels[pin.label] += 1
    c["premise_links"] += len(rec.premises)
    c["verdict_links"] += int(rec.verdict is not None)


# ---------------------------------------------------------------------------
# Apply (a non-live database only)
# ---------------------------------------------------------------------------


def _with_db(dsn: str, db: str) -> str:
    u = urlsplit(dsn)
    return urlunsplit((u.scheme, u.netloc, f"/{db}", u.query, u.fragment))


def _refuse_live(dsn: str) -> None:
    if urlsplit(dsn).path.lstrip("/") == "legba":
        raise SystemExit("refused: the target database is named 'legba' (the live database); apply only "
                         "into a test database (--fresh-db on the test stack)")


async def apply(rows: Sequence[Mapping[str, Any]], target_dsn: str, *, batch: str, fresh: bool,
                keep: bool) -> dict[str, Any]:
    import asyncpg

    from legba.data.held_record_reads import chain_report
    from legba.data.held_record_store import NoLookups, write_shadow_record

    _refuse_live(target_dsn)
    db = None
    dsn = target_dsn
    if fresh:
        db = f"legba_heldreplay_{uuid4().hex[:8]}"
        admin = await asyncpg.connect(_with_db(target_dsn, "postgres"))
        try:
            await admin.execute(f'CREATE DATABASE "{db}"')
        finally:
            await admin.close()
        dsn = _with_db(target_dsn, db)
        from legba.data.config import PostgresConfig
        from legba.data.migrate import apply_primary_migrations

        u = urlsplit(dsn)
        await apply_primary_migrations(PostgresConfig(host=u.hostname or "127.0.0.1", port=u.port or 5432,
                                                      user=u.username or "legba", password=u.password or "",
                                                      database=db))
    out: dict[str, Any] = {"database": db or urlsplit(dsn).path.lstrip("/"), "batch": batch}
    conn = await asyncpg.connect(dsn)
    try:
        written = Counter()
        for row in rows:
            async with conn.transaction():
                res = await write_shadow_record(conn, row_as_shadow(row), lookups=NoLookups(), origin="replay",
                                                batch=batch)
            written["written" if res.written else "already"] += 1
            for k, v in res.counts.items():
                if isinstance(v, int):
                    written[k] += v
        # idempotency: a second pass writes nothing
        again = 0
        for row in rows:
            async with conn.transaction():
                again += int((await write_shadow_record(conn, row_as_shadow(row), lookups=NoLookups(),
                                                        origin="replay", batch=batch)).written)
        out["writer_counts"] = dict(written)
        out["second_pass_written"] = again
        q = {
            "assertions_by_class_status": "SELECT a.class || ':' || s.status, count(*) FROM assertions a "
                                          "JOIN assertion_status s ON s.assertion_id = a.id WHERE a.batch = $1 GROUP BY 1",
            "versions": "SELECT 'versions', count(*) FROM assertion_versions WHERE batch = $1",
            "status_rows": "SELECT status, count(*) FROM assertion_status_log WHERE batch = $1 GROUP BY 1",
            "support_by_role": "SELECT role || ':' || target_kind, count(*) FROM assertion_support WHERE batch = $1 GROUP BY 1",
            "receipts": "SELECT 'receipts', count(*) FROM assertion_shadow_receipts WHERE batch = $1",
        }
        out["tables"] = {k: {r[0]: r[1] for r in await conn.fetch(sql, batch)} for k, sql in q.items()}
        rep = await chain_report(conn)
        out["chain"] = {"rows": rep["rows"], "assertions": rep["assertions"], "breaks": rep["breaks"],
                        "cache_rows": rep["cache_rows"], "cache_differs": len(rep["cache_differs"])}
    finally:
        await conn.close()
    if fresh and db and not keep:
        admin = await asyncpg.connect(_with_db(target_dsn, "postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db}"')
        finally:
            await admin.close()
    return out


# ---------------------------------------------------------------------------


async def _read_source(args: argparse.Namespace) -> tuple[list[dict[str, Any]], Any, Any]:
    from datetime import datetime

    since = datetime.fromisoformat(args.since) if args.since else None
    if args.from_jsonl:
        rows = [json.loads(ln) for ln in Path(args.from_jsonl).read_text().splitlines() if ln.strip()]
        rows = [r for r in rows if r.get("analyst_id") in set(args.analyst)
                and (since is None or datetime.fromisoformat(str(r["occurred_at"])) >= since)]
        rows.sort(key=lambda r: (str(r["occurred_at"]), int(r["id"])))
        data = json.loads(Path(args.lookups_json).read_text()) if args.lookups_json else {}
        return rows, FileLookups(data), None
    import asyncpg

    from legba.data.held_record_store import DbLookups

    conn = await asyncpg.connect(args.dsn, server_settings={"default_transaction_read_only": "on"})
    rows = [dict(r) for r in await conn.fetch(_SOURCE_SQL, list(args.analyst), since)]
    return rows, DbLookups(), conn


async def main_async(args: argparse.Namespace) -> int:
    rows, lookups, conn = await _read_source(args)
    try:
        report: dict[str, Any] = {"source_records": len(rows), "analysts": sorted(set(args.analyst)),
                                  "dry_run": await simulate(rows, lookups, conn)}
    finally:
        if conn is not None:
            await conn.close()
    if args.apply:
        report["apply"] = await apply(rows, args.target_dsn, batch=args.batch, fresh=args.fresh_db,
                                      keep=args.keep)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dsn")
    src.add_argument("--from-jsonl")
    p.add_argument("--lookups-json")
    p.add_argument("--analyst", action="append", default=None)
    p.add_argument("--since")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--target-dsn")
    p.add_argument("--batch", default=None)
    p.add_argument("--fresh-db", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the fresh database (for EXPLAIN)")
    args = p.parse_args(argv)
    args.analyst = args.analyst or ["energy_security"]
    if args.apply and not args.target_dsn:
        p.error("--apply needs --target-dsn (a test database)")
    if args.apply:
        _refuse_live(args.target_dsn) if not args.fresh_db else None
        args.batch = args.batch or f"replay_{uuid4().hex[:8]}"
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
