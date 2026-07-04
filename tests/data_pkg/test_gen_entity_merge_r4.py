# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the DQ Phase-4 entity-merge GENERATOR — round-4 geo fix
(``scripts/gen_entity_merge_migration.py``).

Pure — no DB, no SLM, no network. Covers the r4 name-derived country gate on the
geo backfill: a NULL-geo survivor may inherit a hard-deleted loser's coordinates
ONLY when the loser's country AGREES with the country the survivor's OWN NAME
resolves to offline. This mirrors ``_entity_geo.resolve_entity_geo_offline`` and
closes the round-3 defect where the backfill stamped a WRONG-country geo:

  * 'DR Congo' / 'the Democratic Republic of Congo' (name resolves to a Congo,
    donor a DIFFERENT Congo) -> NO backfill (stays honest-NULL),
  * 'Evian' / 'The Hague' (name is NOT a country at all) -> NO backfill,
  * 'Iran' / 'Vietnam' / 'Taiwanese' (name resolves to a country, donor agrees)
    -> backfill kept, stamped with the NAME's own ISO-2.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gen_entity_merge_migration as gm  # noqa: E402


def _row(rid, name, cls, links, *, source="", created="2026-01-01",
         lat=None, lon=None, country=None, region=None, comp=None):
    return gm.Row(id=rid, name=name, cls=cls, source=source, created_at=created,
                  links=links, geo_lat=lat, geo_lon=lon, geo_country=country,
                  geo_region=region, completeness=comp)


def _geo(plan):
    return {t[0]: t for t in plan.survivor_geo}


# ---------------------------------------------------------------------------
# WRONG-country donor is refused (the round-3 defect)
# ---------------------------------------------------------------------------


def test_dr_congo_survivor_not_backfilled_from_congo_brazzaville_donor():
    # The exact live wrong stamp: a 'DR Congo' (DRC) survivor was given a
    # Congo-Brazzaville (the OTHER Congo) donor's coordinates. The name's own
    # country never agrees with a 'Congo-Brazzaville' donor -> NO backfill.
    rows = [
        _row("s", "DR Congo", "person", 40),
        _row("l", "DR Congo", "location", 3, lat=-0.72, lon=15.64,
             country="Congo-Brazzaville", comp=0.9),
    ]
    plan = gm.build_plan(rows)
    assert ("l", "s") in plan.merge_map, "the fold still merges"
    assert "s" not in _geo(plan), "a Congo mismatch must NOT backfill geo"


def test_democratic_republic_survivor_not_backfilled_across_congo_mismatch():
    # A 'the Democratic Republic of Congo' survivor whose name offline-resolves to
    # a Congo must not inherit a donor labelled as the DIFFERENT Congo (ISO 'CD').
    rows = [
        _row("s", "the Democratic Republic of Congo", "person", 40),
        _row("l", "the Democratic Republic of Congo", "location", 3,
             lat=-2.98, lon=23.82, country="Democratic Republic of the Congo"),
    ]
    plan = gm.build_plan(rows)
    assert "s" not in _geo(plan)


# ---------------------------------------------------------------------------
# A non-country place NAME is never backfilled (the Evian->India bug class)
# ---------------------------------------------------------------------------


def test_evian_non_country_name_never_backfilled():
    # 'Evian' is a town, not a country -> the name resolves to NO country, so a
    # mentioning (India) donor is NEVER attached. This is the canonical bug.
    rows = [
        _row("s", "Evian", "entity", 40),
        _row("l", "Evian", "location", 3, lat=22.35, lon=78.66, country="India"),
    ]
    plan = gm.build_plan(rows)
    assert "s" not in _geo(plan)


def test_the_hague_non_country_name_never_backfilled():
    # 'The Hague' IS in the Netherlands, but the NAME is a city, not a country ->
    # offline we cannot verify it, so we stay honest-NULL rather than stamp NL.
    rows = [
        _row("s", "The Hague", "person", 40),
        _row("l", "The Hague", "location", 3, lat=52.24, lon=5.63,
             country="Netherlands"),
    ]
    plan = gm.build_plan(rows)
    assert "s" not in _geo(plan)


# ---------------------------------------------------------------------------
# A country-named survivor with an AGREEING donor keeps its backfill
# ---------------------------------------------------------------------------


def test_country_named_survivor_inherits_same_country_donor():
    # 'Iran' resolves to IR; a full-name 'Iran' donor agrees -> backfill kept, and
    # the stamped country is the NAME's own ISO-2 (authoritative, not the donor
    # string 'Iran'). ISO-2 vs full-name equivalence: donor 'Iran' == expected IR.
    rows = [
        _row("s", "Iran", "person", 40),
        _row("l", "Iran", "location", 3, lat=36.26, lon=59.59, country="Iran",
             region="Razavi Khorasan", comp=0.8),
    ]
    plan = gm.build_plan(rows)
    sg = _geo(plan)
    assert "s" in sg, "a name-consistent donor must backfill"
    _sid, lat, lon, country, region, comp = sg["s"]
    assert (lat, lon) == (36.26, 59.59)   # lat+lon copied together from one donor
    assert country == "IR"                # NAME-derived ISO-2, not 'Iran'
    assert region == "Razavi Khorasan"
    assert comp == 0.8


def test_iso2_full_name_equivalence_backfills_without_stored_country():
    # survivor has NO stored geo_country; name 'Vietnam' resolves to VN; a
    # full-name 'Vietnam' donor agrees -> backfill, stamped 'VN'.
    rows = [
        _row("s", "Vietnam", "entity", 20),
        _row("l", "Viet Nam", "location", 3, lat=14.0, lon=108.0,
             country="Vietnam"),
    ]
    plan = gm.build_plan(rows)
    sg = _geo(plan)
    assert "s" in sg
    assert (sg["s"][1], sg["s"][2]) == (14.0, 108.0)
    assert sg["s"][3] == "VN"


def test_demonym_survivor_canon_resolves_then_backfills():
    # 'Taiwanese' is a demonym: the RAW surface resolves to NO country
    # (extract('Taiwanese') is None), but the canon DISPLAY collapses it to a
    # Taiwan form (TW), so a Taiwan donor agrees and the backfill is kept. This
    # proves the gate resolves the survivor's CANON display name, not its raw
    # surface — exactly the live 'Taiwanese' survivor.
    rows = [
        _row("s", "Taiwanese", "entity", 40),
        _row("l", "Taiwanese", "location", 3, lat=23.97, lon=120.98,
             country="Taiwan", comp=1.0),
    ]
    plan = gm.build_plan(rows)
    sg = _geo(plan)
    assert "s" in sg
    assert (sg["s"][1], sg["s"][2]) == (23.97, 120.98)
    assert sg["s"][3] == "TW"


# ---------------------------------------------------------------------------
# Determinism of the whole plan with the r4 gate
# ---------------------------------------------------------------------------


def test_generator_deterministic_with_r4_geo_gate():
    rows = [
        _row("s1", "DR Congo", "person", 40),
        _row("l1", "DR Congo", "location", 3, lat=-0.72, lon=15.64,
             country="Congo-Brazzaville"),
        _row("s2", "Vietnam", "entity", 20),
        _row("l2", "Viet Nam", "location", 3, lat=14.0, lon=108.0,
             country="Vietnam"),
        _row("s3", "Evian", "entity", 15),
        _row("l3", "Evian", "location", 2, lat=22.35, lon=78.66, country="India"),
    ]
    p1 = gm.build_plan(list(rows))
    p2 = gm.build_plan(list(reversed(rows)))
    assert p1.survivor_geo == p2.survivor_geo
    # only the country-consistent Vietnam survivor is backfilled
    assert {t[0] for t in p1.survivor_geo} == {"s2"}
    assert gm.emit_sql(p1) == gm.emit_sql(p2)
