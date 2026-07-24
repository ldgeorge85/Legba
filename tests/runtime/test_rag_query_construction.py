# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M22 — the FOCUSED vector:world_context RAG query construction.

The pre-M22 query led with the unit's display NAME and appended the noisy slice-
entity pile (person/org names the officeholder-stripped corpus never contains),
diluting the geo/topic anchor and keeping on-target cosines below the floor. These
tests pin the recalibrated query: "<target country> <theme>", theme from the
descriptor (explicit rag_theme > cleaned name), country from the target id.
"""

from __future__ import annotations

from legba.runtime.analyst_deps_builder import (
    _clean_theme_from_name,
    _rag_theme_for_descriptor,
    _world_context_query,
)
from legba.runtime.grounding import target_country_name


# --- target_country_name --------------------------------------------------


def test_target_country_name_resolves_desks():
    assert target_country_name("country_g20_us") == "United States"
    assert target_country_name("country_watch_ir") == "Iran"
    assert target_country_name("country_g20_de") == "Germany"


def test_target_country_name_none_for_meta_or_unmapped():
    assert target_country_name(None) is None
    assert target_country_name("world_assessment") is None
    # a per-country slug we don't have a gazetteer name for → None (never a bare ISO)
    assert target_country_name("country_g20_zz") is None


# --- _clean_theme_from_name (fallback theme) ------------------------------


def test_clean_theme_strips_abstract_label_noise():
    assert _clean_theme_from_name("Leadership-Transition Risk Unit") == "leadership transition"
    assert _clean_theme_from_name("Internal-Stability / Coup-Risk Unit") == "internal stability coup"
    assert _clean_theme_from_name("Proliferation-Watch / WMD-Risk Unit") == "proliferation wmd"


# --- _rag_theme_for_descriptor --------------------------------------------


class _Ident:
    def __init__(self, name, id):
        self.name = name
        self.id = id


class _Grounding:
    def __init__(self, rag_theme=None):
        self.rag_theme = rag_theme


class _Desc:
    def __init__(self, name="Leadership-Transition Risk Unit", id="leadership_transition",
                 rag_theme=None):
        self.identity = _Ident(name, id)
        self.grounding = _Grounding(rag_theme)


def test_theme_prefers_explicit_rag_theme():
    d = _Desc(rag_theme="government structure and political system")
    assert _rag_theme_for_descriptor(d) == "government structure and political system"


def test_theme_falls_back_to_cleaned_name():
    assert _rag_theme_for_descriptor(_Desc()) == "leadership transition"


# --- _world_context_query -------------------------------------------------


def test_query_country_leads_theme_follows():
    q = _world_context_query(
        theme="government, internal security, and political stability",
        country_name="Germany",
    )
    assert q == "Germany government, internal security, and political stability"
    assert q.startswith("Germany")


def test_query_drops_person_entities_entirely():
    # No mechanism to inject slice entities on a per-country desk — the query is
    # exactly "<country> <theme>", nothing else.
    q = _world_context_query(theme="government and leadership", country_name="Iran")
    assert q == "Iran government and leadership"
    assert "Khamenei" not in q  # the corpus has no people; the query names none


def test_query_meta_run_uses_theme_plus_geo_terms():
    q = _world_context_query(
        theme="government and stability", country_name=None,
        geo_terms=["Russia", "China", "France", "Brazil"],
    )
    # theme leads, at most 2 geo terms follow (capped at 3 total).
    assert q.startswith("government and stability")
    assert "Russia" in q and "China" in q
    assert "France" not in q  # capped


def test_query_empty_when_nothing_to_query():
    assert _world_context_query(theme="", country_name=None) == ""
