# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ROSTER-1 — the inline rosters and the source catalog are PROFILE data.

``src/legba/data/rosters.py`` is the one reader. This file proves:

  * the helpers return the profile's rosters, the packaged export the runtime
    images read is the default profile's export (stale = red), and
    ``LEGBA_PROFILE`` / an injected profile select another instance's rosters;
  * the source catalog moved to ``profiles/geopolitics/rosters/source_catalog.yaml``
    byte for byte: the YAML-loaded tuple equals a FROZEN copy of the code
    literal it replaced, entry by entry, and renders the same descriptor rows
    and credibility seeds; the loader refuses a malformed file; the sanctions
    skeleton declares none;
  * THE GATE (task row ROSTER-1): a tag added to a profile joins the
    desk-coverage read with no code change — against the real driver;
  * ``entity_class``: the registered family when the runtime snapshot has it
    loaded, the documented literal when it does not.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import sys
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data import rosters
from legba.data.profile import PROFILE_ENV, ProfileError, load_profile
from legba.data.tags import EMPTY_INDEX, VocabularyIndex, install_vocabulary_provider

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GEO = REPO_ROOT / "profiles" / "geopolitics"
SANCTIONS = REPO_ROOT / "profiles" / "sanctions_export_control"
CATALOG_YAML = GEO / "rosters" / "source_catalog.yaml"
FROZEN = REPO_ROOT / "tests" / "fixtures" / "bringup" / "source_catalog_frozen_2026-10-04.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ``_p17_registrar`` (imported by the catalog script) sets a process-global
# LEGBA_DATA_PG_DB default at import — snapshot/restore so it cannot leak.
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(SCRIPTS_DIR))
try:
    import bringup_register_source_catalog as catalog  # noqa: E402
    import gen_profile_rosters  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)


@pytest.fixture
def restore_rosters():
    """Any test that injects a profile puts the process's rosters back."""
    yield
    rosters.refresh()


def _profile_copy(tmp_path: pathlib.Path, mutate) -> pathlib.Path:
    """A loadable copy of the geopolitics manifest (own empty descriptors dir,
    no overlay / prompts / catalog), mutated."""
    data = yaml.safe_load((GEO / "profile.yaml").read_text(encoding="utf-8"))
    data["descriptors_dir"] = "descriptors"
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
    (d / "descriptors").mkdir()
    (d / "profile.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# the helpers
# ---------------------------------------------------------------------------


def test_helpers_return_the_default_profiles_rosters():
    r = load_profile(GEO).rosters
    assert rosters.subject_roster_tags() == tuple(r.subject_tags) == ("g20", "watch", "supply_chain")
    assert rosters.scored_roster_tags() == tuple(r.scored_subject_tags) == ("g20", "watch")
    assert rosters.unit_roster() == tuple(u.id for u in r.units)
    assert len(rosters.unit_roster()) == 9
    assert rosters.scored_unit_roster() == tuple(r.scored_units)
    assert set(rosters.scored_unit_roster()) < set(rosters.unit_roster())
    assert {"proliferation_watch", "disruption_status"}.isdisjoint(rosters.scored_unit_roster())


def test_the_packaged_export_is_the_default_profiles_export():
    """The runtime / registry images carry ``src/`` only: they read the
    packaged copy. A profile edit without a regenerate is red here."""
    assert gen_profile_rosters.OUT == rosters.PACKAGED_EXPORT
    assert rosters.PACKAGED_EXPORT.read_text(encoding="utf-8") == gen_profile_rosters.render(None), (
        "stale: run PYTHONPATH=src python3 scripts/gen_profile_rosters.py"
    )
    assert gen_profile_rosters.main(["--check"]) == 0
    assert gen_profile_rosters.main(["--check", "--profile", "profiles/sanctions_export_control"]) == 1
    if not os.environ.get(PROFILE_ENV):
        assert rosters.current().origin.startswith("packaged:")


def test_legba_profile_selects_another_instances_rosters():
    rs = rosters._select(env={PROFILE_ENV: str(SANCTIONS)})
    assert rs.profile == "sanctions_export_control"
    assert rs.subject_tags == ("regime", "counterparty")
    assert rs.scored_subject_tags == () and rs.scored_units == ()
    assert len(rs.units) == 5
    # unset -> the packaged export; a profile that does not load is an error
    assert rosters._select(env={}).origin.startswith("packaged:")
    with pytest.raises(ProfileError):
        rosters._select(env={PROFILE_ENV: "/nonexistent/profile"})


def test_refresh_injects_and_restores(tmp_path, restore_rosters):
    d = _profile_copy(tmp_path, lambda m: m["rosters"].update(subject_tags=["g20", "watch", "lane"]))
    assert rosters.refresh(d).subject_tags == ("g20", "watch", "lane")
    assert rosters.subject_roster_tags() == ("g20", "watch", "lane")
    assert rosters.refresh(load_profile(SANCTIONS)).subject_tags == ("regime", "counterparty")
    assert rosters.refresh().subject_tags == ("g20", "watch", "supply_chain")


def test_the_export_is_deterministic_and_carries_no_ui_keys():
    doc = json.loads(rosters.export_rosters(load_profile(GEO)))
    assert doc["schema"] == rosters.EXPORT_SCHEMA and doc["profile"] == "geopolitics"
    assert set(doc) == {"_generated", "schema", "profile", "subject_tags",
                        "scored_subject_tags", "units", "scored_units", "unit_terms"}
    # ROSTER-2: the units' instrument data rides along; their labels do not
    for terms in doc["unit_terms"].values():
        assert set(terms) <= {"query", "title", "reference_queries", "scope_tag"}


# ---------------------------------------------------------------------------
# the source catalog — byte-identity with the literal it replaced
# ---------------------------------------------------------------------------


#: Lane N-U — the catalog entries that declare ``cadence.expect_quiet``.
_EXPECT_QUIET = frozenset({"source.usgs.earthquakes_m45"})


def _frozen() -> dict[str, Any]:
    return json.loads(FROZEN.read_text(encoding="utf-8"))


def _as_json(entry) -> dict[str, Any]:
    d = dataclasses.asdict(entry)
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


def test_the_profile_names_the_catalog_yaml():
    assert load_profile(GEO).source_catalog_path == CATALOG_YAML.resolve()
    assert load_profile(SANCTIONS).source_catalog_path is None
    assert rosters.source_catalog(load_profile(SANCTIONS)) == ()


def test_the_yaml_catalog_is_the_frozen_literal_entry_by_entry():
    frozen = _frozen()
    loaded = catalog.load_catalog(CATALOG_YAML)
    assert len(loaded) == len(frozen["entries"]) == 46
    assert [e.id for e in loaded] == [e["id"] for e in frozen["entries"]]
    # ROSTER-2: the curated state_media set moved onto the entries
    # (source_class); every other field is the frozen literal's.
    state_media = set(frozen["state_media_ids"])
    for got, want in zip(loaded, frozen["entries"]):
        row = _as_json(got)
        assert row.pop("source_class") == ("state_media" if got.id in state_media else None), got.id
        # W10 TAX-2: the field the literal never had; no entry declares one yet.
        assert row.pop("classifications") == [], got.id
        # Lane N-U: ``cadence.expect_quiet``, a field the literal never had —
        # declared on the USGS significant-quakes feed only.
        assert row.pop("expect_quiet") is (got.id in _EXPECT_QUIET), got.id
        assert row == want, got.id
    assert catalog.STATE_MEDIA_IDS == state_media
    assert catalog.CATALOG == loaded            # the module's import-time load
    assert all(isinstance(e.tags, tuple) and isinstance(e.geo, tuple) for e in loaded)


def test_the_yaml_catalog_renders_the_same_rows():
    """Same descriptor bodies (hashed exactly as frozen) and the same
    credibility seeds: the registrar writes identical rows."""
    frozen = _frozen()
    loaded = catalog.load_catalog(CATALOG_YAML)
    def _sha(e) -> str:
        return hashlib.sha256(json.dumps(
            catalog.build_descriptor(e).model_dump(mode="json", by_alias=True), sort_keys=True,
        ).encode()).hexdigest()

    rendered = {e.id: _sha(e) for e in loaded}
    # Lane N-U: an ``expect_quiet`` entry's body gains ``cadence.expect_quiet``
    # (a new head to register); with the flag off it renders the frozen bytes.
    for e in loaded:
        if e.id in _EXPECT_QUIET:
            assert rendered[e.id] != frozen["rendered_sha256"][e.id]
            rendered[e.id] = _sha(dataclasses.replace(e, expect_quiet=False))
    assert rendered == frozen["rendered_sha256"]
    assert [list(r) for r in catalog.credibility_rows(loaded)] == frozen["credibility_rows"]
    assert catalog.credibility_rows() == catalog.credibility_rows(loaded)


def test_the_catalog_loader_refuses_a_malformed_file(tmp_path):
    good = {"id": "source.x.y", "name": "X", "kind": "rss", "url": "https://x.invalid/rss",
            "tags": ["news"], "cadence_minutes": 60, "cred_host": "x.invalid"}

    def write(doc) -> pathlib.Path:
        p = tmp_path / f"cat_{len(list(tmp_path.iterdir()))}.yaml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        return p

    assert [e.id for e in catalog.load_catalog(write({"entries": [good]}))] == ["source.x.y"]
    assert catalog.load_catalog(write({"entries": []})) == ()
    with pytest.raises(ValueError, match="unknown key"):
        catalog.load_catalog(write({"entries": [{**good, "colour": "red"}]}))
    # ROSTER-2: source_class is a known key, held to the schema's vocabulary
    (sm,) = catalog.load_catalog(write({"entries": [{**good, "source_class": "state_media"}]}))
    assert sm.source_class == "state_media" and catalog.catalog_source_class(sm) == "state_media"
    with pytest.raises(ValueError, match="source_class"):
        catalog.load_catalog(write({"entries": [{**good, "source_class": "propaganda"}]}))
    with pytest.raises(ValueError, match="cadence_minutes"):
        catalog.load_catalog(write({"entries": [{k: v for k, v in good.items() if k != "cadence_minutes"}]}))
    with pytest.raises(ProfileError, match="repeat id"):
        catalog.load_catalog(write({"entries": [good, good]}))
    with pytest.raises(ProfileError, match="one key"):
        catalog.load_catalog(write({"entries": [good], "extra": 1}))
    with pytest.raises(ProfileError, match="string id"):
        catalog.load_catalog(write({"entries": [{"name": "no id"}]}))


# ---------------------------------------------------------------------------
# THE GATE — a new roster tag cannot vanish from desk_coverage
# ---------------------------------------------------------------------------

_GATE_TAG = "w8_roster_gate_tag"
_GATE_DESK = "target_w8_roster_gate_desk"
_PRESENT_DESK = "target_w8_roster_gate_present"


@pytest_asyncio.fixture
async def gate_desks(migrated_pg):
    """Two target heads in the session DB: one tagged ONLY with a tag no
    profile declares, one with a geopolitics roster tag. Deleted at teardown
    (W8 rule: nothing registered here survives into later files)."""
    conn = await asyncpg.connect(migrated_pg.dsn)
    ids = [_GATE_DESK, _PRESENT_DESK]
    try:
        await conn.execute("DELETE FROM target_descriptors WHERE descriptor_id = ANY($1::text[])", ids)
        for did, tags in ((_GATE_DESK, [_GATE_TAG]), (_PRESENT_DESK, ["watch"])):
            await conn.execute(
                "INSERT INTO target_descriptors "
                "  (descriptor_id, version, schema_uri, is_head, state, owner, name, body) "
                "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', 'test_roster1', $2, $3::jsonb)",
                did, f"Gate {did}", json.dumps({"scope": {"geo": [], "tags": tags}}),
            )
        yield conn
    finally:
        await conn.execute("DELETE FROM target_descriptors WHERE descriptor_id = ANY($1::text[])", ids)
        await conn.close()


async def test_a_new_roster_tag_joins_desk_coverage_without_code(tmp_path, gate_desks, restore_rosters):
    from legba.data.analysts import composition_slice as cs

    conn = gate_desks
    before = {d["desk_id"] for d in await cs._resolve_desk_roster(conn)}
    assert _PRESENT_DESK in before and _GATE_DESK not in before

    # The ONLY change: the profile declares one more roster tag.
    rosters.refresh(_profile_copy(
        tmp_path,
        lambda m: m["rosters"].update(subject_tags=[*m["rosters"]["subject_tags"], _GATE_TAG]),
    ))
    after = {d["desk_id"] for d in await cs._resolve_desk_roster(conn)}
    assert {_GATE_DESK, _PRESENT_DESK} <= after

    # ...and the thematic compose NAMES it: a head for one desk, the new
    # desk with none -> an honest gap row, never a silent omission.
    async def basis_reader(_conn, **_kw):
        return [{"target_id": _PRESENT_DESK, "analyst_id": "escalation"}]

    rows = await cs._assemble_thematic_unit_slice(
        conn, unit_analyst_ids=["escalation"], time_window_hours=24, limit=50,
        verify_floor=None, basis_reader=basis_reader,
    )
    coverage = {c["desk_id"]: c["mode"] for c in rows[0]["_thematic_coverage"]}
    assert coverage[_GATE_DESK] == cs.THEMATIC_MODE_GAP
    assert coverage[_PRESENT_DESK] == cs.THEMATIC_MODE_PRESENT


# ---------------------------------------------------------------------------
# entity_class — the registered family, else the documented literal
# ---------------------------------------------------------------------------

_FAMILY = [
    {"family": "entity_class", "value": v}
    for v in ("country", "organization", "corporation", "location", "person", "event",
              "entity", "military_unit", "armed_group")
]


def test_entity_classes_reads_the_family_or_the_fallback():
    fallback = {"person", "entity"}
    loaded = VocabularyIndex.from_entries(_FAMILY)
    assert rosters.entity_classes(loaded, fallback=fallback) == {e["value"] for e in _FAMILY}
    assert rosters.entity_classes(EMPTY_INDEX, fallback=fallback) == fallback
    assert rosters.entity_classes(None, fallback=fallback) == fallback
    unloaded = VocabularyIndex.from_entries(_FAMILY, unloaded=["entity_class"])
    assert rosters.entity_classes(unloaded, fallback=fallback) == fallback


async def test_entity_researcher_falls_back_when_the_family_is_unloaded():
    from legba.data.analysts import entity_researcher as er

    install_vocabulary_provider(None)
    assert await er._valid_entity_classes() == er._FALLBACK_ENTITY_CLASSES
    assert er._coerce_class("military_unit") is None              # not on the literal

    async def provider():
        return VocabularyIndex.from_entries(_FAMILY)

    install_vocabulary_provider(provider)
    try:
        valid = await er._valid_entity_classes()
        assert valid == {e["value"] for e in _FAMILY}
        assert er._coerce_class("military_unit", valid) == "military_unit"
        assert er._coerce_class("org", valid) == "organization"
    finally:
        install_vocabulary_provider(None)


def test_roster_tags_are_never_gazetteer_probes(tmp_path, restore_rosters):
    """target_resolution's generic-token filter takes the roster tags from the
    profile: an instance's roster tag is never probed as a place name."""
    from legba.runtime.target_resolution import _gazetteer_probes

    scope = {"tags": ["supply_chain", "hormuz", "regime_watchers"], "themes": ["watch"]}
    assert _gazetteer_probes(scope) == ["%hormuz%", "%regime_watchers%"]
    rosters.refresh(_profile_copy(
        tmp_path,
        lambda m: m["rosters"].update(subject_tags=["regime_watchers"], scored_subject_tags=[]),
    ))
    assert _gazetteer_probes(scope) == ["%watch%", "%supply_chain%", "%hormuz%"]
