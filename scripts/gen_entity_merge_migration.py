# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline, deterministic generator for the DQ Phase-4 entity-merge migration.

WHY A GENERATOR (not inline SQL): the migration runner (``legba.data.migrate``)
is SQL-ONLY — it globs ``*.sql`` and cannot execute Python, so the demonym /
region-adjective / plural / residue REPLAY that ``legba.data._entity_canon``
performs cannot run inside a migration. This script runs the WORKING-TREE canon
OFFLINE over a snapshot of ``entity_profiles`` and emits a FROZEN, self-contained
``0063_entity_merge.sql`` whose merge decisions are baked in as ``VALUES`` temp
tables, plus a human-readable report.

It is PURE + DETERMINISTIC: given the same input CSV it emits byte-identical SQL
(every collection is sorted before emission; no clock / randomness). It reads
ONLY a CSV export of ``entity_profiles`` (id, canonical_name, entity_class,
source, created_at, link_count) — never the live DB — and imports the canon
(``canonicalize_entity`` / ``is_junk_entity`` / ``identity_fold``).

Pipeline (mirrors planning/DATA_QUALITY_REVIEW_2026-07-03.md §"Auditor: entities"
MERGE PLAN):
  1. CLUSTER every row by ``identity_fold(canonical_name)`` (class-agnostic).
  2. AMBIGUITY GUARD: a cluster spanning a country-class row AND a location-class
     row where BOTH have >= 3 links is a potential genuine ambiguity (Georgia the
     country vs the US state) — do NOT merge; emit to a review list.
  3. SURVIVOR ELECTION (deterministic): seed row wins (prefer country-class seed;
     tie -> highest class priority -> oldest -> smallest id); else highest canon
     class priority (country>organization>location>person>entity; corporation in
     the organization tier; event/treaty low) -> most links -> oldest -> id.
  4. LOSERS = cluster members minus survivor -> merge_map(loser, survivor).
  5. JUNK: a row that is junk-SHAPED (canon form is_junk) AND whose cluster has NO
     real (non-junk) survivor -> junk (gc_status='junk', delete links). A
     junk-shaped row that folds ONTO a real survivor ("Iran</p" -> Iran) is a
     NORMAL loser (re-point + tombstone), NOT junk.
  6. PARTITION INTEGRITY asserts (no id both survivor+loser, both merge+junk;
     every non-survivor non-junk member is in merge_map).
  7. EMIT the migration SQL + the report.

Usage (host, working-tree canon on PYTHONPATH):
    PYTHONPATH=src python3 scripts/gen_entity_merge_migration.py \
        --csv /path/entities_export.csv \
        --out-sql src/legba/data/migrations/0063_entity_merge.sql \
        --out-report planning/entity_merge_report_2026-07-03.md
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from legba.data._entity_canon import (
    _strip_leading_article,
    _strip_name,
    _strip_residue_for_fold,
    canonicalize_entity,
    identity_fold,
    is_junk_entity,
)

# ---------------------------------------------------------------------------
# Class priority for survivor election (lower = higher priority). corporation
# shares the organization tier; event/treaty keep their own low rank. Mirrors
# entity_resolution._CLASS_RANK + the DQ plan (country>organization>location>
# person>entity).
# ---------------------------------------------------------------------------
_GEN_RANK: dict[str, int] = {
    "country": 0,
    "organization": 1,
    "corporation": 1,
    "location": 2,
    "person": 3,
    "entity": 4,
    "event": 5,
    "treaty": 6,
}
_AMBIGUITY_MIN_LINKS = 3


def _grank(cls: str) -> int:
    return _GEN_RANK.get(cls, 9)


def _is_content_junk(name: str) -> bool:
    """True for GENUINE NER spam (clock-time / numeric / money / quantifier /
    age / sports / residual-markup), as opposed to a bare length<=2 abbreviation.

    ``is_junk_entity`` also flags any name <= 2 chars ("LA", "NY") — legit
    high-reference abbreviations that must NEVER be junk-DELETED. This narrows
    the junk bucket to spam flagged by a CONTENT rule (or markup residue), so a
    length-only abbreviation is preserved (merged or left alone), never nuked.
    """
    if not is_junk_entity(name):
        return False
    raw = str(name or "")
    if "<" in raw or ">" in raw:
        return True  # markup residue
    return len(_strip_name(raw)) > 2  # a content rule fired, not the length rule


@dataclass(frozen=True)
class Row:
    id: str
    name: str
    cls: str
    source: str
    created_at: str  # ISO string — sorts lexicographically (oldest = smallest)
    links: int


def _read_csv(path: Path) -> list[Row]:
    rows: list[Row] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(
                Row(
                    id=r["id"].strip(),
                    name=r["canonical_name"],
                    cls=(r["entity_class"] or "entity").strip(),
                    source=(r.get("source") or "").strip(),
                    created_at=(r.get("created_at") or "").strip(),
                    links=int(r.get("link_count") or 0),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Survivor / display election
# ---------------------------------------------------------------------------


def _elect_survivor(members: list[Row]) -> Row:
    """Deterministic survivor election among ALL cluster members.

    Elects among ALL members (not only the non-junk ones): a legit high-link
    abbreviation length-flagged by ``is_junk_entity`` ("LA" — 1148 links) must be
    ELIGIBLE to be the survivor, else a 1-link clean sibling ("L.A") would win
    and the 1148 links would be re-pointed onto it.

      (a) a data.source='seed' row wins (prefer country-class seed -> oldest -> id);
      (b) else highest canon class priority -> most link_count -> oldest -> id.
    """
    seeds = [m for m in members if m.source == "seed"]
    if seeds:
        return min(seeds, key=lambda m: (_grank(m.cls), m.created_at, m.id))
    return min(members, key=lambda m: (_grank(m.cls), -m.links, m.created_at, m.id))


def _elect_display(survivor: Row, elected_cls: str) -> str:
    """Cleanest surface for the survivor.

    Uses the ``canonicalize_entity`` transform ONLY when it actually cleans the
    name into a non-junk, DIFFERENT surface (article strip "the United Kingdom"
    -> "United Kingdom", alias/demonym collapse). Otherwise keeps the survivor's
    OWN elected surface — so a length-flagged abbreviation ("LA") is not renamed
    to a stranger member surface ("L.A"), and a residue-free established name is
    left untouched.
    """
    disp, _ = canonicalize_entity(survivor.name, elected_cls)
    disp = (disp or "").strip()
    if disp and disp != survivor.name and not is_junk_entity(disp):
        return disp
    # Survivor's own canon is junk-shaped (residue / length) or unchanged: if it
    # carries markup residue, de-residue it; else keep the elected surface.
    if "<" in survivor.name or ">" in survivor.name:
        base = _strip_leading_article(_strip_residue_for_fold(survivor.name))
        d, _ = canonicalize_entity(base, elected_cls)
        d = (d or "").strip()
        if d and not is_junk_entity(d):
            return d
    return survivor.name


# ---------------------------------------------------------------------------
# Core planning
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    merge_map: list[tuple[str, str]]              # (loser_id, survivor_id)
    survivor_rewrite: list[tuple[str, str, str]]  # (survivor_id, name, class)
    junk: list[str]                               # entity_id
    ambiguous: list[dict]                         # review list
    fold_clusters: int
    loser_links: int
    samples: list[dict]


def build_plan(rows: list[Row]) -> Plan:
    clusters: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        fold = identity_fold(r.name)
        # A row with no stable identity (fully stripped away) can't cluster —
        # key it on its own id so it stays a singleton (never merged).
        clusters[fold or f"__singleton__{r.id}"].append(r)

    merge_map: list[tuple[str, str]] = []
    survivor_rewrite: list[tuple[str, str, str]] = []
    junk: list[str] = []
    ambiguous: list[dict] = []
    loser_links = 0
    fold_clusters = 0
    samples: list[dict] = []

    survivors_seen: set[str] = set()
    losers_seen: set[str] = set()

    # Deterministic cluster order.
    for fold in sorted(clusters):
        members = clusters[fold]
        if fold.startswith("__singleton__"):
            continue  # empty-fold singleton — no stable identity, leave untouched

        # (5) SINGLETON — junk ONLY a genuine content-spam row (clock/numeric/
        # residue). A clean singleton or a length-only abbreviation is left
        # untouched (never merged, never junked).
        if len(members) == 1:
            m = members[0]
            if _is_content_junk(m.name):
                junk.append(m.id)
            continue

        # (2) AMBIGUITY GUARD — a genuine country/location HOMONYM (Georgia the
        # country vs the US state): a country-class row AND a location-class row
        # that share the SAME surface name, both with >= 3 links. The same-surface
        # requirement is deliberate — the class-agnostic fold pulls a demonym
        # location ("Iranian") into its country cluster ("Iran"), but that is a
        # MISTYPE to be merged, not a distinct referent; only an IDENTICAL surface
        # under two classes is a true homonym worth protecting.
        country_names = {
            m.name.strip().lower()
            for m in members
            if m.cls == "country" and m.links >= _AMBIGUITY_MIN_LINKS
        }
        location_names = {
            m.name.strip().lower()
            for m in members
            if m.cls == "location" and m.links >= _AMBIGUITY_MIN_LINKS
        }
        if country_names & location_names:
            ambiguous.append({
                "fold": fold,
                "shared_surface": sorted(country_names & location_names),
                "members": [
                    {"id": m.id, "name": m.name, "class": m.cls, "links": m.links}
                    for m in sorted(members, key=lambda x: (-x.links, x.id))
                ],
            })
            continue  # skip — leave every member untouched

        # (5b) A cluster whose EVERY member is content-spam (e.g. a clock-time
        # typed two ways) -> junk them all; no real referent to keep.
        if all(_is_content_junk(m.name) for m in members):
            for m in members:
                junk.append(m.id)
            continue

        # (3) survivor election among ALL members (a length-flagged high-link
        # abbreviation must be eligible), then merge the rest.
        survivor = _elect_survivor(members)
        elected_cls = survivor.cls
        display = _elect_display(survivor, elected_cls)

        fold_clusters += 1
        survivors_seen.add(survivor.id)
        # (4) losers = everything else (incl. junk-shaped members that fold onto
        # the real survivor — a NORMAL loser, re-pointed + tombstoned).
        losers = [m for m in members if m.id != survivor.id]
        for m in sorted(losers, key=lambda x: x.id):
            merge_map.append((m.id, survivor.id))
            losers_seen.add(m.id)
            loser_links += m.links

        # survivor rewrite only when the display name OR the class changes.
        if display != survivor.name or elected_cls != survivor.cls:
            survivor_rewrite.append((survivor.id, display, elected_cls))

        if len(samples) < 20:
            samples.append({
                "fold": fold,
                "survivor": {"id": survivor.id, "name": display, "class": elected_cls,
                             "was": survivor.name},
                "losers": [{"id": m.id, "name": m.name, "class": m.cls, "links": m.links}
                           for m in sorted(losers, key=lambda x: (-x.links, x.id))],
            })

    # (6) PARTITION INTEGRITY — fail loudly if violated.
    junk_set = set(junk)
    both_sl = survivors_seen & losers_seen
    if both_sl:
        raise SystemExit(f"PARTITION VIOLATION: ids both survivor+loser: {sorted(both_sl)[:5]}")
    both_mj = losers_seen & junk_set
    if both_mj:
        raise SystemExit(f"PARTITION VIOLATION: ids both merge+junk: {sorted(both_mj)[:5]}")
    surv_junk = survivors_seen & junk_set
    if surv_junk:
        raise SystemExit(f"PARTITION VIOLATION: ids both survivor+junk: {sorted(surv_junk)[:5]}")
    if len(junk) != len(junk_set):
        raise SystemExit("PARTITION VIOLATION: duplicate id in junk list")
    if len({lid for lid, _ in merge_map}) != len(merge_map):
        raise SystemExit("PARTITION VIOLATION: duplicate loser in merge_map")

    # Two survivors must never target the same (lower(name), class) after rewrite
    # (would race the unique index in a single UPDATE). Check against rewrites.
    rw_targets: dict[tuple[str, str], str] = {}
    for sid, name, cls in survivor_rewrite:
        key = (name.lower(), cls)
        if key in rw_targets and rw_targets[key] != sid:
            raise SystemExit(
                f"PARTITION VIOLATION: two survivors rewrite to {key}: "
                f"{rw_targets[key]} + {sid}"
            )
        rw_targets[key] = sid

    return Plan(
        merge_map=sorted(merge_map, key=lambda t: t[0]),
        survivor_rewrite=sorted(survivor_rewrite, key=lambda t: t[0]),
        junk=sorted(junk_set),
        ambiguous=ambiguous,
        fold_clusters=fold_clusters,
        loser_links=loser_links,
        samples=samples,
    )


# ---------------------------------------------------------------------------
# SQL emission
# ---------------------------------------------------------------------------


def _sql_str(s: str) -> str:
    """Single-quote a text literal for SQL (double any embedded quote)."""
    return "'" + s.replace("'", "''") + "'"


def _values_block(rows: list[str], per_line: int = 1) -> str:
    return ",\n".join(rows)


def emit_sql(plan: Plan) -> str:
    n_merge = len(plan.merge_map)
    n_rewrite = len(plan.survivor_rewrite)
    n_junk = len(plan.junk)

    merge_values = _values_block(
        [f"    ('{lid}'::uuid, '{sid}'::uuid)" for lid, sid in plan.merge_map]
    )
    rewrite_values = _values_block(
        [
            f"    ('{sid}'::uuid, {_sql_str(name)}, {_sql_str(cls)})"
            for sid, name, cls in plan.survivor_rewrite
        ]
    ) or "    (NULL::uuid, NULL::text, NULL::text)"
    junk_values = _values_block(
        [f"    ('{eid}'::uuid)" for eid in plan.junk]
    ) or "    (NULL::uuid)"

    rewrite_insert = (
        f"INSERT INTO _survivor_rewrite (survivor_id, canonical_name, entity_class) VALUES\n{rewrite_values};"
        if plan.survivor_rewrite
        else "-- (no survivor name/class rewrites in this snapshot)"
    )
    junk_insert = (
        f"INSERT INTO _junk (entity_id) VALUES\n{junk_values};"
        if plan.junk
        else "-- (no pure-junk rows in this snapshot)"
    )

    header = f"""-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- 0063_entity_merge.sql  (DQ Phase 4 — entity de-fragmentation)
--
-- GENERATED, DO NOT HAND-EDIT. Produced by
--   scripts/gen_entity_merge_migration.py  (re-run on the same DB snapshot to
--   reproduce byte-for-byte). Merge decisions are FROZEN below as VALUES temp
--   tables; the DML that consumes them is GENERIC (joins live tables) so it is
--   robust to signal_entity_links rows added after the snapshot.
--
-- PROBLEM: entity_profiles fragmented the same referent across entity_class
--   ("Palestine" as country/entity/location/person; "the United Kingdom" vs
--   "United Kingdom"; "African"/"Africans"/"Africa"; residue rows "Iran</p").
--   The class-specific dedup key spawned a new row per NER class; the reference
--   graph (signal_entity_links) split across the fragments. Still accreting
--   ~70-112 rows/day before the paired code fix.
--
-- PAIRED CODE FIX (must ship FIRST or dupes regenerate within days):
--   * src/legba/data/_entity_canon.py — identity_fold() + article/zero-width/
--     residue strip, region-adjective + de-pluralization collapse, palestine
--     gazetteer, extended org/place gazetteers.
--   * src/legba/data/analysts/deterministic_handlers/entity_resolution.py —
--     class-agnostic identity fold cache + any-class PRE-LOOKUP that reuses the
--     highest-priority existing row (promoting class upward, never downward),
--     with a country/location ambiguity guard.
--
-- BLAST RADIUS (this snapshot): {n_merge} loser rows re-pointed + tombstoned,
--   {n_junk} pure-junk rows link-stripped + flagged, {n_rewrite} survivor
--   name/class rewrites, {plan.fold_clusters} fold clusters merged,
--   {len(plan.ambiguous)} ambiguous clusters SKIPPED (country/location).
--
-- REVERSIBILITY (tombstone-only): NO entity_profiles ROW is ever deleted. A
--   loser row is kept and marked data.gc_status='merged' + data.duplicate_of=
--   <survivor id>; a junk row is kept and marked data.gc_status='junk'. Only
--   signal_entity_links rows are deleted (after re-point). To reverse, clear the
--   gc_status/duplicate_of keys and re-resolve — the rows are all still present.
--
-- IDEMPOTENT: re-running is a no-op — the link re-point is INSERT..ON CONFLICT
--   DO NOTHING, the loser-link DELETE finds nothing on a second pass, the
--   survivor/junk UPDATEs set identical values, and the provenance append is
--   NOT-EXISTS-guarded. The migrate runner also skips already-applied files via
--   the legba_data_migrations ledger.
--
-- HOUSE RULE: routed through legba.data.migrate (raw mass-DELETE trips the
--   safety classifier). The runner wraps this file in ONE transaction + records
--   the ledger row — so there is NO inline BEGIN/COMMIT here (matching 0062).
--   TEMP TABLEs are ON COMMIT DROP inside that single wrapping transaction.
--
-- ORDER IS LOAD-BEARING: re-point links (1) BEFORE the loser-link DELETE (5)
--   BEFORE the loser tombstone (6) — entity_gc._clean_orphan_edges deletes the
--   links of any gc_status='merged' row, so the links MUST be re-pointed onto
--   the survivor before the losers are tombstoned or the edges would be lost.
"""

    body = f"""
-- ==========================================================================
-- FROZEN DECISIONS (VALUES temp tables — dropped at COMMIT)
-- ==========================================================================
CREATE TEMP TABLE _merge_map (loser_id uuid, survivor_id uuid) ON COMMIT DROP;
INSERT INTO _merge_map (loser_id, survivor_id) VALUES
{merge_values};

CREATE TEMP TABLE _survivor_rewrite (
    survivor_id uuid, canonical_name text, entity_class text
) ON COMMIT DROP;
{rewrite_insert}

CREATE TEMP TABLE _junk (entity_id uuid) ON COMMIT DROP;
{junk_insert}

CREATE INDEX ON _merge_map (loser_id);
CREATE INDEX ON _merge_map (survivor_id);

-- ==========================================================================
-- (1) RE-POINT links from every loser onto its survivor. GENERIC join so links
--     added after the snapshot are re-pointed too. PK (signal_id, entity_id,
--     role) -> ON CONFLICT DO NOTHING collapses a signal that already linked
--     both loser and survivor.
-- ==========================================================================
INSERT INTO signal_entity_links
    (signal_id, entity_id, role, confidence, analyst_id, analyst_version, run_id, created_at)
SELECT sel.signal_id, m.survivor_id, sel.role, sel.confidence,
       sel.analyst_id, sel.analyst_version, sel.run_id, sel.created_at
  FROM signal_entity_links sel
  JOIN _merge_map m ON sel.entity_id = m.loser_id
ON CONFLICT (signal_id, entity_id, role) DO NOTHING;

-- ==========================================================================
-- (2) SURVIVOR PROVENANCE — union every loser's canonical_name into the
--     survivor's data.merged_aliases and every loser's derived_from marker into
--     the survivor's derived_from. Live-read losers via _merge_map (generic).
-- ==========================================================================
UPDATE entity_profiles s
   SET data = jsonb_set(
                  COALESCE(s.data, '{{}}'::jsonb),
                  '{{merged_aliases}}',
                  (
                    SELECT COALESCE(jsonb_agg(DISTINCT a ORDER BY a), '[]'::jsonb)
                      FROM jsonb_array_elements_text(
                             COALESCE(s.data->'merged_aliases', '[]'::jsonb) || agg.alias_json
                           ) AS a
                  )
              ),
       derived_from = (
           SELECT COALESCE(array_agg(DISTINCT d), '{{}}'::uuid[])
             FROM unnest(s.derived_from || agg.derived) AS d
       ),
       updated_at = now()
  FROM (
      SELECT m.survivor_id AS sid,
             COALESCE(
                 jsonb_agg(DISTINCT to_jsonb(l.canonical_name))
                     FILTER (WHERE l.canonical_name IS NOT NULL),
                 '[]'::jsonb
             ) AS alias_json,
             COALESCE(array_agg(DISTINCT ld) FILTER (WHERE ld IS NOT NULL), '{{}}'::uuid[]) AS derived
        FROM _merge_map m
        JOIN entity_profiles l ON l.id = m.loser_id
        LEFT JOIN LATERAL unnest(l.derived_from) AS ld ON TRUE
       GROUP BY m.survivor_id
  ) agg
 WHERE s.id = agg.sid;

-- ==========================================================================
-- (3) SURVIVOR REWRITE — cleanest surface + elected class. Collision-guarded:
--     only rewrite when NO OTHER row already holds (lower(new_name), new_class),
--     so the CREATE-only unique index idx_entity_profiles_name_class can never
--     be violated (a blocked rewrite is a safe no-op; the merge still stands).
-- ==========================================================================
UPDATE entity_profiles s
   SET canonical_name = r.canonical_name,
       entity_class   = r.entity_class,
       entity_type    = r.entity_class,
       version        = s.version + 1,
       updated_at     = now()
  FROM _survivor_rewrite r
 WHERE s.id = r.survivor_id
   AND (s.canonical_name IS DISTINCT FROM r.canonical_name
        OR s.entity_class IS DISTINCT FROM r.entity_class)
   AND NOT EXISTS (
       SELECT 1 FROM entity_profiles o
        WHERE lower(o.canonical_name) = lower(r.canonical_name)
          AND o.entity_class = r.entity_class
          AND o.id <> r.survivor_id
   );

-- ==========================================================================
-- (4) SURVIVOR PROVENANCE ROW in entity_profile_versions (append-only history).
--     NOT-EXISTS-guarded on (entity_id, event, migration) for idempotency.
-- ==========================================================================
INSERT INTO entity_profile_versions
    (entity_id, version, data, analyst_id, analyst_version, run_id)
SELECT s.id, s.version,
       jsonb_build_object(
           'canonical_name', s.canonical_name,
           'entity_class',   s.entity_class,
           'merged_aliases', COALESCE(s.data->'merged_aliases', '[]'::jsonb),
           'event',          'merged_survivor',
           'migration',      '0063_entity_merge'
       ),
       '0063_entity_merge', NULL, NULL
  FROM entity_profiles s
 WHERE s.id IN (SELECT DISTINCT survivor_id FROM _merge_map)
   AND NOT EXISTS (
       SELECT 1 FROM entity_profile_versions v
        WHERE v.entity_id = s.id
          AND v.data->>'event' = 'merged_survivor'
          AND v.data->>'migration' = '0063_entity_merge'
   );

-- ==========================================================================
-- (5) DELETE the losers' now-redundant links (they were re-pointed in step 1).
-- ==========================================================================
DELETE FROM signal_entity_links
 WHERE entity_id IN (SELECT loser_id FROM _merge_map);

-- ==========================================================================
-- (6) TOMBSTONE the losers (alias-link, NOT a row delete): gc_status='merged' +
--     duplicate_of=<survivor id>. Fully reversible.
-- ==========================================================================
UPDATE entity_profiles l
   SET data = jsonb_set(
                  jsonb_set(COALESCE(l.data, '{{}}'::jsonb),
                            '{{gc_status}}', '"merged"'),
                  '{{duplicate_of}}', to_jsonb(m.survivor_id::text)),
       updated_at = now()
  FROM _merge_map m
 WHERE l.id = m.loser_id;

-- ==========================================================================
-- (7) JUNK — pure junk-shaped rows with no real survivor: delete their links,
--     flag gc_status='junk'. NO row delete (reversible).
-- ==========================================================================
DELETE FROM signal_entity_links
 WHERE entity_id IN (SELECT entity_id FROM _junk);

UPDATE entity_profiles
   SET data = jsonb_set(COALESCE(data, '{{}}'::jsonb), '{{gc_status}}', '"junk"'),
       updated_at = now()
 WHERE id IN (SELECT entity_id FROM _junk);
"""
    return header + body


# ---------------------------------------------------------------------------
# Report emission
# ---------------------------------------------------------------------------


def emit_report(plan: Plan, *, total_rows: int) -> str:
    lines: list[str] = []
    A = lines.append
    A("# Entity-merge migration report (DQ Phase 4 — 0063_entity_merge)")
    A("")
    A("GENERATED by `scripts/gen_entity_merge_migration.py` — deterministic, "
      "offline, from a frozen `entity_profiles` CSV snapshot. Re-run on the same "
      "snapshot to reproduce byte-for-byte.")
    A("")
    A("## Bucket counts")
    A("")
    A(f"| bucket | count |")
    A(f"| --- | ---: |")
    A(f"| entity_profiles rows (snapshot) | {total_rows} |")
    A(f"| fold clusters merged | {plan.fold_clusters} |")
    A(f"| loser rows (re-pointed + tombstoned) | {len(plan.merge_map)} |")
    A(f"| loser links (approx, from snapshot link_count) | {plan.loser_links} |")
    A(f"| survivor name/class rewrites | {len(plan.survivor_rewrite)} |")
    A(f"| pure-junk rows (link-stripped + flagged) | {len(plan.junk)} |")
    A(f"| ambiguous clusters SKIPPED (country/location) | {len(plan.ambiguous)} |")
    A("")
    A("Reversibility: TOMBSTONE-ONLY — zero `entity_profiles` row deletes. "
      "Losers get `data.gc_status='merged'` + `data.duplicate_of`; junk rows get "
      "`data.gc_status='junk'`. Only `signal_entity_links` rows are deleted "
      "(after re-point).")
    A("")

    A("## Ambiguous clusters (NOT merged — manual review)")
    A("")
    if not plan.ambiguous:
        A("_None — no cluster spans a country row and a location row that both "
          "carry >= 3 links in this snapshot._")
    else:
        for amb in plan.ambiguous:
            shared = ", ".join(amb.get("shared_surface", []))
            A(f"- fold `{amb['fold']}` (shared country/location surface: {shared!r}):")
            for m in amb["members"]:
                A(f"    - `{m['id']}` {m['class']:<12} links={m['links']:<4} "
                  f"{m['name']!r}")
    A("")

    A("## Sample clusters (first 20)")
    A("")
    for s in plan.samples:
        surv = s["survivor"]
        rename = "" if surv["name"] == surv["was"] else f"  (renamed from {surv['was']!r})"
        A(f"### fold `{s['fold']}`")
        A(f"- SURVIVOR `{surv['id']}` [{surv['class']}] {surv['name']!r}{rename}")
        for m in s["losers"]:
            A(f"    - loser `{m['id']}` [{m['class']}] links={m['links']} {m['name']!r}")
        A("")

    A("## Verification queries (run AFTER apply)")
    A("")
    A("```sql")
    A("-- Every loser is now tombstoned merged + carries a duplicate_of pointer:")
    A("SELECT count(*) FROM entity_profiles WHERE data->>'gc_status' = 'merged';")
    A(f"--   expect >= {len(plan.merge_map)}")
    A("")
    A("-- No merged/junk row retains any signal_entity_links:")
    A("SELECT count(*) FROM signal_entity_links sel JOIN entity_profiles ep")
    A("  ON ep.id = sel.entity_id")
    A("  WHERE ep.data->>'gc_status' IN ('merged','junk');")
    A("--   expect 0")
    A("")
    A("-- No entity_profiles rows were deleted (row count unchanged):")
    A(f"SELECT count(*) FROM entity_profiles;  -- expect {total_rows}")
    A("")
    A("-- Junk rows flagged:")
    A(f"SELECT count(*) FROM entity_profiles WHERE data->>'gc_status' = 'junk';")
    A(f"--   expect >= {len(plan.junk)}")
    A("```")
    A("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--out-sql", required=True, type=Path)
    ap.add_argument("--out-report", required=True, type=Path)
    args = ap.parse_args(argv)

    rows = _read_csv(args.csv)
    plan = build_plan(rows)

    sql = emit_sql(plan)
    report = emit_report(plan, total_rows=len(rows))

    args.out_sql.write_text(sql, encoding="utf-8")
    args.out_report.write_text(report, encoding="utf-8")

    print(
        f"rows={len(rows)} fold_clusters={plan.fold_clusters} "
        f"losers={len(plan.merge_map)} loser_links={plan.loser_links} "
        f"rewrites={len(plan.survivor_rewrite)} junk={len(plan.junk)} "
        f"ambiguous={len(plan.ambiguous)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
