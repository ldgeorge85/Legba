# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``entity_resolution`` sub-handler — ongoing NER-mention → entity-graph fold.

The source baseline's ``ner_multilingual`` filter writes mentions into
``signals.payload.entities`` (each ``{text, class}``), but on its own nothing
resolves those mentions into the entity substrate. ``scripts/backfill_entity_graph.py``
did it once, as a one-shot. This sub-handler makes it **continuous**: every time
the bound ``deterministic`` analyst fires (cadence or coalesced), it folds the
next batch of un-resolved signals into the graph, so new signals auto-link.

Per processed signal (mirrors the backfill's logic exactly, so the two are
interchangeable / re-runnable against each other):

  * ``entity_profiles`` — one node per distinct mention, deduped by the
    COMPOSITE key ``(lower(canonical_name), entity_class)`` (migration 0035) so
    a name shared across classes (Georgia/country vs Georgia/location) resolves
    to TWO rows, never a false merge. The geo of a ``location``/``country``
    entity is resolved by its OWN NAME (``_entity_geo.resolve_entity_geo`` —
    an injected geocoder when available, else an offline pycountry name check),
    NOT by inheriting the mentioning signal's geocode. Signal-geo is demoted to
    a consistency-checked fallback: it is attached only when the entity name is
    itself a country and the signal agrees on that country. This is the fix for
    the Evian→India bleed (a town's geo was its first signal's country, then the
    composite key LOCKED it). The ON-CONFLICT geo guard below still refuses a
    cross-country update so a wrong value can never overwrite a right one.
  * ``signal_entity_links`` — provenance edge signal→entity (role=mentioned),
    ``ON CONFLICT DO NOTHING``.
  * ``proposed_edges`` — pairwise co-occurrence (``co_occurs``) among the
    signal's (capped) mentions; confidence accrues on repeat co-occurrence via
    the ``uq_proposed_edges_triple`` upsert (migration 0029).

Forward-progress + idempotency: the sweep selects ``entities_resolved_at IS
NULL`` signals (migration 0029), oldest-first, ``LIMIT batch_limit``, and stamps
``entities_resolved_at = NOW()`` on each after processing — so a signal whose
mentions all fall below ``MIN_NAME_LEN`` is marked (never reprocessed forever)
and a busy backlog of zero-entity signals can't starve newly-arriving ones. Re-
running is safe (upserts + ``ON CONFLICT``); signals the one-shot backfill
already linked are re-folded once (no-op writes) then stamped.

Output ``data`` keys:
    signals_processed   int — signals folded this run
    entities_upserted   int — distinct entity profiles touched this run
    links_created       int — signal→entity link upserts attempted
    edges_upserted      int — co-occurrence edge upserts attempted
"""

from __future__ import annotations

import itertools
import json
import logging
import uuid
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
# W2 shared canon spine — prefer the new shared module path (the old
# deterministic_handlers/_entity_canon is now a re-export shim). canonicalize_entity
# normalizes a span; is_junk_entity drops true junk; is_org_surface types orgs.
from ..._entity_canon import (
    canonicalize_entity,
    is_junk_entity,
    is_org_surface,
)
from ._entity_geo import NameGeocoder, resolve_entity_geo

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "entity_resolution"

_DEFAULT_BATCH = 500
MAX_ENTITIES_PER_SIGNAL = 8   # cap pairwise co-occurrence edges per signal
MIN_NAME_LEN = 2
# Co-mention snippet window stored in proposed_edges.evidence_text. The
# reifier truncates evidence_text to ~1200 chars, so we cap the prose window
# well under that and leave room for the co-mentioned-entity list — this is
# what lets the proxy-chain candidate path (#99) identify a real cut-out C
# instead of hallucinating one from a bare title.
MAX_SNIPPET_LEN = 600


def _as_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


# Stable namespace for content-addressing an ORIGINAL surface form to a marker
# UUID stored in entity_profiles.derived_from (a uuid[] column). The same
# surface form always maps to the same marker, so appends are idempotent and a
# re-run never re-grows the array. The human-readable forms also live in
# data.merged_aliases for legibility — the marker is only the dedup key.
_ALIAS_NAMESPACE = uuid.UUID("6f6c6c61-0000-0000-0000-6c65676261ab")


def _alias_marker(surface_form: str) -> uuid.UUID:
    """Deterministic v5 UUID for an original surface form (derived_from marker)."""
    return uuid.uuid5(_ALIAS_NAMESPACE, surface_form)


# ---------------------------------------------------------------------------
# D8 — CLASS-AGNOSTIC IDENTITY (forward de-fragmentation, NO migration).
#
# The live audit found 611 names fragmented across classes ("turkey" lived as
# country / entity / location / person simultaneously, "Bank of England" typed
# person, etc.). The composite key (lower(name), entity_class) then LOCKED each
# fragment as a distinct node. The forward fix: resolve a name to ONE entity
# class deterministically at write time so every mention of the same surface
# form converges onto the SAME (name, class) row going forward.
#
# Priority (highest wins): country > organization > person > location > entity.
#  * country  — canonicalize_entity already forces the gazetteer/alias country
#    class; we honour it first so "Turkey" (the nation) never loses to a stray
#    "turkey"/entity NER guess.
#  * organization — is_org_surface (the W1 org-suffix/head gazetteer) types
#    corporate/institutional surfaces ("Bank of England", "Nippon Steel",
#    "Hyundai Motor Group") as organization, NEVER person.
#  * person / location / entity — fall back to the (canonicalized) NER class,
#    floored to the generic "entity" bucket for anything outside the taxonomy.
# ---------------------------------------------------------------------------

#: Deterministic priority order — index 0 is highest. Two competing class
#: signals for the same name resolve to the EARLIER member.
_CLASS_PRIORITY: tuple[str, ...] = (
    "country",
    "organization",
    "person",
    "location",
    "entity",
)
_CLASS_RANK: dict[str, int] = {c: i for i, c in enumerate(_CLASS_PRIORITY)}


def resolve_entity_class(name: str, canonical_class: str) -> str:
    """Resolve ONE deterministic entity_class for a canonicalized name (D8).

    ``canonical_class`` is the class :func:`canonicalize_entity` already settled
    (it forces ``country`` for a gazetteer/alias hit and ``organization`` for an
    NWS-office surface). This adds the org-SURFACE gazetteer
    (:func:`is_org_surface` — "Bank of England" / "Nippon Steel" / "Hyundai
    Motor Group") so a corporate/institutional name typed ``person`` by NER is
    promoted to ``organization``, then collapses any remaining competing signal
    to the single highest-priority class. Pure + deterministic — same name ⇒
    same class, every time, so "turkey" lands in ONE class going forward.
    """
    cls = (str(canonical_class or "entity").strip() or "entity")
    candidates = [cls if cls in _CLASS_RANK else "entity"]
    # Org-surface gazetteer: a corporate/institutional surface is an
    # organization regardless of the NER guess (the canon already handled NWS).
    if is_org_surface(name):
        candidates.append("organization")
    # Lowest rank index (highest priority) wins.
    return min(candidates, key=lambda c: _CLASS_RANK.get(c, len(_CLASS_PRIORITY)))


# ---------------------------------------------------------------------------
# D26 — COMPUTED completeness_score (was a flat 0.3 constant).
#
# The audit flagged entity_profiles.completeness_score as an inert constant 0.3
# on every row. Compute it instead from how many identifying fields the profile
# actually carries, so a richly-resolved entity (name + class + geo) scores
# higher than a bare name. Bounded to [0, 1]; deterministic.
# ---------------------------------------------------------------------------

#: Field-presence weights. canonical_name + a non-generic class are the
#: identity floor; geo (country, then lat/lon) and merge provenance (aliases)
#: add corroboration. Weights sum to 1.0 at full completeness.
_COMPLETENESS_WEIGHTS: dict[str, float] = {
    "name": 0.30,        # has a canonical name (always true past MIN_NAME_LEN)
    "class": 0.20,       # typed to a non-generic class (not the "entity" bucket)
    "geo_country": 0.20,
    "geo_latlon": 0.20,
    "aliases": 0.10,     # at least one folded surface-form alias (merge evidence)
}


def compute_completeness(
    *,
    name: str,
    entity_class: str,
    geo_country: str | None,
    geo_lat: float | None,
    geo_lon: float | None,
    alias_count: int,
) -> float:
    """Completeness in [0, 1] from filled fields (D26 — replaces the 0.3 const).

    Pure + deterministic. A bare name+class entity floors at 0.50; geo + merge
    provenance lift it toward 1.0. ``entity_class == "entity"`` (the generic
    fallback bucket) does NOT count toward the class weight — only a resolved,
    non-generic class does.
    """
    score = 0.0
    if name and name.strip():
        score += _COMPLETENESS_WEIGHTS["name"]
    if entity_class and entity_class.strip() and entity_class.strip().lower() != "entity":
        score += _COMPLETENESS_WEIGHTS["class"]
    if geo_country and str(geo_country).strip():
        score += _COMPLETENESS_WEIGHTS["geo_country"]
    if geo_lat is not None and geo_lon is not None:
        score += _COMPLETENESS_WEIGHTS["geo_latlon"]
    if alias_count > 0:
        score += _COMPLETENESS_WEIGHTS["aliases"]
    return max(0.0, min(1.0, round(score, 4)))


async def _record_provenance(
    conn: Any,
    *,
    entity_id: str,
    version: int,
    created: bool,
    aliases: set[str],
    run_id: Any | None,
    analyst_id: str | None,
    analyst_version: str | None,
) -> None:
    """Record merge provenance for a just-upserted profile.

    Two effects, both idempotent:

      * **derived_from** — each ORIGINAL surface form is content-addressed to a
        stable marker UUID (:func:`_alias_marker`) and appended to the
        ``entity_profiles.derived_from`` ``uuid[]`` (deduped — a marker already
        present is not re-added), and the readable form is unioned into
        ``data.merged_aliases``. Only fires when ``aliases`` is non-empty (i.e.
        canonicalization actually folded a surface form / corrected a class).
      * **entity_profile_versions** — a version row is written on profile
        CREATION (so the 0-row dead table is populated) and on every material
        mutation (a new alias folded in). The row is content-keyed on
        ``(entity_id, version)`` via ``ON CONFLICT DO NOTHING`` so a re-run is a
        no-op.
    """
    folded = False
    if aliases:
        markers = [_alias_marker(a) for a in sorted(aliases)]
        # Append only the markers not already present (dedup), and union the
        # readable forms into data.merged_aliases. A single statement keeps it
        # atomic + idempotent.
        await conn.execute(
            """
            UPDATE entity_profiles
               SET derived_from = (
                       SELECT COALESCE(array_agg(DISTINCT m), '{}'::uuid[])
                         FROM unnest(derived_from || $2::uuid[]) AS m
                   ),
                   data = jsonb_set(
                       COALESCE(data, '{}'::jsonb),
                       '{merged_aliases}',
                       (
                           SELECT COALESCE(jsonb_agg(DISTINCT a ORDER BY a), '[]'::jsonb)
                             FROM jsonb_array_elements_text(
                                 COALESCE(data->'merged_aliases', '[]'::jsonb)
                                 || $3::jsonb
                             ) AS a
                       )
                   ),
                   updated_at = now()
             WHERE id = $1::uuid
            """,
            entity_id,
            markers,
            json.dumps(sorted(aliases)),
        )
        folded = True

    if created or folded:
        # Write a version snapshot. The table has no (entity_id, version) unique
        # constraint (baseline: PK is the surrogate id only), so idempotency on
        # re-run is enforced with a content-guard NOT EXISTS rather than ON
        # CONFLICT — a row whose (entity_id, version, event, merged_aliases) is
        # already present is not re-inserted. This keeps the dead 0-row table
        # populated without a new migration.
        event = "created" if created else "alias_folded"
        merged = await conn.fetchval(
            "SELECT COALESCE(data->'merged_aliases', '[]'::jsonb) "
            "FROM entity_profiles WHERE id = $1::uuid",
            entity_id,
        )
        merged_text = merged if isinstance(merged, str) else json.dumps(
            merged if merged is not None else []
        )
        await conn.execute(
            """
            INSERT INTO entity_profile_versions
                (entity_id, version, data, analyst_id, analyst_version, run_id)
            SELECT $1::uuid, $2,
                   jsonb_build_object(
                       'canonical_name', ep.canonical_name,
                       'entity_class', ep.entity_class,
                       'merged_aliases', COALESCE(ep.data->'merged_aliases', '[]'::jsonb),
                       'event', $6::text
                   ),
                   $3, $4, $5::uuid
              FROM entity_profiles ep
             WHERE ep.id = $1::uuid
               AND NOT EXISTS (
                   SELECT 1 FROM entity_profile_versions v
                    WHERE v.entity_id = $1::uuid
                      AND v.version = $2
                      AND v.data->>'event' = $6::text
                      AND COALESCE(v.data->'merged_aliases', '[]'::jsonb)
                          = $7::jsonb
               )
            """,
            entity_id,
            version,
            analyst_id,
            analyst_version,
            str(run_id) if run_id is not None else None,
            event,
            merged_text,
        )


async def _resolve_batch(
    pool: Any,
    *,
    batch_limit: int,
    geocoder: NameGeocoder | None = None,
    run_id: Any | None = None,
    analyst_id: str | None = None,
    analyst_version: str | None = None,
) -> dict[str, int]:
    """Fold the next batch of un-resolved signals into the entity graph.

    ``geocoder`` (optional) geocodes a location entity by its NAME; absent, the
    offline name-consistency resolver runs. Returns counters for the run
    summary. All writes are idempotent.

    Every NER span is run through :func:`canonicalize_entity` BEFORE the dedup
    key + upsert (surface-form merge + NER type correction). When canonicalization
    changed the surface form OR the class, the ORIGINAL surface form is recorded
    as merge provenance: an ``entity_profile`` row gets a synthetic-UUID marker
    appended to ``derived_from`` (a content-addressed v5 UUID of the original
    surface form, deduped) and an ``entity_profile_versions`` row is written so
    the merge is auditable. ``run_id`` / ``analyst_id`` / ``analyst_version``
    stamp those version rows.
    """
    signals_processed = 0
    links_created = 0
    edges_upserted = 0
    # Per-run cache so two signals mentioning the same entity reuse the id
    # without a second upsert round-trip. Keyed by the COMPOSITE identity
    # (lower(name), class) to match the entity_profiles composite key (0035) —
    # a bare name would re-merge Georgia/country with Georgia/location.
    name_to_id: dict[tuple[str, str], str] = {}

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, payload
              FROM signals
             WHERE entities_resolved_at IS NULL
               AND payload ? 'entities'
             ORDER BY fetched_at ASC NULLS FIRST
             LIMIT $1
            """,
            batch_limit,
        )

        for r in rows:
            payload = _as_dict(r["payload"])
            ents = payload.get("entities") or []
            geo = payload.get("geo") if isinstance(payload.get("geo"), dict) else {}
            title = str(payload.get("title") or "")[:200]
            # Co-mention snippet window: title + the first available prose body
            # field (RSS=summary/raw_body, GDELT/mediacloud=text, etc.). A
            # richer window than the bare title lets the reifier's proxy path
            # spot a real cut-out third entity in the sentence, not invent one.
            body_text = ""
            for _k in ("summary", "raw_body", "text", "body", "content", "description"):
                _v = payload.get(_k)
                if isinstance(_v, str) and _v.strip():
                    body_text = _v.strip()
                    break
            snippet = " — ".join(p for p in (title, body_text) if p).strip()
            snippet = " ".join(snippet.split())[:MAX_SNIPPET_LEN]

            # CANONICALIZE each mention (surface-form merge + NER type
            # correction) BEFORE the dedup key, so fragmented surface forms
            # ({US, U.S., USA, ...}) converge onto ONE canonical identity and
            # mistypes (country-as-person, NWS-office-as-person) are corrected
            # at write. Then dedup by the COMPOSITE canonical identity
            # (lower(canonical_name), canonical_class) — mirroring the
            # entity_profiles composite unique key (migration 0035). Two
            # mentions sharing a name across classes (Georgia/country vs
            # Georgia/location) are DISTINCT entities and must not collapse.
            #
            # ``aliases`` collects, per canonical key, the set of ORIGINAL
            # surface forms whose surface-or-class changed under
            # canonicalization — the merge provenance recorded into the
            # profile's ``derived_from`` below.
            seen: dict[tuple[str, str], tuple[str, str]] = {}
            aliases: dict[tuple[str, str], set[str]] = {}
            for e in ents:
                if not isinstance(e, dict):
                    continue
                raw_text = str(e.get("text") or "").strip()
                raw_cls = (str(e.get("class") or "entity").strip() or "entity")
                text, cls = canonicalize_entity(raw_text, raw_cls)
                if len(text) < MIN_NAME_LEN:
                    continue
                # D8/D7: drop a span canonicalization left as true junk (a
                # clock-time / quantifier / numeric / residual-HTML token NER
                # mis-emitted) so it never becomes an entity node. Demonyms are
                # NOT junk — canonicalize_entity already collapsed them to their
                # country above (is_junk_entity returns False for them).
                if is_junk_entity(text):
                    continue
                # D8 class-agnostic identity: resolve ONE deterministic class so
                # the same surface form ("turkey", "Bank of England") converges
                # on a SINGLE (name, class) row going forward instead of
                # fragmenting across country/entity/location/person.
                cls = resolve_entity_class(text, cls)
                key = (text.lower(), cls)
                seen.setdefault(key, (text, cls))
                # Record the original surface form as provenance only when
                # canonicalization actually changed the surface OR the class.
                if raw_text != text or raw_cls != cls:
                    aliases.setdefault(key, set()).add(raw_text)

            signal_names: list[str] = []
            for key, (text, cls) in seen.items():
                key_aliases = aliases.get(key, set())
                eid = name_to_id.get(key)
                if eid is None:
                    # Geo by the entity's OWN NAME (not the signal's geocode).
                    # Signal-geo is at most a consistency-checked fallback.
                    egeo = await resolve_entity_geo(
                        name=text,
                        entity_class=cls,
                        signal_geo=geo,
                        geocoder=geocoder,
                    )
                    lat, lon, country = egeo.lat, egeo.lon, egeo.country
                    # D26: COMPUTE completeness from the filled fields (was a
                    # flat 0.3 constant). aliases for THIS signal are the merge
                    # evidence; the geo + non-generic class lift it.
                    completeness = compute_completeness(
                        name=text,
                        entity_class=cls,
                        geo_country=country,
                        geo_lat=lat,
                        geo_lon=lon,
                        alias_count=len(key_aliases),
                    )
                    # D26: source-signal provenance — the originating signal id
                    # stamped into derived_from (a uuid[]), plus analyst_id /
                    # analyst_version / run_id (were NULL → lineage couldn't enter
                    # the tier). On conflict we UNION the new signal id, keep the
                    # HIGHER completeness, and backfill a NULL analyst stamp.
                    src_sig = r["id"] if isinstance(r["id"], uuid.UUID) else None
                    derived_arr = [src_sig] if src_sig is not None else []
                    prof = await conn.fetchrow(
                        """
                        INSERT INTO entity_profiles
                            (canonical_name, entity_type, entity_class, data,
                             geo_lat, geo_lon, geo_country, completeness_score,
                             analyst_id, analyst_version, run_id, derived_from,
                             last_event_link_at)
                        VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10,$11::uuid,
                                $12::uuid[], now())
                        ON CONFLICT (lower(canonical_name), entity_class) DO UPDATE
                            SET last_event_link_at = now(),
                                -- D26: keep the richer completeness; never regress.
                                completeness_score = GREATEST(
                                    entity_profiles.completeness_score,
                                    EXCLUDED.completeness_score
                                ),
                                -- D26: backfill a NULL analyst stamp (don't clobber).
                                analyst_id = COALESCE(entity_profiles.analyst_id, EXCLUDED.analyst_id),
                                analyst_version = COALESCE(entity_profiles.analyst_version, EXCLUDED.analyst_version),
                                run_id = COALESCE(entity_profiles.run_id, EXCLUDED.run_id),
                                -- D26: UNION the originating signal id (deduped).
                                derived_from = (
                                    SELECT COALESCE(array_agg(DISTINCT m), '{}'::uuid[])
                                      FROM unnest(entity_profiles.derived_from
                                                  || EXCLUDED.derived_from) AS m
                                ),
                                -- Geo is inherited on conflict ONLY when the
                                -- countries are consistent: fill a NULL stored
                                -- geo, or keep refining within the same
                                -- country. An incoming geo whose country
                                -- DISAGREES with the stored one is never
                                -- inherited -- that cross-country bleed is how
                                -- the single-key bug geocoded country-Georgia
                                -- to Azerbaijan. The conflict is now same-name
                                -- AND same-class (0035), so a country mismatch
                                -- here means a stray location-geo on a mention,
                                -- not a genuinely new place.
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
                        RETURNING id, version, (xmax = 0) AS inserted
                        """,
                        text, cls, cls, json.dumps({"source": "entity_resolution"}),
                        lat, lon, country, completeness,
                        analyst_id, analyst_version,
                        str(run_id) if run_id is not None else None,
                        derived_arr,
                    )
                    eid = str(prof["id"])
                    name_to_id[key] = eid
                    # Merge provenance: fold the ORIGINAL surface form(s) into
                    # derived_from (content-addressed, deduped) + data, and write
                    # an entity_profile_versions row. On creation we still write a
                    # v1 version row so the table is never silently dead.
                    await _record_provenance(
                        conn,
                        entity_id=eid,
                        version=int(prof["version"]),
                        created=bool(prof["inserted"]),
                        aliases=key_aliases,
                        run_id=run_id,
                        analyst_id=analyst_id,
                        analyst_version=analyst_version,
                    )
                elif key_aliases:
                    # The profile was created earlier in THIS batch (cache hit),
                    # but this signal contributes NEW alias provenance for the
                    # same canonical key (e.g. signal A had "USA", signal B has
                    # "U.S." → both fold to United States). Fold it now —
                    # otherwise these aliases are lost (the signal is about to
                    # be stamped resolved and never reprocessed). Idempotent:
                    # _record_provenance dedups derived_from + version rows.
                    cur_version = await conn.fetchval(
                        "SELECT version FROM entity_profiles WHERE id = $1::uuid",
                        eid,
                    )
                    await _record_provenance(
                        conn,
                        entity_id=eid,
                        version=int(cur_version) if cur_version is not None else 1,
                        created=False,
                        aliases=key_aliases,
                        run_id=run_id,
                        analyst_id=analyst_id,
                        analyst_version=analyst_version,
                    )
                await conn.execute(
                    "INSERT INTO signal_entity_links (signal_id, entity_id, role, confidence) "
                    "VALUES ($1,$2,'mentioned',0.8) ON CONFLICT DO NOTHING",
                    r["id"], eid,
                )
                links_created += 1
                signal_names.append(text)

            # Co-occurrence edges — pairwise among the signal's (capped) entities.
            #
            # D15 (upstream): STAMP the originating signal id into the edge's
            # derived_from (uuid[]) at write time. The co_occurs edge is derived
            # from THIS signal's co-mention — that signal id is the real lineage
            # the relationship_reifier copies into the nexus (it reads
            # pe.derived_from and populates BOTH the nexus derived_from +
            # source_signal_ids). Without it ALL proposed_edges carried an empty
            # array, so nothing propagated and agent nexuses had no provenance.
            edge_sig = r["id"] if isinstance(r["id"], uuid.UUID) else None
            edge_derived = [edge_sig] if edge_sig is not None else []
            names = sorted(set(signal_names))[:MAX_ENTITIES_PER_SIGNAL]
            for a, b in itertools.combinations(names, 2):
                # Store the co-mention SNIPPET plus the OTHER entities co-named
                # in this signal — these "co_mentioned" names are the candidate
                # cut-outs the reifier's proxy path selects from (#99). Format is
                # a parseable two-line block; the snippet alone stays human-read.
                others = [n for n in names if n != a and n != b]
                evidence = snippet or title
                if others:
                    evidence = (
                        f"{evidence}\nco_mentioned: {', '.join(others)}"
                    )
                await conn.execute(
                    """
                    INSERT INTO proposed_edges
                        (source_entity, target_entity, relationship_type, confidence,
                         evidence_text, status, derived_from)
                    VALUES ($1,$2,'co_occurs',0.4,$3,'pending',$4::uuid[])
                    ON CONFLICT (lower(source_entity), lower(target_entity), relationship_type)
                    DO UPDATE SET confidence = LEAST(1.0, proposed_edges.confidence + 0.05),
                                 evidence_text = EXCLUDED.evidence_text,
                                 -- D15: UNION the originating signal id (deduped),
                                 -- so a re-corroborated edge accrues every signal
                                 -- that produced it as lineage (never re-grows).
                                 derived_from = (
                                     SELECT COALESCE(array_agg(DISTINCT m), '{}'::uuid[])
                                       FROM unnest(proposed_edges.derived_from
                                                   || EXCLUDED.derived_from) AS m
                                 )
                    """,
                    a, b, evidence, edge_derived,
                )
                edges_upserted += 1

            # Stamp the signal resolved regardless of how many links it produced
            # (forward progress — a zero-mention signal is never reprocessed).
            await conn.execute(
                "UPDATE signals SET entities_resolved_at = now() WHERE id = $1",
                r["id"],
            )
            signals_processed += 1

    return {
        "signals_processed": signals_processed,
        "entities_upserted": len(name_to_id),
        "links_created": links_created,
        "edges_upserted": edges_upserted,
    }


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    sp = counters.get("signals_processed", 0)
    title = (
        f"Entity resolution: folded {sp} signal(s) → "
        f"{counters.get('entities_upserted', 0)} entities, "
        f"{counters.get('links_created', 0)} links, "
        f"{counters.get('edges_upserted', 0)} co-occurrence edges"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "entity_resolution"]
    if sp:
        tags.append("signals_processed")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "all un-resolved signals", not a time window),
    matching the ``cooccurrence_edges`` pattern. ``deps is None`` (unit-test
    path with no live substrate) yields a zeroed run.
    """
    counters: dict[str, int] = {
        "signals_processed": 0,
        "entities_upserted": 0,
        "links_created": 0,
        "edges_upserted": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        batch_limit = int(options.get("batch_limit", _DEFAULT_BATCH))
        # Optional name-geocoder (deps.extras['geocoder']) — when wired, an
        # entity's geo is resolved by its NAME; absent, the offline name-
        # consistency resolver runs (unit-test hermetic).
        extras = getattr(deps, "extras", None) if deps is not None else None
        geocoder = None
        if isinstance(extras, Mapping):
            cand = extras.get("geocoder")
            if isinstance(cand, NameGeocoder):
                geocoder = cand
        # Provenance stamps for the entity_profiles / entity_profile_versions
        # rows the resolver writes. D26 RESIDUAL: the deterministic sub-handler
        # invocation does NOT reliably carry ``analyst_id`` in ``options`` (it is
        # populated for some callers, absent for others), so a bare
        # ``options.get("analyst_id")`` resolved to ``None`` and the upsert's
        # ``analyst_id = COALESCE(..., EXCLUDED.analyst_id)`` always stayed NULL
        # (completeness + derived_from landed because they do not depend on
        # ``options``). Fall back to ``SUB_HANDLER_NAME`` exactly like the sibling
        # deterministic handlers (cross_source_coalesce / cross_source_dedup /
        # graph_mining / hypothesis_lifecycle) so the analyst stamp is never NULL
        # and lands on the same write path as completeness + derived_from.
        run_id = options.get("run_id")
        analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
        analyst_version = options.get("analyst_version")
        try:
            counters = await _resolve_batch(
                pool,
                batch_limit=batch_limit,
                geocoder=geocoder,
                run_id=run_id,
                analyst_id=analyst_id,
                analyst_version=(
                    str(analyst_version) if analyst_version is not None else None
                ),
            )
        except Exception as exc:
            logger.warning("entity_resolution.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
