# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ Phase 5 (facts / nexuses) — paired code-fix gates.

Pure (no-DB, no-network) unit tests over the VERBATIM live junk catalog for the
facts-nexuses audit:

  * fact_extractor: the quantity gate now catches bare PLURAL quantity nouns
    ('Thousands', 'hundreds', 'half'); a trailing spaced-possessive tokenizer
    artifact ("FRANCE 24 's") and an inverted employment relation ("Germany
    employed by ...") are dropped — while REAL facts pass every gate;
  * relationship_reifier: citation-marker residue is stripped from endpoints
    before write; an exact-1.0 nexus confidence is floored to the sentinel; the
    extended sports gate downgrades match-report hostility to co-occurrence;
  * vocabulary: every CamelCase rel_type variant folds to its lowercase canon.
"""

from __future__ import annotations

import pytest

from legba.data.analysts.relationship_reifier import (
    _NEXUS_SENTINEL_FLOOR,
    _coerce_typing,
    _is_sports_context,
    _strip_citation_residue,
)
from legba.data.filters.fact_extractor import (
    FactExtractorHandler,
    _is_employment_country_subject,
    _is_possessive_fragment,
    _is_quantity_phrase,
)
from legba.data.provenance import NexusPayload
from legba.data.vocabulary import normalize_predicate


def _norm_pred(predicate: str) -> str:
    return normalize_predicate(str(predicate).strip().lower())


def _drop(subject: str, predicate: str, value: str) -> str | None:
    return FactExtractorHandler._d6_drop_reason(subject, _norm_pred(predicate), value)


# ---------------------------------------------------------------------------
# P2 — fact junk gate (fragment / possessive / employment-inversion)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface",
    ["Thousands", "thousands", "hundreds", "Hundreds", "half", "Half",
     "dozens", "millions", "billions", "halves"],
)
def test_bare_plural_quantity_noun_is_quantity_phrase(surface: str) -> None:
    # The singular _NUMBER_WORDS set missed these; the DQ-P5 _QUANTITY_NOUNS set
    # now makes the quantity gate catch them ('Thousands located in South Africa').
    assert _is_quantity_phrase(surface) is True


@pytest.mark.parametrize(
    "surface",
    ["United States", "US", "France", "Half Moon Bay", "Thousand Oaks",
     "Kim Jong Un", "NATO"],
)
def test_real_entities_are_not_quantity_phrases(surface: str) -> None:
    # A single nominal token (or a name that merely CONTAINS a quantity word)
    # keeps the endpoint — conservative by construction.
    assert _is_quantity_phrase(surface) is False


@pytest.mark.parametrize(
    "surface",
    ["FRANCE 24 's", "Timor - Leste 's", "Donald Trump 's", "Abu Dhabi 's",
     "New Zealand 's"],
)
def test_trailing_spaced_possessive_is_fragment(surface: str) -> None:
    assert _is_possessive_fragment(surface) is True


@pytest.mark.parametrize(
    "surface",
    ["South Korea's", "United States", "Trump", "Xi Jinping", "s"],
)
def test_glued_possessive_and_names_are_not_fragments(surface: str) -> None:
    # Only the SPACE-before-clitic tokenizer artifact is flagged; a glued
    # possessive ("South Korea's") and plain names are NOT fragments.
    assert _is_possessive_fragment(surface) is False


def test_employment_country_subject_inversion() -> None:
    # A sovereign state is never "employed by" anyone — the inverted-employment
    # artifact ("Germany employed by Nagelsmann").
    assert _is_employment_country_subject("Germany", "employed by") is True
    assert _is_employment_country_subject("Venezuela", "spokesperson for") is True
    # A person subject flows through (real employment fact).
    assert _is_employment_country_subject("Konstantin Sonin", "employed by") is False
    # A non-employment predicate is out of scope ("Germany member of EU").
    assert _is_employment_country_subject("Germany", "member of") is False


@pytest.mark.parametrize(
    "subject,predicate,value,reason",
    [
        ("FRANCE 24 's", "spokesperson for", "Norway", "possessive_fragment"),
        ("Angela Diffley", "employed by", "FRANCE 24 's", "possessive_fragment"),
        ("Germany", "employed by", "Nagelsmann", "employment_country_subject"),
    ],
)
def test_d6_drops_mechanical_junk(subject, predicate, value, reason) -> None:
    assert _drop(subject, predicate, value) == reason


@pytest.mark.parametrize(
    "surface", ["half", "Thousands", "hundreds", "millions"],
)
def test_bare_quantity_noun_dropped_by_quantity_gate(surface: str) -> None:
    # Bare quantity nouns are dropped by the standalone _is_quantity_phrase gate
    # in _write_facts (they are not is_junk_entity, so _d6_drop_reason passes
    # them through — the quantity gate owns this slice).
    assert _is_quantity_phrase(surface) is True


@pytest.mark.parametrize(
    "subject,predicate,value",
    [
        ("Emmanuel Macron", "leader of", "France"),  # (leadership handled elsewhere; shape is clean)
        ("BBC", "operates in", "United Kingdom"),
        ("Germany", "member of", "European Union"),
        ("Konstantin Sonin", "employed by", "University of Chicago"),
        ("Eiffel Tower", "located in", "Paris"),
    ],
)
def test_d6_keeps_real_facts(subject, predicate, value) -> None:
    # None of the DQ-P5 gates may fire on a legitimate fact shape. (Leadership is
    # dropped by _is_junk_triple upstream, not by _d6_drop_reason, so it is not
    # asserted here.)
    reason = _drop(subject, predicate, value)
    assert reason not in {
        "possessive_fragment", "employment_country_subject", "junk_entity",
    }, f"unexpected DQ-P5 drop {reason!r} for {subject}/{predicate}/{value}"


# ---------------------------------------------------------------------------
# P5 — citation-residue strip on nexus endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("Masoud Pezeshkian /*[1]*/", "Masoud Pezeshkian"),
        ("Iran /*[2]*/", "Iran"),
        ("Donald Trump[1", "Donald Trump"),
        ("United States[2", "United States"),
        ("Israel [1", "Israel"),
        ("Macron[1]", "Macron"),
        ("French[1]", "French"),
        ("Lee [2", "Lee"),
        ("Mohammad Baqer Qalibaf[1", "Mohammad Baqer Qalibaf"),
        ("Masoud Pezeshkian 【3】", "Masoud Pezeshkian"),
    ],
)
def test_strip_citation_residue(raw: str, clean: str) -> None:
    assert _strip_citation_residue(raw) == clean


@pytest.mark.parametrize(
    "surface",
    ["United States", "Giorgia Meloni", "DR Congo", "Guinea-Bissau", "G7"],
)
def test_strip_citation_residue_leaves_clean_names(surface: str) -> None:
    assert _strip_citation_residue(surface) == surface


def test_coerce_typing_strips_bracket_endpoints() -> None:
    # A bracket-contaminated endpoint is cleaned before the nexus is built.
    p = _coerce_typing(
        {"related": True, "rel_type": "LeaderOf",
         "subject": "Masoud Pezeshkian /*[1]*/", "object": "Iran /*[2]*/"},
        fallback_subject="x", fallback_object="y",
    )
    assert isinstance(p, NexusPayload)
    assert "[" not in p.subject and "]" not in p.subject
    assert "[" not in p.object and "*" not in p.object
    assert p.subject == "Masoud Pezeshkian"


# ---------------------------------------------------------------------------
# P3 — nexus exact-1.0 sentinel floor
# ---------------------------------------------------------------------------


def test_coerce_typing_floors_exact_one_confidence() -> None:
    p = _coerce_typing(
        {"related": True, "rel_type": "CoOccursWith", "subject": "Monaco",
         "object": "Ukraine", "confidence": 1.0},
        fallback_subject="Monaco", fallback_object="Ukraine",
    )
    assert isinstance(p, NexusPayload)
    assert p.confidence == pytest.approx(_NEXUS_SENTINEL_FLOOR)


def test_coerce_typing_keeps_genuine_subone_confidence() -> None:
    p = _coerce_typing(
        {"related": True, "rel_type": "CoOccursWith", "subject": "France",
         "object": "Senegal", "confidence": 0.82},
        fallback_subject="France", fallback_object="Senegal",
    )
    assert isinstance(p, NexusPayload)
    assert p.confidence == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# P4 — extended sports gate over match-report vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "DR Congo face England in the knockout round",
        "the two squads clash on Saturday",
        "the head coach named his starting lineup",
        "a stadium packed for the qualifier",
        "they beat Morocco 2-1 to reach the final",
        "won 3-0 on the night",
    ],
)
def test_extended_sports_context_matches(text: str) -> None:
    assert _is_sports_context(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Russia launched missiles at Kyiv overnight",
        "the central bank raised interest rates by 2 points",
        "sanctions imposed after the 2022 invasion",
    ],
)
def test_geopolitics_text_not_sports(text: str) -> None:
    assert _is_sports_context(text) is False


def test_coerce_typing_sports_downgrade_over_match_report() -> None:
    # A hostile typing over a match report (no explicit legacy token, only the
    # extended vocabulary) is downgraded to a neutral co-occurrence, not a signed
    # -1 hostility.
    p = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "DR Congo",
         "object": "England", "intent": "hostile"},
        fallback_subject="DR Congo", fallback_object="England",
        evidence_text="DR Congo face England in the knockout stage",
    )
    assert isinstance(p, NexusPayload)
    assert p.polarity == 0


# ---------------------------------------------------------------------------
# P6 — rel_type CamelCase → lowercase canon (write-path convergence)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant,canonical",
    [
        ("AffiliatedWith", "affiliated with"),
        ("AlliedWith", "allied with"),
        ("ConductedVia", "conducted via"),
        ("CoOccursWith", "co occurs with"),
        ("HostileTo", "hostile to"),
        ("LeaderOf", "leader of"),
        ("LocatedIn", "located in"),
        ("MemberOf", "member of"),
        ("OperatesIn", "operates in"),
        ("Targets", "targets"),
    ],
)
def test_reltype_camelcase_folds_to_canon(variant: str, canonical: str) -> None:
    assert normalize_predicate(variant) == canonical
