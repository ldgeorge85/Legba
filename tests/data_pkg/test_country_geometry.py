# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ-C1 — offline point-in-country resolver + payload geometry extraction.

The geo feeds (NWS active_alerts = ~63% of signal) ship an authoritative
coordinate but the geocode filter text-geocoded the title, mis-attributing every
US alert to a constant bogus France/Armenia coordinate. These tests lock the
offline resolver that fixes it.
"""
from __future__ import annotations

from legba.data.filters._country_geometry import (
    country_iso2_for_point,
    representative_point,
)


class TestPointInCountry:
    def test_us_points_resolve_us(self):
        # The exact NWS Wichita KS example that resolved to Armenia.
        assert country_iso2_for_point(37.51, -97.35) == "US"
        assert country_iso2_for_point(30.8, -87.61) == "US"   # Mobile AL
        assert country_iso2_for_point(38.9, -77.04) == "US"   # Washington DC

    def test_known_foreign_points(self):
        assert country_iso2_for_point(48.85, 2.35) == "FR"    # Paris
        assert country_iso2_for_point(35.69, 51.39) == "IR"   # Tehran
        assert country_iso2_for_point(35.69, 139.69) == "JP"  # Tokyo
        assert country_iso2_for_point(-23.55, -46.63) == "BR" # Sao Paulo

    def test_open_ocean_is_none(self):
        assert country_iso2_for_point(0.0, -30.0) is None     # mid-Atlantic
        assert country_iso2_for_point(-60.0, -140.0) is None  # S Pacific

    def test_out_of_range_is_none(self):
        assert country_iso2_for_point(200.0, 0.0) is None
        assert country_iso2_for_point(None, None) is None      # type: ignore[arg-type]
        assert country_iso2_for_point(10.0, "x") is None       # type: ignore[arg-type]


class TestRepresentativePoint:
    def test_nws_polygon_geojson(self):
        payload = {"geojson": {"geometry": {"type": "Polygon", "coordinates": [
            [[-97.35, 37.51], [-97.0, 37.51], [-97.0, 37.8], [-97.35, 37.8], [-97.35, 37.51]]
        ]}}}
        pt = representative_point(payload)
        assert pt is not None
        assert country_iso2_for_point(*pt) == "US"

    def test_nasa_point_geometry(self):
        # GeoJSON [lon, lat] -> (lat, lon)
        assert representative_point(
            {"geometry": {"type": "Point", "coordinates": [139.69, 35.69]}}
        ) == (35.69, 139.69)

    def test_multipolygon_first_ring(self):
        payload = {"geometry": {"type": "MultiPolygon", "coordinates": [
            [[[2.0, 48.0], [2.5, 48.0], [2.5, 48.5], [2.0, 48.5], [2.0, 48.0]]]
        ]}}
        pt = representative_point(payload)
        assert pt is not None and country_iso2_for_point(*pt) == "FR"

    def test_explicit_geo_latlon(self):
        assert representative_point({"geo": {"lat": 35.69, "lon": 51.39}}) == (35.69, 51.39)

    def test_no_geometry_returns_none(self):
        assert representative_point({"title": "Some text-only article"}) is None
        assert representative_point({}) is None
        assert representative_point({"geo": {"location_name": "Paris"}}) is None
