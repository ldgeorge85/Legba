#!/usr/bin/env python3
"""Backfill world leaders from Wikidata SPARQL.

Fetches current and historical heads of state and government with
inauguration/end dates. Stores as temporal facts.

Usage:
    docker compose -p legba exec ui python3 /app/scripts/backfill/wikidata_leaders.py
    # Or directly:
    python3 scripts/backfill/wikidata_leaders.py

Idempotent — uses ON CONFLICT to merge with existing facts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

import httpx

# Add src to path for DB access
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "Legba-Backfill/1.0 (https://github.com/ldgeorge85/legba)"

DB_DSN = os.getenv("DATABASE_URL", "postgresql://legba:legba@postgres:5432/legba")
# Fallback for running from host
if "postgres" in DB_DSN and os.getenv("POSTGRES_HOST"):
    DB_DSN = f"postgresql://legba:legba@{os.getenv('POSTGRES_HOST', 'localhost')}:5432/legba"


async def sparql_query(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Execute a SPARQL query against Wikidata."""
    resp = await client.get(
        WIKIDATA_ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


def extract_value(binding: dict, key: str) -> str:
    """Extract a string value from a SPARQL binding."""
    return binding.get(key, {}).get("value", "")


def extract_date(binding: dict, key: str) -> str | None:
    """Extract an ISO date from a SPARQL binding."""
    val = extract_value(binding, key)
    return val[:10] if val else None


async def fetch_current_leaders(client: httpx.AsyncClient) -> list[dict]:
    """Fetch current heads of state and government."""
    leaders = []

    # Heads of state
    rows = await sparql_query(client, """
        SELECT ?countryLabel ?leaderLabel ?startDate WHERE {
          ?country wdt:P31 wd:Q3624078 .
          ?country p:P35 ?stmt .
          ?stmt ps:P35 ?leader .
          ?stmt pq:P580 ?startDate .
          FILTER NOT EXISTS { ?stmt pq:P582 ?endDate }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
        }
        ORDER BY ?countryLabel
    """)
    for r in rows:
        leaders.append({
            "leader": extract_value(r, "leaderLabel"),
            "country": extract_value(r, "countryLabel"),
            "role": "head_of_state",
            "start": extract_date(r, "startDate"),
            "end": None,
        })

    # Heads of government
    rows = await sparql_query(client, """
        SELECT ?countryLabel ?leaderLabel ?startDate WHERE {
          ?country wdt:P31 wd:Q3624078 .
          ?country p:P6 ?stmt .
          ?stmt ps:P6 ?leader .
          ?stmt pq:P580 ?startDate .
          FILTER NOT EXISTS { ?stmt pq:P582 ?endDate }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
        }
        ORDER BY ?countryLabel
    """)
    for r in rows:
        leaders.append({
            "leader": extract_value(r, "leaderLabel"),
            "country": extract_value(r, "countryLabel"),
            "role": "head_of_government",
            "start": extract_date(r, "startDate"),
            "end": None,
        })

    return leaders


async def fetch_historical_leaders(client: httpx.AsyncClient, since_year: int = 2005) -> list[dict]:
    """Fetch historical heads of state and government since a given year."""
    leaders = []

    # Historical heads of state
    rows = await sparql_query(client, f"""
        SELECT ?countryLabel ?leaderLabel ?startDate ?endDate WHERE {{
          ?country wdt:P31 wd:Q3624078 .
          ?country p:P35 ?stmt .
          ?stmt ps:P35 ?leader .
          ?stmt pq:P580 ?startDate .
          ?stmt pq:P582 ?endDate .
          FILTER(?startDate > "{since_year}-01-01"^^xsd:dateTime)
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
        }}
        ORDER BY ?countryLabel ?startDate
    """)
    for r in rows:
        leaders.append({
            "leader": extract_value(r, "leaderLabel"),
            "country": extract_value(r, "countryLabel"),
            "role": "head_of_state",
            "start": extract_date(r, "startDate"),
            "end": extract_date(r, "endDate"),
        })

    # Historical heads of government
    rows = await sparql_query(client, f"""
        SELECT ?countryLabel ?leaderLabel ?startDate ?endDate WHERE {{
          ?country wdt:P31 wd:Q3624078 .
          ?country p:P6 ?stmt .
          ?stmt ps:P6 ?leader .
          ?stmt pq:P580 ?startDate .
          ?stmt pq:P582 ?endDate .
          FILTER(?startDate > "{since_year}-01-01"^^xsd:dateTime)
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
        }}
        ORDER BY ?countryLabel ?startDate
    """)
    for r in rows:
        leaders.append({
            "leader": extract_value(r, "leaderLabel"),
            "country": extract_value(r, "countryLabel"),
            "role": "head_of_government",
            "start": extract_date(r, "startDate"),
            "end": extract_date(r, "endDate"),
        })

    return leaders


async def store_leaders(leaders: list[dict]) -> tuple[int, int]:
    """Store leader facts in Postgres with temporal markers."""
    import asyncpg

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    inserted = 0
    updated = 0

    async with pool.acquire() as conn:
        for leader in leaders:
            name = leader["leader"].strip()
            country = leader["country"].strip()
            start = leader["start"]
            end = leader["end"]

            if not name or not country or name.startswith("Q") or country.startswith("Q"):
                continue  # Skip unresolved Wikidata IDs

            valid_from = datetime.fromisoformat(start) if start else None
            valid_until = datetime.fromisoformat(end) if end else None

            data = json.dumps({
                "role": leader["role"],
                "effective_date": start,
                "source": "wikidata",
            })

            result = await conn.execute("""
                INSERT INTO facts (id, subject, predicate, value, confidence, source_type,
                                   data, valid_from, valid_until, created_at)
                VALUES ($1, $2, 'LeaderOf', $3, 0.95, 'seed', $4::jsonb,
                        COALESCE($5, NOW()), $6, NOW())
                ON CONFLICT (lower(subject), lower(predicate), lower(value),
                             COALESCE(valid_from, '1970-01-01'::timestamptz))
                DO UPDATE SET
                    valid_from = LEAST(facts.valid_from, EXCLUDED.valid_from),
                    valid_until = COALESCE(EXCLUDED.valid_until, facts.valid_until),
                    confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
                    source_type = 'seed',
                    data = EXCLUDED.data,
                    updated_at = NOW()
            """,
                uuid4(), name, country, data, valid_from, valid_until,
            )

            if "INSERT" in result:
                inserted += 1
            elif "UPDATE" in result:
                updated += 1

    await pool.close()
    return inserted, updated


async def main():
    print("Fetching leaders from Wikidata...")

    async with httpx.AsyncClient(timeout=60) as client:
        print("  Current leaders...")
        current = await fetch_current_leaders(client)
        print(f"  → {len(current)} current leaders")

        print("  Historical leaders (since 2005)...")
        historical = await fetch_historical_leaders(client, since_year=2005)
        print(f"  → {len(historical)} historical leaders")

    all_leaders = current + historical
    print(f"\nTotal: {len(all_leaders)} leader records")

    print("Storing to database...")
    inserted, updated = await store_leaders(all_leaders)
    print(f"Done: {inserted} inserted, {updated} updated")


if __name__ == "__main__":
    asyncio.run(main())
