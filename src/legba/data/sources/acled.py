# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACLED (Armed Conflict Location & Event Data) source handler — L-132.

ACLED is a free / non-commercial dataset of conflict, protest, and political
violence events covering 230+ countries with weekly updates. The handler
pulls from the REST API at ``api.acleddata.com/acled/read`` and emits one
:class:`Signal` per ACLED record.

Reference:
  * API docs:                https://acleddata.com/acled-api/
  * License (non-commercial): https://acleddata.com/terms-of-use/
  * Pillar / phase context:  /usr/local/deployments/myshit/personal/plans/pillar_legba.md
  * Source-kind contract:    /usr/local/deployments/myshit/personal/plans/design/legba_kind_contracts.md §2
  * Task row:                task_tracker.md §15.3 row L-132

Key API details:

  * Auth: per-call ``key`` + ``email`` query params (per ACLED's identification
    requirement for rate-limit attribution).
  * Pagination: ``page`` parameter is 1-indexed; ``limit`` caps the per-page
    record count (max 5000). Empty ``data: []`` array signals end-of-pages.
  * Rate limit: 1000 requests / day at the free tier; ACLED returns HTTP 429
    on overrun. The handler honors a ``Retry-After`` header when present and
    otherwise applies exponential back-off.
  * Cursor: ``event_date`` is sortable; handler queries with
    ``event_date>=since-date`` and tracks the highest seen ``event_date`` plus
    the per-day page cursor so a paused/resumed pull restarts mid-way.

The handler does not write to the substrate directly — that's the runtime's
job. It yields :class:`Signal` instances; the runtime wires them through the
filter chain and ultimately into ``write_target_signal`` (L-001).
"""

from __future__ import annotations

import asyncio
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


#: OAuth2 password-grant token endpoint. ACLED migrated OFF the legacy
#: api-key-as-query-param method; the read API now authenticates with a Bearer
#: token obtained from the account username (email) + password. Confirmed live
#: 2026-06-24 (the legacy ``api.acleddata.com/acled/read`` host now TLS-errors).
ACLED_OAUTH_TOKEN_URL: str = "https://acleddata.com/oauth/token"

#: OAuth2 public client id for the password grant.
ACLED_OAUTH_CLIENT_ID: str = "acled"

#: OAuth2 read endpoint (Bearer-authed). Confirmed live 2026-06-24.
ACLED_API_BASE: str = "https://acleddata.com/api/acled/read"

#: Max records the ACLED API will return per page. Hard cap from the API.
ACLED_PAGE_SIZE_MAX: int = 5000

#: ACLED's canonical event-type strings. The descriptor's `event_types` filter
#: is validated against this set so a typo in the descriptor surfaces at
#: registration (per L-101 conversion-webhook contract), not at first pull.
ACLED_EVENT_TYPES: frozenset[str] = frozenset({
    "Battles",
    "Explosions/Remote violence",
    "Violence against civilians",
    "Protests",
    "Riots",
    "Strategic developments",
})

#: ACLED's canonical region names. Matches the values returned by the API's
#: ``region`` field.
ACLED_REGIONS: frozenset[str] = frozenset({
    "Western Africa",
    "Middle Africa",
    "Eastern Africa",
    "Southern Africa",
    "Northern Africa",
    "South Asia",
    "Southeast Asia",
    "East Asia",
    "Middle East",
    "Europe",
    "Caucasus and Central Asia",
    "Central America",
    "South America",
    "Caribbean",
    "North America",
    "Oceania",
    "Antarctica",
})


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class ACLEDConfig(BaseModel):
    """ACLED source descriptor config.

    Per the L-101 property-factory catalog, ``api_key_secret`` is a SecretRef
    string (the descriptor stores only the vault id; the runtime resolves to
    the live key at call time per L-102 §7). ``email`` is plain Text — ACLED
    requires it for rate-limit identification and rejects bare API keys.

    The remaining fields are ACLED query filters. A descriptor MAY leave any
    of them unset; the handler simply omits the corresponding query param.
    """

    model_config = ConfigDict(extra="forbid")

    username_secret: str = Field(
        ...,
        min_length=1,
        description=(
            "Vault reference (credential_id) for the ACLED account username — "
            "the registration email — used as the OAuth2 password-grant username."
        ),
    )
    password_secret: str = Field(
        ...,
        min_length=1,
        description="Vault reference (credential_id) for the ACLED account password.",
    )
    client_id: str = Field(
        default=ACLED_OAUTH_CLIENT_ID,
        min_length=1,
        description="OAuth2 client id for the password grant (public; 'acled').",
    )
    token_url: str = Field(
        default=ACLED_OAUTH_TOKEN_URL,
        description="ACLED OAuth2 token endpoint (override only if ACLED moves it).",
    )
    api_base: str = Field(
        default=ACLED_API_BASE,
        description="ACLED OAuth2 read endpoint (override only if ACLED moves it).",
    )
    email: str | None = Field(
        default=None,
        max_length=320,
        description=(
            "Optional contact email for ToS attribution. The OAuth2 read API "
            "authenticates via the Bearer token, so this is NO LONGER sent as a "
            "query param; retained for operator reference only."
        ),
    )

    country: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="ISO 3166-1 alpha-3 country filter (e.g. 'BRA'). Optional.",
    )
    event_types: list[str] | None = Field(
        default=None,
        description=(
            "ACLED event-type filter. Each entry must be one of "
            "ACLED_EVENT_TYPES. Translated into a colon-joined ``event_type`` "
            "query param. None = no filter."
        ),
    )
    region: str | None = Field(
        default=None,
        description="ACLED region filter. Must be in ACLED_REGIONS if set.",
    )
    lookback_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description=(
            "Days back from `since` (or now) to request when the handler has "
            "no cursor yet. ACLED publishes weekly so 7 is the natural default."
        ),
    )
    page_size: int = Field(
        default=ACLED_PAGE_SIZE_MAX,
        ge=1,
        le=ACLED_PAGE_SIZE_MAX,
        description="Per-page record cap. ACLED enforces a hard ceiling of 5000.",
    )
    request_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=300,
        description="Per-HTTP-request timeout.",
    )

    @field_validator("event_types")
    @classmethod
    def _validate_event_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        bad = [v for v in value if v not in ACLED_EVENT_TYPES]
        if bad:
            raise ValueError(
                f"unknown ACLED event_types: {bad!r}; "
                f"valid: {sorted(ACLED_EVENT_TYPES)!r}"
            )
        return value

    @field_validator("region")
    @classmethod
    def _validate_region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in ACLED_REGIONS:
            raise ValueError(
                f"unknown ACLED region: {value!r}; valid: {sorted(ACLED_REGIONS)!r}"
            )
        return value


# ---------------------------------------------------------------------------
# Cursor — what the handler persists between pulls
# ---------------------------------------------------------------------------


@dataclass
class _Cursor:
    """Persistent state shape stored under key ``"acled_cursor"``.

    * ``last_event_date``: highest ``event_date`` (ISO date) seen across pages
      so far. Drives the next pull's lower bound (we ask for events on/after
      this date and rely on ``external_id`` dedupe downstream for the same-day
      overlap).
    * ``page``: per-pull page cursor. Reset to 1 between pulls; persisted only
      so a crash mid-pull resumes near where it left off. (ACLED ordering is
      stable enough to make this resumption safe.)
    """

    last_event_date: str | None = None
    page: int = 1

    @classmethod
    def from_state(cls, raw: Any) -> "_Cursor":
        if not raw:
            return cls()
        if isinstance(raw, _Cursor):
            return raw
        if isinstance(raw, Mapping):
            return cls(
                last_event_date=raw.get("last_event_date"),
                page=int(raw.get("page", 1) or 1),
            )
        return cls()

    def to_state(self) -> dict[str, Any]:
        return {
            "last_event_date": self.last_event_date,
            "page": self.page,
        }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@dataclass
class _HealthCounters:
    """Mutable counters the handler keeps for the health probe."""

    last_success_at: datetime | None = None
    last_error: str | None = None
    pull_window: list[datetime] = field(default_factory=list)  # rolling 24h
    rate_limit_remaining: int | None = None
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


class ACLEDSourceHandler:
    """Source handler for the ACLED conflict-event REST API.

    Implements :class:`SourceHandler` (L-102 §2). The handler is stateless
    between calls; cursor state lives in ``ctx.state_store`` under the key
    ``"acled_cursor"``.

    Concrete protocol satisfaction:

      * ``pull(ctx, since)`` paginates the API, yields one :class:`Signal`
        per ACLED record, and persists cursor advancement back to the state
        store after each page.
      * ``health_check(ctx)`` issues a 1-record probe to verify auth +
        reachability, and reports the rolling 24h pull volume.

    Lifecycle hooks (``on_configure`` ... ``on_retire``) are implemented as
    no-ops; the runtime executor (L-160) is free to call them but the
    handler has no cross-call state to set up or tear down.
    """

    kind: ClassVar[str] = "acled"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.acled/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = ACLEDConfig

    #: Class-attribute override used by unit tests — when set to a callable
    #: returning a configured ``httpx.AsyncClient`` (e.g. with a
    #: ``MockTransport``), the handler uses that instead of opening a real
    #: HTTP connection. The runtime in production leaves this as ``None``.
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
        """Paginate the ACLED API and yield one Signal per record.

        ``since`` is a hint, not a contract: ACLED filters on ``event_date``
        (day-precision), so we may re-emit same-day events seen on previous
        pulls — downstream dedupe (L-151) handles that via ``external_id``
        + ``content_hash``.
        """
        config = self._coerce_config(ctx)

        # Cursor: start from stored cursor if newer than the caller's `since`,
        # otherwise from `since - lookback_days` (or `now - lookback_days` if
        # `since` is None).
        cursor = _Cursor.from_state(await ctx.state_store.get("acled_cursor"))
        floor_dt = self._effective_floor(since, cursor, config)

        page = cursor.page if cursor.last_event_date else 1
        highest_event_date = cursor.last_event_date

        async with self._open_client(config) as client:
            token = await self._fetch_token(client, ctx, config)
            auth_headers = {"Authorization": f"Bearer {token}"}
            while True:
                params = self._build_params(
                    config=config,
                    floor_dt=floor_dt,
                    page=page,
                )
                ctx.logger.debug(
                    "ACLED pull page=%s floor=%s country=%s region=%s",
                    page,
                    floor_dt.date().isoformat(),
                    config.country,
                    config.region,
                )
                try:
                    records = await self._fetch_page(client, params, config, auth_headers)
                except httpx.HTTPError as err:
                    self._health.record_error(repr(err))
                    raise

                if not records:
                    # End of result set — reset the page cursor for the next pull.
                    await ctx.state_store.set(
                        "acled_cursor",
                        _Cursor(last_event_date=highest_event_date, page=1).to_state(),
                    )
                    return

                fetched_at = ctx.utcnow() if hasattr(ctx, "utcnow") else datetime.now(tz=timezone.utc)
                for record in records:
                    signal = self._record_to_signal(
                        record=record,
                        ctx=ctx,
                        config=config,
                        fetched_at=fetched_at,
                    )
                    ev_date = (record.get("event_date") or "").strip()
                    if ev_date and (highest_event_date is None or ev_date > highest_event_date):
                        highest_event_date = ev_date
                    yield signal

                self._health.record_pull(fetched_at, len(records))

                # Persist progress; next page.
                page += 1
                await ctx.state_store.set(
                    "acled_cursor",
                    _Cursor(last_event_date=highest_event_date, page=page).to_state(),
                )

                # If the API returned fewer than page_size, no more pages.
                if len(records) < config.page_size:
                    await ctx.state_store.set(
                        "acled_cursor",
                        _Cursor(last_event_date=highest_event_date, page=1).to_state(),
                    )
                    return

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Issue a single-record probe; cheap call that exercises auth + net."""
        config = self._coerce_config(ctx)
        now = ctx.utcnow() if hasattr(ctx, "utcnow") else datetime.now(tz=timezone.utc)

        try:
            async with self._open_client(config) as client:
                try:
                    token = await self._fetch_token(client, ctx, config)
                except Exception as err:                            # noqa: BLE001
                    return SourceHealth(
                        state="unhealthy",
                        last_success_at=self._health.last_success_at,
                        last_error=f"oauth_token_failed: {err!r}",
                        rows_pulled_24h=self._health.rows_24h(now),
                    )
                resp = await client.get(
                    config.api_base,
                    params={"limit": 1},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 429:
                    self._health.record_error("rate_limited (429)")
                    return SourceHealth(
                        state="degraded",
                        last_success_at=self._health.last_success_at,
                        last_error="rate_limited (HTTP 429)",
                        rows_pulled_24h=self._health.rows_24h(now),
                    )
                resp.raise_for_status()
                body = resp.json()
                ok = bool(body.get("success", True)) and isinstance(
                    body.get("data"), list
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
                last_error="acled body missing/invalid data array",
                rows_pulled_24h=self._health.rows_24h(now),
            )

        return SourceHealth(
            state="healthy",
            last_success_at=self._health.last_success_at,
            last_error=None,
            rows_pulled_24h=self._health.rows_24h(now),
            detail={"probe_records": len(body.get("data", []))},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_config(ctx: SourceContext) -> ACLEDConfig:
        """Parse the runtime's raw passthrough ``ctx.config`` into the typed
        ``ACLEDConfig``.

        The runtime supplies ``ctx.config`` as a ``_RawConfig`` open model
        carrying property-factory-wrapped values
        (``{"factory_kind": ..., "raw": <value>}``) — NOT a parsed
        ``ACLEDConfig``. Reading those attributes raw (e.g. ``lookback_days``
        as the wrapper dict) is the bug that kept this handler from ever
        polling. We unwrap each field to its ``raw`` payload, then validate.
        Re-implemented locally (rather than importing the runtime's
        ``source_factory._unwrap_factory_dict``) to keep the data→runtime
        layering one-directional. Unit tests pass a real ``ACLEDConfig`` as
        ``ctx.config`` — the isinstance guard returns it untouched.
        """
        raw = getattr(ctx, "config", None)
        if isinstance(raw, ACLEDConfig):
            return raw
        data = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw or {})
        unwrapped = {
            k: (v["raw"] if isinstance(v, dict) and "raw" in v else v)
            for k, v in data.items()
        }
        return ACLEDConfig.model_validate(unwrapped)

    async def _resolve_secret(self, ctx: SourceContext, vault_id: str) -> str:
        """Resolve a SecretRef vault id to its plaintext via the runtime's
        ``secrets_resolve`` coroutine (per L-102 §7). Honors a literal
        pass-through when no resolver is supplied (unit tests / bootstrap);
        real descriptors always carry a vault id.
        """
        resolver = getattr(ctx, "secrets_resolve", None)
        if resolver is None:
            return vault_id
        return await resolver(vault_id)

    async def _fetch_token(
        self,
        client: httpx.AsyncClient,
        ctx: SourceContext,
        config: ACLEDConfig,
    ) -> str:
        """OAuth2 password grant → Bearer access token.

        ACLED migrated off the legacy api-key-as-query-param method: the read
        API now authenticates with a Bearer token obtained by posting the
        account username (email) + password to the OAuth2 token endpoint.
        Tokens last ~24h and the actor's pull cadence is far longer, so a fresh
        token per pull is fine — no caching/refresh complexity. The token POST
        rides the same egress-guarded client as the read calls.
        """
        username = await self._resolve_secret(ctx, config.username_secret)
        password = await self._resolve_secret(ctx, config.password_secret)
        resp = await client.post(
            config.token_url,
            data={
                "username": username,
                "password": password,
                "grant_type": "password",
                "client_id": config.client_id,
            },
        )
        resp.raise_for_status()
        token = (resp.json() or {}).get("access_token")
        if not token:
            raise httpx.HTTPError("ACLED OAuth: token endpoint returned no access_token")
        return str(token)

    def _effective_floor(
        self,
        since: datetime | None,
        cursor: _Cursor,
        config: ACLEDConfig,
    ) -> datetime:
        """Compute the floor date for the ``event_date>=`` filter.

        Precedence:
          1. cursor.last_event_date if set (resumes mid-stream)
          2. ``since`` if set
          3. now() - lookback_days
        """
        if cursor.last_event_date:
            try:
                return datetime.fromisoformat(cursor.last_event_date).replace(
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
        config: ACLEDConfig,
        floor_dt: datetime,
        page: int,
    ) -> dict[str, Any]:
        """Construct the ACLED query-string parameters.

        ACLED's query language: ``event_date={YYYY-MM-DD}`` with
        ``event_date_where=>=`` filters on/after a date. Event-type lists are
        colon-joined with ``event_type_where=:OR:``. Auth is NOT a query param
        under OAuth2 — it rides the ``Authorization: Bearer`` header.
        """
        params: dict[str, Any] = {
            "limit": config.page_size,
            "page": page,
            "event_date": floor_dt.date().isoformat(),
            "event_date_where": ">=",
        }
        if config.country:
            params["iso3"] = config.country
        if config.region:
            params["region"] = config.region
        if config.event_types:
            params["event_type"] = ":OR:".join(config.event_types)
        return params

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        params: dict[str, Any],
        config: ACLEDConfig,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """One page of records, with rate-limit + back-off handling.

        Retry policy:
          * HTTP 429: honor ``Retry-After`` if present (seconds), else 1s + jitter.
            Up to 4 retries (≈30s total worst case).
          * HTTP 5xx: 1 retry with 1s back-off then raise.
          * Network errors propagate to the caller (transient-failure per L-102 §7).
        """
        max_429_retries = 4
        backoff_seconds = 1.0
        for attempt in range(max_429_retries + 1):
            resp = await client.get(config.api_base, params=params, headers=headers)
            self._record_rate_limit(resp)

            if resp.status_code == 429:
                if attempt >= max_429_retries:
                    self._health.record_error("rate_limited_exhausted (429)")
                    resp.raise_for_status()                # raises HTTPStatusError
                retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else (backoff_seconds * (2**attempt))
                logger.warning(
                    "ACLED 429; sleeping %.1fs (attempt %d/%d)",
                    wait, attempt + 1, max_429_retries,
                )
                await asyncio.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                if attempt == 0:
                    await asyncio.sleep(backoff_seconds)
                    continue
                resp.raise_for_status()

            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                raise httpx.HTTPError(
                    f"ACLED unexpected payload type: {type(body).__name__}"
                )
            success = body.get("success")
            if success is False:
                # ACLED reports auth errors with success=False + an `error` field.
                err = body.get("error") or body.get("message") or "unknown ACLED error"
                raise httpx.HTTPError(f"ACLED API error: {err!r}")
            data = body.get("data") or []
            if not isinstance(data, list):
                raise httpx.HTTPError("ACLED body.data is not a list")
            return data

        return []                                                   # unreachable

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            # ACLED currently always sends seconds; HTTP-date form not handled.
            return None

    def _record_rate_limit(self, resp: httpx.Response) -> None:
        """Extract rate-limit headers if ACLED supplies them (currently
        undocumented; cheap to record opportunistically)."""
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            try:
                self._health.rate_limit_remaining = int(remaining)
            except ValueError:
                pass

    def _open_client(self, config: ACLEDConfig) -> httpx.AsyncClient:
        """Open an httpx.AsyncClient honoring the unit-test factory override."""
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return guarded_async_client(
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": f"legba-source-acled/{self.handler_version}"},
        )

    def _record_to_signal(
        self,
        *,
        record: dict[str, Any],
        ctx: SourceContext,
        config: ACLEDConfig,
        fetched_at: datetime,
    ) -> Signal:
        """Translate one ACLED record into a :class:`Signal`."""
        # NB: ACLED `data_id` is an int and can legitimately be 0; use
        # explicit None/"" check instead of `or` so a zero id doesn't
        # incorrectly fall back to event_id_cnty.
        raw_id = record.get("data_id")
        if raw_id is None or raw_id == "":
            raw_id = record.get("event_id_cnty") or ""
        data_id = str(raw_id)
        published_at = self._parse_event_date(record)
        latitude = _to_float(record.get("latitude"))
        longitude = _to_float(record.get("longitude"))
        fatalities = _to_int(record.get("fatalities"))

        payload: dict[str, Any] = {
            "event_type": record.get("event_type") or "",
            "sub_event_type": record.get("sub_event_type") or "",
            "actors": {
                "actor1": record.get("actor1") or "",
                "actor2": record.get("actor2") or "",
                "assoc_actor_1": record.get("assoc_actor_1") or "",
                "assoc_actor_2": record.get("assoc_actor_2") or "",
            },
            "geo": {
                "latitude": latitude,
                "longitude": longitude,
                "location": record.get("location") or "",
                "admin1": record.get("admin1") or "",
                "admin2": record.get("admin2") or "",
                "country": record.get("country") or "",
                "iso3": record.get("iso3") or record.get("iso") or "",
                "region": record.get("region") or "",
            },
            "fatalities": fatalities,
            "notes": record.get("notes") or "",
            "source": record.get("source") or "",
            "source_scale": record.get("source_scale") or "",
            "event_date": record.get("event_date") or "",
            "interaction": record.get("interaction") or "",
            "external_id": data_id,
            "title": _build_title(record),
            "raw": record,
        }
        content_hash = _content_hash(record, data_id)

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
                "external_id": data_id,
                "published_at": (
                    published_at.isoformat() if published_at else None
                ),
            },
        )

    @staticmethod
    def _parse_event_date(record: dict[str, Any]) -> datetime | None:
        raw = (record.get("event_date") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


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
        return None


def _build_title(record: dict[str, Any]) -> str:
    """Compose a human-readable title for downstream display.

    ACLED records have no title field; we synthesize one. Format:
    ``"<event_type> in <location>, <country>"`` truncated at 240 chars to
    stay under :class:`SignalPayload`'s title limit.
    """
    event_type = (record.get("event_type") or "").strip() or "ACLED event"
    location = (record.get("location") or "").strip()
    country = (record.get("country") or "").strip()
    parts = [event_type]
    if location:
        parts.append(f"in {location}")
    if country:
        parts.append(f", {country}")
    title = " ".join(parts).replace(" ,", ",").strip()
    return title[:240]


def _content_hash(record: dict[str, Any], external_id: str) -> str:
    """SHA-256 over a stable subset of record fields.

    Includes the external id + the user-visible content fields so that an
    ACLED late-edit (typo fix in `notes`, fatality count revision) changes
    the hash and the downstream dedupe (L-151) recognizes the revision as
    a new payload.
    """
    canonical = json.dumps(
        {
            "id": external_id,
            "event_date": record.get("event_date"),
            "event_type": record.get("event_type"),
            "sub_event_type": record.get("sub_event_type"),
            "actor1": record.get("actor1"),
            "actor2": record.get("actor2"),
            "country": record.get("country"),
            "admin1": record.get("admin1"),
            "location": record.get("location"),
            "latitude": record.get("latitude"),
            "longitude": record.get("longitude"),
            "fatalities": record.get("fatalities"),
            "notes": record.get("notes"),
            "source": record.get("source"),
            "interaction": record.get("interaction"),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Protocol satisfaction sanity-check (cheap, runs at import — analog of
# the registration audit done by L-160 once the runtime lands).
assert isinstance(ACLEDSourceHandler(), SourceHandler)  # type: ignore[arg-type]


__all__ = [
    "ACLED_API_BASE",
    "ACLED_EVENT_TYPES",
    "ACLED_PAGE_SIZE_MAX",
    "ACLED_REGIONS",
    "ACLEDConfig",
    "ACLEDSourceHandler",
]
