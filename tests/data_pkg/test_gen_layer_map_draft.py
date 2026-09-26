# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L0 — the retag generator (``scripts/gen_layer_map_draft.py``) on a
small fixture of source descriptors: deterministic output, the documented
rule order, and every generated aperture stamped ``declared: unmeasured``.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_module():
    """Import scripts/gen_layer_map_draft.py by path (scripts/ is not a
    package), the same way test_source_class_taxonomy.py reaches
    bringup_register_source_catalog."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "gen_layer_map_draft", SCRIPTS_DIR / "gen_layer_map_draft.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gen = _load_module()


FIXTURE_SOURCES = {
    # 1. state media scoped to the target country -> official.
    "source_zzfix_state.yaml": {
        "identity": {
            "id": "source.zzfix.state", "name": "ZZ state wire", "kind": "rss",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "test", "created": "2026-01-01T00:00:00Z", "state": "active",
        },
        "scope": {"geo": ["ZZ"], "source_class": "state_media"},
    },
    # 2. domestic reporting scoped to the target country -> domestic_press.
    "source_zzfix_domestic.yaml": {
        "identity": {
            "id": "source.zzfix.domestic", "name": "ZZ Daily", "kind": "rss",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "test", "created": "2026-01-01T00:00:00Z", "state": "active",
        },
        "scope": {"geo": ["ZZ"], "source_class": "reporting"},
    },
    # 3. global reporting, no country scope -> foreign_press.
    "source_zzfix_global.yaml": {
        "identity": {
            "id": "source.zzfix.global", "name": "Global Wire World", "kind": "rss",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "test", "created": "2026-01-01T00:00:00Z", "state": "active",
        },
        "scope": {"geo": [], "source_class": "reporting"},
    },
}


@pytest.fixture()
def fixture_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    for name, body in FIXTURE_SOURCES.items():
        (tmp_path / name).write_text(yaml.safe_dump(body), encoding="utf-8")
    # an empty wire_map.yaml (no wire/agency ids) — the fixture has none anyway
    wire_map = tmp_path / "wire_map.yaml"
    wire_map.write_text(
        yaml.safe_dump({"wire_map": {"same_publisher": {"ap": []}}}),
        encoding="utf-8",
    )
    return tmp_path


def test_fixture_maps_each_source_to_the_documented_layer(fixture_dir):
    draft = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    by_id = {e["source_id"]: e for e in draft["entries"]}

    assert by_id["source.zzfix.state"]["layer"] == "official"
    assert by_id["source.zzfix.domestic"]["layer"] == "domestic_press"
    assert by_id["source.zzfix.global"]["layer"] == "foreign_press"
    assert len(draft["entries"]) == 3
    for entry in draft["entries"]:
        assert entry["reason"].startswith("derived:"), entry


def test_fixture_apertures_cover_every_layer_as_unmeasured(fixture_dir):
    from legba.data.layers._vocab import LAYER_VOCAB

    draft = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    apertures = draft["apertures"]
    assert {a["layer"] for a in apertures} == set(LAYER_VOCAB)
    assert all(a["declared"] == "unmeasured" for a in apertures)


def test_output_is_deterministic_across_two_runs(fixture_dir):
    draft1 = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    draft2 = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    text1 = gen.render_yaml(draft1, "ZZ")
    text2 = gen.render_yaml(draft2, "ZZ")
    assert text1 == text2
    assert draft1 == draft2


def test_entries_are_sorted_by_source_id(fixture_dir):
    draft = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    ids = [e["source_id"] for e in draft["entries"]]
    assert ids == sorted(ids)


def test_a_source_with_no_relevance_to_the_country_is_excluded(fixture_dir):
    # add a fourth fixture, scoped to a DIFFERENT country entirely
    other = fixture_dir / "source_zzfix_other_country.yaml"
    other.write_text(yaml.safe_dump({
        "identity": {
            "id": "source.zzfix.other", "name": "Other Country Press", "kind": "rss",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "test", "created": "2026-01-01T00:00:00Z", "state": "active",
        },
        "scope": {"geo": ["YY"], "source_class": "reporting"},
    }), encoding="utf-8")
    draft = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    ids = {e["source_id"] for e in draft["entries"]}
    assert "source.zzfix.other" not in ids


def test_wire_agency_feed_reads_as_foreign_press_even_when_geo_scoped(fixture_dir, tmp_path):
    """rule 3: a same_publisher['ap'] wire/agency feed is foreign_press even if
    its own regional edition happens to be geo-scoped to this country — an
    agency dispatch is never this country's domestic masthead."""
    ap_feed = fixture_dir / "source_zzfix_ap_local.yaml"
    ap_feed.write_text(yaml.safe_dump({
        "identity": {
            "id": "source.zzfix.ap_local", "name": "AP ZZ desk", "kind": "rss",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "test", "created": "2026-01-01T00:00:00Z", "state": "active",
        },
        # geo-scoped to ZZ, same as the domestic masthead fixture above
        "scope": {"geo": ["ZZ"], "source_class": "reporting"},
    }), encoding="utf-8")
    wire_map = fixture_dir / "wire_map.yaml"
    wire_map.write_text(
        yaml.safe_dump({"wire_map": {"same_publisher": {
            "ap": ["source.zzfix.ap_local"],
        }}}),
        encoding="utf-8",
    )
    draft = gen.build_draft("ZZ", fixture_dir, wire_map)
    by_id = {e["source_id"]: e for e in draft["entries"]}
    assert by_id["source.zzfix.ap_local"]["layer"] == "foreign_press"
    assert "wire" in by_id["source.zzfix.ap_local"]["reason"].lower()


def test_generated_draft_validates_against_the_pydantic_model(fixture_dir):
    from legba.data.registry.layer_map_schema import LayerMapDescriptor

    draft = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    desc = LayerMapDescriptor.model_validate(draft, strict=False)
    assert desc.country == "ZZ"
    assert desc.identity.state.value == "draft"


def test_retired_and_template_sources_are_skipped(fixture_dir):
    retired = fixture_dir / "source_zzfix_retired.yaml"
    retired.write_text(yaml.safe_dump({
        "identity": {
            "id": "source.zzfix.retired", "name": "Retired ZZ Feed", "kind": "rss",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "test", "created": "2026-01-01T00:00:00Z", "state": "retired",
        },
        "scope": {"geo": ["ZZ"], "source_class": "reporting"},
    }), encoding="utf-8")
    template = fixture_dir / "source_zzfix_template.yaml"
    template.write_text(yaml.safe_dump({
        "identity": {
            "id": "source.zzfix.template", "name": "ZZ Template", "kind": "rss",
            "abstraction_level": "L2",
            "schema_uri": "legba/source/1.0.0", "version": "0" * 16,
            "owner": "test", "created": "2026-01-01T00:00:00Z", "state": "draft",
        },
        "scope": {"geo": ["ZZ"], "source_class": "reporting"},
    }), encoding="utf-8")

    draft = gen.build_draft("ZZ", fixture_dir, fixture_dir / "wire_map.yaml")
    ids = {e["source_id"] for e in draft["entries"]}
    assert "source.zzfix.retired" not in ids
    assert "source.zzfix.template" not in ids
