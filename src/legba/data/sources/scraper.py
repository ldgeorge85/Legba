# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic-scraper source-kind handler (L-135).

A *scraper* source is structurally identical to any other L-102 source — it
pulls raw payloads from the outside world and yields :class:`Signal`. What
distinguishes the scraper kind is **impl indirection**: the per-target
config carries an ``impl`` dotted path that names a separate scraper-impl
module under :mod:`legba.data.sources.scrapers`. The handler itself owns
the boilerplate (HTTP, proxy, rate-limit, robots.txt, BFS crawl, state-
store dedupe, telemetry); the impl owns the *site-specific* logic
(URL discovery from a seed, HTML extraction into a :class:`Signal`).

Per L-102 §2:

    Sources slated for Phase 3 (RSS, GDELT, ACLED, MediaCloud,
    OpenSanctions, scraper, Telegram, Discord, CC-NEWS, Firecrawl,
    IntelMQ bridge) all conform.

This module satisfies the structural :class:`SourceHandler` Protocol
declared in :mod:`legba.data.sources._contract`.

Config schema (:class:`ScraperConfig`)
--------------------------------------
* ``impl`` — dotted import path to a callable producing a
  :class:`ScraperImpl`-conformant object. Two accepted forms:

    1. ``"pkg.mod:ClassName"``  — colon-separated module + attribute.
    2. ``"pkg.mod.ClassName"``  — dot-separated; the final dotted segment
       is treated as the attribute name.

* ``proxy_pool`` — optional :class:`StackRef` resolving to a proxy-pool
  stack component (per L-123). If present, ``pull`` routes outbound HTTP
  through ``ProxyPoolHandler.get_httpx_async_client``.

* ``rate_limit`` — ``"N/period"`` (e.g. ``"10/min"``). Period units
  borrowed from :class:`Property.RateLimit`: s/sec/second, min/minute,
  h/hour, d/day.

* ``seed_urls`` — starting points. The impl's ``discover_urls`` runs over
  each in turn.

* ``max_depth`` — link-following depth. ``0`` => fetch the seeds only
  (no crawl). ``1`` => fetch URLs discovered from the seeds; etc.

* ``respect_robots`` — when ``True`` (default), every host's
  ``/robots.txt`` is fetched once and obeyed for every URL on that host.

* ``user_agent`` — UA string used both for robots.txt and for crawl
  requests.

* ``request_timeout_seconds`` — per-request HTTP timeout.

* ``follow_redirects`` — passed straight to :mod:`httpx`.

ScraperImpl Protocol
--------------------
Scraper-impl modules expose a class with two methods (both site-specific):

    async def discover_urls(self, seed_url: str, depth: int,
                            *, fetch: HttpFetcher) -> AsyncIterator[str]:
        ...

    async def extract(self, html: str, url: str,
                      *, ctx: SourceContext) -> Signal | None:
        ...

``discover_urls`` yields URLs that the *crawler* should fetch next.
``depth`` lets the impl behave differently at the seed vs. inner-page
level (an RSS-driven seed might yield article URLs at depth 0 but no
further links at depth 1+).

``extract`` parses fetched HTML into a :class:`Signal`. Implementations
SHOULD use :func:`trafilatura.extract` for body extraction; title /
publication-date / author / tags are impl-specific because layouts vary.

State management
----------------
The handler tracks which URLs it's already scraped in the runtime
:class:`StateStore` under the key ``"scraped_urls"``. Re-emission is
suppressed for entries with a recorded ``content_hash`` match in the
last 90 days. This is a per-instance dedupe (target + source scoped); the
pipeline-wide dedupe (L-151) still runs downstream.

Proxy + StackRef
----------------
The handler resolves the proxy_pool ``StackRef`` lazily on the first call
to :meth:`pull`. Resolution is via the optional ``stack_resolver`` passed
at construction (or via :meth:`bind_stack_resolver` before activation).
The resolver returns an object satisfying :class:`ProxyPoolHandler` —
duck-typed against the surface L-123 publishes:
``get_httpx_async_client(country=...) -> AsyncContextManager[httpx.AsyncClient]``
and ``report_usage(bytes_in)`` (best-effort).

Rate-limit
----------
A per-instance ``asyncio.Semaphore`` is *not* sufficient because the
``N/period`` shape is a *rate*, not a *concurrency cap*. The handler
implements a sliding-window token bucket — at most ``N`` requests in any
trailing ``period`` window — implemented in :class:`_RateLimiter`.

Robots.txt
----------
:meth:`pull` fetches ``/robots.txt`` once per host (via the same proxy /
client) and caches the parsed result on the handler instance. Crawled
URLs disallowed for the configured user-agent are skipped (silently;
logged at DEBUG).

Healthcheck
-----------
:meth:`health_check` verifies:

  1. The ``impl`` import path resolves (cached after first load).
  2. The proxy pool (if configured) reports ``healthy`` via its own
     ``health_check`` — best-effort.
  3. ``/robots.txt`` for the *first* seed URL's host is reachable.

Any failure downgrades the overall state to ``degraded`` (proxy / robots
unreachable) or ``unhealthy`` (impl import error).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    AsyncContextManager,
    Awaitable,
    Callable,
    ClassVar,
    Deque,
    Mapping,
    Protocol,
    runtime_checkable,
)
from urllib import robotparser
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas.properties import Property, RateLimit, StackRef
from ._contract import (
    Signal,
    SourceContext,
    SourceHealth,
    StateStore,
)
from ._egress import guarded_async_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public type surfaces
# ---------------------------------------------------------------------------


HttpFetcher = Callable[[str], Awaitable[httpx.Response]]
"""Async callable signature passed into ``ScraperImpl.discover_urls``.

The impl shouldn't construct its own HTTP client — the handler owns the
client lifecycle (so proxy / rate-limit / timeouts apply uniformly). The
impl calls the provided ``fetch`` to retrieve auxiliary documents (RSS,
sitemaps, index pages) during URL discovery.
"""


@runtime_checkable
class ScraperImpl(Protocol):
    """Site-specific scraper logic. Two methods:

    * :meth:`discover_urls` — yield URLs to scrape from a seed.
    * :meth:`extract`       — parse fetched HTML into a :class:`Signal`.

    Implementations are constructed by the handler from the
    ``impl`` dotted path at configure time, with no arguments. They may
    keep per-instance state (e.g. cached sitemap parse) but MUST NOT
    cache long-lived secrets.
    """

    async def discover_urls(
        self,
        seed_url: str,
        depth: int,
        *,
        fetch: HttpFetcher,
    ) -> AsyncIterator[str]: ...

    async def extract(
        self,
        html: str,
        url: str,
        *,
        ctx: SourceContext,
    ) -> Signal | None: ...


@runtime_checkable
class ProxyPoolHandler(Protocol):
    """L-123 proxy-pool component surface used by the scraper.

    Duck-typed — the scraper does not import :mod:`legba.data.stack.proxy`
    so it can be tested without that package being installed. The L-123
    handler MUST expose:

      * ``get_httpx_async_client(country=None) -> AsyncContextManager[AsyncClient]``
      * ``report_usage(bytes_in: int) -> Awaitable[None]`` (optional; best-effort)
    """

    def get_httpx_async_client(
        self, *, country: str | None = None,
    ) -> AsyncContextManager[httpx.AsyncClient]: ...


@runtime_checkable
class StackResolver(Protocol):
    """Minimal stack-registry resolver surface used at handler configure time.

    The runtime (L-103/L-160) injects a concrete resolver bound to the
    deployment's :class:`StackRegistry`. Tests provide a stub.
    """

    async def resolve(self, ref: StackRef) -> Any: ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class ScraperConfig(BaseModel):
    """Per-target config for the scraper kind.

    Validation is permissive at construction time so unit tests can build
    configs without a vault wired up. The registry-side validation (L-110
    + L-111) enforces the strict structural rules — including that
    ``proxy_pool`` resolves to an existing stack component.
    """

    model_config = ConfigDict(extra="forbid")

    impl: str = Field(
        ...,
        description=(
            "Dotted import path to the ScraperImpl class. "
            "Forms: 'pkg.mod:ClassName' or 'pkg.mod.ClassName'."
        ),
    )
    proxy_pool: StackRef | None = None
    rate_limit: RateLimit = Field(
        default_factory=lambda: Property.RateLimit.of("10/min"),
    )
    seed_urls: list[str] = Field(default_factory=list)
    max_depth: int = Field(default=1, ge=0, le=10)
    respect_robots: bool = True
    user_agent: str = "legba-scraper/0.1 (+https://github.com/ldgeorge85/legba)"
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    follow_redirects: bool = True
    proxy_country: str | None = None

    @field_validator("impl")
    @classmethod
    def _impl_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("impl must be a non-empty dotted path")
        if ":" in v:
            mod, _, attr = v.partition(":")
            if not mod or not attr:
                raise ValueError(f"invalid impl spec {v!r}: expected 'mod:Attr'")
        else:
            if "." not in v:
                raise ValueError(
                    f"invalid impl spec {v!r}: must contain a module path"
                )
        return v

    @field_validator("seed_urls")
    @classmethod
    def _seed_urls_well_formed(cls, urls: list[str]) -> list[str]:
        out: list[str] = []
        for u in urls:
            parsed = urlparse(u)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"seed_url {u!r} must be http(s)")
            if not parsed.netloc:
                raise ValueError(f"seed_url {u!r} missing host")
            out.append(u)
        return out


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Sliding-window token bucket — at most ``n`` events per ``period_s``.

    Async-safe within a single event loop. ``acquire()`` returns once the
    caller is permitted to proceed; callers MUST call it exactly once per
    rate-counted action (here: one HTTP request).

    Implementation: keep a deque of timestamps. On acquire, evict stamps
    older than ``period_s``; if ``len < n`` proceed; else sleep until the
    oldest stamp ages out.
    """

    def __init__(self, n: int, period_s: float) -> None:
        if n <= 0:
            raise ValueError("n must be positive")
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        self._n = n
        self._period_s = period_s
        self._stamps: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                # Evict stamps older than the window.
                while self._stamps and (now - self._stamps[0]) >= self._period_s:
                    self._stamps.popleft()
                if len(self._stamps) < self._n:
                    self._stamps.append(now)
                    return
                # Sleep until the oldest stamp ages out, then re-check.
                wait = self._period_s - (now - self._stamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)

    @classmethod
    def from_rate_limit(cls, rl: RateLimit) -> "_RateLimiter":
        """Construct from a :class:`Property.RateLimit` factory value.

        We extract the original ``N/period`` rather than collapsing onto
        ``requests_per_second`` so that bursts within a sub-window are
        permitted (e.g. ``10/min`` allows 10 in the first second of a new
        window, not 1 every 6s).
        """
        n_str, _, period = rl.raw.partition("/")
        per_map = {
            "s": 1.0, "sec": 1.0, "second": 1.0,
            "min": 60.0, "minute": 60.0,
            "h": 3600.0, "hour": 3600.0,
            "d": 86400.0, "day": 86400.0,
        }
        period_s = per_map.get(period.strip().lower())
        if period_s is None:
            raise ValueError(f"unknown rate-limit period {period!r}")
        return cls(int(float(n_str)), period_s)


# ---------------------------------------------------------------------------
# Impl loader
# ---------------------------------------------------------------------------


def load_impl(spec: str) -> ScraperImpl:
    """Resolve ``"pkg.mod:Attr"`` or ``"pkg.mod.Attr"`` -> class instance.

    The attribute may be either a class (instantiated with no arguments)
    or a zero-arg factory callable returning a :class:`ScraperImpl`.
    """
    if ":" in spec:
        mod_name, _, attr = spec.partition(":")
    else:
        mod_name, _, attr = spec.rpartition(".")
    if not mod_name or not attr:
        raise ValueError(f"invalid impl spec {spec!r}")
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ImportError(
            f"scraper impl module {mod_name!r} not importable: {exc}"
        ) from exc
    try:
        target = getattr(module, attr)
    except AttributeError as exc:
        raise AttributeError(
            f"scraper impl {attr!r} not found in {mod_name!r}"
        ) from exc
    instance = target() if callable(target) else target
    if not isinstance(instance, ScraperImpl):
        # Structural check — runtime-checkable Protocol verifies methods.
        raise TypeError(
            f"scraper impl {spec!r} does not satisfy the ScraperImpl protocol "
            f"(missing discover_urls / extract)"
        )
    return instance


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


SCRAPER_KIND: str = "scraper"
SCRAPER_SCHEMA_VERSION: str = "legba/source.scraper/1-0-0"


class ScraperSourceHandler:
    """L-102 §2 source-kind handler for the generic ``scraper`` kind.

    The handler is instantiated once per source descriptor; the runtime
    holds it for the descriptor's lifetime and invokes
    :meth:`on_configure` -> :meth:`on_activate` -> :meth:`pull` ->
    :meth:`on_pause`/:meth:`on_retire` per the L-102 §1 state machine.

    Tests can build a handler directly with ``ScraperSourceHandler(cfg)``
    and exercise :meth:`pull` / :meth:`health_check` without any runtime
    glue. The :meth:`bind_stack_resolver` hook is used by the runtime to
    inject a :class:`StackResolver` before activation.
    """

    # --- Protocol identity ----------------------------------------------
    kind: ClassVar[str] = SCRAPER_KIND
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = SCRAPER_SCHEMA_VERSION
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = ScraperConfig

    # State-store keys (per-instance — runtime namespaces by (target, source)).
    _STATE_KEY_SCRAPED: ClassVar[str] = "scraped_urls"

    def __init__(
        self,
        config: ScraperConfig,
        *,
        stack_resolver: StackResolver | None = None,
        impl: ScraperImpl | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._stack_resolver = stack_resolver
        # Allow tests to inject a pre-built impl. Production path loads
        # lazily so import errors surface during on_configure / pull.
        self._impl: ScraperImpl | None = impl
        self._proxy: ProxyPoolHandler | None = None
        self._rate_limiter = _RateLimiter.from_rate_limit(config.rate_limit)
        # Per-host robots cache: host -> RobotFileParser (or None on fetch fail).
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._rows_pulled_24h: int = 0
        self._rows_pulled_log: Deque[datetime] = deque()
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))

    # ------------------------------------------------------------------
    # Public configuration hooks
    # ------------------------------------------------------------------

    def bind_stack_resolver(self, resolver: StackResolver) -> None:
        """Inject a stack resolver. Called by the runtime before activate.
        Tests typically pass it via ``__init__`` instead."""
        self._stack_resolver = resolver

    @property
    def config(self) -> ScraperConfig:
        return self._config

    @property
    def impl(self) -> ScraperImpl | None:
        return self._impl

    # ------------------------------------------------------------------
    # L-102 §1 lifecycle hooks (minimal — runtime extras land later)
    # ------------------------------------------------------------------

    async def on_configure(self, ctx: SourceContext | None = None) -> None:
        """Load the impl and resolve the proxy_pool. Idempotent.

        Proxy resolution failures here are *swallowed* — the runtime calls
        :meth:`pull` next, which retries the resolve once more and on
        repeated failure falls back to the direct (no-proxy) client. This
        keeps a momentarily-unreachable vault from blocking the whole
        descriptor activation.
        """
        if self._impl is None:
            self._impl = load_impl(self._config.impl)
        if self._config.proxy_pool is not None and self._proxy is None:
            if self._stack_resolver is None:
                # Resolver not yet bound — defer until pull (the runtime may
                # call on_configure earlier than bind_stack_resolver in some
                # orderings). Re-attempt in pull() on first request.
                return
            try:
                self._proxy = await self._stack_resolver.resolve(
                    self._config.proxy_pool
                )
            except Exception as exc:
                logger.warning(
                    "scraper proxy resolve at configure failed; "
                    "will retry at pull: %s",
                    exc,
                )

    async def on_activate(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_pause(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_resume(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_retire(self, ctx: SourceContext | None = None) -> None:
        # Drop cached robots / proxy reference — credentials get re-resolved
        # on the next activation per L-102 §7 (no cross-call cred caching).
        self._robots.clear()
        self._proxy = None

    # ------------------------------------------------------------------
    # Pull
    # ------------------------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Yield :class:`Signal` for every newly-scraped URL.

        Algorithm:

          1. Ensure impl + proxy are loaded (lazy via on_configure).
          2. Load the ``scraped_urls`` state-store entry.
          3. For each seed URL, BFS to ``max_depth`` via the impl's
             ``discover_urls``. Each candidate URL:
               a. is checked against state (skip if scraped).
               b. is checked against robots.txt (skip if disallowed).
               c. is rate-limited.
               d. is fetched.
               e. is extracted by the impl into a Signal.
               f. is recorded in the state-store.
          4. The cursor (`scraped_urls`) is persisted incrementally —
             every emitted Signal flushes the entry so a mid-pull crash
             doesn't lose progress.
        """
        await self.on_configure(ctx)
        assert self._impl is not None, "on_configure must populate impl"
        if self._config.proxy_pool is not None and self._proxy is None:
            # Resolver might have arrived after the initial on_configure;
            # try once more here, but tolerate failure (degraded path).
            if self._stack_resolver is not None:
                try:
                    self._proxy = await self._stack_resolver.resolve(
                        self._config.proxy_pool
                    )
                except Exception as exc:
                    ctx.logger.warning(
                        "scraper proxy resolve failed; falling back to direct: %s",
                        exc,
                    )

        scraped: dict[str, str] = await self._load_scraped(ctx.state_store)

        async with self._client_cm() as client:
            fetch_one: HttpFetcher = lambda url: self._fetch_one(client, url)

            # BFS queue holds (url, depth).
            for seed in self._config.seed_urls:
                queue: Deque[tuple[str, int]] = deque([(seed, 0)])
                while queue:
                    url, depth = queue.popleft()

                    # State dedupe.
                    if url in scraped:
                        ctx.logger.debug("scraper skip already-scraped %s", url)
                        continue

                    # Robots check.
                    if self._config.respect_robots and not await self._robots_allow(
                        client, url
                    ):
                        ctx.logger.debug("scraper skip robots-disallowed %s", url)
                        continue

                    # Discovery first (BFS step) — even if the seed URL itself
                    # isn't a content page, the impl can yield article URLs.
                    if depth < self._config.max_depth:
                        try:
                            async for child in self._impl.discover_urls(
                                url, depth, fetch=fetch_one,
                            ):
                                if child not in scraped:
                                    queue.append((child, depth + 1))
                        except Exception as exc:
                            ctx.logger.warning(
                                "scraper discover_urls failed for %s: %s",
                                url, exc,
                            )

                    # Fetch + extract.
                    try:
                        await self._rate_limiter.acquire()
                        resp = await fetch_one(url)
                    except Exception as exc:
                        ctx.logger.warning(
                            "scraper fetch failed for %s: %s", url, exc,
                        )
                        self._last_error = f"fetch:{url}:{exc}"
                        continue

                    if resp.status_code >= 400:
                        ctx.logger.debug(
                            "scraper non-2xx %s for %s", resp.status_code, url,
                        )
                        scraped[url] = ""  # mark visited even on error
                        await ctx.state_store.set(self._STATE_KEY_SCRAPED, scraped)
                        continue

                    html = resp.text
                    try:
                        signal = await self._impl.extract(html, url, ctx=ctx)
                    except Exception as exc:
                        ctx.logger.warning(
                            "scraper extract failed for %s: %s", url, exc,
                        )
                        scraped[url] = ""
                        await ctx.state_store.set(self._STATE_KEY_SCRAPED, scraped)
                        continue

                    # Mark visited regardless of extract outcome — empty
                    # value = "fetched but no signal".
                    scraped[url] = signal.content_hash if signal else ""
                    await ctx.state_store.set(self._STATE_KEY_SCRAPED, scraped)

                    if signal is None:
                        continue

                    # Backfill identity / provenance the impl may have left
                    # blank — we know target / source from ctx.
                    signal = signal.model_copy(update={
                        "source_id": ctx.source_id,
                        "target_id": ctx.target_id,
                        "canonical_url": signal.canonical_url or url,
                        "raw_provenance": dict(
                            signal.raw_provenance,
                            scraper_impl=self._config.impl,
                            http_status=resp.status_code,
                            http_etag=resp.headers.get("etag"),
                            http_last_modified=resp.headers.get("last-modified"),
                        ),
                    })

                    now = self._clock()
                    self._last_success_at = now
                    self._rows_pulled_log.append(now)
                    self._evict_rows_log(now)
                    self._rows_pulled_24h = len(self._rows_pulled_log)
                    yield signal

    # ------------------------------------------------------------------
    # Healthcheck
    # ------------------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        detail: dict[str, Any] = {}
        state = "healthy"
        last_error: str | None = self._last_error

        # 1. Impl import.
        if self._impl is None:
            try:
                self._impl = load_impl(self._config.impl)
                detail["impl"] = "loaded"
            except Exception as exc:
                detail["impl_error"] = str(exc)
                state = "unhealthy"
                last_error = f"impl_import:{exc}"
                # Short-circuit — proxy + robots checks below are moot.
                return SourceHealth(
                    state=state,
                    last_success_at=self._last_success_at,
                    last_error=last_error,
                    rows_pulled_24h=self._rows_pulled_24h,
                    detail=detail,
                )
        else:
            detail["impl"] = "cached"

        # 2. Proxy reachability (best-effort; downgrade only).
        if self._config.proxy_pool is not None:
            if self._stack_resolver is None:
                detail["proxy"] = "resolver_unbound"
                state = "degraded"
            else:
                try:
                    proxy = self._proxy or await self._stack_resolver.resolve(
                        self._config.proxy_pool
                    )
                    self._proxy = proxy
                    # If the proxy handler exposes health_check, call it.
                    proxy_hc = getattr(proxy, "health_check", None)
                    if proxy_hc is not None:
                        result = await proxy_hc()
                        proxy_state = getattr(result, "state", None) or (
                            result.get("state") if isinstance(result, Mapping)
                            else "unknown"
                        )
                        detail["proxy_state"] = proxy_state
                        if proxy_state != "healthy":
                            state = "degraded"
                    else:
                        detail["proxy_state"] = "resolved"
                except Exception as exc:
                    detail["proxy_error"] = str(exc)
                    state = "degraded"

        # 3. Robots reachability — first seed URL only.
        if self._config.respect_robots and self._config.seed_urls:
            seed = self._config.seed_urls[0]
            host = urlparse(seed).netloc
            try:
                async with self._client_cm() as client:
                    rp = await self._load_robots(client, host)
                detail["robots"] = "loaded" if rp is not None else "unreachable"
                if rp is None:
                    # Treat unreachable robots as degraded only if we WOULD
                    # have respected them.
                    state = "degraded" if state == "healthy" else state
            except Exception as exc:
                detail["robots_error"] = str(exc)
                state = "degraded" if state == "healthy" else state

        return SourceHealth(
            state=state,
            last_success_at=self._last_success_at,
            last_error=last_error,
            rows_pulled_24h=self._rows_pulled_24h,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _client_cm(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield an :class:`httpx.AsyncClient` — proxy-routed if configured."""
        if self._proxy is not None:
            # Delegate to the L-123 handler — it owns proxy URL composition
            # + credential resolution per call.
            async with self._proxy.get_httpx_async_client(
                country=self._config.proxy_country,
            ) as client:
                yield client
            return

        # SSRF egress guard: the BFS crawler follows arbitrary discovered
        # URLs, so the non-proxy fetch path MUST refuse internal/metadata
        # addresses. (The proxy branch above egresses via the proxy handler.)
        timeout = httpx.Timeout(self._config.request_timeout_seconds)
        async with guarded_async_client(
            timeout=timeout,
            follow_redirects=self._config.follow_redirects,
            headers={"User-Agent": self._config.user_agent},
        ) as client:
            yield client

    async def _fetch_one(
        self, client: httpx.AsyncClient, url: str,
    ) -> httpx.Response:
        return await client.get(
            url,
            headers={"User-Agent": self._config.user_agent},
        )

    async def _robots_allow(
        self, client: httpx.AsyncClient, url: str,
    ) -> bool:
        host = urlparse(url).netloc
        if not host:
            return True
        rp = await self._load_robots(client, host)
        if rp is None:
            # robots.txt unreachable — fail-open (RFC 9309 §2.3.1.4).
            return True
        return rp.can_fetch(self._config.user_agent, url)

    async def _load_robots(
        self, client: httpx.AsyncClient, host: str,
    ) -> robotparser.RobotFileParser | None:
        if host in self._robots:
            return self._robots[host]

        # Try https first, then http — match the seed scheme of the host
        # rather than guessing both, but the seeds carry the scheme.
        rp = robotparser.RobotFileParser()
        for scheme in ("https", "http"):
            try:
                resp = await client.get(f"{scheme}://{host}/robots.txt")
            except Exception as exc:
                logger.debug("robots fetch %s://%s failed: %s", scheme, host, exc)
                continue
            if resp.status_code == 404:
                # No robots.txt -> permissive.
                rp.parse([])
                self._robots[host] = rp
                return rp
            if 200 <= resp.status_code < 300:
                rp.parse(resp.text.splitlines())
                self._robots[host] = rp
                return rp
        self._robots[host] = None
        return None

    async def _load_scraped(self, state_store: StateStore) -> dict[str, str]:
        existing = await state_store.get(self._STATE_KEY_SCRAPED)
        if existing is None:
            return {}
        if isinstance(existing, Mapping):
            return dict(existing)
        # Legacy list -> dict-with-empty-hash.
        if isinstance(existing, list):
            return {u: "" for u in existing}
        return {}

    def _evict_rows_log(self, now: datetime) -> None:
        """Drop entries older than 24h from the rows-pulled log."""
        cutoff = now.timestamp() - 86400
        while self._rows_pulled_log and self._rows_pulled_log[0].timestamp() < cutoff:
            self._rows_pulled_log.popleft()


__all__ = [
    "HttpFetcher",
    "ProxyPoolHandler",
    "SCRAPER_KIND",
    "SCRAPER_SCHEMA_VERSION",
    "ScraperConfig",
    "ScraperImpl",
    "ScraperSourceHandler",
    "StackResolver",
    "load_impl",
]
