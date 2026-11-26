# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Geo PRECEDENCE for issuing-authority sources, multi-country geo, and the
per-code derivation marks.

THE DEFECT (five official sources): the geocoder REPLACED the
country an official source speaks for. A US Federal Register export-control
notice landed geo={FR} off the citation "91 FR 60505"; the US "Declaration of
Freedom in Space" landed {FR} off an NER ``location`` "Space" the backend put
in Paris; German defence-ministry items landed {BA}/{EE}/{BD} off image-hash
hex in the RSS ``<img>`` and {GI} off "GI Breuer" (Generalinspekteur). The
token fixes live in :mod:`._geocode_rules`; this module is the PRECEDENCE.

THE RULE. A source with ``scope.source_class == "official"`` AND a declared
``scope.geo`` is an issuing authority speaking for its state. Its declared
countries are the DEFAULT subject and are ALWAYS in the signal's geo; the
countries its text is clearly ABOUT (named in the title / a field's lead zone,
a country-class NER entity there, an authoritative geometry, or the subject-
position ladder result) are ADDED beside them — never substituted. So an OFAC
designation of an Iranian network carries {US, IR} and both desks admit it.
Official sources WITHOUT a declared country (UN, WHO, USGS, GDACS) and every
other source class keep today's text-derived ladder untouched.

WHERE IT LANDS. ``signals.geo`` is ``text[]`` (multi-valued; desks select by
``geo && $desk_geo``). ``payload.geo`` stays the single PRIMARY block every
existing reader knows (``country_iso2`` / lat / lon), and gains:

* ``countries`` — every ISO2 the signal is filed under, primary first;
* ``derivation`` — ``{iso2: [basis, ...]}`` with basis one of
  ``declared_by_source`` / ``named_in_text`` / ``geometry`` /
  ``publisher_fallback`` (the URL-ccTLD rung of the ladder).

The host promote step (:func:`promoted_geo_codes`) lifts ``countries`` onto
``signals.geo``. The baseline's post-enrichment publisher-origin fallback
(``payload.geo_fallback``) is unchanged; :func:`geo_derivation` folds it in so
a reader sees one map.

AT SEA. A record that carries its OWN point and declares it
authoritative (``payload.geometry_authoritative``, stamped by a json_api
source's ``geometry`` block with ``off_land: no_country``) is never replaced
by a text guess. When that point falls in no admin-0 polygon (the bundled
Natural Earth 110m set) the signal is filed under NO country:
``payload.geo`` keeps the coordinates with ``at_sea: true`` and an empty
``countries`` list, and the geocoder adds the ``at_sea`` tag. A coarse 110m
coastline can put a point a few km offshore of a quay "at sea"; that
under-states (no country) rather than mis-states one. An issuer's declared
country (above) is still added — it is the source's own statement.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import pycountry

DECLARED = "declared_by_source"
NAMED = "named_in_text"
GEOMETRY = "geometry"
PUBLISHER_FALLBACK = "publisher_fallback"
#: Ladder basis (not a derivation mark): an authoritative point in no country.
AT_SEA = "at_sea"
AT_SEA_TAG = "at_sea"
#: ``payload.geo.inferred[].basis`` for an at-sea point's nearby coastal state.
NEAREST_COAST = "nearest_coast"

#: The ``scope.source_class`` that marks an issuing authority.
ISSUER_SOURCE_CLASS = "official"


def issuer_declared_countries(ctx: Any) -> list[str]:
    """The declared ISO2s when ``ctx`` is an official source that declares a
    country; ``[]`` otherwise (the general-news path)."""
    if str(getattr(ctx, "source_class", "") or "").strip().lower() != ISSUER_SOURCE_CLASS:
        return []
    out: list[str] = []
    for code in getattr(ctx, "scope_geo", None) or []:
        if isinstance(code, str) and len(code.strip()) == 2:
            c = code.strip().upper()
            if c not in out:
                out.append(c)
    return out


def payload_language(payload: Mapping[str, Any] | None, ctx: Any = None) -> str | None:
    """The source language the token rules key on: detected, NER, declared."""
    if isinstance(payload, Mapping):
        for key in ("language", "ner_language"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    langs = getattr(ctx, "scope_languages", None) or []
    return str(langs[0]).lower() if langs else None


def _country_block(iso2: str) -> dict[str, Any]:
    c = pycountry.countries.get(alpha_2=iso2)
    block: dict[str, Any] = {"source": "declared", "precision": "country",
                             "country_iso2": iso2}
    if c is not None:
        block["country"] = c.name
        block["country_iso3"] = c.alpha_3
    return block


def _mark(derivation: dict[str, list[str]], iso2: str, basis: str) -> None:
    bases = derivation.setdefault(iso2, [])
    if basis not in bases:
        bases.append(basis)


def build_geo_block(
    resolved: Mapping[str, Any] | None,
    resolved_basis: str | None,
    *,
    declared: Iterable[str] = (),
    named: Iterable[str] = (),
) -> dict[str, Any] | None:
    """The ``payload.geo`` block with ``countries`` + ``derivation``.

    ``resolved`` is the ladder's primary block (``None`` = nothing resolved),
    ``resolved_basis`` how it was reached. ``declared`` (issuer only) and
    ``named`` (subject-zone countries, issuer only) are added beside it.
    """
    declared = [d for d in declared if d]
    derivation: dict[str, list[str]] = {}
    primary_iso = resolved.get("country_iso2") if resolved else None
    if primary_iso:
        block = dict(resolved)
        _mark(derivation, primary_iso, resolved_basis or NAMED)
    elif declared:
        block = _country_block(declared[0])
        primary_iso = declared[0]
    else:
        return dict(resolved) if resolved else None
    for iso in declared:
        _mark(derivation, iso, DECLARED)
    for iso in named:
        if iso:
            _mark(derivation, iso, NAMED)
    countries = [primary_iso] + [c for c in derivation if c != primary_iso]
    block["countries"] = countries
    block["derivation"] = {c: derivation[c] for c in countries}
    return block


def point_is_authoritative(payload: Mapping[str, Any] | None) -> bool:
    """The record declared its own point final (no text-guess replacement)."""
    return isinstance(payload, Mapping) and payload.get("geometry_authoritative") is True


def at_sea_block(payload: Mapping[str, Any], block: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``payload.geo`` block for an authoritative point in no country:
    the coordinates, ``at_sea``, the (possibly declared-only) countries, and
    ``inferred`` — the coastal states it lies off.

    ``inferred`` is ``[{iso2, basis: "nearest_coast", distance_km}]``, nearest
    first, for states whose bundled 110m outline is within
    :data:`~._country_geometry.NEAREST_COAST_MAX_KM` (at most
    :data:`~._country_geometry.NEAREST_COAST_MAX_STATES`). It is a RELATION,
    never a filing: it is not in ``countries`` or ``derivation`` and the
    promote step never lifts it onto ``signals.geo``, so no desk reads an
    at-sea incident as an event IN a country. ``[]`` when no coast is in
    range (open ocean) or the point is unknown."""
    from ._country_geometry import coastal_states_near, representative_point

    out = dict(block) if block else {"source": GEOMETRY, "countries": [], "derivation": {}}
    pt = representative_point(dict(payload))
    inferred: list[dict[str, Any]] = []
    if pt is not None:
        out["lat"], out["lon"] = pt
        inferred = [
            {"iso2": iso2, "basis": NEAREST_COAST, "distance_km": km}
            for iso2, km in coastal_states_near(pt[0], pt[1])
        ]
    out["at_sea"] = True
    out["inferred"] = inferred
    return out


def inferred_coastal_states(geo_block: Any) -> list[str]:
    """The ISO2s an at-sea ``payload.geo`` block names as nearby coastal
    states (``inferred``), nearest first. ``[]`` for any other block."""
    if not isinstance(geo_block, Mapping) or geo_block.get("at_sea") is not True:
        return []
    out: list[str] = []
    for entry in geo_block.get("inferred") or []:
        iso2 = entry.get("iso2") if isinstance(entry, Mapping) else None
        if isinstance(iso2, str) and iso2 and iso2 not in out:
            out.append(iso2)
    return out


def with_at_sea_tag(tags: Iterable[str]) -> list[str]:
    out = list(tags)
    return out if AT_SEA_TAG in out else out + [AT_SEA_TAG]


def promoted_geo_codes(geo_block: Any) -> list[str]:
    """Every ISO2 a ``payload.geo`` block files the signal under, primary first
    — what the host promote step appends onto ``signals.geo``."""
    if not isinstance(geo_block, Mapping):
        return []
    out: list[str] = []
    primary = geo_block.get("country_iso2")
    if isinstance(primary, str) and primary:
        out.append(primary)
    extra = geo_block.get("countries")
    if isinstance(extra, list):
        for c in extra:
            if isinstance(c, str) and c and c not in out:
                out.append(c)
    return out


def promote_payload_columns(sig: Any) -> Any:
    """The host promote step: lift the payload enrichment (``payload.geo`` /
    ``payload.language`` / ``payload.entities``) onto the typed, indexed
    columns the subscription fan-out and per-target scoping match on
    (``geo`` / ``language`` / ``entity_classes``). Every code the geo block
    files the signal under is appended to ``geo`` (an issuer's declared state
    plus the subject), primary first. Mutates and returns ``sig``. Called by
    ``dapr_host``'s source enrichment stage, ahead of the document sink's
    ``write_canonical_signal`` (split out so a test can drive the
    exact step and read the persisted row)."""
    pl = sig.payload if isinstance(sig.payload, dict) else {}
    for iso2 in promoted_geo_codes(pl.get("geo")):
        if iso2 not in sig.geo:
            sig.geo.append(iso2)
    lang = pl.get("language")
    if lang and not sig.language:
        sig.language = lang
    ents = pl.get("entities")
    if isinstance(ents, list):
        for e in ents:
            cls = e.get("class") if isinstance(e, dict) else None
            if cls and cls not in sig.entity_classes:
                sig.entity_classes.append(cls)
    return sig


def geo_derivation(payload: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """One ``{iso2: [basis]}`` map for a stored payload: ``payload.geo``'s own
    ``derivation`` plus the baseline's ``payload.geo_fallback``."""
    out: dict[str, list[str]] = {}
    if not isinstance(payload, Mapping):
        return out
    geo = payload.get("geo")
    if isinstance(geo, Mapping):
        der = geo.get("derivation")
        if isinstance(der, Mapping):
            for iso, bases in der.items():
                for b in bases if isinstance(bases, list) else [bases]:
                    _mark(out, str(iso), str(b))
        elif isinstance(geo.get("country_iso2"), str):
            _mark(out, geo["country_iso2"], NAMED)
    fb = payload.get("geo_fallback")
    if isinstance(fb, Mapping):
        for iso in fb.get("iso2") or []:
            _mark(out, str(iso), PUBLISHER_FALLBACK)
    return out


__all__ = [
    "AT_SEA",
    "AT_SEA_TAG",
    "at_sea_block",
    "inferred_coastal_states",
    "NEAREST_COAST",
    "point_is_authoritative",
    "with_at_sea_tag",
    "DECLARED",
    "GEOMETRY",
    "ISSUER_SOURCE_CLASS",
    "NAMED",
    "PUBLISHER_FALLBACK",
    "build_geo_block",
    "geo_derivation",
    "issuer_declared_countries",
    "payload_language",
    "promote_payload_columns",
    "promoted_geo_codes",
]
