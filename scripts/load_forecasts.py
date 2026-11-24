#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""load_forecasts.py — seed CLI for the exogenous forecast ledger (ST0-B).

Reads a curated forecasts YAML and writes, through the ONE store
(``src/legba/data/forecast_ledger.py``, migration 0242):

  * each question (idempotent on its ``slug``: unchanged → nothing written;
    changed and unsealed → restated; changed and sealed → refused, named);
  * the operator's number where the file gives one (``issuer_kind
    operator``); ``operator_probability: null`` is OWED — nothing is stated
    and the seal will refuse until it is.

``--seal`` is a SEPARATE, explicit step: it writes no question and no number,
it seals the file's questions — every one or none. It refuses (exit 1,
nothing sealed) while any question in the file is missing from the DB, lacks
an operator number, fails its resolver spec, or still carries an
``<OPERATOR …>`` placeholder. Once sealed, a question takes no new number and
its words cannot change.

House seed pattern: this MACHINERY ships; the curated DATA does not. The
default input ``seeds/forecasts.yaml`` is gitignored (``seeds/*.yaml``); the
tracked schema doc is ``seeds/forecasts.example.yaml`` (FICTIONAL rows only).

Usage (on the host, the registry image's env — the compose-run idiom):

    reg() { docker compose -p legba --env-file .env -f docker-compose.yml run --rm \\
        --no-deps -v "$PWD:$PWD" -w "$PWD" -e LEGBA_DATA_PG_DB=legba \\
        --entrypoint python legba-registry "$@"; }
    reg scripts/load_forecasts.py --dry-run          # parse + validate + seal preflight
    reg scripts/load_forecasts.py                    # load (re-run = 0 new)
    reg scripts/load_forecasts.py --seal --dry-run   # what the seal would refuse, against the DB
    reg scripts/load_forecasts.py --seal             # seal every question in the file, or none

Connection: PostgresConfig.from_env() (the same LEGBA_* env the runtime uses).
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from legba.data.forecast_ledger import (  # noqa: E402
    DEFAULT_YAML,
    ForecastNumber,
    LedgerRefusal,
    SeedEntry,
    parse_forecasts_yaml,
    seal_problems,
    seal_question,
    state_number,
    upsert_question,
)

_SELECT_ID = "SELECT id::text AS id FROM forecast_questions WHERE slug = $1"
#: Who writes the rows this loader inserts (data-model lane 9): the loader
#: itself; the YAML's own ``created_by`` / issuer columns say whose question
#: and whose number it is.
WRITTEN_BY = "rule:forecast_loader"


def file_seal_problems(entries: list[SeedEntry], errors: list[str]) -> list[str]:
    """What the FILE alone says the seal would refuse (no DB needed)."""
    out = [f"file: {e}" for e in errors]
    for e in entries:
        slug = e.question.slug
        if e.operator_probability is None:
            out.append(f"{slug}: operator_probability is owed")
        out += [f"{slug}: {k} still carries an operator placeholder" for k in e.question.owed_fields()]
    return out


def _row_view(e: SeedEntry) -> dict[str, Any]:
    q = e.question
    return {
        "slug": q.slug,
        "resolver_kind": q.resolver_kind,
        "due_at": q.due_at.isoformat(),
        "window": [q.window_start.isoformat() if q.window_start else None,
                   q.window_end.isoformat() if q.window_end else None],
        "operator_probability": e.operator_probability,
        "owed_fields": q.owed_fields(),
        "content_sha256": q.content_sha256(),
    }


async def load(conn: Any, entries: list[SeedEntry], *, as_of: datetime) -> dict[str, Any]:
    """Write every question and every given operator number; one bad row is
    reported, never an abort."""
    report: dict[str, Any] = {
        "questions": {"inserted": 0, "updated": 0, "unchanged": 0},
        "operator_numbers": {"inserted": 0, "updated": 0, "unchanged": 0},
        "owed": [],
        "errors": [],
    }
    for e in entries:
        slug = e.question.slug
        try:
            outcome, qid = await upsert_question(conn, e.question, written_by=WRITTEN_BY)
        except LedgerRefusal as exc:
            report["errors"].append(f"{slug}: {exc}")
            continue
        report["questions"][outcome] += 1
        if e.operator_probability is None:
            report["owed"].append(slug)
            continue
        try:
            n_out, _ = await state_number(conn, qid, ForecastNumber(
                issuer_kind="operator", issuer_ref=e.operator_ref,
                p=e.operator_probability, as_of=e.operator_as_of or as_of,
                rationale=e.operator_rationale,
            ), written_by=WRITTEN_BY)
        except LedgerRefusal as exc:
            report["errors"].append(f"{slug}: operator number: {exc}")
            continue
        report["operator_numbers"][n_out] += 1
    return report


async def seal(conn: Any, entries: list[SeedEntry], errors: list[str], *,
               at: datetime, dry_run: bool) -> dict[str, Any]:
    """Seal every question in the file, or none."""
    problems = [f"file: {e}" for e in errors]
    ids: list[tuple[str, str]] = []
    for e in entries:
        row = await conn.fetchrow(_SELECT_ID, e.question.slug)
        if row is None:
            problems.append(f"{e.question.slug}: not loaded")
            continue
        ids.append((e.question.slug, str(row["id"])))
        problems += await seal_problems(conn, str(row["id"]))
    if not entries:
        problems.append("file: no forecasts to seal")
    if problems or dry_run:
        return {"sealed": [], "refused": bool(problems), "problems": problems, "dry_run": dry_run}
    sealed: list[str] = []
    async with conn.transaction():
        for slug, qid in ids:
            await seal_question(conn, qid, at=at)
            sealed.append(slug)
    return {"sealed": sealed, "sealed_at": at.isoformat(), "refused": False, "problems": []}


async def run(conn_factory: Any, path: Path, *, dry_run: bool, do_seal: bool,
              at: datetime | None = None) -> tuple[int, dict[str, Any]]:
    """The CLI body; ``conn_factory()`` is awaited only when the DB is needed."""
    if not path.exists():
        return 0, {"file": str(path), "note": (
            "no seed file — nothing to do; Legba ships no bundled seed data "
            "(see seeds/forecasts.example.yaml)")}
    entries, errors = parse_forecasts_yaml(path)
    when = at or datetime.now(timezone.utc)
    if dry_run and not do_seal:
        return (1 if errors else 0), {
            "dry_run": True,
            "file": str(path),
            "valid_rows": [_row_view(e) for e in entries],
            "errors": errors,
            "seal_preflight": file_seal_problems(entries, errors),
        }
    conn = await conn_factory()
    try:
        if do_seal:
            out = await seal(conn, entries, errors, at=when, dry_run=dry_run)
            return (1 if out["refused"] else 0), {"file": str(path), **out}
        report = await load(conn, entries, as_of=when)
    finally:
        close = getattr(conn, "close", None)
        if close is not None:
            await close()
    report["errors"] = errors + report["errors"]
    return (1 if report["errors"] else 0), {"file": str(path), **report}


async def _connect() -> Any:  # pragma: no cover — the live path
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001 — dotenv optional
        pass
    import asyncpg

    from legba.data.config import PostgresConfig

    return await asyncpg.connect(PostgresConfig.from_env().dsn)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load the exogenous forecast ledger (ST0-B).")
    ap.add_argument("--file", type=Path, default=DEFAULT_YAML,
                    help=f"curated forecasts YAML (default: {DEFAULT_YAML})")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + validate (+ the seal preflight); write nothing")
    ap.add_argument("--seal", action="store_true",
                    help="seal every question in the file, or none (writes no question/number)")
    ap.add_argument("--seal-at", help="ISO instant to stamp as sealed_at (default: now)")
    a = ap.parse_args(argv)
    at = None
    if a.seal_at:
        at = datetime.fromisoformat(a.seal_at.replace("Z", "+00:00"))
        if at.tzinfo is None:
            ap.error("--seal-at must carry a timezone")
    code, out = asyncio.run(run(_connect, a.file, dry_run=a.dry_run, do_seal=a.seal, at=at))
    print(json.dumps(out, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
