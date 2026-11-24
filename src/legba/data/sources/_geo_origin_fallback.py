# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The publisher-origin GEO FALLBACK (lane N-B): an untagged signal is not
left outside every desk.

THE DEFECT. A signal whose geocoder resolved nothing carries ``geo = {}``, and
a country desk's slice is ``geo && [code]`` — so it can enter NO desk. Times of
Israel's "Right-wing protesters warn of 'civil war' if government formed with
Arab-party support" (850253c4, 11-10) names the Knesset but no country; the
S-2 corroboration rule in :mod:`legba.data.sources.baseline` (origin applies
only when the body NAMES the origin country) left it untagged, and the Israel
desk never saw it.

THE RULE. When, after the enrichment chain and the corroboration rule, the
signal still has no geo, the publisher's home country (the source's
``scope.geo``, parked as ``payload.publisher_origin``) becomes its geo — as a
LOWER-CONFIDENCE, MARKED tag: ``payload.geo_fallback`` records the basis, so
any reader can tell it from an in-body resolution, and the watermark slice
(:mod:`legba.data.slice_coverage`) weighs a fallback row at half its salience.

S-2 STILL HOLDS for every row that names a place: the fallback fires ONLY when
the content names NO country at all — no country name or code (person names
blanked first, the lane's person guard), no country-class NER entity, and no
national DEMONYM of another country (``gazetteer.demonym_map``: "E.ON reviews
former German nuclear sites" from a Singapore wire stays untagged). A row that
names a country the geocoder could not resolve is exactly the row the outlet's
home must not overwrite.

Kill switch: ``LEGBA_GEO_ORIGIN_FALLBACK=0`` restores the pre-lane behaviour
(nothing written). New ingest only — existing rows are not rewritten.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Mapping

#: The payload key that marks a fallback tag.
GEO_FALLBACK_KEY = "geo_fallback"
_ENV = "LEGBA_GEO_ORIGIN_FALLBACK"
_TEXT_FIELDS = ("title", "summary", "distilled_body", "text", "raw_body")


def origin_fallback_enabled() -> bool:
    return (os.getenv(_ENV) or "1").strip().lower() not in ("0", "false", "off", "no")


@lru_cache(maxsize=1)
def _demonym_index() -> tuple[re.Pattern[str] | None, dict[str, str]]:
    from ..filters.geocode import _COUNTRY_NAME_INDEX
    from ..gazetteer import or_empty

    table = or_empty("demonym_map", consumer="geo_origin_fallback") or {}
    iso_of: dict[str, str] = {}
    for demonym, country in dict(table).items():
        iso = _COUNTRY_NAME_INDEX.get(str(country).strip().lower())
        if iso:
            iso_of[str(demonym).strip().lower()] = iso
    if not iso_of:
        return None, {}
    forms = sorted(iso_of, key=len, reverse=True)
    pat = re.compile(
        r"(?<![A-Za-z])(" + "|".join(re.escape(f) for f in forms) + r")(?![A-Za-z])",
        re.IGNORECASE,
    )
    return pat, iso_of


def _texts(payload: Mapping[str, Any]) -> list[str]:
    return [v for k in _TEXT_FIELDS if isinstance(v := payload.get(k), str) and v.strip()]


def names_any_country(payload: Mapping[str, Any], origin: list[str]) -> bool:
    """Does the content name a country (or another country's demonym)?"""
    from ..filters.geocode import (
        country_iso2s_from_country_entities,
        country_iso2s_in_text,
    )

    entities = payload.get("entities")
    if country_iso2s_from_country_entities(entities):
        return True
    pat, iso_of = _demonym_index()
    home = {o.upper() for o in origin}
    for text in _texts(payload):
        if country_iso2s_in_text(text, entities=entities):
            return True
        if pat is not None:
            for m in pat.finditer(text):
                if iso_of.get(m.group(1).lower()) not in home:
                    return True
    return False


def origin_fallback(signal: Any, origin_iso2: list[str]) -> list[str]:
    """The fallback geo for a still-untagged signal (``[]`` = none), stamping
    ``payload.geo_fallback`` when it applies."""
    if not origin_iso2 or not origin_fallback_enabled():
        return []
    payload = signal.payload if isinstance(signal.payload, dict) else None
    if payload is None or names_any_country(payload, origin_iso2):
        return []
    codes = [o.upper() for o in origin_iso2]
    payload[GEO_FALLBACK_KEY] = {
        "basis": "publisher_origin",
        "iso2": codes,
        "confidence": "low",
    }
    return codes


__all__ = [
    "GEO_FALLBACK_KEY",
    "names_any_country",
    "origin_fallback",
    "origin_fallback_enabled",
]
