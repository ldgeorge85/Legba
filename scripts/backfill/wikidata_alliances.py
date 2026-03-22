#!/usr/bin/env python3
"""Backfill alliance/organization memberships from Wikidata SPARQL.

Fetches membership data for key international organizations with join dates.
Stores as temporal facts (MemberOf predicate).

Organizations covered:
- NATO (Q7184), EU (Q458), GCC (Q217172), BRICS (Q42534268),
  ASEAN (Q7768), African Union (Q7159), UN Security Council permanent (Q160016),
  G7 (Q37143), G20 (Q35493), OPEC (Q1752581), Arab League (Q7172)

Usage:
    docker compose -p legba run --rm -v $(pwd)/scripts:/scripts \
      -e DATABASE_URL=postgresql://legba:legba@postgres:5432/legba \
      ingestion python3 /scripts/backfill/wikidata_alliances.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from uuid import uuid4

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "Legba-Backfill/1.0 (https://github.com/ldgeorge85/legba)"

DB_DSN = os.getenv("DATABASE_URL", "postgresql://legba:legba@postgres:5432/legba")

# Wikidata IDs for organizations to query
ORGANIZATIONS = {
    "Q7184": "NATO",
    "Q458": "European Union",
    "Q217172": "Gulf Cooperation Council",
    "Q42534268": "BRICS",
    "Q7768": "ASEAN",
    "Q7159": "African Union",
    "Q160016": "UN Security Council",
    "Q37143": "G7",
    "Q35493": "G20",
    "Q1752581": "OPEC",
    "Q7172": "Arab League",
    "Q8908": "OSCE",
    "Q81299": "OECD",
    "Q4230": "Commonwealth of Nations",
    "Q45177": "Five Eyes",
}


async def sparql_query(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Execute a SPARQL query against Wikidata."""
    resp = await client.get(
        WIKIDATA_ENDPOINT,
        params={"query": query, "format": "json"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


async def fetch_members(client: httpx.AsyncClient, org_qid: str, org_name: str) -> list[dict]:
    """Fetch current and historical members of an organization."""
    members = []

    # Current members (no end date)
    try:
        rows = await sparql_query(client, f"""
            SELECT ?memberLabel ?startDate WHERE {{
              wd:{org_qid} wdt:P527|wdt:P150 ?member .
              OPTIONAL {{
                ?member p:P463 ?stmt .
                ?stmt ps:P463 wd:{org_qid} .
                ?stmt pq:P580 ?startDate .
                FILTER NOT EXISTS {{ ?stmt pq:P582 ?endDate }}
              }}
              ?member wdt:P31 wd:Q3624078 .
              SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
            }}
        """)

        if not rows:
            # Alternative: query via P463 (member of) on the country side
            rows = await sparql_query(client, f"""
                SELECT ?memberLabel ?startDate WHERE {{
                  ?member wdt:P463 wd:{org_qid} .
                  ?member wdt:P31 wd:Q3624078 .
                  OPTIONAL {{
                    ?member p:P463 ?stmt .
                    ?stmt ps:P463 wd:{org_qid} .
                    ?stmt pq:P580 ?startDate .
                    FILTER NOT EXISTS {{ ?stmt pq:P582 ?endDate }}
                  }}
                  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
                }}
            """)

        for r in rows:
            name = r.get("memberLabel", {}).get("value", "")
            if not name or name.startswith("Q"):
                continue
            start = r.get("startDate", {}).get("value", "")[:10] if r.get("startDate") else None
            members.append({
                "country": name,
                "org": org_name,
                "start": start,
                "end": None,
            })
    except Exception as e:
        print(f"  Warning: query failed for {org_name}: {e}", file=sys.stderr)

    return members


async def store_memberships(memberships: list[dict]) -> tuple[int, int]:
    """Store membership facts in Postgres."""
    import asyncpg

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    inserted = 0
    updated = 0

    async with pool.acquire() as conn:
        for m in memberships:
            country = m["country"].strip()
            org = m["org"].strip()
            start = m.get("start")
            end = m.get("end")

            valid_from = datetime.fromisoformat(start) if start else None
            valid_until = datetime.fromisoformat(end) if end else None

            data = json.dumps({
                "membership": True,
                "effective_date": start,
                "source": "wikidata",
            })

            result = await conn.execute("""
                INSERT INTO facts (id, subject, predicate, value, confidence, source_type,
                                   data, valid_from, valid_until, created_at)
                VALUES ($1, $2, 'MemberOf', $3, 0.95, 'seed', $4::jsonb,
                        COALESCE($5, NOW()), $6, NOW())
                ON CONFLICT (lower(subject), lower(predicate), lower(value),
                             COALESCE(valid_from, '1970-01-01'::timestamptz))
                DO UPDATE SET
                    valid_from = LEAST(facts.valid_from, EXCLUDED.valid_from),
                    confidence = GREATEST(facts.confidence, EXCLUDED.confidence),
                    source_type = 'seed',
                    data = EXCLUDED.data,
                    updated_at = NOW()
            """,
                uuid4(), country, org, data, valid_from, valid_until,
            )

            if "INSERT" in result:
                inserted += 1
            elif "UPDATE" in result:
                updated += 1

    await pool.close()
    return inserted, updated


async def main():
    print("Fetching alliance memberships from Wikidata...")

    all_memberships = []

    async with httpx.AsyncClient(timeout=60) as client:
        for qid, name in ORGANIZATIONS.items():
            print(f"  {name}...")
            members = await fetch_members(client, qid, name)
            print(f"    → {len(members)} members")
            all_memberships.extend(members)
            # Rate limit courtesy
            await asyncio.sleep(1)

    print(f"\nTotal: {len(all_memberships)} membership records")

    print("Storing to database...")
    inserted, updated = await store_memberships(all_memberships)
    print(f"Done: {inserted} inserted, {updated} updated")


if __name__ == "__main__":
    asyncio.run(main())
