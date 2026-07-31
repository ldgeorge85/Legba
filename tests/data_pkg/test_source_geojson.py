# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.geojson.GeoJSONSourceHandler`.

The first model-free non-text modality. Coverage:

  * Config schema validation (defaults, bad URL, unknown field).
  * L-102 class-var contract conformance.
  * 200 happy path: parse a FeatureCollection, Signal shape
    (``modality="structured"`` + ``mime_type="application/geo+json"`` +
    ``media_ref`` = source URL + inlined per-feature geojson in payload),
    cursor persistence, healthy health record.
  * Bare Feature + bare Geometry document shapes both flow through.
  * 304 path: no Signals, no cursor mutation, healthy.
  * Conditional headers: stored ETag / Last-Modified → If-None-Match /
    If-Modified-Since on the next pull.
  * Malformed body (not JSON / not GeoJSON): unhealthy, empty iterator.
  * Transient 503: one retry then empty + degraded.
  * Persistent 4xx: unhealthy, empty iterator.
  * max_features cap truncates a large collection.
  * Baseline flow: the structured/geo+json signal survives the per-source
    baseline (no text assumption breaks it) and stays a renderable
    geo+json node.

httpx is mocked via :class:`httpx.MockTransport` so the handler hits a
deterministic transport while still exercising the real ``httpx.AsyncClient``.
``json`` parsing runs for real.
"""

from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
)
from legba.data.sources.baseline import run_baseline
from legba.data.sources.geojson import (
    GEOJSON_MIME_TYPE,
    GeoJSONConfig,
    GeoJSONSourceHandler,
    _GEOJSON_CURSOR_KEY,
    _GEOJSON_HEALTH_KEY,
)


# ---------------------------------------------------------------------------
# Sample documents
# ---------------------------------------------------------------------------


FEATURE_COLLECTION = json.dumps({
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "us6000abcd",
            "geometry": {"type": "Point", "coordinates": [-122.4, 37.8, 8.0]},
            "properties": {
                "title": "M 5.2 - 10km W of Somewhere",
                "mag": 5.2,
                "place": "10km W of Somewhere",
                "url": "https://earthquake.invalid/event/us6000abcd",
                "iso3": "USA",
            },
        },
        {
            "type": "Feature",
            "id": "us6000wxyz",
            "geometry": {"type": "Point", "coordinates": [139.7, 35.7]},
            "properties": {
                "title": "M 4.8 - offshore Honshu",
                "mag": 4.8,
                "place": "offshore Honshu",
                "country": "JPN",
            },
        },
    ],
})


SINGLE_FEATURE = json.dumps({
    "type": "Feature",
    "id": "feat-1",
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    "properties": {"name": "A region"},
})


BARE_GEOMETRY = json.dumps({
    "type": "Point",
    "coordinates": [10.0, 20.0],
})


NOT_JSON = "<html>not geojson at all</html>"
NOT_GEOJSON = json.dumps({"type": "Soup", "ingredients": []})


# --- R7 (DQ sweep) fixtures: prose-flattening / modality-flip coverage ------

#: NWS-shaped feature: `properties.description` (the full bulletin) AND
#: `properties.instruction` (public-safety guidance) are both real paragraphs
#: — the motivating case for the prose flatten.
_NWS_DESCRIPTION = (
    "At 415 PM CDT, a severe thunderstorm was located near Chesterfield, "
    "moving east at 35 mph. HAZARD: 60 mph wind gusts and quarter size hail. "
    "SOURCE: Radar indicated. IMPACT: Expect damage to roofs, siding, and "
    "trees. Locations impacted include St. Louis, Clayton, and University City."
)
_NWS_INSTRUCTION = (
    "For your protection move to an interior room on the lowest floor of a "
    "building."
)

NWS_FEATURE_COLLECTION = json.dumps({
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "urn:oid:2.49.0.1.840.0.abc123",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-90.1, 38.5], [-90.0, 38.5], [-90.0, 38.6], [-90.1, 38.5],
                ]],
            },
            "properties": {
                "headline": "Severe Thunderstorm Warning issued for St. Louis County",
                "description": _NWS_DESCRIPTION,
                "instruction": _NWS_INSTRUCTION,
                "severity": "Severe",
                "areaDesc": "St. Louis, MO",
            },
        },
    ],
})

#: EONET-shaped feature: `properties.description` IS present, but is a short
#: category-style tag, not a bulletin — well under the prose length floor.
EONET_FEATURE_COLLECTION = json.dumps({
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "EONET_6321",
            "geometry": {"type": "Point", "coordinates": [-119.4, 36.8]},
            "properties": {
                "title": "Wildfire - California, United States",
                "description": "Wildfires",
                "link": "https://eonet.gsfc.nasa.gov/api/v3/events/EONET_6321",
            },
        },
    ],
})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(
    state: InMemoryStateStore | None = None,
    *,
    config: GeoJSONConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.gis",
        target_version="v-test",
        source_id="src.usgs_quakes",
        config=config or GeoJSONConfig(url="https://example.invalid/feed.geojson"),
        state_store=state or InMemoryStateStore(),
        scope_geo=["GLOBAL"],
        scope_languages=["en"],
    )


def _make_handler(
    transport_handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: GeoJSONConfig | None = None,
) -> GeoJSONSourceHandler:
    transport = httpx.MockTransport(transport_handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=True, timeout=5)
    cfg = config or GeoJSONConfig(url="https://example.invalid/feed.geojson")
    return GeoJSONSourceHandler(cfg, http_client=client)


async def _collect(it) -> list[Signal]:
    out: list[Signal] = []
    async for s in it:
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Config + class-var contract
# ---------------------------------------------------------------------------


def test_geojson_config_defaults():
    cfg = GeoJSONConfig(url="https://x.invalid/feed.geojson")
    assert cfg.feature_id_key == "id"
    assert cfg.max_features == 5000
    assert cfg.user_agent == "Legba/2.0"
    assert cfg.timeout_seconds == 30


def test_geojson_config_rejects_blank_url():
    with pytest.raises(ValidationError):
        GeoJSONConfig(url="")


def test_geojson_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        GeoJSONConfig(url="https://x.invalid/f", what="ever")  # type: ignore[call-arg]


def test_handler_class_contract():
    assert GeoJSONSourceHandler.kind == "geojson"
    assert GeoJSONSourceHandler.family == "source"
    assert GeoJSONSourceHandler.schema_version == "legba/source.geojson/1-0-0"
    assert GeoJSONSourceHandler.config_schema is GeoJSONConfig
    assert GeoJSONSourceHandler.handler_version


# ---------------------------------------------------------------------------
# Happy path: 200 FeatureCollection → structured/geo+json Signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_200_yields_geojson_signals_and_persists_cursor():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=FEATURE_COLLECTION,
            headers={
                "Content-Type": "application/geo+json",
                "ETag": 'W/"gj123"',
                "Last-Modified": "Tue, 20 May 2025 10:00:00 GMT",
            },
            request=req,
        )

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    gj = _make_handler(handler)

    signals = await _collect(gj.pull(ctx, since=None))
    await gj.aclose()

    assert len(signals) == 2

    s = signals[0]
    assert isinstance(s, Signal)
    assert s.source_id == "src.usgs_quakes"
    # The non-text modality contract — this is the whole point of the task.
    assert s.modality == "structured"
    assert s.mime_type == GEOJSON_MIME_TYPE
    # media_ref is a REFERENCE to the source document.
    assert s.media_ref == "https://example.invalid/feed.geojson"
    # canonical_url lifted from a feature property.
    assert s.canonical_url == "https://earthquake.invalid/event/us6000abcd"
    # geo lifted from properties (iso3).
    assert "USA" in s.geo
    # A self-contained geo+json Feature is inlined in the payload so the UI
    # renderer can draw it directly (no re-fetch).
    gjson = s.payload["geojson"]
    assert gjson["type"] == "Feature"
    assert gjson["geometry"]["type"] == "Point"
    assert gjson["id"] == "us6000abcd"
    assert s.payload["external_id"] == "us6000abcd"
    assert s.payload["title"].startswith("M 5.2")
    assert s.content_hash  # set by the handler

    # Second feature uses `country` for geo.
    assert "JPN" in signals[1].geo

    # Cursor persisted from the 200 response headers.
    cur = state.snapshot()[_GEOJSON_CURSOR_KEY]
    assert cur["etag"] == 'W/"gj123"'
    health = state.snapshot()[_GEOJSON_HEALTH_KEY]
    assert health["state"] == "healthy"
    assert health["detail"]["features_yielded"] == 2


@pytest.mark.asyncio
async def test_pull_single_feature_document():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SINGLE_FEATURE, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(), since=None))
    assert len(signals) == 1
    assert signals[0].modality == "structured"
    assert signals[0].payload["geometry_type"] == "Polygon"
    assert signals[0].payload["external_id"] == "feat-1"


@pytest.mark.asyncio
async def test_pull_bare_geometry_document_wraps_feature():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=BARE_GEOMETRY, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(), since=None))
    assert len(signals) == 1
    s = signals[0]
    assert s.mime_type == GEOJSON_MIME_TYPE
    assert s.payload["geojson"]["geometry"]["type"] == "Point"
    # No id / properties → stable content-hash fallback id.
    assert s.payload["external_id"]


# ---------------------------------------------------------------------------
# R7 (DQ sweep) — prose-bearing features flatten to modality="text"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nws_shaped_feature_flattens_prose_and_flips_modality():
    """(a) An NWS-shaped feature — `description` + `instruction` real
    paragraphs — gets its prose flattened to `payload["raw_body"]` and its
    modality flipped to "text" so `signal_summarizer` (`WHERE
    modality='text'`) and opensearch's `_BEST_BODY_FIELDS` (which reads
    `payload.raw_body`) can both see it."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NWS_FEATURE_COLLECTION, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(), since=None))
    assert len(signals) == 1
    s = signals[0]

    # The modality flip — the whole point of this fix.
    assert s.modality == "text"
    # mime_type is UNCHANGED — the UI's mime_type-first renderer resolution
    # still draws the map from the inlined geometry regardless of modality.
    assert s.mime_type == GEOJSON_MIME_TYPE

    # The full bulletin + instruction are flattened, concatenated in that
    # fixed order, exactly as `_extract_prose` joins them.
    assert s.payload["raw_body"] == f"{_NWS_DESCRIPTION}\n\n{_NWS_INSTRUCTION}"
    # The raw properties dict is untouched — still carries the same text
    # nested, plus everything else the feature had.
    assert s.payload["properties"]["description"] == _NWS_DESCRIPTION
    assert s.payload["properties"]["instruction"] == _NWS_INSTRUCTION
    # Title still comes from the headline (unaffected by the prose flatten).
    assert s.payload["title"] == "Severe Thunderstorm Warning issued for St. Louis County"
    # The geo+json fragment is still inlined for the map renderer.
    assert s.payload["geojson"]["geometry"]["type"] == "Polygon"


@pytest.mark.asyncio
async def test_usgs_shaped_feature_unchanged_and_byte_identical():
    """(b) A USGS-shaped feature (no prose property at all) is completely
    unaffected by the R7 change — same modality, same payload keys, no
    `raw_body` added. Regression guard: this must never flip."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FEATURE_COLLECTION, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(), since=None))
    assert len(signals) == 2
    s = signals[0]

    assert s.modality == "structured"
    assert s.mime_type == GEOJSON_MIME_TYPE
    assert "raw_body" not in s.payload
    # Full payload shape, byte-identical to the pre-fix contract (same keys
    # the "Happy path" test above already exercises individually).
    assert s.payload == {
        "external_id": "us6000abcd",
        "title": "M 5.2 - 10km W of Somewhere",
        "geometry_type": "Point",
        "properties": {
            "title": "M 5.2 - 10km W of Somewhere",
            "mag": 5.2,
            "place": "10km W of Somewhere",
            "url": "https://earthquake.invalid/event/us6000abcd",
            "iso3": "USA",
        },
        "geojson": {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.4, 37.8, 8.0]},
            "properties": {
                "title": "M 5.2 - 10km W of Somewhere",
                "mag": 5.2,
                "place": "10km W of Somewhere",
                "url": "https://earthquake.invalid/event/us6000abcd",
                "iso3": "USA",
            },
            "id": "us6000abcd",
        },
        "source_url": "https://example.invalid/feed.geojson",
    }


@pytest.mark.asyncio
async def test_eonet_shaped_short_description_stays_structured():
    """(c) An EONET-shaped feature has a `description` KEY, but it's a short
    category-style tag (well under `_PROSE_MIN_LEN`) — the length guard keeps
    it `structured`, unchanged, with no `raw_body` added."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=EONET_FEATURE_COLLECTION, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(), since=None))
    assert len(signals) == 1
    s = signals[0]

    assert s.modality == "structured"
    assert s.mime_type == GEOJSON_MIME_TYPE
    assert "raw_body" not in s.payload
    assert s.payload["properties"]["description"] == "Wildfires"


# ---------------------------------------------------------------------------
# 304 not-modified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_304_yields_nothing_and_stays_healthy():
    state = InMemoryStateStore({_GEOJSON_CURSOR_KEY: {"etag": 'W/"gj123"', "last_modified": ""}})

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("If-None-Match") == 'W/"gj123"'
        return httpx.Response(304, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    # Cursor unchanged; health healthy.
    assert state.snapshot()[_GEOJSON_CURSOR_KEY]["etag"] == 'W/"gj123"'
    assert state.snapshot()[_GEOJSON_HEALTH_KEY]["state"] == "healthy"


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_not_json_is_unhealthy():
    state = InMemoryStateStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NOT_JSON, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    assert state.snapshot()[_GEOJSON_HEALTH_KEY]["state"] == "unhealthy"


@pytest.mark.asyncio
async def test_pull_not_geojson_object_is_unhealthy():
    state = InMemoryStateStore()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NOT_GEOJSON, request=req)

    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    assert state.snapshot()[_GEOJSON_HEALTH_KEY]["state"] == "unhealthy"


@pytest.mark.asyncio
async def test_pull_transient_503_retries_then_records_status():
    # Mirrors the RSS handler: a transient 5xx is retried once; if it persists
    # the returned 503 falls through `pull`'s >=400 branch and is recorded as
    # unhealthy (the health_check probe is where a live 5xx maps to degraded).
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, request=req)

    state = InMemoryStateStore()
    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    # One retry on transient → two attempts total.
    assert calls["n"] == 2
    rec = state.snapshot()[_GEOJSON_HEALTH_KEY]
    assert rec["state"] == "unhealthy"
    assert rec["detail"]["status"] == 503


@pytest.mark.asyncio
async def test_pull_network_error_retries_then_degraded():
    # A genuine network error (not a 5xx response) exhausts the retry and
    # records `degraded` via the retry-exhausted branch.
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=req)

    state = InMemoryStateStore()
    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    assert calls["n"] == 2  # one retry
    assert state.snapshot()[_GEOJSON_HEALTH_KEY]["state"] == "degraded"


@pytest.mark.asyncio
async def test_health_check_503_is_degraded():
    # The live probe path: a 5xx during health_check is a transient → degraded.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=req)

    health = await _make_handler(handler).health_check(_make_ctx())
    assert health.state == "degraded"


@pytest.mark.asyncio
async def test_pull_persistent_404_is_unhealthy():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=req)

    state = InMemoryStateStore()
    signals = await _collect(_make_handler(handler).pull(_make_ctx(state), since=None))
    assert signals == []
    assert state.snapshot()[_GEOJSON_HEALTH_KEY]["state"] == "unhealthy"


@pytest.mark.asyncio
async def test_max_features_cap_truncates():
    big = json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": f"f{i}", "geometry": {"type": "Point", "coordinates": [i, i]}, "properties": {"name": f"f{i}"}}
            for i in range(10)
        ],
    })

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big, request=req)

    cfg = GeoJSONConfig(url="https://example.invalid/feed.geojson", max_features=3)
    signals = await _collect(_make_handler(handler, config=cfg).pull(_make_ctx(config=cfg), since=None))
    assert len(signals) == 3


# ---------------------------------------------------------------------------
# Conditional headers on a subsequent pull
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conditional_headers_sent_after_first_pull():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200,
            text=SINGLE_FEATURE,
            headers={"ETag": 'W/"abc"', "Last-Modified": "Wed, 21 May 2025 00:00:00 GMT"},
            request=req,
        )

    state = InMemoryStateStore()
    ctx = _make_ctx(state)
    gj = _make_handler(handler)

    await _collect(gj.pull(ctx, since=None))
    await _collect(gj.pull(ctx, since=None))
    await gj.aclose()

    # First request has no conditional headers; second carries the stored cursor.
    assert "If-None-Match" not in captured[0].headers
    assert captured[1].headers.get("If-None-Match") == 'W/"abc"'
    assert captured[1].headers.get("If-Modified-Since") == "Wed, 21 May 2025 00:00:00 GMT"


# ---------------------------------------------------------------------------
# Baseline flow — the structured/geo+json signal survives the per-source
# baseline and stays a renderable geo+json node (no text assumption breaks it).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_geojson_signal_flows_through_baseline_unbroken():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FEATURE_COLLECTION, request=req)

    ctx = _make_ctx()
    gj = _make_handler(handler)
    signals = await _collect(gj.pull(ctx, since=None))
    await gj.aclose()
    raw = signals[0]

    enriched = await run_baseline(raw, ctx, media="reference")
    assert enriched is not None
    # Modality + mime preserved through the baseline (the renderer keys on these).
    assert enriched.modality == "structured"
    assert enriched.mime_type == GEOJSON_MIME_TYPE
    assert enriched.media_ref == "https://example.invalid/feed.geojson"
    # The inlined geo+json fragment the UI renderer draws is intact.
    assert enriched.payload["geojson"]["type"] == "Feature"
    # content_hash present (dedupe key); geo carried.
    assert enriched.content_hash
    assert "USA" in enriched.geo
