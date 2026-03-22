#!/usr/bin/env python3
"""Backfill organization memberships that Wikidata SPARQL didn't return.

Hardcoded authoritative data for: BRICS, UN Security Council (permanent),
G20, G7, OPEC, Arab League, Five Eyes, Quad, AUKUS.

Usage:
    docker compose -p legba run --rm -v $(pwd)/scripts:/scripts \
      -e DATABASE_URL=postgresql://legba:legba@postgres:5432/legba \
      ingestion python3 /scripts/backfill/manual_orgs.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

DB_DSN = os.getenv("DATABASE_URL", "postgresql://legba:legba@postgres:5432/legba")

# --- Membership data ---

MEMBERSHIPS = [
    # BRICS (expanded Jan 2024)
    ("Brazil", "BRICS", "2009-06-16"),
    ("Russia", "BRICS", "2009-06-16"),
    ("India", "BRICS", "2009-06-16"),
    ("China", "BRICS", "2009-06-16"),
    ("South Africa", "BRICS", "2010-12-24"),
    ("Egypt", "BRICS", "2024-01-01"),
    ("Ethiopia", "BRICS", "2024-01-01"),
    ("Iran", "BRICS", "2024-01-01"),
    ("Saudi Arabia", "BRICS", "2024-01-01"),
    ("United Arab Emirates", "BRICS", "2024-01-01"),
    ("Indonesia", "BRICS", "2025-01-01"),

    # UN Security Council — permanent members
    ("United States", "UN Security Council (Permanent)", "1945-10-24"),
    ("Russia", "UN Security Council (Permanent)", "1945-10-24"),
    ("China", "UN Security Council (Permanent)", "1945-10-24"),
    ("United Kingdom", "UN Security Council (Permanent)", "1945-10-24"),
    ("France", "UN Security Council (Permanent)", "1945-10-24"),

    # G20
    ("Argentina", "G20", "1999-09-26"),
    ("Australia", "G20", "1999-09-26"),
    ("Brazil", "G20", "1999-09-26"),
    ("Canada", "G20", "1999-09-26"),
    ("China", "G20", "1999-09-26"),
    ("France", "G20", "1999-09-26"),
    ("Germany", "G20", "1999-09-26"),
    ("India", "G20", "1999-09-26"),
    ("Indonesia", "G20", "1999-09-26"),
    ("Italy", "G20", "1999-09-26"),
    ("Japan", "G20", "1999-09-26"),
    ("Mexico", "G20", "1999-09-26"),
    ("Russia", "G20", "1999-09-26"),
    ("Saudi Arabia", "G20", "1999-09-26"),
    ("South Africa", "G20", "1999-09-26"),
    ("South Korea", "G20", "1999-09-26"),
    ("Turkey", "G20", "1999-09-26"),
    ("United Kingdom", "G20", "1999-09-26"),
    ("United States", "G20", "1999-09-26"),

    # OPEC
    ("Iran", "OPEC", "1960-09-14"),
    ("Iraq", "OPEC", "1960-09-14"),
    ("Kuwait", "OPEC", "1960-09-14"),
    ("Saudi Arabia", "OPEC", "1960-09-14"),
    ("Venezuela", "OPEC", "1960-09-14"),
    ("Algeria", "OPEC", "1969-01-01"),
    ("Libya", "OPEC", "1962-01-01"),
    ("Nigeria", "OPEC", "1971-01-01"),
    ("United Arab Emirates", "OPEC", "1967-01-01"),
    ("Angola", "OPEC", "2007-01-01"),
    ("Republic of the Congo", "OPEC", "2018-01-01"),
    ("Equatorial Guinea", "OPEC", "2017-01-01"),
    ("Gabon", "OPEC", "2016-07-01"),

    # Arab League
    ("Egypt", "Arab League", "1945-03-22"),
    ("Iraq", "Arab League", "1945-03-22"),
    ("Jordan", "Arab League", "1945-03-22"),
    ("Lebanon", "Arab League", "1945-03-22"),
    ("Saudi Arabia", "Arab League", "1945-03-22"),
    ("Syria", "Arab League", "1945-03-22"),
    ("Yemen", "Arab League", "1945-03-22"),
    ("Libya", "Arab League", "1953-03-28"),
    ("Sudan", "Arab League", "1956-01-19"),
    ("Morocco", "Arab League", "1958-10-01"),
    ("Tunisia", "Arab League", "1958-10-01"),
    ("Kuwait", "Arab League", "1961-07-20"),
    ("Algeria", "Arab League", "1962-08-16"),
    ("Bahrain", "Arab League", "1971-09-11"),
    ("Qatar", "Arab League", "1971-09-11"),
    ("United Arab Emirates", "Arab League", "1971-12-06"),
    ("Oman", "Arab League", "1971-09-29"),
    ("Mauritania", "Arab League", "1973-11-26"),
    ("Somalia", "Arab League", "1974-02-14"),
    ("Palestine", "Arab League", "1976-09-09"),
    ("Djibouti", "Arab League", "1977-09-04"),
    ("Comoros", "Arab League", "1993-11-20"),

    # Five Eyes
    ("United States", "Five Eyes", "1946-03-05"),
    ("United Kingdom", "Five Eyes", "1946-03-05"),
    ("Canada", "Five Eyes", "1948-01-01"),
    ("Australia", "Five Eyes", "1956-01-01"),
    ("New Zealand", "Five Eyes", "1956-01-01"),

    # Quad (Quadrilateral Security Dialogue)
    ("United States", "Quad", "2007-05-25"),
    ("Japan", "Quad", "2007-05-25"),
    ("Australia", "Quad", "2007-05-25"),
    ("India", "Quad", "2007-05-25"),

    # AUKUS
    ("Australia", "AUKUS", "2021-09-15"),
    ("United Kingdom", "AUKUS", "2021-09-15"),
    ("United States", "AUKUS", "2021-09-15"),
]

# --- Key hostile/allied relationships ---

RELATIONSHIPS = [
    # Active conflicts/hostilities
    ("Russia", "HostileTo", "Ukraine", "2022-02-24"),
    ("Iran", "HostileTo", "Israel", "1979-04-01"),
    ("Iran", "HostileTo", "United States", "1979-11-04"),
    ("North Korea", "HostileTo", "South Korea", "1950-06-25"),
    ("North Korea", "HostileTo", "United States", "1950-06-25"),
    ("China", "HostileTo", "Taiwan", "1949-10-01"),

    # Key alliances
    ("United States", "AlliedWith", "Israel", "1948-05-14"),
    ("United States", "AlliedWith", "United Kingdom", "1942-01-01"),
    ("United States", "AlliedWith", "Japan", "1952-04-28"),
    ("United States", "AlliedWith", "South Korea", "1953-10-01"),
    ("Russia", "AlliedWith", "China", "2001-07-16"),
    ("Russia", "AlliedWith", "Iran", "2015-09-30"),
    ("Iran", "AlliedWith", "Syria", "1979-01-01"),
    ("Saudi Arabia", "AlliedWith", "United Arab Emirates", "1981-05-25"),
    ("Iran", "SuppliesWeaponsTo", "Hezbollah", "1982-01-01"),
    ("United States", "SuppliesWeaponsTo", "Israel", "1962-01-01"),
    ("Russia", "SuppliesWeaponsTo", "India", "1960-01-01"),
    ("China", "SuppliesWeaponsTo", "Pakistan", "1966-01-01"),

    # Sanctions
    ("United States", "SanctionedBy", "Iran", "1979-11-14"),
    ("United States", "SanctionedBy", "North Korea", "1950-06-28"),
    ("United States", "SanctionedBy", "Russia", "2014-03-17"),
    ("European Union", "SanctionedBy", "Russia", "2014-07-31"),
]


async def store_all():
    import asyncpg

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    inserted = 0

    async with pool.acquire() as conn:
        # Store memberships
        for country, org, start_date in MEMBERSHIPS:
            valid_from = datetime.fromisoformat(start_date) if start_date else None
            data = json.dumps({"membership": True, "effective_date": start_date, "source": "manual_seed"})

            result = await conn.execute("""
                INSERT INTO facts (id, subject, predicate, value, confidence, source_type,
                                   data, valid_from, valid_until, created_at)
                VALUES ($1, $2, 'MemberOf', $3, 0.98, 'seed', $4::jsonb,
                        COALESCE($5, NOW()), NULL, NOW())
                ON CONFLICT (lower(subject), lower(predicate), lower(value),
                             COALESCE(valid_from, '1970-01-01'::timestamptz))
                DO UPDATE SET
                    confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
                    source_type = 'seed', data = EXCLUDED.data, updated_at = NOW()
            """, uuid4(), country, org, data, valid_from)

            if "INSERT" in result:
                inserted += 1

        # Store relationships
        for subj, pred, obj, start_date in RELATIONSHIPS:
            valid_from = datetime.fromisoformat(start_date) if start_date else None
            data = json.dumps({"effective_date": start_date, "source": "manual_seed"})

            result = await conn.execute("""
                INSERT INTO facts (id, subject, predicate, value, confidence, source_type,
                                   data, valid_from, valid_until, created_at)
                VALUES ($1, $2, $3, $4, 0.95, 'seed', $5::jsonb,
                        COALESCE($6, NOW()), NULL, NOW())
                ON CONFLICT (lower(subject), lower(predicate), lower(value),
                             COALESCE(valid_from, '1970-01-01'::timestamptz))
                DO UPDATE SET
                    confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
                    source_type = 'seed', data = EXCLUDED.data, updated_at = NOW()
            """, uuid4(), subj, pred, obj, data, valid_from)

            if "INSERT" in result:
                inserted += 1

    await pool.close()
    return inserted


async def main():
    print(f"Seeding {len(MEMBERSHIPS)} memberships + {len(RELATIONSHIPS)} relationships...")
    inserted = await store_all()
    print(f"Done: {inserted} inserted")


if __name__ == "__main__":
    asyncio.run(main())
