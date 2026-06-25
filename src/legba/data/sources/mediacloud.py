# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MediaCloud source-kind handler (L-133, Phase 3).

Pulls stories from the Berkman-Klein MediaCloud open-news corpus. Free
service with weaker SLA than commercial news APIs but billion-story scale
coverage.

The official ``mediacloud`` Python client (5.x line, requests-based,
synchronous) is a thin wrapper over the v4 HTTP API. We deliberately do
NOT take a dependency on it:

  * The rest of legba.data uses ``httpx`` for async HTTP. Mixing a sync
    ``requests`` client into the source pull loop would block the actor's
    event loop and is the kind of footgun the L-102 §2 cancellation
    contract explicitly cares about.
  * The v4 HTTP surface we need (``api/search/story-list`` plus an auth
    ping) is tiny — wrapping it directly in httpx keeps surface area
    smaller than pinning + tracking a third-party version.

If we later need richer endpoints (collection browsing, source directory)
we can either expand this module's small client or add the upstream
package as an optional extra; for L-133 the lean path is correct.

Public surface:
  * :class:`MediaCloudConfig` — pydantic config schema (api key SecretRef,
    query DSL, optional collection ids / language, lookback days).
  * :class:`MediaCloudSourceHandler` — implements ``pull`` /
    ``health_check`` per the L-102 source-kind contract.
  * :class:`MediaCloudHttpError` — raised on non-retryable HTTP errors.
  * :class:`MediaCloudRateLimited` — raised after exhausted 429 retries.

The handler reads its API key from ``ctx.secrets`` at *invocation time*,
never caches it (L-102 §7 — credential resolution per call). For Phase-3
unit tests the resolver is faked via a constructor-injected callable; the
runtime (L-103, deferred) will wire the real :class:`SecretResolver`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, Mapping
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas.properties import Secret as SecretRef
from ._contract import (
    Signal,
    SourceContext,
    SourceHealth,
)
from ._egress import guarded_async_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class MediaCloudConfig(BaseModel):
    """MediaCloud source-handler config.

    ``api_key_secret`` resolves through the runtime credential vault — the
    raw key is never stored in the descriptor (L-102 §7 / topology §9.9).

    ``query`` is the MediaCloud query DSL string. Syntax mirrors
    Elasticsearch query-string (booleans, phrase quoting, field qualifiers
    such as ``language:pt`` or ``media_id:1234``).

    ``collections`` narrows the search to specific MediaCloud collection
    IDs (e.g. country-specific corpora like ``US_NATIONAL = 34412234``).

    ``language`` is an ISO-639-1 hint applied as a server-side filter when
    set (in addition to anything in ``query``).

    ``lookback_days`` is a floor on the cursor window: even if the runtime
    passes ``since=None``, the handler will not page back further than
    ``now - lookback_days`` to bound first-run volume.
    """

    model_config = ConfigDict(extra="forbid")

    api_key_secret: SecretRef
    query: str = Field(min_length=1, max_length=4096)

    @field_validator("api_key_secret", mode="before")
    @classmethod
    def _coerce_vault_id(cls, value: Any) -> Any:
        """Accept a bare vault-id string for ``api_key_secret``.

        Committed SourceDescriptor YAMLs carry the vault ref as a property-
        factory value (``{factory_kind: secret, raw: "<vault-id>"}``); the
        runtime's :func:`legba.runtime.source_factory._unwrap_factory_dict`
        unwraps that to the bare ``raw`` string before this schema parses
        (every other keyed handler — acled / gdelt / telegram — already
        types its vault ref as plain ``str``). Coercing here keeps BOTH
        construction surfaces working: tests that build a ``SecretRef``
        directly, and descriptors arriving through the source factory.
        """
        if isinstance(value, str):
            return SecretRef(raw=value)
        return value
    collections: list[int] | None = None
    language: str | None = Field(default=None, max_length=8)
    lookback_days: int = Field(default=1, ge=1, le=90)
    page_size: int = Field(default=1000, ge=10, le=1000)

    # Rate-limit / retry knobs — exposed so an operator can tighten under
    # bursty load without redeploying. Defaults are the upstream-advertised
    # safe band.
    rate_limit_backoff_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    rate_limit_max_retries: int = Field(default=4, ge=0, le=10)

    # Allow tests + dev to point at a fixture URL.
    api_base_url: str = Field(default="https://search.mediacloud.org/api")

    # If True the handler fetches the article body when the response omits
    # ``story_text``; if False, raw_body is left empty and downstream
    # enrichers handle full-text extraction.
    fetch_missing_text: bool = True
    full_text_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)


# ---------------------------------------------------------------------------
# Exception shapes — mirror L-102 §7 failure semantics.
# ---------------------------------------------------------------------------


class MediaCloudHttpError(Exception):
    """Non-transient HTTP failure (4xx other than 429) — surfaces as the
    runtime's ``HardFailure`` shape."""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"MediaCloud HTTP {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class MediaCloudRateLimited(Exception):
    """Raised after exhausting ``rate_limit_max_retries`` 429s. Maps to the
    L-102 §7 ``TransientFailure`` — the runtime will retry the whole pull
    per descriptor retry policy."""


# ---------------------------------------------------------------------------
# Credential resolver shim — Phase 3 standalone surface.
# ---------------------------------------------------------------------------


# The resolver receives the vault-id STRING (``SecretRef.raw``) and may
# return ``str`` or ``bytes`` — matching the runtime's
# ``StandardDeps.secrets_resolve`` / ``CredentialVault.resolve`` contract.
SecretResolverFn = Callable[[str], Awaitable["str | bytes"]]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


# Cursor state-store key. Scoped per (target_id, source_id) by the runtime.
_CURSOR_KEY = "mediacloud_cursor"


class MediaCloudSourceHandler:
    """L-102 source-kind handler for MediaCloud.

    Construction takes an optional HTTP client (so tests can inject a
    ``MockTransport``) and an optional secret resolver (for the same
    reason). The runtime (L-103) will pass real ones at activation time.
    """

    kind: ClassVar[str] = "mediacloud"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.mediacloud/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = MediaCloudConfig

    def __init__(
        self,
        config: MediaCloudConfig,
        *,
        secret_resolver: SecretResolverFn | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._secret_resolver = secret_resolver
        self._http_client = http_client
        self._owns_client = http_client is None
        # Last-success bookkeeping for health reports.
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._last_cursor: str | None = None
        self._rows_pulled_24h: int = 0
        self._rate_limit_remaining: int | None = None
        # Window for the rolling 24h counter — reset on first event past
        # the window edge.
        self._window_started_at = datetime.now(tz=timezone.utc)

    # ------------------------------------------------------------------ #
    # Lifecycle (no-op except for connection management).
    # ------------------------------------------------------------------ #

    async def on_configure(self, ctx: SourceContext | None = None) -> None:
        # Lazy http client creation deferred to first pull; on_configure is
        # validated config-only.
        return None

    async def on_activate(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_pause(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_resume(self, ctx: SourceContext | None = None) -> None:
        return None

    async def on_retire(self, ctx: SourceContext | None = None) -> None:
        await self._maybe_close_client()

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Yield MediaCloud stories since ``since`` (or since
        ``now - lookback_days`` if None).

        Cursor: ``(start_date, pagination_token)`` — persisted in
        ``ctx.state_store`` under ``mediacloud_cursor``. On a clean run the
        token is omitted and the API returns the first page; on resume the
        token from state continues exactly where we left off.

        Re-emission is allowed; downstream dedupe handles overlap windows.
        """
        cfg = self._config
        client = await self._ensure_client()
        api_key = await self._resolve_api_key()

        cursor_state = await self._load_cursor(ctx)
        start_date = self._compute_start_date(since, cursor_state, cfg.lookback_days)
        end_date = datetime.now(tz=timezone.utc)

        # If the cursor was persisted with a different start_date (cold
        # restart after a long pause), prefer the descriptor's lookback
        # window and discard the stale token.
        pagination_token: str | None = cursor_state.get("pagination_token") if (
            cursor_state.get("start_date") == start_date.date().isoformat()
        ) else None

        pages_pulled = 0
        stories_pulled = 0
        try:
            while True:
                page, next_token = await self._fetch_page(
                    client=client,
                    api_key=api_key,
                    start_date=start_date,
                    end_date=end_date,
                    pagination_token=pagination_token,
                )
                pages_pulled += 1

                for story in page:
                    signal = await self._story_to_signal(
                        ctx=ctx,
                        story=story,
                        client=client,
                        api_key=api_key,
                    )
                    if signal is not None:
                        stories_pulled += 1
                        self._bump_counter()
                        yield signal

                # Persist progress after each page so a crash mid-scan
                # resumes cleanly.
                pagination_token = next_token
                await ctx.state_store.set(
                    _CURSOR_KEY,
                    {
                        "start_date": start_date.date().isoformat(),
                        "pagination_token": pagination_token,
                        "last_fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                    },
                )
                self._last_cursor = pagination_token

                if not pagination_token or not page:
                    break

            self._last_success_at = datetime.now(tz=timezone.utc)
            self._last_error = None
            ctx.logger.info(
                "mediacloud.pull complete",
                extra={
                    "pages_pulled": pages_pulled,
                    "stories_pulled": stories_pulled,
                    "start_date": start_date.isoformat(),
                },
            )
        except asyncio.CancelledError:
            # Runtime cancellation — propagate per L-102 §2.
            raise
        except MediaCloudRateLimited as exc:
            self._last_error = f"rate-limited: {exc}"
            raise
        except MediaCloudHttpError as exc:
            self._last_error = str(exc)
            raise
        except httpx.HTTPError as exc:
            self._last_error = f"transport error: {exc}"
            raise

    async def health_check(self, ctx: SourceContext | None = None) -> SourceHealth:
        """Tiny probe — ``*:*`` query, limit 1, just to check that the API
        key + reachability are intact. Counts against the rate-limit
        budget; runtime should poll at a polite cadence."""
        cfg = self._config
        try:
            client = await self._ensure_client()
            api_key = await self._resolve_api_key()
            end = datetime.now(tz=timezone.utc)
            start = end - timedelta(days=cfg.lookback_days)
            params = {
                "q": "*",
                "start_date": start.date().isoformat(),
                "end_date": end.date().isoformat(),
                "limit": 1,
            }
            if cfg.collections:
                params["collections"] = ",".join(str(c) for c in cfg.collections)
            if cfg.language:
                params["language"] = cfg.language

            resp = await self._call_api(
                client=client,
                method="GET",
                path="/search/story-list",
                api_key=api_key,
                params=params,
                retry_on_429=False,
            )
            self._update_rate_limit_from_headers(resp.headers)
            return SourceHealth(
                state="healthy",
                last_success_at=self._last_success_at,
                last_error=self._last_error,
                rows_pulled_24h=self._current_window_count(),
                last_cursor=self._last_cursor,
                rate_limit_remaining=self._rate_limit_remaining,
                detail={"endpoint": cfg.api_base_url, "probe": "story-list?limit=1"},
            )
        except MediaCloudRateLimited:
            return SourceHealth(
                state="degraded",
                last_success_at=self._last_success_at,
                last_error="rate-limited at probe time",
                rows_pulled_24h=self._current_window_count(),
                last_cursor=self._last_cursor,
                rate_limit_remaining=self._rate_limit_remaining,
                detail={"endpoint": cfg.api_base_url, "probe": "story-list?limit=1"},
            )
        except Exception as exc:
            return SourceHealth(
                state="unhealthy",
                last_success_at=self._last_success_at,
                last_error=f"{type(exc).__name__}: {exc}",
                rows_pulled_24h=self._current_window_count(),
                last_cursor=self._last_cursor,
                rate_limit_remaining=self._rate_limit_remaining,
                detail={"endpoint": cfg.api_base_url, "probe": "story-list?limit=1"},
            )

    # ------------------------------------------------------------------ #
    # Internal: HTTP + paging
    # ------------------------------------------------------------------ #

    async def _fetch_page(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        start_date: datetime,
        end_date: datetime,
        pagination_token: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        cfg = self._config
        params: dict[str, Any] = {
            "q": cfg.query,
            "start_date": start_date.date().isoformat(),
            "end_date": end_date.date().isoformat(),
            "limit": cfg.page_size,
        }
        if cfg.collections:
            params["collections"] = ",".join(str(c) for c in cfg.collections)
        if cfg.language:
            params["language"] = cfg.language
        if pagination_token:
            params["pagination_token"] = pagination_token

        resp = await self._call_api(
            client=client,
            method="GET",
            path="/search/story-list",
            api_key=api_key,
            params=params,
            retry_on_429=True,
        )
        self._update_rate_limit_from_headers(resp.headers)
        data = resp.json()
        stories = data.get("stories") or data.get("results") or []
        next_token = data.get("pagination_token") or data.get("next") or None
        return stories, next_token

    async def _story_to_signal(
        self,
        *,
        ctx: SourceContext,
        story: Mapping[str, Any],
        client: httpx.AsyncClient,
        api_key: str,
    ) -> Signal | None:
        """Convert a MediaCloud story payload into a :class:`Signal`.

        Returns ``None`` if the story is missing the minimum required
        fields (a stories_id and either a URL or a title) — defensive: the
        upstream API has been seen to emit malformed rows during ingest
        retries.
        """
        stories_id = story.get("stories_id") or story.get("id")
        if not stories_id:
            return None
        url = story.get("url") or story.get("media_url") or ""
        title = story.get("title") or ""
        if not url and not title:
            return None

        publish_date = _parse_publish_date(
            story.get("publish_date") or story.get("publish_at")
        )

        raw_body = story.get("story_text") or story.get("text") or ""
        if not raw_body and self._config.fetch_missing_text and url:
            raw_body = await self._fetch_full_text(client=client, url=url)

        media_id = story.get("media_id")
        media_name = story.get("media_name") or story.get("media", {}).get("name") if isinstance(story.get("media"), dict) else story.get("media_name")
        language = story.get("language") or self._config.language

        # Idempotent content hash for downstream dedupe (tier 2).
        canonical_text = "\n".join(
            [str(stories_id), url or "", title or "", raw_body or ""]
        )
        content_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()

        payload: dict[str, Any] = {
            "external_id": str(stories_id),
            "title": title,
            "url": url,
            "raw_body": raw_body,
            "published_at": publish_date.isoformat() if publish_date else None,
            "media_id": media_id,
            "media_name": media_name,
            "language": language,
        }

        return Signal(
            signal_id=uuid4(),
            source_id=ctx.source_id,
            payload=payload,
            content_hash=content_hash,
            canonical_url=url or None,
            language_hint=language,
            raw_provenance={
                "mediacloud_stories_id": str(stories_id),
                "mediacloud_media_id": media_id,
                "mediacloud_publish_date": story.get("publish_date"),
                "fetched_full_text": bool(raw_body) and not story.get("story_text"),
            },
        )

    async def _fetch_full_text(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
    ) -> str:
        """Best-effort full-text fetch for stories where MediaCloud didn't
        include ``story_text``. Failures degrade silently — downstream
        enrichers (trafilatura, L-141) re-attempt with smarter extraction
        if needed."""
        try:
            resp = await client.get(
                url,
                timeout=self._config.full_text_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "legba-source-mediacloud/0.1"},
            )
            if resp.status_code == 200 and resp.text:
                # We deliberately do NOT parse HTML here — keep raw_body as
                # the source server returned it. Phase 4 enrichers (NER /
                # boilerplate strip) own that layer.
                return resp.text
        except httpx.HTTPError:
            return ""
        return ""

    async def _call_api(
        self,
        *,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        api_key: str,
        params: dict[str, Any] | None = None,
        retry_on_429: bool,
    ) -> httpx.Response:
        """Single API call with optional 429 backoff retries.

        429 responses honour the server's ``Retry-After`` header when
        present; otherwise fall back to
        ``rate_limit_backoff_seconds`` * (2 ** attempt).
        """
        cfg = self._config
        url = cfg.api_base_url.rstrip("/") + path
        headers = {
            "Authorization": f"Token {api_key}",
            "Accept": "application/json",
        }
        attempts_max = cfg.rate_limit_max_retries if retry_on_429 else 0

        attempt = 0
        while True:
            resp = await client.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=30.0,
            )
            if resp.status_code == 429:
                if attempt >= attempts_max:
                    raise MediaCloudRateLimited(
                        f"429 after {attempt + 1} attempts; "
                        f"retry-after={resp.headers.get('Retry-After')}"
                    )
                wait = _retry_after_seconds(
                    resp.headers,
                    fallback=cfg.rate_limit_backoff_seconds * (2 ** attempt),
                )
                await asyncio.sleep(wait)
                attempt += 1
                continue
            if 400 <= resp.status_code < 600:
                raise MediaCloudHttpError(resp.status_code, resp.text)
            return resp

    # ------------------------------------------------------------------ #
    # Internal: cursor + counters
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_start_date(
        since: datetime | None,
        cursor_state: Mapping[str, Any],
        lookback_days: int,
    ) -> datetime:
        """Pick the actual start_date for this pull.

        Order of precedence: explicit ``since`` arg → resume from cursor's
        ``start_date`` → ``now - lookback_days``.
        """
        if since is not None:
            return _ensure_utc(since)
        cursor_start = cursor_state.get("start_date")
        if cursor_start:
            try:
                # Stored as YYYY-MM-DD; widen to midnight UTC.
                return datetime.fromisoformat(cursor_start).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        return datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)

    async def _load_cursor(self, ctx: SourceContext) -> dict[str, Any]:
        raw = await ctx.state_store.get(_CURSOR_KEY)
        if isinstance(raw, dict):
            return raw
        return {}

    def _bump_counter(self) -> None:
        now = datetime.now(tz=timezone.utc)
        if now - self._window_started_at >= timedelta(hours=24):
            self._window_started_at = now
            self._rows_pulled_24h = 0
        self._rows_pulled_24h += 1

    def _current_window_count(self) -> int:
        now = datetime.now(tz=timezone.utc)
        if now - self._window_started_at >= timedelta(hours=24):
            return 0
        return self._rows_pulled_24h

    def _update_rate_limit_from_headers(self, headers: httpx.Headers) -> None:
        # MediaCloud surfaces remaining quota as ``X-RateLimit-Remaining``
        # on most paths; some deployments use ``RateLimit-Remaining``.
        for h in ("X-RateLimit-Remaining", "RateLimit-Remaining"):
            if h in headers:
                try:
                    self._rate_limit_remaining = int(headers[h])
                except ValueError:
                    pass
                return

    # ------------------------------------------------------------------ #
    # Internal: client + credentials
    # ------------------------------------------------------------------ #

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = guarded_async_client(timeout=30.0)
            self._owns_client = True
        return self._http_client

    async def _maybe_close_client(self) -> None:
        if self._http_client is not None and self._owns_client:
            try:
                await self._http_client.aclose()
            except Exception:                                       # pragma: no cover
                logger.exception("mediacloud httpx client close failed")
        self._http_client = None

    async def _resolve_api_key(self) -> str:
        """Resolve the configured vault ref to the live API key.

        The production resolver is the runtime's
        ``StandardDeps.secrets_resolve`` (``async (vault_id: str) -> bytes``,
        backed by ``CredentialVault.resolve`` — raises ``MissingSecretError``
        when the vault has no such key: the loud activation-gating failure).
        We therefore pass the vault-id STRING (``SecretRef.raw``), not the
        ``SecretRef`` wrapper, and tolerate a bytes return.
        """
        if self._secret_resolver is None:
            raise RuntimeError(
                "MediaCloudSourceHandler has no secret resolver wired; "
                "the runtime (L-103) injects one at activation time."
            )
        ref = self._config.api_key_secret
        secret_id = ref.raw if isinstance(ref, SecretRef) else str(ref)
        resolved = await self._secret_resolver(secret_id)
        if isinstance(resolved, (bytes, bytearray)):
            return bytes(resolved).decode("utf-8")
        return str(resolved)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _retry_after_seconds(headers: httpx.Headers | Mapping[str, str], *, fallback: float) -> float:
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return float(fallback)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        # HTTP-date form not supported by upstream — fall back.
        return float(fallback)


def _parse_publish_date(raw: Any) -> datetime | None:
    """Parse the upstream ``publish_date`` field into a tz-aware datetime.

    MediaCloud emits ISO 8601 with or without tz, and occasionally a date
    only. Returns None on unparseable input — callers must tolerate.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if isinstance(raw, str):
        # Strip a trailing 'Z' the API sometimes emits.
        candidate = raw.rstrip("Z")
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            # Date-only fallback.
            try:
                dt = datetime.strptime(raw[:10], "%Y-%m-%d")
            except ValueError:
                return None
        return _ensure_utc(dt)
    return None


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


__all__ = [
    "MediaCloudConfig",
    "MediaCloudSourceHandler",
    "MediaCloudHttpError",
    "MediaCloudRateLimited",
]
