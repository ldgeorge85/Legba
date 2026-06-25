# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.seed._driver — the SeedDriver (writes seed payloads → substrate).

For each enabled :class:`~legba.data.seed._base.SeedSource` the driver:

  1. ``fetch`` → ``map`` the adapter into typed :data:`SeedPayload` objects.
  2. RESOLVES every entity endpoint (fact subject/object, nexus endpoints,
     explicit :class:`SeedEntity`) against ``entity_profiles`` by canonical
     name + class — reusing the exact
     ``ON CONFLICT (lower(canonical_name), entity_class)`` upsert (migration
     0035) the ongoing ``entity_resolution`` sub-handler uses, so NO duplicate
     entities are ever spawned and names shared across classes never merge.
  3. WRITES each fact via :func:`write_fact` and each nexus via
     :func:`write_nexus`, stamped with the adapter's ``source_type`` + the
     batch's ``seed_batch_id``. Idempotency rides the existing open-only
     temporal-triple uniqueness (re-import = upsert no-op); a per-record
     failure is logged + skipped (degrade-not-drop), never aborts the batch.
  4. RECORDS the ``seed_batches`` row (counts + manifest).

The driver creates the ``seed_batches`` row FIRST (so the FK stamp on each
fact/nexus is valid), runs the writes, then UPDATEs the row's counts. In
``dry_run`` mode nothing is written: the adapter is fetched + mapped and the
counts are returned without a batch row or any fact/nexus/entity write.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from ..analysts.deterministic_handlers._entity_geo import resolve_entity_geo_offline
from ..provenance import AnalystContext, FactPayload, NexusPayload, write_fact, write_nexus
from ._base import SeedContext, SeedEntity, SeedFact, SeedNexus, SeedSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class SeedRunResult:
    source: str
    source_type: str
    dry_run: bool
    seed_batch_id: UUID | None
    counts: dict[str, int] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_type": self.source_type,
            "dry_run": self.dry_run,
            "seed_batch_id": str(self.seed_batch_id) if self.seed_batch_id else None,
            "counts": dict(self.counts),
            "manifest": dict(self.manifest),
            "errors": list(self.errors),
        }


# A synthetic schema_uri family for the seed-write provenance context. Must
# match the iglu family grammar ([a-z_]+) in provenance._core.
_SEED_SCHEMA_URI = "iglu:legba/fact/jsonschema/2-0-0"


def _content_hash(source: str, kind: str, payloads: list[Any]) -> str:
    """A STABLE fingerprint of the (source, kind, yielded payloads) for a run.

    Used to dedupe the ``seed_batches`` ledger row on re-run: two runs of the
    same source over identical input (same leaders/alliances YAML → identical
    typed payloads) hash the same, so the second run UPDATES the prior batch row
    instead of inserting a duplicate that overstates seeded volume. The hash
    deliberately excludes volatile fields (``imported_at``) — those vary every
    run and would defeat the dedupe. ``SeedEntity``/``SeedFact``/``SeedNexus``
    are frozen dataclasses, so ``asdict`` over a sorted projection is
    deterministic across runs (and datetimes serialize stably via isoformat).
    """
    items = sorted(
        json.dumps(asdict(p), sort_keys=True, default=str) for p in payloads
    )
    digest = hashlib.sha256()
    digest.update(source.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(kind.encode("utf-8"))
    digest.update(b"\x00")
    for item in items:
        digest.update(item.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _seed_ctx(source: str) -> AnalystContext:
    """A provenance context for seed writes.

    Seed rows are NOT analyst-produced; we stamp a synthetic, stable
    ``analyst_id`` (``seed.<source>``) with no target so the row's provenance
    columns clearly read "produced by the seed import", and the row's
    ``source_type='seed'`` + ``seed_batch_id`` carry the real classification.
    """
    return AnalystContext(
        analyst_id=f"seed.{source}",
        analyst_version="seed",
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )


# ---------------------------------------------------------------------------
# Entity resolution (reuses the entity_resolution ON CONFLICT upsert)
# ---------------------------------------------------------------------------


async def _resolve_entity(
    conn: Any,
    *,
    canonical_name: str,
    entity_class: str = "entity",
    geo_lat: float | None = None,
    geo_lon: float | None = None,
    geo_country: str | None = None,
    data: dict[str, Any] | None = None,
) -> UUID:
    """Resolve (dedupe-or-create) an entity by canonical name + class.

    Identical ``ON CONFLICT (lower(canonical_name), entity_class)`` upsert to
    the ``entity_resolution`` sub-handler (migration 0035) — so a seeded entity
    and a live mention of the SAME name AND class fold to ONE
    ``entity_profiles`` row (never a duplicate), while a name shared across
    classes stays two distinct rows (never a false merge). Geo is inherited on
    conflict only when the countries are consistent. Returns the resolved
    entity id.

    Entity-geo (graph-and-data Wave-1b, item 1): for a country-NAMED entity,
    an incoming geo whose country disagrees with the name is reconciled to the
    name (the source coords belong to a different place) — the same Evian→India
    defence as the live path, but CONSERVATIVE for seeds: a seed's geo is
    authoritative input, so an unverifiable town geo (a real "Evian, France"
    coordinate) is KEPT, not wiped. We only override on a provable
    name-vs-country contradiction.
    """
    cls_lower = (entity_class or "").strip().lower()
    if cls_lower in ("location", "country") and (geo_country or geo_lat is not None):
        reconciled = resolve_entity_geo_offline(
            name=canonical_name,
            entity_class=entity_class,
            signal_geo={"lat": geo_lat, "lon": geo_lon, "country": geo_country},
        )
        # Only act when the NAME is itself a country (reconciled carries a
        # country): a contradiction drops the bad coords. A non-country place
        # name yields an empty reconciliation here, which we IGNORE so a
        # legitimately-seeded town keeps its provided coordinates.
        if reconciled.country is not None:
            geo_lat, geo_lon, geo_country = (
                reconciled.lat,
                reconciled.lon,
                reconciled.country,
            )
    payload = {"source": "seed", **(data or {})}
    row = await conn.fetchrow(
        """
        INSERT INTO entity_profiles
            (canonical_name, entity_type, entity_class, data,
             geo_lat, geo_lon, geo_country, completeness_score,
             last_event_link_at)
        VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8, now())
        ON CONFLICT (lower(canonical_name), entity_class) DO UPDATE
            SET last_event_link_at = now(),
                -- Geo inherited only when countries are consistent (fill a
                -- NULL, or refine within the same country); a disagreeing
                -- incoming country is never inherited (the cross-country bleed
                -- that geocoded country-Georgia to Azerbaijan).
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
        canonical_name,
        entity_class,
        entity_class,
        json.dumps(payload),
        geo_lat,
        geo_lon,
        geo_country,
        0.3,
    )
    return row["id"]


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


async def run_seed_source(
    pool: Any,
    source: SeedSource,
    *,
    dry_run: bool = False,
    options: dict[str, Any] | None = None,
) -> SeedRunResult:
    """Fetch → map → resolve → write one seed adapter; record the batch.

    ``pool`` is an asyncpg pool (``pool.acquire()``). On ``dry_run`` the
    adapter is fetched + mapped and the counts returned WITHOUT writing
    anything (no batch row, no fact/nexus/entity).
    """
    opts = dict(options or {})
    result = SeedRunResult(
        source=source.name,
        source_type=source.source_type,
        dry_run=dry_run,
        seed_batch_id=None,
        counts={"facts": 0, "nexuses": 0, "entities": 0, "skipped": 0},
    )

    ctx = SeedContext(pool=pool, dry_run=dry_run, options=opts)
    raw = await source.fetch(ctx)
    payloads = list(source.map(raw))

    entities = [p for p in payloads if isinstance(p, SeedEntity)]
    facts = [p for p in payloads if isinstance(p, SeedFact)]
    nexuses = [p for p in payloads if isinstance(p, SeedNexus)]

    kind = opts.get("kind", source.name)
    content_hash = _content_hash(source.name, kind, payloads)

    result.manifest = {
        "adapter": source.name,
        "yielded": {
            "entities": len(entities),
            "facts": len(facts),
            "nexuses": len(nexuses),
        },
        # Stable fingerprint of the yielded payloads (excludes imported_at) —
        # the natural key the ledger dedupes on so a re-run UPDATEs the prior
        # batch row instead of inserting a duplicate that overstates volume.
        "content_hash": content_hash,
        "imported_at": datetime.now(tz=timezone.utc).isoformat(),
        "dry_run": dry_run,
        **{k: v for k, v in opts.items() if k.startswith("manifest_")},
    }

    if dry_run:
        # Map-only: report what WOULD be written; touch nothing.
        result.counts["facts"] = len(facts)
        result.counts["nexuses"] = len(nexuses)
        result.counts["entities"] = len(entities)
        return result

    actx = _seed_ctx(source.name)

    async with pool.acquire() as conn:
        # 1) Establish the batch row first so the FK stamp on every fact/nexus
        #    is valid. counts are filled in at the end.
        #
        #    Idempotency (P3-3): the row-level fact/nexus writes are ALREADY
        #    idempotent (open-triple upsert no-op), but an UNCONDITIONAL INSERT
        #    here minted a fresh ledger row every run — so N re-runs of the same
        #    source over identical input left N rows each claiming the full
        #    seeded volume, overstating the ledger N-fold. Dedupe on the natural
        #    key (source, kind, manifest content_hash): a re-run with an
        #    identical payload set UPDATES the prior batch row in place (refresh
        #    manifest + imported_at), reusing its id, so the ledger carries
        #    exactly ONE row per distinct seed set.
        existing_id = await conn.fetchval(
            """
            SELECT id FROM seed_batches
             WHERE source = $1 AND kind = $2
               AND manifest->>'content_hash' = $3
             ORDER BY imported_at DESC
             LIMIT 1
            """,
            source.name,
            kind,
            content_hash,
        )
        if existing_id is not None:
            batch_id = await conn.fetchval(
                """
                UPDATE seed_batches
                   SET source_type = $2,
                       manifest = $3::jsonb,
                       imported_at = now()
                 WHERE id = $1
                RETURNING id
                """,
                existing_id,
                source.source_type,
                json.dumps(result.manifest),
            )
        else:
            batch_id = await conn.fetchval(
                """
                INSERT INTO seed_batches (source, kind, source_type, manifest)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING id
                """,
                source.name,
                kind,
                source.source_type,
                json.dumps(result.manifest),
            )
        result.seed_batch_id = batch_id

        # 2) Explicit entity enrichment (best-effort).
        seen_entities: set[str] = set()
        for ent in entities:
            try:
                await _resolve_entity(
                    conn,
                    canonical_name=ent.canonical_name,
                    entity_class=ent.entity_class,
                    geo_lat=ent.geo_lat,
                    geo_lon=ent.geo_lon,
                    geo_country=ent.geo_country,
                    data=ent.data,
                )
                seen_entities.add(ent.canonical_name.lower())
                result.counts["entities"] += 1
            except Exception as exc:  # degrade-not-drop
                msg = f"entity {ent.canonical_name!r}: {exc}"
                logger.warning("seed.%s entity resolve failed: %s", source.name, msg)
                result.errors.append(msg)

        # 3) Facts — resolve subject + object endpoints, then write.
        for f in facts:
            try:
                for name in (f.subject, f.value):
                    if name.lower() not in seen_entities:
                        await _resolve_entity(conn, canonical_name=name)
                        seen_entities.add(name.lower())
                out, dlq = await write_fact(
                    conn,
                    analyst_ctx=actx,
                    payload=FactPayload(
                        subject=f.subject,
                        predicate=f.predicate,
                        value=f.value,
                        confidence=f.confidence,
                        source_type=source.source_type,
                        valid_from=f.valid_from,
                        valid_until=f.valid_until,
                        geo_lat=f.geo_lat,
                        geo_lon=f.geo_lon,
                        data=f.data,
                    ),
                    derived_from=[],
                    source_type=source.source_type,
                    seed_batch_id=batch_id,
                )
                if dlq is not None or out is None:
                    result.counts["skipped"] += 1
                    result.errors.append(
                        f"fact ({f.subject}|{f.predicate}|{f.value}) → DLQ"
                    )
                else:
                    result.counts["facts"] += 1
            except Exception as exc:  # degrade-not-drop
                result.counts["skipped"] += 1
                msg = f"fact ({f.subject}|{f.predicate}|{f.value}): {exc}"
                logger.warning("seed.%s fact write failed: %s", source.name, msg)
                result.errors.append(msg)

        # 4) Nexuses — resolve both endpoints, then write (typed + signed).
        for n in nexuses:
            try:
                for name in (n.subject, n.object, n.intermediary):
                    if name and name.lower() not in seen_entities:
                        await _resolve_entity(conn, canonical_name=name)
                        seen_entities.add(name.lower())
                out, dlq = await write_nexus(
                    conn,
                    analyst_ctx=actx,
                    payload=NexusPayload(
                        subject=n.subject,
                        intermediary=n.intermediary,
                        object=n.object,
                        rel_type=n.rel_type,
                        label=n.label,
                        polarity=n.polarity,
                        intent=n.intent,
                        channel=n.channel,
                        confidence=n.confidence,
                        valid_from=n.valid_from,
                        valid_until=n.valid_until,
                        data=n.data,
                    ),
                    derived_from=[],
                    source_type=source.source_type,
                    seed_batch_id=batch_id,
                )
                if dlq is not None or out is None:
                    result.counts["skipped"] += 1
                    result.errors.append(
                        f"nexus ({n.subject}|{n.rel_type}|{n.object}) → DLQ"
                    )
                else:
                    result.counts["nexuses"] += 1
            except Exception as exc:  # degrade-not-drop
                result.counts["skipped"] += 1
                msg = f"nexus ({n.subject}|{n.rel_type}|{n.object}): {exc}"
                logger.warning("seed.%s nexus write failed: %s", source.name, msg)
                result.errors.append(msg)

        # 5) Finalize the batch row's counts.
        await conn.execute(
            "UPDATE seed_batches SET counts = $2::jsonb WHERE id = $1",
            batch_id,
            json.dumps(result.counts),
        )

    return result


__all__ = ["SeedRunResult", "run_seed_source"]
