# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4 / cluster C4-enrichment-geo unit tests for defects D5 + D30.

Pure (no-DB, no-network) coverage of:

D5 (geo enrichment ladder):
  * in-body NER place mentions resolve ABOVE the publisher-origin TLD sweep
    (Venezuela 0/141, BBC->{GB} class);
  * the telegram `t.me` (`.me`/Montenegro) origin no longer leaks a {ME} tag;
  * `tld_fallback=False` turns the publisher-origin fallback off entirely;
  * EONET Point geometry reverse-geocodes to a country ISO2 in the geojson
    source path (694/697 empties).

D30 (language normalization):
  * region/script-tagged codes normalize to the ISO-639-1 base (en-US -> en);
  * short ALL-CAPS headlines abstain to `und` (the Amnesty English->de class).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from legba.data.filters.geocode import (
    GeocodeConfig,
    GeocodeHandler,
    GeocodeResult,
    country_iso2_from_tld,
    place_candidates_from_entities,
)
from legba.data.filters.language_detect import (
    LanguageDetectConfig,
    LanguageDetectHandler,
    PAYLOAD_LANGUAGE_KEY,
    UND,
    is_short_allcaps_headline,
    normalize_lang,
)
from legba.data.filters._contract import FilterContext
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Shared stubs / helpers
# ---------------------------------------------------------------------------


class _StubBackend:
    """Deterministic geocode backend keyed on the query string."""

    name = "stub"

    def __init__(self, results: dict[str, GeocodeResult | None]) -> None:
        self.results = dict(results)
        self.calls: list[str] = []

    async def geocode(self, query: str) -> GeocodeResult | None:
        self.calls.append(query)
        return self.results.get(query)

    async def reachable(self) -> bool:
        return True


def _result(country: str, iso2: str, *, precision: str = "country") -> GeocodeResult:
    return GeocodeResult(
        country=country,
        country_iso2=iso2,
        country_iso3=None,
        region=None,
        municipality=None,
        address=None,
        lat=0.0,
        lon=0.0,
        precision=precision,  # type: ignore[arg-type]
        source="stub",
    )


def _ctx() -> FilterContext:
    return FilterContext(
        target_id="t1",
        target_version="v1",
        filter_id="f1",
        logger=logging.getLogger("test.c4.geo"),
    )


def _signal(**payload: Any) -> Signal:
    kwargs: dict[str, Any] = {"source_id": "s1"}
    if "canonical_url" in payload:
        kwargs["canonical_url"] = payload.pop("canonical_url")
    kwargs["payload"] = payload
    return Signal(**kwargs)


class _StaticDetector:
    """Deterministic language detector returning a fixed (lang, conf)."""

    def __init__(self, lang: str, conf: float = 0.99) -> None:
        self._lang = lang
        self._conf = conf
        self.calls: list[str] = []

    @property
    def backend(self) -> str:
        return "static"

    def detect(self, text: str) -> tuple[str, float]:
        self.calls.append(text)
        return self._lang, self._conf


def _lang_ctx() -> FilterContext:
    return FilterContext(
        target_id="t1",
        target_version="v1",
        filter_id="lf1",
        logger=logging.getLogger("test.c4.lang"),
    )


# ===========================================================================
# D5 — place_candidates_from_entities
# ===========================================================================


class TestPlaceCandidatesFromEntities:
    def test_extracts_country_and_location_classes(self):
        ents = [
            {"class": "person", "text": "Nicolas Maduro"},
            {"class": "location", "text": "Caracas"},
            {"class": "country", "text": "Venezuela"},
            {"class": "organization", "text": "PDVSA"},
        ]
        # country-class first, then locations, person/org excluded.
        assert place_candidates_from_entities(ents) == ["Venezuela", "Caracas"]

    def test_dedupes_case_insensitively(self):
        ents = [
            {"class": "location", "text": "Caracas"},
            {"class": "location", "text": "caracas"},
        ]
        assert place_candidates_from_entities(ents) == ["Caracas"]

    def test_ignores_non_list_and_junk(self):
        assert place_candidates_from_entities(None) == []
        assert place_candidates_from_entities("nope") == []
        assert place_candidates_from_entities([{"class": "location"}]) == []  # no text
        assert place_candidates_from_entities([{"text": "X"}]) == []          # no class


# ===========================================================================
# D5 — TLD / publisher-origin demotion
# ===========================================================================


class TestPublisherOriginTld:
    def test_t_me_no_longer_resolves_to_montenegro(self):
        # The root {ME} bug: t.me -> .me -> Montenegro. Now skipped.
        assert country_iso2_from_tld("https://t.me/tassagency_en/123") is None
        assert country_iso2_from_tld("https://www.t.me/guardian/9") is None
        assert country_iso2_from_tld("https://telegram.me/x/1") is None

    def test_social_aggregator_hosts_skipped(self):
        assert country_iso2_from_tld("https://x.com/foo/status/1") is None
        assert country_iso2_from_tld("https://www.youtube.com/watch?v=1") is None

    def test_real_cctld_still_resolves(self):
        assert country_iso2_from_tld("https://uol.com.br/news") == "BR"
        assert country_iso2_from_tld("https://example.co.uk/x") == "GB"


# ===========================================================================
# D5 — the inference ladder
# ===========================================================================


@pytest.mark.asyncio
class TestGeoLadderOrdering:
    async def test_in_body_entity_beats_publisher_origin(self):
        # BBC story ABOUT Venezuela, published on bbc.co.uk (.uk -> GB).
        # The in-body NER entity for Venezuela must win over the GB origin.
        backend = _StubBackend(
            {
                "Venezuela": _result("Venezuela", "VE"),
                "United Kingdom": _result("United Kingdom", "GB"),
            }
        )
        handler = GeocodeHandler(
            GeocodeConfig(precision="country"), backend=backend
        )
        sig = _signal(
            title="Maduro tightens grip",
            entities=[{"class": "country", "text": "Venezuela"}],
            canonical_url="https://www.bbc.co.uk/news/world-123",
        )
        out = await handler.transform(sig, _ctx())
        assert out.payload["geo"]["country_iso2"] == "VE"
        # The GB origin candidate was never even queried (VE resolved first).
        assert backend.calls[0] == "Venezuela"

    async def test_falls_back_to_publisher_origin_when_no_in_body_place(self):
        # No in-body place anywhere -> the WEAK TLD origin is the last resort.
        backend = _StubBackend({"United Kingdom": _result("United Kingdom", "GB")})
        handler = GeocodeHandler(
            GeocodeConfig(precision="country"), backend=backend
        )
        sig = _signal(
            title="Quarterly results released",
            canonical_url="https://example.co.uk/biz/1",
        )
        out = await handler.transform(sig, _ctx())
        assert out.payload["geo"]["country_iso2"] == "GB"

    async def test_tld_fallback_off_leaves_unattributed(self):
        # With tld_fallback off and no in-body place, the signal passes through
        # geo-unattributed (correct) rather than mis-tagged by origin.
        backend = _StubBackend({"United Kingdom": _result("United Kingdom", "GB")})
        handler = GeocodeHandler(
            GeocodeConfig(precision="country", tld_fallback=False),
            backend=backend,
        )
        sig = _signal(
            title="Quarterly results released",
            canonical_url="https://example.co.uk/biz/1",
        )
        out = await handler.transform(sig, _ctx())
        assert "geo" not in (out.payload or {})
        assert backend.calls == []

    async def test_telegram_body_text_resolves_not_montenegro(self):
        # Telegram: content in payload.text, canonical_url is t.me. With t.me
        # skipped + text swept, the body's country wins; no {ME}.
        backend = _StubBackend({"Ukraine": _result("Ukraine", "UA")})
        handler = GeocodeHandler(
            GeocodeConfig(precision="country"), backend=backend
        )
        sig = _signal(
            text="Heavy shelling reported across Ukraine overnight.",
            canonical_url="https://t.me/tassagency_en/4567",
            publisher_origin_nongeo=True,
        )
        out = await handler.transform(sig, _ctx())
        assert out.payload["geo"]["country_iso2"] == "UA"

    async def test_entities_location_resolves_when_no_country_class(self):
        backend = _StubBackend({"Caracas": _result("Venezuela", "VE", precision="municipality")})
        handler = GeocodeHandler(
            GeocodeConfig(precision="municipality"), backend=backend
        )
        sig = _signal(
            text="Protesters gathered downtown.",
            entities=[{"class": "location", "text": "Caracas"}],
            canonical_url="https://t.me/news/1",
        )
        out = await handler.transform(sig, _ctx())
        assert out.payload["geo"]["country_iso2"] == "VE"
        assert backend.calls[0] == "Caracas"


# ===========================================================================
# D5 — EONET geometry reverse-geocode (geojson source path)
# ===========================================================================


class TestEonetGeometryReverseGeocode:
    def test_point_geometry_resolves_to_country(self):
        from legba.data.sources.geojson import _country_from_geometry

        # A point well inside Brazil (Brasilia ~ -15.79, -47.88) -> BR.
        geom = {"type": "Point", "coordinates": [-47.88, -15.79]}
        assert _country_from_geometry(geom) == "BR"

    def test_open_ocean_point_returns_none(self):
        from legba.data.sources.geojson import _country_from_geometry

        # Mid-South-Atlantic — no country (correct: a sea-cyclone has no land).
        geom = {"type": "Point", "coordinates": [-25.0, -30.0]}
        assert _country_from_geometry(geom) is None

    def test_non_geometry_returns_none(self):
        from legba.data.sources.geojson import _country_from_geometry

        assert _country_from_geometry(None) is None
        assert _country_from_geometry({"type": "Point"}) is None  # no coords

    def test_eonet_feature_signal_gets_geo_from_geometry(self):
        # Full feature -> signal path: properties carry NO iso code (EONET
        # shape), so geo must come from the geometry reverse-geocode.
        from legba.data.sources.geojson import GeoJSONConfig, GeoJSONSourceHandler

        handler = GeoJSONSourceHandler(GeoJSONConfig(url="https://eonet.example/x"))
        feature = {
            "type": "Feature",
            "id": "EONET_1234",
            "geometry": {"type": "Point", "coordinates": [-47.88, -15.79]},
            "properties": {"title": "Wildfire - Mato Grosso", "categories": ["wildfires"]},
        }

        class _Ctx:
            source_id = "src.eonet"
            logger = logging.getLogger("test.eonet")

        sig = handler._feature_to_signal(feature, ctx=_Ctx())
        assert sig is not None
        assert sig.geo == ["BR"]

    def test_explicit_iso_in_properties_still_wins(self):
        # If a feed DOES carry an iso code, we use it (no geometry override).
        from legba.data.sources.geojson import GeoJSONConfig, GeoJSONSourceHandler

        handler = GeoJSONSourceHandler(GeoJSONConfig(url="https://x/y"))
        feature = {
            "type": "Feature",
            "id": "f1",
            "geometry": {"type": "Point", "coordinates": [-47.88, -15.79]},
            "properties": {"title": "x", "iso_a2": "AR"},
        }

        class _Ctx:
            source_id = "src.x"
            logger = logging.getLogger("test.x")

        sig = handler._feature_to_signal(feature, ctx=_Ctx())
        assert sig is not None
        # Properties iso (AR) wins; geometry (BR) is NOT consulted.
        assert "AR" in sig.geo
        assert "BR" not in sig.geo


# ===========================================================================
# D30 — normalize_lang
# ===========================================================================


class TestNormalizeLang:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("en-US", "en"),
            ("en_US", "en"),
            ("pt-BR", "pt"),
            ("zh-Hans", "zh"),
            ("zh-CN", "zh"),
            ("EN", "en"),
            ("  fr-FR  ", "fr"),
            ("es", "es"),
            ("und", "und"),
            ("", "und"),
            ("unknown", "und"),
            (None, "und"),
            (123, "und"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_lang(raw) == expected


class TestShortAllCapsGuard:
    def test_flags_short_allcaps(self):
        assert is_short_allcaps_headline("URGENT ACTION: FREE THE PRISONER") is True
        assert is_short_allcaps_headline("BREAKING NEWS") is True

    def test_not_flagged_when_long(self):
        long_caps = "URGENT ACTION " * 5  # > 40 chars
        assert is_short_allcaps_headline(long_caps) is False

    def test_not_flagged_when_mixed_case(self):
        assert is_short_allcaps_headline("Breaking News Today") is False

    def test_not_flagged_without_two_letters(self):
        assert is_short_allcaps_headline("123 45") is False
        assert is_short_allcaps_headline("") is False


# ===========================================================================
# D30 — handler integration
# ===========================================================================


@pytest.mark.asyncio
class TestLanguageDetectNormalization:
    async def test_existing_region_tag_normalized_without_redetect(self):
        # Source stamped `en-US`; handler must normalize to `en` on the
        # idempotent path (no detector call needed).
        det = _StaticDetector("should-not-be-called")
        handler = LanguageDetectHandler(LanguageDetectConfig(), detector=det)
        sig = Signal(
            source_id="s1",
            payload={PAYLOAD_LANGUAGE_KEY: "en-US", "title": "x"},
        )
        out = await handler.transform(sig, _lang_ctx())
        assert out.payload[PAYLOAD_LANGUAGE_KEY] == "en"
        assert out.language_hint == "en"
        assert det.calls == []  # no re-detect

    async def test_existing_base_code_is_fast_noop(self):
        det = _StaticDetector("nope")
        handler = LanguageDetectHandler(LanguageDetectConfig(), detector=det)
        sig = Signal(
            source_id="s1",
            payload={PAYLOAD_LANGUAGE_KEY: "en", "title": "x"},
        )
        out = await handler.transform(sig, _lang_ctx())
        assert out.payload[PAYLOAD_LANGUAGE_KEY] == "en"
        assert det.calls == []

    async def test_detected_region_tag_normalized(self):
        # A detector returning a region-tagged code is normalized at the stamp.
        det = _StaticDetector("pt-BR", conf=0.99)
        handler = LanguageDetectHandler(
            LanguageDetectConfig(min_text_length=5), detector=det
        )
        sig = Signal(
            source_id="s1",
            payload={"text": "Reportagem completa sobre a economia brasileira hoje."},
        )
        out = await handler.transform(sig, _lang_ctx())
        assert out.payload[PAYLOAD_LANGUAGE_KEY] == "pt"

    async def test_short_allcaps_headline_abstains_to_und(self):
        # The Amnesty class: short ALL-CAPS English headline; the detector would
        # confidently say `de`, but the guard abstains to `und`.
        det = _StaticDetector("de", conf=0.99)
        handler = LanguageDetectHandler(
            LanguageDetectConfig(min_text_length=5), detector=det
        )
        sig = Signal(
            source_id="s1",
            payload={"title": "URGENT ACTION: RELEASE DETAINEE"},
        )
        out = await handler.transform(sig, _lang_ctx())
        assert out.payload[PAYLOAD_LANGUAGE_KEY] == UND
        assert det.calls == []  # guard fired before the detector

    async def test_normal_sentence_still_detected(self):
        det = _StaticDetector("en", conf=0.99)
        handler = LanguageDetectHandler(
            LanguageDetectConfig(min_text_length=5), detector=det
        )
        sig = Signal(
            source_id="s1",
            payload={"title": "The council approved the budget after a long debate."},
        )
        out = await handler.transform(sig, _lang_ctx())
        assert out.payload[PAYLOAD_LANGUAGE_KEY] == "en"
        assert det.calls  # detector ran
