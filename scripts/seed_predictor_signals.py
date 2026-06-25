# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-3 bringup helper — seed N days of synthetic India signals so the
registered predictor has substrate in its 24h read-slice window.

Used once at predictor-activation time to validate the end-to-end loop
while the production RSS-pull cadence is still warming up. Idempotent:
re-runs INSERT new rows (different uuids/guids per call); cleanup is a
one-line ``DELETE FROM signals WHERE guid LIKE 'k3-predictor-synth-%';``.

Documented gap: ``_read_substrate_slice`` in dapr_actors hard-codes a
24h window and ignores ``subscription.time_window`` — until that's
generalized, only signals within the last 24h reach the predictor.  We
therefore back-fill with hourly spacing across the last 24h (not 14
calendar days) so AutoARIMA's MIN_OBSERVATIONS=5 daily-bucket gate is
clearable in a single tick.

Run inside the runtime container or from a host with the standard
postgres creds reachable on 127.0.0.1:5432.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg


SCHEMA_URI = "iglu:legba/signal/jsonschema/2-0-0"
GUID_PREFIX = "k3-predictor-synth"


async def _seed(*, target_id: str, days: int, pg_dsn: dict) -> None:
    conn = await asyncpg.connect(**pg_dsn)
    try:
        target_version = await conn.fetchval(
            "SELECT version FROM target_descriptors "
            "WHERE descriptor_id=$1 AND is_head",
            target_id,
        )
        if target_version is None:
            raise RuntimeError(
                f"no head version for target {target_id!r}; "
                "register the target descriptor first"
            )

        now = datetime.now(tz=timezone.utc)
        inserted: list[tuple[str, str]] = []
        for d in range(days):
            # Spread across the trailing N days; clamp to within 23h so the
            # 24h substrate-window picks them all up.
            offset_hours = (d / max(days - 1, 1)) * 23.0
            day = now - timedelta(hours=23.0 - offset_hours)
            new_id = uuid4()
            data = {
                "summary": f"K-3 synthetic predictor seed slot {d}",
                "sentiment": 0.05 * (d - days / 2),
                "descriptor_source_id": "k3_predictor_synth",
            }
            await conn.execute(
                """
                INSERT INTO signals (
                    id, data, title, source_id, source_url, guid, category,
                    event_timestamp, language, confidence,
                    classification_scores,
                    target_id, target_version, analyst_id, analyst_version,
                    produced_at, derived_from, schema_uri, run_id
                ) VALUES (
                    $1, $2::jsonb, $3, NULL, $4, $5, '',
                    NULL, 'en', 0.5, NULL,
                    $6, $7, NULL, NULL,
                    $8, '{}'::uuid[], $9, NULL
                )
                """,
                new_id, json.dumps(data),
                f"K-3 synthetic Brazil signal slot {d}",
                f"https://example.invalid/{GUID_PREFIX}/{d}-{new_id}",
                f"{GUID_PREFIX}-{new_id}",
                target_id, target_version,
                day, SCHEMA_URI,
            )
            inserted.append((str(new_id), day.isoformat()))

        print(
            f"Inserted {len(inserted)} synthetic signals for "
            f"target_id={target_id} target_version={target_version[:12]}"
        )
        for sid, dt in inserted[:3]:
            print(f"  {dt}  {sid}")
        if len(inserted) > 3:
            print(f"  ... ({len(inserted) - 3} more)")
    finally:
        await conn.close()


async def _purge(*, pg_dsn: dict) -> None:
    conn = await asyncpg.connect(**pg_dsn)
    try:
        rows = await conn.execute(
            f"DELETE FROM signals WHERE guid LIKE '{GUID_PREFIX}-%'",
        )
        print(f"DELETE result: {rows}")
    finally:
        await conn.close()


def _pg_dsn() -> dict:
    return {
        "host": os.environ.get("PGHOST", "127.0.0.1"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "legba"),
        "password": os.environ.get("PGPASSWORD", "legba"),
        "database": os.environ.get("PGDATABASE", "legba"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-id", default="india_energy_infra",
        help="target descriptor id whose signals get seeded",
    )
    parser.add_argument(
        "--days", type=int, default=10,
        help="how many synthetic signals to insert "
        "(spaced across the trailing 23h)",
    )
    parser.add_argument(
        "--purge", action="store_true",
        help=f"DELETE all rows where guid LIKE '{GUID_PREFIX}-%' and exit",
    )
    args = parser.parse_args()

    if args.purge:
        asyncio.run(_purge(pg_dsn=_pg_dsn()))
        return 0

    asyncio.run(_seed(
        target_id=args.target_id,
        days=args.days,
        pg_dsn=_pg_dsn(),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
