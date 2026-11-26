# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""json_api ``title_template`` + ``geometry``, and the geocoder's
at-sea rung, end to end through the real handler and the real geocode filter.

Fixture records mirror the UKMTO incident shape (field names and value
quirks such as ``"Tanker "`` and a vesselName of ``"."``); no payload from the
real endpoint is committed or fetched.
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import yaml
from pydantic import ValidationError

from legba.data.filters import _geo_precedence as prec
from legba.data.filters._contract import FilterContext
from legba.data.filters.geocode import GeocodeConfig, GeocodeHandler
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import InMemoryStateStore, Signal, SourceContext
from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler
from legba.data.sources.json_api_mapping import (
    JsonApiGeometry,
    apply_geometry,
    parse_title_template,
    render_title,
)
from legba.runtime.source_factory import _unwrap_factory_dict

REPO = pathlib.Path(__file__).resolve().parents[2]
UKMTO = REPO / "descriptors" / "source_ukmto_incidents.yaml"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
TEMPLATE = "{incidentTypeName}: {place} — {vesselType}"


def _incident(**over: Any) -> dict[str, Any]:
    rec = {
        "incidentNumber": 143, "sitecoreId": "a5b33f9d-0000-4000-8000-000000000143",
        "utcDateCreated": "2026-11-13T10:28:14Z", "utcDateOfIncident": "2026-11-12T23:00:00Z",
        "incidentTypeName": "Attack", "incidentTypeLevel": 3, "region": "IO",
        "place": "Strait of Hormuz", "locationLatitude": 26.433333, "locationLongitude": 56.486111,
        "vesselName": ".", "vesselType": "Tanker ", "crewHeld": 0,
        "otherDetails": "UKMTO has received a report of an incident 20NM north of Oman.",
    }
    rec.update(over)
    return rec


# ---------------------------------------------------------------------------
# title_template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("item, expected", [
    ({"a": "A", "b": "B", "c": "C"}, "A: B — C"),
    ({"a": "A", "c": "C"}, "A — C"),
    ({"b": "B", "c": "C"}, "B — C"),
    ({"a": "A", "b": "B"}, "A: B"),
    ({"a": "A", "b": " . ", "c": "-"}, "A"),
    ({"a": "  Attack\n ", "b": 7, "c": True}, "Attack: 7"),
    ({}, ""),
])
def test_render_title_drops_missing_fields_with_their_separator(item, expected):
    assert render_title("{a}: {b} — {c}", item) == expected


def test_render_title_keeps_prefix_suffix_only_with_a_value():
    assert render_title("UKMTO {a}!", {"a": "Attack"}) == "UKMTO Attack!"
    assert render_title("UKMTO {a}!", {}) == ""
    assert render_title("{x.y[0]} / {x.z}", {"x": {"y": ["deep"], "z": "q"}}) == "deep / q"


@pytest.mark.parametrize("bad", ["no placeholder", "{a} {", "{a}}", "{}", "{a..b}", "{ }",
                                 "".join("{f%d}" % i for i in range(9))])
def test_title_template_is_validated_at_config_parse(bad):
    with pytest.raises((ValueError, ValidationError)):
        parse_title_template(bad)
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/a", title_template=bad)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def test_geometry_block_is_validated():
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/a", geometry={"lat_path": "lat"})
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/a",
                      geometry={"lat_path": "a..b", "lon_path": "lon"})
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/a",
                      geometry={"lat_path": "lat", "lon_path": "lon", "off_land": "guess"})
    with pytest.raises(ValidationError):
        JsonApiConfig(url_template="https://x.invalid/a",
                      geometry={"lat_path": "lat", "lon_path": "lon", "nope": 1})


@pytest.mark.parametrize("lat, lon, stamped", [
    (26.4, 56.5, True), ("26.4", "56.5", True), (0, 0, False), (None, 56.5, False),
    (91, 10, False), (10, 181, False), (True, 10, False), ("x", 10, False),
    (float("nan"), 10, False),
])
def test_apply_geometry_stamps_only_a_usable_point(lat, lon, stamped):
    payload: dict[str, Any] = {}
    apply_geometry(payload, {"la": lat, "lo": lon, "p": "Gulf of Aden"},
                   JsonApiGeometry(lat_path="la", lon_path="lo", place_path="p"))
    assert payload["geo"] == {"location_name": "Gulf of Aden"}
    if stamped:
        assert payload["geometry"] == {"type": "Point", "coordinates": [float(lon), float(lat)]}
        assert payload["geometry_authoritative"] is True
    else:
        assert "geometry" not in payload and "geometry_authoritative" not in payload


def test_text_fallback_mode_does_not_mark_the_point_authoritative():
    payload: dict[str, Any] = {}
    apply_geometry(payload, {"la": 1.0, "lo": 2.0},
                   JsonApiGeometry(lat_path="la", lon_path="lo", off_land="text_fallback"))
    assert payload["geometry"]["coordinates"] == [2.0, 1.0]
    assert "geometry_authoritative" not in payload and "geo" not in payload


# ---------------------------------------------------------------------------
# The UKMTO descriptor, through the real handler
# ---------------------------------------------------------------------------


def _ukmto_config() -> JsonApiConfig:
    desc = SourceDescriptor.model_validate(yaml.safe_load(UKMTO.read_text()), strict=False)
    return JsonApiConfig(**_unwrap_factory_dict(desc.config))


def test_ukmto_descriptor_declares_the_budget_template_and_geometry():
    cfg = _ukmto_config()
    assert cfg.poll_budget_seconds == 180
    assert cfg.title_template == TEMPLATE
    assert cfg.geometry == JsonApiGeometry(lat_path="locationLatitude", lon_path="locationLongitude",
                                           place_path="place", off_land="no_country")
    assert cfg.geo_path is None   # `region` ("IO") is never read as a country


async def _pull(cfg: JsonApiConfig, items: list[dict[str, Any]]) -> list[Signal]:
    body = json.dumps(items)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text=body, request=req)))
    handler = JsonApiSourceHandler(cfg, http_client=client)
    ctx = SourceContext(target_id="t", target_version="v", source_id="source.ukmto.incidents",
                        config=cfg, state_store=InMemoryStateStore(), now_fn=lambda: NOW,
                        scope_geo=[], scope_languages=["en"])
    out = [s async for s in handler.pull(ctx, since=datetime(2026, 11, 1, tzinfo=timezone.utc))]
    await handler.aclose()
    return out


@pytest.mark.asyncio
async def test_ukmto_records_map_to_composed_titles_and_their_own_point():
    cfg = _ukmto_config()
    sigs = await _pull(cfg, [
        _incident(),
        _incident(sitecoreId="b", place="", vesselType="Bulk Carrier"),
        _incident(sitecoreId="c", incidentTypeName="", place="", vesselType="",
                  locationLatitude=None),
    ])
    assert [s.payload["title"] for s in sigs] == [
        "Attack: Strait of Hormuz — Tanker", "Attack — Bulk Carrier", "",
    ]
    first = sigs[0].payload
    assert first["geometry"] == {"type": "Point", "coordinates": [56.486111, 26.433333]}
    assert first["geometry_authoritative"] is True
    assert first["geo"] == {"location_name": "Strait of Hormuz"}
    assert sigs[0].geo == []                          # no declared geo, region ignored
    assert "geometry" not in sigs[2].payload           # a record without a point
    # The content hash keys on the composed title (a dedupe key per incident).
    assert len({s.content_hash for s in sigs}) == 3


# ---------------------------------------------------------------------------
# The geocoder's at-sea rung
# ---------------------------------------------------------------------------


class _Backend:
    name = "stub"

    def __init__(self, answer: Any = None) -> None:
        self.calls: list[str] = []
        self._answer = answer

    async def geocode(self, query: str):
        self.calls.append(query)
        return self._answer

    async def reachable(self) -> bool:
        return True


def _fctx(**over: Any) -> FilterContext:
    base: dict[str, Any] = dict(target_id="t", target_version="v", filter_id="geocode",
                                logger=logging.getLogger("test.n_r.geocode"))
    base.update(over)
    return FilterContext(**base)


def _sig(payload: dict[str, Any]) -> Signal:
    return Signal(source_id="source.ukmto.incidents", payload=payload, tags=["maritime"])


def _at_sea_payload(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Attack: Strait of Hormuz — Tanker",
                               "raw_body": "An incident 20NM north of Oman, near Iran."}
    apply_geometry(payload, _incident(**over),
                   JsonApiGeometry(lat_path="locationLatitude", lon_path="locationLongitude",
                                   place_path="place"))
    return payload


@pytest.mark.asyncio
async def test_an_authoritative_point_at_sea_files_no_country_and_never_guesses():
    backend = _Backend()
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    out = await handler.transform(_sig(_at_sea_payload()), _fctx())
    geo = out.payload["geo"]
    assert geo["at_sea"] is True and geo["countries"] == []
    assert (geo["lat"], geo["lon"]) == (26.433333, 56.486111)
    assert geo["location_name"] == "Strait of Hormuz"
    assert "country_iso2" not in geo
    assert out.tags == ["maritime", prec.AT_SEA_TAG]
    assert backend.calls == []                         # no text ladder ran
    assert prec.promote_payload_columns(out).geo == []  # signals.geo stays empty


@pytest.mark.asyncio
async def test_an_authoritative_point_on_land_resolves_by_geometry():
    handler = GeocodeHandler(GeocodeConfig(), backend=_Backend())
    # Muscat, on land.
    out = await handler.transform(
        _sig(_at_sea_payload(locationLatitude=23.588, locationLongitude=58.3829)), _fctx())
    assert out.payload["geo"]["country_iso2"] == "OM"
    assert "at_sea" not in out.payload["geo"] and prec.AT_SEA_TAG not in out.tags


@pytest.mark.asyncio
async def test_an_issuer_keeps_its_declared_country_beside_an_at_sea_point():
    handler = GeocodeHandler(GeocodeConfig(), backend=_Backend())
    out = await handler.transform(
        _sig(_at_sea_payload()), _fctx(source_class="official", scope_geo=["GB"]))
    geo = out.payload["geo"]
    assert geo["at_sea"] is True and geo["countries"] == ["GB"]
    assert geo["derivation"] == {"GB": [prec.DECLARED]}


@pytest.mark.asyncio
async def test_a_non_authoritative_point_at_sea_keeps_the_text_ladder():
    backend = _Backend()
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    payload = _at_sea_payload()
    payload.pop("geometry_authoritative")
    out = await handler.transform(_sig(payload), _fctx())
    assert backend.calls, "text_fallback / legacy geometry feeds still run the ladder"
    assert prec.AT_SEA_TAG not in out.tags


@pytest.mark.asyncio
async def test_baseline_gives_an_at_sea_signal_no_publisher_home_country():
    """The N-B publisher-origin fallback must not file an at-sea incident under
    the publisher's home (a GB-scoped maritime source would tag Hormuz GB)."""
    from pydantic import BaseModel

    from legba.data.sources.baseline import run_baseline

    class _Cfg(BaseModel):
        pass

    ctx = SourceContext(target_id="t", target_version="v", source_id="source.x",
                        config=_Cfg(), state_store=InMemoryStateStore(), scope_geo=["GB"])
    handler = GeocodeHandler(GeocodeConfig(), backend=_Backend())

    async def stage(sig: Signal, _ctx: Any) -> Signal:
        out = await handler.transform(sig, _fctx())
        return prec.promote_payload_columns(out)

    body = {"title": "Attack: Gulf of Aden", "raw_body": "A vessel was approached."}
    point = {k: v for k, v in _at_sea_payload().items() if k not in body}
    at_sea = await run_baseline(_sig({**body, **point}), ctx, enrichment_stage=stage)
    assert at_sea.geo == [] and "geo_fallback" not in at_sea.payload
    # Control: the same signal with no point still takes the marked fallback.
    plain = await run_baseline(_sig(dict(body)), ctx, enrichment_stage=stage)
    assert plain.geo == ["GB"] and plain.payload["geo_fallback"]["basis"] == "publisher_origin"
