"""
Fact predicate and value normalization.

Mirrors the approach used for graph relationship types in graph_tools.py:
canonical PascalCase predicates with an alias table for common variants.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical fact predicates — the closed vocabulary.
# Mirrors CANONICAL_RELATIONSHIP_TYPES from graph_tools.py where applicable,
# plus attribute-style predicates for entity properties.
# ---------------------------------------------------------------------------

CANONICAL_FACT_PREDICATES: frozenset[str] = frozenset({
    # Geopolitical relationships (shared with graph)
    "AlliedWith", "HostileTo", "TradesWith", "SanctionedBy",
    "SuppliesWeaponsTo", "MemberOf", "LeaderOf",
    "OperatesIn", "LocatedIn", "BordersWith", "OccupiedBy",
    "SignatoryTo", "ProducesResource", "ImportsFrom", "ExportsTo",
    "AffiliatedWith", "PartOf", "FundedBy", "RelatedTo",
    # Entity attribute predicates
    "Capital", "Population", "GDP", "Area", "Currency",
    "GovernmentType", "OfficialLanguage", "Founded",
    "DisplacedFrom", "MediatesBetween",
})

# ---------------------------------------------------------------------------
# Alias table — maps variant forms to canonical predicates.
# ---------------------------------------------------------------------------

FACT_PREDICATE_ALIASES: dict[str, str] = {
    # HostileTo
    "hostile_to": "HostileTo",
    "hostile to": "HostileTo",
    "hostileTo": "HostileTo",
    "is hostile to": "HostileTo",
    "are hostile to": "HostileTo",
    "enemy_of": "HostileTo",
    "enemyOf": "HostileTo",
    "EnemyOf": "HostileTo",
    "opposed_to": "HostileTo",
    "OpposedTo": "HostileTo",
    "at_war_with": "HostileTo",
    "AtWarWith": "HostileTo",
    "rival_of": "HostileTo",
    "RivalOf": "HostileTo",
    # AlliedWith
    "allied_with": "AlliedWith",
    "allied with": "AlliedWith",
    "alliedWith": "AlliedWith",
    "AlliedTo": "AlliedWith",
    "allied_to": "AlliedWith",
    "ally_of": "AlliedWith",
    "AllyOf": "AlliedWith",
    # LeaderOf
    "leader_of": "LeaderOf",
    "leader of": "LeaderOf",
    "leaderOf": "LeaderOf",
    "is leader of": "LeaderOf",
    "is supreme leader of": "LeaderOf",
    "elected_leader_of": "LeaderOf",
    "head_of": "LeaderOf",
    "HeadOf": "LeaderOf",
    "president_of": "LeaderOf",
    "PresidentOf": "LeaderOf",
    "commander_of": "LeaderOf",
    "CommanderOf": "LeaderOf",
    "leader is": "LeaderOf",
    # LocatedIn
    "located_in": "LocatedIn",
    "located in": "LocatedIn",
    "locatedIn": "LocatedIn",
    "located_in_region": "LocatedIn",
    "based_in": "LocatedIn",
    "BasedIn": "LocatedIn",
    "situated_in": "LocatedIn",
    "SituatedIn": "LocatedIn",
    # OperatesIn
    "operates_in": "OperatesIn",
    "operates in": "OperatesIn",
    "operatesIn": "OperatesIn",
    "operates_in_region": "OperatesIn",
    "active_in": "OperatesIn",
    "ActiveIn": "OperatesIn",
    "deployed_in": "OperatesIn",
    "DeployedIn": "OperatesIn",
    # PartOf
    "part_of": "PartOf",
    "part of": "PartOf",
    "partOf": "PartOf",
    "belongs_to": "PartOf",
    "BelongsTo": "PartOf",
    "component_of": "PartOf",
    "ComponentOf": "PartOf",
    # RelatedTo
    "related_to": "RelatedTo",
    "relatedTo": "RelatedTo",
    "related to": "RelatedTo",
    "related_to_event": "RelatedTo",
    # SuppliesWeaponsTo
    "supplies_weapons_to": "SuppliesWeaponsTo",
    "arms_supplier": "SuppliesWeaponsTo",
    "ArmsSupplier": "SuppliesWeaponsTo",
    # MemberOf
    "member_of": "MemberOf",
    "member of": "MemberOf",
    "memberOf": "MemberOf",
    "belongs_to_org": "MemberOf",
    "BelongsToOrg": "MemberOf",
    # SanctionedBy
    "sanctioned_by": "SanctionedBy",
    "sanctionedBy": "SanctionedBy",
    "embargoed_by": "SanctionedBy",
    "EmbargoedBy": "SanctionedBy",
    # TradesWith
    "trades_with": "TradesWith",
    "tradesWith": "TradesWith",
    "trading_partner": "TradesWith",
    "TradingPartner": "TradesWith",
    # BordersWith
    "borders_with": "BordersWith",
    "borders": "BordersWith",
    "adjacent_to": "BordersWith",
    "AdjacentTo": "BordersWith",
    # OccupiedBy
    "occupied_by": "OccupiedBy",
    "occupiedBy": "OccupiedBy",
    "controlled_by": "OccupiedBy",
    "ControlledBy": "OccupiedBy",
    # FundedBy
    "funded_by": "FundedBy",
    "fundedBy": "FundedBy",
    "financed_by": "FundedBy",
    "FinancedBy": "FundedBy",
    # AffiliatedWith
    "affiliated_with": "AffiliatedWith",
    "affiliatedWith": "AffiliatedWith",
    "associated_with": "AffiliatedWith",
    "AssociatedWith": "AffiliatedWith",
    # SignatoryTo
    "signatory_to": "SignatoryTo",
    "signatoryTo": "SignatoryTo",
    "party_to": "SignatoryTo",
    "PartyTo": "SignatoryTo",
}

# Build case-insensitive lookup for fast matching
_ALIAS_LOWER: dict[str, str] = {k.lower(): v for k, v in FACT_PREDICATE_ALIASES.items()}
_CANONICAL_LOWER: dict[str, str] = {c.lower(): c for c in CANONICAL_FACT_PREDICATES}


def normalize_fact_predicate(predicate: str) -> str:
    """Normalize a fact predicate to its canonical PascalCase form.

    Pipeline: exact alias → case-insensitive alias → canonical passthrough → as-is.
    """
    stripped = predicate.strip()
    if not stripped:
        return stripped

    # 1. Exact alias match (fast path)
    if stripped in FACT_PREDICATE_ALIASES:
        return FACT_PREDICATE_ALIASES[stripped]

    # 2. Case-insensitive alias match
    lower = stripped.lower()
    if lower in _ALIAS_LOWER:
        return _ALIAS_LOWER[lower]

    # 3. Already a canonical predicate (case-insensitive check)
    if lower in _CANONICAL_LOWER:
        return _CANONICAL_LOWER[lower]

    # 4. Strip "relation_" prefix junk (e.g. "relation_HostileTo_United States")
    if lower.startswith("relation_"):
        inner = stripped[len("relation_"):]
        # Try to extract just the predicate part before the target
        parts = inner.split("_", 1)
        return normalize_fact_predicate(parts[0])

    # 5. Strip "_since" suffix junk (e.g. "HostileTo_Iran_since")
    if lower.endswith("_since"):
        return normalize_fact_predicate(stripped[:-6])

    # 6. Pass through as-is (attribute predicates like "population_total_2024")
    return stripped


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------

_SINCE_PAREN = re.compile(r'\s*\(since\s+[0-9]{4}[-/][0-9]{2}(?:[-/][0-9]{2})?\)')
_SINCE_SUFFIX = re.compile(r'\s+since\s+[0-9]{4}[-/][0-9]{2}(?:[-/][0-9]{2})?')


def normalize_fact_value(value: str) -> str:
    """Strip temporal qualifiers and trailing whitespace from fact values.

    Temporal data belongs in separate since/until fields, not baked into the value.
    """
    cleaned = _SINCE_PAREN.sub('', value)
    cleaned = _SINCE_SUFFIX.sub('', cleaned)
    return cleaned.strip()
