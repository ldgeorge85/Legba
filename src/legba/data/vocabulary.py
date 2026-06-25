# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.vocabulary — trimmed AGE vocabulary baked into the redesign.

Per `design/legba_data_mapping.md` §4.5 and the Lewis-confirmed scope cut in
the L-001 brief, the populated half of the legacy AGE vocabulary survives;
the unpopulated half retires.

**Retained entity_classes (9):** entity, location, organization, person,
event, country, concept, corporation, software.

**Retained relationship_types (14):** HostileTo, LocatedIn, AlliedWith,
PartyTo, Targets, OperatesIn, MemberOf, LeaderOf, ConductedVia,
SuppliesWeaponsTo, PartOf, CoOccursWith, AffiliatedWith, InvolvedIn.

Notes:
  * `Nexus` retires; only `intent` survives, promoted to a property on the
    underlying canonical edge (DM-1 resolved).
  * `Targets` (1,245 live edges) promoted to canonical (DM-5 resolved).
  * Legacy UPPER_SNAKE forms (`INVOLVED_IN`, `PART_OF`, `TRACKED_BY`)
    normalize to PascalCase via aliases (DM-6 resolved). `TRACKED_BY` /
    `PART_OF` collapse onto `InvolvedIn` / `PartOf`.
  * Future extension: operators register new values via the
    `vocabulary_entries` registry table (L-101 §8 `VocabularyRegistry`).

Runtime extensibility per L-101 §8: this module ships the *seed* set. The
descriptor registry table `vocabulary_entries` (created by migration 0002)
is the authoritative source at runtime — `apply_seed_vocabulary` inserts
this seed; new entries land via the registry API.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Seed vocabulary
# ---------------------------------------------------------------------------

ENTITY_CLASSES: tuple[str, ...] = (
    "entity",
    "location",
    "organization",
    "person",
    "event",
    "country",
    "concept",
    "corporation",
    "software",
)

RELATIONSHIP_TYPES: tuple[str, ...] = (
    "HostileTo",
    "LocatedIn",
    "AlliedWith",
    "PartyTo",
    "Targets",
    "OperatesIn",
    "MemberOf",
    "LeaderOf",
    "ConductedVia",
    "SuppliesWeaponsTo",
    "PartOf",
    "CoOccursWith",
    "AffiliatedWith",
    "InvolvedIn",
)

# Legacy UPPER_SNAKE → canonical PascalCase normalization.
RELATIONSHIP_ALIASES: dict[str, str] = {
    "INVOLVED_IN": "InvolvedIn",
    "PART_OF": "PartOf",
    "TRACKED_BY": "InvolvedIn",  # Subsumed into InvolvedIn per L-090 §4.5.
}


# ---------------------------------------------------------------------------
# AGE vertex/edge label cypher conventions
# ---------------------------------------------------------------------------

# Vertex labels in AGE are PascalCase, e.g. ":Organization".
# Map entity_class (snake_case) → AGE vertex label (PascalCase).
def vertex_label(entity_class: str) -> str:
    """Map an `entity_class` to its AGE vertex label."""
    if not entity_class:
        return "Entity"
    return "".join(part.capitalize() for part in entity_class.split("_"))


def normalize_relationship(rel_type: str) -> str:
    """Apply legacy aliases and return the canonical relationship type."""
    return RELATIONSHIP_ALIASES.get(rel_type, rel_type)


# ---------------------------------------------------------------------------
# Substrate predicate-column normalization (facts.predicate / nexuses.rel_type)
# ---------------------------------------------------------------------------
#
# DISTINCT from `normalize_relationship` above: that maps to the AGE *graph*
# relationship_type, which is PascalCase by convention (":HostileTo"). This
# normalizes the predicate/rel_type STORED ON THE ROW (the
# `facts.predicate` / `nexuses.rel_type` text columns), where two producers
# diverged:
#
#   * the seed driver writes CamelCase ("LeaderOf", "MemberOf", "HostileTo");
#   * the ingest `fact_extractor` writes lowercase-spaced ("leader of",
#     "member of", "hostile to").
#
# Same relations, two surface forms — so the supersession/dedup keys
# (lower(predicate)) never line up across the two producers and the read
# layer sees split vocabularies. We converge on the LOWERCASE-SPACED form: it
# DOMINATES live data (every ingestion fact uses it) and the supersession
# index already lower()s the column, so this is the form the index keys on.
# Note this is the row-column form, NOT the AGE edge label (which stays the
# `_fact_graph.edge_label_for_predicate` PascalCase bucket, lossily mapping
# many predicates onto a handful of seeded edge labels).
#
# Conservative by construction: an unknown predicate passes through unchanged
# (lower-cased + ws-collapsed only when it is a single CamelCase token that we
# explicitly map; everything else is returned verbatim).

#: CamelCase row-predicate (the seed adapters' form) → canonical
#: lowercase-spaced form. Covers every CamelCase rel_type / predicate the
#: seed adapters emit (`world_baseline` LeaderOf+MemberOf+InActiveConflictWith,
#: `wikidata_leaders` LeaderOf+MemberOf, `acled_conflict`
#: HostileTo+InvolvedInConflictEvent, `sipri_arms_transfers` ArmsTransferTo)
#: plus the AGE vocabulary's signed relation types so any future CamelCase
#: emitter converges too.
PREDICATE_CANONICAL: dict[str, str] = {
    "leaderof": "leader of",
    # Country-SUBJECT office fact (subject=country, value=leader) — the
    # supersession-correct leader shape the seed adapters emit alongside
    # `LeaderOf`. CamelCase + the already-canonical lowercase-spaced form both
    # fold to one key so the (lower(subject), lower(predicate)) supersession
    # gate lines up across world_baseline + wikidata_leaders.
    "headofstate": "head of state",
    "head of state": "head of state",
    "headofgovernment": "head of government",
    "head of government": "head of government",
    "memberof": "member of",
    "locatedin": "located in",
    "alliedwith": "allied with",
    "hostileto": "hostile to",
    # Curated active-conflict edge (world_baseline ``conflicts``) — the
    # current-world-state "these states are at war now" relation, distinct from
    # ACLED's event-level HostileTo. Both CamelCase + the already-canonical
    # form fold to one key so the lower(rel_type) dedup/supersession lines up.
    "inactiveconflictwith": "in active conflict with",
    "in active conflict with": "in active conflict with",
    "sanctions": "sanctions",
    "armstransferto": "arms transfer to",
    "involvedinconflictevent": "involved in conflict event",
    "partof": "part of",
    "cooccurswith": "co occurs with",
    "operatesin": "operates in",
    "partyto": "party to",
    "targets": "targets",
    "conductedvia": "conducted via",
    "suppliesweaponsto": "supplies weapons to",
    "affiliatedwith": "affiliated with",
    "involvedin": "involved in",
}


def normalize_predicate(predicate: str) -> str:
    """Converge a fact/nexus row predicate (``facts.predicate`` /
    ``nexuses.rel_type``) onto ONE canonical, lowercase-spaced surface form.

    The seed driver emits CamelCase ("LeaderOf"); the ingest extractor emits
    lowercase-spaced ("leader of"). Both producers call this at write so a
    relation has a single form on the row (and the lower(predicate) dedup /
    supersession keys line up across producers).

    Resolution order:
      * empty / whitespace → returned unchanged (callers own required-field
        validation; we make no claim about empties);
      * exact CamelCase hit in :data:`PREDICATE_CANONICAL` (case-folded,
        ws-stripped) → its canonical lowercase-spaced form;
      * anything else → returned VERBATIM (conservative: an unknown predicate
        is never rewritten, so an unmapped ingestion phrase or a novel
        operator predicate flows through untouched).
    """
    if not predicate or not predicate.strip():
        return predicate
    key = predicate.strip().casefold()
    return PREDICATE_CANONICAL.get(key, predicate)


# ---------------------------------------------------------------------------
# Vocabulary entry shape (matches `vocabulary_entries` row in migration 0002)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedEntry:
    family: str  # "entity_class" | "relationship_type" | …
    value: str
    aliases: tuple[str, ...] = ()
    parent: str | None = None
    notes: str | None = None


def seed_entries() -> list[SeedEntry]:
    """Return the seed vocabulary as a list of `SeedEntry` records.

    Used by migration 0010 + the runtime `apply_seed_vocabulary` helper.
    """
    seeds: list[SeedEntry] = []

    for ec in ENTITY_CLASSES:
        seeds.append(SeedEntry(family="entity_class", value=ec))

    # Reverse-lookup aliases per relationship.
    rev_aliases: dict[str, list[str]] = {}
    for legacy, canonical in RELATIONSHIP_ALIASES.items():
        rev_aliases.setdefault(canonical, []).append(legacy)

    for rt in RELATIONSHIP_TYPES:
        seeds.append(
            SeedEntry(
                family="relationship_type",
                value=rt,
                aliases=tuple(rev_aliases.get(rt, ())),
            )
        )

    return seeds


__all__ = [
    "ENTITY_CLASSES",
    "RELATIONSHIP_TYPES",
    "RELATIONSHIP_ALIASES",
    "PREDICATE_CANONICAL",
    "SeedEntry",
    "vertex_label",
    "normalize_relationship",
    "normalize_predicate",
    "seed_entries",
]
