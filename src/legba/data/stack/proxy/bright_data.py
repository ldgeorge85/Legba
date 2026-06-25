# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bright Data residential proxy handler (L-123).

Conforms to the L-102 §1 `KindHandler` shape: declares `kind`, `family`,
`schema_version`, lifecycle hooks, a `health_check`, and the per-family
operations every `ProxyPoolHandler` exposes.

URL format (per Bright Data docs):

    http://user-customer-{customer_id}-zone-{zone}-country-{country}:{password}
    @brd.superproxy.io:22225

Sticky sessions append `-session-{session_id}` to the user component so
subsequent requests using the same session id traverse the same exit IP
for the session's lifetime (Bright Data manages eviction).

Credentials live in the L-001 / L-111 credential vault as separate
`Property.Secret` references:

  * `<credentials_prefix>.customer_id`
  * `<credentials_prefix>.zone`
  * `<credentials_prefix>.password`

The single `Secret` on `ProxyPoolConfig.credentials` carries the prefix; the
handler resolves the three sub-keys at call time. This mirrors the pattern
used by other multi-field credentials (e.g. a `user:pass` pair) but
keeps each piece independently rotatable.

Healthcheck (default): vault credential resolve check only. NO outbound
call through the proxy — every poll would cost residential-bandwidth dollars.
`deep_health_check()` makes one `ipinfo.io/json` call via the proxy and is
gated by env `LEGBA_PROXY_DEEP_HEALTHCHECK_ENABLED=1`.

Country selection: ISO 3166-1 alpha-2 codes only. Validated against
`ProxyPoolConfig`'s `allowed_countries` list (or the module-level default if
unset). `country=None` passes through to Bright Data's default (no
`country-` segment in the URL, which Bright Data interprets as "any
geography in the zone").

Usage attribution: `report_usage(bytes_used, country)` writes to a Postgres
`proxy_usage_ledger` table. The ledger migration lands only once a budget
analyst actually reads from it — until then the write path REFUSES loudly
(:class:`UsageLedgerUnavailable`) instead of faking a persisted record
(declared seam: docs/SEAMS.md "Proxy usage ledger persistence").
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from dataclasses import dataclass
from typing import (
    Any,
    ClassVar,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Family / kind identifier per L-102 §1 (used in handler registry keys).
BRIGHT_DATA_KIND: str = "proxy_pool"

#: Bright Data residential superproxy endpoint. Fixed; no need to make it
#: configurable per-instance (Bright Data exposes only this single ingress
#: for the residential network).
BRIGHT_DATA_DEFAULT_HOST: str = "brd.superproxy.io"
BRIGHT_DATA_DEFAULT_PORT: int = 22225

#: Env-flag gating the optional `deep_health_check()` outbound test call.
#: Off by default — Bright Data bills per byte even for tiny probe traffic.
DEEP_HEALTHCHECK_ENV: str = "LEGBA_PROXY_DEEP_HEALTHCHECK_ENABLED"

#: ipinfo.io endpoint used for the deep healthcheck. Tiny JSON response
#: (~150 bytes), no auth required. Picked because it's a stable target with
#: no rate-limit floor for unauthenticated traffic.
_DEEP_HEALTHCHECK_URL: str = "https://ipinfo.io/json"

#: Pattern for valid Bright Data session ids. The vendor docs are loose
#: ("alphanumeric"); we enforce a conservative `[a-z0-9_-]{1,32}` to keep
#: URL escaping simple.
_SESSION_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

#: ISO 3166-1 alpha-2 — exactly two uppercase ASCII letters.
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

#: Conservative global default. Operators tighten via `ProxyPoolConfig`'s
#: top-level `geo_targeting` list or per-handler `allowed_countries`.
#: Picked to cover Legba's existing 195-country target ambition while
#: excluding sanctioned / OFAC-restricted destinations Bright Data doesn't
#: serve anyway. Operators can override either direction.
DEFAULT_ALLOWED_COUNTRIES: tuple[str, ...] = (
    "US", "GB", "DE", "FR", "ES", "IT", "NL", "BE", "SE", "NO",
    "FI", "DK", "PL", "CZ", "AT", "CH", "IE", "PT", "GR", "HU",
    "RO", "BG", "HR", "SK", "SI", "EE", "LV", "LT", "LU", "MT",
    "CA", "MX", "BR", "AR", "CL", "CO", "PE", "VE", "UY", "EC",
    "JP", "KR", "TW", "HK", "SG", "MY", "TH", "VN", "PH", "ID",
    "IN", "PK", "BD", "LK", "NP",
    "AU", "NZ",
    "ZA", "NG", "KE", "EG", "MA", "GH", "TN", "DZ",
    "TR", "IL", "AE", "SA", "QA", "KW", "BH", "OM", "JO", "LB",
    "UA", "RS", "MD",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidCountryError(ValueError):
    """Raised when a country code passed to a proxy operation is not on the
    handler's allowed list. Includes the offending code + the allowed set
    so callers can render a useful error to the operator."""

    def __init__(self, country: str, allowed: tuple[str, ...]):
        super().__init__(
            f"country {country!r} not in allowed list "
            f"(allowed={sorted(allowed)})"
        )
        self.country = country
        self.allowed = tuple(allowed)


class MissingCredentialError(RuntimeError):
    """Vault is missing one of the three Bright Data credential keys
    (`customer_id`, `zone`, `password`). Surfaced with the missing key
    so the operator can rotate it in."""

    def __init__(self, key: str):
        super().__init__(f"vault missing required Bright Data credential: {key!r}")
        self.key = key


class UsageLedgerUnavailable(RuntimeError):
    """Loud-fail guard for the usage-attribution write path.

    Raised by :meth:`ProxyPoolHandler.report_usage` when the
    `proxy_usage_ledger` row cannot be persisted — no pg_store wired, the
    ledger table's migration hasn't landed, or the INSERT failed. Per the
    no-stub rule the handler refuses to hand back an unpersisted record as
    if attribution succeeded (declared seam: docs/SEAMS.md "Proxy usage
    ledger persistence")."""


# ---------------------------------------------------------------------------
# Resolver protocol — minimal slice of L-111 `CredentialResolverProtocol`
# we depend on. Re-typed here so this module has no hard import dependency
# on the registry package (matches the L-102 stance: handlers are plugin
# code; structural typing only).
# ---------------------------------------------------------------------------


@runtime_checkable
class _Resolver(Protocol):
    async def resolve(self, secret_id: str) -> bytes: ...
    async def verify_exists(self, secret_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Postgres store protocol — minimal slice we depend on for the optional
# usage-ledger writes. The store is supplied by the runtime; this handler
# never owns a connection.
# ---------------------------------------------------------------------------


@runtime_checkable
class _PgStore(Protocol):
    def acquire(self) -> Any: ...


# ---------------------------------------------------------------------------
# Health shape — mirrors L-102 §1 `HandlerHealth`. Kept local so this
# module is import-cheap; the registry-side equivalent is
# `legba.data.registry.health.StackComponentHealth` and tests confirm the
# field set lines up.
# ---------------------------------------------------------------------------


@dataclass
class HandlerHealth:
    state: Literal["healthy", "degraded", "unhealthy", "unknown"]
    last_success_at: _dt.datetime | None = None
    last_error: str | None = None
    detail: Mapping[str, Any] | None = None


@dataclass
class UsageRecord:
    """Single row written to (or stubbed against) `proxy_usage_ledger`."""

    component_id: str
    country: str
    bytes_used: int
    recorded_at: _dt.datetime


# ---------------------------------------------------------------------------
# Base handler
# ---------------------------------------------------------------------------


class ProxyPoolHandler:
    """Abstract base for proxy-pool stack handlers.

    Subclasses implement at minimum:

      * `_build_proxy_url(country, sticky, session_id)` — pure-string URL
        construction. Sync; never touches the vault directly. The vault
        lookup is done in `_resolve_credentials` and the result feeds in.

      * `get_aiohttp_session` / `get_httpx_async_client` — factory methods
        returning a pre-configured client with the proxy URL applied. Default
        implementations (here) are correct for any handler whose
        `get_proxy_url` returns a normal HTTP-proxy URL.

      * `health_check` — vault-credential check + (optional) deep call.

    Subclasses MUST set `kind`, `family`, `schema_version` ClassVars per
    L-102 §1. The base class enforces those by raising on instantiation if
    they're missing.
    """

    # L-102 §1 identity. Subclasses MUST override.
    kind: ClassVar[str] = ""
    family: ClassVar[str] = "stack"  # stack components family per L-102 §1
    schema_version: ClassVar[str] = ""
    handler_version: ClassVar[str] = "0.1.0"

    # Subclass advertises the schema_uri prefix it accepts; the registry
    # confirms when binding a config to a handler.
    accepted_schema_uri_prefix: ClassVar[str] = "legba/stack/proxy_pool/"

    def __init__(
        self,
        *,
        component_id: str,
        config: Any,  # ProxyPoolConfig — kept loose to avoid the schema import dependency cycle
        resolver: _Resolver | None = None,
        pg_store: _PgStore | None = None,
        allowed_countries: tuple[str, ...] | list[str] | None = None,
    ):
        if not self.__class__.kind:
            raise TypeError(
                f"{type(self).__name__} must declare a class-level `kind`"
            )
        if not self.__class__.schema_version:
            raise TypeError(
                f"{type(self).__name__} must declare a class-level `schema_version`"
            )

        self._component_id = component_id
        self._config = config
        self._resolver = resolver
        self._pg_store = pg_store

        if allowed_countries is None:
            # Fall back to (1) the config's geo_targeting list when populated,
            # (2) the module default.
            cfg_geo = self._extract_config_geo_targeting(config)
            allowed_countries = cfg_geo if cfg_geo else DEFAULT_ALLOWED_COUNTRIES

        self._allowed_countries: tuple[str, ...] = tuple(
            self._normalize_country(c) for c in allowed_countries
        )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def component_id(self) -> str:
        return self._component_id

    @property
    def config(self) -> Any:
        return self._config

    @property
    def allowed_countries(self) -> tuple[str, ...]:
        return self._allowed_countries

    # ------------------------------------------------------------------
    # Country validation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_country(country: str) -> str:
        """Uppercase + strip; raise `ValueError` on a non-ISO-3166-1-α-2 shape."""
        if not isinstance(country, str):
            raise ValueError(f"country must be a string, got {type(country).__name__}")
        norm = country.strip().upper()
        if not _COUNTRY_RE.match(norm):
            raise ValueError(
                f"country {country!r} is not a valid ISO 3166-1 alpha-2 code"
            )
        return norm

    def _validate_country(self, country: str | None) -> str | None:
        if country is None:
            return None
        norm = self._normalize_country(country)
        if norm not in self._allowed_countries:
            raise InvalidCountryError(norm, self._allowed_countries)
        return norm

    # ------------------------------------------------------------------
    # Operations — concrete subclasses fill these in.
    # ------------------------------------------------------------------

    async def get_proxy_url(
        self,
        country: str | None = None,
        sticky: bool = False,
        session_id: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def get_aiohttp_session(
        self,
        country: str | None = None,
        sticky: bool = False,
        session_id: str | None = None,
    ) -> Any:
        """Return a pre-configured `aiohttp.ClientSession` with the proxy URL
        wired in.

        aiohttp does not accept a session-default proxy parameter directly.
        The supported pattern is to set the `trust_env` flag or to pass
        `proxy=` per request. To avoid forcing every caller to repeat the
        kwarg, we attach the proxy URL to the session as a custom attribute
        (`session.legba_proxy_url`) and patch the bound `_request` method on
        the instance — composition rather than inheriting from
        `ClientSession`, which aiohttp now deprecates.
        """
        # Import here to keep aiohttp out of import-time for callers who
        # only need the URL string.
        import aiohttp

        proxy_url = await self.get_proxy_url(
            country=country, sticky=sticky, session_id=session_id
        )

        session = aiohttp.ClientSession()
        setattr(session, "legba_proxy_url", proxy_url)
        setattr(session, "legba_proxy_country", country)

        original_request = session._request

        async def _request_with_proxy(method, url, **kwargs):
            if proxy_url != "DIRECT":
                kwargs.setdefault("proxy", proxy_url)
            return await original_request(method, url, **kwargs)

        # Bind to the instance only (does not pollute the class).
        session._request = _request_with_proxy  # type: ignore[method-assign]
        return session

    async def get_httpx_async_client(
        self,
        country: str | None = None,
        sticky: bool = False,
        session_id: str | None = None,
        **httpx_kwargs: Any,
    ) -> httpx.AsyncClient:
        """Return a pre-configured `httpx.AsyncClient` with the proxy URL
        wired in. httpx accepts `proxy=` (singular) or `mounts=` (per-scheme).
        We use `proxy=` since Bright Data's URL routes both http and https
        through the same superproxy."""
        proxy_url = await self.get_proxy_url(
            country=country, sticky=sticky, session_id=session_id
        )
        kwargs = dict(httpx_kwargs)
        if proxy_url != "DIRECT":
            # httpx 0.28+ uses `proxy=`; 0.27 used `proxies=` (deprecated path).
            kwargs.setdefault("proxy", proxy_url)
        client = httpx.AsyncClient(**kwargs)
        # Surface the proxy URL on the client for observability / tests.
        setattr(client, "legba_proxy_url", proxy_url)
        setattr(client, "legba_proxy_country", country)
        return client

    async def report_usage(self, bytes_used: int, country: str) -> UsageRecord:
        """Record bandwidth attribution to `proxy_usage_ledger`.

        Loud-fail contract (no-stub rule; docs/SEAMS.md "Proxy usage
        ledger persistence"): this method either persists the row and
        returns it, or raises :class:`UsageLedgerUnavailable`. It never
        returns an unpersisted record as success — the previous behaviour
        (DEBUG log + return) silently faked attribution."""
        norm = self._normalize_country(country)
        if bytes_used < 0:
            raise ValueError(f"bytes_used must be >= 0, got {bytes_used}")
        record = UsageRecord(
            component_id=self._component_id,
            country=norm,
            bytes_used=int(bytes_used),
            recorded_at=_dt.datetime.now(tz=_dt.timezone.utc),
        )

        store = self._pg_store
        if store is None:
            raise UsageLedgerUnavailable(
                f"report_usage refused for component={record.component_id!r}: "
                "no pg_store wired — the usage row would be dropped silently "
                "(see docs/SEAMS.md: proxy usage ledger persistence)"
            )

        try:
            async with store.acquire() as conn:
                # Check existence first — avoids noisy 42P01 in logs when
                # the ledger migration hasn't landed yet. `to_regclass`
                # returns NULL if the relation doesn't exist.
                exists = await conn.fetchval(
                    "SELECT to_regclass('public.proxy_usage_ledger') IS NOT NULL"
                )
                if not exists:
                    raise UsageLedgerUnavailable(
                        f"report_usage refused for component="
                        f"{record.component_id!r}: proxy_usage_ledger table "
                        "absent — the ledger migration has not landed "
                        "(see docs/SEAMS.md: proxy usage ledger persistence)"
                    )
                await conn.execute(
                    """
                    INSERT INTO proxy_usage_ledger
                        (component_id, country, bytes_used, recorded_at)
                    VALUES ($1, $2, $3, $4)
                    """,
                    record.component_id,
                    record.country,
                    record.bytes_used,
                    record.recorded_at,
                )
        except UsageLedgerUnavailable:
            raise
        except Exception as exc:
            raise UsageLedgerUnavailable(
                f"failed to write proxy_usage_ledger row for component="
                f"{record.component_id!r}: {exc}"
            ) from exc
        return record

    # ------------------------------------------------------------------
    # Lifecycle — defaults are no-ops. Subclasses override as needed.
    # L-102 §1: every handler implements these so the runtime can call
    # them on state transitions.
    # ------------------------------------------------------------------

    async def on_configure(self, ctx: Any = None) -> None:
        return None

    async def on_activate(self, ctx: Any = None) -> None:
        return None

    async def on_pause(self, ctx: Any = None) -> None:
        return None

    async def on_resume(self, ctx: Any = None) -> None:
        return None

    async def on_retire(self, ctx: Any = None) -> None:
        return None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self, ctx: Any = None) -> HandlerHealth:
        """Default: degraded if no resolver wired; otherwise call subclass
        `_credential_health`."""
        if self._resolver is None:
            return HandlerHealth(
                state="degraded",
                detail={"reason": "no credential resolver wired"},
            )
        try:
            return await self._credential_health()
        except Exception as exc:
            return HandlerHealth(
                state="unhealthy", last_error=str(exc),
                detail={"phase": "credential_health"},
            )

    async def deep_health_check(self) -> HandlerHealth:
        """One outbound HTTP via the proxy. Gated by `DEEP_HEALTHCHECK_ENV`
        because residential bandwidth is metered. Subclasses override to
        actually perform the outbound call; the base method enforces the
        gate and returns 'unknown' when disabled."""
        if os.getenv(DEEP_HEALTHCHECK_ENV, "").lower() not in {"1", "true", "yes"}:
            return HandlerHealth(
                state="unknown",
                detail={
                    "reason": f"{DEEP_HEALTHCHECK_ENV} not set; deep check skipped",
                },
            )
        return await self._do_deep_health_check()

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    async def _credential_health(self) -> HandlerHealth:
        """Subclasses override to verify their required credential set."""
        return HandlerHealth(state="unknown")

    async def _do_deep_health_check(self) -> HandlerHealth:
        """Subclasses override to perform an outbound call through the proxy."""
        return HandlerHealth(state="unknown")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_config_geo_targeting(config: Any) -> tuple[str, ...] | None:
        """Pull `geo_targeting.raw` off the typed `ProxyPoolConfig`. Returns
        a tuple of normalized country codes, or `None` if the field is
        unset / empty / not present (anything that should fall through to
        the module default)."""
        geo = getattr(config, "geo_targeting", None)
        if geo is None:
            return None
        raw = getattr(geo, "raw", None)
        if not raw:
            return None
        if not isinstance(raw, (list, tuple)):
            return None
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            norm = item.strip().upper()
            if _COUNTRY_RE.match(norm):
                out.append(norm)
        return tuple(out) if out else None


# ---------------------------------------------------------------------------
# Bright Data implementation
# ---------------------------------------------------------------------------


class BrightDataProxyHandler(ProxyPoolHandler):
    """Concrete handler for Bright Data residential proxies.

    Expected `ProxyPoolConfig` shape (per `legba.data.schemas.stack`):

      * `provider`     — must be `"bright_data"` (validated at registration).
      * `endpoint`     — optional; defaults to the official residential host.
      * `credentials`  — `Property.Secret` whose `.raw` is the *prefix* for
                         the three sub-keys `<prefix>.customer_id`,
                         `<prefix>.zone`, `<prefix>.password`.
      * `geo_targeting`— optional ISO-3166-1-α-2 list overriding the module
                         default. Empty / unset → module default.
      * `rotation`     — informational; the Bright Data URL doesn't carry
                         rotation parameters separately (it's session-based).

    The handler's `_credentials_prefix` defaults to the config's
    `credentials.raw` value; tests / operators can override per-instance.
    """

    kind: ClassVar[str] = BRIGHT_DATA_KIND
    family: ClassVar[str] = "stack"
    schema_version: ClassVar[str] = "legba/stack/proxy_pool/1.0.0"
    handler_version: ClassVar[str] = "0.1.0"

    #: Sub-key suffixes appended to `credentials_prefix`.
    CRED_SUFFIX_CUSTOMER_ID: ClassVar[str] = "customer_id"
    CRED_SUFFIX_ZONE: ClassVar[str] = "zone"
    CRED_SUFFIX_PASSWORD: ClassVar[str] = "password"

    def __init__(
        self,
        *,
        component_id: str,
        config: Any,
        resolver: _Resolver | None = None,
        pg_store: _PgStore | None = None,
        allowed_countries: tuple[str, ...] | list[str] | None = None,
        host: str = BRIGHT_DATA_DEFAULT_HOST,
        port: int = BRIGHT_DATA_DEFAULT_PORT,
        credentials_prefix: str | None = None,
    ):
        super().__init__(
            component_id=component_id,
            config=config,
            resolver=resolver,
            pg_store=pg_store,
            allowed_countries=allowed_countries,
        )
        self._host = host
        self._port = int(port)

        if credentials_prefix is None:
            cred = getattr(config, "credentials", None)
            raw = getattr(cred, "raw", None) if cred is not None else None
            if not raw:
                raise ValueError(
                    "BrightDataProxyHandler requires a credentials prefix; "
                    "either via config.credentials.raw or the "
                    "`credentials_prefix=` constructor kwarg"
                )
            credentials_prefix = raw
        if not credentials_prefix or " " in credentials_prefix:
            raise ValueError(
                f"invalid credentials_prefix {credentials_prefix!r}: "
                "must be a non-empty dotted identifier"
            )
        self._credentials_prefix = credentials_prefix

    # ------------------------------------------------------------------
    # Credential resolution
    # ------------------------------------------------------------------

    @property
    def credentials_prefix(self) -> str:
        return self._credentials_prefix

    def _secret_id(self, suffix: str) -> str:
        return f"{self._credentials_prefix}.{suffix}"

    async def _resolve_credentials(self) -> tuple[str, str, str]:
        """Resolve `(customer_id, zone, password)` from the vault.

        Each value is decoded as utf-8. Per L-102 §7 we DO NOT cache the
        resolved plaintext; every call hits the resolver so rotations take
        effect immediately. Caching would also leak credentials through
        actor memory snapshots once the runtime persists state.
        """
        if self._resolver is None:
            raise MissingCredentialError("(no resolver wired)")
        out: list[str] = []
        for suffix in (
            self.CRED_SUFFIX_CUSTOMER_ID,
            self.CRED_SUFFIX_ZONE,
            self.CRED_SUFFIX_PASSWORD,
        ):
            sid = self._secret_id(suffix)
            try:
                blob = await self._resolver.resolve(sid)
            except KeyError as exc:
                raise MissingCredentialError(suffix) from exc
            if blob is None:
                raise MissingCredentialError(suffix)
            try:
                value = blob.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise MissingCredentialError(suffix) from exc
            if not value:
                raise MissingCredentialError(suffix)
            out.append(value)
        return out[0], out[1], out[2]

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def _build_user_component(
        self,
        customer_id: str,
        zone: str,
        country: str | None,
        sticky: bool,
        session_id: str | None,
    ) -> str:
        """Construct the `user-customer-XXX-zone-YYY[-country-CC][-session-SID]`
        URL-username component per Bright Data's docs.

        Validates session id shape; sticky-without-session is allowed and
        falls through to Bright Data's default session behaviour (which
        sticks for ~30s)."""
        parts = [
            "user",
            f"customer-{customer_id}",
            f"zone-{zone}",
        ]
        if country is not None:
            # Bright Data wants lowercase ISO-3166-1-α-2 in the URL.
            parts.append(f"country-{country.lower()}")
        if sticky and session_id is not None:
            if not _SESSION_ID_RE.match(session_id):
                raise ValueError(
                    f"invalid session_id {session_id!r}: must match "
                    f"{_SESSION_ID_RE.pattern}"
                )
            parts.append(f"session-{session_id}")
        return "-".join(parts)

    async def get_proxy_url(
        self,
        country: str | None = None,
        sticky: bool = False,
        session_id: str | None = None,
    ) -> str:
        """Return the fully-formed proxy URL.

        Resolves the customer_id, zone, password tuple from the vault per
        call (no caching, per L-102 §7).
        """
        normalized_country = self._validate_country(country)
        if sticky and session_id is None:
            # Bright Data's session sticking requires an id. We don't generate
            # one server-side because that defeats the point — callers want
            # to control session lifetime / scope. Raise so misuse is loud.
            raise ValueError(
                "sticky=True requires session_id (caller-controlled)"
            )

        customer_id, zone, password = await self._resolve_credentials()
        user_part = self._build_user_component(
            customer_id=customer_id,
            zone=zone,
            country=normalized_country,
            sticky=sticky,
            session_id=session_id,
        )
        # NOTE: Bright Data's password may include URL-unsafe characters.
        # We keep it raw in the URL because the vendor's gateway accepts
        # passwords verbatim; if we ever switch to a vendor that requires
        # percent-encoding, do it here.
        return f"http://{user_part}:{password}@{self._host}:{self._port}"

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def _credential_health(self) -> HandlerHealth:
        try:
            await self._resolve_credentials()
        except MissingCredentialError as exc:
            return HandlerHealth(
                state="unhealthy",
                last_error=str(exc),
                detail={"missing_key": getattr(exc, "key", None)},
            )
        return HandlerHealth(
            state="healthy",
            last_success_at=_dt.datetime.now(tz=_dt.timezone.utc),
            detail={
                "credentials_prefix": self._credentials_prefix,
                "host": self._host,
                "port": self._port,
            },
        )

    async def _do_deep_health_check(self) -> HandlerHealth:
        # One real outbound call. Bright Data bills bytes for this, hence
        # the env gate in `deep_health_check`.
        try:
            async with await self.get_httpx_async_client(timeout=10.0) as client:
                resp = await client.get(_DEEP_HEALTHCHECK_URL)
                if resp.status_code != 200:
                    return HandlerHealth(
                        state="degraded",
                        last_error=f"status {resp.status_code}",
                        detail={"url": _DEEP_HEALTHCHECK_URL},
                    )
                # Report usage best-effort. The bytes_used here is the
                # response size; the actual billed traffic is request +
                # response, but ipinfo's response dominates request.
                bytes_used = len(resp.content)
                # We don't know which country the proxy chose; pass an
                # informational marker.
                country_marker = "??"
                try:
                    # ipinfo response includes a "country" field.
                    body = resp.json()
                    cc = body.get("country")
                    if isinstance(cc, str) and _COUNTRY_RE.match(cc.upper()):
                        country_marker = cc.upper()
                except Exception:
                    pass
                return HandlerHealth(
                    state="healthy",
                    last_success_at=_dt.datetime.now(tz=_dt.timezone.utc),
                    detail={
                        "exit_country": country_marker,
                        "bytes_used": bytes_used,
                    },
                )
        except Exception as exc:
            return HandlerHealth(
                state="unhealthy",
                last_error=str(exc),
                detail={"phase": "deep_health_check_outbound"},
            )
