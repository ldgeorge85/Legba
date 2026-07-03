# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""UCDP (Uppsala Conflict Data Program) source handler — S1-T9.

UCDP is a free, no-auth, academically curated dataset of organized-violence
events (state-based conflict, non-state conflict, and one-sided violence)
covering the whole world back to 1989. The Georeferenced Event Dataset (GED)
gives one row per lethal event with geo (lat/long + country + admin), the two
conflict sides (actors), the violence type, and best/high/low fatality
estimates. The handler pulls from the public GED REST API and emits one
:class:`Signal` per event, feeding the escalation (U5) and military_posture
(U6) bounded units.

Reference:
  * API docs / codebook:  https://ucdp.uu.se/apidocs/
  * GED endpoint shape:   https://ucdpapi.pcr.uu.se/api/gedevents/{version}
  * License (CC BY 4.0):  https://ucdp.uu.se/downloads/ (attribution, free reuse)
  * Task row:             planning/PORTFOLIO_AND_INGESTION_EXECUTION_PLAN_2026-07-01.md (S1-T9)
  * Source-kind contract: src/legba/data/sources/_contract.py (L-102 §2)

Key API details (documented shape — the live fetch is integrator-verified
post-deploy per the task; this handler is written against the codebook):

  * Auth: NONE. GED is a public, no-auth endpoint (unlike ACLED which needs an
    OAuth2 account). No SecretRef in the config.
  * URL:  ``{api_base}/{resource}/{version}`` — e.g.
    ``https://ucdpapi.pcr.uu.se/api/gedevents/24.1``. The ``version`` selects
    the dataset release; a candidate (monthly-updated, most-current) release is
    selected by pointing ``version`` at the candidate version string.
  * Response envelope: ``{"TotalCount", "TotalPages", "NextPageUrl",
    "PreviousPageUrl", "Result": [ ...events... ]}``. Pagination follows
    ``NextPageUrl`` (an absolute URL carrying ``pagesize`` + ``page`` + the
    original filters) until it is empty — version/index-agnostic, so we never
    have to guess whether ``page`` is 0- or 1-based.
  * Filters (query params): ``pagesize`` (max 1000), ``StartDate`` /
    ``EndDate`` (YYYY-MM-DD, filter on ``date_start``), ``Country`` (comma-
    joined Gleditsch-Ward country ids), ``Region`` (comma-joined region ids
    1..5). ``type_of_violence`` is applied CLIENT-SIDE (kept out of the query
    string to stay robust to codebook drift).
  * Cursor: ``date_start`` is the natural high-water mark. The handler tracks
    the highest ``date_start`` seen and asks the next pull for
    ``StartDate>=`` that date, relying on downstream dedupe (external_id = the
    UCDP event ``id`` + content_hash) for the same-day overlap.

The handler does not write to the substrate directly — that's the runtime's
job. It yields :class:`Signal` instances; the runtime wires them through the
filter chain (dedupe + geocode enrichment) and into the shared signal pool.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator, ClassVar, Mapping
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import (
    Signal,
    SourceContext,
    SourceHandler,
    SourceHealth,
)
from ._egress import guarded_async_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


#: Public GED API host + path prefix (no auth). Confirmed as the documented
#: endpoint in scripts/seed_sources.py ("UCDP API").
UCDP_API_BASE: str = "https://ucdpapi.pcr.uu.se/api"

#: Default GED dataset version. The endpoint is ``{api_base}/{resource}/{version}``.
#: Point ``version`` at a candidate release string for the most-current
#: (monthly-updated) UCDP Candidate Events Dataset.
UCDP_DEFAULT_VERSION: str = "24.1"

#: Event-bearing GED resources this handler knows how to shape into Signals.
#: ``gedevents`` is the Georeferenced Event Dataset (one row per lethal event,
#: with geo + actors + fatalities); the candidate dataset is served from the
#: same resource under a candidate ``version``.
UCDP_RESOURCES: frozenset[str] = frozenset({"gedevents"})

#: Hard per-page cap the GED API enforces.
UCDP_PAGE_SIZE_MAX: int = 1000

#: UCDP ``type_of_violence`` codes → human label. 1 = state-based armed
#: conflict, 2 = non-state conflict, 3 = one-sided violence (codebook §type).
UCDP_VIOLENCE_TYPES: dict[int, str] = {
    1: "state-based",
    2: "non-state",
    3: "one-sided",
}

#: UCDP region ids (codebook): 1 Europe, 2 Middle East, 3 Asia, 4 Africa,
#: 5 Americas.
UCDP_REGION_IDS: frozenset[int] = frozenset({1, 2, 3, 4, 5})


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class UCDPConfig(BaseModel):
    """UCDP GED source descriptor config.

    No credentials — GED is a public, no-auth endpoint. A descriptor MAY leave
    every filter unset (the full global feed; consuming targets narrow by their
    Subscription.geo). ``type_of_violence`` filters client-side; ``country_ids``
    / ``region_ids`` ride the query string.
    """

    model_config = ConfigDict(extra="forbid")

    api_base: str = Field(
        default=UCDP_API_BASE,
        description="GED API host + path prefix (override only if UCDP moves it).",
    )
    resource: str = Field(
        default="gedevents",
        description="GED resource. Must be one of UCDP_RESOURCES.",
    )
    version: str = Field(
        default=UCDP_DEFAULT_VERSION,
        min_length=1,
        max_length=64,
        description=(
            "Dataset release version, e.g. '24.1'. Point at a candidate release "
            "string for the most-current (monthly) UCDP Candidate Events Dataset."
        ),
    )
    country_ids: list[int] | None = Field(
        default=None,
        description=(
            "Gleditsch-Ward country-id filter (comma-joined into the "
            "``Country`` query param). None = no filter."
        ),
    )
    region_ids: list[int] | None = Field(
        default=None,
        description=(
            "UCDP region-id filter (1..5; comma-joined into the ``Region`` "
            "query param). Each entry must be in UCDP_REGION_IDS. None = no filter."
        ),
    )
    type_of_violence: list[int] | None = Field(
        default=None,
        description=(
            "Violence-type filter (subset of {1,2,3}); applied CLIENT-SIDE after "
            "fetch. None = no filter."
        ),
    )
    lookback_days: int = Field(
        default=365,
        ge=1,
        le=3650,
        description=(
            "Days back from `since` (or now) to request when the handler has no "
            "cursor yet. GED is released ~annually (candidate ~monthly), so a "
            "wide default is fine — dedupe absorbs the overlap."
        ),
    )
    page_size: int = Field(
        default=UCDP_PAGE_SIZE_MAX,
        ge=1,
        le=UCDP_PAGE_SIZE_MAX,
        description="Per-page record cap. GED enforces a hard ceiling of 1000.",
    )
    max_pages: int = Field(
        default=500,
        ge=1,
        le=100000,
        description=(
            "Safety cap on pages walked per pull so a mis-scoped (unfiltered) "
            "descriptor can't spin the full multi-hundred-thousand-row GED in "
            "one tick. Raise for a deliberate full backfill."
        ),
    )
    request_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=300,
        description="Per-HTTP-request timeout.",
    )

    @field_validator("resource")
    @classmethod
    def _validate_resource(cls, value: str) -> str:
        if value not in UCDP_RESOURCES:
            raise ValueError(
                f"unknown UCDP resource: {value!r}; valid: {sorted(UCDP_RESOURCES)!r}"
            )
        return value

    @field_validator("region_ids")
    @classmethod
    def _validate_region_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        bad = [v for v in value if v not in UCDP_REGION_IDS]
        if bad:
            raise ValueError(
                f"unknown UCDP region_ids: {bad!r}; valid: {sorted(UCDP_REGION_IDS)!r}"
            )
        return value

    @field_validator("type_of_violence")
    @classmethod
    def _validate_violence(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        bad = [v for v in value if v not in UCDP_VIOLENCE_TYPES]
        if bad:
            raise ValueError(
                f"unknown type_of_violence: {bad!r}; "
                f"valid: {sorted(UCDP_VIOLENCE_TYPES)!r}"
            )
        return value


# ---------------------------------------------------------------------------
# Cursor — what the handler persists between pulls
# ---------------------------------------------------------------------------


@dataclass
class _Cursor:
    """Persistent state shape stored under key ``"ucdp_cursor"``.

    * ``last_date_start``: highest ``date_start`` (ISO date) seen across pages
      so far. Drives the next pull's ``StartDate>=`` lower bound; downstream
      dedupe (external_id = UCDP event id) absorbs the same-day overlap.
    * ``next_page_url``: the ``NextPageUrl`` the API handed us mid-pull, so a
      crash/eviction mid-stream resumes on the same page rather than restarting
      the whole date window. Cleared to None when a pull drains fully.
    """

    last_date_start: str | None = None
    next_page_url: str | None = None

    @classmethod
    def from_state(cls, raw: Any) -> "_Cursor":
        if not raw:
            return cls()
        if isinstance(raw, _Cursor):
            return raw
        if isinstance(raw, Mapping):
            return cls(
                last_date_start=raw.get("last_date_start"),
                next_page_url=raw.get("next_page_url"),
            )
        return cls()

    def to_state(self) -> dict[str, Any]:
        return {
            "last_date_start": self.last_date_start,
            "next_page_url": self.next_page_url,
        }


# ---------------------------------------------------------------------------
# Health counters
# ---------------------------------------------------------------------------


@dataclass
class _HealthCounters:
    """Mutable counters the handler keeps for the health probe."""

    last_success_at: datetime | None = None
    last_error: str | None = None
    pull_window: list[datetime] = field(default_factory=list)  # rolling 24h
    last_status: str = "healthy"

    def record_pull(self, ts: datetime, count: int) -> None:
        self.last_success_at = ts
        cutoff = ts - timedelta(hours=24)
        for _ in range(count):
            self.pull_window.append(ts)
        self.pull_window = [t for t in self.pull_window if t >= cutoff]
        self.last_status = "healthy"
        self.last_error = None

    def record_error(self, err: str) -> None:
        self.last_error = err
        self.last_status = "degraded"

    def rows_24h(self, now: datetime) -> int:
        cutoff = now - timedelta(hours=24)
        return sum(1 for t in self.pull_window if t >= cutoff)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class UCDPSourceHandler:
    """Source handler for the UCDP GED conflict-event REST API.

    Implements :class:`SourceHandler` (L-102 §2). Stateless between calls;
    cursor state lives in ``ctx.state_store`` under the key ``"ucdp_cursor"``.

      * ``pull(ctx, since)`` walks the GED pages (following ``NextPageUrl``),
        yields one :class:`Signal` per event, and persists cursor advancement
        after each page.
      * ``health_check(ctx)`` issues a 1-record probe to verify reachability +
        envelope shape, and reports the rolling 24h pull volume.

    Lifecycle hooks are no-ops; the handler has no cross-call state to set up.
    """

    kind: ClassVar[str] = "ucdp"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.ucdp/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = UCDPConfig

    #: Unit-test hook — a callable returning a configured ``httpx.AsyncClient``
    #: (e.g. backed by ``MockTransport``). Production leaves this None so the
    #: SSRF-guarded client is used.
    _http_client_factory: ClassVar[Any] = None

    def __init__(self) -> None:
        self._health = _HealthCounters()

    # ------------------------------------------------------------------
    # Lifecycle hooks — runtime-optional. L-102 §1.
    # ------------------------------------------------------------------

    async def on_configure(self, ctx: SourceContext) -> None:  # pragma: no cover - trivial
        return None

    async def on_activate(self, ctx: SourceContext) -> None:  # pragma: no cover - trivial
        return None

    async def on_pause(self, ctx: SourceContext) -> None:  # pragma: no cover - trivial
        return None

    async def on_resume(self, ctx: SourceContext) -> None:  # pragma: no cover - trivial
        return None

    async def on_retire(self, ctx: SourceContext) -> None:  # pragma: no cover - trivial
        return None

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Walk the GED pages and yield one Signal per event.

        ``since`` is a hint, not a contract: GED filters on ``date_start``
        (day precision), so same-day events may re-emit across pulls —
        downstream dedupe (external_id + content_hash) handles that.
        """
        config = self._coerce_config(ctx)
        cursor = _Cursor.from_state(await ctx.state_store.get("ucdp_cursor"))
        floor_dt = self._effective_floor(since, cursor, config)
        violence_filter = set(config.type_of_violence or [])

        highest_date_start = cursor.last_date_start
        api_host = httpx.URL(config.api_base).host

        async with self._open_client(config) as client:
            # Resume from a mid-pull NextPageUrl if we have one and it points at
            # the same host; otherwise build the first-page URL from the floor.
            url: str | None
            params: dict[str, Any] | None
            if cursor.next_page_url and httpx.URL(cursor.next_page_url).host == api_host:
                url, params = cursor.next_page_url, None
            else:
                url, params = self._first_page_url(config), self._build_params(
                    config=config, floor_dt=floor_dt
                )

            pages = 0
            while url is not None and pages < config.max_pages:
                ctx.logger.debug(
                    "UCDP pull page=%s floor=%s resource=%s version=%s",
                    pages,
                    floor_dt.date().isoformat(),
                    config.resource,
                    config.version,
                )
                try:
                    records, next_url = await self._fetch_page(
                        client, url, params, config
                    )
                except httpx.HTTPError as err:
                    self._health.record_error(repr(err))
                    raise

                # After the first request, pagination rides NextPageUrl (params
                # already embedded), so clear the explicit params.
                params = None
                pages += 1

                fetched_at = self._now(ctx)
                emitted = 0
                for record in records:
                    ds = _date_str(record.get("date_start"))
                    if ds and (highest_date_start is None or ds > highest_date_start):
                        highest_date_start = ds
                    if violence_filter:
                        tov = _to_int(record.get("type_of_violence"))
                        if tov not in violence_filter:
                            continue
                    yield self._record_to_signal(
                        record=record, ctx=ctx, fetched_at=fetched_at
                    )
                    emitted += 1

                self._health.record_pull(fetched_at, emitted)

                # Only follow NextPageUrl when it stays on the configured host —
                # keeps a compromised API from redirecting our pagination
                # elsewhere (the SSRF guard already blocks internal targets).
                if next_url and httpx.URL(next_url).host == api_host:
                    url = next_url
                else:
                    url = None

                # Persist progress; a mid-pull crash resumes on `url`.
                await ctx.state_store.set(
                    "ucdp_cursor",
                    _Cursor(
                        last_date_start=highest_date_start,
                        next_page_url=url,
                    ).to_state(),
                )

            # Drained (or hit the page cap): clear the mid-pull page pointer so
            # the next pull restarts from the advanced date high-water.
            await ctx.state_store.set(
                "ucdp_cursor",
                _Cursor(last_date_start=highest_date_start, next_page_url=None).to_state(),
            )

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Issue a single-record probe; cheap call exercising reachability."""
        config = self._coerce_config(ctx)
        now = self._now(ctx)
        probe_params = {"pagesize": 1}
        url = self._first_page_url(config)

        try:
            async with self._open_client(config) as client:
                resp = await client.get(url, params=probe_params)
                resp.raise_for_status()
                body = resp.json()
                ok = isinstance(body, dict) and isinstance(
                    body.get("Result", body.get("result")), list
                )
        except httpx.HTTPError as err:
            self._health.record_error(repr(err))
            return SourceHealth(
                state="unhealthy",
                last_success_at=self._health.last_success_at,
                last_error=repr(err),
                rows_pulled_24h=self._health.rows_24h(now),
            )

        if not ok:
            return SourceHealth(
                state="degraded",
                last_success_at=self._health.last_success_at,
                last_error="ucdp body missing/invalid Result array",
                rows_pulled_24h=self._health.rows_24h(now),
            )

        result = body.get("Result", body.get("result")) or []
        return SourceHealth(
            state="healthy",
            last_success_at=self._health.last_success_at,
            last_error=None,
            rows_pulled_24h=self._health.rows_24h(now),
            detail={
                "probe_records": len(result),
                "total_count": body.get("TotalCount", body.get("totalCount")),
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_config(ctx: SourceContext) -> UCDPConfig:
        """Parse the runtime's raw passthrough ``ctx.config`` into UCDPConfig.

        The runtime supplies ``ctx.config`` as a ``_RawConfig`` open model
        carrying property-factory-wrapped values (``{"raw": <value>}``). We
        unwrap each field to its ``raw`` payload, then validate. Unit tests
        pass a real ``UCDPConfig`` — the isinstance guard returns it untouched.
        """
        raw = getattr(ctx, "config", None)
        if isinstance(raw, UCDPConfig):
            return raw
        data = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw or {})
        unwrapped = {
            k: (v["raw"] if isinstance(v, dict) and "raw" in v else v)
            for k, v in data.items()
        }
        return UCDPConfig.model_validate(unwrapped)

    @staticmethod
    def _now(ctx: SourceContext) -> datetime:
        return ctx.utcnow() if hasattr(ctx, "utcnow") else datetime.now(tz=timezone.utc)

    @staticmethod
    def _first_page_url(config: UCDPConfig) -> str:
        return f"{config.api_base.rstrip('/')}/{config.resource}/{config.version}"

    def _effective_floor(
        self,
        since: datetime | None,
        cursor: _Cursor,
        config: UCDPConfig,
    ) -> datetime:
        """Floor date for the ``StartDate>=`` filter.

        Precedence: cursor.last_date_start (resume) > ``since`` > now - lookback.
        """
        if cursor.last_date_start:
            try:
                return datetime.fromisoformat(cursor.last_date_start).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            return since
        return datetime.now(tz=timezone.utc) - timedelta(days=config.lookback_days)

    def _build_params(
        self,
        *,
        config: UCDPConfig,
        floor_dt: datetime,
    ) -> dict[str, Any]:
        """Construct the GED first-page query params.

        ``StartDate`` filters on ``date_start``; ``Country`` / ``Region`` take
        comma-joined id lists. ``type_of_violence`` is NOT sent (client-side).
        """
        params: dict[str, Any] = {
            "pagesize": config.page_size,
            "StartDate": floor_dt.date().isoformat(),
        }
        if config.country_ids:
            params["Country"] = ",".join(str(c) for c in config.country_ids)
        if config.region_ids:
            params["Region"] = ",".join(str(r) for r in config.region_ids)
        return params

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, Any] | None,
        config: UCDPConfig,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of GED events + the NextPageUrl (or None at the end)."""
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise httpx.HTTPError(
                f"UCDP unexpected payload type: {type(body).__name__}"
            )
        # UCDP documents PascalCase keys; tolerate a camelCase variant too.
        result = body.get("Result", body.get("result"))
        if result is None:
            result = []
        if not isinstance(result, list):
            raise httpx.HTTPError("UCDP body.Result is not a list")
        next_url = body.get("NextPageUrl", body.get("nextPageUrl"))
        if not next_url or not str(next_url).strip():
            next_url = None
        return result, (str(next_url) if next_url is not None else None)

    def _open_client(self, config: UCDPConfig) -> httpx.AsyncClient:
        """Open an httpx.AsyncClient honoring the unit-test factory override."""
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return guarded_async_client(
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": f"legba-source-ucdp/{self.handler_version}"},
        )

    def _record_to_signal(
        self,
        *,
        record: dict[str, Any],
        ctx: SourceContext,
        fetched_at: datetime,
    ) -> Signal:
        """Translate one GED event into a :class:`Signal`."""
        # UCDP event ``id`` is an int and can be 0-adjacent in test data; use an
        # explicit None/"" check so a zero id doesn't fall back incorrectly.
        raw_id = record.get("id")
        if raw_id is None or raw_id == "":
            raw_id = record.get("relid") or ""
        event_id = str(raw_id)

        tov = _to_int(record.get("type_of_violence"))
        event_type = UCDP_VIOLENCE_TYPES.get(tov or -1, "organized violence")
        latitude = _to_float(record.get("latitude"))
        longitude = _to_float(record.get("longitude"))
        best = _to_int(record.get("best"))
        published_at = _parse_ucdp_dt(record.get("date_start"))

        payload: dict[str, Any] = {
            "event_type": event_type,
            "type_of_violence": tov,
            "conflict_name": record.get("conflict_name") or "",
            "dyad_name": record.get("dyad_name") or "",
            "actors": {
                "side_a": record.get("side_a") or "",
                "side_b": record.get("side_b") or "",
                "side_a_2nd": record.get("side_a_2nd") or "",
                "side_b_2nd": record.get("side_b_2nd") or "",
            },
            "geo": {
                "latitude": latitude,
                "longitude": longitude,
                "country": record.get("country") or "",
                "country_id": record.get("country_id"),
                "region": record.get("region") or "",
                "adm_1": record.get("adm_1") or "",
                "adm_2": record.get("adm_2") or "",
                "where_description": record.get("where_description") or "",
                "where_coordinates": record.get("where_coordinates") or "",
            },
            "fatalities": best,
            "fatalities_estimates": {
                "best": best,
                "high": _to_int(record.get("high")),
                "low": _to_int(record.get("low")),
                "deaths_a": _to_int(record.get("deaths_a")),
                "deaths_b": _to_int(record.get("deaths_b")),
                "deaths_civilians": _to_int(record.get("deaths_civilians")),
                "deaths_unknown": _to_int(record.get("deaths_unknown")),
            },
            "date_start": _date_str(record.get("date_start")) or "",
            "date_end": _date_str(record.get("date_end")) or "",
            "date_prec": record.get("date_prec"),
            "source_headline": record.get("source_headline") or "",
            "source_article": record.get("source_article") or "",
            "number_of_sources": _to_int(record.get("number_of_sources")),
            "external_id": event_id,
            "title": _build_title(record, event_type),
            "raw": record,
        }
        content_hash = _content_hash(record, event_id)

        return Signal(
            signal_id=uuid4(),
            source_id=ctx.source_id,
            fetched_at=fetched_at,
            payload=payload,
            content_hash=content_hash,
            canonical_url=None,
            language_hint=None,
            raw_provenance={
                "kind": self.kind,
                "schema_version": self.schema_version,
                "external_id": event_id,
                "published_at": (
                    published_at.isoformat() if published_at else None
                ),
            },
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        # UCDP fatality fields are ints, but be defensive about "12.0" strings.
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _date_str(value: Any) -> str | None:
    """Normalize a UCDP date/datetime to a ``YYYY-MM-DD`` string (or None).

    GED ``date_start`` / ``date_end`` arrive as ``"2023-05-10"`` or
    ``"2023-05-10 00:00:00.000"`` (or ``"...T..."``). We keep the date part so
    the cursor comparison is a plain lexical ``>`` on ISO dates.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Split on the first space or 'T' to drop any time component.
    for sep in (" ", "T"):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s[:10] or None


def _parse_ucdp_dt(value: Any) -> datetime | None:
    ds = _date_str(value)
    if not ds:
        return None
    try:
        return datetime.fromisoformat(ds).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _build_title(record: dict[str, Any], event_type: str) -> str:
    """Compose a human-readable title. GED has no title field; synthesize one.

    Format: ``"<conflict_name> (<event_type>) in <where>, <country>"`` truncated
    at 240 chars.
    """
    conflict = (record.get("conflict_name") or "").strip()
    head = conflict or f"UCDP {event_type} event"
    where = (record.get("where_description") or "").strip()
    country = (record.get("country") or "").strip()
    parts = [head]
    if event_type and not conflict:
        # event_type already folded into head when there's no conflict name.
        pass
    elif event_type:
        parts.append(f"({event_type})")
    if where:
        parts.append(f"in {where}")
    if country:
        parts.append(f", {country}")
    title = " ".join(parts).replace(" ,", ",").strip()
    return title[:240]


def _content_hash(record: dict[str, Any], external_id: str) -> str:
    """SHA-256 over a stable subset of GED fields.

    Includes the event id + the content fields that a UCDP revision (a
    fatality re-estimate, an actor correction) would change, so downstream
    dedupe (L-151) recognizes the revision as a new payload rather than a
    duplicate of the prior version.
    """
    canonical = json.dumps(
        {
            "id": external_id,
            "date_start": _date_str(record.get("date_start")),
            "date_end": _date_str(record.get("date_end")),
            "type_of_violence": record.get("type_of_violence"),
            "conflict_name": record.get("conflict_name"),
            "dyad_name": record.get("dyad_name"),
            "side_a": record.get("side_a"),
            "side_b": record.get("side_b"),
            "country": record.get("country"),
            "adm_1": record.get("adm_1"),
            "latitude": record.get("latitude"),
            "longitude": record.get("longitude"),
            "best": record.get("best"),
            "high": record.get("high"),
            "low": record.get("low"),
            "deaths_civilians": record.get("deaths_civilians"),
            "source_headline": record.get("source_headline"),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Protocol satisfaction sanity-check (cheap, runs at import — analog of the
# registration audit done by L-160 once the runtime lands).
assert isinstance(UCDPSourceHandler(), SourceHandler)  # type: ignore[arg-type]


__all__ = [
    "UCDP_API_BASE",
    "UCDP_DEFAULT_VERSION",
    "UCDP_PAGE_SIZE_MAX",
    "UCDP_REGION_IDS",
    "UCDP_RESOURCES",
    "UCDP_VIOLENCE_TYPES",
    "UCDPConfig",
    "UCDPSourceHandler",
]
