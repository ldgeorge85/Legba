# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The light projections: a signal's summary
row and a descriptor's summary body. Pure functions; no database."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from legba.data.registry.descriptor_list_weight import SUMMARY_SCOPE_KEYS, summary_body
from legba.data.registry.signal_list_weight import (
    LIGHT_DATA_KEYS,
    SignalSummaryRow,
    light_data,
    summary_row,
)
from legba.data.registry.event_list_weight import EventSummaryRow
from legba.data.registry.event_list_weight import summary_row as event_summary_row
from legba.data.registry.events_api import EventRow
from legba.data.registry.substrate_reads_api import SignalRow

NOW = datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc)
PAYLOAD = {
    "title": "Port strike enters its third day",
    "published_at": "2026-11-14T03:00:00Z",
    "raw_body": "<html>" + "x" * 20_000,
    "archived_text": "y" * 8_000,
    "entities": [{"name": "n", "type": "ORG"}] * 50,
    "relations": [{"a": 1, "b": 2}] * 50,
    "geojson": {"type": "Point", "coordinates": [1.0, 2.0]},
}


def _full() -> SignalRow:
    return SignalRow(
        id="s-1", data=PAYLOAD, title=PAYLOAD["title"], source_id="source.example.wire",
        source_url="https://example.org/a", guid="g", category="labour", event_timestamp=NOW,
        language="en", confidence=0.7, classification_scores=None, target_id=None,
        target_version=None, analyst_id=None, analyst_version=None, produced_at=NOW,
        derived_from=[], schema_uri="iglu:legba/signal/jsonschema/3-0-0", run_id=None,
        created_at=NOW, updated_at=NOW, descriptor_source_id="source.example.wire",
        geo=["FR"], tags=["labour"], entity_classes=["ORG"],
    )


def test_a_summary_signal_keeps_what_a_list_draws_and_drops_the_payload() -> None:
    full = _full()
    light = summary_row(full)
    assert isinstance(light, SignalSummaryRow)
    assert light.data == {"title": PAYLOAD["title"], "published_at": PAYLOAD["published_at"]}
    assert set(light.data) <= set(LIGHT_DATA_KEYS)
    assert (light.id, light.title, light.category, light.geo, light.tags) == (
        "s-1", PAYLOAD["title"], "labour", ["FR"], ["labour"])
    assert light.produced_at == light.event_timestamp == NOW
    assert len(light.model_dump_json()) * 20 < len(full.model_dump_json())


def test_light_data_never_invents_a_key() -> None:
    assert light_data({"raw_body": "x"}) == {}


def test_a_light_row_keeps_the_point_the_world_map_places_it_at() -> None:
    """The world map (v4/world/mapData) reads the default weight
    and places a signal at ``data.geo.lat/lon``; the light default dropped it,
    so every point fell back to a country centroid (or off the map)."""
    geo = {"lat": 48.85, "lon": 2.35, "full_name": "Paris, FR", "country_iso2": "FR",
           "derivation": "gazetteer", "adm1_code": "FR11", "precision": "city"}
    assert light_data({**PAYLOAD, "geo": geo}) == {
        "title": PAYLOAD["title"], "published_at": PAYLOAD["published_at"],
        "geo": {"lat": 48.85, "lon": 2.35}}
    # a geo without a point, or not an object, is left out — never invented
    assert "geo" not in light_data({"geo": {"country_iso2": "FR"}})
    assert "geo" not in light_data({"geo": ["FR"]})
    assert "geo" not in light_data({"geo": None})


def test_a_summary_descriptor_body_keeps_the_roster_fields_only() -> None:
    body = {
        "identity": {"name": "France", "aliases": ["FR"] * 200},
        "scope": {
            "domain": "country", "geo": ["FR"], "tags": ["g20"],
            "geometry": {"type": "Point", "coordinates": [2, 46]},
            "entity_classes": ["x"] * 500, "relationship_types": ["y"] * 500,
        },
        "_auto_wired_sources": ["source.a"] * 400,
        "sources": [{"id": "s"}] * 300,
        "coordination": {"k": "v" * 2000},
    }
    out = summary_body(body)
    assert out == {
        "identity": {"name": "France"},
        "scope": {
            "domain": "country", "geo": ["FR"], "tags": ["g20"],
            "geometry": {"type": "Point", "coordinates": [2, 46]},
        },
        "declared_sources": 300,
    }
    assert set(out["scope"]) <= set(SUMMARY_SCOPE_KEYS)
    assert len(json.dumps(out)) * 20 < len(json.dumps(body))


def test_a_summary_keeps_whether_the_subject_declares_a_source() -> None:
    """The map hatches a subject that declares no source as
    "not covered"; the summary cut dropped ``sources`` whole, so every subject
    read as declaring none. The count survives; an empty list says 0; a body
    without the key says nothing."""
    assert summary_body({"sources": [{"id": "a"}, {"id": "b"}]})["declared_sources"] == 2
    assert summary_body({"sources": []})["declared_sources"] == 0
    assert "declared_sources" not in summary_body({"scope": {"geo": ["FR"]}})
    assert "declared_sources" not in summary_body({"sources": "not a list"})


def test_a_summary_of_a_body_without_scope_is_empty_not_invented() -> None:
    assert summary_body({"analyst": {"kind": "x"}}) == {}
    assert summary_body(None) == {}


#: The cases the walk's fixture mirror (legba-ui-v3/tools/walk/weights.mjs) is
#: held to as well: the fixture walk answered a summary read with the
#: full recording, so it could not see what the light roster dropped.
GOLDEN = Path(__file__).resolve().parents[2] / "legba-ui-v3" / "tools" / "walk" / "fixtures" / "weights_golden.json"


def test_the_projections_answer_the_cases_the_walk_mirror_is_held_to() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for case in golden["descriptor_body"]:
        assert summary_body(case["in"]) == case["out"], case
    for case in golden["signal_data"]:
        assert light_data(case["in"]) == case["out"], case
    assert list(SignalSummaryRow.model_fields) == golden["signal_summary_columns"]
    for case in golden["event_rows"]:
        assert event_summary_row(EventRow(**case["in"])).model_dump(mode="json") == case["out"], case
    assert list(EventSummaryRow.model_fields) == golden["event_summary_columns"]


def test_a_light_event_keeps_what_the_map_and_the_time_band_draw() -> None:
    """The Watch page 2.01 MB against 2.00, its largest read
    ``/v3/events?limit=500`` (0.42 MB). Both readers of that read ask the
    light row; every field either one reads survives it."""
    # v4/world/mapData.ts fetchWorldEvents + lib/temporalLayers.ts eventRow
    map_reads = {"id", "title", "severity", "lifecycle_state", "target_id", "geo",
                 "geo_lat", "geo_lon", "time_start", "produced_at"}
    band_reads = {"id", "title", "severity", "lifecycle_state", "valid_from",
                  "time_start", "produced_at"}
    assert map_reads | band_reads <= set(EventSummaryRow.model_fields)
    full = EventRow(id="e", title="t", summary="s" * 400, locations=["x"] * 20,
                    geo=["FR"], geo_lat=1.0, geo_lon=2.0, produced_at=NOW)
    light = event_summary_row(full)
    assert (light.geo_lat, light.geo_lon, light.geo, light.produced_at) == (1.0, 2.0, ["FR"], NOW)
    assert len(light.model_dump_json()) * 3 < len(full.model_dump_json())
