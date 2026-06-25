# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity-geo resolution (graph-and-data Wave-1b, item 1).

The headline acceptance: a location entity named "Evian" mentioned in an
India-datelined signal must resolve toward France (when a geocoder is wired) or
stay geo-NULL (no gazetteer) — NEVER the mentioning signal's country (India).
The bug was that location entities inherited their first signal's geocode, then
the composite key LOCKED that wrong value.

Pure-unit (hermetic, no network, no DB): exercises
``_entity_geo.resolve_entity_geo[_offline]`` directly.
"""

from __future__ import annotations

import pytest

from legba.data.analysts.deterministic_handlers._entity_geo import (
    EntityGeo,
    resolve_entity_geo,
    resolve_entity_geo_offline,
)


# A signal datelined in India (the conflating context).
_INDIA_GEO = {"lat": 28.61, "lon": 77.21, "country": "India"}


def test_evian_never_inherits_signal_country_offline():
    """The core bug: "Evian" (a French town) seen in an India signal must NOT
    take India. Offline we can't resolve the town → geo stays NULL."""
    geo = resolve_entity_geo_offline(
        name="Evian", entity_class="location", signal_geo=_INDIA_GEO
    )
    assert geo.country is None, "non-country place must not inherit signal country"
    assert geo.lat is None and geo.lon is None
    assert geo.is_empty


def test_country_named_entity_takes_its_own_country_not_signals():
    """"France" mentioned in an India signal resolves to France, not India."""
    geo = resolve_entity_geo_offline(
        name="France", entity_class="country", signal_geo=_INDIA_GEO
    )
    # Name-derived country wins; the India coords are dropped (different place).
    assert geo.country is not None
    assert "france" in geo.country.lower() or geo.country.upper() == "FR"
    assert geo.lat is None and geo.lon is None


def test_country_named_entity_keeps_consistent_signal_coords():
    """When the signal AGREES on the country, its coords are inherited."""
    france_geo = {"lat": 48.85, "lon": 2.35, "country": "France"}
    geo = resolve_entity_geo_offline(
        name="France", entity_class="country", signal_geo=france_geo
    )
    assert geo.country == "France"
    assert geo.lat == 48.85 and geo.lon == 2.35


def test_non_geo_class_never_carries_geo():
    """A person/organization mention never gets a geo, regardless of signal."""
    for cls in ("person", "organization", "entity"):
        geo = resolve_entity_geo_offline(
            name="Angela Merkel", entity_class=cls, signal_geo=_INDIA_GEO
        )
        assert geo.is_empty, f"class {cls} must not carry geo"


@pytest.mark.asyncio
async def test_injected_geocoder_resolves_entity_name_toward_france():
    """With a geocoder wired, "Evian" geocodes by NAME → France (lat/lon set),
    never the India signal."""

    class _Result:
        lat = 46.40
        lon = 6.59
        country = "France"

    class _Geocoder:
        async def geocode(self, query):
            assert query == "Evian", "must geocode the ENTITY NAME, not the signal"
            return _Result()

    geo = await resolve_entity_geo(
        name="Evian",
        entity_class="location",
        signal_geo=_INDIA_GEO,
        geocoder=_Geocoder(),
    )
    assert geo.country == "France"
    assert geo.lat == 46.40 and geo.lon == 6.59


@pytest.mark.asyncio
async def test_geocoder_failure_degrades_to_offline_not_signal():
    """A geocoder that raises must degrade to the offline path (geo-NULL for a
    town), never fall back to the mentioning signal's country."""

    class _BoomGeocoder:
        async def geocode(self, query):
            raise RuntimeError("nominatim down")

    geo = await resolve_entity_geo(
        name="Evian",
        entity_class="location",
        signal_geo=_INDIA_GEO,
        geocoder=_BoomGeocoder(),
    )
    assert geo == EntityGeo()  # empty, NOT India


@pytest.mark.asyncio
async def test_no_geocoder_uses_offline_path():
    geo = await resolve_entity_geo(
        name="Evian", entity_class="location", signal_geo=_INDIA_GEO, geocoder=None
    )
    assert geo.is_empty
