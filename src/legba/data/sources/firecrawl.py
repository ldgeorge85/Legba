# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Firecrawl source-kind handler — L-139.

Firecrawl (https://firecrawl.dev) is a commercial SaaS that turns arbitrary
web pages into clean, LLM-friendly markdown. It handles JS rendering, anti-
bot countermeasures, link/screenshot extraction, and structured output. The
generic scraper kind (L-135) covers raw HTML retrieval through proxy pools;
this handler covers the orthogonal case where *the cleaned, AI-consumable
markdown is the product*.

API surface used (v1):

  * ``POST /v1/scrape``     — single URL → markdown/html/links/screenshot.
  * ``POST /v1/crawl``      — async crawl job; returns ``{id}``; polled via
                              ``GET /v1/crawl/{id}``; final page list under
                              ``data[]``.
  * ``POST /v1/map``        — fast URL enumeration; returns ``{links: [...]}``.

Auth: ``Authorization: Bearer <api_key>`` over HTTPS.

Credit accounting: Firecrawl bills per credit. Every successful response
carries ``creditsUsed`` (top level for scrape/map; on the crawl status
payload for crawl jobs). The handler accumulates per-pull, exposes the
figure via :meth:`report_usage` so the runtime's budget ledger (L-163,
modelled after ``stack/proxy/bright_data.py``) can persist attribution,
and includes lifetime credits in the health detail.

State store keys (scoped per ``(target_id, source_id)`` by the runtime):

  * ``firecrawl.last_pull_at``     — RFC-3339 timestamp of last pull.
  * ``firecrawl.lifetime_credits`` — running sum of credits ever consumed.

Healthcheck: a single small ``POST /v1/scrape`` against ``example.com``
(documented as a 1-credit operation) — proves both auth and end-to-end
reachability.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas.properties import Secret as SecretRef
from ._contract import Signal, SourceContext, SourceHealth
from ._egress import guarded_async_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_API_BASE: str = "https://api.firecrawl.dev"
HEALTHCHECK_URL: str = "https://example.com"
CRAWL_POLL_INTERVAL_S: float = 2.0
CRAWL_MAX_POLLS: int = 60        # 60 * 2s = 2-minute crawl budget per pull
DEFAULT_TIMEOUT_S: float = 60.0

FirecrawlMode = Literal["scrape", "crawl", "map"]
ExtractFormat = Literal["markdown", "html", "links", "screenshot"]


# ---------------------------------------------------------------------------
# Config schema (L-102 §1 — pydantic model)
# ---------------------------------------------------------------------------


class FirecrawlConfig(BaseModel):
    """Pydantic config for the Firecrawl source kind.

    All vault credential references are :class:`SecretRef` factory values
    per L-101 §2. The runtime L-103 layer resolves them via the configured
    secret resolver; the handler accepts a pre-resolved plaintext key for
    tests via constructor injection.
    """

    model_config = ConfigDict(extra="forbid")

    api_key_secret: SecretRef
    seed_urls: list[str] = Field(default_factory=list)
    mode: FirecrawlMode = "scrape"
    max_depth: int = Field(default=1, ge=0, le=10)
    extract_format: ExtractFormat = "markdown"
    include_paths: list[str] | None = None
    exclude_paths: list[str] | None = None

    api_base: str = Field(default=DEFAULT_API_BASE)
    request_timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_S, gt=0)
    crawl_limit: int = Field(default=100, ge=1, le=10_000)
    crawl_max_polls: int = Field(default=CRAWL_MAX_POLLS, ge=1, le=600)
    crawl_poll_interval_seconds: float = Field(
        default=CRAWL_POLL_INTERVAL_S, gt=0, le=60.0
    )

    @field_validator("seed_urls")
    @classmethod
    def _validate_seeds(cls, v: list[str]) -> list[str]:
        # Allow empty (operator may seed via include_paths-only on map mode),
        # but reject obviously bad entries early.
        for u in v:
            if not (u.startswith("http://") or u.startswith("https://")):
                raise ValueError(f"seed_urls entry not an http(s) URL: {u!r}")
        return v


# ---------------------------------------------------------------------------
# Exception shapes — mirror L-102 §7 failure semantics.
# ---------------------------------------------------------------------------


class FirecrawlAPIError(Exception):
    """Generic Firecrawl error. Maps to the runtime's ``HardFailure`` shape
    unless a subclass narrows it."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class FirecrawlAuthError(FirecrawlAPIError):
    """401 / 403 — credential is bad. Hard failure; runtime should not retry."""


class FirecrawlRateLimited(FirecrawlAPIError):
    """429 — runtime should retry with backoff (L-164 ``TransientFailure``)."""


# ---------------------------------------------------------------------------
# Credit-usage record (mirrors proxy_usage_ledger semantics, L-163)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreditUsageRecord:
    """One credit-attribution row produced by :meth:`report_usage`.

    The runtime's budget ledger (per L-163) consumes these. Until the
    ledger table lands the handler still returns the record so callers
    always have a structured row in hand.
    """

    source_id: str
    target_id: str
    credits: int
    mode: str
    recorded_at: datetime


SecretResolverFn = Callable[[SecretRef], Awaitable[str]]


# ---------------------------------------------------------------------------
# Internal pull bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _PullStats:
    pages_yielded: int = 0
    credits_consumed: int = 0
    errors: list[str] = field(default_factory=list)


# State-store keys.
_LAST_PULL_KEY = "firecrawl.last_pull_at"
_LIFETIME_CREDITS_KEY = "firecrawl.lifetime_credits"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class FirecrawlSourceHandler:
    """L-102 source-kind handler for Firecrawl.

    Construction takes a parsed :class:`FirecrawlConfig` plus an optional
    HTTP client (so tests can inject ``httpx.MockTransport``) and an
    optional secret resolver. The runtime (L-103) wires real ones at
    activation time; until then tests pass them directly.
    """

    kind: ClassVar[str] = "firecrawl"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.firecrawl/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = FirecrawlConfig

    def __init__(
        self,
        config: FirecrawlConfig,
        *,
        secret_resolver: SecretResolverFn | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._secret_resolver = secret_resolver
        self._http_client = http_client
        self._owns_client = http_client is None
        self._api_key: str | None = None
        # Bookkeeping for health + report_usage.
        self._lifetime_credits: int = 0
        self._last_pull_stats: _PullStats | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # ---- Lifecycle ------------------------------------------------------

    async def on_configure(self, ctx: SourceContext | None = None) -> None:
        # Config-only validation; HTTP client is built lazily so the runtime
        # can re-configure without forcing a connection.
        return None

    async def on_activate(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_pause(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_resume(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_retire(self, ctx: SourceContext | None = None) -> None:
        await self._maybe_close_client()

    # ---- pull (L-102 §2) -------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Yield one :class:`Signal` per extracted page.

        Firecrawl does not preserve canonical original-page timestamps in
        any consistent way, so ``since`` is informative only — overlap is
        permitted per the L-102 idempotency contract; downstream dedupe
        (L-151) handles re-emission. Page-level ``publishedTime`` from
        Firecrawl's metadata block is forwarded into ``payload`` for filters
        that can use it.
        """
        cfg = self._config
        stats = _PullStats()
        self._last_pull_stats = stats

        # Hydrate persistent lifetime credits at the start of the pull so
        # the figure stays accurate across actor evictions.
        persisted = await ctx.state_store.get(_LIFETIME_CREDITS_KEY)
        if isinstance(persisted, int) and persisted > self._lifetime_credits:
            self._lifetime_credits = persisted

        try:
            if cfg.mode == "scrape":
                async for sig in self._pull_scrape(ctx, cfg, stats):
                    yield sig
            elif cfg.mode == "crawl":
                async for sig in self._pull_crawl(ctx, cfg, stats):
                    yield sig
            elif cfg.mode == "map":
                async for sig in self._pull_map(ctx, cfg, stats):
                    yield sig
            else:  # pragma: no cover — pydantic guards this
                raise ValueError(f"unknown firecrawl mode: {cfg.mode!r}")

            self._last_success_at = datetime.now(tz=timezone.utc)
            self._last_error = None
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # Persist cursor + lifetime credits even on partial failures so
            # restart logic + ledger reconciliation stay accurate.
            self._lifetime_credits += stats.credits_consumed
            await ctx.state_store.set(
                _LAST_PULL_KEY,
                datetime.now(tz=timezone.utc).isoformat(),
            )
            await ctx.state_store.set(
                _LIFETIME_CREDITS_KEY, self._lifetime_credits,
            )

    # ---- mode implementations -------------------------------------------

    async def _pull_scrape(
        self,
        ctx: SourceContext,
        cfg: FirecrawlConfig,
        stats: _PullStats,
    ) -> AsyncIterator[Signal]:
        for url in cfg.seed_urls:
            body = self._build_scrape_body(url, cfg)
            data = await self._post("/v1/scrape", body)
            stats.credits_consumed += int(data.get("creditsUsed", 0) or 0)
            page = data.get("data") or {}
            sig = self._page_to_signal(ctx, cfg, page, fallback_url=url)
            if sig is not None:
                stats.pages_yielded += 1
                yield sig

    async def _pull_crawl(
        self,
        ctx: SourceContext,
        cfg: FirecrawlConfig,
        stats: _PullStats,
    ) -> AsyncIterator[Signal]:
        for seed in cfg.seed_urls:
            body: dict[str, Any] = {
                "url": seed,
                "limit": cfg.crawl_limit,
                "maxDepth": cfg.max_depth,
                "scrapeOptions": {"formats": [cfg.extract_format]},
            }
            if cfg.include_paths:
                body["includePaths"] = list(cfg.include_paths)
            if cfg.exclude_paths:
                body["excludePaths"] = list(cfg.exclude_paths)

            start = await self._post("/v1/crawl", body)
            job_id = start.get("id") or start.get("jobId")
            if not job_id:
                stats.errors.append(
                    f"crawl start for {seed!r} returned no job id: {start!r}"
                )
                continue

            final = await self._poll_crawl(
                job_id,
                max_polls=cfg.crawl_max_polls,
                interval=cfg.crawl_poll_interval_seconds,
            )
            stats.credits_consumed += int(final.get("creditsUsed", 0) or 0)
            for page in final.get("data") or []:
                sig = self._page_to_signal(ctx, cfg, page, fallback_url=seed)
                if sig is not None:
                    stats.pages_yielded += 1
                    yield sig

    async def _pull_map(
        self,
        ctx: SourceContext,
        cfg: FirecrawlConfig,
        stats: _PullStats,
    ) -> AsyncIterator[Signal]:
        for seed in cfg.seed_urls:
            body: dict[str, Any] = {"url": seed}
            if cfg.include_paths:
                # Firecrawl /v1/map takes one substring filter.
                body["search"] = cfg.include_paths[0]
            data = await self._post("/v1/map", body)
            stats.credits_consumed += int(data.get("creditsUsed", 0) or 0)
            links = data.get("links") or []
            for link in links:
                stats.pages_yielded += 1
                yield Signal(
                    signal_id=uuid4(),
                    source_id=ctx.source_id,
                    fetched_at=datetime.now(tz=timezone.utc),
                    payload={
                        "external_id": link,
                        "url": link,
                        "raw_body": "",
                        "title": None,
                        "published_at": datetime.now(
                            tz=timezone.utc
                        ).isoformat(),
                        "kind": "firecrawl_map_link",
                    },
                    content_hash=hashlib.sha256(
                        link.encode("utf-8")
                    ).hexdigest(),
                    canonical_url=link,
                    raw_provenance={"mode": "map", "seed": seed},
                )

    # ---- shared helpers --------------------------------------------------

    def _build_scrape_body(
        self,
        url: str,
        cfg: FirecrawlConfig,
    ) -> dict[str, Any]:
        formats: list[str] = [cfg.extract_format]
        # Markdown is the workhorse; always include it as a fallback so
        # ``raw_body`` is populated even when the operator asks for html/
        # links/screenshot as primary.
        if cfg.extract_format != "markdown":
            formats.insert(0, "markdown")
        return {"url": url, "formats": formats}

    async def _poll_crawl(
        self,
        job_id: str,
        *,
        max_polls: int,
        interval: float,
    ) -> dict[str, Any]:
        """Poll ``GET /v1/crawl/{id}`` until terminal state. Returns the
        final status payload (includes ``data`` list and ``creditsUsed``)."""
        last: dict[str, Any] = {}
        for _ in range(max_polls):
            last = await self._get(f"/v1/crawl/{job_id}")
            status = last.get("status")
            if status in ("completed", "failed", "cancelled"):
                return last
            await asyncio.sleep(interval)
        logger.warning(
            "firecrawl crawl %s did not terminate within %d polls; "
            "returning interim payload",
            job_id, max_polls,
        )
        return last

    def _page_to_signal(
        self,
        ctx: SourceContext,
        cfg: FirecrawlConfig,
        page: dict[str, Any],
        *,
        fallback_url: str,
    ) -> Signal | None:
        if not page:
            return None
        metadata = page.get("metadata") or {}
        url = (
            metadata.get("sourceURL")
            or metadata.get("url")
            or page.get("url")
            or fallback_url
        )
        markdown = page.get("markdown") or ""
        title = metadata.get("title") or page.get("title")

        # Prefer markdown for raw_body (the whole point of the kind); fall
        # back to whatever the configured extract_format yielded if markdown
        # is empty.
        raw_body = markdown
        if not raw_body and cfg.extract_format == "html":
            raw_body = page.get("html") or ""

        # raw_provenance gets the rich Firecrawl metadata blob — links,
        # screenshots, statusCode, etc. — so downstream consumers don't
        # reach back for it later.
        provenance: dict[str, Any] = {
            "mode": cfg.mode,
            "extract_format": cfg.extract_format,
            "firecrawl_metadata": metadata,
        }
        for opt in ("links", "screenshot", "html"):
            if opt in page and opt != cfg.extract_format:
                provenance[opt] = page[opt]

        content_hash = hashlib.sha256(
            (raw_body or url).encode("utf-8"),
        ).hexdigest()

        return Signal(
            signal_id=uuid4(),
            source_id=ctx.source_id,
            fetched_at=datetime.now(tz=timezone.utc),
            payload={
                # external_id = canonical URL per brief.
                "external_id": url,
                "url": url,
                "raw_body": raw_body,
                "title": title,
                # Firecrawl rarely returns a reliable original page date;
                # default to now() per brief. Downstream filters can mine
                # the body for a real timestamp.
                "published_at": (
                    metadata.get("publishedTime")
                    or datetime.now(tz=timezone.utc).isoformat()
                ),
                "firecrawl_metadata": metadata,
                "extract_format": cfg.extract_format,
            },
            content_hash=content_hash,
            canonical_url=url,
            language_hint=metadata.get("language"),
            raw_provenance=provenance,
        )

    # ---- health (L-102 §2) ----------------------------------------------

    async def health_check(
        self,
        ctx: SourceContext | None = None,
    ) -> SourceHealth:
        """Lightweight reachability + auth probe.

        Issues a single small ``POST /v1/scrape`` against ``example.com``.
        Failures are caught and surfaced via ``state`` / ``last_error``
        rather than raised — the runtime's health-poll loop expects a
        returned :class:`SourceHealth`.
        """
        cfg = self._config

        last_success: datetime | None = self._last_success_at
        if ctx is not None and last_success is None:
            stored = await ctx.state_store.get(_LAST_PULL_KEY)
            if isinstance(stored, str):
                try:
                    last_success = datetime.fromisoformat(stored)
                except ValueError:
                    last_success = None

        try:
            data = await self._post(
                "/v1/scrape",
                {"url": HEALTHCHECK_URL, "formats": ["markdown"]},
            )
            credits_used = int(data.get("creditsUsed", 0) or 0)
            return SourceHealth(
                state="healthy",
                last_success_at=last_success,
                rows_pulled_24h=(
                    self._last_pull_stats.pages_yielded
                    if self._last_pull_stats else 0
                ),
                detail={
                    "mode": cfg.mode,
                    "healthcheck_credits": credits_used,
                    "lifetime_credits": self._lifetime_credits,
                    "api_base": cfg.api_base,
                },
            )
        except FirecrawlAuthError as exc:
            return SourceHealth(
                state="unhealthy",
                last_success_at=last_success,
                last_error=f"auth failed: {exc}",
                detail={"api_base": cfg.api_base},
            )
        except FirecrawlRateLimited as exc:
            return SourceHealth(
                state="degraded",
                last_success_at=last_success,
                last_error=str(exc),
                detail={"status": 429, "api_base": cfg.api_base},
            )
        except FirecrawlAPIError as exc:
            return SourceHealth(
                state="degraded",
                last_success_at=last_success,
                last_error=str(exc),
                detail={"status": exc.status, "api_base": cfg.api_base},
            )
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return SourceHealth(
                state="unhealthy",
                last_success_at=last_success,
                last_error=f"network error: {exc}",
                detail={"api_base": cfg.api_base},
            )

    # ---- credit reporting (L-163) ---------------------------------------

    def report_usage(self, ctx: SourceContext) -> CreditUsageRecord:
        """Return a :class:`CreditUsageRecord` for the most recent pull.

        Synchronous (no I/O) — the runtime's budget ledger writer persists.
        Mirrors ``stack/proxy/bright_data.py::report_usage`` semantics; the
        persistence step is the runtime's job once the
        ``firecrawl_usage_ledger`` table lands per L-163.
        """
        stats = self._last_pull_stats or _PullStats()
        return CreditUsageRecord(
            source_id=ctx.source_id,
            target_id=ctx.target_id,
            credits=stats.credits_consumed,
            mode=self._config.mode,
            recorded_at=datetime.now(tz=timezone.utc),
        )

    # ---- HTTP layer ------------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        cfg = self._config
        self._http_client = guarded_async_client(
            base_url=cfg.api_base.rstrip("/"),
            timeout=httpx.Timeout(cfg.request_timeout_seconds),
        )
        return self._http_client

    async def _maybe_close_client(self) -> None:
        if (
            self._http_client is not None
            and self._owns_client
        ):
            try:
                await self._http_client.aclose()
            except Exception:  # pragma: no cover
                pass
            self._http_client = None

    async def _resolve_api_key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        if self._secret_resolver is None:
            raise RuntimeError(
                "FirecrawlSourceHandler requires a secret_resolver (or "
                "pre-resolved api key) before HTTP calls"
            )
        self._api_key = await self._secret_resolver(self._config.api_key_secret)
        return self._api_key

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": (
                f"legba-source-firecrawl/{self.handler_version}"
            ),
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        api_key = await self._resolve_api_key()
        client = await self._ensure_client()
        resp = await client.post(
            path, json=body, headers=self._auth_headers(api_key)
        )
        return self._parse_response(resp, method="POST", path=path)

    async def _get(self, path: str) -> dict[str, Any]:
        api_key = await self._resolve_api_key()
        client = await self._ensure_client()
        resp = await client.get(path, headers=self._auth_headers(api_key))
        return self._parse_response(resp, method="GET", path=path)

    @staticmethod
    def _parse_response(
        resp: httpx.Response,
        *,
        method: str,
        path: str,
    ) -> dict[str, Any]:
        if resp.status_code in (401, 403):
            raise FirecrawlAuthError(
                f"{method} {path}: {resp.status_code} {resp.text[:200]}",
                status=resp.status_code,
            )
        if resp.status_code == 429:
            raise FirecrawlRateLimited(
                f"{method} {path}: 429 rate-limited",
                status=429,
            )
        if resp.status_code >= 400:
            raise FirecrawlAPIError(
                f"{method} {path}: {resp.status_code} {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise FirecrawlAPIError(
                f"{method} {path}: non-JSON body: {resp.text[:200]}",
                status=resp.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise FirecrawlAPIError(
                f"{method} {path}: expected JSON object, got "
                f"{type(data).__name__}",
                status=resp.status_code,
            )
        # Firecrawl wraps responses as `{"success": bool, ...}`. A
        # success=False with a 2xx is the quota-exhausted / validation
        # path; treat it as a hard API error.
        if data.get("success") is False:
            err = data.get("error") or "firecrawl returned success=false"
            raise FirecrawlAPIError(
                f"{method} {path}: {err}",
                status=resp.status_code,
            )
        return data


__all__ = [
    "CreditUsageRecord",
    "FirecrawlAPIError",
    "FirecrawlAuthError",
    "FirecrawlConfig",
    "FirecrawlRateLimited",
    "FirecrawlSourceHandler",
    "SecretResolverFn",
]
