# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W2 Agent-B — producer-side defects D3, D14, D15 for ``relationship_reifier``.

PURE unit tests (no DB, no LLM) over the VERBATIM live garbage strings the
Platform Health review (planning/PLATFORM_HEALTH_RESULTS.md) flagged:

  * D3  — demonym endpoints collapse → self-loop drop: "Israel leader of
          Israeli", "Iran supplies weapons to Iranian" can never be reified.
  * D14 — polarity is a DETERMINISTIC function of intent/rel_type (never the
          LLM's free integer), and a sports/fixture co-mention is not signed -1
          "hostile": "Spain hostile to Saudi Arabia", "Iran hostile to Group G".
  * (D15 — the source_signal_ids pass-through is exercised in the integration
          path; here we only assert the payload carries the field.)
"""

from __future__ import annotations

import pytest

from legba.data.analysts.relationship_reifier import (
    _canonical_polarity,
    _coerce_typing,
    _is_sports_context,
)
from legba.data.analysts.deterministic_handlers.structural_balance import (
    INTENT_POLARITY,
    POLARITY,
    polarity_from,
)
from legba.data.provenance import NexusPayload


# ---------------------------------------------------------------------------
# D3 — demonym endpoints collapse → self-loop drop (canonicalize before build)
# ---------------------------------------------------------------------------


def test_d3_israel_leader_of_israeli_dropped():
    # "Israel leader of Israeli" — Israeli demonym collapses to Israel; the
    # subject == object self-loop is NOT a relationship and must be dropped.
    assert _coerce_typing(
        {"related": True, "rel_type": "LeaderOf", "subject": "Israel",
         "object": "Israeli"},
        fallback_subject="Israel", fallback_object="Israeli",
    ) is None


def test_d3_iran_supplies_weapons_to_iranian_dropped():
    # "Iran supplies weapons to Iranian" — Iranian → Iran; self-loop drop.
    assert _coerce_typing(
        {"related": True, "rel_type": "SuppliesWeaponsTo", "subject": "Iran",
         "object": "Iranian"},
        fallback_subject="Iran", fallback_object="Iranian",
    ) is None


def test_d3_demonym_endpoint_collapses_when_other_endpoint_distinct():
    # A demonym endpoint that collapses to a DISTINCT country is kept — the
    # endpoint is normalized to the canonical country form, not dropped.
    p = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Iranian",
         "object": "Israel", "intent": "hostile"},
        fallback_subject="Iranian", fallback_object="Israel",
    )
    assert isinstance(p, NexusPayload)
    assert p.subject == "Iran"          # "Iranian" canonicalized to its country
    assert p.object == "Israel"
    assert p.polarity == -1


def test_d3_junk_endpoint_dropped():
    # A junk endpoint ("TV") must never reach a nexus.
    assert _coerce_typing(
        {"related": True, "rel_type": "AlliedWith", "subject": "TV",
         "object": "France"},
        fallback_subject="TV", fallback_object="France",
    ) is None


def test_d3_demonym_alias_us_collapses():
    # "American" → United States; a distinct other endpoint survives.
    p = _coerce_typing(
        {"related": True, "rel_type": "AlliedWith", "subject": "American",
         "object": "Germany", "intent": "supportive"},
        fallback_subject="American", fallback_object="Germany",
    )
    assert isinstance(p, NexusPayload)
    assert p.subject == "United States"
    assert p.object == "Germany"


# ---------------------------------------------------------------------------
# D14 — sports / event co-occurrence is NOT signed hostile geopolitics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        "Spain face Saudi Arabia in World Cup Group G",
        "Spain vs Saudi Arabia — Group G fixture preview",
        "the two meet in the football qualifiers",
        "Champions League semi-final draw",
        "FIFA tournament fixtures announced",
    ],
)
def test_d14_sports_context_detected(evidence):
    assert _is_sports_context(evidence) is True


@pytest.mark.parametrize(
    "evidence",
    [
        "Spain and Saudi Arabia signed an arms deal",
        "tensions rose along the disputed border",
        "the group of seven nations met in Geneva",  # "group of" != "group G"
        "",
    ],
)
def test_d14_non_sports_context_not_flagged(evidence):
    assert _is_sports_context(evidence) is False


def test_d14_spain_hostile_to_saudi_arabia_downgraded():
    # "Spain hostile to Saudi Arabia" out of a World-Cup group draw must NOT be
    # reified as hostile geopolitics — downgrade to neutral co-occurrence.
    p = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Spain",
         "object": "Saudi Arabia", "intent": "hostile"},
        fallback_subject="Spain", fallback_object="Saudi Arabia",
        evidence_text="Spain face Saudi Arabia in World Cup Group G",
    )
    assert isinstance(p, NexusPayload)
    assert p.polarity == 0, "a sports fixture is not signed hostile"
    assert p.intent == "neutral"
    assert p.rel_type == "CoOccursWith"


def test_d14_iran_hostile_to_group_g_downgraded():
    # "Iran hostile to Group G" — a World-Cup group, not an enemy.
    p = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Iran",
         "object": "Spain", "intent": "hostile"},
        fallback_subject="Iran", fallback_object="Spain",
        evidence_text="Iran drawn into Group G of the World Cup",
    )
    assert isinstance(p, NexusPayload)
    assert p.polarity == 0
    assert p.intent == "neutral"


def test_d14_real_hostility_outside_sports_kept():
    # The gate must NOT suppress genuine antagonism in a non-sports context.
    p = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Iran",
         "object": "Israel", "intent": "hostile"},
        fallback_subject="Iran", fallback_object="Israel",
        evidence_text="missile strikes exchanged across the border",
    )
    assert isinstance(p, NexusPayload)
    assert p.polarity == -1
    assert p.intent == "hostile"
    assert p.rel_type == "HostileTo"


# ---------------------------------------------------------------------------
# D14 — polarity is a DETERMINISTIC function of (intent, rel_type); the LLM's
# free polarity integer can never contradict the words next to it.
# ---------------------------------------------------------------------------


def test_d14_polarity_equals_f_of_intent():
    # intent wins over the rel_type table AND over any LLM polarity integer.
    assert polarity_from("supportive", "HostileTo") == 1
    assert polarity_from("hostile", "AlliedWith") == -1
    assert polarity_from("neutral", "SuppliesWeaponsTo") == 0
    assert polarity_from("dual-use", "HostileTo") == 0
    assert polarity_from("dual_use", "HostileTo") == 0  # legacy spelling


def test_d14_polarity_falls_back_to_rel_type_when_intent_unknown():
    # An unmapped / empty intent falls through to the rel_type POLARITY table.
    assert polarity_from("", "HostileTo") == -1
    assert polarity_from(None, "AlliedWith") == 1
    assert polarity_from("frobnicate", "SuppliesWeaponsTo") == -1
    # Off-table rel_type AND unknown intent → neutral 0.
    assert polarity_from("frobnicate", "FriendsWith") == 0


def test_d14_coerce_ignores_llm_polarity_integer():
    # The contradiction class: LLM claims polarity=-1 but intent=supportive.
    # The row must come out coherent — polarity follows intent, not the integer.
    p = _coerce_typing(
        {"related": True, "rel_type": "MemberOf", "subject": "France",
         "object": "European Union", "intent": "supportive", "polarity": -1},
        fallback_subject="France", fallback_object="European Union",
    )
    assert isinstance(p, NexusPayload)
    assert p.intent == "supportive"
    assert p.polarity == 1, "polarity follows intent, never the LLM integer"


def test_d14_intent_backfilled_from_sign_when_unknown():
    # No usable intent on a clearly-signed rel_type → intent is backfilled from
    # the resolved sign so intent ⇔ polarity stay consistent.
    p = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Iran",
         "object": "Israel"},
        fallback_subject="Iran", fallback_object="Israel",
    )
    assert isinstance(p, NexusPayload)
    assert p.polarity == -1
    assert p.intent == "hostile"


def test_d14_canonical_polarity_helper_is_intent_first():
    # _canonical_polarity now delegates to polarity_from (intent-first, no LLM).
    assert _canonical_polarity("HostileTo", "supportive") == 1
    assert _canonical_polarity("AlliedWith", "hostile") == -1
    assert _canonical_polarity("OperatesIn", "neutral") == 0


def test_intent_polarity_table_covers_closed_intent_set():
    # Every intent the reifier coerces to must have a deterministic sign.
    for intent in ("supportive", "hostile", "dual-use", "neutral"):
        assert intent in INTENT_POLARITY


# ---------------------------------------------------------------------------
# D15 — the nexus payload carries source_signal_ids (populated at the call site)
# ---------------------------------------------------------------------------


def test_d15_payload_has_source_signal_ids_field():
    # The coerced payload exposes source_signal_ids (defaults empty); run_method
    # stamps it + passes it to write_nexus as the D15 provenance.
    p = _coerce_typing(
        {"related": True, "rel_type": "AlliedWith", "subject": "France",
         "object": "Germany", "intent": "supportive"},
        fallback_subject="France", fallback_object="Germany",
    )
    assert isinstance(p, NexusPayload)
    assert hasattr(p, "source_signal_ids")
    assert p.source_signal_ids == []


# ---------------------------------------------------------------------------
# Regression — the prior behaviors still hold (off-list skip, related=false).
# ---------------------------------------------------------------------------


def test_off_list_rel_type_still_skipped():
    assert _coerce_typing(
        {"related": True, "rel_type": "FriendsWithBenefits", "subject": "A",
         "object": "B"},
        fallback_subject="A", fallback_object="B",
    ) is None


def test_related_false_still_skipped():
    assert _coerce_typing(
        {"related": False}, fallback_subject="A", fallback_object="B",
    ) is None
