# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for :mod:`legba.data.filters.geocode` (L-153).

These tests exercise the filter handler against an injected stub backend
so they're hermetic (no live Nominatim, no Redis). One marker-gated
integration test against public Nominatim runs only if
``LEGBA_NOMINATIM_LIVE_TEST=1`` is set in the environment — the OSM
usage policy makes hammering it from CI a non-starter.

Coverage:

  * country-name + ISO code extraction from text;
  * TLD → ISO2 fallback;
  * inference precedence (geo.location_name > title > raw_body > TLD);
  * idempotency when ``signal.payload["geo"]`` already populated;
  * precision truncation (country / region / municipality / address);
  * Redis-style cache get-or-set + negative cache;
  * health check happy + unreachable paths;
  * config validation (extra fields rejected; precision enum enforced).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest
from pydantic import ValidationError

from legba.data.filters._contract import FilterContext, FilterHealth
from legba.data.filters.geocode import (
    GeocodeConfig,
    GeocodeHandler,
    GeocodeResult,
    _InMemoryCache,
    country_iso2_from_tld,
    extract_country_iso2_from_text,
)
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Stub backend
# ---------------------------------------------------------------------------


class StubBackend:
    """Deterministic backend for unit tests.

    Construct with a ``{query → GeocodeResult or None}`` dict. Records
    every call in ``self.calls`` so tests can assert cache + dedupe
    behavior.
    """

    name = "stub"

    def __init__(
        self,
        results: dict[str, GeocodeResult | None] | None = None,
        *,
        reachable: bool = True,
        raise_on: set[str] | None = None,
    ) -> None:
        self.results: dict[str, GeocodeResult | None] = dict(results or {})
        self.calls: list[str] = []
        self._reachable = reachable
        self._raise_on = raise_on or set()

    async def geocode(self, query: str) -> GeocodeResult | None:
        self.calls.append(query)
        if query in self._raise_on:
            raise RuntimeError(f"stub backend raised on {query!r}")
        return self.results.get(query)

    async def reachable(self) -> bool:
        return self._reachable


def _make_result(
    *,
    country: str = "Brazil",
    iso2: str = "BR",
    iso3: str = "BRA",
    region: str | None = "Sao Paulo",
    municipality: str | None = "Sao Paulo",
    lat: float = -23.55,
    lon: float = -46.63,
    precision: str = "municipality",
    source: str = "stub",
    address: str | None = "Sao Paulo, Brazil",
) -> GeocodeResult:
    return GeocodeResult(
        country=country,
        country_iso2=iso2,
        country_iso3=iso3,
        region=region,
        municipality=municipality,
        address=address,
        lat=lat,
        lon=lon,
        precision=precision,                                     # type: ignore[arg-type]
        source=source,
    )


def _ctx(**overrides: Any) -> FilterContext:
    base: dict[str, Any] = dict(
        target_id="t1",
        target_version="v1",
        filter_id="f1",
        logger=logging.getLogger("test.legba.filter.geocode"),
    )
    base.update(overrides)
    return FilterContext(**base)


def _signal(**payload: Any) -> Signal:
    # Source-first pivot: Signal is source-owned; target_id was dropped from
    # the model (lives only on derived analyst outputs). See PIVOT_BUILD_PLAN.
    sig_kwargs: dict[str, Any] = {"source_id": "s1"}
    if "canonical_url" in payload:
        sig_kwargs["canonical_url"] = payload.pop("canonical_url")
    sig_kwargs["payload"] = payload
    return Signal(**sig_kwargs)


# ---------------------------------------------------------------------------
# Country extraction
# ---------------------------------------------------------------------------


class TestCountryExtraction:
    def test_extracts_canonical_name(self):
        assert extract_country_iso2_from_text("Protests in Brazil today") == "BR"

    def test_extracts_common_alias(self):
        # "russia" is an alias for "Russian Federation" in our index.
        assert extract_country_iso2_from_text("Russia announces sanctions") == "RU"

    def test_case_insensitive(self):
        assert extract_country_iso2_from_text("brazil and Mexico") == "BR"

    def test_no_partial_match(self):
        # "Cuban" should not match "Cuba" as a substring; whole-word only.
        # (We use a negative-lookahead boundary.)
        assert extract_country_iso2_from_text("Cuban cigars") is None

    def test_iso2_uppercase_token(self):
        # Uppercase 2-letter token matches an ISO2 code.
        assert extract_country_iso2_from_text("Activity in BR today") == "BR"

    def test_iso3_uppercase_token(self):
        assert extract_country_iso2_from_text("Reports from BRA jurisdiction") == "BR"

    def test_lowercase_iso2_token_does_not_match(self):
        # "at" must not match Austria; only uppercase ISO codes accepted.
        assert extract_country_iso2_from_text("This is at home") is None

    def test_empty_input(self):
        assert extract_country_iso2_from_text("") is None
        assert extract_country_iso2_from_text("\n  \t") is None

    def test_uk_alias(self):
        assert extract_country_iso2_from_text("Reports in the UK") == "GB"

    def test_us_state_code_not_a_country(self):
        # DQ-C1: trailing US state codes in NWS/USGS titles must NOT resolve to a
        # collision country (AL->Albania, PA->Panama, MT->Malta).
        assert extract_country_iso2_from_text("Flood Warning issued by NWS Mobile AL") is None
        assert extract_country_iso2_from_text("Winter Storm Warning State College PA") is None
        assert extract_country_iso2_from_text("Red Flag Warning Great Falls MT") is None

    def test_timezone_abbrev_not_a_country(self):
        # DQ-C1: timezone tokens must not resolve (EST->Estonia, PT->Portugal).
        assert extract_country_iso2_from_text("Effective until 3:00 PM EST") is None
        assert extract_country_iso2_from_text("Advisory until 6:00 AM PT") is None

    def test_real_country_token_still_resolves(self):
        # Non-state/-tz ISO tokens still work (BR not in the stop-set).
        assert extract_country_iso2_from_text("Activity in BR today") == "BR"


# ---------------------------------------------------------------------------
# TLD fallback
# ---------------------------------------------------------------------------


class TestTldFallback:
    def test_simple_cctld_br(self):
        assert country_iso2_from_tld("https://uol.com.br/path") == "BR"

    def test_two_part_cctld(self):
        assert country_iso2_from_tld("https://example.fr/") == "FR"

    def test_generic_gtld_returns_none(self):
        assert country_iso2_from_tld("https://example.com/path") is None

    def test_uk_override(self):
        assert country_iso2_from_tld("https://bbc.co.uk/news") == "GB"

    def test_naked_host(self):
        assert country_iso2_from_tld("http://news.example.de") == "DE"

    def test_missing_url(self):
        assert country_iso2_from_tld(None) is None
        assert country_iso2_from_tld("") is None

    def test_single_label(self):
        assert country_iso2_from_tld("http://localhost") is None

    def test_invalid_url_returns_none(self):
        # Strings without scheme but with dots still get parsed via netloc/path.
        assert country_iso2_from_tld("not a url") is None


# ---------------------------------------------------------------------------
# transform — idempotency + inference precedence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transform_skips_when_geo_complete():
    backend = StubBackend()
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    sig = _signal(
        title="Some news",
        geo={"country": "Brazil", "lat": -23.55, "lon": -46.63},
    )
    out = await handler.transform(sig, _ctx())
    assert out is sig
    assert backend.calls == []


@pytest.mark.asyncio
async def test_transform_uses_location_name_first():
    backend = StubBackend(results={"Rio de Janeiro": _make_result(municipality="Rio de Janeiro")})
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    sig = _signal(
        title="Brazil protests escalate",
        geo={"location_name": "Rio de Janeiro"},
    )
    out = await handler.transform(sig, _ctx())
    assert out is not None and out is not sig                    # mutated copy
    geo = out.payload["geo"]
    assert geo["country_iso2"] == "BR"
    assert geo["municipality"] == "Rio de Janeiro"
    # location_name hint preserved via merge.
    assert geo["location_name"] == "Rio de Janeiro"
    assert backend.calls == ["Rio de Janeiro"]


@pytest.mark.asyncio
async def test_transform_falls_back_to_title():
    backend = StubBackend(results={"Brazil": _make_result(precision="country")})
    handler = GeocodeHandler(
        GeocodeConfig(precision="country"),
        backend=backend,
    )
    sig = _signal(title="Brazil protests escalate")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    geo = out.payload["geo"]
    assert geo["country_iso2"] == "BR"
    assert geo["precision"] == "country"
    assert backend.calls == ["Brazil"]


@pytest.mark.asyncio
async def test_transform_falls_back_to_raw_body_when_title_empty():
    backend = StubBackend(results={"Brazil": _make_result()})
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    sig = _signal(title="", raw_body="A report from Brazil about energy.")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["geo"]["country_iso2"] == "BR"


@pytest.mark.asyncio
async def test_transform_tld_fallback_used_when_text_misses():
    backend = StubBackend(results={"Brazil": _make_result()})
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    sig = Signal(
        source_id="s",
        canonical_url="https://uol.com.br/article",
        payload={"title": "Energy news", "raw_body": "No country names here."},
    )
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["geo"]["country_iso2"] == "BR"
    assert backend.calls == ["Brazil"]


@pytest.mark.asyncio
async def test_transform_returns_signal_unchanged_when_unresolved():
    backend = StubBackend()                                       # no results
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    sig = _signal(title="purely abstract content", raw_body="no geo hints")
    out = await handler.transform(sig, _ctx())
    assert out is sig
    assert "geo" not in (sig.payload or {})


@pytest.mark.asyncio
async def test_transform_first_candidate_wins_subsequent_skipped():
    backend = StubBackend(results={
        "Brazil": _make_result(),
        "Mexico": _make_result(country="Mexico", iso2="MX", iso3="MEX",
                               municipality="Mexico City", region="CDMX",
                               address="Mexico City, Mexico"),
    })
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    sig = _signal(
        title="Brazil",
        raw_body="A piece about Mexico City",
    )
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["geo"]["country_iso2"] == "BR"
    # Only the first (title-derived) candidate hits the backend.
    assert backend.calls == ["Brazil"]


@pytest.mark.asyncio
async def test_transform_skips_field_when_infer_from_omits_it():
    backend = StubBackend(results={"Brazil": _make_result()})
    handler = GeocodeHandler(
        GeocodeConfig(infer_from=["raw_body"]),
        backend=backend,
    )
    sig = _signal(title="Brazil protests", raw_body="Generic content")
    out = await handler.transform(sig, _ctx())
    # Title was the only Brazil mention but infer_from excluded it; no resolution.
    assert out is sig
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Precision truncation
# ---------------------------------------------------------------------------


def test_precision_truncation_country():
    r = _make_result()
    out = r.to_payload("country")
    assert out["country_iso2"] == "BR"
    assert out["precision"] == "country"
    assert "region" not in out
    assert "municipality" not in out
    assert "address" not in out


def test_precision_truncation_region():
    r = _make_result()
    out = r.to_payload("region")
    assert out["region"] == "Sao Paulo"
    assert out["precision"] == "region"
    assert "municipality" not in out
    assert "address" not in out


def test_precision_target_higher_than_resolved_demotes():
    # Resolved only to country level; target asks for municipality.
    r = _make_result(precision="country", region=None, municipality=None, address=None)
    out = r.to_payload("municipality")
    assert out["precision"] == "country"           # truncated to what we have
    assert "municipality" not in out


def test_precision_address():
    r = _make_result(precision="address",
                     address="Rua das Flores 123, Sao Paulo, Brazil")
    out = r.to_payload("address")
    assert out["address"] == "Rua das Flores 123, Sao Paulo, Brazil"
    assert out["precision"] == "address"


# ---------------------------------------------------------------------------
# GeocodeResult serialization
# ---------------------------------------------------------------------------


def test_geocode_result_round_trip_json():
    r = _make_result()
    encoded = r.to_json()
    decoded = GeocodeResult.from_json(encoded)
    assert decoded == r


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_does_not_call_backend_twice():
    backend = StubBackend(results={"Brazil": _make_result()})
    cache = _InMemoryCache()
    handler = GeocodeHandler(GeocodeConfig(), backend=backend, cache=cache)

    sig1 = _signal(title="Brazil 1")
    sig2 = _signal(title="Brazil 2")
    out1 = await handler.transform(sig1, _ctx())
    out2 = await handler.transform(sig2, _ctx())
    assert out1 is not None and out2 is not None
    assert out1.payload["geo"]["country_iso2"] == "BR"
    assert out2.payload["geo"]["country_iso2"] == "BR"
    # Backend called exactly once thanks to the cache.
    assert backend.calls == ["Brazil"]


@pytest.mark.asyncio
async def test_negative_cache_short_circuits_repeat_misses():
    backend = StubBackend()                                       # always None
    cache = _InMemoryCache()
    handler = GeocodeHandler(GeocodeConfig(), backend=backend, cache=cache)
    sig1 = _signal(title="Brazil")
    sig2 = _signal(title="Brazil")
    await handler.transform(sig1, _ctx())
    await handler.transform(sig2, _ctx())
    # First call hit the backend; the second was answered from the
    # negative-result sentinel.
    assert backend.calls == ["Brazil"]


@pytest.mark.asyncio
async def test_cache_key_namespacing_isolates_backend_and_precision():
    cache = _InMemoryCache()
    backend = StubBackend(results={"Brazil": _make_result()})
    h1 = GeocodeHandler(GeocodeConfig(precision="country"), backend=backend, cache=cache)
    h2 = GeocodeHandler(GeocodeConfig(precision="municipality"), backend=backend, cache=cache)

    await h1.transform(_signal(title="Brazil"), _ctx())
    await h2.transform(_signal(title="Brazil"), _ctx())
    # Different precisions → different cache keys → two backend calls.
    assert backend.calls == ["Brazil", "Brazil"]


# ---------------------------------------------------------------------------
# Transient backend failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_backend_error_does_not_drop_signal():
    backend = StubBackend(raise_on={"Brazil"})
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    sig = _signal(title="Brazil protests")
    out = await handler.transform(sig, _ctx())
    # Signal preserved even when backend raised on the only candidate.
    assert out is sig
    assert "geo" not in (sig.payload or {})


@pytest.mark.asyncio
async def test_transient_error_does_not_block_next_candidate():
    backend = StubBackend(
        results={"Mexico": _make_result(country="Mexico", iso2="MX", iso3="MEX")},
        raise_on={"Brazil"},
    )
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    # Brazil from title raises; raw_body has Mexico hint as fallback.
    sig = _signal(title="Brazil", raw_body="Reports from Mexico City")
    out = await handler.transform(sig, _ctx())
    assert out is not None
    assert out.payload["geo"]["country_iso2"] == "MX"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_healthy():
    backend = StubBackend(results={"Brazil": _make_result()})
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    health = await handler.health_check(_ctx())
    assert isinstance(health, FilterHealth)
    assert health.state == "healthy"
    assert health.detail["backend"] == "stub"
    assert health.detail["fresh_or_cached_query_ok"] is True


@pytest.mark.asyncio
async def test_health_check_unhealthy_when_backend_unreachable():
    backend = StubBackend(reachable=False)
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    health = await handler.health_check(_ctx())
    assert health.state == "unhealthy"
    assert health.detail["reachable"] is False


@pytest.mark.asyncio
async def test_health_check_degraded_when_sample_query_raises():
    backend = StubBackend(raise_on={"Brazil"})
    handler = GeocodeHandler(GeocodeConfig(), backend=backend)
    health = await handler.health_check(_ctx())
    assert health.state == "degraded"
    assert "sample query failed" in (health.last_error or "")


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self):
        cfg = GeocodeConfig()
        assert cfg.backend == "nominatim"
        assert cfg.precision == "municipality"
        assert cfg.cache_ttl_seconds == 86_400
        # D5: in-body NER entities + chat-body `text` are now in the default
        # ladder, ABOVE the (weak) publisher-origin TLD fallback.
        assert cfg.infer_from == ["geo", "entities", "title", "text", "raw_body"]
        assert cfg.tld_fallback is True

    def test_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            GeocodeConfig(unknown_field=1)  # type: ignore[call-arg]

    def test_rejects_invalid_backend(self):
        with pytest.raises(ValidationError):
            GeocodeConfig(backend="bing")   # type: ignore[arg-type]

    def test_rejects_invalid_precision(self):
        with pytest.raises(ValidationError):
            GeocodeConfig(precision="continent")   # type: ignore[arg-type]

    def test_cache_ttl_bounds(self):
        with pytest.raises(ValidationError):
            GeocodeConfig(cache_ttl_seconds=-1)
        # Upper bound: 30 days * 86400.
        with pytest.raises(ValidationError):
            GeocodeConfig(cache_ttl_seconds=30 * 86_400 + 1)

    def test_infer_from_must_be_nonempty(self):
        with pytest.raises(ValidationError):
            GeocodeConfig(infer_from=[])


# ---------------------------------------------------------------------------
# Handler classvars (L-102 §1 conformance)
# ---------------------------------------------------------------------------


def test_handler_classvars_match_contract():
    assert GeocodeHandler.kind == "geocode"
    assert GeocodeHandler.family == "filter"
    assert GeocodeHandler.schema_version.startswith("legba/filter.geocode/")
    assert GeocodeHandler.config_schema is GeocodeConfig
    # output_contract surface — exercised by registry-composition checks.
    assert "payload.geo" in GeocodeHandler.output_contract
    assert GeocodeHandler.output_contract["payload.geo"] is dict


# ---------------------------------------------------------------------------
# Optional integration — public Nominatim (skipped unless env opted in)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("LEGBA_NOMINATIM_LIVE_TEST"),
    reason="public Nominatim hits skipped (set LEGBA_NOMINATIM_LIVE_TEST=1).",
)
async def test_nominatim_live_brazil():
    from legba.data.filters.geocode import NominatimBackend, resolve_user_agent
    # B-3: live opt-in run requires LEGBA_GEOCODER_CONTACT_EMAIL — the
    # public-endpoint UA must carry a real operator contact (OSM policy);
    # resolve_user_agent raises if it's unset or a placeholder.
    backend = NominatimBackend(
        user_agent=resolve_user_agent(None, nominatim_url=None)
    )
    result = await backend.geocode("Brazil")
    assert result is not None
    assert result.country_iso2 == "BR"
    assert result.country_iso3 == "BRA"
    assert result.lat is not None and result.lon is not None
