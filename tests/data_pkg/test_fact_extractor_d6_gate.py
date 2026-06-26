# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure (no-DB) unit tests for the D6/D13 hardened ingestion fact gate.

Covers the VERBATIM live-garbage triples from
``planning/PLATFORM_HEALTH_RESULTS.md`` (D6/D13): adjective / demonymic-plural
values, reflexive-after-canon triples, source-publication subjects, inverted
relation direction, and the sports-roster / topic noise (the World-Cup feed
flooding geopolitical extraction). No database, no network — only the pure
gate helpers + the handler's ``_d6_drop_reason`` static method.

The fact-write loop applies the gates in this order:
  1. ``_is_junk_triple`` (W1-owned: leadership predicates, HTML, self-ref) ;
  2. ``_d6_drop_reason`` (THIS gate) ;
  3. quantity / confidence / allowlist.
A couple of cases (e.g. "China leader of America") are dropped at step 1, so a
combined ``_pipeline_drop`` helper mirrors the real ordering.
"""

from __future__ import annotations

import pytest

from legba.data.filters.fact_extractor import (
    _INGESTION_DEFAULT_CONFIDENCE,
    FactExtractorConfig,
    FactExtractorHandler,
    _is_adjective_or_demonymic_value,
    _is_inverted_relation,
    _is_junk_triple,
    _is_reflexive_after_canon,
    _is_roster_triple,
    _is_source_publication_subject,
    _text_is_sports_dominated,
)
from legba.data.vocabulary import normalize_predicate


def _norm_pred(predicate: str) -> str:
    """Mirror the write-loop predicate normalization."""
    return normalize_predicate(str(predicate).strip().lower())


def _d6(subject: str, predicate: str, value: str) -> str | None:
    """The D6 gate alone (predicate normalized as the loop does)."""
    return FactExtractorHandler._d6_drop_reason(
        subject, _norm_pred(predicate), value
    )


def _pipeline_drop(subject: str, predicate: str, value: str) -> str | None:
    """Mirror the real fact-write drop order: the W1 junk gate first, then D6.

    Returns the drop reason ("junk_triple" for a step-1 drop, else the D6 tag),
    or ``None`` when the triple survives both gates.
    """
    pred = _norm_pred(predicate)
    if _is_junk_triple(subject, pred, value):
        return "junk_triple"
    return FactExtractorHandler._d6_drop_reason(subject, pred, value)


# ---------------------------------------------------------------------------
# Verbatim live-garbage triples — every one MUST be dropped.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "subject,predicate,value,reason",
    [
        # The four results-doc verbatim strings. Exact-RAW self-references
        # ("US located in US", "EU member of EU") are caught by the W1 junk
        # gate at step 1 ("junk_triple"); distinct-surface reflexives fall to
        # the D6 canon check ("reflexive_after_canon").
        ("France", "capital of", "Parisians", "adjective_value"),
        ("US", "located in", "US", "junk_triple"),       # exact self-ref @ step 1
        ("China", "leader of", "America", "junk_triple"),  # leadership @ step 1
        ("EU", "member of", "EU", "junk_triple"),         # exact self-ref @ step 1
        # Inversion examples (mandate).
        ("US", "located in", "New York", "inverted_relation"),
        ("US", "capital of", "France", "inverted_relation"),  # country subject
        # Reflexive-after-canon via DISTINCT-surface alias collapse (the cases
        # the raw self-ref check misses — this is the D6 gate's job).
        ("United States", "located in", "US", "reflexive_after_canon"),
        ("America", "member of", "USA", "reflexive_after_canon"),
        ("EU", "part of", "European Union", "reflexive_after_canon"),
        # Sports-roster World-Cup noise.
        ("Kylian Mbappe", "member of", "Iraq", "sports_roster_triple"),
        ("Harry Kane", "member of", "Jude Bellingham", "sports_roster_triple"),
        # Source-publication subject = byline noise (the messenger as subject of a
        # REPORTING predicate, or a non-country value). A real org relation
        # ("BBC operates in United Kingdom") is NOT byline noise and must survive
        # (locked by test_filter_fact_extractor.test_junk_triple_dropped_at_ingestion).
        ("Reuters", "located in", "Gaza", "source_publication_subject"),
        ("Al Jazeera", "reports", "ceasefire talks", "source_publication_subject"),
    ],
)
def test_verbatim_garbage_triples_dropped(subject, predicate, value, reason):
    assert _pipeline_drop(subject, predicate, value) == reason


# ---------------------------------------------------------------------------
# Legitimate triples — every one MUST survive (no false drops).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "subject,predicate,value",
    [
        ("Paris", "capital of", "France"),
        ("Germany", "member of", "NATO"),
        ("Texas", "located in", "United States"),
        ("Apple Inc.", "located in", "Cupertino"),
        ("France", "located in", "Europe"),
        ("Vladimir Putin", "party to", "Russia"),  # person, non-membership pred
    ],
)
def test_legitimate_triples_survive(subject, predicate, value):
    assert _pipeline_drop(subject, predicate, value) is None


# ---------------------------------------------------------------------------
# Adjective / demonymic-plural value gate.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("Parisians", True),
        ("Londoners", True),
        ("Texans", True),
        ("Germans", True),          # demonymic plural value, not a typed entity
        ("Paris", False),           # a real place, no demonym suffix
        ("France", False),          # a country
        ("United States", False),   # multi-token name
        ("Iranian", False),         # NATIONAL demonym -> canon collapses, not dropped here
        ("New Yorkers", False),     # multi-token (caller's other gates own it)
        ("NATO", False),
    ],
)
def test_adjective_or_demonymic_value(value, expected):
    assert _is_adjective_or_demonymic_value(value) is expected


# ---------------------------------------------------------------------------
# Reflexive-after-canon — distinct RAW surfaces, one referent.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "subject,value,expected",
    [
        ("US", "US", True),
        ("US", "United States", True),
        ("America", "USA", True),
        ("EU", "European Union", True),
        ("UK", "Britain", True),
        ("China", "America", False),       # two distinct countries
        ("France", "Germany", False),
        ("Macron", "France", False),
    ],
)
def test_reflexive_after_canon(subject, value, expected):
    assert _is_reflexive_after_canon(subject, value) is expected


# ---------------------------------------------------------------------------
# Relation-DIRECTION sanity.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "subject,predicate,value,expected",
    [
        # capital of: country-subject inversion, non-country value.
        ("US", "capital of", "France", True),       # country as subject
        ("France", "capital of", "Parisians", True),  # value not a country
        ("Paris", "capital of", "France", False),    # correct direction
        # located in: a sovereign state inside a city/state is inverted.
        ("US", "located in", "New York", True),
        ("France", "located in", "Europe", False),   # country in a region: ok
        ("Texas", "located in", "United States", False),  # non-country subject
        # unrelated predicate: never an inversion here.
        ("Russia", "allied with", "China", False),
    ],
)
def test_inverted_relation(subject, predicate, value, expected):
    assert _is_inverted_relation(subject, _norm_pred(predicate), value) is expected


# ---------------------------------------------------------------------------
# Sports-roster triple shape (independent of the text topic gate).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "subject,predicate,value,expected",
    [
        ("Kylian Mbappe", "member of", "Iraq", True),       # person -> country
        ("Harry Kane", "member of", "Jude Bellingham", True),  # person -> person
        ("Lionel Messi", "plays for", "Argentina", True),
        ("Germany", "member of", "NATO", False),            # not a person subject
        ("Macron", "party to", "France", False),            # not a membership pred
        ("Apple Inc.", "part of", "United States", False),  # org subject
    ],
)
def test_roster_triple(subject, predicate, value, expected):
    assert _is_roster_triple(subject, _norm_pred(predicate), value) is expected


# ---------------------------------------------------------------------------
# Source-publication subject gate.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Reuters", True),
        ("reuters", True),
        ("Al Jazeera", True),
        ("BBC", True),
        ("The New York Times", True),
        ("AP", True),
        ("France", False),
        ("Emmanuel Macron", False),
        ("Apple Inc.", False),
    ],
)
def test_source_publication_subject(subject, expected):
    assert _is_source_publication_subject(subject) is expected


# ---------------------------------------------------------------------------
# Sports topic / relevance gate over the signal text.
# ---------------------------------------------------------------------------

def test_sports_dominated_text_is_gated():
    text = (
        "World Cup qualifier: the striker scored a hat-trick before half-time "
        "in the group stage fixture; the manager named his starting XI and the "
        "midfielder was substituted in injury time."
    )
    assert _text_is_sports_dominated(text) is True


def test_geopolitical_text_not_gated():
    text = (
        "The president met the prime minister to discuss the final agreement on "
        "sanctions and a ceasefire, with the foreign ministry confirming talks."
    )
    assert _text_is_sports_dominated(text) is False


def test_incidental_single_sports_mention_not_gated():
    # One sports word ("final") must NOT trip the gate (needs >=3 distinct hits).
    text = "The cabinet reached a final decision on the budget after the summit."
    assert _text_is_sports_dominated(text) is False


# ---------------------------------------------------------------------------
# Honest confidence — no longer a hardcoded 0.75 presented as calibrated.
# ---------------------------------------------------------------------------

def test_default_confidence_is_conservative_and_below_seed():
    # Below the curated-seed (0.95) and the prior 0.75; not presented as
    # calibrated. A real extractor score still overrides via the resolver.
    assert _INGESTION_DEFAULT_CONFIDENCE < 0.75
    assert _INGESTION_DEFAULT_CONFIDENCE < 0.95
    assert 0.0 < _INGESTION_DEFAULT_CONFIDENCE < 1.0


# ---------------------------------------------------------------------------
# Config: the new topic gate defaults ON and is rejectable.
# ---------------------------------------------------------------------------

def test_reject_sports_topic_defaults_on():
    cfg = FactExtractorConfig()
    assert cfg.reject_sports_topic is True
    cfg2 = FactExtractorConfig(reject_sports_topic=False)
    assert cfg2.reject_sports_topic is False
