#!/usr/bin/env python3
"""Export backfill/seed data as a portable SQL file.

Dumps all facts with source_type='seed' and optionally entity profiles
into a SQL file that can be imported into a fresh Legba instance.

Usage:
    # Export seed data
    python3 scripts/backfill/seed_export.py > seed_data.sql

    # Import into fresh instance
    psql -U legba -d legba < seed_data.sql

The export uses INSERT ... ON CONFLICT for idempotent import.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

DB_DSN = os.getenv("DATABASE_URL", "postgresql://legba:legba@postgres:5432/legba")
OUTPUT_FILE = os.getenv("SEED_OUTPUT", "seed_data.sql")


def escape_sql(s: str) -> str:
    """Escape a string for SQL single quotes."""
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def format_ts(dt) -> str:
    """Format a datetime for SQL."""
    if dt is None:
        return "NULL"
    if isinstance(dt, str):
        return f"'{dt}'::timestamptz"
    return f"'{dt.isoformat()}'::timestamptz"


async def export_seed():
    import asyncpg
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)

    lines = []
    lines.append("-- Legba Seed Data Export")
    lines.append(f"-- Generated: {datetime.utcnow().isoformat()}")
    lines.append("-- Import: psql -U legba -d legba < seed_data.sql")
    lines.append("")
    lines.append("BEGIN;")
    lines.append("")

    async with pool.acquire() as conn:
        # Export seed facts
        facts = await conn.fetch("""
            SELECT id, subject, predicate, value, confidence, source_type,
                   data, valid_from, valid_until, created_at
            FROM facts
            WHERE source_type = 'seed'
            ORDER BY subject, predicate, valid_from
        """)

        lines.append(f"-- Facts: {len(facts)} seed records")
        lines.append("")

        for f in facts:
            data_json = json.dumps(f['data']) if isinstance(f['data'], dict) else str(f['data'])
            lines.append(
                f"INSERT INTO facts (id, subject, predicate, value, confidence, source_type, "
                f"data, valid_from, valid_until, created_at) VALUES ("
                f"{escape_sql(str(f['id']))}, "
                f"{escape_sql(f['subject'])}, "
                f"{escape_sql(f['predicate'])}, "
                f"{escape_sql(f['value'])}, "
                f"{f['confidence']}, "
                f"'seed', "
                f"{escape_sql(data_json)}::jsonb, "
                f"{format_ts(f['valid_from'])}, "
                f"{format_ts(f['valid_until'])}, "
                f"{format_ts(f['created_at'])}"
                f") ON CONFLICT (lower(subject), lower(predicate), lower(value), "
                f"COALESCE(valid_from, '1970-01-01'::timestamptz)) "
                f"DO UPDATE SET "
                f"valid_from = LEAST(facts.valid_from, EXCLUDED.valid_from), "
                f"valid_until = COALESCE(EXCLUDED.valid_until, facts.valid_until), "
                f"confidence = GREATEST(facts.confidence, EXCLUDED.confidence), "
                f"source_type = 'seed', data = EXCLUDED.data, updated_at = NOW();"
            )

        lines.append("")

        # Export entity profiles that were created/enriched by seed process
        # (optional — only if we add entity seeding later)
        entity_count = await conn.fetchval(
            "SELECT count(*) FROM entity_profiles WHERE completeness_score > 0.3"
        )
        lines.append(f"-- Entity profiles with completeness > 0.3: {entity_count}")
        lines.append(f"-- (Entity export not yet implemented — add when entity seeding is built)")

    lines.append("")
    lines.append("COMMIT;")
    lines.append("")

    await pool.close()

    # Write to file
    output = OUTPUT_FILE
    with open(output, "w") as fp:
        fp.write("\n".join(lines))

    print(f"Exported {len(facts)} seed facts to {output}", file=sys.stderr)
    print(f"File size: {os.path.getsize(output):,} bytes", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(export_seed())
