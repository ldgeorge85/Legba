# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.proxy — L-123 proxy pool stack-component handlers.

Phase-2 task L-123 (per `legba_execution_plan.md`). Proxy pool components
let Legba sources / scrapers route outbound HTTP through residential or
datacenter exit IPs, with country targeting and (optional) sticky sessions.

Handlers in this package:

  * `ProxyPoolHandler` — the abstract base + shared protocol surface.
    Defines `kind = "proxy_pool"`, the operations every concrete handler
    implements (`get_proxy_url`, `get_aiohttp_session`,
    `get_httpx_async_client`, `report_usage`), and the lifecycle / health
    contract per L-102 §1.

  * `BrightDataProxyHandler` — Bright Data residential proxy implementation.
    URL format per Bright Data's docs:
        http://user-customer-{cid}-zone-{zone}-country-{cc}:password@
        brd.superproxy.io:22225
    Country selection via the `country=` op argument; sticky sessions via
    Bright Data's `session-{id}` URL syntax. Credentials (`customer_id`,
    `zone`, `password`) resolved per-call from the vault — NEVER cached
    beyond a single invocation per L-102 §7.

  * `LocalNoneProxyHandler` — pass-through "no proxy" handler. The
    development default; lets target/source descriptors reference
    `proxy_pool` without forcing operators to set up Bright Data credentials.
    `get_proxy_url` returns the literal `"DIRECT"` sentinel that
    `get_aiohttp_session` / `get_httpx_async_client` translate into a
    sessions with no proxy configured.

LB-13 decision (per `pillar_legba.md` §8): Bright Data is the default real
provider. Oxylabs / self-managed remain valid `ProxyPoolConfig.provider`
values; their handlers can be added later without touching this package's
contract.

Cost note: residential proxy bandwidth runs ~$8 / GB on Bright Data. Real
integration tests are gated on `LEGBA_BRIGHT_DATA_*` environment variables
being present; unit tests cover URL formatting + session construction +
credential resolution paths against a mocked vault.
"""

from __future__ import annotations

from .bright_data import (
    BRIGHT_DATA_DEFAULT_HOST,
    BRIGHT_DATA_DEFAULT_PORT,
    BRIGHT_DATA_KIND,
    BrightDataProxyHandler,
    DEEP_HEALTHCHECK_ENV,
    DEFAULT_ALLOWED_COUNTRIES,
    InvalidCountryError,
    ProxyPoolHandler,
    UsageLedgerUnavailable,
    UsageRecord,
)
from .local_none import LOCAL_NONE_KIND, LocalNoneProxyHandler

__all__ = [
    "BRIGHT_DATA_DEFAULT_HOST",
    "BRIGHT_DATA_DEFAULT_PORT",
    "BRIGHT_DATA_KIND",
    "BrightDataProxyHandler",
    "DEEP_HEALTHCHECK_ENV",
    "DEFAULT_ALLOWED_COUNTRIES",
    "InvalidCountryError",
    "LOCAL_NONE_KIND",
    "LocalNoneProxyHandler",
    "ProxyPoolHandler",
    "UsageLedgerUnavailable",
    "UsageRecord",
]
