# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W2 Agent-C unit tests — class-agnostic identity (D8), computed completeness
(D26), and the governance-side canon endpoints (D3).

Pure unit tests — no DB, no SLM, no network. Each input is a VERBATIM live
garbage / fragmentation string lifted from planning/PLATFORM_HEALTH_RESULTS.md:

  * D8: ``"turkey"`` lived as country / entity / location / person at once.
  * D7/D8: ``"Bank of England"`` / ``"Nippon Steel"`` / ``"Hyundai Motor Group"``
    were typed ``person`` — orgs must be ``organization``.
  * D26: ``completeness_score`` was an inert flat 0.3 constant on every row.
  * D3: a co-occurrence endpoint demonym ("Iranian") collapses to its country
    so an "Iran ↔ Iranian" edge degenerates to a self-edge (rejected), and
    HTML/possessive garbage ("Cape Verde&#039;s") canonicalizes clean.
"""

from __future__ import annotations

import pytest

from legba.data._entity_canon import canonicalize_entity
from legba.data.analysts.deterministic_handlers.entity_resolution import (
    _CLASS_PRIORITY,
    compute_completeness,
    resolve_entity_class,
)
from legba.data.vocabulary import ENTITY_CLASSES


# ---------------------------------------------------------------------------
# D8 — class-agnostic identity: ONE deterministic class per name.
# ---------------------------------------------------------------------------


def test_priority_order_members_are_taxonomy():
    # The priority ladder is drawn from the closed taxonomy — never invented.
    for cls in _CLASS_PRIORITY:
        assert cls in ENTITY_CLASSES


def test_country_name_lands_in_one_class_regardless_of_ner_guess():
    # The D8 "turkey lived in 4 classes at once" defect: a country name that the
    # canon gazetteer recognises must converge on the SAME single class for EVERY
    # NER guess, so it stops fragmenting. "Iran" is in the canon's curated
    # gazetteer (the canon forces country); the class resolver then keeps it.
    resolved = set()
    for ner_class in ("country", "entity", "location", "person"):
        name, canon_class = canonicalize_entity("Iran", ner_class)
        assert name == "Iran"
        resolved.add(resolve_entity_class(name, canon_class))
    assert resolved == {"country"}, resolved


def test_turkey_defrag_is_class_resolver_deterministic():
    # The literal "turkey"/"Turkey" surface no longer hits pycountry's gazetteer
    # (renamed "Türkiye"), so the canon does NOT force country for it — that
    # residual gap lives in the W1 canon (_entity_canon), NOT this file (see the
    # open-issues note in the report). What THIS file guarantees: the SAME canon
    # output ALWAYS resolves to the SAME class, so two mentions that converge on
    # the canon converge on one class. Demonyms whose country IS gazetteered
    # ("Iranian"/"French") de-frag fully to country, exercised here.
    for demonym, country in (("Iranian", "Iran"), ("French", "France")):
        name, cls = canonicalize_entity(demonym, "person")
        assert name == country
        assert resolve_entity_class(name, cls) == "country", (demonym, name, cls)


def test_org_surface_typed_organization_not_person():
    # The audit's exact org-as-person cases (D7/D8). Even when NER guessed
    # "person", the org-surface gazetteer must promote them to organization.
    for surface in ("Bank of England", "Nippon Steel", "Hyundai Motor Group"):
        name, canon_class = canonicalize_entity(surface, "person")
        assert resolve_entity_class(name, canon_class) == "organization", surface


def test_org_beats_person_by_priority():
    # organization outranks person on the ladder, so a surface that reads as
    # both resolves to organization.
    assert _CLASS_PRIORITY.index("organization") < _CLASS_PRIORITY.index("person")
    assert resolve_entity_class("Nippon Steel", "person") == "organization"


def test_person_surname_collision_stays_person():
    # "Michelle Steel" is a PERSON surnamed Steel — the org gazetteer's curated
    # given-name guard must keep her a person, not an org (no false de-frag).
    assert resolve_entity_class("Michelle Steel", "person") == "person"


def test_article_prefixed_surface_never_resolves_person():
    # 2026-07-21 review: NER can emit a positive person label for an
    # article-prefixed span ("the Golden State Warriors" minted person live,
    # 16 in 4 days) and the D8 election accepted it. No personal name starts
    # with the/a/an — demote to the generic bucket; reclassify settles it.
    assert resolve_entity_class("the Golden State Warriors", "person") == "entity"
    assert resolve_entity_class("The Elders", "person") == "entity"
    # a non-person class keeps its article-prefixed surface untouched
    assert resolve_entity_class("the Strait of Hormuz", "location") == "location"
    # and a plain personal name still resolves person
    assert resolve_entity_class("Ali Khamenei", "person") == "person"


def test_unknown_name_keeps_ner_class():
    # A plain person name with no org/country signal keeps its NER class.
    assert resolve_entity_class("Giorgia Meloni", "person") == "person"
    assert resolve_entity_class("Evian", "location") == "location"


def test_unknown_class_floors_to_entity():
    # An out-of-taxonomy class string floors to the generic bucket.
    assert resolve_entity_class("Some Thing", "weather_alert") == "entity"


def test_resolve_class_is_deterministic_idempotent():
    # Same inputs ⇒ same class, every call.
    a = resolve_entity_class("Bank of England", "person")
    b = resolve_entity_class("Bank of England", "person")
    assert a == b == "organization"


# ---------------------------------------------------------------------------
# D26 — completeness is COMPUTED from filled fields, not a flat 0.3.
# ---------------------------------------------------------------------------


def test_completeness_is_not_the_old_constant():
    # A bare name+class entity must NOT score the inert 0.3 the audit flagged.
    bare = compute_completeness(
        name="Acme",
        entity_class="organization",
        geo_country=None,
        geo_lat=None,
        geo_lon=None,
        alias_count=0,
    )
    assert bare != 0.3
    # name (0.30) + non-generic class (0.20) = 0.50 floor.
    assert bare == pytest.approx(0.50)


def test_completeness_rises_with_filled_fields():
    bare = compute_completeness(
        name="Iran",
        entity_class="country",
        geo_country=None,
        geo_lat=None,
        geo_lon=None,
        alias_count=0,
    )
    with_geo = compute_completeness(
        name="Iran",
        entity_class="country",
        geo_country="IR",
        geo_lat=32.0,
        geo_lon=53.0,
        alias_count=2,
    )
    assert with_geo > bare
    # name+class+geo_country+geo_latlon+aliases = full.
    assert with_geo == pytest.approx(1.0)


def test_completeness_generic_class_does_not_count():
    # The generic "entity" bucket is not a resolved class → no class weight.
    generic = compute_completeness(
        name="Thing",
        entity_class="entity",
        geo_country=None,
        geo_lat=None,
        geo_lon=None,
        alias_count=0,
    )
    assert generic == pytest.approx(0.30)


def test_completeness_bounded_unit_interval():
    full = compute_completeness(
        name="x",
        entity_class="country",
        geo_country="US",
        geo_lat=1.0,
        geo_lon=2.0,
        alias_count=99,
    )
    assert 0.0 <= full <= 1.0


def test_completeness_half_geo_does_not_award_latlon():
    # Only ONE of lat/lon present ⇒ no lat/lon weight (both required).
    half = compute_completeness(
        name="Iran",
        entity_class="country",
        geo_country="IR",
        geo_lat=32.0,
        geo_lon=None,
        alias_count=0,
    )
    # name(.30)+class(.20)+geo_country(.20) = 0.70, NO latlon.
    assert half == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# D3 — governance-side endpoint canonicalization (the canon the loop relies on).
# ---------------------------------------------------------------------------


def test_demonym_endpoint_collapses_to_self_edge():
    # "Iran" ↔ "Iranian" is the same referent (graph-centrality inflation). The
    # governance loop canonicalizes both endpoints, so they become equal and the
    # edge degenerates to a self-edge → rejected, never graduated.
    subj, _ = canonicalize_entity("Iran", "entity")
    obj, _ = canonicalize_entity("Iranian", "entity")
    assert subj.lower() == obj.lower() == "iran"


def test_html_possessive_endpoint_canonicalizes_clean():
    # "Cape Verde&#039;s" is verbatim live garbage — must canonicalize to a clean
    # endpoint the governance loop can promote (or, if a country, flow through).
    name, _ = canonicalize_entity("Cape Verde&#039;s", "entity")
    assert name == "Cape Verde"
    assert "&" not in name and "'" not in name


def test_alias_endpoint_normalizes():
    # "US" → "United States" so the promoted nexus carries the canonical referent.
    name, _ = canonicalize_entity("US", "entity")
    assert name == "United States"
