# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``watchlist_hit`` trigger-class internals — P5-6 Watchlist v2.

The operator-defined standing-watch layer of the alert loop: the operator
names a watch — an entity ("Wagner Group"), a topic ("Strait of Hormuz"), or a
place (countries / point+radius) — and any VERIFIED finding touching it alerts
regardless of desk or severity (unless the watch sets its own ``min_severity``
floor). Evaluated by :mod:`.alert_trigger_scan` as its fifth trigger class,
over the ``watchlist`` table (migration 0105).

Design decisions
----------------
* **Storage = a table, not a descriptor family.** Watches are operator DATA —
  cheap to add/remove, no versioning/lifecycle ceremony, soft-deleted via
  ``active=false``. A descriptor would buy audit-chain machinery nobody needs
  for "watch this" and make the add/remove loop heavyweight.
* **The verified bar** is the SAME bar the P1-3 ``verified_finding`` trigger
  uses — a faithfulness-verify critique exists AND
  ``min(confidence, faithfulness) >= floor`` (0.50), not superseded — with ONE
  deliberate widening: findings from
  :data:`~legba.data.provenance.kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`
  COUNT (structural-verified). Those analysts are deterministic (no LLM prose)
  and never enter the verify pass, so excluding them — correct for the
  "verified high-sev finding" trigger, which is ABOUT the verify event — would
  silently blind a watch to real deterministic substrate events. The alert's
  verify posture stays honest either way: a faithfulness score when one
  exists, an explicit structural-exempt statement when not.
* **No history paging.** Two guards: (a) the class-level first-scan seed (the
  0091 contract — bringing the analyst up never pages history), and (b) a
  per-watch ``produced_at > watch.created_at`` gate, so a NEWLY created watch
  only ever fires on findings produced AFTER it existed.
* **No refire.** One ``alert_trigger_watermarks`` row per
  ``(watch_id, finding_id)`` (append-style, pruned with the verified_finding
  class's age rule), advanced only after the alert row landed.
* **Per-watch cap.** At most ``per_watch_cap`` (default 3) alerts per watch
  per scan, worst-first; the remainder folds into ONE per-watch rollup whose
  count is stated honestly and whose members' watermarks still advance. The
  handler's shared per-desk cap then applies downstream as usual.

Matching semantics (exact, per kind)
------------------------------------
* ``entity`` — ENTITY-RESOLUTION first, raw text only as lineage fallback:
  the watch pattern (``name`` and/or ``entity_id``) resolves against
  ``entity_profiles`` via (1) id, (2) case-insensitive canonical-name match,
  (3) case-insensitive ``data.merged_aliases`` match (so a watch on "SNSC"
  matches the canonical Supreme National Security Council the resolver folded
  that alias into), (4) an :func:`~legba.data._entity_canon.identity_fold`
  confirm over a bounded ILIKE candidate set (article/case/punct variants:
  "The Wagner Group" → "Wagner Group"). Tombstones follow ``merged_into`` to
  the keeper, and first-level merged losers are INCLUDED (links may predate a
  merge). A finding matches when a lineage signal (``derived_from`` directly,
  or through a fact's ``derived_from``) is linked to a resolved entity id in
  ``signal_entity_links``, OR a lineage fact's ``subject`` equals (case-
  insensitive) the resolved canonical name / an alias. An unresolvable entity
  watch matches NOTHING (logged; it never degrades into a text search).
* ``text`` — EXACTLY the existing search plane's semantics
  (``substrate_reads_api`` findings ``q``): ``to_tsvector('simple', title
  || ' ' || body) @@ plainto_tsquery('simple', query)``. HONEST LIMITS: this
  is an AND-of-all-terms match over the finding's TITLE + BODY with the
  'simple' config — no stemming, no OR/NOT/phrase operators, no websearch
  syntax, and claim/citation sidecar text is NOT separately matched (composed
  findings quote their claims in the body, which IS matched).
* ``geo`` — countries: a lineage signal's ``geo`` ISO2 tags intersect the
  watched country list. Point+radius: a lineage signal's geocoded point lies
  within ``radius_km`` (great-circle, computed in SQL) — POINT-TRUSTWORTHY
  tier only (``payload.geo.precision`` in region/municipality/address, or
  ``payload.geo.source = 'geometry'``): the geo_convergence honesty precedent
  — a country-centroid geocode is never treated as a point.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional
from uuid import UUID

from legba.data._entity_canon import identity_fold
from ...provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
from ...provenance.models import _SEVERITY_RANK, severity_from_tags

logger = logging.getLogger(__name__)

#: Bound on active watches evaluated per scan (defensive; operator data).
_MAX_WATCHES = 200
#: Bound on verified window findings considered per scan.
_MAX_WINDOW_FINDINGS = 500
#: Bounds on entity resolution fan-out.
_MAX_ENTITY_IDS = 50
_MAX_ENTITY_NAMES = 100
_FOLD_PROBE_LIMIT = 50

#: Default per-watch alert cap per scan (worst-first; remainder → one rollup).
DEFAULT_PER_WATCH_CAP = 3

#: Unit-tag severity vocabulary → AlertPayload ladder (moderate/elevated are
#: the unit tags; the alert row's own severity field speaks the alert ladder).
_ALERT_SEVERITY = {
    "info": "info",
    "low": "low",
    "moderate": "medium",
    "medium": "medium",
    "elevated": "high",
    "high": "high",
    "critical": "critical",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_jsonish(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def resolved_finding_severity(
    severity_col: Any, tags: Any
) -> Optional[str]:
    """The finding's resolved severity level, column first, tag fallback
    (the S3-T4 lift mirror). ``None`` when neither carries a known level."""
    col = str(severity_col or "").strip().lower()
    if col in _SEVERITY_RANK:
        return col
    parsed = _parse_jsonish(tags)
    return severity_from_tags(parsed if isinstance(parsed, list) else None)


def severity_meets_floor(resolved: Optional[str], min_severity: Optional[str]) -> bool:
    """Does the finding's resolved severity clear the watch's floor?

    No floor → always. A floor + an UNRESOLVED severity → False (we cannot
    prove the finding meets the operator's stated bar — honest refusal, never
    a guess)."""
    if not min_severity:
        return True
    if resolved is None:
        return False
    return _SEVERITY_RANK.get(resolved, -1) >= _SEVERITY_RANK.get(min_severity, 99)


def alert_severity_for(resolved: Optional[str]) -> str:
    """Map the finding's resolved severity onto the AlertPayload ladder
    (``medium`` when unresolved — a watch hit is an operator page either way)."""
    if resolved is None:
        return "medium"
    return _ALERT_SEVERITY.get(resolved, "medium")


def watermark_key(watch_id: str, finding_id: str) -> str:
    return f"{watch_id}|{finding_id}"


def fold_watch_rollups(
    candidates: list[Any], cap: int, make_candidate: Any
) -> tuple[list[Any], list[Any]]:
    """Per-WATCH cap: keep the worst ``cap`` hits per watch, fold the rest
    into ONE per-watch rollup (count stated honestly; watermarks carried so
    the summarized hits never refire). Returns (kept, rollups).

    ``make_candidate`` is the AlertCandidate constructor (injected to keep
    this module import-cycle-free with alert_trigger_scan)."""
    sev_rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    by_watch: dict[str, list[Any]] = {}
    for cand in candidates:
        by_watch.setdefault(str(cand.data.get("watch_id") or ""), []).append(cand)

    kept: list[Any] = []
    rollups: list[Any] = []
    for watch_id, cands in sorted(by_watch.items()):
        ordered = sorted(
            cands, key=lambda c: (-sev_rank.get(c.severity, 0), c.title)
        )
        kept.extend(ordered[:cap])
        rest = ordered[cap:]
        if not rest:
            continue
        worst = max((c.severity for c in rest), key=lambda s: sev_rank.get(s, 0))
        label = str(rest[0].data.get("watch_label") or watch_id)
        summaries = [
            {"severity": c.severity, "title": c.title[:200]} for c in rest
        ]
        merged_watermarks: list[tuple[str, str, dict[str, Any]]] = []
        merged_refs: list[UUID] = []
        for c in rest:
            merged_watermarks.extend(c.watermarks)
            for ref in c.derived_from:
                if ref not in merged_refs and len(merged_refs) < 8:
                    merged_refs.append(ref)
        rollups.append(
            make_candidate(
                trigger_class="rollup",
                severity=worst,
                title=(
                    f"Watch rollup [{label}]: {len(rest)} further hit(s) this "
                    f"scan beyond the per-watch cap of {cap}"
                ),
                body="\n".join(
                    f"[{s['severity']}] {s['title']}" for s in summaries
                ),
                target_id=None,
                derived_from=merged_refs,
                data={
                    "trigger_class": "rollup",
                    "rollup_of": "watchlist_hit",
                    "watch_id": watch_id,
                    "watch_label": label,
                    "suppressed_count": len(rest),
                    "per_watch_cap": cap,
                    "suppressed": summaries,
                },
                watermarks=merged_watermarks,
            )
        )
    return kept, rollups


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_WATCHES_SQL = """
    SELECT id::text AS id, kind, pattern, label, min_severity, created_at
      FROM watchlist
     WHERE active
     ORDER BY created_at, id
     LIMIT $1
"""

# The verified bar (P1-3 verified_finding), widened by the structural-exempt
# branch (see module docstring). LEFT lateral — the structural branch needs
# rows with no critique.
_VERIFIED_WINDOW_SQL = """
    SELECT f.id::text            AS finding_id,
           f.analyst_id          AS analyst_id,
           f.target_id           AS target_id,
           f.title               AS title,
           f.confidence          AS confidence,
           f.severity            AS severity,
           f.data -> 'tags'      AS tags,
           f.produced_at         AS produced_at,
           v.faithfulness_score  AS faithfulness_score,
           (f.analyst_id = ANY($2::text[])) AS structural_exempt
      FROM analyst_outputs f
      LEFT JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.superseded_by IS NULL
       AND f.produced_at > now() - make_interval(hours => $1)
       AND (
             (v.faithfulness_score IS NOT NULL
              AND LEAST(f.confidence, v.faithfulness_score) >= $3)
             OR f.analyst_id = ANY($2::text[])
           )
     ORDER BY f.produced_at DESC
     LIMIT $4
"""

# Entity resolution — exact canonical / merged_aliases match (case-insensitive).
_ENTITY_EXACT_SQL = """
    SELECT id, canonical_name, merged_into,
           COALESCE(data->'merged_aliases', '[]'::jsonb) AS aliases
      FROM entity_profiles
     WHERE lower(canonical_name) = lower($1)
        OR EXISTS (
             SELECT 1
               FROM jsonb_array_elements_text(
                        COALESCE(data->'merged_aliases', '[]'::jsonb)
                    ) a(v)
              WHERE lower(a.v) = lower($1)
           )
     LIMIT 25
"""

_ENTITY_BY_ID_SQL = """
    SELECT id, canonical_name, merged_into,
           COALESCE(data->'merged_aliases', '[]'::jsonb) AS aliases
      FROM entity_profiles
     WHERE id = $1
"""

# Bounded fold-probe candidate fetch (confirmed Python-side via identity_fold).
_ENTITY_PROBE_SQL = """
    SELECT id, canonical_name, merged_into,
           COALESCE(data->'merged_aliases', '[]'::jsonb) AS aliases
      FROM entity_profiles
     WHERE canonical_name ILIKE '%' || $1 || '%'
       AND merged_into IS NULL
     LIMIT $2
"""

# First-level merged losers of the keeper set (links may predate the merge).
_ENTITY_LOSERS_SQL = """
    SELECT id FROM entity_profiles WHERE merged_into = ANY($1::uuid[]) LIMIT $2
"""

# Entity match over the window findings: lineage signal links (finding →
# derived signals, directly or through a fact's derived_from) OR a lineage
# fact whose subject names the watched canonical/alias. Both EXISTS are
# surfaced so matched_via stays honest.
_ENTITY_MATCH_SQL = """
    SELECT f.id::text AS finding_id,
           EXISTS (
             SELECT 1 FROM signal_entity_links sel
              WHERE sel.entity_id = ANY($2::uuid[])
                AND (sel.signal_id = ANY(f.derived_from)
                     OR EXISTS (
                          SELECT 1 FROM facts fx
                           WHERE fx.id = ANY(f.derived_from)
                             AND sel.signal_id = ANY(fx.derived_from)
                        ))
           ) AS via_links,
           EXISTS (
             SELECT 1 FROM facts fs
              WHERE fs.id = ANY(f.derived_from)
                AND lower(fs.subject) = ANY($3::text[])
           ) AS via_fact_subject
      FROM analyst_outputs f
     WHERE f.id = ANY($1::uuid[])
       AND (
             EXISTS (
               SELECT 1 FROM signal_entity_links sel
                WHERE sel.entity_id = ANY($2::uuid[])
                  AND (sel.signal_id = ANY(f.derived_from)
                       OR EXISTS (
                            SELECT 1 FROM facts fx
                             WHERE fx.id = ANY(f.derived_from)
                               AND sel.signal_id = ANY(fx.derived_from)
                          ))
             )
             OR EXISTS (
               SELECT 1 FROM facts fs
                WHERE fs.id = ANY(f.derived_from)
                  AND lower(fs.subject) = ANY($3::text[])
             )
           )
"""

# Text match — EXACTLY the search plane's predicate (see module docstring).
_TEXT_MATCH_SQL = """
    SELECT f.id::text AS finding_id
      FROM analyst_outputs f
     WHERE f.id = ANY($1::uuid[])
       AND to_tsvector('simple',
                       coalesce(f.title, '') || ' ' || coalesce(f.body, ''))
           @@ plainto_tsquery('simple', $2)
"""

# Geo country-tier match: lineage signal geo tags intersect the watch list.
_GEO_COUNTRY_MATCH_SQL = """
    SELECT DISTINCT f.id::text AS finding_id
      FROM analyst_outputs f
     WHERE f.id = ANY($1::uuid[])
       AND EXISTS (
             SELECT 1 FROM signals s
              WHERE (s.id = ANY(f.derived_from)
                     OR EXISTS (
                          SELECT 1 FROM facts fx
                           WHERE fx.id = ANY(f.derived_from)
                             AND s.id = ANY(fx.derived_from)
                        ))
                AND s.geo && $2::text[]
           )
"""

# Geo point-tier match: POINT-TRUSTWORTHY lineage signals only (the
# geo_convergence precision gate), great-circle distance <= radius_km.
_GEO_POINT_MATCH_SQL = """
    SELECT DISTINCT f.id::text AS finding_id
      FROM analyst_outputs f
     WHERE f.id = ANY($1::uuid[])
       AND EXISTS (
             SELECT 1 FROM signals s
              WHERE (s.id = ANY(f.derived_from)
                     OR EXISTS (
                          SELECT 1 FROM facts fx
                           WHERE fx.id = ANY(f.derived_from)
                             AND s.id = ANY(fx.derived_from)
                        ))
                AND s.payload->'geo'->>'lat' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                AND s.payload->'geo'->>'lon' ~ '^-?[0-9]+(\\.[0-9]+)?$'
                AND (
                      s.payload->'geo'->>'precision' IN
                          ('region', 'municipality', 'address')
                      OR s.payload->'geo'->>'source' = 'geometry'
                    )
                AND 6371.0088 * acos(LEAST(1.0, GREATEST(-1.0,
                      sin(radians($2))
                        * sin(radians((s.payload->'geo'->>'lat')::float8))
                      + cos(radians($2))
                        * cos(radians((s.payload->'geo'->>'lat')::float8))
                        * cos(radians((s.payload->'geo'->>'lon')::float8)
                              - radians($3))
                    ))) <= $4
           )
"""


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def _collect_names(rows: list[Any], names: set[str]) -> None:
    for r in rows:
        cn = str(r["canonical_name"] or "").strip().lower()
        if cn:
            names.add(cn)
        aliases = _parse_jsonish(r["aliases"])
        if isinstance(aliases, list):
            for a in aliases:
                if isinstance(a, str) and a.strip():
                    names.add(a.strip().lower())
        if len(names) >= _MAX_ENTITY_NAMES:
            return


async def _follow_merged(conn: Any, row: Any) -> Any:
    """Follow a tombstone's ``merged_into`` chain to its keeper (bounded)."""
    seen = 0
    while row is not None and row["merged_into"] is not None and seen < 5:
        row = await conn.fetchrow(_ENTITY_BY_ID_SQL, row["merged_into"])
        seen += 1
    return row


async def resolve_watch_entity(
    conn: Any, pattern: Mapping[str, Any]
) -> tuple[list[UUID], list[str]]:
    """(entity ids, lowered canonical+alias names) for one entity watch.

    Resolution order per the module docstring: id → exact canonical/alias →
    identity_fold confirm over a bounded ILIKE candidate set. Empty results
    mean the watch matches nothing (never a text fallback)."""
    hits: list[Any] = []

    raw_id = pattern.get("entity_id")
    if raw_id:
        try:
            eid = UUID(str(raw_id))
        except (ValueError, TypeError):
            eid = None
        if eid is not None:
            row = await conn.fetchrow(_ENTITY_BY_ID_SQL, eid)
            row = await _follow_merged(conn, row)
            if row is not None:
                hits.append(row)

    name = str(pattern.get("name") or "").strip()
    if name:
        exact = await conn.fetch(_ENTITY_EXACT_SQL, name)
        for row in exact:
            row = await _follow_merged(conn, row)
            if row is not None:
                hits.append(row)
        if not exact:
            # Fold-probe: article/case/punctuation variants via the canon.
            fold = identity_fold(name)
            if fold:
                words = [w for w in name.split() if w.strip()]
                token = max(words, key=len) if words else name
                probe = await conn.fetch(
                    _ENTITY_PROBE_SQL, token, _FOLD_PROBE_LIMIT
                )
                for row in probe:
                    if identity_fold(str(row["canonical_name"] or "")) == fold:
                        hits.append(row)
                        continue
                    aliases = _parse_jsonish(row["aliases"])
                    if isinstance(aliases, list) and any(
                        isinstance(a, str) and identity_fold(a) == fold
                        for a in aliases
                    ):
                        hits.append(row)

    ids: list[UUID] = []
    for row in hits:
        if row["id"] not in ids and len(ids) < _MAX_ENTITY_IDS:
            ids.append(row["id"])
    names: set[str] = set()
    _collect_names(hits, names)

    # First-level merged losers still referenced by pre-merge links.
    if ids:
        losers = await conn.fetch(_ENTITY_LOSERS_SQL, ids, _MAX_ENTITY_IDS)
        for r in losers:
            if r["id"] not in ids and len(ids) < _MAX_ENTITY_IDS:
                ids.append(r["id"])
    return ids, sorted(names)


# ---------------------------------------------------------------------------
# Per-kind matchers → {finding_id: matched_via description}
# ---------------------------------------------------------------------------


async def _match_entity(
    conn: Any, pattern: Mapping[str, Any], finding_ids: list[UUID]
) -> dict[str, str]:
    ids, names = await resolve_watch_entity(conn, pattern)
    if not ids and not names:
        logger.info(
            "watchlist_scan.entity_unresolved pattern=%s — watch matches "
            "nothing (no text fallback by design)",
            dict(pattern),
        )
        return {}
    rows = await conn.fetch(
        _ENTITY_MATCH_SQL, finding_ids, ids, names or [""]
    )
    out: dict[str, str] = {}
    for r in rows:
        via = []
        if r["via_links"]:
            via.append("signal_entity_links lineage")
        if r["via_fact_subject"]:
            via.append("fact subject")
        out[str(r["finding_id"])] = (
            f"entity-resolution ({' + '.join(via) or 'lineage'})"
        )
    return out


async def _match_text(
    conn: Any, pattern: Mapping[str, Any], finding_ids: list[UUID]
) -> dict[str, str]:
    query = str(pattern.get("query") or "").strip()
    if not query:
        return {}
    rows = await conn.fetch(_TEXT_MATCH_SQL, finding_ids, query)
    return {
        str(r["finding_id"]): (
            "text match (plainto_tsquery 'simple' over title+body — "
            "AND of all terms, no stemming)"
        )
        for r in rows
    }


async def _match_geo(
    conn: Any, pattern: Mapping[str, Any], finding_ids: list[UUID]
) -> dict[str, str]:
    countries = pattern.get("countries")
    if isinstance(countries, list) and countries:
        codes = sorted(
            {
                str(c).strip().upper()
                for c in countries
                if isinstance(c, str) and str(c).strip()
            }
        )[:50]
        if not codes:
            return {}
        rows = await conn.fetch(_GEO_COUNTRY_MATCH_SQL, finding_ids, codes)
        return {
            str(r["finding_id"]): f"geo country tags ∩ {codes}" for r in rows
        }

    try:
        lat = float(pattern.get("lat"))
        lon = float(pattern.get("lon"))
        radius_km = float(pattern.get("radius_km"))
    except (TypeError, ValueError):
        return {}
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and radius_km > 0):
        return {}
    rows = await conn.fetch(
        _GEO_POINT_MATCH_SQL, finding_ids, lat, lon, min(radius_km, 1000.0)
    )
    return {
        str(r["finding_id"]): (
            f"geocoded point within {radius_km:g} km of ({lat:g}, {lon:g}) "
            "(point-trustworthy precision tier only)"
        )
        for r in rows
    }


_MATCHERS = {
    "entity": _match_entity,
    "text": _match_text,
    "geo": _match_geo,
}


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


async def scan_watchlist(
    conn: Any,
    *,
    floor: float,
    window_hours: int,
    per_watch_cap: int,
) -> tuple[list[Any], list[tuple[str, str, dict[str, Any]]], bool, dict[str, int]]:
    """One watchlist_hit scan pass. Returns
    ``(candidates, silent_watermarks, was_seeded, stats)``.

    ``candidates`` already includes the per-watch rollups (cap applied here so
    the cap is per WATCH, not per desk — the handler's desk cap still applies
    downstream). ``stats`` lands in the receipt's ``counts_by_class`` entry:
    ``watches_evaluated`` / ``window_findings`` / ``suppressed_into_watch_rollups``
    / ``unavailable`` (1 when the ``watchlist`` table does not exist yet — the
    scan degrades to empty LOUDLY rather than killing the other four classes
    on a not-yet-migrated substrate).
    """
    # Local imports — alert_trigger_scan imports this module; deferring the
    # reverse import to call time keeps the cycle harmless.
    from .alert_trigger_scan import (
        _FINDING_WATERMARK_PRUNE_DAYS,
        _WM_PRUNE_FINDINGS_SQL,
        SEED_KEY,
        TRIGGER_WATCHLIST,
        AlertCandidate,
        _load_class_watermarks,
        _uuid_or_none,
    )

    stats = {
        "watches_evaluated": 0,
        "window_findings": 0,
        "suppressed_into_watch_rollups": 0,
        "unavailable": 0,
    }

    try:
        watch_rows = await conn.fetch(_WATCHES_SQL, _MAX_WATCHES)
    except Exception as exc:  # asyncpg.UndefinedTableError pre-migration
        if type(exc).__name__ != "UndefinedTableError":
            raise
        logger.warning(
            "watchlist_scan.unavailable — the watchlist table does not exist "
            "yet (migration 0105 not applied); the class scanned nothing"
        )
        stats["unavailable"] = 1
        return [], [], True, stats

    seeded, marked = await _load_class_watermarks(conn, TRIGGER_WATCHLIST)
    # Prune aged-out (watch, finding) rows — same age rule as verified_finding.
    await conn.execute(
        _WM_PRUNE_FINDINGS_SQL,
        TRIGGER_WATCHLIST,
        SEED_KEY,
        _FINDING_WATERMARK_PRUNE_DAYS,
    )

    stats["watches_evaluated"] = len(watch_rows)
    if not watch_rows:
        return [], [], seeded, stats

    finding_rows = await conn.fetch(
        _VERIFIED_WINDOW_SQL,
        int(window_hours),
        sorted(STRUCTURAL_VERIFY_EXEMPT_ANALYSTS),
        float(floor),
        _MAX_WINDOW_FINDINGS,
    )
    stats["window_findings"] = len(finding_rows)
    if not finding_rows:
        return [], [], seeded, stats

    by_id = {str(r["finding_id"]): r for r in finding_rows}
    finding_ids = [
        u for u in (_uuid_or_none(fid) for fid in by_id) if u is not None
    ]

    candidates: list[AlertCandidate] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for w in watch_rows:
        watch_id = str(w["id"])
        kind = str(w["kind"] or "")
        pattern = _parse_jsonish(w["pattern"])
        if not isinstance(pattern, Mapping):
            pattern = {}
        matcher = _MATCHERS.get(kind)
        if matcher is None:
            logger.warning(
                "watchlist_scan.unknown_kind watch=%s kind=%r — skipped",
                watch_id,
                kind,
            )
            continue
        matched = await matcher(conn, pattern, finding_ids)
        if not matched:
            continue

        for fid, matched_via in sorted(matched.items()):
            row = by_id.get(fid)
            if row is None:
                continue
            resolved = resolved_finding_severity(row["severity"], row["tags"])
            if not severity_meets_floor(resolved, w["min_severity"]):
                continue
            key = watermark_key(watch_id, fid)
            if key in marked:
                continue  # already reported (or seeded) — the no-refire contract
            state = {
                "watch_label": str(w["label"] or "")[:200],
                "severity": resolved,
            }
            if not seeded:
                # First-ever scan of the class: seed every current match
                # silently (bring-up never pages history).
                silent.append((TRIGGER_WATCHLIST, key, state))
                continue
            if row["produced_at"] <= w["created_at"]:
                # A NEW watch never pages findings that predate it. No
                # watermark needed — produced_at can never move past
                # created_at later.
                continue

            conf = (
                float(row["confidence"])
                if row["confidence"] is not None
                else None
            )
            faith = (
                float(row["faithfulness_score"])
                if row["faithfulness_score"] is not None
                else None
            )
            eff = (
                min(conf, faith)
                if conf is not None and faith is not None
                else None
            )
            structural = bool(row["structural_exempt"])
            fid_uuid = _uuid_or_none(fid)
            candidates.append(
                AlertCandidate(
                    trigger_class=TRIGGER_WATCHLIST,
                    severity=alert_severity_for(resolved),
                    title=(
                        f"Watch hit [{str(w['label'] or '')[:80]}]: "
                        f"{str(row['title'] or '')[:160]}"
                    ),
                    body=(
                        f"watch={watch_id} kind={kind} "
                        f"label={w['label']}\n"
                        f"matched_via={matched_via}\n"
                        f"finding={fid} analyst={row['analyst_id']} "
                        f"target={row['target_id']}\n"
                        f"severity={resolved} confidence={conf} "
                        f"faithfulness={faith} effective_confidence={eff} "
                        f"structural_verify_exempt={structural} "
                        f"(floor={floor})"
                    ),
                    target_id=(
                        str(row["target_id"]) if row["target_id"] else None
                    ),
                    derived_from=(
                        [fid_uuid] if fid_uuid is not None else []
                    ),
                    data={
                        "trigger_class": TRIGGER_WATCHLIST,
                        "watch_id": watch_id,
                        "watch_kind": kind,
                        "watch_label": str(w["label"] or ""),
                        "matched_finding_id": fid,
                        "matched_via": matched_via,
                        "finding_analyst_id": str(row["analyst_id"] or ""),
                        "finding_severity": resolved,
                        "confidence": conf,
                        "faithfulness_score": faith,
                        "effective_confidence": eff,
                        "structural_verify_exempt": structural,
                        "effective_conf_floor": floor,
                    },
                    watermarks=[(TRIGGER_WATCHLIST, key, state)],
                    effective_confidence=eff,
                    faithfulness_score=faith,
                    event_at=row["produced_at"],
                )
            )

    kept, rollups = fold_watch_rollups(
        candidates, max(1, int(per_watch_cap)), AlertCandidate
    )
    stats["suppressed_into_watch_rollups"] = sum(
        int(r.data.get("suppressed_count", 0)) for r in rollups
    )
    return kept + rollups, silent, seeded, stats


__all__ = [
    "DEFAULT_PER_WATCH_CAP",
    "alert_severity_for",
    "fold_watch_rollups",
    "resolve_watch_entity",
    "resolved_finding_severity",
    "scan_watchlist",
    "severity_meets_floor",
    "watermark_key",
]
