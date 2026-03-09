"""
Location Normalization & Geocoding

Resolves free-text location strings to structured geo data using:
- pycountry for country-level resolution (offline, fast)
- GeoNames cities15000 gazetteer for city-level resolution (bundled in image)

Called automatically at event_store time to enrich events with
geo_countries, geo_regions, and geo_coordinates.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# GeoNames gazetteer data (loaded lazily on first use)
_cities: dict[str, dict[str, Any]] | None = None
_GEONAMES_PATH = Path("/data/geo/cities15000.txt")

# Country name aliases not covered by pycountry
_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united states of america": "US",
    "uk": "GB", "u.k.": "GB", "britain": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB",
    "russia": "RU", "soviet union": "RU",
    "south korea": "KR", "north korea": "KP",
    "taiwan": "TW", "ivory coast": "CI", "czech republic": "CZ",
    "vatican": "VA", "vatican city": "VA",
    "palestine": "PS", "congo": "CD", "drc": "CD",
    "uae": "AE", "middle east": "",  # region, not country
    "european union": "", "eu": "",   # org, not country
    "east africa": "", "west africa": "", "central asia": "",
    "southeast asia": "", "latin america": "", "sub-saharan africa": "",
}


def _load_cities() -> dict[str, dict[str, Any]]:
    """Load GeoNames cities15000 gazetteer. Keyed by lowercase city name."""
    global _cities
    if _cities is not None:
        return _cities

    _cities = {}
    if not _GEONAMES_PATH.exists():
        logger.warning("GeoNames gazetteer not found at %s", _GEONAMES_PATH)
        return _cities

    try:
        with open(_GEONAMES_PATH, "r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="\t")
            for row in reader:
                if len(row) < 15:
                    continue
                name = row[1]  # asciiname
                alt_names = row[3]  # alternatenames
                lat = float(row[4])
                lon = float(row[5])
                country_code = row[8]
                admin1 = row[10]  # admin1 code (state/province)
                population = int(row[14]) if row[14] else 0

                entry = {
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "country_code": country_code,
                    "admin1": admin1,
                    "population": population,
                }

                key = name.lower()
                # Keep the more populous city if duplicate names
                if key not in _cities or population > _cities[key]["population"]:
                    _cities[key] = entry

                # Also index alternate names (first 5 to avoid bloat)
                for alt in alt_names.split(",")[:5]:
                    alt_key = alt.strip().lower()
                    if alt_key and alt_key not in _cities:
                        _cities[alt_key] = entry

        logger.info("Loaded %d city entries from GeoNames", len(_cities))
    except Exception as e:
        logger.error("Failed to load GeoNames gazetteer: %s", e)
        _cities = {}

    return _cities


def _resolve_country(location: str) -> str | None:
    """Try to resolve a location string to an ISO country code."""
    low = location.lower().strip()

    # Check aliases first
    if low in _COUNTRY_ALIASES:
        code = _COUNTRY_ALIASES[low]
        return code if code else None  # empty string = known region, not country

    try:
        import pycountry

        # Exact name match
        country = pycountry.countries.get(name=location)
        if country:
            return country.alpha_2

        # Official name match
        country = pycountry.countries.get(official_name=location)
        if country:
            return country.alpha_2

        # Fuzzy search
        results = pycountry.countries.search_fuzzy(location)
        if results:
            return results[0].alpha_2
    except (LookupError, Exception):
        pass

    return None


def resolve_locations(locations: list[str]) -> dict[str, Any]:
    """
    Resolve a list of free-text location strings to structured geo data.

    Returns:
        {
            "countries": ["US", "GB", ...],       # ISO codes (deduplicated)
            "regions": ["California", ...],        # admin regions
            "coordinates": [{"name": ..., "lat": ..., "lon": ...}, ...]
        }
    """
    countries: list[str] = []
    regions: list[str] = []
    coordinates: list[dict] = []
    seen_countries: set[str] = set()
    cities = _load_cities()

    for loc in locations:
        loc_stripped = loc.strip()
        if not loc_stripped:
            continue

        # Try city lookup first (more specific)
        city_key = loc_stripped.lower()
        city = cities.get(city_key)
        if city:
            cc = city["country_code"]
            if cc and cc not in seen_countries:
                countries.append(cc)
                seen_countries.add(cc)
            if city["admin1"]:
                regions.append(city["admin1"])
            coordinates.append({
                "name": loc_stripped,
                "lat": round(city["lat"], 4),
                "lon": round(city["lon"], 4),
            })
            continue

        # Try country-level resolution
        cc = _resolve_country(loc_stripped)
        if cc and cc not in seen_countries:
            countries.append(cc)
            seen_countries.add(cc)

    return {
        "countries": countries,
        "regions": regions,
        "coordinates": coordinates,
    }
