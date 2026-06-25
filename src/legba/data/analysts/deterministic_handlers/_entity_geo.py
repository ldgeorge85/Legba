# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity-geo resolution — geocode the ENTITY NAME, not its mentioning signal.

THE BUG THIS FIXES (graph-and-data Wave-1b, item 1; proven live: Evian→India)
----------------------------------------------------------------------------
``entity_resolution`` (and the seed driver) used to attach the *mentioning
signal's* geocode (lat/lon/country) to every ``location``-class entity. A town
named "Evian" first seen in an India-datelined article inherited **India's**
geo; the Wave-1a composite key ``(lower(name), class)`` then LOCKED that wrong
first value forever. Signal-location is NOT entity-location — a place can be
named anywhere on Earth.

THE FIX
-------
Resolve the geo of the entity **by its own name**, with the mentioning
signal's geocode demoted to at most a *consistency-checked fallback*:

  * If an offline geocoder (``GeocodeBackend``) is injected (production) and it
    resolves the entity NAME → use that lat/lon/country directly. This is the
    "geocode the entity name" primary path. Networked geocoders are NOT used
    here (the sweep is hot + batched); only an injected port is consulted, and
    a geocoder failure degrades to the offline path (never raises).
  * Offline name check (always available, hermetic): if the entity name itself
    *is* a country (``extract_country_iso2_from_text`` over the name), the geo
    country is taken from the NAME. The signal's lat/lon are inherited ONLY
    when the signal's geocoded country agrees with the name-derived country;
    on a mismatch the lat/lon are dropped (country-from-name wins, coords NULL).
  * Otherwise — a non-country place name we cannot offline-resolve (e.g.
    "Evian") — we DO NOT attach the signal's country at all. Geo stays NULL.
    Per the acceptance bar: such an entity resolves toward its real country (if
    a geocoder is wired) or stays geo-NULL, but NEVER the mentioning signal's
    country.

Non-``location``/``country`` classes (person/org/…) never carry a geo here.

Returns an :class:`EntityGeo` ``(lat, lon, country)`` triple of values to store
on ``entity_profiles``; any field may be ``None``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from ...filters.geocode import extract_country_iso2_from_text

logger = logging.getLogger(__name__)

#: Entity classes that legitimately carry a geographic location.
_GEO_CLASSES: frozenset[str] = frozenset({"location", "country"})


@dataclass(frozen=True)
class EntityGeo:
    """Resolved geo for an entity profile. Any field may be ``None``."""

    lat: float | None = None
    lon: float | None = None
    country: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.lat is None and self.lon is None and self.country is None


@runtime_checkable
class NameGeocoder(Protocol):
    """A minimal async geocoder port keyed by a place NAME.

    Structurally satisfied by :class:`legba.data.filters.geocode.GeocodeBackend`
    (``async def geocode(query) -> GeocodeResult | None``). Injected via
    ``deps.extras['geocoder']`` in production; absent in unit tests so the
    resolution stays hermetic (offline name-check only).
    """

    async def geocode(self, query: str) -> Any: ...  # -> GeocodeResult | None


def _signal_country(signal_geo: Mapping[str, Any] | None) -> str | None:
    if not signal_geo:
        return None
    c = signal_geo.get("country")
    return str(c) if c else None


def _offline_country_from_name(name: str) -> str | None:
    """ISO2 country if the entity NAME itself denotes a country, else None.

    Reuses the geocode filter's offline pycountry index — no network.
    """
    if not name:
        return None
    try:
        return extract_country_iso2_from_text(name)
    except Exception:  # pragma: no cover - defensive; pycountry is pure
        return None


def resolve_entity_geo_offline(
    *,
    name: str,
    entity_class: str,
    signal_geo: Mapping[str, Any] | None,
) -> EntityGeo:
    """Hermetic (no-network) entity-geo resolution. See module docstring.

    The signal's geocode is inherited ONLY when it is consistent with the
    entity NAME; a non-country place name we cannot resolve offline yields an
    empty geo rather than the (wrong) signal country.
    """
    cls = (entity_class or "").strip().lower()
    if cls not in _GEO_CLASSES:
        return EntityGeo()

    sig_country = _signal_country(signal_geo)
    name_iso2 = _offline_country_from_name(name)

    if name_iso2 is not None:
        # The name IS a country. Country comes from the name. Inherit the
        # signal's lat/lon only when the signal agrees on the country.
        sig_iso2 = (
            _offline_country_from_name(sig_country) if sig_country else None
        )
        if sig_iso2 is not None and sig_iso2 == name_iso2 and signal_geo:
            return EntityGeo(
                lat=signal_geo.get("lat"),
                lon=signal_geo.get("lon"),
                country=sig_country or name_iso2,
            )
        # Mismatch (or unknown signal country): keep the name's country, drop
        # the signal's coords (they belong to a different place).
        return EntityGeo(country=sig_country if sig_iso2 == name_iso2 else name_iso2)

    # A non-country place name (e.g. "Evian"): we cannot offline-resolve it, so
    # we must NOT attach the mentioning signal's country (that is the bug).
    # Geo stays NULL until a real geocoder resolves the name.
    return EntityGeo()


async def resolve_entity_geo(
    *,
    name: str,
    entity_class: str,
    signal_geo: Mapping[str, Any] | None,
    geocoder: NameGeocoder | None = None,
) -> EntityGeo:
    """Resolve an entity's geo by its NAME (primary), signal geo as fallback.

    When a ``geocoder`` is injected and resolves the entity NAME, its result is
    authoritative (the "geocode the entity name directly" path). A geocoder
    failure or miss degrades to :func:`resolve_entity_geo_offline` — never
    raises. Absent a geocoder (unit tests), the offline path is used directly.
    """
    cls = (entity_class or "").strip().lower()
    if cls not in _GEO_CLASSES:
        return EntityGeo()

    if geocoder is not None and name.strip():
        try:
            res = await geocoder.geocode(name.strip())
        except Exception as exc:  # degrade-not-drop
            logger.debug("entity_geo.geocode_failed name=%r err=%s", name, exc)
            res = None
        if res is not None:
            lat = getattr(res, "lat", None)
            lon = getattr(res, "lon", None)
            country = getattr(res, "country", None)
            if lat is not None or lon is not None or country is not None:
                return EntityGeo(lat=lat, lon=lon, country=country)

    return resolve_entity_geo_offline(
        name=name, entity_class=entity_class, signal_geo=signal_geo
    )


__all__ = [
    "EntityGeo",
    "NameGeocoder",
    "resolve_entity_geo",
    "resolve_entity_geo_offline",
]
