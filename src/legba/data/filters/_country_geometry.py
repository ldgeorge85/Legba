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
import math
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


# ---- nearest coast (lane N-S) ------------------------------------------------
#
# An authoritative point in NO country (an incident at sea, lane N-R) is filed
# under no country; this answers the next question honestly — WHICH coastal
# states it lies off, and how far — from the same bundled 110m outlines, so
# nothing is fetched and nothing is added. The distance is to the 110m
# outline, whose generalisation is itself tens of km along an intricate coast
# (the Musandam peninsula, the Gulf islands), so it is reported to the km but
# is a coarse figure: a relation for routing, never a position.

#: Coastal states within this distance of an at-sea point are recorded as
#: ``payload.geo.inferred``. 200 km spans the Gulf, the Red Sea and the
#: Malacca/Singapore straits from their median lines.
NEAREST_COAST_MAX_KM = 200.0
#: At most this many states, nearest first.
NEAREST_COAST_MAX_STATES = 3

_KM_PER_DEG_LAT = 110.574
_KM_PER_DEG_LON_EQ = 111.320


def _segment_km(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Planar distance from P to segment AB (all in local km)."""
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx, cy = ax + t * dx - px, ay + t * dy - py
    return math.hypot(cx, cy)


def coastal_states_near(
    lat: float, lon: float, *,
    max_km: float = NEAREST_COAST_MAX_KM,
    limit: int = NEAREST_COAST_MAX_STATES,
) -> list[tuple[str, float]]:
    """``[(iso2, distance_km), ...]`` — the states whose 110m outline passes
    within ``max_km`` of ``(lat, lon)``, nearest first, at most ``limit``.

    Local equirectangular projection centred on the point (error well under
    1% inside 200 km), point-to-edge distance over every outer ring, a
    bbox prefilter widened by ``max_km``. Offline, deterministic. A point
    INSIDE a country returns that country at 0 km — callers ask only for
    points that are in none.
    """
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return []
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or limit <= 0:
        return []
    kx = _KM_PER_DEG_LON_EQ * max(math.cos(math.radians(lat)), 1e-6)
    dlat_pad = max_km / _KM_PER_DEG_LAT
    dlon_pad = min(180.0, max_km / kx)
    best: dict[str, float] = {}
    for c in _countries():
        iso2 = c.get("iso2")
        bb = c.get("bbox")
        if not iso2 or iso2 == "-99":
            continue
        if bb and not (bb[1] - dlat_pad <= lat <= bb[3] + dlat_pad
                       and bb[0] - dlon_pad <= lon <= bb[2] + dlon_pad):
            continue
        d_min = math.inf
        for poly in c.get("polys") or []:
            if not poly:
                continue
            if _point_in_polygon(lon, lat, poly):
                d_min = 0.0
                break
            ring = poly[0]
            pts = []
            for v in ring:
                dlon = (float(v[0]) - lon + 540.0) % 360.0 - 180.0
                pts.append((dlon * kx, (float(v[1]) - lat) * _KM_PER_DEG_LAT))
            for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
                d = _segment_km(0.0, 0.0, ax, ay, bx, by)
                if d < d_min:
                    d_min = d
        if d_min <= max_km:
            best[iso2] = d_min
    ranked = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
    return [(iso, round(km, 1)) for iso, km in ranked[:limit]]


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


__all__ = [
    "NEAREST_COAST_MAX_KM",
    "NEAREST_COAST_MAX_STATES",
    "coastal_states_near",
    "country_iso2_for_point",
    "representative_point",
]
