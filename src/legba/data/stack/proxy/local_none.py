# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local "no proxy" handler (L-123).

Pass-through implementation. Conforms to the same `ProxyPoolHandler`
surface as `BrightDataProxyHandler` so target / source descriptors can
reference a `proxy_pool` stack component without forcing operators to
configure a real residential provider during development.

The handler returns the sentinel string `"DIRECT"` from `get_proxy_url`;
`get_aiohttp_session` / `get_httpx_async_client` interpret this as "no
proxy configured" and produce a session that makes outbound calls direct
to the destination.

No credentials. `health_check` always returns `healthy`. `report_usage`
inherits the Bright Data handler's loud-fail contract: it persists to
`proxy_usage_ledger` or raises `UsageLedgerUnavailable` (docs/SEAMS.md
"Proxy usage ledger persistence") — developer-mode runs that record usage
must wire a pg_store with the ledger table.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, ClassVar

from .bright_data import (
    BRIGHT_DATA_KIND,
    DEFAULT_ALLOWED_COUNTRIES,
    HandlerHealth,
    ProxyPoolHandler,
    _PgStore,
    _Resolver,
)

#: kept as a separate constant so handler-registry keys are explicit even
#: though it shares the family "proxy_pool" with Bright Data.
LOCAL_NONE_KIND: str = BRIGHT_DATA_KIND


#: The sentinel value `get_proxy_url` returns; downstream session factories
#: in `ProxyPoolHandler` test for this exact string and emit an unproxied
#: client when they see it.
DIRECT_SENTINEL: str = "DIRECT"


class LocalNoneProxyHandler(ProxyPoolHandler):
    """No-op proxy handler.

    Construction takes the same arguments as `BrightDataProxyHandler` but
    accepts `config=None` and `resolver=None` — the dev path needs no
    vault. `allowed_countries` defaults to the module global so country
    validation behaves identically to Bright Data; this matters for
    correctness tests that check descriptors compose with either provider.
    """

    kind: ClassVar[str] = BRIGHT_DATA_KIND  # family + kind match Bright Data
    family: ClassVar[str] = "stack"
    schema_version: ClassVar[str] = "legba/stack/proxy_pool/1.0.0"
    handler_version: ClassVar[str] = "0.1.0"

    def __init__(
        self,
        *,
        component_id: str = "proxy.local.none",
        config: Any = None,
        resolver: _Resolver | None = None,
        pg_store: _PgStore | None = None,
        allowed_countries: tuple[str, ...] | list[str] | None = None,
    ):
        # We *intentionally* tolerate config=None — dev callers can spin up
        # the no-op handler without going through the stack registry.
        super().__init__(
            component_id=component_id,
            config=config if config is not None else _NullConfig(),
            resolver=resolver,
            pg_store=pg_store,
            allowed_countries=allowed_countries,
        )

    async def get_proxy_url(
        self,
        country: str | None = None,
        sticky: bool = False,
        session_id: str | None = None,
    ) -> str:
        # Country still validated so dev / prod behaviour matches for the
        # caller (raises `InvalidCountryError` on a disallowed code).
        self._validate_country(country)
        return DIRECT_SENTINEL

    async def _credential_health(self) -> HandlerHealth:
        return HandlerHealth(
            state="healthy",
            last_success_at=_dt.datetime.now(tz=_dt.timezone.utc),
            detail={"provider": "none"},
        )

    async def _do_deep_health_check(self) -> HandlerHealth:
        # A "deep" check for the no-op handler is meaningless — we'd just
        # be testing the host's outbound connectivity, which is what the
        # caller is responsible for in dev mode. Report unknown so the
        # caller doesn't mistake it for a green signal.
        return HandlerHealth(
            state="unknown",
            detail={"reason": "no-op proxy: deep check not applicable"},
        )

    async def health_check(self, ctx: Any = None) -> HandlerHealth:
        # Override the base default which requires a resolver. The no-op
        # handler is always healthy from its own perspective.
        return await self._credential_health()


class _NullConfig:
    """Stand-in object exposing the subset of `ProxyPoolConfig` attributes
    `ProxyPoolHandler.__init__` reads when no real config is supplied.

    Kept as a private internal — callers wiring real `ProxyPoolConfig`
    instances through the stack registry never see this.
    """

    geo_targeting = None
    credentials = None
    provider = type("D", (), {"raw": "none"})()
    endpoint = None
    rotation = type("D", (), {"raw": "session"})()
