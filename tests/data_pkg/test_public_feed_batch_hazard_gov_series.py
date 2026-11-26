# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ten public, keyless feeds: disaster and health alerts, space weather, a
council's press releases, the Federal Register's significant documents and two
Eurostat monthly series.

* Every descriptor validates through the real ``SourceDescriptor`` model and
  its handler's own config schema, builds its handler, ships ``draft``, and the
  example profile's overlay walks it to ``active``.
* The licence is never left unset; an attribution class carries a credit line;
  a source whose terms the publisher's page did not state carries the
  conservative class for unreviewed terms and says so in its header.
* Each handler's own parser reads a body in the documented shape. The ECDC,
  GDACS and EMSC bodies are trimmed from the publishers' CC BY 4.0 feeds; the
  others (terms not stated, or reuse needing an acknowledgement a test body does
  not carry) are synthetic bodies of the same shape.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.source_series_api import SeriesApiConfig
from legba.data.sources._contract import InMemoryStateStore, SourceContext
from legba.data.sources.geojson import GeoJSONConfig, GeoJSONSourceHandler
from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler
from legba.data.sources.rss import RSSConfig, RSSSourceHandler
from legba.data.sources.series_api import parse_rows, records_from_rows
from legba.runtime.source_factory import _unwrap_factory_dict, build_source_handler
from tests._lifecycle_rows import lifecycle_text

REPO = Path(__file__).resolve().parents[2]
DESC = REPO / "descriptors"
NOW = datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc)

FEEDS: dict[str, str] = {
    "source_ecdc_epidemiological_updates.yaml": "rss",
    "source_gdacs_events.yaml": "geojson",
    "source_emsc_earthquakes.yaml": "json_api",
    "source_swpc_alerts.yaml": "json_api",
    "source_nhc_atlantic.yaml": "rss",
    "source_nhc_eastern_pacific.yaml": "rss",
    "source_consilium_press_releases.yaml": "rss",
    "source_federalregister_significant.yaml": "json_api",
    "source_eurostat_unemployment_monthly.yaml": "series_api",
    "source_eurostat_industrial_production_monthly.yaml": "series_api",
}
_SCHEMA = {"rss": RSSConfig, "geojson": GeoJSONConfig, "json_api": JsonApiConfig,
           "series_api": SeriesApiConfig}
#: Sources whose publisher page stated no usable terms: the conservative class.
UNREVIEWED = {
    "source_swpc_alerts.yaml", "source_nhc_atlantic.yaml", "source_nhc_eastern_pacific.yaml",
    "source_consilium_press_releases.yaml", "source_federalregister_significant.yaml",
}


def _body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((DESC / name).read_text(encoding="utf-8"))
    body["identity"]["version"] = "0" * 16
    return body


def _lifecycle() -> dict[str, tuple[str, str]]:
    return {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(
        r"^\| source \| (\S+) \| (\w+) \| (\w+) \|$", lifecycle_text(DESC), re.M)}


@pytest.mark.parametrize("name", sorted(FEEDS))
def test_descriptor_validates_ships_draft_and_the_overlay_switches_it_on(name) -> None:
    body = _body(name)
    d = SourceDescriptor.model_validate(body, strict=False)
    kind = FEEDS[name]
    assert d.identity.kind == kind
    _SCHEMA[kind].model_validate(_unwrap_factory_dict(body["config"]))
    build_source_handler(kind, body["config"])
    assert d.identity.state.value == "draft" and d.identity.owner == "owner@local"
    assert d.scope.access_class == "public" and d.scope.source_class == "official"
    assert d.acquisition == "poll" and d.cadence is not None and d.cadence.schedule is not None
    assert _lifecycle().get(d.identity.id) == ("active", "draft"), d.identity.id


@pytest.mark.parametrize("name", sorted(FEEDS))
def test_licence_is_declared_and_its_obligation_recorded(name) -> None:
    d = SourceDescriptor.model_validate(_body(name), strict=False)
    lic = d.scope.license_class
    assert lic is not None
    if name in UNREVIEWED:
        assert lic == "permissive_feed_unreviewed"
        header = (DESC / name).read_text().split("\nidentity:", 1)[0]
        assert "NOT STATED" in header and "conservative class" in header
    else:
        assert lic in ("cc_by", "open_gov_attribution") and d.scope.attribution


@pytest.mark.parametrize("name", sorted(FEEDS))
def test_the_header_names_the_check_and_the_licence_page(name) -> None:
    header = (DESC / name).read_text().split("\nidentity:", 1)[0]
    assert "Checked 2026-11-25" in header and "Licence" in header and "https://" in header


def test_the_series_and_the_pollers_are_not_faster_than_the_publishers() -> None:
    for name in FEEDS:
        d = SourceDescriptor.model_validate(_body(name), strict=False)
        minute, hour = d.cadence.schedule.raw.split()[:2]
        assert minute.isdigit(), name                     # no "every minute"/"*/n" polling
    # the two series poll once a day
    for name in ("source_eurostat_unemployment_monthly.yaml",
                 "source_eurostat_industrial_production_monthly.yaml"):
        d = SourceDescriptor.model_validate(_body(name), strict=False)
        assert d.cadence.schedule.raw.split()[2:] == ["*", "*", "*"] and d.cadence.schedule.raw.split()[1].isdigit()


def test_every_new_series_id_is_unique_across_the_tree() -> None:
    seen: dict[str, str] = {}
    for p in sorted(DESC.glob("source_*.yaml")):
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
        for s in ((body.get("sink") or {}).get("config") or {}).get("series") or []:
            assert s["id"] not in seen, (s["id"], p.name, seen[s["id"]])
            seen[s["id"]] = p.name


# ---------------------------------------------------------------------------
# Handler-level parsing
# ---------------------------------------------------------------------------


def _ctx(source_id: str, cfg: Any, tags: list[str] | None = None) -> SourceContext:
    return SourceContext(target_id="t", target_version="v", source_id=source_id, config=cfg,
                         state_store=InMemoryStateStore(), now_fn=lambda: NOW,
                         scope_geo=[], scope_languages=["en"])


async def _pull(handler: Any, ctx: SourceContext) -> list[Any]:
    return [s async for s in handler.pull(ctx, None)]


def _client(body: bytes, ctype: str, seen: list[str] | None = None) -> httpx.AsyncClient:
    def serve(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        return httpx.Response(200, content=body, headers={"content-type": ctype})
    return httpx.AsyncClient(transport=httpx.MockTransport(serve), follow_redirects=True, timeout=5)


# Trimmed from the ECDC epidemiological-update feed (CC BY 4.0, ECDC credited).
ECDC_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0" xml:base="https://www.ecdc.europa.eu/en">
  <channel>
    <title>Epidemiological update</title>
    <link>https://www.ecdc.europa.eu/en</link>
    <description/>
    <language>en</language>
    <item>
      <title>Ebola disease: Multi-country outbreak 2026</title>
      <link>https://www.ecdc.europa.eu/en/ebola-disease-multi-country-outbreak-2026</link>
      <description>&lt;p&gt;Since May 2026, an Ebola virus disease outbreak caused by Bundibugyo virus has been ongoing in the Democratic Republic of the Congo (DRC).&lt;/p&gt;</description>
      <pubDate>Tue, 17 Nov 2026 17:36:21 +0100</pubDate>
      <dc:creator>ECDC</dc:creator>
      <guid isPermaLink="true">https://www.ecdc.europa.eu/en/ebola-disease-multi-country-outbreak-2026</guid>
    </item>
    <item>
      <title>Chikungunya virus disease worldwide overview</title>
      <link>https://www.ecdc.europa.eu/en/chikungunya-monthly</link>
      <description>&lt;p&gt;Every month ECDC provides an overview of chikungunya virus disease worldwide.&lt;/p&gt;</description>
      <pubDate>Mon, 05 Oct 2026 16:05:05 +0200</pubDate>
      <dc:creator>ECDC</dc:creator>
      <guid isPermaLink="true">https://www.ecdc.europa.eu/en/chikungunya-monthly</guid>
    </item>
  </channel>
</rss>
"""

# A synthetic body of the checked shape: the outlook (a new guid per issue), and
# the placeholder item with the bare site guid and a future pubDate.
NHC_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:georss="http://www.georss.org/georss"
     xmlns:gml="http://www.opengis.net/gml" xmlns:nhc="https://www.nhc.noaa.gov">
  <channel>
    <pubDate>Wed, 25 Nov 2026 22:45:16 GMT</pubDate>
    <title>NHC Atlantic</title>
    <description>Active tropical cyclones in the Atlantic</description>
    <link>https://www.nhc.noaa.gov/</link>
    <item>
      <title>Atlantic Tropical Weather Outlook</title>
      <description><![CDATA[Tropical Weather Outlook<br />For the North Atlantic: tropical cyclone formation is not expected during the next 7 days.]]></description>
      <pubDate>Wed, 25 Nov 2026 17:38:28 GMT</pubDate>
      <link>https://www.nhc.noaa.gov/gtwo.php?basin=atlc</link>
      <guid>https://www.nhc.noaa.gov/gtwo.php?basin=atlc&amp;202611251738</guid>
    </item>
    <item>
      <title>There are no tropical cyclones at this time.</title>
      <description>No tropical cyclones as of Wed, 25 Nov 2026 22:45:16 GMT</description>
      <pubDate>Fri, 27 Nov 2026 05:38:28 GMT</pubDate>
      <link>https://www.nhc.noaa.gov/</link>
      <guid>https://www.nhc.noaa.gov/</guid>
    </item>
  </channel>
</rss>
"""

# A synthetic body of the checked shape: an Atom-namespace `updated` in place of
# a pubDate, a numeric guid, categories.
COUNCIL_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:a10="http://www.w3.org/2005/Atom" version="2.0"><channel>
<title>Council of the EU</title><link>https://www.consilium.europa.eu/</link><description />
<language>en</language>
<item><guid isPermaLink="false">900001</guid>
<link>https://www.consilium.example/en/press/press-releases/2026/11/25/invented-release/</link>
<title>Invented release: the Council adopts a measure</title>
<description>The Council adopted an invented measure on 25 November 2026.</description>
<a10:updated>2026-11-25T11:09:00+01:00</a10:updated>
<category>Sanctions</category><category>Trade</category></item>
</channel></rss>
"""


@pytest.mark.asyncio
async def test_ecdc_updates_parse() -> None:
    d = _body("source_ecdc_epidemiological_updates.yaml")
    cfg = RSSConfig.model_validate(_unwrap_factory_dict(d["config"]))
    h = RSSSourceHandler(cfg, http_client=_client(ECDC_RSS.encode(), "application/rss+xml"))
    sigs = await _pull(h, _ctx("source.ecdc.epidemiological_updates", cfg))
    titles = {s.payload["title"] for s in sigs}
    assert "Ebola disease: Multi-country outbreak 2026" in titles
    first = next(s for s in sigs if s.payload["title"].startswith("Ebola"))
    assert first.canonical_url == "https://www.ecdc.europa.eu/en/ebola-disease-multi-country-outbreak-2026"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,sid", [("source_nhc_atlantic.yaml", "source.nhc.atlantic"),
                                      ("source_nhc_eastern_pacific.yaml", "source.nhc.eastern_pacific")])
async def test_nhc_keeps_the_outlook_and_drops_the_no_storm_placeholder(name, sid) -> None:
    cfg = RSSConfig.model_validate(_unwrap_factory_dict(_body(name)["config"]))
    h = RSSSourceHandler(cfg, http_client=_client(NHC_RSS.encode(), "text/xml"))
    sigs = await _pull(h, _ctx(sid, cfg))
    assert len(sigs) == 2                                    # the parser reads both items
    kept = [s.payload["title"] for s in sigs if h.keyword_gate.admit(s, source_id=sid)]
    assert kept == ["Atlantic Tropical Weather Outlook"]     # the gate drops the placeholder


@pytest.mark.asyncio
async def test_council_items_are_read_with_the_atom_updated_as_the_date() -> None:
    cfg = RSSConfig.model_validate(_unwrap_factory_dict(_body("source_consilium_press_releases.yaml")["config"]))
    h = RSSSourceHandler(cfg, http_client=_client(COUNCIL_RSS.encode(), "text/xml"))
    sigs = await _pull(h, _ctx("source.consilium.press_releases", cfg))
    assert len(sigs) == 1
    p = sigs[0].payload
    assert p["title"].startswith("Invented release") and p["link"].endswith("/invented-release/")
    assert (p.get("published_at") or "").startswith("2026-11-25")


# Trimmed from the GDACS event list (CC BY 4.0, GDACS credited).
GDACS_JSON = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "bbox": [-66.243, 7.036, -66.243, 7.036],
         "geometry": {"type": "Point", "coordinates": [-66.243, 7.036]},
         "properties": {
             "eventtype": "DR", "eventid": 1023877, "episodeid": 15,
             "name": "Drought in Brazil, Colombia, Venezuela",
             "description": "Drought in Brazil, Colombia, Venezuela",
             "alertlevel": "Orange", "iscurrent": "false", "iso3": "VEN",
             "country": "Brazil, Colombia, Venezuela",
             "fromdate": "2022-09-21T00:00:00", "todate": "2026-11-23T00:00:00",
             "datemodified": "2026-11-25T05:57:06",
             "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1023877&episodeid=15&eventtype=DR"}}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [123.0, -0.35]},
         "properties": {"eventtype": "EQ", "eventid": 1500001, "episodeid": 1,
                        "name": "Earthquake in Indonesia", "alertlevel": "Red", "iso3": "IDN",
                        "datemodified": "2026-11-25T12:50:00"}},
    ],
}


@pytest.mark.asyncio
async def test_gdacs_events_use_the_event_id_and_the_name() -> None:
    cfg = GeoJSONConfig.model_validate(_unwrap_factory_dict(_body("source_gdacs_events.yaml")["config"]))
    h = GeoJSONSourceHandler(cfg, http_client=_client(json.dumps(GDACS_JSON).encode(), "application/json"))
    sigs = await _pull(h, _ctx("source.gdacs.events", cfg))
    by = {s.payload["external_id"]: s for s in sigs}
    assert set(by) == {"1023877", "1500001"}
    assert by["1500001"].payload["title"] == "Earthquake in Indonesia"
    assert by["1023877"].payload["geometry_type"] == "Point" and by["1023877"].modality == "structured"
    assert "IDN" in by["1500001"].geo


# Trimmed from the EMSC FDSN GeoJSON (CC BY 4.0, EMSC credited).
EMSC_JSON = {
    "type": "FeatureCollection",
    "metadata": {"count": 2},
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [123.0313, -0.3529, -84.0]},
         "id": "20261125_0000184",
         "properties": {"source_id": "2094959", "source_catalog": "EMSC-RTS",
                        "lastupdate": "2026-11-25T12:42:23.319367Z", "time": "2026-11-25T12:08:11.45Z",
                        "flynn_region": "SULAWESI, INDONESIA", "lat": -0.3529, "lon": 123.0313,
                        "depth": 84.0, "evtype": "ke", "auth": "EMSC", "mag": 5.6, "magtype": "mb",
                        "unid": "20261125_0000184"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-170.0, -20.0, -10.0]},
         "id": "20261124_0000100",
         "properties": {"time": "2026-11-24T03:00:00.0Z", "flynn_region": "TONGA REGION", "lat": -20.0,
                        "lon": -170.0, "depth": 10.0, "auth": "EMSC", "mag": 6.1, "magtype": "mw"}},
    ],
}


@pytest.mark.asyncio
async def test_emsc_quakes_get_a_readable_title_and_the_epicentre() -> None:
    cfg = JsonApiConfig.model_validate(_unwrap_factory_dict(_body("source_emsc_earthquakes.yaml")["config"]))
    seen: list[str] = []
    h = JsonApiSourceHandler(cfg, http_client=_client(json.dumps(EMSC_JSON).encode(), "application/json", seen))
    sigs = await _pull(h, _ctx("source.emsc.earthquakes", cfg))
    by = {s.payload["external_id"]: s.payload for s in sigs}
    assert by["20261125_0000184"]["title"] == "M5.6 mb earthquake: SULAWESI, INDONESIA"
    assert by["20261125_0000184"]["published_at"] == "2026-11-25T12:08:11.450000+00:00"
    assert "Depth: 84.0 km" in by["20261125_0000184"]["raw_body"]
    assert by["20261125_0000184"]["geometry"]["coordinates"] == [123.0313, -0.3529]
    assert seen and "minmag=5.5" in seen[0] and "format=json" in seen[0]


# A synthetic body of the checked shape: three fields per item, newest first.
SWPC_JSON = [
    {"product_id": "A20F", "issue_datetime": "2026-11-25 08:33:12.430",
     "message": "Space Weather Message Code: WATA20\r\nSerial Number: 1\r\nWATCH: Geomagnetic Storm Category G1 Predicted"},
    {"product_id": "ALTK04", "issue_datetime": "2026-11-24 23:01:05.100",
     "message": "ALERT: Geomagnetic K-index of 4\r\nThreshold Reached: 2026 Nov 24 2300 UTC"},
]


@pytest.mark.asyncio
async def test_swpc_alerts_are_identified_by_their_issue_time() -> None:
    cfg = JsonApiConfig.model_validate(_unwrap_factory_dict(_body("source_swpc_alerts.yaml")["config"]))
    h = JsonApiSourceHandler(cfg, http_client=_client(json.dumps(SWPC_JSON).encode(), "application/json"))
    sigs = await _pull(h, _ctx("source.swpc.alerts", cfg))
    by = {s.payload["external_id"]: s.payload for s in sigs}
    assert set(by) == {"2026-11-25 08:33:12.430", "2026-11-24 23:01:05.100"}
    first = by["2026-11-25 08:33:12.430"]
    assert first["title"] == "NOAA SWPC space weather alert A20F"
    assert first["published_at"] == "2026-11-25T08:33:12.430000+00:00"
    assert "Geomagnetic Storm Category G1" in first["raw_body"]


# A synthetic body of the checked shape (the API's result keys).
FR_JSON = {
    "description": "Documents whose Associated Unified Agenda Deemed Significant Under EO 12866",
    "count": 2, "total_pages": 1, "next_page_url": None,
    "results": [
        {"title": "Invented Rule on Widget Standards", "type": "Rule", "abstract": "An invented abstract.",
         "document_number": "2026-90001",
         "html_url": "https://www.federalregister.example/documents/2026/11/25/2026-90001/invented-rule",
         "publication_date": "2026-11-25", "agencies": [{"name": "Invented Agency"}]},
        {"title": "Invented Notice", "type": "Notice", "abstract": None, "document_number": "2026-90002",
         "html_url": "https://www.federalregister.example/documents/2026/11/25/2026-90002/invented-notice",
         "publication_date": "2026-11-25", "agencies": []},
    ],
}


@pytest.mark.asyncio
async def test_federal_register_documents_map_to_signals_and_ask_from_yesterday() -> None:
    cfg = JsonApiConfig.model_validate(_unwrap_factory_dict(_body("source_federalregister_significant.yaml")["config"]))
    seen: list[str] = []
    h = JsonApiSourceHandler(cfg, http_client=_client(json.dumps(FR_JSON).encode(), "application/json", seen))
    sigs = await _pull(h, _ctx("source.federalregister.significant_documents", cfg))
    by = {s.payload["external_id"]: s for s in sigs}
    assert set(by) == {"2026-90001", "2026-90002"}
    rule = by["2026-90001"]
    assert rule.payload["title"] == "Rule: Invented Rule on Widget Standards"
    assert rule.canonical_url.endswith("/2026-90001/invented-rule")
    assert rule.payload["published_at"] == "2026-11-25T00:00:00+00:00"
    assert "conditions%5Bpublication_date%5D%5Bgte%5D=2026-11-24" in seen[0]
    assert "conditions%5Bsignificant%5D=1" in seen[0]


def _cube(dims: list[tuple[str, list[str]]], values: dict[str, float], status: dict[str, str] | None = None) -> bytes:
    cube: dict[str, Any] = {
        "version": "2.0", "class": "dataset", "label": "invented", "source": "x", "updated": "2026-11-24",
        "id": [d for d, _ in dims], "size": [len(c) for _, c in dims],
        "dimension": {d: {"category": {"index": {c: i for i, c in enumerate(codes)}}} for d, codes in dims},
        "value": values,
    }
    if status:
        cube["status"] = status
    return json.dumps(cube).encode()


def _series_recs(name: str, body: bytes, since: date | None = None):
    cfg = SeriesApiConfig.model_validate(_unwrap_factory_dict(_body(name)["config"]))
    return records_from_rows(parse_rows(body, cfg), cfg, page_url="https://example.invalid/p",
                             sha256="0" * 64, since=since)


def test_unemployment_cube_reads_country_months() -> None:
    body = _cube([("freq", ["M"]), ("s_adj", ["SA"]), ("age", ["TOTAL"]), ("unit", ["PC_ACT"]),
                  ("sex", ["T"]), ("geo", ["DE", "ES"]), ("time", ["2026-09", "2026-10"])],
                 {"0": 3.9, "1": 4.0, "2": 10.4, "3": 10.3}, {"3": "p"})
    recs, _ = _series_recs("source_eurostat_unemployment_monthly.yaml", body)
    got = sorted((r.series_id, r.subject, r.subject_kind, r.valid_from, r.valid_to, str(r.value), r.unit,
                  r.provenance.get("status")) for r in recs)
    assert got == [
        ("eurostat.unemployment.rate_sa", "DE", "country", date(2026, 9, 1), date(2026, 9, 30), "3.9", "percent", None),
        ("eurostat.unemployment.rate_sa", "DE", "country", date(2026, 10, 1), date(2026, 10, 31), "4.0", "percent", None),
        ("eurostat.unemployment.rate_sa", "ES", "country", date(2026, 9, 1), date(2026, 9, 30), "10.4", "percent", None),
        ("eurostat.unemployment.rate_sa", "ES", "country", date(2026, 10, 1), date(2026, 10, 31), "10.3", "percent", "p"),
    ]


def test_industrial_production_splits_the_area_from_the_countries() -> None:
    body = _cube([("freq", ["M"]), ("indic_bt", ["PRD"]), ("nace_r2", ["B-D"]), ("s_adj", ["SCA"]),
                  ("unit", ["I21"]), ("geo", ["EA20", "FR"]), ("time", ["2026-08", "2026-09"])],
                 {"0": 97.3, "1": 97.6, "2": 102.3, "3": 101.8}, {"1": "p"})
    recs, _ = _series_recs("source_eurostat_industrial_production_monthly.yaml", body)
    got = sorted((r.series_id, r.subject, r.subject_kind, r.valid_from, str(r.value), r.unit) for r in recs)
    assert got == [
        ("eurostat.industrial_production.index", "FR", "country", date(2026, 8, 1), "102.3", "index"),
        ("eurostat.industrial_production.index", "FR", "country", date(2026, 9, 1), "101.8", "index"),
        ("eurostat.industrial_production.index_area", "EA20", "entity", date(2026, 8, 1), "97.3", "index"),
        ("eurostat.industrial_production.index_area", "EA20", "entity", date(2026, 9, 1), "97.6", "index"),
    ]
    assert {r.provenance.get("status") for r in recs} == {None, "p"}
