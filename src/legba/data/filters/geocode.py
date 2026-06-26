# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Geocoding filter handler (L-153).

Implements the L-102 §3 filter/enrichment-kind contract. Each in-flight
:class:`legba.data.sources.Signal` is annotated with a structured ``geo``
block on its ``payload``:

.. code-block:: python

    signal.payload["geo"] = {
        "country": "Brazil",
        "country_iso2": "BR",
        "country_iso3": "BRA",
        "region": "Sao Paulo",
        "municipality": "Sao Paulo",
        "lat": -23.55,
        "lon": -46.63,
        "precision": "municipality",
        "source": "nominatim",
    }

Precision-configurable: configured per descriptor as one of ``country``,
``region``, ``municipality``, ``address`` — fields below the configured
precision are truncated from the result (e.g. at ``country`` precision we
only keep country-level fields). Strictly higher precisions are best-
effort: if the backend can't resolve to municipality, the handler emits
whatever it could resolve and marks ``precision`` accordingly.

Backend strategy
================
* **Nominatim** (default) — OpenStreetMap, free. The handler talks to
  ``nominatim_url`` if configured (self-hosted production), else the
  public ``https://nominatim.openstreetmap.org`` endpoint with the OSM
  usage policy's 1 req/sec rate limit honored client-side.
* **Google** — :class:`geopy.geocoders.GoogleV3` if the configured
  ``api_key_secret_ref`` resolves; selected by setting ``backend:
  "google"`` in the handler config.

Both backends are :mod:`geopy` clients wrapped in :func:`asyncio.to_thread`
so the actor loop never blocks. The handler exposes a backend-agnostic
:class:`GeocodeBackend` protocol so tests can inject deterministic stubs.

Inference precedence (D5 ladder)
================================
The ladder is ordered so an IN-BODY place mention always beats the
publisher's origin. ``transform`` runs, in order:

0. **geometry-first** (DQ-C1): an authoritative coordinate/geometry on the
   payload (NWS Polygon, USGS/GDACS/NASA point) is attributed offline by
   point-in-country and short-circuits everything below.
1. **geo.location_name**: if ``signal.payload["geo"]["location_name"]`` is
   present, geocode that directly.
2. **entities** (in-body NER): the ``country`` / ``location`` -class
   entities the upstream ner_multilingual filter stamped onto
   ``payload.entities`` — the place the *story* is about.
3. **title / text / raw_body** (in-body gazetteer): a country-name / ISO
   sweep via :mod:`pycountry` over each configured text field — ``text`` is
   the telegram/chat body. The first text hit wins.
4. **TLD fallback (WEAK publisher-origin)**: ONLY when ``tld_fallback`` is
   True AND every in-body candidate above failed. Derives country from the
   ``signal.canonical_url`` (or ``payload["link"]``) ccTLD — e.g. ``.br`` →
   Brazil. This is a deliberately demoted last resort: a ``.uk`` URL means
   the *publisher* is British, and ``t.me`` (telegram) resolves to Montenegro
   (``.me``) — neither describes the story, so it only fires when nothing
   in the body resolved.

The first candidate that the backend successfully resolves wins. If
``signal.payload["geo"]`` already has ``country`` + ``lat`` + ``lon``,
:meth:`transform` returns the signal unchanged (idempotency).

Caching
=======
Lookup results are memoized by ``(backend, query, precision)`` in a
Redis-backed cache (or any :class:`CacheStore`-shaped object provided via
constructor) with a 24h TTL by default. Cache values are JSON-encoded
:class:`GeocodeResult` instances. A negative-lookup (backend returned
``None``) is also cached for a short TTL so a typo-laden title doesn't
hammer the backend; see :data:`_NEGATIVE_CACHE_TTL_S`.

Failure semantics
=================
The handler never drops a signal: if every candidate fails to resolve,
:meth:`transform` returns the signal unchanged (no ``geo`` block
populated). Transient backend errors (timeouts, 5xx, quota exceeded) are
logged and surface in the next ``health_check``; per L-102 §7 they map to
``TransientFailure``-style retry semantics at the runtime layer.

This module never imports from ``legba.data.runtime`` — the runtime
(L-103) is not yet landed. It depends only on the structural-typing
surface in :mod:`legba.data.filters._contract`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import urlparse

import pycountry
from pydantic import BaseModel, ConfigDict, Field

from ._country_geometry import country_iso2_for_point, representative_point
from ..sources._contract import Signal
from ._contract import FilterContext, FilterHealth


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Precision + result types
# ---------------------------------------------------------------------------


Precision = Literal["country", "region", "municipality", "address"]
_PRECISION_RANK: dict[str, int] = {
    "country": 0,
    "region": 1,
    "municipality": 2,
    "address": 3,
}


@dataclass(frozen=True)
class GeocodeResult:
    """One backend lookup result, before precision truncation.

    Immutable so we can safely share between cache hits. Use
    :meth:`to_payload` to serialize into the ``signal.payload["geo"]``
    dict at the configured precision.
    """

    country: str | None
    country_iso2: str | None
    country_iso3: str | None
    region: str | None
    municipality: str | None
    address: str | None
    lat: float | None
    lon: float | None
    precision: Precision
    source: str                                  # backend name, e.g. "nominatim"

    def to_payload(self, target_precision: Precision) -> dict[str, Any]:
        """Return a ``signal.payload["geo"]`` dict at ``target_precision``.

        Fields strictly more specific than ``target_precision`` are
        dropped. ``precision`` in the output is ``min(resolved,
        target)`` — so a ``country``-precision target asking for
        ``municipality`` data gets country-only fields back, marked
        ``country``.
        """
        target_rank = _PRECISION_RANK[target_precision]
        resolved_rank = _PRECISION_RANK[self.precision]
        emit_rank = min(target_rank, resolved_rank)

        out: dict[str, Any] = {
            "source": self.source,
            "precision": _rank_to_precision(emit_rank),
        }
        if self.country is not None:
            out["country"] = self.country
        if self.country_iso2 is not None:
            out["country_iso2"] = self.country_iso2
        if self.country_iso3 is not None:
            out["country_iso3"] = self.country_iso3
        if emit_rank >= _PRECISION_RANK["region"] and self.region is not None:
            out["region"] = self.region
        if emit_rank >= _PRECISION_RANK["municipality"] and self.municipality is not None:
            out["municipality"] = self.municipality
        if emit_rank >= _PRECISION_RANK["address"] and self.address is not None:
            out["address"] = self.address
        if self.lat is not None:
            out["lat"] = self.lat
        if self.lon is not None:
            out["lon"] = self.lon
        return out

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "GeocodeResult":
        data = json.loads(raw)
        return cls(**data)


def _rank_to_precision(rank: int) -> Precision:
    for name, r in _PRECISION_RANK.items():
        if r == rank:
            return name                          # type: ignore[return-value]
    return "country"                             # pragma: no cover


def _geometry_result(iso2: str, *, lat: float, lon: float) -> GeocodeResult:
    """Build a country-precision GeocodeResult from an offline point-in-country
    hit (DQ-C1). The exact source coordinate is preserved; precision is
    ``country`` because the offline boundary set only resolves the country."""
    try:
        c = pycountry.countries.get(alpha_2=iso2)
    except (KeyError, AttributeError):           # pragma: no cover - defensive
        c = None
    return GeocodeResult(
        country=(c.name if c is not None else None),
        country_iso2=iso2,
        country_iso3=(getattr(c, "alpha_3", None) if c is not None else None),
        region=None,
        municipality=None,
        address=None,
        lat=lat,
        lon=lon,
        precision="country",
        source="geometry",
    )


# ---------------------------------------------------------------------------
# Backend protocol + Nominatim / Google implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class GeocodeBackend(Protocol):
    """Backend-agnostic surface for a geocoder.

    The handler depends on this protocol so the production geopy-backed
    backend, a self-hosted Nominatim, and the test stub can all stand in
    for one another. ``geocode`` returns ``None`` if the query couldn't
    be resolved; transient errors raise.
    """

    name: str

    async def geocode(self, query: str) -> GeocodeResult | None: ...

    async def reachable(self) -> bool: ...


# Public-Nominatim usage policy: max 1 req/sec.
_PUBLIC_NOMINATIM_URL = "https://nominatim.openstreetmap.org"
_PUBLIC_NOMINATIM_MIN_INTERVAL_S = 1.0

# Operator contact email threaded into the Nominatim User-Agent. The OSM
# Nominatim usage policy expects requests against the PUBLIC endpoint to
# carry an identifiable User-Agent including a way to reach the operator
# (so misbehaving clients can be contacted before being blocked).
_CONTACT_EMAIL_ENV = "LEGBA_GEOCODER_CONTACT_EMAIL"
_UA_BASE = "legba-filter-geocode/0.1"


def geocoder_contact_email() -> str | None:
    """Operator contact email for geocoder User-Agent headers.

    Read from the ``LEGBA_GEOCODER_CONTACT_EMAIL`` env var. Unset,
    non-address, and placeholder values (any address under the reserved
    ``.invalid`` TLD, e.g. the ``.env.example`` default
    ``ops@example.invalid``) are treated as "not configured" → ``None``.
    """
    raw = os.environ.get(_CONTACT_EMAIL_ENV, "").strip()
    if not raw or "@" not in raw or raw.lower().endswith(".invalid"):
        return None
    return raw


def resolve_user_agent(configured: str | None, *, nominatim_url: str | None) -> str:
    """Resolve the User-Agent string for a Nominatim backend.

    The OSM Nominatim usage policy
    (https://operations.osmfoundation.org/policies/nominatim/) requires
    requests to the shared PUBLIC endpoint to identify the application
    and give the operator a means of contact. Resolution order:

      * an explicit ``user_agent`` in the handler config wins as-is (the
        operator takes responsibility for its contents);
      * else, a real ``LEGBA_GEOCODER_CONTACT_EMAIL`` yields
        ``"<base> (<email>)"``;
      * else, against a SELF-HOSTED instance (``nominatim_url`` set) no
        contact is owed to anyone — the bare base UA is used;
      * else (public endpoint, no contact) we REFUSE to construct the
        backend: fail loud at build/activation time rather than hit the
        shared service anonymously, per the service's ToS expectations.
    """
    if configured:
        return configured
    email = geocoder_contact_email()
    if email:
        return f"{_UA_BASE} ({email})"
    if nominatim_url:
        return _UA_BASE
    raise RuntimeError(
        "geocode filter: the public Nominatim endpoint requires an operator "
        f"contact in the User-Agent (OSM usage policy). Set {_CONTACT_EMAIL_ENV} "
        "to a reachable email address, point `nominatim_url` at a self-hosted "
        "instance, or set `user_agent` explicitly in the handler config."
    )


class NominatimBackend:
    """:mod:`geopy` Nominatim adapter, wrapped to be async-safe.

    Constructor accepts an optional ``base_url`` — when ``None`` we hit
    the public OSM endpoint and apply a 1 req/sec client-side rate limit
    (per OSM policy). Self-hosted Nominatim has no such limit; set
    ``base_url`` to point at it.

    ``geopy.Nominatim`` is synchronous; we offload each call via
    :func:`asyncio.to_thread`. The blocking work is short (one HTTPS
    round-trip), and offloading keeps the actor loop responsive.
    """

    name: ClassVar[str] = "nominatim"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        user_agent: str = "legba-filter-geocode/0.1",
        timeout_seconds: int = 10,
        geocoder_cls: Any = None,
    ) -> None:
        # Import lazily so import-time failure of geopy doesn't break the
        # whole filters package import.
        if geocoder_cls is None:
            from geopy.geocoders import Nominatim as geocoder_cls  # noqa: N813
        self._base_url = base_url or _PUBLIC_NOMINATIM_URL
        self._is_public = base_url is None
        # geopy.Nominatim takes a `domain` (host) rather than full URL.
        parsed = urlparse(self._base_url)
        scheme = parsed.scheme or "https"
        domain = parsed.netloc or parsed.path                   # tolerate naked host
        self._client = geocoder_cls(
            user_agent=user_agent,
            domain=domain,
            scheme=scheme,
            timeout=timeout_seconds,
        )
        self._last_call_ts: float = 0.0
        self._rate_lock = asyncio.Lock()

    async def _respect_public_rate_limit(self) -> None:
        if not self._is_public:
            return
        async with self._rate_lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = (self._last_call_ts + _PUBLIC_NOMINATIM_MIN_INTERVAL_S) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_ts = loop.time()

    async def geocode(self, query: str) -> GeocodeResult | None:
        if not query or not query.strip():
            return None
        await self._respect_public_rate_limit()
        try:
            location = await asyncio.to_thread(
                self._client.geocode, query, addressdetails=True, language="en"
            )
        except Exception as exc:                                 # geopy.exc.*
            logger.warning("nominatim.geocode_failed query=%r err=%s", query, exc)
            raise
        if location is None:
            return None
        return _location_to_result(location, source="nominatim")

    async def reachable(self) -> bool:
        try:
            await self._respect_public_rate_limit()
            await asyncio.to_thread(self._client.geocode, "Brazil", addressdetails=False)
            return True
        except Exception as exc:
            logger.debug("nominatim.reachable_check_failed err=%s", exc)
            return False


class GoogleBackend:
    """:mod:`geopy` Google V3 adapter, wrapped to be async-safe.

    Requires an API key resolved from the credentials vault. The runtime
    injects the resolved key at handler construction time (per topology
    §9.9 — credentials never live inline in the descriptor).
    """

    name: ClassVar[str] = "google"

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: int = 10,
        geocoder_cls: Any = None,
    ) -> None:
        if geocoder_cls is None:
            from geopy.geocoders import GoogleV3 as geocoder_cls  # noqa: N813
        if not api_key:
            raise ValueError("GoogleBackend requires an api_key")
        self._client = geocoder_cls(api_key=api_key, timeout=timeout_seconds)

    async def geocode(self, query: str) -> GeocodeResult | None:
        if not query or not query.strip():
            return None
        try:
            location = await asyncio.to_thread(self._client.geocode, query)
        except Exception as exc:
            logger.warning("google.geocode_failed query=%r err=%s", query, exc)
            raise
        if location is None:
            return None
        return _location_to_result(location, source="google")

    async def reachable(self) -> bool:
        try:
            await asyncio.to_thread(self._client.geocode, "Brazil")
            return True
        except Exception as exc:
            logger.debug("google.reachable_check_failed err=%s", exc)
            return False


def _location_to_result(location: Any, *, source: str) -> GeocodeResult:
    """Normalize a :class:`geopy.location.Location` into a :class:`GeocodeResult`."""
    raw = getattr(location, "raw", {}) or {}
    address = raw.get("address") if isinstance(raw, dict) else None

    # geopy/Nominatim "address" fields: country, country_code, state, region,
    # county, city, town, village, municipality, road, house_number, etc.
    country = None
    country_iso2 = None
    region = None
    municipality = None
    address_str = getattr(location, "address", None)

    if isinstance(address, dict):
        country = address.get("country")
        cc = address.get("country_code")
        if isinstance(cc, str):
            country_iso2 = cc.upper()
        region = (
            address.get("state")
            or address.get("region")
            or address.get("province")
        )
        municipality = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )
    else:
        # Google + bare Location: address is a comma-joined string; the
        # last component is the country.
        components: list[str] = []
        if isinstance(address_str, str):
            components = [c.strip() for c in address_str.split(",") if c.strip()]
        if components:
            country = components[-1]
        if len(components) >= 2:
            region = components[-2]
        if len(components) >= 3:
            municipality = components[-3]

    iso3 = None
    if country_iso2:
        try:
            c = pycountry.countries.get(alpha_2=country_iso2)
            if c is not None:
                iso3 = c.alpha_3
                if not country:
                    country = c.name
        except (KeyError, AttributeError):                       # pragma: no cover
            pass
    elif country:
        try:
            c = pycountry.countries.lookup(country)
            country_iso2 = c.alpha_2
            iso3 = c.alpha_3
        except LookupError:
            pass

    lat = _safe_float(getattr(location, "latitude", None))
    lon = _safe_float(getattr(location, "longitude", None))

    # Resolved precision: the most-specific field we got back.
    if municipality:
        resolved: Precision = "municipality"
    elif region:
        resolved = "region"
    elif country:
        resolved = "country"
    else:
        # Backend returned a coordinate without a country — treat as
        # address-only (we still have lat/lon).
        resolved = "address"

    if address_str and isinstance(address, dict):
        # When address details came in structured form, derive a single-
        # line "address" representation for the address-precision case.
        address_field = address_str
    elif isinstance(address_str, str):
        address_field = address_str
    else:
        address_field = None

    return GeocodeResult(
        country=country,
        country_iso2=country_iso2,
        country_iso3=iso3,
        region=region,
        municipality=municipality,
        address=address_field,
        lat=lat,
        lon=lon,
        precision=resolved,
        source=source,
    )


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class GeocodeConfig(BaseModel):
    """Pydantic config schema for :class:`GeocodeHandler`.

    Used at descriptor-validation time (per L-101 / L-102 §1). The runtime
    parses each filter binding's ``config`` block against this model
    before the handler is activated.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["nominatim", "google"] = "nominatim"
    precision: Precision = "municipality"

    # When set, the handler points at a self-hosted Nominatim and skips
    # the public-OSM rate limit. When None, hits public OSM with 1
    # req/sec client-side throttle.
    nominatim_url: str | None = None

    # Vault reference for the Google API key. Resolved by the runtime
    # (per topology §9.9); the handler never sees the raw value via
    # config. Tests pass `api_key` directly to :class:`GoogleBackend`.
    google_api_key_secret_ref: str | None = None

    # Per-target user-agent override. OSM policy requires an identifiable
    # UA; when None (default) the UA is derived at backend-construction
    # time via :func:`resolve_user_agent` — base name plus the operator
    # contact from LEGBA_GEOCODER_CONTACT_EMAIL. Against the PUBLIC
    # Nominatim endpoint a missing contact makes construction fail loud.
    user_agent: str | None = None

    timeout_seconds: int = Field(default=10, ge=1, le=120)

    cache_ttl_seconds: int = Field(default=86_400, ge=0, le=30 * 86_400)

    # Priority list of payload fields to search for location hints. Order
    # matters — earlier entries win.
    #
    # D5: ``entities`` and ``text`` are now in the default list. ``entities``
    # reads the in-body NER place mentions (``country`` / ``location`` class
    # entities stamped by the upstream ner_multilingual filter) and ``text``
    # is the telegram/chat body field (telegram signals carry their content in
    # ``payload.text``, not ``title``/``raw_body``). Sweeping an in-body place
    # mention BEFORE the publisher-origin (TLD) fallback is what fixes the
    # Venezuela-0/141 and the telegram-``{ME}`` mis-tags: the article is about
    # Caracas, not about Montenegro (``t.me`` → ``.me``) or the BBC's ``.uk``.
    infer_from: list[str] = Field(
        default_factory=lambda: ["geo", "entities", "title", "text", "raw_body"],
        min_length=1,
    )

    # D5: publisher-origin (URL ccTLD) is a WEAK signal — a ``.uk`` URL says
    # the *publisher* is British, not that the *story* is about Britain, and a
    # ``t.me`` URL says nothing about the message's subject at all. When True
    # (default) the TLD is consulted ONLY after every in-body place candidate
    # has failed to resolve; when False the publisher-origin fallback is off
    # entirely (recommended for aggregator / chat sources like telegram whose
    # canonical URL host carries no story-geo signal — operators flip this per
    # descriptor). Set independently of ``infer_from``.
    tld_fallback: bool = True

    # When True, skip the handler entirely for signals that already have
    # a populated geo block (default; idempotent stream).
    skip_if_geo_present: bool = True


# ---------------------------------------------------------------------------
# Inference helpers — country-name + TLD extraction
# ---------------------------------------------------------------------------


# Hand-maintained TLD → ISO2 overrides. ccTLDs not in :mod:`pycountry`
# directly mappable (.uk / .su / .ac / etc.) plus generic 2-letter overrides.
_TLD_TO_ISO2: dict[str, str] = {
    "uk": "GB",
    "su": "RU",         # legacy Soviet — common still on archive content
    "ac": "SH",         # Ascension Island
    "io": "IO",         # British Indian Ocean Territory; commonly a gTLD-ish though
    "tv": "TV",         # Tuvalu
    "me": "ME",         # Montenegro
}

# D5: aggregator / chat / social hosts whose ccTLD is meaningful for the
# PLATFORM but says nothing about a message's story-geo. ``t.me`` /
# ``telegram.me`` resolve to Montenegro (``.me``) — the source of the spurious
# {ME} tag on every telegram channel message. We skip the TLD-origin
# derivation for these hosts entirely (the in-body NER ladder still runs).
# Matched as a full host suffix, so ``t.me`` and ``www.t.me`` both hit.
_NON_GEO_AGGREGATOR_HOSTS: frozenset[str] = frozenset({
    "t.me",
    "telegram.me",
    "telegram.org",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.com",
    "reddit.com",
    "medium.com",
})

# Allowed-list for the "very long" gTLDs we do NOT want to treat as ccTLDs.
_NON_CC_TLD: frozenset[str] = frozenset({
    "com", "org", "net", "gov", "edu", "mil", "int", "info", "biz",
    "name", "pro", "museum", "aero", "coop", "jobs", "mobi",
    "app", "dev", "tech", "ai", "xyz", "site", "online", "store",
    "blog", "news", "media", "press", "wiki",
})


# Build a fast lookup table once at module import. Index by canonical
# country name (lower-cased) → ISO2.
def _build_country_name_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for c in pycountry.countries:
        idx[c.name.lower()] = c.alpha_2
        # Common short / alternate names
        common = getattr(c, "common_name", None)
        official = getattr(c, "official_name", None)
        if isinstance(common, str):
            idx[common.lower()] = c.alpha_2
        if isinstance(official, str):
            idx[official.lower()] = c.alpha_2
    # Add a few high-traffic aliases pycountry doesn't cover natively.
    aliases = {
        "south korea": "KR",
        "north korea": "KP",
        "russia": "RU",
        "iran": "IR",
        "syria": "SY",
        "vietnam": "VN",
        "laos": "LA",
        "moldova": "MD",
        "tanzania": "TZ",
        "uk": "GB",
        "u.k.": "GB",
        "united kingdom": "GB",
        "usa": "US",
        "u.s.a.": "US",
        "u.s.": "US",
        "united states": "US",
        "czechia": "CZ",
        "czech republic": "CZ",
    }
    idx.update(aliases)
    return idx


_COUNTRY_NAME_INDEX: dict[str, str] = _build_country_name_index()

# Pre-compile a regex that matches any country name as a whole-word run.
# Names are sorted longest-first so "Republic of Korea" wins over "Korea".
def _build_country_regex() -> re.Pattern[str]:
    names = sorted(_COUNTRY_NAME_INDEX.keys(), key=len, reverse=True)
    # Escape + non-word boundary on each side. Use a character class to
    # avoid the Unicode-word edge-case in `\b`.
    escaped = [re.escape(n) for n in names if len(n) >= 2]
    pattern = r"(?<![A-Za-z])(" + "|".join(escaped) + r")(?![A-Za-z])"
    return re.compile(pattern, re.IGNORECASE)


_COUNTRY_REGEX = _build_country_regex()

# ISO2 / ISO3 standalone-token regex (e.g. "BR" or "BRA" appearing as a
# whole word). We filter these by membership in pycountry's tables.
_ISO_TOKEN_REGEX = re.compile(r"(?<![A-Za-z])([A-Z]{2,3})(?![A-Za-z])")

# DQ-C1: bare uppercase tokens that collide with ISO codes but are almost always
# a US state postal code or a (US) timezone abbreviation in the geospatial feeds
# (NWS/USGS titles end in "... by NWS Mobile AL"/"... 3:00 PM EDT"). Treating
# "AL"->Albania, "PA"->Panama, "MT"->Malta, "EST"->Estonia etc. as countries is
# exactly the catastrophic mis-attribution this fix removes. We reject these in
# the bare-token path ONLY (full country NAMES still resolve normally, and the
# geometry-first branch already handles the geo feeds authoritatively).
_US_STATE_CODES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
})
_TZ_ABBREVS = frozenset({
    "EST", "EDT", "CST", "CDT", "MST", "MDT", "PST", "PDT", "AKST", "AKDT",
    "HST", "HDT", "AST", "ADT", "UTC", "GMT", "ET", "CT", "MT", "PT", "AT",
    # Time meridians: "AM" collides with Armenia and "PM" with Saint Pierre &
    # Miquelon — this is the actual source of the constant Armenia/SPM
    # mis-attribution in NWS alert titles ("Effective until 3:00 AM ...").
    "AM", "PM",
})
_GEO_TOKEN_STOPSET = _US_STATE_CODES | _TZ_ABBREVS


def extract_country_iso2_from_text(text: str) -> str | None:
    """Find the first country-name or ISO2/3 hit in ``text``.

    Used by inference: feed the title, then the body. Returns ``None``
    if no hit. Case-insensitive on names; ISO codes require uppercase to
    avoid false positives ("at" the preposition vs "AT" Austria).
    """
    if not text:
        return None
    m = _COUNTRY_REGEX.search(text)
    if m:
        name = m.group(1).lower()
        iso = _COUNTRY_NAME_INDEX.get(name)
        if iso:
            return iso

    # Try uppercase ISO tokens; pycountry membership filter.
    for m2 in _ISO_TOKEN_REGEX.finditer(text):
        token = m2.group(1)
        if token in _GEO_TOKEN_STOPSET:
            # US state postal code / timezone abbrev — not a country signal.
            continue
        if len(token) == 2:
            c = pycountry.countries.get(alpha_2=token)
        else:
            c = pycountry.countries.get(alpha_3=token)
        if c is not None:
            return c.alpha_2
    return None


# D5: NER entity classes whose text is a place we can hand to the geocoder.
# ``country`` resolves to a nation directly; ``location`` is a city / region /
# landmark the backend can resolve to a finer point. We deliberately exclude
# ``person`` / ``organization`` / etc.
_PLACE_ENTITY_CLASSES: frozenset[str] = frozenset({"country", "location"})


def place_candidates_from_entities(entities: Any) -> list[str]:
    """Extract in-body place mentions from an NER ``entities`` list.

    Reads the ``payload.entities`` list the upstream ner_multilingual filter
    stamps (each item ``{"class": ..., "text": ...}``). Returns the de-duped
    place-bearing entity texts in list order, ``country``-class first (a named
    country is a stronger, less ambiguous geocode query than a bare
    ``location`` like "Springfield"). Non-place classes are ignored.

    This is the step that fixes Venezuela-0/141: a Reuters / telegram story
    *about* Caracas carries a ``location``/``country`` entity for it even when
    the publisher origin (``.uk`` / ``t.me``) says nothing about the subject.
    """
    if not isinstance(entities, (list, tuple)):
        return []
    countries: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        cls = str(ent.get("class") or "").strip().lower()
        if cls not in _PLACE_ENTITY_CLASSES:
            continue
        text = ent.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        if cls == "country":
            countries.append(text)
        else:
            others.append(text)
    return countries + others


def country_iso2_from_tld(url: str | None) -> str | None:
    """Derive an ISO2 country from a URL's TLD.

    Examples: ``https://uol.com.br/foo`` → ``"BR"``;
    ``https://example.com`` → ``None``;
    ``https://example.co.uk`` → ``"GB"`` (via override table).
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return None
    host = (parsed.netloc or parsed.path or "").split(":", 1)[0].lower()
    if not host:
        return None
    # D5: aggregator / chat / social hosts carry no story-geo in their ccTLD
    # (``t.me`` → Montenegro). Skip the origin derivation for them — full-host
    # OR registrable-suffix match so ``www.t.me`` and ``t.me`` both hit.
    parts = host.split(".")
    if host in _NON_GEO_AGGREGATOR_HOSTS or (
        len(parts) >= 2 and ".".join(parts[-2:]) in _NON_GEO_AGGREGATOR_HOSTS
    ):
        return None
    if len(parts) < 2:
        return None
    tld = parts[-1]
    if tld in _NON_CC_TLD:
        # Generic gTLD like `.com` or `.org` — no country info derivable.
        # We could try the second-level label, but `.com` is global by
        # design and false-positives outweigh hits there.
        return None
    if tld in _TLD_TO_ISO2:
        return _TLD_TO_ISO2[tld]
    if len(tld) == 2:
        c = pycountry.countries.get(alpha_2=tld.upper())
        if c is not None:
            return c.alpha_2
    return None


# ---------------------------------------------------------------------------
# Cache abstraction
# ---------------------------------------------------------------------------


_NEGATIVE_CACHE_TTL_S = 3600   # 1h for null lookups
_NEGATIVE_SENTINEL = b"__null__"


@runtime_checkable
class CacheStore(Protocol):
    """Async cache surface — minimal slice of :class:`redis.asyncio.Redis`.

    The production runtime injects an instance of L-001's :class:`RedisStore`'s
    underlying client (``RedisStore.client``); tests inject the in-memory
    :class:`_InMemoryCache` below.
    """

    async def get(self, key: str) -> bytes | None: ...
    async def setex(self, key: str, ttl: int, value: bytes) -> None: ...


class _InMemoryCache:
    """Process-local :class:`CacheStore` for tests and unit work.

    Stores ``(value, expires_at_monotonic_s)`` per key. TTL=0 means no
    expiry. Not thread-safe; intended for single-event-loop test use.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, float]] = {}

    async def get(self, key: str) -> bytes | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires > 0 and asyncio.get_event_loop().time() > expires:
            self._data.pop(key, None)
            return None
        return value

    async def setex(self, key: str, ttl: int, value: bytes) -> None:
        if ttl <= 0:
            self._data[key] = (value, 0.0)
        else:
            self._data[key] = (value, asyncio.get_event_loop().time() + ttl)

    def snapshot(self) -> dict[str, bytes]:
        """Test helper — non-protocol."""
        return {k: v for k, (v, _e) in self._data.items()}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


_CACHE_KEY_PREFIX = "legba:filter:geocode"


class GeocodeHandler:
    """Filter handler for the ``geocode`` kind.

    L-102 §3 conformance:

      * ``kind = "geocode"``, ``family = "filter"``.
      * Idempotent on ``(signal.content_hash, handler_version)`` — re-running
        a transformed signal is a no-op (the geo block is already populated).
      * Declares :attr:`output_contract` so registry composition checks can
        warn when downstream filters require ``payload.geo``.
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "geocode"
    family: ClassVar[str] = "filter"
    schema_version: ClassVar[str] = "legba/filter.geocode/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = GeocodeConfig
    handler_version: ClassVar[str] = "0.1.0"

    # Advisory composition contract — registry warns if downstream
    # filters / outputs reference `geo.*` but nothing populates them.
    output_contract: ClassVar[Mapping[str, type]] = {
        "payload.geo": dict,
        "payload.geo.country": str,
        "payload.geo.country_iso2": str,
        "payload.geo.lat": float,
        "payload.geo.lon": float,
        "payload.geo.precision": str,
    }

    def __init__(
        self,
        config: GeocodeConfig,
        *,
        backend: GeocodeBackend | None = None,
        cache: CacheStore | None = None,
    ) -> None:
        """Construct a handler bound to a parsed :class:`GeocodeConfig`.

        Parameters
        ----------
        config:
            Validated handler config.
        backend:
            Optional pre-built backend. If ``None``, a backend matching
            ``config.backend`` is constructed (with the configured URL or
            credentials). Tests inject a deterministic stub here.
        cache:
            Optional :class:`CacheStore` for memoization. If ``None``,
            an in-memory cache is used. Production runtime injects the
            shared Redis client.
        """
        self._config = config
        self._backend: GeocodeBackend | None = backend
        self._cache: CacheStore = cache if cache is not None else _InMemoryCache()
        self._stats = {"signals_in": 0, "signals_out": 0, "signals_dropped": 0}
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ transform

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None:
        """Annotate the signal with structured geographic info.

        Never drops the signal. Returns the signal unchanged if no
        location can be inferred.
        """
        self._stats["signals_in"] += 1

        # Idempotency: skip if already geocoded.
        existing_geo = (signal.payload or {}).get("geo")
        if (
            self._config.skip_if_geo_present
            and isinstance(existing_geo, dict)
            and existing_geo.get("country")
            and existing_geo.get("lat") is not None
            and existing_geo.get("lon") is not None
        ):
            self._stats["signals_out"] += 1
            return signal

        result = await self._infer_and_lookup(signal, ctx)
        if result is None:
            # No location resolved — emit signal unchanged.
            self._stats["signals_out"] += 1
            return signal

        geo_payload = result.to_payload(self._config.precision)
        # Preserve any pre-existing geo hints (e.g. `location_name`) by
        # merging — handler-derived fields take precedence.
        if isinstance(existing_geo, dict):
            merged = dict(existing_geo)
            merged.update(geo_payload)
        else:
            merged = geo_payload
        new_payload = dict(signal.payload or {})
        new_payload["geo"] = merged

        self._stats["signals_out"] += 1
        return signal.model_copy(update={"payload": new_payload})

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: FilterContext) -> FilterHealth:
        """Backend reachability + one cached + one fresh geocode."""
        backend = await self._ensure_backend()
        detail: dict[str, Any] = {"backend": backend.name}

        # Probe 1: backend reachable.
        try:
            reachable = await backend.reachable()
        except Exception as exc:
            return FilterHealth(
                state="unhealthy",
                last_error=f"backend reachability probe raised: {exc!s}",
                signals_in_24h=self._stats["signals_in"],
                signals_out_24h=self._stats["signals_out"],
                signals_dropped_24h=self._stats["signals_dropped"],
                detail={**detail, "probe": "reachable"},
            )
        detail["reachable"] = bool(reachable)
        if not reachable:
            return FilterHealth(
                state="unhealthy",
                last_error="backend not reachable",
                signals_in_24h=self._stats["signals_in"],
                signals_out_24h=self._stats["signals_out"],
                signals_dropped_24h=self._stats["signals_dropped"],
                detail=detail,
            )

        # Probe 2: a known-good query through the full pipeline (cache + fetch).
        try:
            r = await self._lookup_cached(backend, "Brazil")
            detail["fresh_or_cached_query_ok"] = r is not None
        except Exception as exc:
            return FilterHealth(
                state="degraded",
                last_error=f"sample query failed: {exc!s}",
                signals_in_24h=self._stats["signals_in"],
                signals_out_24h=self._stats["signals_out"],
                signals_dropped_24h=self._stats["signals_dropped"],
                detail={**detail, "probe": "sample_query"},
            )

        return FilterHealth(
            state="healthy",
            last_success_at=self._last_success_at,
            signals_in_24h=self._stats["signals_in"],
            signals_out_24h=self._stats["signals_out"],
            signals_dropped_24h=self._stats["signals_dropped"],
            detail=detail,
        )

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(self, ctx: FilterContext) -> None:
        return None

    async def on_activate(self, ctx: FilterContext) -> None:
        return None

    async def on_pause(self, ctx: FilterContext) -> None:
        return None

    async def on_resume(self, ctx: FilterContext) -> None:
        return None

    async def on_retire(self, ctx: FilterContext) -> None:
        return None

    # ------------------------------------------------------------- internals

    async def _ensure_backend(self) -> GeocodeBackend:
        if self._backend is not None:
            return self._backend
        if self._config.backend == "nominatim":
            self._backend = NominatimBackend(
                base_url=self._config.nominatim_url,
                user_agent=resolve_user_agent(
                    self._config.user_agent,
                    nominatim_url=self._config.nominatim_url,
                ),
                timeout_seconds=self._config.timeout_seconds,
            )
        elif self._config.backend == "google":
            # The runtime resolves `google_api_key_secret_ref` from the
            # vault and injects a pre-built backend via the constructor.
            # When constructed without injection (e.g. ad-hoc dev runs),
            # we honor the LEGBA_GOOGLE_MAPS_API_KEY env var.
            api_key = os.environ.get("LEGBA_GOOGLE_MAPS_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "google backend requires `google_api_key_secret_ref` resolved "
                    "by the runtime or LEGBA_GOOGLE_MAPS_API_KEY env var."
                )
            self._backend = GoogleBackend(
                api_key=api_key,
                timeout_seconds=self._config.timeout_seconds,
            )
        else:                                                    # pragma: no cover
            raise ValueError(f"unknown backend: {self._config.backend!r}")
        return self._backend

    async def _infer_and_lookup(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> GeocodeResult | None:
        """Run the inference precedence ladder until something resolves."""
        # DQ-C1: GEOMETRY-FIRST. If the payload carries an authoritative
        # coordinate/geometry (NWS active_alerts ship a Polygon; USGS/GDACS/NASA
        # ship points), attribute the country offline by point-in-country and
        # short-circuit — the structured coordinate is ground truth, whereas
        # text-geocoding a weather/quake title (which carries a state/timezone
        # token, not a country) yields a constant bogus France/Armenia match.
        pt = representative_point(signal.payload or {})
        if pt is not None:
            iso2 = country_iso2_for_point(pt[0], pt[1])
            if iso2:
                self._last_success_at = datetime.now(tz=timezone.utc)
                return _geometry_result(iso2, lat=pt[0], lon=pt[1])
        backend = await self._ensure_backend()
        candidates = self._derive_candidates(signal)
        for query in candidates:
            try:
                result = await self._lookup_cached(backend, query)
            except Exception as exc:
                # Transient backend errors don't fail the whole filter —
                # we log + move to the next candidate. The runtime's
                # health-probe surfaces persistent backend issues.
                ctx.logger.warning(
                    "geocode.candidate_failed query=%r err=%s", query, exc
                )
                self._last_error = f"{exc!s}"
                continue
            if result is not None:
                self._last_success_at = datetime.now(tz=timezone.utc)
                return result
        return None

    def _derive_candidates(self, signal: Signal) -> list[str]:
        """Return a priority-ordered, de-duplicated candidate list.

        D5 ordering: every in-body candidate (geo.location_name, NER place
        entities, text-field country sweep) is appended FIRST, in the
        configured ``infer_from`` order. The publisher-origin (TLD) fallback
        is appended LAST and ONLY when ``tld_fallback`` is enabled — so a
        ``.uk`` / ``t.me`` host can never beat an in-body place mention.
        """
        out: list[str] = []

        def _add(candidate: str | None) -> None:
            if candidate and candidate not in out:
                out.append(candidate)

        payload = signal.payload or {}
        for field_name in self._config.infer_from:
            for candidate in self._candidates_for_field(field_name, signal, payload):
                _add(candidate)

        # WEAK publisher-origin fallback — last resort, opt-out-able.
        if self._config.tld_fallback:
            tld_iso = country_iso2_from_tld(
                signal.canonical_url or payload.get("link") or payload.get("source_url")
            )
            if tld_iso:
                try:
                    c = pycountry.countries.get(alpha_2=tld_iso)
                    if c is not None:
                        _add(c.name)
                except (KeyError, AttributeError):                # pragma: no cover
                    pass
        return out

    def _candidates_for_field(
        self,
        field_name: str,
        signal: Signal,
        payload: dict[str, Any],
    ) -> list[str]:
        """Return the (possibly multiple) candidate query strings for one
        ``infer_from`` field, in priority order."""
        if field_name == "geo":
            geo = payload.get("geo")
            if isinstance(geo, dict):
                loc = geo.get("location_name")
                if isinstance(loc, str) and loc.strip():
                    return [loc.strip()]
            return []
        if field_name == "entities":
            # In-body NER place mentions — hand the raw place text to the
            # backend (a city/region resolves to a finer point than the
            # country-name sweep would). Country-class entities first.
            return place_candidates_from_entities(payload.get("entities"))
        # All other fields are text — sweep for a country mention.
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return []
        iso = extract_country_iso2_from_text(value)
        if iso:
            try:
                c = pycountry.countries.get(alpha_2=iso)
                if c is not None:
                    return [c.name]
            except (KeyError, AttributeError):                    # pragma: no cover
                pass
        return []

    async def _lookup_cached(
        self,
        backend: GeocodeBackend,
        query: str,
    ) -> GeocodeResult | None:
        """Get-or-set: consult cache, fall back to backend, store result."""
        key = self._cache_key(backend.name, query)
        cached = None
        try:
            cached = await self._cache.get(key)
        except Exception as exc:                                  # pragma: no cover
            logger.warning("geocode.cache_get_failed key=%s err=%s", key, exc)
        if cached is not None:
            if cached == _NEGATIVE_SENTINEL:
                return None
            try:
                return GeocodeResult.from_json(cached)
            except (ValueError, TypeError, KeyError) as exc:
                logger.warning("geocode.cache_decode_failed err=%s", exc)
                # Treat corrupt cache as miss; fall through to backend.

        result = await backend.geocode(query)
        try:
            if result is None:
                await self._cache.setex(
                    key, _NEGATIVE_CACHE_TTL_S, _NEGATIVE_SENTINEL
                )
            else:
                await self._cache.setex(
                    key,
                    self._config.cache_ttl_seconds,
                    result.to_json().encode("utf-8"),
                )
        except Exception as exc:                                  # pragma: no cover
            logger.warning("geocode.cache_set_failed err=%s", exc)
        return result

    def _cache_key(self, backend_name: str, query: str) -> str:
        # Include precision so a different-precision target doesn't pull
        # truncated data from a more-precise cache. Note: the *value* is
        # the full GeocodeResult; precision only affects to_payload().
        # We key by precision anyway so backend selection + query +
        # precision are isolated. This is conservative — we could share
        # values across precisions safely but keeping the namespace clean
        # is worth the duplicate-fetch cost.
        return (
            f"{_CACHE_KEY_PREFIX}:{backend_name}:{self._config.precision}:"
            f"{_canon_query(query)}"
        )


def _canon_query(q: str) -> str:
    """Canonicalize a query string for cache keying.

    Lowercase, collapse internal whitespace, strip. Keeps the key human-
    readable so an operator running ``redis-cli KEYS legba:filter:geocode:*``
    can scan it.
    """
    return re.sub(r"\s+", " ", q.strip().lower())


__all__ = [
    "GeocodeBackend",
    "GeocodeConfig",
    "GeocodeHandler",
    "GeocodeResult",
    "GoogleBackend",
    "NominatimBackend",
    "country_iso2_from_tld",
    "extract_country_iso2_from_text",
    "geocoder_contact_email",
    "place_candidates_from_entities",
    "resolve_user_agent",
]
