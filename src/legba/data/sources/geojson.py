# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GeoJSON / GIS source handler — the first model-free non-text modality.

Implements the L-102 source-kind contract for a structured geospatial feed:
a configurable URL serving a GeoJSON document (RFC 7946) — a
``FeatureCollection``, a bare ``Feature``, or a bare ``Geometry``. The handler
pulls the document over ``httpx`` (honoring stored ETag / Last-Modified like
the RSS handler), parses it, and yields one :class:`Signal` per geographic
feature with:

  * ``modality = "structured"``
  * ``mime_type = "application/geo+json"``
  * ``media_ref`` pointing at the source document URL (REFERENCE, not inlined
    bytes — the per-feature geometry IS inlined in the payload because it is
    small and the value of the signal)
  * ``geo`` populated from the feature's properties when an ISO country / admin
    code is present, else from the source scope.

This is deliberately **model-free**: GeoJSON is already structured, so there is
no extraction model in the loop. The UI's ``application/geo+json`` renderer
(``legba-ui-v3/src/lib/modalityRenderers.tsx``, reached via ``ModalityRef``)
maps a feature payload to a MapLibre view — no ML at all on either side.

Failure semantics (L-102 §7), mirroring the RSS handler:

  * Parse failure (not JSON, or not a GeoJSON object) → log, yield nothing,
    next ``health_check`` reports ``unhealthy`` (a malformed document does not
    self-heal on the same payload).
  * Transient network error / 5xx → one retry, then yield nothing; health is
    ``degraded``.
  * 4xx (other than 304) → no retry, yield nothing; health is ``unhealthy``.
  * HTTP 304 → empty iterator, health ``healthy`` (not modified).

This module never imports from ``legba.data.runtime`` — it depends only on the
structural-typing surface in ``_contract.py`` plus ``httpx`` (a hard dep).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ._contract import Signal, SourceContext, SourceHealth
from ._egress import guarded_async_client
from ..filters._country_geometry import (
    country_iso2_for_point,
    representative_point,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


#: The mime type the UI's geo+json renderer (MODALITY_RENDERERS) keys on.
GEOJSON_MIME_TYPE: str = "application/geo+json"

_GEOJSON_CURSOR_KEY = "geojson_cursor"
_GEOJSON_HEALTH_KEY = "geojson_health"
_DEFAULT_RETRIES_FOR_TRANSIENT = 1
_TRANSIENT_STATUS = {502, 503, 504}

#: RFC 7946 top-level GeoJSON object types.
_GEOJSON_GEOMETRY_TYPES: frozenset[str] = frozenset({
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
})

#: Feature property keys we probe for a human-readable title, in priority order.
_TITLE_KEYS: tuple[str, ...] = (
    "title", "name", "label", "place", "headline", "id",
)

#: Feature property keys we probe for an ISO geo code (country / admin region).
_GEO_KEYS: tuple[str, ...] = (
    "iso3", "iso_a3", "iso", "iso2", "iso_a2", "country_code",
    "admin1", "admin", "region", "country",
)

#: Feature property keys we probe for a canonical / source URL.
_URL_KEYS: tuple[str, ...] = ("url", "link", "source_url", "canonical_url")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class GeoJSONConfig(BaseModel):
    """Pydantic config schema for :class:`GeoJSONSourceHandler`.

    Validated at descriptor-registration time (L-101 / L-102 §1). The runtime
    parses each ``SourceBinding.config`` against this model before the handler
    is activated.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=4096)
    #: When the document is a ``FeatureCollection``, the property key whose
    #: value uniquely identifies a feature across pulls (drives ``external_id``
    #: + dedupe). Defaults to GeoJSON's optional top-level feature ``id``;
    #: falls back to a content hash when absent.
    feature_id_key: str = Field(default="id", min_length=1, max_length=128)
    #: Cap the number of features emitted per pull — a defensive bound so a
    #: pathological document (millions of features) can't trap the poller.
    max_features: int = Field(default=5000, ge=1, le=1_000_000)
    user_agent: str = Field(default="Legba/2.0", max_length=256)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class GeoJSONSourceHandler:
    """Source handler for a GeoJSON (RFC 7946) document feed.

    L-102 conformance:

      * ``kind = "geojson"``, ``family = "source"``.
      * Owns its cursor in ``ctx.state_store`` (ETag + Last-Modified), same
        conditional-GET shape as :class:`~legba.data.sources.rss.RSSSourceHandler`.
      * Yields :class:`Signal` instances with ``modality="structured"`` +
        ``mime_type="application/geo+json"``; idempotent — downstream dedupe
        handles overlap windows.
      * Exposes ``health_check`` + lifecycle hooks (default no-op).
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "geojson"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.geojson/1-0-0"
    config_schema: ClassVar[type[BaseModel]] = GeoJSONConfig
    handler_version: ClassVar[str] = "0.1.0"
    # DQ-H5b (#88) — state-store key under which this handler records its poll
    # health, so the source actor can read the WHY for a non-productive poll.
    health_state_key: ClassVar[str] = _GEOJSON_HEALTH_KEY

    def __init__(
        self,
        config: GeoJSONConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Construct a handler bound to a parsed :class:`GeoJSONConfig`.

        ``http_client`` is optional — tests inject a client wired to a mock
        transport; production uses the client this class creates on demand.
        """
        self._config = config
        self._client = http_client

    # ------------------------------------------------------------------ pull

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Async-generator yielding one :class:`Signal` per GeoJSON feature.

        ``since`` is unused for the document fetch (GeoJSON documents carry no
        per-feature publish timestamp by spec); the conditional-GET cursor +
        downstream dedupe absorb re-pulls of an unchanged document.

        State:
          ``ctx.state_store[_GEOJSON_CURSOR_KEY] = {"etag", "last_modified"}``
        """
        cursor = await self._load_cursor(ctx)
        headers = self._build_conditional_headers(cursor)

        response = await self._fetch_with_retry(headers=headers, ctx=ctx)
        if response is None:
            # Transient + retry exhausted; health probe surfaces the cause.
            return

        if response.status_code == 304:
            await self._record_health(
                ctx,
                state="healthy",
                last_success_at=datetime.now(tz=timezone.utc),
                detail={"status": 304, "note": "not modified"},
            )
            return

        if response.status_code >= 400:
            await self._record_health(
                ctx,
                state="unhealthy",
                last_error=f"HTTP {response.status_code}",
                detail={"status": response.status_code},
            )
            return

        features = self._safe_parse(response.text)
        if features is None:
            await self._record_health(
                ctx,
                state="unhealthy",
                last_error="geojson parse failure",
                detail={"status": response.status_code},
            )
            return

        emitted = 0
        for feature in features:
            if emitted >= self._config.max_features:
                ctx.logger.warning(
                    "geojson.pull.max_features url=%s cap=%d — truncating",
                    self._config.url, self._config.max_features,
                )
                break
            signal = self._feature_to_signal(feature, ctx=ctx)
            if signal is None:
                continue
            emitted += 1
            yield signal

        new_cursor = {
            "etag": response.headers.get("etag", "") or "",
            "last_modified": response.headers.get("last-modified", "") or "",
        }
        await ctx.state_store.set(_GEOJSON_CURSOR_KEY, new_cursor)
        await self._record_health(
            ctx,
            state="healthy",
            last_success_at=datetime.now(tz=timezone.utc),
            detail={
                "status": response.status_code,
                "features_yielded": emitted,
                "etag": new_cursor["etag"],
            },
        )

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Conditional GET probe. 200/304 → healthy; transient → degraded;
        persistent 4xx → unhealthy. Falls back to the last recorded summary
        when a probe attempt fails outright (e.g. DNS failure)."""
        cursor = await self._load_cursor(ctx)
        headers = self._build_conditional_headers(cursor)
        previous = await ctx.state_store.get(_GEOJSON_HEALTH_KEY) or {}

        try:
            client = await self._get_or_create_client()
            response = await client.get(
                self._config.url,
                headers=self._merge_with_useragent(headers),
                timeout=self._config.timeout_seconds,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            return SourceHealth(
                state="degraded",
                last_error=f"probe transient: {exc!s}",
                detail={**previous.get("detail", {}), "probe": "network"},
                last_cursor=cursor.get("etag") or None,
            )

        if response.status_code in (200, 304):
            return SourceHealth(
                state="healthy",
                last_success_at=datetime.now(tz=timezone.utc),
                detail={"status": response.status_code},
                last_cursor=cursor.get("etag") or None,
            )
        if response.status_code in _TRANSIENT_STATUS:
            return SourceHealth(
                state="degraded",
                last_error=f"HTTP {response.status_code}",
                detail={"status": response.status_code},
                last_cursor=cursor.get("etag") or None,
            )
        return SourceHealth(
            state="unhealthy",
            last_error=f"HTTP {response.status_code}",
            detail={"status": response.status_code},
            last_cursor=cursor.get("etag") or None,
        )

    # ------------------------------------------------------- lifecycle hooks

    async def on_configure(self, ctx: SourceContext) -> None:
        """No-op (default). Override for handlers that need per-instance setup."""
        return None

    async def on_activate(self, ctx: SourceContext) -> None:
        return None

    async def on_pause(self, ctx: SourceContext) -> None:
        return None

    async def on_resume(self, ctx: SourceContext) -> None:
        return None

    async def on_retire(self, ctx: SourceContext) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this handler owns one."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:                       # pragma: no cover
                pass
            self._client = None

    # ------------------------------------------------------------- internals

    async def _get_or_create_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = guarded_async_client(
                follow_redirects=True,
                timeout=self._config.timeout_seconds,
                headers={"User-Agent": self._config.user_agent},
            )
        return self._client

    def _merge_with_useragent(self, headers: dict[str, str]) -> dict[str, str]:
        merged = dict(headers)
        merged.setdefault("User-Agent", self._config.user_agent)
        return merged

    async def _load_cursor(self, ctx: SourceContext) -> dict[str, str]:
        raw = await ctx.state_store.get(_GEOJSON_CURSOR_KEY)
        if not isinstance(raw, dict):
            return {}
        return {
            "etag": str(raw.get("etag") or ""),
            "last_modified": str(raw.get("last_modified") or ""),
        }

    @staticmethod
    def _build_conditional_headers(cursor: dict[str, str]) -> dict[str, str]:
        headers: dict[str, str] = {}
        etag = cursor.get("etag") or ""
        last_modified = cursor.get("last_modified") or ""
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    async def _fetch_with_retry(
        self,
        *,
        headers: dict[str, str],
        ctx: SourceContext,
    ) -> httpx.Response | None:
        """Single retry on transient (network / timeout / 5xx-transient)."""
        attempts = 0
        last_err: Exception | None = None
        while attempts <= _DEFAULT_RETRIES_FOR_TRANSIENT:
            attempts += 1
            try:
                client = await self._get_or_create_client()
                response = await client.get(
                    self._config.url,
                    headers=self._merge_with_useragent(headers),
                    timeout=self._config.timeout_seconds,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_err = exc
                ctx.logger.warning(
                    "geojson.fetch.transient attempt=%d url=%s err=%s",
                    attempts, self._config.url, exc,
                )
                if attempts > _DEFAULT_RETRIES_FOR_TRANSIENT:
                    break
                await asyncio.sleep(0)  # cooperative yield
                continue

            if response.status_code in _TRANSIENT_STATUS:
                last_err = httpx.HTTPStatusError(
                    f"transient {response.status_code}",
                    request=response.request,
                    response=response,
                )
                ctx.logger.warning(
                    "geojson.fetch.transient attempt=%d url=%s status=%d",
                    attempts, self._config.url, response.status_code,
                )
                if attempts > _DEFAULT_RETRIES_FOR_TRANSIENT:
                    return response
                await asyncio.sleep(0)
                continue

            return response

        if last_err is not None:
            await self._record_health(
                ctx,
                state="degraded",
                last_error=f"transient retries exhausted: {last_err!s}",
                detail={"url": self._config.url},
            )
        return None

    def _safe_parse(self, body_text: str) -> list[dict[str, Any]] | None:
        """Parse a GeoJSON document into a flat list of Feature dicts.

        Accepts the three RFC 7946 top-level object types:

          * ``FeatureCollection`` → its ``features`` array.
          * ``Feature`` → a single-element list.
          * a bare ``Geometry`` (``Point`` / ``Polygon`` / ...) → wrapped in a
            synthetic ``Feature`` so downstream handling is uniform.

        Returns ``None`` on hard parse failure (not JSON / not a GeoJSON
        object) — the caller maps that to ``unhealthy`` health.
        """
        try:
            doc = json.loads(body_text)
        except (ValueError, TypeError) as exc:
            logger.warning("geojson.parse.not_json: %s", exc)
            return None

        if not isinstance(doc, dict):
            logger.warning("geojson.parse.not_object type=%s", type(doc).__name__)
            return None

        gtype = doc.get("type")
        if gtype == "FeatureCollection":
            feats = doc.get("features")
            if not isinstance(feats, list):
                return None
            return [f for f in feats if isinstance(f, dict)]
        if gtype == "Feature":
            return [doc]
        if gtype in _GEOJSON_GEOMETRY_TYPES:
            # Wrap a bare geometry in a synthetic Feature.
            return [{"type": "Feature", "geometry": doc, "properties": {}}]

        logger.warning("geojson.parse.unknown_type type=%r", gtype)
        return None

    def _feature_to_signal(
        self, feature: dict[str, Any], *, ctx: SourceContext,
    ) -> Signal | None:
        """Map one GeoJSON Feature to a :class:`Signal`.

        The per-feature geometry + properties are inlined in the payload
        (small, structured, and the value of the signal); ``media_ref`` points
        at the SOURCE document URL (a reference, per the modality contract).
        """
        if not isinstance(feature, dict):
            return None

        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            properties = {}

        # A feature with neither geometry nor properties is junk — skip it.
        if geometry is None and not properties:
            return None

        external_id = self._feature_external_id(feature, properties)
        title = _first_str(properties, _TITLE_KEYS) or external_id or "GeoJSON feature"
        geo_codes = _extract_geo(properties)
        # D5: EONET (and other event feeds) ship an authoritative Point/Polygon
        # geometry but NO ISO code in `properties` — so `_extract_geo` returned
        # [] and `signal.geo` was empty for 694/697 EONET features. When the
        # properties carry no geo code, reverse-geocode the feature geometry
        # offline (point-in-country) and stamp the resolved ISO2.
        if not geo_codes:
            iso2 = _country_from_geometry(geometry)
            if iso2:
                geo_codes = [iso2]
        canonical = _first_str(properties, _URL_KEYS) or None

        # Re-wrap as a minimal, self-contained GeoJSON Feature so the UI
        # renderer receives a valid `application/geo+json` fragment directly
        # from the payload (no re-fetch needed to draw the map).
        geojson_feature: dict[str, Any] = {
            "type": "Feature",
            "geometry": geometry,
            "properties": properties,
        }
        if external_id:
            geojson_feature["id"] = external_id

        payload: dict[str, Any] = {
            "external_id": external_id,
            "title": title[:240],
            "geometry_type": (
                geometry.get("type") if isinstance(geometry, dict) else None
            ),
            "properties": properties,
            "geojson": geojson_feature,
            "source_url": self._config.url,
        }

        content_hash = hashlib.sha256(
            json.dumps(
                geojson_feature,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        # Source-first pivot (P-06): the Signal is target-agnostic. We stamp
        # the source identity + the structured/geo+json modality. ``media_ref``
        # is a REFERENCE to the source document; the per-feature geometry lives
        # inline in the payload for the renderer.
        return Signal(
            source_id=ctx.source_id,
            modality="structured",
            mime_type=GEOJSON_MIME_TYPE,
            media_ref=self._config.url,
            payload=payload,
            content_hash=content_hash,
            canonical_url=canonical,
            geo=geo_codes,
            raw_provenance={
                "kind": self.kind,
                "schema_version": self.schema_version,
                "source_url": self._config.url,
                "external_id": external_id,
            },
        )

    def _feature_external_id(
        self, feature: dict[str, Any], properties: dict[str, Any],
    ) -> str:
        """Resolve a stable per-feature id.

        Priority: the configured ``feature_id_key`` in properties, then the
        GeoJSON top-level ``id`` member, then a content hash of the feature.
        """
        key = self._config.feature_id_key
        val = properties.get(key)
        if val is None and key != "id":
            val = feature.get("id")
        elif val is None:
            val = feature.get("id")
        if val is not None and val != "":
            return str(val)
        # Stable fallback: hash the feature body so the same feature gets the
        # same id across pulls (dedupe key).
        digest = hashlib.sha256(
            json.dumps(feature, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return digest[:24]

    async def _record_health(
        self,
        ctx: SourceContext,
        *,
        state: str,
        last_success_at: datetime | None = None,
        last_error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "state": state,
            "last_success_at": (
                last_success_at.astimezone(timezone.utc).isoformat()
                if last_success_at is not None
                else None
            ),
            "last_error": last_error,
            "detail": detail or {},
        }
        try:
            await ctx.state_store.set(_GEOJSON_HEALTH_KEY, record)
        except Exception:                                # pragma: no cover
            ctx.logger.warning("geojson.health.persist_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_str(properties: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value among ``keys`` in ``properties``."""
    for key in keys:
        val = properties.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(val)
    return ""


def _country_from_geometry(geometry: Any) -> str | None:
    """Reverse-geocode a GeoJSON geometry to an ISO-3166-1 alpha-2 country (D5).

    Offline + deterministic (Natural-Earth admin-0 bbox + ray-casting, no
    network). Used only as a fallback when the feature properties carry no ISO
    geo code — e.g. NASA EONET, whose features are bare ``Point``s with an
    event title but no country. Returns ``None`` for open-ocean / unmatched
    points (a mid-Pacific cyclone has no country, which is correct).
    """
    if not isinstance(geometry, dict):
        return None
    pt = representative_point({"geometry": geometry})
    if pt is None:
        return None
    return country_iso2_for_point(pt[0], pt[1])


def _extract_geo(properties: dict[str, Any]) -> list[str]:
    """Pull a list of geo codes (ISO country / admin) from feature properties.

    Best-effort: GeoJSON has no mandated geo-code member, so we probe a set of
    common property keys. The downstream geocode enrichment filter (when the
    descriptor wires one) can refine this from the geometry centroid.
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in _GEO_KEYS:
        val = properties.get(key)
        if isinstance(val, str):
            code = val.strip().upper()
            if code and code not in seen:
                seen.add(code)
                out.append(code)
    return out[:16]


# Protocol-satisfaction sanity check at import — analog of the ACLED handler's
# guard. The handler needs a config to construct, so we use a throwaway one.
assert hasattr(GeoJSONSourceHandler, "pull")
assert hasattr(GeoJSONSourceHandler, "health_check")


__all__ = [
    "GEOJSON_MIME_TYPE",
    "GeoJSONConfig",
    "GeoJSONSourceHandler",
]
