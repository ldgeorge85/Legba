#!/usr/bin/env python3
"""Backfill geo fields on existing entities and facts.

A.8/A.9 wired geo resolution into the save paths for new data, but
existing entities (~600) and facts (~13k) have NULL geo fields.
This script resolves and populates them.

Usage:
    docker compose -p legba exec ingestion python3 /app/scripts/backfill/geo_backfill.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Add src to path for geo module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

DB_DSN = os.getenv("DATABASE_URL", "postgresql://legba:legba@postgres:5432/legba")


async def backfill_entities() -> tuple[int, int]:
    """Resolve geo for entity_profiles with NULL geo_lat."""
    import asyncpg
    from legba.agent.tools.builtins.geo import resolve_locations

    conn = await asyncpg.connect(DB_DSN)
    try:
        rows = await conn.fetch(
            "SELECT id, canonical_name FROM entity_profiles WHERE geo_lat IS NULL"
        )
        print(f"  Entities to process: {len(rows)}")

        updated = 0
        skipped = 0
        for row in rows:
            name = row["canonical_name"]
            try:
                geo = resolve_locations([name])
            except Exception:
                skipped += 1
                continue

            geo_lat, geo_lon = None, None
            geo_country, geo_region = None, None

            if geo.get("coordinates"):
                coord = geo["coordinates"][0]
                geo_lat = coord["lat"]
                geo_lon = coord["lon"]
            if geo.get("countries"):
                geo_country = geo["countries"][0]
            if geo.get("regions"):
                geo_region = geo["regions"][0]

            if not geo_lat and not geo_country:
                skipped += 1
                continue

            await conn.execute(
                """UPDATE entity_profiles
                   SET geo_lat = COALESCE($2, geo_lat),
                       geo_lon = COALESCE($3, geo_lon),
                       geo_country = COALESCE($4, geo_country),
                       geo_region = COALESCE($5, geo_region),
                       updated_at = NOW()
                   WHERE id = $1""",
                row["id"], geo_lat, geo_lon, geo_country, geo_region,
            )
            updated += 1

        return updated, skipped
    finally:
        await conn.close()


async def backfill_facts() -> tuple[int, int]:
    """Resolve geo for facts with NULL geo_lat."""
    import asyncpg
    from legba.agent.tools.builtins.geo import resolve_locations

    conn = await asyncpg.connect(DB_DSN)
    try:
        rows = await conn.fetch(
            "SELECT id, subject, value FROM facts WHERE geo_lat IS NULL"
        )
        print(f"  Facts to process: {len(rows)}")

        updated = 0
        skipped = 0
        for row in rows:
            try:
                geo = resolve_locations([row["subject"], row["value"]])
            except Exception:
                skipped += 1
                continue

            if not geo.get("coordinates"):
                skipped += 1
                continue

            coord = geo["coordinates"][0]
            await conn.execute(
                """UPDATE facts
                   SET geo_lat = $2, geo_lon = $3, updated_at = NOW()
                   WHERE id = $1""",
                row["id"], coord["lat"], coord["lon"],
            )
            updated += 1

        return updated, skipped
    finally:
        await conn.close()


async def main():
    # Pre-check counts
    import asyncpg
    conn = await asyncpg.connect(DB_DSN)
    e_total = await conn.fetchval("SELECT count(*) FROM entity_profiles")
    e_geo = await conn.fetchval(
        "SELECT count(*) FROM entity_profiles WHERE geo_lat IS NOT NULL"
    )
    f_total = await conn.fetchval("SELECT count(*) FROM facts")
    f_geo = await conn.fetchval(
        "SELECT count(*) FROM facts WHERE geo_lat IS NOT NULL"
    )
    await conn.close()
    print(f"Before: entities {e_geo}/{e_total} have geo, "
          f"facts {f_geo}/{f_total} have geo")

    print("\nBackfilling entities...")
    e_updated, e_skipped = await backfill_entities()
    print(f"  Updated: {e_updated}, Skipped: {e_skipped}")

    print("\nBackfilling facts...")
    f_updated, f_skipped = await backfill_facts()
    print(f"  Updated: {f_updated}, Skipped: {f_skipped}")

    # Post-check
    conn = await asyncpg.connect(DB_DSN)
    e_geo = await conn.fetchval(
        "SELECT count(*) FROM entity_profiles WHERE geo_lat IS NOT NULL"
    )
    f_geo = await conn.fetchval(
        "SELECT count(*) FROM facts WHERE geo_lat IS NOT NULL"
    )
    await conn.close()
    print(f"\nAfter: entities {e_geo}/{e_total} have geo, "
          f"facts {f_geo}/{f_total} have geo")


if __name__ == "__main__":
    print("Geo Enrichment Backfill")
    print("=" * 40)
    asyncio.run(main())
