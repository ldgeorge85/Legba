# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FIX A — widened ingestion fact gate + direction-sanity + confidence.

Pure (no-DB, no-network) unit tests over the VERBATIM live junk catalog for the
2026-06-26 second-pass regressions:

  * the widened ingestion gate DROPS the new junk classes (money / age-time /
    sports-event / mis-typed surfaces) the first pass missed (re-runs the real
    gate logic — ``_is_junk_triple`` then ``_d6_drop_reason`` — over the
    catalog);
  * direction-sanity rejects two-entity inversions ("Facebook located in
    Instagram") + acronym self-reference ("Alternative for Germany located in
    AfD");
  * the flat 0.5 confidence is replaced by the extractor's real per-triple score
    when available, else a clearly-documented heuristic floor, with the basis
    recorded in ``confidence_components``.

No DB / no network — only the pure module-level gate helpers + the handler's
static ``_d6_drop_reason`` (mirrors the real fact-write drop ordering).
"""

from __future__ import annotations

import pytest

from legba.data.filters.fact_extractor import (
    _INGESTION_DEFAULT_CONFIDENCE,
    FactExtractorHandler,
    _is_junk_triple,
    _is_nongeo_containment_inversion,
    _resolve_ingestion_confidence,
    _resolve_ingestion_confidence_components,
)
from legba.data.vocabulary import normalize_predicate


def _norm_pred(predicate: str) -> str:
    return normalize_predicate(str(predicate).strip().lower())


def _pipeline_drop(subject: str, predicate: str, value: str) -> str | None:
    """Mirror the real fact-write drop order: the W1 junk gate first, then D6.

    Returns "junk_triple" for a step-1 drop, else the D6 reason tag, else None
    (the triple survives both gates).
    """
    pred = _norm_pred(predicate)
    if _is_junk_triple(subject, pred, value):
        return "junk_triple"
    return FactExtractorHandler._d6_drop_reason(subject, pred, value)


# ---------------------------------------------------------------------------
# D6/D13 — the widened ingestion gate drops the new junk endpoint classes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    ["S$2,500", "US$ 525 million", "$3.2bn"],
)
def test_money_value_dropped_by_gate(value):
    assert _pipeline_drop("Country", "spends", value) == "junk_entity"


@pytest.mark.parametrize(
    "value",
    ["51 - year - old", "2,600 - year - old", "24 - year - old", "centuries"],
)
def test_age_time_value_dropped_by_gate(value):
    assert _pipeline_drop("Artifact", "aged", value) == "junk_entity"


@pytest.mark.parametrize("value", ["World Cup", "Group F"])
def test_sports_event_value_dropped_by_gate(value):
    # "Germany operates in World Cup", "... part of Group F".
    assert _pipeline_drop("Germany", "operates in", value) == "junk_entity"


def test_money_or_age_subject_dropped_by_gate():
    # The junk class drops regardless of which endpoint carries it.
    assert _pipeline_drop("US$ 525 million", "funds", "Project X") == "junk_entity"
    assert _pipeline_drop("51 - year - old", "leads", "Party") == "junk_entity"


def test_legit_triple_survives_widened_gate():
    # A clean distinct-name geopolitical fact still passes every gate.
    assert _pipeline_drop("Macron", "leader of", "France") in (None, "junk_triple")
    # (leader-of is W1-dropped as seed-owned; use a structural relation to show
    # a genuine survivor.)
    assert _pipeline_drop("BBC", "operates in", "United Kingdom") is None
    assert _pipeline_drop("Robertson Quay", "located in", "Singapore") is None


# ---------------------------------------------------------------------------
# Direction-sanity: two-entity inversion + acronym self-reference
# ---------------------------------------------------------------------------

def test_facebook_located_in_instagram_is_dropped():
    assert _is_nongeo_containment_inversion("Facebook", "located in", "Instagram") is True
    assert _pipeline_drop("Facebook", "located in", "Instagram") == "nongeo_containment_inversion"


def test_alternative_for_germany_located_in_afd_is_dropped():
    # Full name + acronym of the SAME party — a self-reference artifact; neither
    # endpoint is a place.
    assert _is_nongeo_containment_inversion(
        "Alternative for Germany", "located in", "AfD"
    ) is True
    assert _pipeline_drop(
        "Alternative for Germany", "located in", "AfD"
    ) == "nongeo_containment_inversion"


def test_geographic_containment_with_real_place_survives():
    # The direction-sanity must NOT touch a legit geo-containment fact.
    for s, p, v in (
        ("BBC", "located in", "United Kingdom"),
        ("Eiffel Tower", "located in", "Paris"),
        ("Robertson Quay", "located in", "Singapore"),
        ("Texas", "located in", "United States"),
    ):
        assert _is_nongeo_containment_inversion(s, p, v) is False, (s, p, v)


def test_nongeo_inversion_only_fires_on_containment_predicates():
    # A non-containment predicate between two orgs is out of scope here.
    assert _is_nongeo_containment_inversion("Facebook", "acquired", "Instagram") is False


# ---------------------------------------------------------------------------
# Confidence: extractor score when available, else documented heuristic floor
# ---------------------------------------------------------------------------

def test_missing_score_uses_documented_floor():
    conf, comp = _resolve_ingestion_confidence_components({}, "relation")
    assert conf == _INGESTION_DEFAULT_CONFIDENCE
    assert comp["source"] == "heuristic_floor"
    assert comp["extractor_score"] is None
    assert comp["floor"] == _INGESTION_DEFAULT_CONFIDENCE
    assert comp["backend"] == "relation"
    assert "note" in comp


def test_real_extractor_score_is_used():
    conf, comp = _resolve_ingestion_confidence_components(
        {"confidence": 0.9}, "relation"
    )
    assert conf == pytest.approx(0.9)
    assert comp["source"] == "extractor_score"
    assert comp["extractor_score"] == pytest.approx(0.9)


def test_relation_backend_exact_one_is_sentinel_floor():
    # Legacy REBEL "no real score" sentinel on the relation backend → floor.
    conf, comp = _resolve_ingestion_confidence_components(
        {"confidence": 1.0}, "relation"
    )
    assert conf == _INGESTION_DEFAULT_CONFIDENCE
    assert comp["source"] == "heuristic_floor"
    assert comp["extractor_score"] == pytest.approx(1.0)


def test_llm_backend_score_used_as_is():
    conf, comp = _resolve_ingestion_confidence_components(
        {"confidence": 0.8}, "llm"
    )
    assert conf == pytest.approx(0.8)
    assert comp["source"] == "extractor_score"
    assert comp["backend"] == "llm"


def test_non_numeric_score_uses_floor():
    conf, comp = _resolve_ingestion_confidence_components(
        {"confidence": "high"}, "relation"
    )
    assert conf == _INGESTION_DEFAULT_CONFIDENCE
    assert comp["source"] == "heuristic_floor"


def test_scalar_wrapper_is_backward_compatible():
    # The old float-only signature still returns just the scalar.
    assert _resolve_ingestion_confidence({}, "relation") == _INGESTION_DEFAULT_CONFIDENCE
    assert _resolve_ingestion_confidence({"confidence": 0.7}, "relation") == pytest.approx(0.7)
    assert _resolve_ingestion_confidence({"confidence": 1.0}, "relation") == _INGESTION_DEFAULT_CONFIDENCE
