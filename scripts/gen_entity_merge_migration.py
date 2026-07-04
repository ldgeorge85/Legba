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

SURVIVOR ELECTION (DQ P4 round 2 — LOCKED REDESIGN):
  * Survivor ROW  = MOST signal links -> oldest created_at -> smallest id.
    NOT class-priority, NOT seed-first (a seed can be a 0-link mistyped stub).
    This is the round-1 fix: the class-first election elected a 4-link 'location'
    Trump over the 489-link 'entity' row and re-pointed 16k links the wrong way.
  * Survivor NAME = the canon display of the fold
    (``canonicalize_entity(survivor.canonical_name)[0]``) — so it EQUALS what the
    forward pre-lookup produces for an incoming surface and the merged fold is
    reused by name (never re-forked).
  * Survivor CLASS: ``(cn, cc) = canonicalize_entity(survivor_name, "entity")``.
    ``cc != "entity"``  -> canon is authoritative (gazetteer/org/demonym/region);
    else a seed member's class; else the link-plurality class (the highest-link
    fragment == the survivor row). ``entity_type`` is kept == ``entity_class``.

AMBIGUITY (two lists, both reported, NEITHER merged):
  * country/location HOMONYM — a surface carrying BOTH a country-class row and a
    location-class row (each >= 3 links) is a genuine homonym (Georgia the country
    vs the US state). ONLY those specific rows are excluded; the rest of the
    cluster (entity/person/demonym mistypes) STILL elects a survivor and merges.
  * bare-token homonym — a cluster whose survivor name is a single bare token with
    NO gazetteer/demonym/org backing, spanning >= 2 classes with > 50 combined
    links AND a BALANCED class split (2nd-largest class-link total >= 0.5x the
    largest) is a likely surname collision (Norman the city vs a person). Routed
    to manual review, NOT merged. The balance test is what lets a DOMINANT-referent
    bare token (Trump 490/128/4, Yonhap 751/4) still merge while a genuinely split
    one (Norman 429/305) is held back.

Pipeline:
  1. CLUSTER every row by ``identity_fold(canonical_name)`` (class-agnostic).
  2. PROTECT the country/location homonym rows; elect + merge the rest.
  3. SURVIVOR ELECTION (most-links) among the mergeable members.
  4. LOSERS = mergeable minus survivor -> merge_map(loser, survivor). A NULL-geo
     survivor inherits geo from a deterministic, country-consistent geo-bearing
     loser (C1) so a hard-deleted loser's coordinates are not lost.
  5. JUNK: a content-spam SINGLETON, or a cluster whose EVERY mergeable member is
     content-spam -> junk (hard-DELETEd). A junk-SHAPED row that folds ONTO a real
     survivor ("Iran</p" -> Iran) is a NORMAL loser (re-point + hard-delete).
     A fold whose ELECTED survivor NAME is itself junk (sports scaffolding /
     number-word; article variants defeat the all-junk gate) routes the WHOLE fold
     to junk rather than keep a junk survivor with consolidated links (B1).
  6. PARTITION INTEGRITY asserts (no id both survivor+loser, both merge+junk).
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
from dataclasses import dataclass, field
from pathlib import Path

try:  # pycountry is already a canon dependency; used to normalise geo_country.
    import pycountry
except Exception:  # pragma: no cover - defensive; canon import would fail first
    pycountry = None

from legba.data._entity_canon import (
    DEFAULT_CLASS,
    _strip_leading_article,
    _strip_name,
    canonicalize_entity,
    identity_fold,
    is_junk_entity,
)


def _country_iso2(c: str | None) -> str | None:
    """Normalise a geo_country value (ISO-2 code OR full/common name) to an ISO-2
    code so 'AE' and 'United Arab Emirates' compare EQUAL. Returns None for an
    empty value, and a lowercased passthrough when pycountry cannot resolve the
    string (e.g. 'Congo-Brazzaville') — a conservative fallback that only ever
    causes a country check to REFUSE (never a wrong match)."""
    if not c:
        return None
    s = str(c).strip()
    if not s:
        return None
    if pycountry is None:
        return s.lower()
    try:
        if len(s) == 2:
            rec = pycountry.countries.get(alpha_2=s.upper())
            return rec.alpha_2 if rec else s.upper()
        return pycountry.countries.lookup(s).alpha_2
    except LookupError:
        return s.lower()

# ---------------------------------------------------------------------------
# Ambiguity thresholds.
# ---------------------------------------------------------------------------
#: A country-class row and a location-class row at the SAME surface each need at
#: least this many links to count as a genuine country/location homonym.
_AMBIGUITY_MIN_LINKS = 3
#: A bare-token cluster needs MORE than this many combined links to be considered
#: for the manual homonym review list.
_HOMONYM_MIN_TOTAL_LINKS = 50
#: … AND its class split must be BALANCED: the second-largest per-class link total
#: must be at least this fraction of the largest. This is the discriminator that
#: keeps a DOMINANT-referent bare token merging (Trump 128/490 = 0.26 < 0.5,
#: Yonhap 4/751 = 0.005) while holding back a genuinely split one for review
#: (Norman 305/429 = 0.71 >= 0.5). Without it the literal "single token, >=2
#: classes, >50 links" rule would wrongly hold back Trump/Yonhap (which the DQ
#: ground-truth requires to merge), so the balance gate reconciles the two.
_HOMONYM_BALANCE_FRAC = 0.5


def _is_content_junk(name: str) -> bool:
    """True for GENUINE NER spam (clock-time / numeric / money / quantifier /
    age / sports / stopword / residual-markup), as opposed to a bare length<=2
    abbreviation.

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
    # Geo carried so a hard-deleted geo-bearing loser can backfill a NULL-geo
    # survivor (DQ P4 r3 / C1). All optional — absent columns parse to None.
    geo_lat: float | None = None
    geo_lon: float | None = None
    geo_country: str | None = None
    geo_region: str | None = None
    completeness: float | None = None


def _opt_float(v: str | None) -> float | None:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _opt_text(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


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
                    geo_lat=_opt_float(r.get("geo_lat")),
                    geo_lon=_opt_float(r.get("geo_lon")),
                    geo_country=_opt_text(r.get("geo_country")),
                    geo_region=_opt_text(r.get("geo_region")),
                    completeness=_opt_float(r.get("completeness_score")),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Survivor / display / class election
# ---------------------------------------------------------------------------


def _elect_survivor(members: list[Row]) -> Row:
    """Elect the survivor ROW: MOST links -> oldest created_at -> smallest id.

    NOT class-priority, NOT seed-first. The highest-link fragment is the row the
    de-fragmented graph should converge on; anything else re-points the majority
    of the reference edges onto a minority stub (the round-1 16k-link bloat).
    """
    return min(members, key=lambda m: (-m.links, m.created_at, m.id))


def _survivor_is_junk(survivor_name: str) -> bool:
    """True when the ELECTED survivor's display name is INHERENT non-referent junk
    (sports scaffolding / quantifier / clock / numeral / bare number-word /
    stopword), so the whole fold must route to JUNK rather than keep a junk
    survivor with consolidated links (DQ P4 r3 / B1). Article variants ("The World
    Cup" / "A World Cup") defeat the pre-election all-junk gate, so a leading
    article is stripped and the check re-run.

    Deliberately EXCLUDES two ``is_junk_entity`` classes that must NEVER sweep a
    whole fold (they would delete a LEGIT entity, violating the B1 guard):

      * length <= 2 abbreviations (``LA`` / ``DC`` / ``Xi`` / ``G7`` / ``DW``) —
        real, often high-reference entities the length rule flags; and
      * HTML-residue surfaces (``"Daniel Noboa.</p"`` / ``"Jeanne Shaheen</a"``) —
        a DIRTY surface of a real referent, which round-2 keeps (re-pointed), not a
        whole-fold junk.

    Legit short forms (US/UK/EU/UN/WHO) are additionally exempted INSIDE
    ``is_junk_entity`` (alias / demonym / country / org), so they are never junk."""
    cand = (survivor_name or "").strip()
    if not cand:
        return False
    for probe in (cand, _strip_leading_article(cand)):
        if not probe or "<" in probe or ">" in probe:
            continue                        # residue: a real referent's dirty surface
        if len(_strip_name(probe)) <= 2:
            continue                        # length<=2 abbreviation: a real entity
        if is_junk_entity(probe):
            return True
    return False


def _pick_geo_donor(survivor: Row, losers: list[Row]) -> Row | None:
    """Deterministically pick a geo-bearing loser to backfill the survivor's geo.

    A hard-deleted loser can carry the referent's coordinates that the most-links
    survivor lacks (DQ P4 r3 / C1). Only a loser with BOTH ``geo_lat`` and
    ``geo_lon`` qualifies as a donor (coordinates copied as a pair, never mixed).

    Cross-country guard — mirrors ``entity_resolution``'s ON-CONFLICT geo rule: if
    the survivor already has a ``geo_country``, a donor whose ``geo_country``
    DISAGREES is NEVER eligible (a country mismatch must not overwrite). Country
    strings are normalised to ISO-2 first, so 'AE' and 'United Arab Emirates' are
    recognised as the SAME country (not a spurious mismatch). Among the eligible
    donors, prefer one whose country MATCHES the survivor's, then the smallest id
    — stable + deterministic."""
    sc = _country_iso2(survivor.geo_country)
    cands = [m for m in losers if m.geo_lat is not None and m.geo_lon is not None]
    if sc:
        cands = [m for m in cands if _country_iso2(m.geo_country) in (None, sc)]
    if not cands:
        return None
    if sc:
        return min(cands, key=lambda m: (
            0 if _country_iso2(m.geo_country) == sc else 1, m.id))
    return min(cands, key=lambda m: m.id)


def _survivor_name(survivor: Row) -> str:
    """Canon display of the survivor's surface — what the forward pre-lookup will
    produce for an incoming mention of the same fold, so the merged row is reused
    by name (never re-forked). Guarded: if the canon drops the survivor as junk
    (residue / stopword), fall back to its stripped surface so the name is stable
    and non-empty."""
    disp, _ = canonicalize_entity(survivor.name, DEFAULT_CLASS)
    disp = (disp or "").strip()
    if not disp or is_junk_entity(disp):
        disp = _strip_name(survivor.name) or survivor.name
    return disp


def _survivor_class(
    survivor_name: str,
    survivor: Row,
    mergeable: list[Row],
    protected_slots: set[tuple[str, str]],
) -> str:
    """Elect the survivor CLASS (canon-authoritative, then seed, then plurality).

      * ``canonicalize_entity(survivor_name, "entity")`` -> if the canon settles a
        NON-generic class (country/org/location/…), that is authoritative.
      * else if a seed member is present, adopt the seed's class (curated).
      * else the link-plurality class — the survivor IS the highest-link fragment,
        so ``survivor.cls`` is that plurality class.

    Protected-collision revert: if the elected class would land the survivor on a
    (lower(name), class) slot ALREADY held by a protected country/location row of
    the SAME cluster (e.g. a "Turkey"/entity survivor canon-typed 'country' while
    the protected "Turkey"/country row still holds that slot), keep the survivor's
    STORED class instead — the rewrite would otherwise be a doomed no-op blocked
    by the unique index. ``entity_type`` is kept == ``entity_class`` by the caller.
    """
    _cn, cc = canonicalize_entity(survivor_name, DEFAULT_CLASS)
    if cc != DEFAULT_CLASS:
        cls = cc
    else:
        seeds = [m for m in mergeable if m.source == "seed"]
        if seeds:
            cls = min(seeds, key=lambda m: (-m.links, m.created_at, m.id)).cls
        else:
            cls = survivor.cls  # link-plurality (survivor = highest links)
    if cls != survivor.cls and (survivor_name.strip().lower(), cls) in protected_slots:
        cls = survivor.cls
    return cls


def _protected_rows(members: list[Row]) -> tuple[set[str], list[Row], list[str]]:
    """The genuine country/location homonym rows to EXCLUDE from the merge.

    A surface carrying BOTH a country-class row (>= 3 links) and a location-class
    row (>= 3 links) is a genuine homonym (Georgia the country vs the US state).
    Only those specific rows are protected; the rest of the cluster still merges.
    Returns (protected_ids, protected_rows, shared_surfaces).
    """
    by_surface: dict[str, dict[str, list[Row]]] = defaultdict(
        lambda: {"country": [], "location": []}
    )
    for m in members:
        if m.cls in ("country", "location") and m.links >= _AMBIGUITY_MIN_LINKS:
            by_surface[m.name.strip().lower()][m.cls].append(m)

    protected_ids: set[str] = set()
    protected_rows: list[Row] = []
    shared: list[str] = []
    for surf in sorted(by_surface):
        d = by_surface[surf]
        if d["country"] and d["location"]:
            shared.append(surf)
            for m in d["country"] + d["location"]:
                if m.id not in protected_ids:
                    protected_ids.add(m.id)
                    protected_rows.append(m)
    return protected_ids, protected_rows, shared


def _is_manual_homonym(survivor_name: str, cc: str, mergeable: list[Row]) -> bool:
    """A single bare token, no canon backing, split ACROSS classes with a BALANCED
    link distribution -> a likely surname collision (Norman the city vs a person).
    See :data:`_HOMONYM_BALANCE_FRAC`."""
    if cc != DEFAULT_CLASS:
        return False  # canon recognises the name (gazetteer/org/demonym) — not a homonym
    if not survivor_name.strip() or " " in survivor_name.strip():
        return False  # only a SINGLE bare token
    per_class: dict[str, int] = defaultdict(int)
    for m in mergeable:
        per_class[m.cls] += m.links
    if len(per_class) < 2:
        return False  # a single class — nothing to be ambiguous between
    if sum(per_class.values()) <= _HOMONYM_MIN_TOTAL_LINKS:
        return False
    tots = sorted(per_class.values(), reverse=True)
    if tots[0] <= 0:
        return False
    return (tots[1] / tots[0]) >= _HOMONYM_BALANCE_FRAC


# ---------------------------------------------------------------------------
# Core planning
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    merge_map: list[tuple[str, str]]              # (loser_id, survivor_id)
    survivor_rewrite: list[tuple[str, str, str]]  # (survivor_id, name, class)
    junk: list[str]                               # entity_id
    ambiguous_geo: list[dict]                     # country/location homonyms
    ambiguous_token: list[dict]                   # bare-token surname homonyms
    fold_clusters: int
    loser_links: int
    # (survivor_id, geo_lat, geo_lon, geo_country, geo_region, completeness) —
    # geo COALESCE'd onto a NULL-geo survivor from a geo-bearing loser (C1).
    survivor_geo: list[tuple] = field(default_factory=list)
    # folds whose elected survivor NAME was itself junk -> routed wholly to junk
    # instead of keeping a junk survivor (B1).
    junk_from_survivor: int = 0
    samples: list[dict] = field(default_factory=list)


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
    ambiguous_geo: list[dict] = []
    ambiguous_token: list[dict] = []
    survivor_geo: list[tuple] = []
    loser_links = 0
    fold_clusters = 0
    junk_from_survivor = 0
    samples: list[dict] = []

    survivors_seen: set[str] = set()
    losers_seen: set[str] = set()

    for fold in sorted(clusters):
        members = clusters[fold]
        if fold.startswith("__singleton__"):
            continue  # empty-fold singleton — no stable identity, leave untouched

        # (5) SINGLETON — junk ONLY a genuine content-spam row (clock/numeric/
        # stopword/residue). A clean singleton or a length-only abbreviation is
        # left untouched (never merged, never junked).
        if len(members) == 1:
            m = members[0]
            if _is_content_junk(m.name):
                junk.append(m.id)
            continue

        # (2) COUNTRY/LOCATION HOMONYM — protect ONLY those specific rows, still
        # merge the rest of the cluster onto its own survivor.
        protected_ids, protected_rows, shared = _protected_rows(members)
        if protected_rows:
            ambiguous_geo.append({
                "fold": fold,
                "shared_surface": shared,
                "protected": [
                    {"id": m.id, "name": m.name, "class": m.cls, "links": m.links}
                    for m in sorted(protected_rows, key=lambda x: (-x.links, x.id))
                ],
            })
        protected_slots = {
            (m.name.strip().lower(), m.cls) for m in protected_rows
        }
        mergeable = [m for m in members if m.id not in protected_ids]

        # Fewer than two mergeable members -> nothing to fold. A lone content-junk
        # leftover is still junked.
        if len(mergeable) < 2:
            if len(mergeable) == 1 and _is_content_junk(mergeable[0].name):
                junk.append(mergeable[0].id)
            continue

        # (5b) EVERY mergeable member is content-spam -> junk them all; no real
        # referent to keep.
        if all(_is_content_junk(m.name) for m in mergeable):
            for m in mergeable:
                junk.append(m.id)
            continue

        # (3) survivor election among the mergeable members (MOST links).
        survivor = _elect_survivor(mergeable)
        survivor_name = _survivor_name(survivor)
        _cn, cc = canonicalize_entity(survivor_name, DEFAULT_CLASS)

        # (2c) JUNK-NAMED SURVIVOR — the highest-link fragment's own display name
        # is itself content junk (World Cup / Group F / a bare number-word), so
        # article/quantifier variants defeated the pre-election all-junk gate and
        # a junk survivor would otherwise be KEPT with the fold's consolidated
        # links. Route the ENTIRE fold to junk (hard-delete all members + links);
        # do NOT elect a junk survivor. Gated on is_junk_entity, so US/UK/EU/UN
        # and real names are never swept.
        if _survivor_is_junk(survivor_name):
            for m in mergeable:
                junk.append(m.id)
            junk_from_survivor += 1
            continue

        # (2b) BARE-TOKEN surname homonym -> manual review, do NOT merge.
        if _is_manual_homonym(survivor_name, cc, mergeable):
            ambiguous_token.append({
                "fold": fold,
                "survivor_name": survivor_name,
                "members": [
                    {"id": m.id, "name": m.name, "class": m.cls, "links": m.links}
                    for m in sorted(mergeable, key=lambda x: (-x.links, x.id))
                ],
            })
            continue

        survivor_class = _survivor_class(
            survivor_name, survivor, mergeable, protected_slots
        )

        fold_clusters += 1
        survivors_seen.add(survivor.id)
        # (4) losers = the mergeable rest (incl. junk-shaped members that fold onto
        # the real survivor — a NORMAL loser, re-pointed then hard-deleted).
        losers = [m for m in mergeable if m.id != survivor.id]
        for m in sorted(losers, key=lambda x: x.id):
            merge_map.append((m.id, survivor.id))
            losers_seen.add(m.id)
            loser_links += m.links

        # (C1) GEO BACKFILL — hard-deleting a geo-bearing loser blanks the
        # referent's coordinates when the most-links survivor has NULL geo. Pick a
        # deterministic, country-consistent geo-bearing loser and record its geo so
        # the emitted SQL can COALESCE the survivor's NULL geo BEFORE the delete.
        donor = _pick_geo_donor(survivor, losers)
        if donor is not None:
            fills_geo = (
                (survivor.geo_lat is None and survivor.geo_lon is None
                 and donor.geo_lat is not None and donor.geo_lon is not None)
                or (survivor.geo_country is None and bool(donor.geo_country))
                or (survivor.geo_region is None and bool(donor.geo_region))
            )
            if fills_geo:
                # Emit the survivor's OWN snapshot country as the guard sentinel
                # when it has one (the donor is already ISO-2-consistent with it),
                # so the emitted SQL's string-equality cross-country CASE passes
                # for this verified-consistent donor AND still blocks a live
                # country DRIFT between snapshot and apply. When the survivor has
                # NO country, emit the donor's country so the COALESCE backfills
                # it. The lat/lon/region/completeness are always the donor's.
                emit_country = survivor.geo_country or donor.geo_country
                survivor_geo.append((
                    survivor.id, donor.geo_lat, donor.geo_lon,
                    emit_country, donor.geo_region, donor.completeness,
                ))

        # survivor rewrite only when the display name OR the class changes.
        if survivor_name != survivor.name or survivor_class != survivor.cls:
            survivor_rewrite.append((survivor.id, survivor_name, survivor_class))

        if len(samples) < 20:
            samples.append({
                "fold": fold,
                "survivor": {"id": survivor.id, "name": survivor_name,
                             "class": survivor_class, "was": survivor.name,
                             "was_class": survivor.cls, "links": survivor.links},
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
    # Geo backfill only ever targets an elected merge survivor (never a loser or
    # junk row), and at most once per survivor.
    geo_ids = [sid for sid, *_ in survivor_geo]
    if len(set(geo_ids)) != len(geo_ids):
        raise SystemExit("PARTITION VIOLATION: duplicate survivor in survivor_geo")
    bad_geo = (set(geo_ids) - survivors_seen) | (set(geo_ids) & junk_set)
    if bad_geo:
        raise SystemExit(
            f"PARTITION VIOLATION: survivor_geo id not a live survivor: {sorted(bad_geo)[:5]}"
        )

    # Two survivors must never rewrite to the same (lower(name), class).
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
        ambiguous_geo=sorted(ambiguous_geo, key=lambda a: a["fold"]),
        ambiguous_token=sorted(ambiguous_token, key=lambda a: a["fold"]),
        fold_clusters=fold_clusters,
        loser_links=loser_links,
        survivor_geo=sorted(survivor_geo, key=lambda t: t[0]),
        junk_from_survivor=junk_from_survivor,
        samples=samples,
    )


# ---------------------------------------------------------------------------
# SQL emission
# ---------------------------------------------------------------------------


def _sql_str(s: str) -> str:
    """Single-quote a text literal for SQL (double any embedded quote)."""
    return "'" + s.replace("'", "''") + "'"


def _sql_num(v: float | None) -> str:
    """Emit a NULL or a deterministic float literal for a numeric column."""
    return "NULL" if v is None else repr(float(v))


def _sql_txt(v: str | None) -> str:
    """Emit a NULL or a quoted text literal (None / empty -> NULL)."""
    return "NULL" if v is None or str(v).strip() == "" else _sql_str(str(v))


def _values_block(rows: list[str]) -> str:
    return ",\n".join(rows)


def emit_sql(plan: Plan) -> str:
    n_merge = len(plan.merge_map)
    n_rewrite = len(plan.survivor_rewrite)
    n_junk = len(plan.junk)
    n_deleted = n_merge + n_junk

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
    geo_values = _values_block(
        [
            f"    ('{sid}'::uuid, {_sql_num(lat)}, {_sql_num(lon)}, "
            f"{_sql_txt(country)}, {_sql_txt(region)}, {_sql_num(comp)})"
            for sid, lat, lon, country, region, comp in plan.survivor_geo
        ]
    ) or "    (NULL::uuid, NULL::double precision, NULL::double precision, NULL::text, NULL::text, NULL::real)"

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
    geo_insert = (
        "INSERT INTO _survivor_geo\n"
        "    (survivor_id, geo_lat, geo_lon, geo_country, geo_region, completeness_score) VALUES\n"
        f"{geo_values};"
        if plan.survivor_geo
        else "-- (no NULL-geo survivors with a geo-bearing loser in this snapshot)"
    )
    n_geo = len(plan.survivor_geo)
    n_junk_surv = plan.junk_from_survivor

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
--   ("Trump" as entity/person/location; "the United Kingdom" vs "United
--   Kingdom"; "African"/"Africans"/"Africa"; residue rows "Iran</p"). The
--   class-specific dedup key spawned a new row per NER class; the reference
--   graph (signal_entity_links) split across the fragments.
--
-- PAIRED CODE FIX (must ship FIRST or dupes regenerate within days):
--   * src/legba/data/_entity_canon.py — identity_fold() + article/zero-width/
--     residue strip, region-adjective + de-pluralization collapse, palestine
--     gazetteer, extended org/place gazetteers, article/stopword junk gate.
--   * src/legba/data/analysts/deterministic_handlers/entity_resolution.py —
--     class-agnostic identity fold cache + any-class PRE-LOOKUP that reuses the
--     ACTIVE survivor (gc_status merged/junk rows are excluded, so a forward
--     write can never re-animate a de-fragmentation loser), promoting class
--     upward via canon (never downward).
--
-- SURVIVOR ELECTION = MOST links -> oldest -> smallest id; survivor NAME/CLASS
--   are canon-authoritative so the row the forward code converges on IS the
--   survivor (no re-divergence). See the generator docstring.
--
-- BLAST RADIUS (this snapshot): {n_merge} loser rows re-pointed then HARD-DELETED,
--   {n_junk} pure-junk rows HARD-DELETED (of which {n_junk_surv} folds were routed
--   to junk because their elected survivor NAME was itself junk), {n_rewrite}
--   survivor name/class rewrites, {n_geo} survivors geo-backfilled from a
--   geo-bearing loser, {plan.fold_clusters} fold clusters merged,
--   {len(plan.ambiguous_geo)} country/location homonyms + {len(plan.ambiguous_token)}
--   bare-token homonyms held for review (NOT merged). Net entity_profiles row
--   delta: -{n_deleted}.
--
-- ==========================================================================
-- HARD-DELETE — REVERSIBILITY IS AN OPERATOR PRE-APPLY BACKUP, NOT A TOMBSTONE.
-- ==========================================================================
--   This migration DELETEs the loser + junk rows from entity_profiles (their
--   signal_entity_links + entity_profile_versions rows cascade away via the
--   ON DELETE CASCADE FKs). There is NO in-table undo. BEFORE APPLYING, the
--   operator MUST take a pg_dump backup of the three affected tables so the
--   change is reversible by restore:
--
--     pg_dump -U legba -d legba -Fc \\
--       -t entity_profiles -t signal_entity_links -t entity_profile_versions \\
--       -f entity_merge_0063_preapply.dump
--
--   (proposed_edges / facts / nexuses reference entities by TEXT name, not id,
--   so a deleted loser's name simply orphans its proposed_edges — entity_gc
--   quarantines those on its next tick; acceptable dup/variant cleanup.)
--
-- APPLY-WINDOW QUIESCENCE (recommended): applying is a SINGLE sub-second
--   transaction, but a concurrent entity_resolution write DURING that window
--   could insert a signal_entity_link pointing at a loser row that step (4) then
--   CASCADE-deletes — a one-time, self-healing transient (the next resolution
--   tick re-links the signal to the surviving row by name). To avoid it entirely,
--   apply during a quiet window OR briefly pause the entity_resolution cadence
--   for the (sub-second) apply. This is a recommendation, not a correctness
--   requirement — the transaction is atomic and no live row is left dangling.
--
-- IDEMPOTENT: re-running is a no-op — the link re-point is INSERT..ON CONFLICT
--   DO NOTHING, the loser/junk DELETEs find nothing on a second pass, the
--   survivor rewrite sets identical values, the geo backfill COALESCEs only a
--   still-NULL survivor geo (GREATEST never regresses completeness), the version
--   append is NOT-EXISTS-guarded on event='merge_0063', and the version bump is
--   guarded to fire only while the survivor still sits below its merge-version
--   number. The migrate runner also skips already-applied files via the
--   legba_data_migrations ledger.
--
-- HOUSE RULE: routed through legba.data.migrate (raw mass-DELETE trips the
--   safety classifier). The runner wraps this file in ONE transaction + records
--   the ledger row — so there is NO inline BEGIN/COMMIT here (matching 0062).
--   TEMP TABLEs are ON COMMIT DROP inside that single wrapping transaction.
--
-- ORDER IS LOAD-BEARING:
--   (1)  re-point loser links onto the survivor,
--   (2)  copy loser aliases/derived_from onto the survivor  -- BEFORE the losers
--   (2b) backfill NULL survivor geo from a geo-bearing loser -- are deleted,
--   (3)  append the survivor merge-version row (at version+1),
--   (3b) bump the survivor's entity_profiles.version to that number,
--   (4)  DELETE the loser rows (CASCADE takes their leftover links + versions),
--   (5)  rewrite the survivor name/class NOW that the loser slots are freed,
--   (6)  DELETE the junk rows (CASCADE takes their links + versions).
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

CREATE TEMP TABLE _survivor_geo (
    survivor_id uuid, geo_lat double precision, geo_lon double precision,
    geo_country text, geo_region text, completeness_score real
) ON COMMIT DROP;
{geo_insert}

CREATE INDEX ON _merge_map (loser_id);
CREATE INDEX ON _merge_map (survivor_id);

-- ==========================================================================
-- (1) RE-POINT links from every loser onto its survivor. GENERIC join so links
--     added after the snapshot are re-pointed too. PK (signal_id, entity_id,
--     role) -> ON CONFLICT DO NOTHING collapses a signal that already linked
--     both loser and survivor. (The losers' now-redundant original links are
--     removed by the CASCADE when the loser rows are DELETEd in step 4.)
-- ==========================================================================
INSERT INTO signal_entity_links
    (signal_id, entity_id, role, confidence, analyst_id, analyst_version, run_id, created_at)
SELECT sel.signal_id, m.survivor_id, sel.role, sel.confidence,
       sel.analyst_id, sel.analyst_version, sel.run_id, sel.created_at
  FROM signal_entity_links sel
  JOIN _merge_map m ON sel.entity_id = m.loser_id
ON CONFLICT (signal_id, entity_id, role) DO NOTHING;

-- ==========================================================================
-- (2) SURVIVOR PROVENANCE — read the losers BEFORE they are deleted: union every
--     loser's canonical_name into the survivor's data.merged_aliases and every
--     loser's derived_from marker into the survivor's derived_from (deduped,
--     ORDER BY for determinism).
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
           SELECT COALESCE(array_agg(DISTINCT d ORDER BY d), '{{}}'::uuid[])
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
-- (2b) SURVIVOR GEO BACKFILL — a hard-deleted geo-bearing loser can carry the
--     referent's coordinates the most-links survivor lacks, so blanking it would
--     lose the geo. COALESCE the survivor's NULL geo from the frozen donor row
--     (a deterministic, country-consistent geo-bearing member of the same fold),
--     BEFORE the loser DELETE in step 4. Cross-country guard mirrors the
--     entity_resolution ON-CONFLICT geo rule: lat/lon/region are inherited ONLY
--     when the countries are consistent (a mismatch never overwrites). lat+lon
--     are copied TOGETHER (both from the same donor, only when BOTH are NULL) so
--     they can never be mixed across donors. completeness_score = GREATEST (never
--     regressed). Idempotent: COALESCE fills only a still-NULL value.
-- ==========================================================================
UPDATE entity_profiles s
   SET geo_lat = CASE
                     WHEN (s.geo_country IS NULL OR g.geo_country IS NULL
                           OR lower(s.geo_country) = lower(g.geo_country))
                          AND s.geo_lat IS NULL AND s.geo_lon IS NULL
                     THEN g.geo_lat ELSE s.geo_lat END,
       geo_lon = CASE
                     WHEN (s.geo_country IS NULL OR g.geo_country IS NULL
                           OR lower(s.geo_country) = lower(g.geo_country))
                          AND s.geo_lat IS NULL AND s.geo_lon IS NULL
                     THEN g.geo_lon ELSE s.geo_lon END,
       geo_country = COALESCE(s.geo_country, g.geo_country),
       geo_region = CASE
                     WHEN s.geo_country IS NULL OR g.geo_country IS NULL
                          OR lower(s.geo_country) = lower(g.geo_country)
                     THEN COALESCE(s.geo_region, g.geo_region) ELSE s.geo_region END,
       completeness_score = GREATEST(
                                COALESCE(s.completeness_score, 0),
                                COALESCE(g.completeness_score, 0)),
       updated_at = now()
  FROM _survivor_geo g
 WHERE s.id = g.survivor_id;

-- ==========================================================================
-- (3) SURVIVOR MERGE-VERSION ROW in entity_profile_versions (append-only) at
--     version+1. This preserves the merge record even though the losers' own
--     version rows cascade away in step 4, and — inserting ABOVE the survivor's
--     current version rather than duplicating it — keeps (entity_id, version)
--     monotonic + unique. NOT-EXISTS-guarded on (entity_id, event='merge_0063')
--     for idempotency. Step 3b then advances entity_profiles.version to match.
-- ==========================================================================
INSERT INTO entity_profile_versions
    (entity_id, version, data, analyst_id, analyst_version, run_id)
SELECT s.id, s.version + 1,
       jsonb_build_object(
           'canonical_name', s.canonical_name,
           'entity_class',   s.entity_class,
           'merged_aliases', COALESCE(s.data->'merged_aliases', '[]'::jsonb),
           'merged_losers',  (SELECT count(*) FROM _merge_map m WHERE m.survivor_id = s.id),
           'event',          'merge_0063',
           'migration',      '0063_entity_merge'
       ),
       '0063_entity_merge', NULL, NULL
  FROM entity_profiles s
 WHERE s.id IN (SELECT DISTINCT survivor_id FROM _merge_map)
   AND NOT EXISTS (
       SELECT 1 FROM entity_profile_versions v
        WHERE v.entity_id = s.id
          AND v.data->>'event' = 'merge_0063'
   );

-- ==========================================================================
-- (3b) VERSION BUMP — advance EVERY merged survivor's entity_profiles.version to
--     the merge-version row's number (step 3 appended it at version+1) so
--     (entity_id, version) stays monotonic + unique. Guarded to fire exactly
--     once: only a survivor still sitting BELOW its merge_0063 version is bumped,
--     so a forced re-run is a no-op (version already == the merge-version number).
--     The step-5 rewrite no longer touches version (this owns it, for ALL merged
--     survivors, not only the renamed ones).
-- ==========================================================================
UPDATE entity_profiles s
   SET version    = s.version + 1,
       updated_at = now()
 WHERE s.id IN (SELECT DISTINCT survivor_id FROM _merge_map)
   AND s.version < (
       SELECT v.version FROM entity_profile_versions v
        WHERE v.entity_id = s.id
          AND v.data->>'event' = 'merge_0063'
   );

-- ==========================================================================
-- (4) HARD-DELETE the loser rows. The ON DELETE CASCADE FKs remove their leftover
--     signal_entity_links (already re-pointed in step 1) and their
--     entity_profile_versions in the same statement.
-- ==========================================================================
DELETE FROM entity_profiles WHERE id IN (SELECT loser_id FROM _merge_map);

-- ==========================================================================
-- (5) SURVIVOR REWRITE — cleanest canon surface + elected class, NOW that the
--     loser slots are freed (so the cluster's own losers no longer block it). The
--     NOT-EXISTS collision guard only catches a rare CROSS-cluster / protected row
--     still holding (lower(new_name), new_class); a blocked rewrite is a safe
--     no-op (the merge still stands). entity_type is kept == entity_class.
-- ==========================================================================
UPDATE entity_profiles s
   SET canonical_name = r.canonical_name,
       entity_class   = r.entity_class,
       entity_type    = r.entity_class,
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
-- (6) HARD-DELETE the pure-junk rows (content-spam with no real survivor). The
--     CASCADE removes their links + versions.
-- ==========================================================================
DELETE FROM entity_profiles WHERE id IN (SELECT entity_id FROM _junk);
"""
    return header + body


# ---------------------------------------------------------------------------
# Report emission
# ---------------------------------------------------------------------------


def emit_report(plan: Plan, *, total_rows: int) -> str:
    n_deleted = len(plan.merge_map) + len(plan.junk)
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
    A(f"| loser rows (re-pointed + HARD-DELETED) | {len(plan.merge_map)} |")
    A(f"| loser links (approx, from snapshot link_count) | {plan.loser_links} |")
    A(f"| survivor name/class rewrites | {len(plan.survivor_rewrite)} |")
    A(f"| pure-junk rows (HARD-DELETED) | {len(plan.junk)} |")
    A(f"| &nbsp;&nbsp;↳ folds routed to junk (elected survivor NAME was junk) | {plan.junk_from_survivor} |")
    A(f"| survivors geo-backfilled from a geo-bearing loser | {len(plan.survivor_geo)} |")
    A(f"| country/location homonyms held (NOT merged) | {len(plan.ambiguous_geo)} |")
    A(f"| bare-token homonyms held (NOT merged) | {len(plan.ambiguous_token)} |")
    A(f"| **net entity_profiles row delta** | **-{n_deleted}** |")
    A("")
    A("Reversibility: **HARD-DELETE** — loser + junk rows are removed from "
      "`entity_profiles` (links + version rows cascade). The operator MUST "
      "`pg_dump` `entity_profiles` + `signal_entity_links` + "
      "`entity_profile_versions` BEFORE applying; restore is the undo path. "
      "Apply during a quiet window (or briefly pause the entity_resolution "
      "cadence) — a concurrent write during the sub-second apply could link a "
      "signal to a loser that the CASCADE then deletes (a self-healing transient).")
    A("")

    A("## Survivor geo backfill (C1 — a hard-deleted loser's geo preserved)")
    A("")
    if not plan.survivor_geo:
        A("_None — no NULL-geo survivor had a country-consistent geo-bearing loser "
          "in this snapshot._")
    else:
        A(f"{len(plan.survivor_geo)} survivor(s) inherit geo from a geo-bearing "
          "loser (lat+lon copied together from one donor; never across a country "
          "mismatch; completeness_score = GREATEST):")
        A("")
        for sid, lat, lon, country, region, comp in plan.survivor_geo:
            latlon = f"{lat},{lon}" if lat is not None and lon is not None else "—"
            A(f"    - `{sid}` <- geo=({latlon}) country={country!r} region={region!r}")
    A("")

    A("## Country/location homonyms (protected rows NOT merged; the rest of the "
      "cluster still merges)")
    A("")
    if not plan.ambiguous_geo:
        A("_None — no cluster carries a country row AND a location row at the same "
          "surface that both reach >= 3 links in this snapshot._")
    else:
        for amb in plan.ambiguous_geo:
            shared = ", ".join(repr(s) for s in amb.get("shared_surface", []))
            A(f"- fold `{amb['fold']}` (shared surface: {shared}):")
            for m in amb["protected"]:
                A(f"    - `{m['id']}` {m['class']:<12} links={m['links']:<4} "
                  f"{m['name']!r}")
    A("")

    A("## Bare-token homonyms (manual review — NOT merged)")
    A("")
    if not plan.ambiguous_token:
        A("_None — no bare-token cluster spans >= 2 classes with a balanced "
          "(2nd/1st >= 0.5), > 50-link split in this snapshot._")
    else:
        for amb in plan.ambiguous_token:
            A(f"- fold `{amb['fold']}` (survivor name {amb['survivor_name']!r}):")
            for m in amb["members"]:
                A(f"    - `{m['id']}` {m['class']:<12} links={m['links']:<4} "
                  f"{m['name']!r}")
    A("")

    A("## Sample clusters (first 20)")
    A("")
    for s in plan.samples:
        surv = s["survivor"]
        bits = []
        if surv["name"] != surv["was"]:
            bits.append(f"renamed from {surv['was']!r}")
        if surv["class"] != surv["was_class"]:
            bits.append(f"class {surv['was_class']}->{surv['class']}")
        note = f"  ({'; '.join(bits)})" if bits else ""
        A(f"### fold `{s['fold']}`")
        A(f"- SURVIVOR `{surv['id']}` [{surv['class']}] links={surv['links']} "
          f"{surv['name']!r}{note}")
        for m in s["losers"]:
            A(f"    - loser `{m['id']}` [{m['class']}] links={m['links']} {m['name']!r}")
        A("")

    A("## Verification queries (run AFTER apply)")
    A("")
    A("```sql")
    A("-- entity_profiles shrank by exactly the deleted losers + junk:")
    A(f"SELECT count(*) FROM entity_profiles;  -- expect {total_rows - n_deleted}")
    A("")
    A("-- No signal_entity_links dangles (every link points at a live entity):")
    A("SELECT count(*) FROM signal_entity_links sel")
    A("  LEFT JOIN entity_profiles ep ON ep.id = sel.entity_id")
    A("  WHERE ep.id IS NULL;")
    A("--   expect 0")
    A("")
    A("-- The survivor merge-version rows landed:")
    A("SELECT count(*) FROM entity_profile_versions")
    A("  WHERE data->>'event' = 'merge_0063';")
    A(f"--   expect ~{plan.fold_clusters} (one per merged survivor)")
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

    n_deleted = len(plan.merge_map) + len(plan.junk)
    print(
        f"rows={len(rows)} fold_clusters={plan.fold_clusters} "
        f"losers={len(plan.merge_map)} loser_links={plan.loser_links} "
        f"rewrites={len(plan.survivor_rewrite)} junk={len(plan.junk)} "
        f"junk_from_survivor={plan.junk_from_survivor} "
        f"survivors_geo_backfilled={len(plan.survivor_geo)} "
        f"ambiguous_geo={len(plan.ambiguous_geo)} "
        f"ambiguous_token={len(plan.ambiguous_token)} "
        f"net_row_delta=-{n_deleted}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
