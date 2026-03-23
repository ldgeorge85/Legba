#!/usr/bin/env python3
"""Extract entities and relations from unlinked signals using REBEL.

Reads signals that have no entity links, sends titles to /extract,
and stores the resulting triples as facts.

Usage:
    docker compose -p legba run --rm -v $(pwd)/scripts:/scripts \
      -e DATABASE_URL=postgresql://legba:legba@postgres:5432/legba \
      -e MODELS_API_URL=https://models.ai1.infra.innoscale.net \
      -e MODELS_API_USER=legba \
      -e MODELS_API_PASS=$MODELS_API_PASS \
      ingestion python3 /scripts/backfill/extract_entities.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from uuid import uuid4

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

DB_DSN = os.getenv("DATABASE_URL", "postgresql://legba:legba@postgres:5432/legba")
MODELS_URL = os.getenv("MODELS_API_URL", "").rstrip("/")
MODELS_USER = os.getenv("MODELS_API_USER", "")
MODELS_PASS = os.getenv("MODELS_API_PASS", "")
BATCH_SIZE = 50
MAX_SIGNALS = int(os.getenv("MAX_SIGNALS", "0"))  # 0 = all

# Map REBEL predicates to our canonical vocabulary
PREDICATE_MAP = {
    # Location
    "place of birth": "LocatedIn",
    "country of citizenship": "LocatedIn",
    "located in the administrative territorial entity": "LocatedIn",
    "country": "LocatedIn",
    "headquarters location": "LocatedIn",
    "location": "LocatedIn",
    "capital": "LocatedIn",
    "continent": "LocatedIn",
    "territory": "LocatedIn",
    # Borders
    "shares border with": "BordersWith",
    "border": "BordersWith",
    # Leadership
    "head of government": "LeaderOf",
    "head of state": "LeaderOf",
    "position held": "LeaderOf",
    "officeholder": "LeaderOf",
    "leader": "LeaderOf",
    "chairperson": "LeaderOf",
    "director": "LeaderOf",
    "commander": "LeaderOf",
    "chief executive officer": "LeaderOf",
    # Membership
    "member of": "MemberOf",
    "member of political party": "MemberOf",
    "membership": "MemberOf",
    # Part of
    "part of": "PartOf",
    "participant": "PartOf",
    "instance of": "PartOf",
    "subclass of": "PartOf",
    # Operations
    "operator": "OperatesIn",
    "manufacturer": "OperatesIn",
    "country of origin": "OperatesIn",
    # Conflict
    "conflict": "HostileTo",
    "enemy": "HostileTo",
    "opponent": "HostileTo",
    # Alliance
    "alliance": "AlliedWith",
    "ally": "AlliedWith",
    "diplomatic relation": "AlliedWith",
    # Affiliation
    "employer": "AffiliatedWith",
    "affiliation": "AffiliatedWith",
    "owned by": "AffiliatedWith",
    "founded by": "FundedBy",
    "sponsor": "FundedBy",
    "subsidiary": "PartOf",
    # Trade/supply
    "import": "TradesWith",
    "export": "TradesWith",
    "supplier": "SuppliesWeaponsTo",
}


def map_predicate(rebel_pred: str) -> str | None:
    """Map a REBEL predicate to our canonical vocabulary. Returns None if unmappable."""
    lower = rebel_pred.lower().strip()
    mapped = PREDICATE_MAP.get(lower)
    if mapped:
        return mapped
    # Check partial matches
    for key, val in PREDICATE_MAP.items():
        if key in lower or lower in key:
            return val
    return None


async def main():
    if not MODELS_URL:
        print("ERROR: MODELS_API_URL not set", file=sys.stderr)
        sys.exit(1)

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    # Count unlinked signals
    total = await pool.fetchval("""
        SELECT count(*) FROM signals s
        LEFT JOIN signal_entity_links sel ON sel.signal_id = s.id
        WHERE sel.signal_id IS NULL AND length(s.title) >= 20
    """)
    logger.info(f"Unlinked signals to process: {total}")

    if MAX_SIGNALS > 0:
        total = min(total, MAX_SIGNALS)
        logger.info(f"Limited to: {total}")

    if total == 0:
        return

    auth = httpx.BasicAuth(MODELS_USER, MODELS_PASS) if MODELS_USER else None
    async with httpx.AsyncClient(base_url=MODELS_URL, auth=auth, timeout=30) as http:
        resp = await http.get("/health")
        if resp.status_code != 200:
            print(f"ERROR: Models service unhealthy", file=sys.stderr)
            sys.exit(1)
        logger.info("Models service healthy")

        facts_created = 0
        signals_processed = 0
        errors = 0
        offset = 0

        while offset < total:
            rows = await pool.fetch("""
                SELECT s.id, s.title FROM signals s
                LEFT JOIN signal_entity_links sel ON sel.signal_id = s.id
                WHERE sel.signal_id IS NULL AND length(s.title) >= 20
                ORDER BY s.created_at DESC
                LIMIT $1 OFFSET $2
            """, BATCH_SIZE, offset)

            if not rows:
                break

            for row in rows:
                try:
                    resp = await http.post("/extract", json={"text": row["title"]})
                    if resp.status_code != 200:
                        errors += 1
                        continue

                    triples = resp.json().get("triples", [])
                    for triple in triples:
                        subj = triple.get("subject", "").strip()
                        pred_raw = triple.get("predicate", "").strip()
                        obj = triple.get("object", "").strip()

                        if not subj or not obj or len(subj) < 2 or len(obj) < 2:
                            continue

                        pred = map_predicate(pred_raw)
                        if not pred:
                            continue

                        # Store as fact
                        data = json.dumps({
                            "source": "rebel_backfill",
                            "signal_id": str(row["id"]),
                            "rebel_predicate": pred_raw,
                        })

                        await pool.execute("""
                            INSERT INTO facts (id, subject, predicate, value, confidence,
                                             source_type, data, valid_from, created_at)
                            VALUES ($1, $2, $3, $4, 0.7, 'backfill', $5::jsonb, NOW(), NOW())
                            ON CONFLICT (lower(subject), lower(predicate), lower(value),
                                        COALESCE(valid_from, '1970-01-01'::timestamptz))
                            DO NOTHING
                        """, uuid4(), subj, pred, obj, data)

                        facts_created += 1

                    signals_processed += 1

                except Exception as e:
                    errors += 1
                    if errors % 10 == 0:
                        logger.warning(f"Error count: {errors}, last: {e}")

            offset += BATCH_SIZE
            logger.info(
                f"Progress: {min(offset, total)}/{total} signals "
                f"(facts={facts_created}, errors={errors})"
            )

    await pool.close()
    logger.info(f"Done: {signals_processed} signals processed, {facts_created} facts created, {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
