# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ROSTER-2 — every remaining copy of the nine-unit roster reads ONE resolver.

ROSTER-1 made ``legba.data.rosters`` the roster reader; ROSTER-2 folds what it
left: the per-unit data keyed by unit id (desk_reference's query terms, the
title-frame gauge's vocabulary, the reference builder's query patterns, the
correctness grader's proliferation constant), the tag-targets desk list, the
catalog's hand-kept state-media id set, and the workstation's unit literals.
This file proves:

  * the resolver returns TODAY's tuples (each moved literal pinned by the
    sha256 of its value at the pin, d1893001);
  * every per-unit dict's keys are a subset of ``unit_roster()``;
  * a roster unit with no data degrades BY NAME — skipped and reported,
    never a ``KeyError``;
  * each unit's ``scope_tag`` is its descriptor's fan-out predicate;
  * ``bringup_tag_targets``' desk list is the profile's;
  * the catalog's state-media set is read from the catalog entries;
  * the three UI files carry no unit id.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
from datetime import date

import pytest
import yaml

from legba.data import rosters
from legba.data.analysts.deterministic_handlers import _reference_manifest as manifest
from legba.data.analysts.deterministic_handlers import _title_frame_gauge as gauge
from legba.data.analysts.deterministic_handlers import collection_gap
from legba.data.analysts.deterministic_handlers import desk_reference as dr
from legba.data.analysts.deterministic_handlers import scorecard_banding
from legba.data.profile import load_profile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GEO = REPO_ROOT / "profiles" / "geopolitics"
SANCTIONS = REPO_ROOT / "profiles" / "sanctions_export_control"
UI_SRC = REPO_ROOT / "legba-ui-v3" / "src"
UI_FILES = (
    UI_SRC / "lib" / "gapStripModel.ts",
    UI_SRC / "lib" / "feedProducers.ts",
    UI_SRC / "v4" / "why" / "CountryUnitsAssessment.tsx",
)

_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    import bringup_register_source_catalog as catalog  # noqa: E402
    import bringup_tag_targets as tag_targets  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)

#: sha256 (canonical JSON, sorted keys, tuples as lists) of each literal this
#: lane moved into the profile, measured on the pin d1893001.
_PIN_DIGESTS = {
    "desk_reference.UNIT_QUERY_TERMS": "5470324d69fd5ee182e7f83567e4741526166e1bcd19af8c1e1134221920dc37",
    "_title_frame_gauge.DIMENSION_TERMS": "726510ea34da2870d14e96a2bb261854e5c39a335507028df213a8a7fbf8dfe8",
    "_reference_manifest.QUERY_PATTERNS": "b7041253b85f5a9a21c98196b7f13e72ec9fe04bc75cbb012cddcabdbe46c229",
}

#: The 32 (desk, iso2) pairs bringup_tag_targets spelled as G20_ISO2 /
#: WATCH_ISO2 at the pin.
_PIN_DESKS = {
    *((f"country_g20_{i.lower()}", i) for i in
      "AR AU BR CA CN DE FR GB ID IN IT JP KR MX RU SA TR US ZA".split()),
    *((f"country_watch_{i.lower()}", i) for i in
      "IL IR UA TW KP PK SD ML BF NE CD MM HT".split()),
}


def _digest(d) -> str:
    canon = {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


@pytest.fixture
def restore_rosters():
    yield
    rosters.refresh()


def _profile_copy(tmp_path: pathlib.Path, mutate) -> pathlib.Path:
    """A loadable copy of the geopolitics manifest that still walks the real
    descriptors directory (absolute), mutated."""
    data = yaml.safe_load((GEO / "profile.yaml").read_text(encoding="utf-8"))
    data["descriptors_dir"] = str(REPO_ROOT / "descriptors")
    data["lifecycle"] = None
    data["prompts"] = None
    data["rosters"].pop("source_catalog", None)
    data["rosters"].pop("gazetteer", None)  # CANON-1: the gazetteer file is not copied
    data.pop("projection", None)  # VIEW-1: the view file is not copied
    data.pop("constants", None)  # SPLIT-3: inline instance data the copy does not need (validated against ui.frames)
    data.pop("subjects", None)  # SUBJECT-1: the label style; the platform default applies
    mutate(data)
    d = tmp_path / "geopolitics"
    d.mkdir()
    (d / "profile.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


def _unit(data, unit_id):
    return next(u for u in data["rosters"]["units"] if u["id"] == unit_id)


# ---------------------------------------------------------------------------
# the resolver returns today's tuples
# ---------------------------------------------------------------------------


def test_the_moved_literals_are_todays_values():
    assert _digest(dr.UNIT_QUERY_TERMS) == _PIN_DIGESTS["desk_reference.UNIT_QUERY_TERMS"]
    assert _digest(gauge.DIMENSION_TERMS) == _PIN_DIGESTS["_title_frame_gauge.DIMENSION_TERMS"]
    assert _digest(manifest.QUERY_PATTERNS) == _PIN_DIGESTS["_reference_manifest.QUERY_PATTERNS"]
    assert len(dr.UNIT_QUERY_TERMS) == 9 and len(gauge.DIMENSIONS) == 8
    assert dr.BOUNDED_UNITS == rosters.unit_roster()


def test_scored_subject_units_is_the_graders_old_tuple():
    """``scorecard_banding.DIMENSIONS + (correctness_grader.PROLIFERATION,)``
    at the pin — the grader's and the reference builder's dimension set."""
    assert rosters.scored_subject_units() == (*scorecard_banding.DIMENSIONS, "proliferation_watch")
    assert "disruption_status" in rosters.unit_roster()
    assert "disruption_status" not in rosters.scored_subject_units()


def test_the_packaged_export_round_trips_the_unit_data():
    from dataclasses import replace

    packaged = rosters._from_export(rosters.PACKAGED_EXPORT)
    direct = rosters.roster_set(load_profile(GEO))
    assert replace(packaged, origin="") == replace(direct, origin="")
    assert dict(packaged.unit_terms)["proliferation_watch"].scope_tag == "nuclear_watch"


# ---------------------------------------------------------------------------
# keyed by the roster
# ---------------------------------------------------------------------------


def test_every_per_unit_dict_is_keyed_by_the_roster():
    roster = set(rosters.unit_roster())
    for name, d in (
        ("desk_reference.UNIT_QUERY_TERMS", dr.UNIT_QUERY_TERMS),
        ("_title_frame_gauge.DIMENSION_TERMS", gauge.DIMENSION_TERMS),
        ("_reference_manifest.QUERY_PATTERNS", manifest.QUERY_PATTERNS),
        # doctrine, not profile data: stays code, keyed by the roster
        ("collection_gap.SOURCE_CLASSES_BY_DIMENSION", collection_gap.SOURCE_CLASSES_BY_DIMENSION),
    ):
        assert set(d) <= roster, f"{name} names units off the roster: {sorted(set(d) - roster)}"
    assert set(rosters.scored_subject_units()) <= roster
    assert set(rosters.unit_terms()) <= roster


def test_the_manifest_refuses_unit_data_it_cannot_read(tmp_path):
    from pydantic import ValidationError

    cases = [
        (lambda d: _unit(d, "escalation").update(title_terms=["Escalat"]), "lower-case"),
        (lambda d: _unit(d, "escalation").update(reference_queries=["{place} clash"]), "other than"),
        (lambda d: _unit(d, "escalation").update(reference_queries=["border clash"]), "names no"),
        (lambda d: _unit(d, "escalation").update(scope_note="why"), "without a scope_tag"),
        (lambda d: _unit(d, "escalation").update(scope_tag="g20"), "fire on every scored subject"),
        (lambda d: _unit(d, "escalation").update(query_terms=" padded "), "whitespace"),
        (lambda d: _unit(d, "escalation").update(colour="red"), "colour"),
    ]
    for i, (mutate, err) in enumerate(cases):
        sub = tmp_path / str(i)
        sub.mkdir()
        with pytest.raises(ValidationError, match=err):
            load_profile(_profile_copy(sub, mutate))


# ---------------------------------------------------------------------------
# a roster unit with no data degrades by name
# ---------------------------------------------------------------------------


def _strip_escalation(data):
    u = _unit(data, "escalation")
    for key in ("query_terms", "title_terms", "reference_queries", "label", "short"):
        u.pop(key, None)


def test_a_unit_with_no_terms_reads_as_no_terms(tmp_path, restore_rosters):
    rosters.refresh(_profile_copy(tmp_path, _strip_escalation))
    assert "escalation" in rosters.unit_roster()
    assert rosters.terms_for("escalation") == rosters.NO_TERMS
    assert rosters.terms_for("not_a_unit") == rosters.NO_TERMS
    assert "escalation" not in rosters.unit_query_terms()
    assert "escalation" not in rosters.unit_title_terms()
    assert "escalation" not in rosters.unit_reference_queries()
    assert "escalation" in rosters.scored_subject_units()     # still a unit, just termless


class _FakeConn:
    """The three reads _load_population makes, answered from memory."""

    def __init__(self, questions: dict[str, str]):
        self.questions = questions

    async def fetch(self, sql, *args):
        if sql is dr._BOUNDED_QUESTIONS_SQL:
            return [{"analyst_id": u, "bounded_question": q} for u, q in self.questions.items()]
        return []


async def test_the_desk_reference_skips_a_termless_unit_by_name(tmp_path, restore_rosters, monkeypatch):
    rosters.refresh(_profile_copy(tmp_path, _strip_escalation))
    monkeypatch.setattr(dr, "UNIT_QUERY_TERMS", rosters.unit_query_terms())
    assert dr.build_query("Israel", "escalation", date(2026, 9, 5)) == ""
    assert dr.build_query("Israel", "energy_security", date(2026, 9, 5)).startswith("Israel energy security")
    units = rosters.unit_roster()
    pairs, _targets, _questions, warnings = await dr._load_population(
        _FakeConn({u: f"what about {u}?" for u in units}),
        targets=[], units=units, census_days=7,
    )
    assert pairs == []
    assert any("no query_terms" in w and "escalation" in w for w in warnings), warnings


def test_the_gauge_and_the_reference_builder_degrade_by_name(tmp_path, restore_rosters, monkeypatch):
    rosters.refresh(_profile_copy(tmp_path, _strip_escalation))
    monkeypatch.setattr(gauge, "DIMENSION_TERMS", rosters.unit_title_terms())
    assert "escalation" not in gauge.title_dimensions("Shelling escalates along the front line")
    monkeypatch.setattr(manifest, "QUERY_PATTERNS", rosters.unit_reference_queries())
    guidance = manifest.query_guidance("Australia", ["escalation", "energy_security"])
    assert "`escalation` — `Australia escalation announcement`" in guidance   # the generic patterns
    assert "`Australia gas supply pipeline outage`" in guidance


def test_the_sanctions_skeleton_declares_no_terms_and_loses_nothing(restore_rosters):
    rosters.refresh(load_profile(SANCTIONS))
    assert len(rosters.unit_roster()) == 5
    assert rosters.unit_terms() == {}
    assert rosters.scored_subject_units() == rosters.unit_roster()   # no leg-scoped unit


# ---------------------------------------------------------------------------
# scope tags are the descriptors' predicates
# ---------------------------------------------------------------------------


def test_each_units_scope_tag_is_its_descriptor_predicate():
    profile = load_profile(GEO)
    broad = " or ".join(f'has_tag("{t}")' for t in profile.rosters.scored_subject_tags)
    for u in profile.rosters.units:
        body = yaml.safe_load((REPO_ROOT / "descriptors" / f"analyst_{u.id}.yaml").read_text(encoding="utf-8"))
        predicate = body["subscription"]["targets"]["predicate"]
        want = f'has_tag("{u.scope_tag}")' if u.scope_tag else broad
        assert predicate == want, u.id


# ---------------------------------------------------------------------------
# the tag-targets desk list is the profile's
# ---------------------------------------------------------------------------


def test_the_tag_targets_desk_list_is_the_profiles():
    desks = tag_targets.desks()
    assert set(desks) == _PIN_DESKS and len(desks) == 32
    assert tag_targets.desks(load_profile(GEO)) == desks
    for _did, iso2 in desks:
        assert iso2 in tag_targets.REGION_BY_ISO2


def test_the_tag_targets_desk_list_follows_the_profile(tmp_path):
    only_watch = load_profile(_profile_copy(
        tmp_path, lambda d: d["rosters"].update(scored_subject_tags=["watch"]),
    ))
    assert {did for did, _ in tag_targets.desks(only_watch)} == {
        did for did, _ in _PIN_DESKS if did.startswith("country_watch_")
    }


def test_the_tag_targets_script_spells_no_desk_list():
    text = (REPO_ROOT / "scripts" / "bringup_tag_targets.py").read_text(encoding="utf-8")
    assert "G20_ISO2" not in text and "WATCH_ISO2" not in text
    assert "desk_list=desk_list" in text      # main() passes the profile's list


# ---------------------------------------------------------------------------
# the catalog's state-media set is read from the catalog
# ---------------------------------------------------------------------------


def test_state_media_is_read_from_the_catalog_entries():
    assert catalog.STATE_MEDIA_IDS == frozenset(
        e.id for e in catalog.CATALOG if e.source_class == "state_media"
    )
    assert len(catalog.STATE_MEDIA_IDS) == 5
    text = (REPO_ROOT / "scripts" / "bringup_register_source_catalog.py").read_text(encoding="utf-8")
    for sid in catalog.STATE_MEDIA_IDS:
        assert sid not in text, f"{sid} is still spelled in the catalog script"


# ---------------------------------------------------------------------------
# the UI half: no unit id in the three files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", UI_FILES, ids=lambda p: p.name)
def test_the_ui_files_carry_no_unit_literal(path):
    text = path.read_text(encoding="utf-8")
    # a whole identifier only: `escalation_composition` is a composition's id
    found = [u for u in rosters.unit_roster() if re.search(rf"(?<![A-Za-z0-9_]){u}(?![A-Za-z0-9_])", text)]
    assert not found, f"{path.relative_to(REPO_ROOT)} spells unit id(s) {found}: read lib/profileUi.ts"


def test_the_ui_export_carries_labels_not_instrument_terms():
    doc = json.loads((UI_SRC / "lib" / "profile_ui.json").read_text(encoding="utf-8"))
    units = doc["rosters"]["units"]
    assert [u["id"] for u in units] == list(rosters.unit_roster())
    for u in units:
        assert set(u) <= {"id", "question", "label", "short", "scope_tag", "scope_note"}, u["id"]
        assert u.get("label") and u.get("short"), u["id"]
