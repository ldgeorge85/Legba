# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m legba.data.alerts.cli raise ...`` — the host scripts' door into
the ONE alert path (L-1).

The host scripts (disk floor, LLM heartbeat, search canary, nightly suite,
stall watchdog) used to ``curl`` the push service directly and keep no record.
They now run this inside the runtime container (``scripts/lib/legba_alert.sh``,
``docker exec``), which raises through
:func:`legba.data.alerts.raise_alert.raise_alert` with the container's own
database and push-service configuration: the alert is logged, the page rule
decides, the delivery ledger records the push.

Exit status (the scripts read it):

* ``0`` — raised and recorded (paged or not: the rule decided);
* ``2`` — refused: the kind is not in the closed list, or a bad argument;
* ``3`` — the database could not be reached; NOTHING was recorded or sent, so
  the script falls back to its direct push (losing the database must not lose
  the disk page).

The body of the alert is read from stdin with ``--detail-stdin``. One line is
printed: the decision and its reason.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional, Sequence

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_NO_DATABASE = 3


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="legba.data.alerts.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("raise", help="raise one alert through the single alert path")
    r.add_argument("--kind", required=True)
    r.add_argument("--severity", required=True)
    r.add_argument("--subject", required=True, help="the subject id (a host path, a check name, ...)")
    r.add_argument("--subject-kind", default=None)
    r.add_argument("--summary", required=True)
    r.add_argument("--raised-by", required=True)
    r.add_argument("--dedupe-key", default=None)
    r.add_argument("--close", action="store_true", help="close the open episode (a recovery)")
    r.add_argument("--detail-stdin", action="store_true")
    r.add_argument("--evidence", action="append", default=[])
    return p


async def _raise(args: argparse.Namespace, detail: str) -> int:
    import asyncpg

    from ..config import PostgresConfig
    from .kinds import UnknownAlertKind, require_kind
    from .raise_alert import raise_alert
    from .sinks import AlertSinkDispatcher

    try:
        require_kind(args.kind)
    except UnknownAlertKind as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED
    try:
        pool = await asyncpg.create_pool(
            PostgresConfig.from_env().dsn, min_size=1, max_size=2, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"no_database: {type(exc).__name__}")
        return EXIT_NO_DATABASE
    try:
        try:
            await pool.fetchval("SELECT 1 FROM alerts LIMIT 1")
        except Exception as exc:  # noqa: BLE001 — not migrated ⇒ fall back
            print(f"no_database: alert log unavailable ({type(exc).__name__})")
            return EXIT_NO_DATABASE
        result = await raise_alert(
            pool,
            kind=args.kind,
            severity=args.severity,
            summary=args.summary,
            detail=detail,
            subject_id=args.subject,
            subject_kind=args.subject_kind,
            dedupe_key=args.dedupe_key,
            evidence_refs=args.evidence,
            close=args.close,
            raised_by=args.raised_by,
            dispatcher=AlertSinkDispatcher(pg_pool=pool),
        )
        print(f"{result.decision}: {result.reason} outcomes={','.join(result.outcomes) or '-'}")
        return EXIT_OK
    finally:
        await pool.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    detail = sys.stdin.read()[:4000] if args.detail_stdin else ""
    return asyncio.run(_raise(args, detail))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
