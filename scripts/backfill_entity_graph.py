#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill the entity knowledge-graph from signals' NER payload.

The source baseline's ``ner_multilingual`` filter already populates
``signals.payload.entities`` (mentions with text/class). Nothing, however,
resolves those mentions into the entity substrate, so ``entity_profiles`` /
``signal_entity_links`` / ``proposed_edges`` sit empty. This stands the graph
up from existing data:

  * ``entity_profiles`` — one node per distinct entity (deduped by the
    composite key ``(lower(canonical_name), entity_class)``, migration 0035);
    location-class entities inherit the signal's geocoded lat/lon/country so
    the entity-geo map has points (only when the country is consistent).
  * ``signal_entity_links`` — provenance edge signal→entity (role=mentioned).
  * ``proposed_edges`` — co-occurrence relationships between entities mentioned
    in the same signal (confidence accrues with repeated co-occurrence).

Idempotent: profiles upsert on the unique name index, links ``ON CONFLICT DO
NOTHING``, edges upsert on a (source,target,type) unique index created here.
Re-run any time to fold in new signals.

Run on the host (same pattern as the other bringup scripts):
  set -a; . ./.env; set +a
  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \
    LEGBA_DATA_PG_USER=legba LEGBA_DATA_PG_PASSWORD=legba \
    python3 scripts/backfill_entity_graph.py
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os

import asyncpg

MAX_ENTITIES_PER_SIGNAL = 8   # cap pairwise co-occurrence edges per signal
MIN_NAME_LEN = 2


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


def _as_dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


async def main() -> None:
    conn = await _connect()
    # Edge dedup index for the upsert.
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_proposed_edges_triple "
        "ON proposed_edges (lower(source_entity), lower(target_entity), relationship_type)"
    )

    rows = await conn.fetch(
        "SELECT id, payload FROM signals WHERE payload ? 'entities'"
    )
    print(f"signals with NER entities: {len(rows)}")

    # Keyed by the COMPOSITE identity (lower(name), class) to match the
    # entity_profiles composite key (migration 0035) — a bare name would
    # re-merge Georgia/country with Georgia/location.
    name_to_id: dict[tuple[str, str], str] = {}
    n_links = 0
    n_edges = 0

    for r in rows:
        payload = _as_dict(r["payload"])
        ents = payload.get("entities") or []
        geo = payload.get("geo") if isinstance(payload.get("geo"), dict) else {}
        title = str(payload.get("title") or "")[:200]

        # Dedup the signal's mentions by the COMPOSITE identity (lowercased
        # name, class) — mirroring the entity_profiles composite key (0035).
        seen: dict[tuple[str, str], tuple[str, str]] = {}
        for e in ents:
            if not isinstance(e, dict):
                continue
            text = str(e.get("text") or "").strip()
            cls = (str(e.get("class") or "entity").strip() or "entity")
            if len(text) < MIN_NAME_LEN:
                continue
            seen.setdefault((text.lower(), cls), (text, cls))

        signal_names: list[str] = []
        for key, (text, cls) in seen.items():
            eid = name_to_id.get(key)
            if eid is None:
                lat = lon = None
                country = None
                if cls == "location" and geo:
                    lat = geo.get("lat")
                    lon = geo.get("lon")
                    country = geo.get("country")
                row = await conn.fetchrow(
                    """
                    INSERT INTO entity_profiles
                        (canonical_name, entity_type, entity_class, data,
                         geo_lat, geo_lon, geo_country, completeness_score,
                         last_event_link_at)
                    VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8, now())
                    ON CONFLICT (lower(canonical_name), entity_class) DO UPDATE
                        SET last_event_link_at = now(),
                            -- Geo inherited only when countries agree (fill a
                            -- NULL, or refine within the same country); a
                            -- disagreeing incoming country is never inherited
                            -- (the cross-country bleed that geocoded
                            -- country-Georgia to Azerbaijan).
                            geo_lat = CASE
                                WHEN entity_profiles.geo_country IS NULL
                                  OR EXCLUDED.geo_country IS NULL
                                  OR lower(entity_profiles.geo_country)
                                     = lower(EXCLUDED.geo_country)
                                THEN COALESCE(entity_profiles.geo_lat, EXCLUDED.geo_lat)
                                ELSE entity_profiles.geo_lat
                            END,
                            geo_lon = CASE
                                WHEN entity_profiles.geo_country IS NULL
                                  OR EXCLUDED.geo_country IS NULL
                                  OR lower(entity_profiles.geo_country)
                                     = lower(EXCLUDED.geo_country)
                                THEN COALESCE(entity_profiles.geo_lon, EXCLUDED.geo_lon)
                                ELSE entity_profiles.geo_lon
                            END,
                            geo_country = COALESCE(entity_profiles.geo_country, EXCLUDED.geo_country)
                    RETURNING id
                    """,
                    text, cls, cls, json.dumps({"source": "ner_backfill"}),
                    lat, lon, country, 0.3,
                )
                eid = str(row["id"])
                name_to_id[key] = eid
            await conn.execute(
                "INSERT INTO signal_entity_links (signal_id, entity_id, role, confidence) "
                "VALUES ($1,$2,'mentioned',0.8) ON CONFLICT DO NOTHING",
                r["id"], eid,
            )
            n_links += 1
            signal_names.append(text)

        # Co-occurrence edges — pairwise among the signal's (capped) entities.
        names = sorted(set(signal_names))[:MAX_ENTITIES_PER_SIGNAL]
        for a, b in itertools.combinations(names, 2):
            await conn.execute(
                """
                INSERT INTO proposed_edges
                    (source_entity, target_entity, relationship_type, confidence,
                     evidence_text, status)
                VALUES ($1,$2,'co_occurs',0.4,$3,'pending')
                ON CONFLICT (lower(source_entity), lower(target_entity), relationship_type)
                DO UPDATE SET confidence = LEAST(1.0, proposed_edges.confidence + 0.05)
                """,
                a, b, title,
            )
            n_edges += 1

    prof = await conn.fetchval("SELECT count(*) FROM entity_profiles")
    links = await conn.fetchval("SELECT count(*) FROM signal_entity_links")
    edges = await conn.fetchval("SELECT count(*) FROM proposed_edges")
    print(f"DONE — entity_profiles={prof} signal_entity_links={links} proposed_edges={edges} "
          f"(distinct entities this run={len(name_to_id)}, link upserts={n_links}, edge upserts={n_edges})")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
