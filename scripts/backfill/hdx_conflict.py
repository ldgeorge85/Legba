#!/usr/bin/env python3
"""Backfill HDX conflict baseline data into TimescaleDB.

Downloads ACLED aggregate CSVs (2018-2025) from HDX and loads monthly
country-level conflict metrics. This gives the agent historical baselines
for anomaly detection via metrics_query.

4 metrics loaded:
  - conflict_events       (political_violence event count)
  - conflict_fatalities   (political_violence fatality count)
  - civilian_targeting    (civilian_targeting event count)
  - demonstrations        (demonstration event count)

Dimension: country:{Name} (e.g. country:Ukraine)

Usage:
    docker compose -p legba exec ingestion python3 /app/scripts/backfill/hdx_conflict.py
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import sys
from datetime import datetime, timezone

import httpx

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

METRICS_DSN = os.getenv(
    "METRICS_DSN",
    "postgresql://legba_metrics:legba_metrics@timescaledb:5432/legba_metrics",
)

HDX_BASE = (
    "https://data.humdata.org/dataset/d57047fe-62f5-458b-9dc6-3dec2e892345"
    "/resource/{rid}/download/hdx_hapi_conflict_event_global_{year}.csv"
)

RESOURCES = {
    2018: "cfc0920a-cdd8-46a7-9895-38676977db59",
    2019: "c7951296-187a-4ace-a013-9efa676fb164",
    2020: "25d5a810-7a83-4dc8-9cb0-af51ed5a6304",
    2021: "97f46e2e-04ba-4c8c-8411-727958e3026b",
    2022: "c54ba3c7-b7ff-4934-89ce-3deaf4a6fdbd",
    2023: "22799375-419f-4259-a667-4120be432196",
    2024: "7f775c16-ed12-466f-9db4-4242e7481935",
    2025: "ba5e6199-6eba-40b8-862e-68f76749f4c9",
}

# Map HDX event_type → our metric names
EVENT_TYPE_MAP = {
    "political_violence": ("conflict_events", "conflict_fatalities"),
    "civilian_targeting": ("civilian_targeting", None),
    "demonstration": ("demonstrations", None),
}

# ISO3 → country name cache
_country_cache: dict[str, str] = {}


def iso3_to_name(code: str) -> str | None:
    """Convert ISO 3166-1 alpha-3 to country name."""
    if code in _country_cache:
        return _country_cache[code]
    try:
        import pycountry
        c = pycountry.countries.get(alpha_3=code)
        if c:
            _country_cache[code] = c.name
            return c.name
    except Exception:
        pass
    return None


async def fetch_csv(client: httpx.AsyncClient, year: int) -> str:
    """Download a single year's CSV from HDX."""
    rid = RESOURCES[year]
    url = HDX_BASE.format(rid=rid, year=year)
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def parse_csv(text: str) -> list[tuple[datetime, str, str, float]]:
    """Parse CSV into (time, metric, dimension, value) tuples.

    Uses admin_level=0 rows where available. For countries that only have
    sub-national data (HRP countries like Ukraine, Syria, etc.), aggregates
    sub-national rows to country level.
    """
    # Strip BOM if present
    if text.startswith("\ufeff"):
        text = text[1:]

    # First pass: find which countries have admin_level=0 data
    rows_list = list(csv.DictReader(io.StringIO(text)))
    countries_with_l0: set[str] = set()
    for row in rows_list:
        if row.get("admin_level") == "0":
            countries_with_l0.add(row["location_code"])

    # Second pass: collect data
    # For L0 countries: use L0 rows directly
    # For non-L0 countries: aggregate sub-national rows
    points = []
    # Accumulator for sub-national aggregation: (iso3, event_type, period) → {events, fatalities}
    subnational: dict[tuple[str, str, str], dict[str, int]] = {}

    for row in rows_list:
        iso3 = row["location_code"]
        event_type = row.get("event_type", "")
        mapping = EVENT_TYPE_MAP.get(event_type)
        if not mapping:
            continue

        events = int(row["events"]) if row.get("events") else 0
        fatalities = int(row["fatalities"]) if row.get("fatalities") else 0
        period_start = row.get("reference_period_start", "")

        if row.get("admin_level") == "0":
            # Direct country-level row
            country = iso3_to_name(iso3)
            if not country:
                continue
            events_metric, fatalities_metric = mapping
            ts = datetime.fromisoformat(period_start).replace(tzinfo=timezone.utc)
            dim = f"country:{country}"
            points.append((ts, events_metric, dim, float(events)))
            if fatalities_metric and fatalities > 0:
                points.append((ts, fatalities_metric, dim, float(fatalities)))
        elif iso3 not in countries_with_l0:
            # Aggregate sub-national data for countries without L0
            key = (iso3, event_type, period_start)
            if key not in subnational:
                subnational[key] = {"events": 0, "fatalities": 0}
            subnational[key]["events"] += events
            subnational[key]["fatalities"] += fatalities

    # Emit aggregated sub-national points
    for (iso3, event_type, period_start), totals in subnational.items():
        country = iso3_to_name(iso3)
        if not country:
            continue
        mapping = EVENT_TYPE_MAP.get(event_type)
        if not mapping:
            continue
        events_metric, fatalities_metric = mapping
        ts = datetime.fromisoformat(period_start).replace(tzinfo=timezone.utc)
        dim = f"country:{country}"
        points.append((ts, events_metric, dim, float(totals["events"])))
        if fatalities_metric and totals["fatalities"] > 0:
            points.append((ts, fatalities_metric, dim, float(totals["fatalities"])))

    return points


async def store_points(points: list[tuple[datetime, str, str, float]]) -> int:
    """Write metric points to TimescaleDB."""
    import asyncpg

    conn = await asyncpg.connect(METRICS_DSN)
    try:
        # Batch insert
        await conn.executemany(
            "INSERT INTO metrics (time, metric, dimension, value) "
            "VALUES ($1, $2, $3, $4)",
            points,
        )
        return len(points)
    finally:
        await conn.close()


async def main():
    total = 0
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for year in sorted(RESOURCES.keys()):
            print(f"  Fetching {year}...", end=" ", flush=True)
            text = await fetch_csv(client, year)
            points = parse_csv(text)
            if points:
                stored = await store_points(points)
                total += stored
                countries = len({p[2] for p in points})
                print(f"{stored} points, {countries} countries")
            else:
                print("no data")

    print(f"\nTotal: {total} metric points loaded")

    # Verify
    import asyncpg
    conn = await asyncpg.connect(METRICS_DSN)
    try:
        for metric in ["conflict_events", "conflict_fatalities",
                        "civilian_targeting", "demonstrations"]:
            row = await conn.fetchrow(
                "SELECT count(*) as n, count(DISTINCT dimension) as countries "
                "FROM metrics WHERE metric = $1",
                metric,
            )
            print(f"  {metric}: {row['n']} rows, {row['countries']} countries")
    finally:
        await conn.close()


if __name__ == "__main__":
    print("HDX Conflict Baseline Backfill")
    print("=" * 40)
    asyncio.run(main())
