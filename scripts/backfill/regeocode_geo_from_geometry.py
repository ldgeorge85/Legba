# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ-C1 backfill — re-geocode poisoned signal geo from authoritative geometry.

The geometry-first geocode fix (DQ-C1, commit 96be457) corrected NEW signals,
but the historical NWS rows remain text-geocoded to the wrong country
(France|15982, Armenia|11855 via a constant bogus Nominatim coord) and NASA
EONET rows carry 0% geo despite shipping exact point coordinates. Both still
poison the map + every geo slice.

This re-runs the SAME live resolver (``representative_point`` +
``country_iso2_for_point`` → ``_geometry_result.to_payload``) over each
targeted row's ``payload.geojson.geometry`` and rewrites ``signals.geo`` (ISO2
array) + ``payload.geo`` to match exactly what a fresh signal would now get.

Idempotent (recomputes the same correct values) + batched + transactional per
batch. Dry-run by default — pass ``--apply`` to write.

Usage (inside the runtime container, which has the resolver + live DB env):
    python scripts/backfill/regeocode_geo_from_geometry.py            # dry run
    python scripts/backfill/regeocode_geo_from_geometry.py --apply    # write
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg

from legba.data.filters._country_geometry import (
    _point_from_geometry,
    country_iso2_for_point,
)
from legba.data.filters.geocode import _geometry_result

# Sources whose geo the sweep found poisoned/missing AND that ship authoritative
# geometry. USGS/GDACS geocode correctly from their titles (country names), so
# they are deliberately NOT touched.
TARGET_SOURCES = ("source.nws.active_alerts", "source.nasa.eonet_events")
BATCH = 1000

# Sources that are DEFINITIONALLY single-country: every item is in this country
# regardless of geometry. The US National Weather Service only issues alerts for
# US (+ territories). So a zone-based NWS alert with no polygon geometry — which
# the geometry path can't fix and which is currently mis-attributed to
# France/Armenia by the bogus text-geocode — is still confidently US. NASA EONET
# is global, so it has NO default (geometry-only; unresolved rows are skipped).
_SOURCE_DEFAULT_ISO2 = {"source.nws.active_alerts": "US"}


def _authoritative_point(payload: dict) -> tuple[float, float] | None:
    """(lat, lon) from the AUTHORITATIVE structured geometry ONLY.

    Deliberately does NOT use ``representative_point``'s ``payload.geo.lat/lon``
    fallback — that field IS the poison (the constant bogus Nominatim coord that
    mis-attributed every NWS row to France/Armenia). A backfill must trust only
    the feed's own geometry; rows with no parseable geometry are skipped, never
    re-derived from the corrupt geo.lat/lon."""
    for path in (("geojson", "geometry"), ("geometry",), ("geo", "geometry")):
        node: object = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        pt = _point_from_geometry(node)
        if pt is not None:
            return pt
    return None


def _country_geo_payload(iso2: str) -> dict:
    """A country-precision payload.geo dict for a source-default attribution
    (no coordinates). Mirrors the geometry path's shape, source='source_default'."""
    out = {"source": "source_default", "precision": "country", "country_iso2": iso2}
    try:
        import pycountry
        c = pycountry.countries.get(alpha_2=iso2)
        if c is not None:
            out["country"] = getattr(c, "common_name", None) or c.name
            out["country_iso3"] = c.alpha_3
    except Exception:
        pass
    return out


def _corrected_geo(payload: dict, source_id: str) -> tuple[list[str], dict] | None:
    """Return (geo_iso2_array, payload_geo_dict) for the row, or None when it
    can't be confidently re-attributed (leave it untouched).

    Authoritative GEOMETRY first; if absent, fall back to a DEFINITIONAL
    per-source default (NWS ⇒ US) — never the corrupt payload.geo.lat/lon."""
    if not isinstance(payload, dict):
        return None
    pt = _authoritative_point(payload)
    if pt:
        lat, lon = pt
        iso2 = country_iso2_for_point(lat, lon)
        if iso2:
            return [iso2], _geometry_result(iso2, lat=lat, lon=lon).to_payload("country")
    default = _SOURCE_DEFAULT_ISO2.get(source_id)
    if default:
        return [default], _country_geo_payload(default)
    return None


async def main(apply: bool) -> int:
    dsn = (
        f"postgresql://{os.environ.get('LEGBA_DATA_PG_USER', 'legba')}:"
        f"{os.environ.get('LEGBA_DATA_PG_PASSWORD', 'legba')}@"
        f"{os.environ.get('LEGBA_DATA_PG_HOST', 'postgres')}:"
        f"{os.environ.get('LEGBA_DATA_PG_PORT', '5432')}/"
        f"{os.environ.get('LEGBA_DATA_PG_DB', 'legba')}"
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    scanned = fixed = skipped = unchanged = 0
    by_country: dict[str, int] = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source_id, payload FROM signals WHERE source_id = ANY($1::text[])",
            list(TARGET_SOURCES),
        )
        updates: list[tuple] = []
        for r in rows:
            scanned += 1
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    skipped += 1
                    continue
            corrected = _corrected_geo(payload, r["source_id"])
            if corrected is None:
                skipped += 1
                continue
            geo_arr, geo_payload = corrected
            cur_geo = payload.get("geo") or {}
            # Already correct (right iso2 from a non-poison source) → skip rewrite.
            if cur_geo.get("country_iso2") == geo_arr[0] and cur_geo.get("source") in (
                "geometry", "source_default",
            ):
                unchanged += 1
                continue
            by_country[geo_arr[0]] = by_country.get(geo_arr[0], 0) + 1
            fixed += 1
            updates.append((r["id"], geo_arr, json.dumps(geo_payload)))

        if apply and updates:
            for i in range(0, len(updates), BATCH):
                chunk = updates[i:i + BATCH]
                async with conn.transaction():
                    await conn.executemany(
                        """
                        UPDATE signals
                           SET geo = $2::text[],
                               payload = jsonb_set(payload, '{geo}', $3::jsonb, true)
                         WHERE id = $1
                        """,
                        chunk,
                    )
    await pool.close()
    print(f"scanned={scanned} would_fix={fixed} unchanged={unchanged} skipped={skipped}")
    print("by_country:", dict(sorted(by_country.items(), key=lambda x: -x[1])))
    print("APPLIED" if apply else "DRY RUN — pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(apply="--apply" in sys.argv[1:])))
