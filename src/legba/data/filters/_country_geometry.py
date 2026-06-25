# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline point-in-country resolver (DQ-C1).

Geospatial feeds (NWS active_alerts, USGS, GDACS, NASA EONET) ship an
authoritative coordinate/geometry in their payload, but the geocode filter only
ever text-geocoded the *title* — so e.g. every US NWS alert resolved to a
constant bogus France/Armenia coordinate (a noise-token forward-Nominatim
match), corrupting ~63% of geo-attributed signal. The fix is to attribute
country from the structured geometry FIRST.

This module does that fully offline (no network, no new deps): a Natural-Earth
110m admin-0 boundary set (``world_admin0.min.json``, ISO2 + ring geometry +
bbox, generated from the UI's world.geojson) + a pure-python bbox-prefiltered
ray-casting point-in-polygon (holes honored). Country-precision is enough for
attribution; the source's exact lat/lon is preserved separately.
"""
from __future__ import annotations

import json
import pathlib
from functools import lru_cache
from typing import Any

_DATA_PATH = pathlib.Path(__file__).resolve().parent / "world_admin0.min.json"


@lru_cache(maxsize=1)
def _countries() -> list[dict[str, Any]]:
    """Lazy-load the bundled admin-0 boundaries. Empty list if missing."""
    try:
        with _DATA_PATH.open("r", encoding="utf-8") as fh:
            return list(json.load(fh).get("countries") or [])
    except Exception:  # pragma: no cover - the asset is bundled; defensive only
        return []


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Standard even-odd ray-casting test for a point in a single ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        # Does the horizontal ray at `lat` cross edge (i, j)?
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _point_in_polygon(lon: float, lat: float, rings: list[list[list[float]]]) -> bool:
    """A GeoJSON polygon = [outer_ring, hole1, hole2, ...]. Inside iff inside the
    outer ring and NOT inside any hole."""
    if not rings:
        return False
    if not _point_in_ring(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if _point_in_ring(lon, lat, hole):
            return False
    return True


def country_iso2_for_point(lat: float, lon: float) -> str | None:
    """Resolve the ISO-3166-1 alpha-2 country containing ``(lat, lon)``, or
    ``None`` (open ocean / unmatched). Offline, deterministic."""
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    for c in _countries():
        bb = c.get("bbox")
        if bb and not (bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]):
            continue
        for poly in c.get("polys") or []:
            if _point_in_polygon(lon, lat, poly):
                return c.get("iso2")
    return None


# ---- representative-point extraction from a heterogeneous payload -----------

def _ring_centroid(ring: list[list[float]]) -> tuple[float, float] | None:
    """Mean of a ring's vertices (lon, lat). For a small alert/event zone this
    lands inside the issuing country, which is all we need for attribution."""
    if not ring:
        return None
    sx = sy = 0.0
    n = 0
    for pt in ring:
        try:
            sx += float(pt[0])
            sy += float(pt[1])
            n += 1
        except (TypeError, ValueError, IndexError):
            continue
    if n == 0:
        return None
    return (sx / n, sy / n)


def _point_from_geometry(geom: Any) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` from a GeoJSON geometry dict, or None."""
    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if coords is None:
        return None
    try:
        if gtype == "Point":
            return (float(coords[1]), float(coords[0]))
        if gtype in ("MultiPoint", "LineString"):
            return (float(coords[0][1]), float(coords[0][0]))
        if gtype == "Polygon":
            c = _ring_centroid(coords[0])
            return (c[1], c[0]) if c else None
        if gtype == "MultiPolygon":
            # Centroid of the first polygon's outer ring.
            c = _ring_centroid(coords[0][0])
            return (c[1], c[0]) if c else None
    except (TypeError, ValueError, IndexError):
        return None
    return None


def representative_point(payload: dict[str, Any]) -> tuple[float, float] | None:
    """Best-effort ``(lat, lon)`` from a signal payload's structured geometry.

    Checks, in order, the locations the live feeds actually use:
      * ``payload.geojson.geometry`` (NWS active_alerts),
      * ``payload.geometry`` (GeoJSON-native adapters / NASA EONET),
      * ``payload.geo.geometry``,
      * an explicit numeric ``payload.geo.lat`` / ``lon`` pair.
    Returns None when no structured coordinate is present (text geocode then
    runs as before).
    """
    if not isinstance(payload, dict):
        return None
    for path in (("geojson", "geometry"), ("geometry",), ("geo", "geometry")):
        node: Any = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        pt = _point_from_geometry(node)
        if pt is not None:
            return pt
    geo = payload.get("geo")
    if isinstance(geo, dict):
        lat, lon = geo.get("lat"), geo.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return (float(lat), float(lon))
        # GeoJSON-style [lon, lat] under geo.coordinates
        coords = geo.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) == 2:
            try:
                return (float(coords[1]), float(coords[0]))
            except (TypeError, ValueError):
                return None
    return None


__all__ = ["country_iso2_for_point", "representative_point"]
