# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-L unit tests: the sink seam, the declared seams (#77 families, #78
subscribe), kinds by registration, the fetch-capture tee, the PortWatch parse
and the series_change rule. DB-backed acceptance is
``tests/data_pkg/test_ingestion_series_acceptance.py``."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
import yaml
from pydantic import ValidationError

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.source_sink import SEAM_FAMILIES, SourceSink
from legba.data.schemas.versioning import content_hash
from legba.data.sources import fetch_capture as fc
from legba.data.sources.records import ObservationRecord, record_family
from legba.runtime.sinks import SinkSet, UnroutableRecord, sink_factories

from tests.runtime import _ingest_fakes as F

PORTWATCH_YAML = F.REPO / "descriptors" / "source_imf_portwatch_chokepoints.yaml"


def _portwatch_body() -> dict[str, Any]:
    return yaml.safe_load(PORTWATCH_YAML.read_text())


# ---------------------------------------------------------------------------
# The descriptor seam
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", sorted(SEAM_FAMILIES))
def test_an_unbuilt_family_fails_loud_at_save(family: str) -> None:
    body = _portwatch_body()
    body["sink"] = {"family": family}
    with pytest.raises(ValidationError) as exc:
        SourceDescriptor.model_validate(body, strict=False)
    assert "SEAMS #77" in str(exc.value) and "not built" in str(exc.value)


def test_subscribe_acquisition_fails_loud_at_save() -> None:
    """I-4 built the ``sse`` transport: ``subscribe`` is saveable WITH a
    ``subscribe`` block, refused without one, and an unbuilt transport still
    names SEAMS #78 (tests/runtime/test_subscribe_units.py has the rest)."""
    body = _portwatch_body()
    body["acquisition"] = "subscribe"
    with pytest.raises(ValidationError, match="needs a 'subscribe' block"):
        SourceDescriptor.model_validate(body, strict=False)
    body["subscribe"] = {"transport": "websocket", "url": "wss://stream.example.org/x"}
    with pytest.raises(ValidationError) as exc:
        SourceDescriptor.model_validate(body, strict=False)
    assert "SEAMS #78" in str(exc.value)


def test_observation_config_is_validated_at_save() -> None:
    body = _portwatch_body()
    body["sink"]["config"]["series"][0]["unit"] = "furlongs"
    with pytest.raises(ValidationError, match="unit 'furlongs'"):
        SourceDescriptor.model_validate(body, strict=False)
    with pytest.raises(ValidationError, match="takes no sink.config"):
        SourceSink(family="document", config={"series": []})


def test_the_default_sink_is_hash_neutral() -> None:
    body = _portwatch_body()
    body.pop("sink")
    d = SourceDescriptor.model_validate(body, strict=False)
    assert d.sink.family == "document"
    assert "sink" not in d.model_dump(mode="json")
    explicit = SourceDescriptor.model_validate({**body, "sink": {"family": "document"}}, strict=False)
    assert content_hash(explicit) == content_hash(d)


def test_the_portwatch_descriptor_parses_and_declares_seven_series() -> None:
    d = SourceDescriptor.model_validate(_portwatch_body(), strict=False)
    assert d.identity.state.value == "draft" and d.sink.family == "observation"
    assert {s.unit for s in d.sink.observation_config().series} == {"vessels"}


# ---------------------------------------------------------------------------
# The sink registry
# ---------------------------------------------------------------------------


class _Core:
    def __init__(self, family: str, desc: SourceDescriptor | None = None) -> None:
        self.descriptor = desc or SourceDescriptor.model_validate(_portwatch_body(), strict=False)
        self.actor_id = "a::b::c"


def test_the_registry_holds_the_built_families_only() -> None:
    assert {"document", "observation"} <= set(sink_factories())
    assert not set(SEAM_FAMILIES) & set(sink_factories())


def test_a_record_of_another_family_is_unroutable() -> None:
    core = _Core("observation")
    sinks = SinkSet(core, "observation")
    from legba.data.sources._contract import Signal

    with pytest.raises(UnroutableRecord):
        sinks.for_record(Signal(source_id="s"))
    with pytest.raises(TypeError):
        record_family(object())


def test_an_unregistered_family_is_refused_when_the_actor_is_built() -> None:
    with pytest.raises(UnroutableRecord):
        SinkSet(_Core("x"), "register")


# ---------------------------------------------------------------------------
# Kinds by registration
# ---------------------------------------------------------------------------


def test_kinds_come_from_registration_and_portwatch_is_a_plugin() -> None:
    from legba.data.sources.kinds import declared_source_kinds
    from legba.runtime import source_factory

    table = declared_source_kinds()
    assert table[:18] == source_factory._SOURCE_MODULE_TABLE[:18]
    assert ("portwatch", "PortWatchSourceHandler") in table
    assert table[0] == ("rss", "RSSSourceHandler")
    kinds = source_factory.discover_source_kinds()
    # I-2: the ``page`` kind is a plug-in; S-1: ``series_api`` another;
    # I-4: ``stream_json`` (the subscribe parser) another.
    assert "portwatch" in kinds and "page" in kinds and "series_api" in kinds and len(kinds) == 22
    assert "stream_json" in kinds


def test_a_dotted_declaration_resolves_outside_the_package(monkeypatch) -> None:
    from legba.runtime import source_factory

    monkeypatch.setattr(source_factory, "_SOURCE_MODULE_TABLE",
                        source_factory._SOURCE_MODULE_TABLE + (("legba.data.sources.portwatch", "PortWatchSourceHandler"),))
    from legba.data.kind_discovery import KindDiscoveryError

    # The same class twice is a duplicate kind — and it was resolved by its dotted path.
    with pytest.raises(KindDiscoveryError, match="duplicate_kind"):
        source_factory.discover_source_kinds()


# ---------------------------------------------------------------------------
# The fetch-capture tee
# ---------------------------------------------------------------------------


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


class _Streaming(httpx.AsyncBaseTransport):
    """A transport that returns a STREAMING body, as a network one does."""

    def __init__(self, status: int, chunks: list[bytes], headers: dict[str, str]) -> None:
        self.status, self.chunks, self.headers = status, chunks, headers

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, headers=self.headers, stream=_Chunks(list(self.chunks)))


@pytest.mark.asyncio
async def test_no_scope_means_no_wrapper() -> None:
    resp = httpx.Response(200, stream=_Chunks([b"x"]))
    before = resp.stream
    assert fc.tee_response(httpx.Request("GET", "https://a.example/"), resp) is resp
    assert resp.stream is before


@pytest.mark.asyncio
async def test_a_streamed_gzip_body_is_captured_decoded_and_hashed() -> None:
    raw = b"<rss>" + b"x" * 5000 + b"</rss>"
    gz = gzip.compress(raw)
    t = fc.ReceiptTeeTransport(_Streaming(200, [gz[:100], gz[100:]], {
        "content-encoding": "gzip", "content-type": "application/rss+xml"}))
    log = fc.CaptureLog()
    async with httpx.AsyncClient(transport=t) as c:
        with fc.capture_scope(log):
            r = await c.get("https://feeds.example.org/rss")
    assert r.content == raw
    [cap] = log.captures
    assert cap.complete and cap.body == raw and cap.sha256 == hashlib.sha256(raw).hexdigest()
    assert cap.content_encoding == "gzip" and cap.hash_basis == "entity" and cap.role == "response"


@pytest.mark.asyncio
async def test_an_abandoned_stream_keeps_no_bytes() -> None:
    t = fc.ReceiptTeeTransport(_Streaming(200, [b"a" * 10, b"b" * 10], {}))
    log = fc.CaptureLog()
    async with httpx.AsyncClient(transport=t) as c:
        with fc.capture_scope(log):
            async with c.stream("GET", "https://big.example.org/") as resp:
                async for _ in resp.aiter_raw():
                    break
    [cap] = log.captures
    assert not cap.complete and cap.body is None and cap.sha256 is None


@pytest.mark.asyncio
async def test_a_page_role_and_a_redirect_hop() -> None:
    def _serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://site.example.org/new"})
        return httpx.Response(200, content=b"<html>article</html>", headers={"content-type": "text/html"})

    log = fc.CaptureLog()
    async with F.client_for(_serve) as c:
        with fc.capture_scope(log), fc.fetch_role("page"):
            await c.get("https://site.example.org/old")
    [cap] = log.captures   # the 301 hop is not a receipt; the final page is
    assert cap.url.endswith("/new") and cap.role == "page"
    assert log.pages_since_last_yield() == [0] and log.latest_response() is None


# ---------------------------------------------------------------------------
# PortWatch parse + window, series_change rule
# ---------------------------------------------------------------------------


def _layer_page(rows: list[tuple[str, str, int]], *, more: bool = False) -> bytes:
    feats = [{"attributes": {"date": d, "portid": p, "portname": p.upper(), "ObjectId": i,
                             "n_total": v, "n_tanker": v // 2, "n_container": 1, "n_dry_bulk": 0,
                             "n_general_cargo": 0, "n_roro": 0, "n_cargo": v - v // 2}}
             for i, (d, p, v) in enumerate(rows)]
    doc: dict[str, Any] = {"features": feats}
    if more:
        doc["exceededTransferLimit"] = True
    return json.dumps(doc).encode()


def test_portwatch_parse_page_shape() -> None:
    from legba.data.sources.portwatch import PortWatchConfig, parse_page

    body = _layer_page([("2026-11-08", "chokepoint1", 43)], more=True)
    recs, more, sha = parse_page(body, cfg=PortWatchConfig(), page_url="https://pw.example/query")
    assert more and sha == hashlib.sha256(body).hexdigest()
    assert len(recs) == 7 and all(r.fetch_sha256 == sha for r in recs)
    total = next(r for r in recs if r.series_id.endswith("n_total"))
    assert (total.subject, total.valid_from, total.value, total.unit, total.period) == (
        "chokepoint1", date(2026, 11, 8), Decimal("43"), "vessels", "day")
    with pytest.raises(ValueError):
        parse_page(b'{"error": {"code": 400}}', cfg=PortWatchConfig(), page_url="u")


def test_portwatch_window_covers_lag_and_revisions() -> None:
    from legba.data.sources.portwatch import PortWatchConfig, build_where, window_start

    cfg = PortWatchConfig()
    today = date(2026, 11, 13)
    assert window_start(today, cfg, first_poll=True) == date(2026, 9, 29)
    assert window_start(today, cfg, first_poll=False) == date(2026, 10, 30)
    assert window_start(today, cfg.model_copy(update={"backfill_from": "2026-01-01"}), first_poll=False) == date(2026, 1, 1)
    assert build_where(date(2026, 10, 30), ["chokepoint1"]) == "date >= DATE '2026-10-30' AND portid IN ('chokepoint1')"
    with pytest.raises(ValidationError):
        PortWatchConfig(backfill_days=365)   # not a bulk downloader (IMF terms)


def test_series_change_rule() -> None:
    from legba.data.analysts.deterministic_handlers.series_change import evaluate_pair

    anchor = date(2026, 11, 8)
    flat = [{"valid_from": anchor - timedelta(days=i), "value": 40 + (i % 3)} for i in range(35)]
    kw = dict(current_days=7, baseline_days=28, n_sigma=2.0, min_current=5.0, min_baseline_points=14)
    assert evaluate_pair(flat, **kw)["status"] == "within"
    spike = [dict(p, value=p["value"] * 3 if (anchor - p["valid_from"]).days < 7 else p["value"]) for p in flat]
    res = evaluate_pair(spike, **kw)
    assert res["status"] == "above" and res["anchor"] == "2026-11-08" and len(res["current_points"]) == 7
    assert evaluate_pair(flat[:10], **kw)["status"] == "insufficient"
    # Anchored on the newest HELD day: a provider lag is not a collapse.
    assert evaluate_pair(flat, **kw)["current_window"] == ["2026-11-02", "2026-11-08"]


def test_an_observation_record_is_explicit_about_time_and_units() -> None:
    base = dict(series_id="s", subject="x", valid_from=date(2026, 1, 2), valid_to=date(2026, 1, 2),
                value=Decimal(1), unit="vessels", methodology_version="m/1", source_url="u")
    ObservationRecord(**base)
    with pytest.raises(ValidationError):
        ObservationRecord(**{**base, "unit": "parsecs"})
    with pytest.raises(ValidationError):
        ObservationRecord(**{**base, "valid_to": date(2026, 1, 1)})
    with pytest.raises(ValidationError):
        ObservationRecord(**{**base, "record_time": datetime(2026, 1, 3)})   # naive
    with pytest.raises(ValidationError):
        ObservationRecord(**{**base, "value_text": "one"})   # both value kinds
    assert record_family(ObservationRecord(**base)) == "observation"
    _ = timezone
