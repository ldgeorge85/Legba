"""
Contradiction detection module.

Pure functions for detecting contradictory facts among stored knowledge.
No database access — callers pass in the existing facts list.

Works with the 30 canonical relationship predicates defined in
``graph_tools.CANONICAL_RELATIONSHIP_TYPES`` and
``fact_normalize.CANONICAL_FACT_PREDICATES``.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Contradictory predicate pairs
#
# Each canonical predicate maps to a frozenset of predicates that are
# semantically incompatible.  The mapping is symmetric: if A contradicts B,
# then B contradicts A.
# ---------------------------------------------------------------------------

CONTRADICTORY_PREDICATES: dict[str, frozenset[str]] = {
    # Geopolitical relationships
    "AlliedWith":        frozenset({"HostileTo", "SanctionedBy"}),
    "HostileTo":         frozenset({"AlliedWith", "TradesWith", "PartnersWith"}),
    "TradesWith":        frozenset({"SanctionedBy", "HostileTo"}),
    "SanctionedBy":      frozenset({"AlliedWith", "TradesWith"}),
    "SuppliesWeaponsTo": frozenset({"SanctionedBy", "HostileTo"}),
    "MemberOf":          frozenset({"WithdrewFrom", "ExpelledFrom"}),
    "PartnersWith":      frozenset({"HostileTo", "SanctionedBy"}),

    # Territorial / location
    "LocatedIn":         frozenset(),  # not inherently contradictory
    "OperatesIn":        frozenset(),
    "BordersWith":       frozenset(),
    "OccupiedBy":        frozenset(),  # value-level contradictions only

    # Institutional
    "LeaderOf":          frozenset(),  # volatile — handled by auto-supersede
    "AffiliatedWith":    frozenset(),
    "PartOf":            frozenset(),
    "FundedBy":          frozenset(),
    "SignatoryTo":       frozenset({"WithdrewFrom"}),

    # Resource / trade
    "ProducesResource":  frozenset(),
    "ImportsFrom":       frozenset(),
    "ExportsTo":         frozenset(),

    # Technical (retained for backward compat)
    "CreatedBy":         frozenset(),
    "MaintainedBy":      frozenset(),
    "UsesArchitecture":  frozenset(),
    "UsesPersistence":   frozenset(),
    "HasSafety":         frozenset(),
    "HasLimitation":     frozenset(),
    "HasFeature":        frozenset(),
    "Extends":           frozenset(),
    "DependsOn":         frozenset(),
    "AlternativeTo":     frozenset(),
    "InspiredBy":        frozenset(),

    # Fact-only predicates
    "RelatedTo":         frozenset(),
    "DisplacedFrom":     frozenset(),
    "MediatesBetween":   frozenset(),

    # Pseudo-predicates used in contradiction references (not canonical
    # themselves but may appear as the "other side" of a contradiction).
    "WithdrewFrom":      frozenset({"MemberOf", "SignatoryTo"}),
    "ExpelledFrom":      frozenset({"MemberOf"}),
}


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

def detect_contradiction(
    subject: str,
    predicate: str,
    value: str,
    existing_facts: list[dict],
) -> list[dict]:
    """Detect facts that contradict a proposed (subject, predicate, value) triple.

    Two kinds of contradictions are checked:

    1. **Predicate contradiction** — the existing fact has the same subject
       (case-insensitive) and a predicate in the contradictory set of the
       new predicate, with the same value.
       Example: ``(Iran, AlliedWith, Russia)`` contradicts
       ``(Iran, HostileTo, Russia)``.

    2. **Value contradiction** — the existing fact has the same subject and
       predicate but a *different* value, for predicates that are inherently
       single-valued (e.g. ``LeaderOf`` — only one leader at a time).
       Note: multi-valued predicates (``AlliedWith``, ``TradesWith``, etc.)
       do NOT trigger value contradictions since an entity can have many.

    Parameters
    ----------
    subject : str
    predicate : str
    value : str
    existing_facts : list[dict]
        Each dict must contain at least: ``id``, ``subject``, ``predicate``,
        ``value``, ``confidence``.

    Returns
    -------
    list[dict]
        Contradicted fact dicts (subset of *existing_facts*), each with keys:
        ``id``, ``subject``, ``predicate``, ``value``, ``confidence``,
        ``contradiction_type`` (``"predicate"`` or ``"value"``).
    """
    contradicted: list[dict] = []
    sub_lower = subject.lower()
    val_lower = value.lower()
    contra_preds = CONTRADICTORY_PREDICATES.get(predicate, frozenset())

    for fact in existing_facts:
        fact_sub = fact.get("subject", "")
        fact_pred = fact.get("predicate", "")
        fact_val = fact.get("value", "")

        if fact_sub.lower() != sub_lower:
            continue

        # 1. Predicate contradiction: same subject, contradictory predicate,
        #    same target (value)
        if fact_pred in contra_preds and fact_val.lower() == val_lower:
            contradicted.append({
                "id": fact.get("id"),
                "subject": fact_sub,
                "predicate": fact_pred,
                "value": fact_val,
                "confidence": fact.get("confidence", 0.5),
                "contradiction_type": "predicate",
            })
            continue

        # 2. Value contradiction: same subject + predicate, different value,
        #    for single-valued predicates only.
        if (fact_pred == predicate
                and fact_val.lower() != val_lower
                and predicate in _SINGLE_VALUED_PREDICATES):
            contradicted.append({
                "id": fact.get("id"),
                "subject": fact_sub,
                "predicate": fact_pred,
                "value": fact_val,
                "confidence": fact.get("confidence", 0.5),
                "contradiction_type": "value",
            })

    return contradicted


# Predicates where only one value per subject is meaningful at a time.
# Multi-valued predicates like AlliedWith, TradesWith are intentionally
# excluded — a country can trade with many partners simultaneously.
_SINGLE_VALUED_PREDICATES: frozenset[str] = frozenset({
    "LeaderOf", "Capital", "Population", "GDP", "Area", "Currency",
    "GovernmentType", "OfficialLanguage", "LocatedIn", "OccupiedBy",
})


# ---------------------------------------------------------------------------
# Hypothesis trigger
# ---------------------------------------------------------------------------

def should_auto_create_hypothesis(
    contradiction: dict,
    new_fact: dict,
    *,
    min_signal_refs: int = 2,
    signal_ref_count: int = 0,
) -> bool:
    """Decide whether a contradiction warrants automatic hypothesis creation.

    A hypothesis is created when both the existing and new facts have
    sufficient confidence and the entities involved have enough signal
    references to indicate genuine analytical tension (not just noise).

    Parameters
    ----------
    contradiction : dict
        A contradicted fact dict (from :func:`detect_contradiction`).
        Must contain ``confidence``.
    new_fact : dict
        The proposed new fact.  Must contain ``confidence``.
    min_signal_refs : int
        Minimum signal reference count required.
    signal_ref_count : int
        Actual signal reference count for the involved entities.

    Returns
    -------
    bool
        True if a hypothesis should be auto-created.
    """
    old_conf = contradiction.get("confidence", 0.0)
    new_conf = new_fact.get("confidence", 0.0)

    if old_conf <= 0.5 or new_conf <= 0.5:
        return False

    if signal_ref_count < min_signal_refs:
        return False

    return True
