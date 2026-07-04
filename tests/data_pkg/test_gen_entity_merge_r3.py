# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the DQ Phase-4 entity-merge GENERATOR — round-3 polish
(``scripts/gen_entity_merge_migration.py``).

Pure — no DB, no SLM, no network. Covers the three r3 fixes:

  * C1 GEO BACKFILL — a NULL-geo survivor inherits geo from a geo-bearing loser
    that is about to be hard-deleted; NEVER across a country mismatch; ISO-2 vs
    full-name is not a mismatch; lat/lon are copied together from ONE donor.
  * B1 JUNK-NAMED SURVIVOR — a fold whose elected survivor name is itself junk
    (World Cup / article variant / bare number-word) routes WHOLLY to junk with
    no surviving row; legit short forms (US/UK/…) are never swept.
  * B3 VERSION MONOTONICITY — the merge-version row is appended at version+1 and
    every merged survivor's version is bumped to match (SQL emission asserts).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gen_entity_merge_migration as gm  # noqa: E402

from legba.data._entity_canon import is_junk_entity  # noqa: E402


def _row(rid, name, cls, links, *, source="", created="2026-01-01",
         lat=None, lon=None, country=None, region=None, comp=None):
    return gm.Row(id=rid, name=name, cls=cls, source=source, created_at=created,
                  links=links, geo_lat=lat, geo_lon=lon, geo_country=country,
                  geo_region=region, completeness=comp)


def _survivors(plan):
    return {sid for _lid, sid in plan.merge_map}


# ---------------------------------------------------------------------------
# C1 — GEO BACKFILL
# ---------------------------------------------------------------------------


def test_geo_copied_from_geo_bearing_loser_to_null_geo_survivor():
    rows = [
        # highest-link survivor carries a country code but NO coordinates
        _row("s", "United Arab Emirates", "country", 49, country="AE"),
        # low-link loser carries the coordinates (full country name)
        _row("l", "The United Arab Emirates", "location", 1,
             lat=24.0002488, lon=54.0, country="United Arab Emirates",
             region="Abu Dhabi", comp=0.7),
    ]
    plan = gm.build_plan(rows)
    assert _survivors(plan) == {"s"}
    sg = {t[0]: t for t in plan.survivor_geo}
    assert "s" in sg, "the NULL-geo survivor should inherit the loser's geo"
    _sid, lat, lon, country, region, comp = sg["s"]
    # lat + lon copied TOGETHER from the one donor
    assert (lat, lon) == (24.0002488, 54.0)
    assert region == "Abu Dhabi"
    # the emitted country sentinel is the survivor's OWN code so the SQL
    # cross-country guard passes for this verified-consistent donor
    assert country == "AE"
    assert comp == 0.7


def test_geo_never_copied_across_a_country_mismatch():
    rows = [
        # survivor has a country (GE) but no coordinates
        _row("s", "Zormania", "entity", 50, country="GE"),
        # loser carries coordinates for a DIFFERENT country (US)
        _row("l", "Zormania", "person", 5, lat=1.0, lon=1.0, country="US"),
    ]
    plan = gm.build_plan(rows)
    # the cluster still merges (survivor = most links) ...
    assert _survivors(plan) == {"s"}
    # ... but NO geo is donated across the country mismatch.
    assert all(t[0] != "s" for t in plan.survivor_geo)


def test_iso2_vs_full_country_name_is_not_a_mismatch():
    # survivor country 'VN', donor country 'Vietnam' -> SAME country -> backfill
    rows = [
        _row("s", "Vietnam", "country", 13, country="VN"),
        _row("l", "Viet Nam", "location", 2, lat=14.0, lon=108.0,
             country="Vietnam"),
    ]
    plan = gm.build_plan(rows)
    sg = {t[0]: t for t in plan.survivor_geo}
    assert "s" in sg
    assert (sg["s"][1], sg["s"][2]) == (14.0, 108.0)


def test_geo_donor_is_deterministic_smallest_id_when_survivor_has_no_country():
    rows = [
        _row("surv", "Springfield", "entity", 20),  # no country, no geo
        _row("l-b", "Springfield", "location", 5, lat=2.0, lon=2.0, country="X"),
        _row("l-a", "Springfield", "person", 4, lat=1.0, lon=1.0, country="Y"),
    ]
    plan = gm.build_plan(rows)
    sg = {t[0]: t for t in plan.survivor_geo}
    assert "surv" in sg
    # smallest id ('l-a') wins deterministically
    assert (sg["surv"][1], sg["surv"][2]) == (1.0, 1.0)


def test_geo_not_touched_when_survivor_already_has_coordinates():
    rows = [
        _row("s", "Paris", "location", 30, lat=48.8, lon=2.3, country="FR"),
        _row("l", "Paris", "person", 5, lat=1.0, lon=1.0, country="FR"),
    ]
    plan = gm.build_plan(rows)
    # survivor already has geo -> nothing to backfill
    assert all(t[0] != "s" for t in plan.survivor_geo)


# ---------------------------------------------------------------------------
# B1 — JUNK-NAMED SURVIVOR routes the WHOLE fold to junk
# ---------------------------------------------------------------------------


def test_world_cup_fold_routes_wholly_to_junk_no_surviving_row():
    rows = [
        _row("wc1", "World Cup", "person", 240),
        _row("wc2", "The World Cup", "person", 38),
        _row("wc3", "A World Cup", "person", 6),
        _row("wc4", "world cup", "entity", 1),
    ]
    plan = gm.build_plan(rows)
    assert set(plan.junk) == {"wc1", "wc2", "wc3", "wc4"}
    assert plan.merge_map == []          # no losers -> no survivor kept
    assert _survivors(plan) == set()
    assert plan.junk_from_survivor == 1  # routed AFTER election


def test_article_variant_survivor_still_routed_via_prefix_strip():
    # the HIGHEST-link member is the article variant ("The World Cup"), which
    # is_junk_entity does not flag directly -> the leading-article strip catches
    # the junk core "World Cup".
    rows = [
        _row("a", "The World Cup", "person", 300),
        _row("b", "World Cup", "person", 10),
    ]
    plan = gm.build_plan(rows)
    assert set(plan.junk) == {"a", "b"}
    assert plan.merge_map == []
    assert plan.junk_from_survivor == 1


def test_first_fold_routes_wholly_to_junk():
    rows = [
        _row("f1", "first", "entity", 326),
        _row("f2", "first", "person", 68),
        _row("f3", "first", "location", 1),
    ]
    plan = gm.build_plan(rows)
    # 'first' is a bare number-word -> every member is junk -> whole fold junked
    assert set(plan.junk) == {"f1", "f2", "f3"}
    assert plan.merge_map == []
    assert _survivors(plan) == set()


def test_two_fold_routes_wholly_to_junk():
    rows = [
        _row("t1", "Two", "entity", 295),
        _row("t2", "two", "person", 44),
    ]
    plan = gm.build_plan(rows)
    assert set(plan.junk) == {"t1", "t2"}
    assert plan.merge_map == []


def test_legit_short_forms_are_never_swept_to_junk():
    # US/USA fold to United States and MERGE — never routed to junk.
    rows = [
        _row("us", "US", "country", 100),
        _row("usa", "USA", "country", 50),
    ]
    plan = gm.build_plan(rows)
    assert not (set(plan.junk) & {"us", "usa"})
    assert _survivors(plan) & {"us", "usa"}


def test_number_words_are_junk_but_real_short_forms_are_not():
    for j in ["one", "two", "Two", "first", "First", "million", "hundred"]:
        assert is_junk_entity(j) is True, f"{j!r} should be junk"
    for k in ["US", "UK", "EU", "UN", "WHO", "NATO", "Iran", "Chad",
              "Georgia", "last", "next"]:
        assert is_junk_entity(k) is False, f"{k!r} should NOT be junk"


def test_survivor_junk_gate_excludes_length2_and_html_residue():
    # INHERENT non-referent junk -> routed
    for j in ["World Cup", "The World Cup", "A World Cup", "round of 32",
              "US Open", "third", "fifth", "Group F"]:
        assert gm._survivor_is_junk(j) is True, f"{j!r} should route the fold to junk"
    # length<=2 abbreviations are REAL entities the length rule flags -> NEVER
    # routed (LA carries ~1148 links in the live snapshot).
    for k in ["LA", "DC", "VA", "Xi", "G7", "G8", "DW", "TV", "PM", "SA",
              "EC", "C2", "6B", "VK"]:
        assert gm._survivor_is_junk(k) is False, f"{k!r} must NOT route the fold to junk"
    # HTML-residue surfaces are a DIRTY surface of a real referent -> NEVER routed
    # (routing would delete the clean twin that folds with it).
    for r in ["Daniel Noboa.</p", "Jeanne Shaheen</a", "the NATO Defense College</a",
              "Yokota Air Base.</p", "the Veterans Health Administration.</p"]:
        assert gm._survivor_is_junk(r) is False, f"{r!r} must NOT route the fold to junk"


def test_high_link_length2_abbreviation_fold_merges_and_is_not_junked():
    # 'LA'/'L.A' fold together; the 1148-link 'LA' survives (never junk-deleted).
    rows = [
        _row("la", "LA", "location", 1148),
        _row("la2", "L.A", "entity", 2),
    ]
    plan = gm.build_plan(rows)
    assert set(plan.junk) == set()
    assert _survivors(plan) == {"la"}
    assert plan.junk_from_survivor == 0


def test_html_residue_survivor_fold_keeps_the_real_referent():
    # a residue survivor + its clean twin must NOT be wholly junked.
    rows = [
        _row("res", "Daniel Noboa.</p", "person", 5),
        _row("clean", "Daniel Noboa", "person", 2),
    ]
    plan = gm.build_plan(rows)
    assert not ({"res", "clean"} <= set(plan.junk)), "a real referent must survive"
    assert _survivors(plan)  # the fold still merges onto a surviving row
    assert plan.junk_from_survivor == 0


# ---------------------------------------------------------------------------
# B3 — VERSION MONOTONICITY (SQL emission)
# ---------------------------------------------------------------------------


def test_merge_version_appended_at_version_plus_one_and_bumped_once():
    rows = [
        _row("a", "Kyiv City Council", "person", 10),
        _row("b", "the Kyiv City Council", "person", 3),
    ]
    plan = gm.build_plan(rows)
    sql = gm.emit_sql(plan)
    # merge-version row inserted ABOVE the current version (not duplicating it)
    assert "SELECT s.id, s.version + 1," in sql
    # exactly two 's.version + 1' sites: the append (3) and the bump (3b) —
    # the step-5 rewrite no longer touches version.
    assert sql.count("s.version + 1") == 2
    # the version bump is guarded on the merge-version marker (idempotent)
    assert "v.data->>'event' = 'merge_0063'" in sql


# ---------------------------------------------------------------------------
# DML ORDER + geo guard in the emitted SQL
# ---------------------------------------------------------------------------


def test_emitted_sql_dml_order_and_geo_guard():
    rows = [
        _row("s", "United Arab Emirates", "country", 49, country="AE"),
        _row("l", "The United Arab Emirates", "location", 1,
             lat=24.0, lon=54.0, country="United Arab Emirates"),
        _row("j", "Group F", "entity", 2),  # singleton content junk
    ]
    plan = gm.build_plan(rows)
    sql = gm.emit_sql(plan)
    i_repoint = sql.index("(1) RE-POINT")
    i_geo = sql.index("FROM _survivor_geo g")
    i_vappend = sql.index("SELECT s.id, s.version + 1,")
    i_ldelete = sql.index("DELETE FROM entity_profiles WHERE id IN (SELECT loser_id")
    i_rewrite = sql.index("(5) SURVIVOR REWRITE")
    i_jdelete = sql.index("DELETE FROM entity_profiles WHERE id IN (SELECT entity_id FROM _junk")
    # re-point < geo backfill < version append < loser DELETE < rewrite < junk DELETE
    assert i_repoint < i_geo < i_vappend < i_ldelete < i_rewrite < i_jdelete
    # cross-country guard mirrors entity_resolution's ON-CONFLICT geo rule
    assert "lower(s.geo_country) = lower(g.geo_country)" in sql
    # completeness never regressed
    assert "completeness_score = GREATEST(" in sql


def test_generator_is_deterministic_with_geo_and_junk_survivors():
    rows = [
        _row("wc1", "World Cup", "person", 240),
        _row("wc2", "The World Cup", "person", 38),
        _row("s", "Vietnam", "country", 13, country="VN"),
        _row("l", "Viet Nam", "location", 2, lat=14.0, lon=108.0,
             country="Vietnam"),
    ]
    p1 = gm.build_plan(list(rows))
    p2 = gm.build_plan(list(reversed(rows)))
    assert p1.merge_map == p2.merge_map
    assert p1.junk == p2.junk
    assert p1.survivor_geo == p2.survivor_geo
    assert p1.junk_from_survivor == p2.junk_from_survivor
    assert gm.emit_sql(p1) == gm.emit_sql(p2)
