#!/usr/bin/env python3
"""Reclassify signals with category='other' using the GPU models service.

Reads signals from Postgres where category='other', sends each title
to the /classify endpoint, and updates the category if confidence > 0.5.

Usage:
    docker compose -p legba run --rm -v $(pwd)/scripts:/scripts \
      -e DATABASE_URL=postgresql://legba:legba@postgres:5432/legba \
      -e MODELS_API_URL=https://models.ai1.infra.innoscale.net \
      -e MODELS_API_USER=legba \
      -e MODELS_API_PASS=$MODELS_API_PASS \
      ingestion python3 /scripts/backfill/reclassify_other.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

DB_DSN = os.getenv("DATABASE_URL", "postgresql://legba:legba@postgres:5432/legba")
MODELS_URL = os.getenv("MODELS_API_URL", "").rstrip("/")
MODELS_USER = os.getenv("MODELS_API_USER", "")
MODELS_PASS = os.getenv("MODELS_API_PASS", "")
BATCH_SIZE = 100
MIN_CONFIDENCE = 0.5


async def main():
    if not MODELS_URL:
        print("ERROR: MODELS_API_URL not set", file=sys.stderr)
        sys.exit(1)

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    # Count
    total = await pool.fetchval("SELECT count(*) FROM signals WHERE category = 'other'")
    logger.info(f"Signals to reclassify: {total}")

    if total == 0:
        logger.info("Nothing to do")
        return

    auth = httpx.BasicAuth(MODELS_USER, MODELS_PASS) if MODELS_USER else None
    async with httpx.AsyncClient(base_url=MODELS_URL, auth=auth, timeout=30) as http:
        # Health check
        resp = await http.get("/health")
        if resp.status_code != 200:
            print(f"ERROR: Models service unhealthy: {resp.status_code}", file=sys.stderr)
            sys.exit(1)
        logger.info("Models service healthy")

        reclassified = 0
        skipped = 0
        errors = 0
        offset = 0

        while offset < total:
            rows = await pool.fetch(
                "SELECT id, title FROM signals WHERE category = 'other' "
                "ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                BATCH_SIZE, offset,
            )
            if not rows:
                break

            for row in rows:
                title = row["title"]
                if not title or len(title) < 10:
                    skipped += 1
                    continue

                try:
                    resp = await http.post("/classify", json={"text": title[:500]})
                    if resp.status_code == 200:
                        data = resp.json()
                        category = data["category"]
                        confidence = data["confidence"]

                        if category != "other" and confidence >= MIN_CONFIDENCE:
                            await pool.execute(
                                "UPDATE signals SET category = $1 WHERE id = $2",
                                category, row["id"],
                            )
                            reclassified += 1
                        else:
                            skipped += 1
                    else:
                        errors += 1
                except Exception as e:
                    errors += 1
                    if errors % 10 == 0:
                        logger.warning(f"Error count: {errors}, last: {e}")

            offset += BATCH_SIZE
            processed = offset if offset < total else total
            logger.info(
                f"Progress: {processed}/{total} "
                f"(reclassified={reclassified}, skipped={skipped}, errors={errors})"
            )

    await pool.close()
    logger.info(f"Done: {reclassified} reclassified, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
