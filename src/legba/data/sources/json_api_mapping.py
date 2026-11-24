# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declarative record mapping for the ``json_api`` kind (lane N-R).

Two OPTIONAL config keys a ``json_api`` source may declare; a source that
declares neither emits exactly what it emitted before (pinned by
``tests/data_pkg/test_json_api_mapping_equivalence.py``):

``title_template``
    A small template over record fields, e.g.
    ``"{incidentTypeName}: {place} — {vesselType}"``. Each ``{...}`` is a
    field path in the same dot/bracket syntax as ``title_path``. Rendering is
    forgiving about MISSING fields and strict about nothing else:

      * a value is used when it is a string or a number; it is trimmed and
        its internal whitespace collapsed; a value with no letter or digit
        (``"."``, ``"-"``, ``""``) counts as missing;
      * a missing field drops itself AND the separator in front of it; the
        first value present drops its own leading separator, so
        ``"{a}: {b} — {c}"`` renders ``"A: B — C"``, ``"A — C"`` (no b),
        ``"B — C"`` (no a), ``"A: B"`` (no c);
      * the text before the first placeholder and after the last one is kept
        only when at least one value is present;
      * every field missing renders ``""`` and the handler falls back to
        ``title_path`` (when declared).

    Braces are reserved: a literal ``{`` or ``}`` outside a placeholder is
    refused at validation, as is a template with no placeholder.

``body_template`` (lane K-1)
    The same grammar and rules, rendered by the same function, for the
    signal's body (``payload.raw_body``): a register record (the KEV
    catalogue) carries its meaning in several fields, and a labelled
    separator (``" Due: {dueDate}"``) drops with its missing field. An
    all-missing render falls back to ``body_path``.

``geometry``
    ``{lat_path, lon_path, place_path?, off_land?}`` — take the record's own
    coordinates as the signal's geometry. The handler stamps
    ``payload.geometry`` as a GeoJSON ``Point`` (``[lon, lat]``), which the
    geocode filter's geometry-first tier already reads before any text
    inference. ``place_path`` (optional) names the record's own place label;
    it is stamped as ``payload.geo.location_name``. ``off_land`` says what a
    point that falls in NO country polygon means:

      * ``no_country`` (default) — the point is authoritative; the signal is
        filed under no country (``payload.geometry_authoritative = true``
        tells the geocoder not to replace the point with a text guess). The
        geocoder records ``payload.geo.at_sea = true`` with the coordinates
        and adds the ``at_sea`` tag. This is the honest reading of an
        incident at sea: the record says WHERE, not WHICH STATE.
      * ``text_fallback`` — the pre-lane behaviour for a geometry feed: an
        off-land point falls through to text geocoding.

    A coordinate that is missing, not numeric, out of range, or exactly
    ``(0, 0)`` (the usual "unknown" sentinel) yields no geometry.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AT_SEA_TAG",
    "JsonApiGeometry",
    "apply_geometry",
    "parse_title_template",
    "render_title",
]

#: The tag the geocoder adds to a signal whose authoritative point is at sea.
AT_SEA_TAG = "at_sea"

_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_WS_RE = re.compile(r"\s+")
_HAS_WORD_RE = re.compile(r"\w", re.UNICODE)
_MAX_PLACEHOLDERS = 8


def parse_title_template(template: str) -> tuple[str, list[tuple[str, str]], str]:
    """``(prefix, [(separator_before, path), ...], suffix)``.

    Raises ``ValueError`` for a stray brace, an empty placeholder, a bad path,
    no placeholder, or more than :data:`_MAX_PLACEHOLDERS`.
    """
    from .json_api import parse_path  # the one path grammar (json_api imports us lazily)

    literals: list[str] = []
    paths: list[str] = []
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        literals.append(template[pos:match.start()])
        path = match.group(1).strip()
        if not path:
            raise ValueError("title_template placeholders must name a field path")
        if not parse_path(path):
            raise ValueError(f"title_template placeholder {{{path}}} is not a field path")
        paths.append(path)
        pos = match.end()
    literals.append(template[pos:])
    for literal in literals:
        if "{" in literal or "}" in literal:
            raise ValueError(f"title_template has a stray brace in {literal!r}")
    if not paths:
        raise ValueError("title_template needs at least one {field} placeholder")
    if len(paths) > _MAX_PLACEHOLDERS:
        raise ValueError(f"title_template allows at most {_MAX_PLACEHOLDERS} placeholders")
    # literals[0] is the prefix, literals[i] (1..n-1) the separator in front
    # of placeholder i, literals[n] the suffix.
    parts = [("" if i == 0 else literals[i], p) for i, p in enumerate(paths)]
    return literals[0], parts, literals[-1]


def _clean(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return ""
    text = _WS_RE.sub(" ", text).strip()
    return text if _HAS_WORD_RE.search(text) else ""


def render_title(template: str, item: dict[str, Any]) -> str:
    """Render ``template`` against one record (missing fields handled as the
    module docstring says). ``""`` when every field is missing."""
    from .json_api import parse_path, resolve_path

    prefix, parts, suffix = parse_title_template(template)
    present = [(sep, v) for sep, path in parts
               if (v := _clean(resolve_path(item, parse_path(path))))]
    if not present:
        return ""
    body = present[0][1] + "".join(sep + v for sep, v in present[1:])
    return _WS_RE.sub(" ", prefix + body + suffix).strip()


class JsonApiGeometry(BaseModel):
    """The ``geometry`` block of a ``json_api`` config (see module docstring)."""

    model_config = ConfigDict(extra="forbid")

    lat_path: str = Field(..., min_length=1, max_length=512)
    lon_path: str = Field(..., min_length=1, max_length=512)
    place_path: str | None = Field(default=None, min_length=1, max_length=512)
    off_land: Literal["no_country", "text_fallback"] = "no_country"

    @field_validator("lat_path", "lon_path", "place_path")
    @classmethod
    def _paths(cls, v: str | None) -> str | None:
        from .json_api import parse_path

        if v is not None and not parse_path(v):
            raise ValueError("geometry paths must name a record field")
        return v


def _coord(value: Any, bound: float) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    out = float(value)
    if not math.isfinite(out) or abs(out) > bound:
        return None
    return out


def apply_geometry(payload: dict[str, Any], item: dict[str, Any], geom: JsonApiGeometry) -> None:
    """Stamp ``payload.geometry`` (+ the authoritative flag and the record's
    place label) from one record. A record without a usable point gets
    nothing — the geocoder then runs its text ladder as for any signal."""
    from .json_api import parse_path, resolve_path

    lat = _coord(resolve_path(item, parse_path(geom.lat_path)), 90.0)
    lon = _coord(resolve_path(item, parse_path(geom.lon_path)), 180.0)
    if geom.place_path:
        place = _clean(resolve_path(item, parse_path(geom.place_path)))
        if place:
            payload["geo"] = {"location_name": place}
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return
    payload["geometry"] = {"type": "Point", "coordinates": [lon, lat]}
    if geom.off_land == "no_country":
        payload["geometry_authoritative"] = True
