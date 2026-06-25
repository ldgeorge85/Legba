#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2, Task 2.1: Purge proposed situations from the database.

Removes all situations with status='proposed' (approximately 818 items from
the last burn-in). These are low-quality auto-detected situations that
should be cleared before a fresh burn-in.

Idempotent — safe to re-run. Only deletes proposed situations.

Usage:
    python3 scripts/purge_proposed_situations.py [--dry-run]

    # Via docker
    docker compose -p legba -f docker-compose.yml -f docker-compose.cognitive.yml run --rm --no-deps \
      -e POSTGRES_HOST=postgres -e POSTGRES_PORT=5432 \
      -e POSTGRES_USER=legba -e POSTGRES_PASSWORD=legba -e POSTGRES_DB=legba \
      agent python3 /app/scripts/purge_proposed_situations.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import asyncpg

DB_CONFIG = {
    "host": os.environ.get("POSTGRES_HOST", "localhost"),
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "user": os.environ.get("POSTGRES_USER", "legba"),
    "password": os.environ.get("POSTGRES_PASSWORD", "legba"),
    "database": os.environ.get("POSTGRES_DB", "legba"),
}


async def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("Phase 2.1: Purge Proposed Situations")
    print("=" * 60)
    if dry_run:
        print("  MODE: DRY RUN (no changes will be made)\n")

    print(f"Connecting to {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}...")
    try:
        conn = await asyncpg.connect(**DB_CONFIG)
    except Exception as e:
        print(f"  ERROR: Could not connect to Postgres: {e}")
        sys.exit(1)

    try:
        # Count proposed situations
        proposed_count = await conn.fetchval(
            "SELECT count(*) FROM situations WHERE status = 'proposed'"
        )
        total_count = await conn.fetchval("SELECT count(*) FROM situations")
        active_count = await conn.fetchval(
            "SELECT count(*) FROM situations WHERE status = 'active'"
        )

        print(f"\n  Total situations:    {total_count}")
        print(f"  Proposed situations: {proposed_count}")
        print(f"  Active situations:   {active_count}")

        if proposed_count == 0:
            print("\n  No proposed situations to purge. Done.")
            return

        if dry_run:
            # Show a sample of what would be deleted
            sample = await conn.fetch(
                "SELECT name, category, created_at FROM situations "
                "WHERE status = 'proposed' ORDER BY created_at DESC LIMIT 10"
            )
            print(f"\n  Sample of proposed situations to be purged:")
            for row in sample:
                print(f"    - {row['name']} ({row['category']}, {row['created_at'].date()})")
            print(f"\n  DRY RUN: Would delete {proposed_count} proposed situations.")
            return

        # First, delete any hypotheses linked to proposed situations
        hyp_deleted = await conn.execute(
            """DELETE FROM hypotheses
               WHERE situation_id IN (
                   SELECT id FROM situations WHERE status = 'proposed'
               )"""
        )
        hyp_count = int(hyp_deleted.split()[-1]) if hyp_deleted else 0
        print(f"\n  Deleted {hyp_count} hypotheses linked to proposed situations.")

        # Delete the proposed situations
        result = await conn.execute(
            "DELETE FROM situations WHERE status = 'proposed'"
        )
        deleted_count = int(result.split()[-1]) if result else 0
        print(f"  Deleted {deleted_count} proposed situations.")

        # Verify
        remaining = await conn.fetchval("SELECT count(*) FROM situations")
        print(f"\n  Remaining situations: {remaining}")
        print(f"  Purge complete.")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
