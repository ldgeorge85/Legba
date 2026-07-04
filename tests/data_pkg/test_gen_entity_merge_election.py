# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the DQ Phase-4 entity-merge GENERATOR
(``scripts/gen_entity_merge_migration.py``) — round-2 LOCKED REDESIGN.

Pure — no DB, no SLM, no network. Exercises ``build_plan`` over synthetic
``Row`` clusters that mirror the live folds the orchestrator ground-truthed
(Trump / Vienna / Yonhap / New Orleans / Turkey / Norman), asserting:

  * survivor election is MOST-LINKS (not class-priority): the 489-link 'entity'
    Trump wins, NOT the 4-link 'location' one;
  * survivor CLASS is canon-authoritative (Vienna -> location, Air Force ->
    organization, South America -> location) and never a low-link fragment's;
  * seeds do NOT auto-win (a 0-link seed Vienna stub is a loser);
  * the country/location homonym rows are PROTECTED but the rest still merges;
  * a balanced bare-token homonym (Norman) is held for manual review while a
    dominant-referent one (Trump/Yonhap) still merges;
  * a pure-stopword cluster ("the") is junked, never elected a survivor;
  * the plan is deterministic (identical on re-run).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Load the offline generator by file path (scripts/ is not an installed package).
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gen_entity_merge_migration as gm  # noqa: E402


def _row(rid: str, name: str, cls: str, links: int, *, source: str = "",
         created: str = "2026-01-01") -> gm.Row:
    return gm.Row(id=rid, name=name, cls=cls, source=source,
                  created_at=created, links=links)


def _plan(rows):
    return gm.build_plan(list(rows))


def _survivor_ids(plan):
    return {sid for _lid, sid in plan.merge_map}


def _losers_of(plan, sid):
    return {lid for lid, s in plan.merge_map if s == sid}


# ---------------------------------------------------------------------------
# Election is MOST-LINKS, not class-priority
# ---------------------------------------------------------------------------


def test_trump_survivor_is_the_high_link_entity_row_not_location():
    rows = [
        _row("s-entity", "Trump", "entity", 489),
        _row("l-person", "Trump", "person", 128),
        _row("l-location", "Trump", "location", 4),
    ]
    plan = _plan(rows)
    # The 489-link entity row survives — NOT the class-priority 'location' one.
    assert _survivor_ids(plan) == {"s-entity"}
    assert _losers_of(plan, "s-entity") == {"l-person", "l-location"}
    # Class stays 'entity' (link-plurality); name unchanged -> no rewrite.
    assert plan.survivor_rewrite == []
    # Trump is dominant (128/489 = 0.26 < 0.5) -> NOT held for manual review.
    assert plan.ambiguous_token == []
    assert plan.loser_links == 128 + 4


def test_yonhap_survivor_is_entity():
    rows = [
        _row("y-entity", "Yonhap", "entity", 751),
        _row("y-person", "Yonhap", "person", 4),
    ]
    plan = _plan(rows)
    assert _survivor_ids(plan) == {"y-entity"}
    assert plan.ambiguous_token == []  # dominant -> merges


def test_new_orleans_multiword_merges_location_survivor():
    rows = [
        _row("no-loc", "New Orleans", "location", 632),
        _row("no-per", "New Orleans", "person", 460),
    ]
    plan = _plan(rows)
    assert _survivor_ids(plan) == {"no-loc"}
    # Multi-word survivor name -> the bare-token guard never applies.
    assert plan.ambiguous_token == []
    assert _losers_of(plan, "no-loc") == {"no-per"}


# ---------------------------------------------------------------------------
# Survivor CLASS is canon-authoritative; a 0-link seed does not win
# ---------------------------------------------------------------------------


def test_vienna_survivor_is_location_seed_stub_is_a_loser():
    rows = [
        _row("v-loc", "Vienna", "location", 24, created="2026-02-01"),
        _row("v-seed", "Vienna", "entity", 0, source="seed", created="2026-01-01"),
    ]
    plan = _plan(rows)
    # Most-links wins over the (older) 0-link seed stub.
    assert _survivor_ids(plan) == {"v-loc"}
    assert _losers_of(plan, "v-loc") == {"v-seed"}
    # Canon types Vienna -> location; name+class already match -> no rewrite.
    assert plan.survivor_rewrite == []


def test_air_force_survivor_class_forced_organization_by_canon():
    rows = [
        _row("af1", "Air Force", "person", 34),
        _row("af2", "The Air Force", "person", 22),
    ]
    plan = _plan(rows)
    assert _survivor_ids(plan) == {"af1"}
    (sid, name, cls), = plan.survivor_rewrite
    assert sid == "af1"
    assert name == "Air Force"
    assert cls == "organization"  # canon override, NOT the stored 'person'


def test_south_america_region_adjective_survivor_class_location():
    rows = [
        _row("sa1", "South American", "person", 7),
        _row("sa2", "the South Americans", "person", 2),
        _row("sa3", "South America", "person", 1),
    ]
    plan = _plan(rows)
    assert _survivor_ids(plan) == {"sa1"}
    (sid, name, cls), = plan.survivor_rewrite
    assert (name, cls) == ("South America", "location")


# ---------------------------------------------------------------------------
# Country/location homonym: protect the two rows, still merge the rest
# ---------------------------------------------------------------------------


def test_turkey_protects_country_and_location_merges_the_rest():
    rows = [
        _row("t-country", "Turkey", "country", 108, source="seed"),
        _row("t-entity", "Turkey", "entity", 54),
        _row("t-loc", "Turkey", "location", 5),
        _row("t-person", "Turkey", "person", 8),
        _row("tk-entity", "Turkish", "entity", 15),
        _row("tk-person", "Turkish", "person", 7),
        _row("tk-loc", "Turkish", "location", 4),
    ]
    plan = _plan(rows)
    merged_ids = {lid for lid, _ in plan.merge_map} | _survivor_ids(plan)
    junk = set(plan.junk)
    # The country + location 'Turkey' rows are PROTECTED (untouched).
    assert "t-country" not in merged_ids and "t-country" not in junk
    assert "t-loc" not in merged_ids and "t-loc" not in junk
    # The rest still merges onto the highest-link mergeable row (entity 54).
    assert _survivor_ids(plan) == {"t-entity"}
    assert _losers_of(plan, "t-entity") == {"t-person", "tk-entity", "tk-person", "tk-loc"}
    # The survivor is NOT rewritten to (Turkey, country) — that slot is held by
    # the protected country row, so the class reverts to the stored 'entity'.
    assert plan.survivor_rewrite == []
    # Reported as a country/location homonym.
    assert len(plan.ambiguous_geo) == 1
    assert plan.ambiguous_geo[0]["fold"] == "turkey"


# ---------------------------------------------------------------------------
# Bare-token homonym: balanced split -> held; dominant -> merged
# ---------------------------------------------------------------------------


def test_norman_balanced_bare_token_held_for_manual_review():
    rows = [
        _row("n-loc", "Norman", "location", 429),
        _row("n-entity", "Norman", "entity", 305),
    ]
    plan = _plan(rows)
    # Balanced (305/429 = 0.71 >= 0.5) -> NOT merged, held for manual review.
    assert plan.merge_map == []
    assert len(plan.ambiguous_token) == 1
    assert plan.ambiguous_token[0]["fold"] == "norman"


def test_dominant_bare_token_still_merges_not_held():
    # 90% dominant (Lee-shape) -> merges, never held (mirrors Trump/Yonhap).
    rows = [
        _row("lee-e", "Lee", "entity", 71),
        _row("lee-p", "Lee", "person", 8),
    ]
    plan = _plan(rows)
    assert _survivor_ids(plan) == {"lee-e"}
    assert plan.ambiguous_token == []


# ---------------------------------------------------------------------------
# Stopword clusters are junked, never elected a survivor
# ---------------------------------------------------------------------------


def test_the_stopword_cluster_is_junked_never_a_survivor():
    rows = [
        _row("the1", "the", "entity", 5),
        _row("the2", "The", "entity", 3),
    ]
    plan = _plan(rows)
    assert set(plan.junk) == {"the1", "the2"}
    assert plan.merge_map == []
    # "the" appears in no survivor rewrite / no survivor row.
    assert all(name.strip().lower() != "the"
               for _sid, name, _cls in plan.survivor_rewrite)


def test_singleton_stopword_is_junked():
    plan = _plan([_row("only-the", "the", "entity", 9),
                  _row("real", "Ukraine", "country", 3, source="seed")])
    assert "only-the" in set(plan.junk)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_build_plan_is_deterministic():
    rows = [
        _row("a", "Trump", "entity", 489),
        _row("b", "Trump", "person", 128),
        _row("c", "Norman", "location", 429),
        _row("d", "Norman", "entity", 305),
        _row("e", "Air Force", "person", 34),
        _row("f", "The Air Force", "person", 22),
    ]
    p1 = _plan(rows)
    p2 = _plan(list(reversed(rows)))  # input order must not matter
    assert p1.merge_map == p2.merge_map
    assert p1.survivor_rewrite == p2.survivor_rewrite
    assert p1.junk == p2.junk
    assert [a["fold"] for a in p1.ambiguous_token] == [a["fold"] for a in p2.ambiguous_token]


def test_no_id_is_both_survivor_and_loser():
    rows = [
        _row("a", "Kyiv Council", "person", 10),
        _row("b", "the Kyiv Council", "person", 3),
    ]
    plan = _plan(rows)
    survivors = _survivor_ids(plan)
    losers = {lid for lid, _ in plan.merge_map}
    assert survivors.isdisjoint(losers)
    assert survivors.isdisjoint(set(plan.junk))
