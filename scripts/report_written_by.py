#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""For the last N days (default 7), per class-(b) table of
``legba.data.write_attribution``: how many rows were written, and which KIND
of principal each would have named had ``written_by`` existed — READ-ONLY.

Before the migration lands nothing records the principal, so each table's
writer is reconstructed from what the row (or the audit log) already says:

* ``actor`` — a server-stamped audit actor (``descriptor_audit_log.actor_id``,
  ``consult_sessions.principal``, an author column the route stamps) mapped by
  ``principals.handle_for_actor`` exactly as the write path now maps it;
* ``rule`` / ``agent`` — the table's machine writer (the collection_gap rule,
  a proposing seat, a loader);
* ``unknown`` — the row carries nothing that names its writer (a route that
  stamped no author, or a body-claimed name with sign-in off).

Prints KINDS and COUNTS only — never a handle, a user id or a name. After the
migration, ``--stored`` counts the stored ``written_by`` column instead.

    python3 scripts/report_written_by.py [--days 7] [--json] [--stored]

Connection from the usual ``LEGBA_DATA_PG_*`` variables; the session is
``default_transaction_read_only`` with a 120 s statement timeout. Exit 2 on a
connection error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from collections import Counter, defaultdict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.principals import handle_for_actor, parse_handle  # noqa: E402
from legba.data.write_attribution import CLASS_B, by_class  # noqa: E402

#: Per class-(b) table: one query returning ``(event, how, actor, n)`` over
#: rows written since ``$1``. ``how`` is ``actor`` (map ``actor`` through
#: ``handle_for_actor``), or a principal KIND written out (``rule``,
#: ``agent``), or ``unknown``.
_DESC = """
SELECT 'version' AS event,
       CASE WHEN a.actor_id IS NOT NULL THEN 'actor' ELSE 'rule' END AS how,
       a.actor_id AS actor, count(*) AS n
  FROM {table} d
  LEFT JOIN LATERAL (
       SELECT actor_id FROM descriptor_audit_log l
        WHERE l.namespace = '{ns}' AND l.descriptor_id = d.descriptor_id
          AND l.to_version = d.version AND l.action IN ('register', 'update')
        ORDER BY l.occurred_at LIMIT 1) a ON true
 WHERE d.created_at >= $1
 GROUP BY 1, 2, 3
"""

QUERIES: dict[str, str] = {
    "watchlist": "SELECT 'insert', 'actor', created_by, count(*) FROM watchlist WHERE created_at >= $1 GROUP BY 3",
    "collection_requirements": """
        SELECT 'insert', 'rule', NULL, count(*) FROM collection_requirements WHERE created_at >= $1
        UNION ALL
        SELECT 'review', 'actor', reviewed_by, count(*) FROM collection_requirements
         WHERE reviewed_at >= $1 GROUP BY 3""",
    "correctness_labels": """
        SELECT 'label', CASE WHEN labeled_by LIKE 'desk_reference_sample/%' THEN 'rule' ELSE 'actor' END,
               CASE WHEN labeled_by LIKE 'desk_reference_sample/%' THEN NULL ELSE labeled_by END,
               count(*) FROM correctness_labels WHERE labeled_at >= $1 GROUP BY 2, 3""",
    "unit_reference_labels": """
        SELECT 'label', CASE WHEN labeled_by LIKE 'desk_reference/%' THEN 'rule' ELSE 'actor' END,
               CASE WHEN labeled_by LIKE 'desk_reference/%' THEN NULL ELSE labeled_by END, count(*)
          FROM unit_reference_labels WHERE created_at >= $1 GROUP BY 2, 3""",
    "source_credibility": "SELECT 'score', 'actor', scored_by, count(*) FROM source_credibility WHERE last_updated >= $1 GROUP BY 3",
    "journal_proposals": """
        SELECT 'propose', 'agent', NULL, count(*) FROM journal_proposals WHERE created_at >= $1
        UNION ALL
        SELECT 'decide', 'actor', decided_by, count(*) FROM journal_proposals
         WHERE decided_at >= $1 AND decided_by IS NOT NULL GROUP BY 3""",
    "entity_judgement": """
        SELECT 'verdict', CASE decided_by WHEN 'llm' THEN 'agent' WHEN 'rule' THEN 'rule' ELSE 'unknown' END,
               NULL, count(*) FROM entity_judgement WHERE decided_at >= $1 GROUP BY 2""",
    "consult_writes": """
        SELECT 'change', 'actor', s.principal, count(*) FROM consult_writes w
          LEFT JOIN consult_sessions s ON s.id = w.session_id
         WHERE w.created_at >= $1 GROUP BY 3""",
    "vocabulary_entries": "SELECT 'entry', 'unknown', NULL, count(*) FROM vocabulary_entries WHERE introduced >= $1 OR deprecated >= $1",
    "retention_policies": "SELECT 'edit', 'unknown', NULL, count(*) FROM retention_policies WHERE updated_at >= $1",
    "stack_components": _DESC.format(table="stack_components", ns="stack").replace(
        "d.descriptor_id", "d.component_id"),
    "stack_credentials": "SELECT 'secret', 'actor', created_by, count(*) FROM stack_credentials WHERE created_at >= $1 GROUP BY 3",
    "analyst_descriptors": _DESC.format(table="analyst_descriptors", ns="analyst"),
    "source_descriptors": _DESC.format(table="source_descriptors", ns="source"),
    "target_descriptors": _DESC.format(table="target_descriptors", ns="target"),
    "action_pack_descriptors": _DESC.format(table="action_pack_descriptors", ns="action_pack"),
    "collection_descriptors": _DESC.format(table="collection_descriptors", ns="collection"),
    "wiring_descriptors": "SELECT 'grant', 'unknown', NULL, count(*) FROM wiring_descriptors WHERE created_at >= $1",
    "read_events": "SELECT 'read', 'unknown', NULL, count(*) FROM read_events WHERE received_at >= $1",
    "scheduled_events": "SELECT 'entry', 'rule', NULL, count(*) FROM scheduled_events WHERE created_at >= $1",
    "forecast_questions": "SELECT 'question', 'rule', NULL, count(*) FROM forecast_questions WHERE created_at >= $1",
    "forecast_numbers": "SELECT 'number', 'rule', NULL, count(*) FROM forecast_numbers WHERE issued_at >= $1",
    "forecast_resolutions": "SELECT 'resolution', 'actor', resolved_by, count(*) FROM forecast_resolutions WHERE resolved_at >= $1 GROUP BY 3",
    "inquiry_ledger": "SELECT 'entry', 'agent', NULL, count(*) FROM inquiry_ledger WHERE updated_at >= $1",
    # Created by the migration: nothing exists before it.
    "origins": None,
    "origin_groups": None,
    "origin_assignment_log": None,
    # Citations as rows created with written_by from the start.
    "finding_sentences": None,
    "citation_support": None,
    "citation_unresolved": None,
    "run_handed": None,
    "finding_citation_receipts": None,
    # The held family: written_by NOT NULL from the start.
    "assertions": None,
    "assertion_versions": None,
    "assertion_support": None,
    "assertion_shadow_receipts": None,
}

_STORED = """
SELECT 'stored', COALESCE(p.kind, 'none'), NULL, count(*)
  FROM {table} t LEFT JOIN principals p ON p.id = t.written_by
 GROUP BY 2
"""


def _kind(how: str, actor: str | None) -> str:
    if how != "actor":
        return how
    handle = handle_for_actor(actor)
    if handle is None:
        # A name the row carries that is not a server-stamped actor: with
        # sign-in off the body's claimed name, or an empty column.
        return "unknown" if actor else "none_recorded"
    return parse_handle(handle)[0]


async def _connect():
    import asyncpg

    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
        server_settings={
            "default_transaction_read_only": "on",
            "statement_timeout": "120000",
            "application_name": "legba-written-by-report",
        },
    )


async def collect(conn, days: int, *, stored: bool = False) -> dict[str, dict[str, dict[str, int]]]:
    """``{table: {event: {kind: n}}}`` (pure over the connection)."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    tables = sorted(by_class(CLASS_B))
    since = await conn.fetchval("SELECT now() - make_interval(days => $1)", days)
    for table in tables:
        sql = _STORED.format(table=table) if stored else QUERIES.get(table)
        if sql is None:
            out[table] = {}
            continue
        counts: dict[str, Counter] = defaultdict(Counter)
        rows = await conn.fetch(sql) if stored else await conn.fetch(sql, since)
        for event, how, actor, n in rows:
            if int(n or 0):
                counts[str(event)][_kind(str(how), actor)] += int(n)
        out[table] = {e: dict(c) for e, c in counts.items()}
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stored", action="store_true",
                    help="count the stored written_by column (after the migration)")
    args = ap.parse_args()
    missing = sorted(set(by_class(CLASS_B)) - set(QUERIES))
    if missing:
        print(f"no query for class (b) tables: {missing}", file=sys.stderr)
        return 1
    try:
        conn = await _connect()
    except Exception as exc:  # noqa: BLE001 — a report, not a service
        print(f"cannot connect: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        async with conn.transaction(readonly=True):
            report = await collect(conn, args.days, stored=args.stored)
    finally:
        await conn.close()
    if args.json:
        print(json.dumps({"days": args.days, "tables": report}, indent=2, sort_keys=True))
        return 0
    print(f"class (b) tables, rows written in the last {args.days} days, by principal kind")
    for table, events in report.items():
        if not events:
            print(f"  {table:26} (none)")
            continue
        for event, kinds in sorted(events.items()):
            line = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
            print(f"  {table:26} {event:11} {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
