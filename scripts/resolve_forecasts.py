#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve the forecast ledger: proposals by rule, decisions by the operator (ST1-A).

  run       find due questions, run the resolvers, write proposals (idempotent),
            settle series_threshold by rule. ``--dry-run`` writes nothing.
  rehearse  every sealed open question AS IF due now (grace spent), read-only:
            the session is opened READ ONLY, nothing can be written.
  confirm   accept a proposal's yes|no|void:  --record ID --actor operator:<id>
  reject    reject a proposal (it stays open): --record ID --actor operator:<id>
            --reason "..."
  override  decide what the rule cannot:      --slug S --outcome yes|no|void
            --actor operator:<id> --reason "..." [--judgement KEY ...]
            [--basis RECORD] [--evidence REF ...]  (writes an override_log row)
  correct   replace an active resolution:     --slug S --outcome ... --actor
            operator:<id> --reason "..." [--evidence REF ...]

Output is JSON on stdout. Connection: PostgresConfig.from_env() (the LEGBA_*
env the runtime uses). No model is called; nothing here costs money.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legba.data import forecast_resolution as fr  # noqa: E402

_QID_SQL = "SELECT id::text AS id FROM forecast_questions WHERE slug = $1"


async def _connect(read_only: bool) -> Any:  # pragma: no cover — the live path
    import asyncpg

    from legba.data.config import PostgresConfig

    settings = {"default_transaction_read_only": "on"} if read_only else None
    return await asyncpg.connect(PostgresConfig.from_env().dsn, server_settings=settings)


async def _qid(conn: Any, slug: str) -> str:
    row = await conn.fetchrow(_QID_SQL, slug)
    if row is None:
        raise SystemExit(f"no forecast question {slug!r}")
    return str(row["id"])


async def run(conn: Any, a: argparse.Namespace, now: datetime) -> dict[str, Any]:
    if a.cmd in ("run", "rehearse"):
        return await fr.run_resolution_job(
            conn, now=now, dry_run=a.cmd == "rehearse" or a.dry_run, as_if_due=a.cmd == "rehearse",
            slugs=a.slug or None,
        )
    if a.cmd == "confirm":
        return {"confirmation_id": await fr.confirm_proposal(conn, a.record, actor=a.actor, note=a.note, now=now)}
    if a.cmd == "reject":
        rid, new = await fr.reject_proposal(conn, a.record, actor=a.actor, reason=a.reason, now=now)
        return {"rejection_id": rid, "written": new}
    qid = await _qid(conn, a.slug)
    if a.cmd == "override":
        return {"override_id": await fr.override_resolution(
            conn, qid, outcome=a.outcome, actor=a.actor, reason=a.reason,
            judgements=a.judgement or (), evidence_refs=a.evidence or (), basis_record_id=a.basis, now=now)}
    return {"correction_id": await fr.correct_resolution(
        conn, qid, outcome=a.outcome, actor=a.actor, reason=a.reason, evidence_refs=a.evidence or (), now=now)}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "rehearse"):
        s = sub.add_parser(name)
        s.add_argument("--slug", action="append")
        s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("confirm")
    s.add_argument("--record", required=True)
    s.add_argument("--actor", required=True)
    s.add_argument("--note")
    s = sub.add_parser("reject")
    s.add_argument("--record", required=True)
    s.add_argument("--actor", required=True)
    s.add_argument("--reason", required=True)
    for name in ("override", "correct"):
        s = sub.add_parser(name)
        s.add_argument("--slug", required=True)
        s.add_argument("--outcome", required=True, choices=("yes", "no", "void"))
        s.add_argument("--actor", required=True)
        s.add_argument("--reason", required=True)
        s.add_argument("--evidence", action="append")
        if name == "override":
            s.add_argument("--judgement", action="append")
            s.add_argument("--basis")
    p.add_argument("--now", help="ISO instant (default: now)")
    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — the live path
    a = _parser().parse_args(argv)
    now = (datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(timezone.utc))

    async def _go() -> dict[str, Any]:
        conn = await _connect(read_only=a.cmd == "rehearse" or bool(getattr(a, "dry_run", False)))
        try:
            return await run(conn, a, now)
        finally:
            await conn.close()

    print(json.dumps(asyncio.run(_go()), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
