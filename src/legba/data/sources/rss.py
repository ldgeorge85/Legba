# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSS / Atom source handler (L-130).

Implements the L-102 source-kind contract for RSS 2.0 and Atom 1.0 feeds.

Behavior:

  * ``pull(ctx, since)``: fetch the feed via ``httpx`` honoring stored
    ETag / Last-Modified, parse via ``feedparser``, yield one
    :class:`Signal` per entry whose ``published_at`` is strictly after
    ``since``. On HTTP 304 the iterator is empty.
  * Cursor state ``(etag, last_modified)`` is persisted via
    ``ctx.state_store`` under key :data:`_RSS_CURSOR_KEY` so subsequent
    pulls send ``If-None-Match`` / ``If-Modified-Since``.
  * Healthcheck: a conditional GET with the stored cursor. 200/304 →
    healthy; transient (5xx / timeout / network) → degraded; persistent
    (4xx other than 304 / parse-fail) → unhealthy.

Failure semantics (L-102 §7):

  * Feed parse failure → log, yield nothing, ``last_error`` set, health
    next probe reports ``degraded`` (parser unrecoverable on the same
    payload).
  * Transient network error → one retry, then yield nothing.
  * 4xx / 5xx (other than 304) → no retry, yield nothing; future
    ``health_check`` surfaces the latest status.

This module never imports from ``legba.data.runtime`` — the runtime (L-103)
is not yet landed. It depends only on the structural-typing surface in
``_contract.py``.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from time import struct_time
from typing import Any, AsyncIterator, ClassVar
from urllib.parse import urlparse

import feedparser
import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from ._contract import Signal, SourceContext, SourceHealth
from ._egress import guarded_async_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class RSSConfig(BaseModel):
    """Pydantic config schema for :class:`RSSSourceHandler`.

    Used at descriptor-validation time (per L-101 / L-102 §1). The runtime
    parses each ``SourceBinding.config`` against this model before the
    handler is activated.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=4096)
    parser: str = Field(default="auto", pattern=r"^(auto|rss|atom)$")
    user_agent: str = Field(default="Legba/2.0", max_length=256)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_RSS_CURSOR_KEY = "rss_cursor"
_RSS_HEALTH_KEY = "rss_health"
_DEFAULT_TIMEOUT_S = 30
_DEFAULT_RETRIES_FOR_TRANSIENT = 1
_TRANSIENT_STATUS = {502, 503, 504}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class RSSSourceHandler:
    """Source handler for RSS 2.0 / Atom 1.0 feeds.

    L-102 conformance:

      * ``kind = "rss"``, ``family = "source"``.
      * Owns its cursor in ``ctx.state_store`` (ETag + Last-Modified).
      * Yields :class:`Signal` instances; idempotent — downstream dedupe
        handles overlap windows.
      * Exposes ``health_check`` and lifecycle hooks (default no-op).
    """

    # --- L-102 §1 class-vars ------------------------------------------------
    kind: ClassVar[str] = "rss"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.rss/2-0-0"
    config_schema: ClassVar[type[BaseModel]] = RSSConfig
    handler_version: ClassVar[str] = "0.1.0"
    # DQ-H5b (#88) — state-store key under which this handler records its poll
    # health, so the source actor can read the WHY for a non-productive poll.
    health_state_key: ClassVar[str] = _RSS_HEALTH_KEY

    def __init__(
        self,
        config: RSSConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        feed_parser: Any = feedparser,
    ) -> None:
        """Construct a handler bound to a parsed :class:`RSSConfig`.

        Parameters
        ----------
        config:
            Validated handler config. The runtime constructs this from the
            descriptor's ``SourceBinding.config`` block.
        http_client:
            Optional pre-built ``httpx.AsyncClient``. Tests inject a client
            wired to a mock transport; production uses the per-pull client
            this class creates on demand.
        feed_parser:
            ``feedparser`` module override (tests can pass a stub to inject
            malformed-feed behavior).
        """
        self._config = config
        self._client = http_client
        self._feedparser = feed_parser

    # ------------------------------------------------------------------ pull

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Async-generator yielding :class:`Signal` per new feed entry.

        ``since`` is a hint — entries strictly after this timestamp are
        emitted. The downstream dedupe filter handles overlap.

        State:
          ``ctx.state_store[_RSS_CURSOR_KEY] = {"etag", "last_modified"}``
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

        body_text = response.text
        feed = self._safe_parse(body_text)
        if feed is None:
            await self._record_health(
                ctx,
                state="degraded",
                last_error="feed parse failure",
                detail={"status": response.status_code},
            )
            return

        emitted = 0
        for entry in getattr(feed, "entries", []) or []:
            signal = self._entry_to_signal(entry, ctx=ctx)
            if signal is None:
                continue
            if since is not None and _is_not_after(signal, since):
                continue
            emitted += 1
            yield signal

        # Update cursor with whatever the server gave us (only on 200).
        new_cursor = {
            "etag": response.headers.get("etag", "") or "",
            "last_modified": response.headers.get("last-modified", "") or "",
        }
        await ctx.state_store.set(_RSS_CURSOR_KEY, new_cursor)
        await self._record_health(
            ctx,
            state="healthy",
            last_success_at=datetime.now(tz=timezone.utc),
            detail={
                "status": response.status_code,
                "entries_yielded": emitted,
                "etag": new_cursor["etag"],
            },
        )

    # ----------------------------------------------------------- health_check

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Conditional GET probe. Falls back to a fresh GET when no cursor.

        Returns the last recorded health summary if a probe attempt fails
        outright (e.g., DNS failure) so a single bad poll doesn't mask
        history.
        """
        cursor = await self._load_cursor(ctx)
        headers = self._build_conditional_headers(cursor)

        previous = await ctx.state_store.get(_RSS_HEALTH_KEY) or {}

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
                detail={
                    **previous.get("detail", {}),
                    "probe": "network",
                },
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
        raw = await ctx.state_store.get(_RSS_CURSOR_KEY)
        if not isinstance(raw, dict):
            return {}
        # Defensive: only carry strings forward.
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
                    "rss.fetch.transient attempt=%d url=%s err=%s",
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
                    "rss.fetch.transient attempt=%d url=%s status=%d",
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

    def _safe_parse(self, body_text: str) -> Any | None:
        """Parse via ``feedparser``. Returns ``None`` on hard parse failure.

        ``feedparser`` is forgiving — it almost always returns a result
        object, with the ``bozo`` flag set on malformed feeds. We treat
        ``bozo`` + no entries + no feed metadata as a hard failure.
        """
        try:
            parsed = self._feedparser.parse(body_text)
        except Exception as exc:                        # pragma: no cover
            logger.warning("feedparser raised: %s", exc)
            return None

        # feedparser sets `bozo` to truthy on malformed; treat
        # "bozo + no usable content" as parse failure. Some valid feeds
        # still trip bozo (e.g., character-encoding quirks) but supply
        # entries — those we accept.
        entries = getattr(parsed, "entries", []) or []
        feed_meta = getattr(parsed, "feed", {}) or {}
        if getattr(parsed, "bozo", 0) and not entries and not feed_meta:
            return None
        return parsed

    def _entry_to_signal(
        self, entry: Any, *, ctx: SourceContext,
    ) -> Signal | None:
        """Map a feedparser entry to a :class:`Signal`."""
        title = _safe_str(entry, "title")
        link = _safe_str(entry, "link")
        summary = _safe_str(entry, "summary") or _safe_str(entry, "description")
        author = _safe_str(entry, "author")
        external_id = (
            _safe_str(entry, "id")
            or _safe_str(entry, "guid")
            or link
            or ""
        )
        published_at = _extract_published(entry)
        tags = _extract_tags(entry)
        raw_body = (
            _extract_content_html(entry)
            or summary
            or ""
        )

        # Skip entries that have NOTHING usable (no id, no link, no title)
        # — these are usually parse-junk artifacts.
        if not external_id and not link and not title:
            return None

        payload: dict[str, Any] = {
            "external_id": external_id,
            "published_at": (
                published_at.astimezone(timezone.utc).isoformat()
                if published_at is not None
                else None
            ),
            "title": title,
            "link": link,
            "summary": summary,
            "author": author,
            "tags": tags,
            "raw_body": raw_body,
            "source_url": self._config.url,
        }

        # Stash the parsed datetime on the signal itself so downstream
        # filtering by `since` doesn't have to re-parse the iso string.
        if published_at is not None:
            payload["_published_at_dt"] = published_at

        canonical = link or external_id or None
        content_hash = hashlib.sha256(
            (
                (external_id or "")
                + "\x1f"
                + (title or "")
                + "\x1f"
                + (raw_body or "")
            ).encode("utf-8")
        ).hexdigest()

        # Source-first pivot (P-06): the Signal is target-agnostic — the
        # observation is source-owned, ``target_id`` left the schema entirely
        # (it lives only on derived analyst outputs). We stamp ``source_id``
        # (the ORIGIN SourceDescriptor.id) + the modality; the per-source
        # baseline pipeline fills the structured-filter columns later.
        return Signal(
            source_id=ctx.source_id,
            modality="text",
            payload=payload,
            content_hash=content_hash,
            canonical_url=canonical,
            language_hint=_safe_str(entry, "language") or None,
            raw_provenance={
                "feed_url": self._config.url,
                "fetch_kind": "rss",
            },
        )

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
            await ctx.state_store.set(_RSS_HEALTH_KEY, record)
        except Exception:                                # pragma: no cover
            ctx.logger.warning("rss.health.persist_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_not_after(signal: Signal, since: datetime) -> bool:
    """Return True iff signal's parsed published_at is <= ``since``."""
    payload_dt = signal.payload.get("_published_at_dt")
    if isinstance(payload_dt, datetime):
        # Both must be aware/naive consistently — normalize to aware UTC.
        a = payload_dt if payload_dt.tzinfo else payload_dt.replace(tzinfo=timezone.utc)
        b = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        return a <= b
    # If no parsed date, don't filter — emit it; downstream dedupe handles it.
    return False


def _safe_str(entry: Any, key: str) -> str:
    """Safely read a string-like field off a feedparser entry."""
    if entry is None:
        return ""
    if isinstance(entry, dict):
        val = entry.get(key)
    else:
        val = getattr(entry, key, None)
    if val is None:
        return ""
    if isinstance(val, (str, bytes)):
        return val.decode("utf-8", "replace") if isinstance(val, bytes) else val
    # feedparser sometimes wraps in detail dicts.
    if isinstance(val, dict):
        for k in ("value", "term", "label", "name"):
            if k in val and isinstance(val[k], str):
                return val[k]
    return str(val)


def _extract_published(entry: Any) -> datetime | None:
    """Pull a UTC-aware datetime from feedparser's various published fields."""
    # 1) The struct_time fields (feedparser populates these from the wire).
    #    feedparser documents these as UTC struct_time, so use
    #    ``calendar.timegm`` (UTC-correct) rather than ``time.mktime``
    #    (which treats input as local time and would skew by tz offset).
    for st_field in ("published_parsed", "updated_parsed", "created_parsed"):
        st = getattr(entry, st_field, None)
        if isinstance(st, struct_time):
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
        if isinstance(entry, dict) and isinstance(entry.get(st_field), struct_time):
            return datetime.fromtimestamp(
                calendar.timegm(entry[st_field]), tz=timezone.utc
            )
    # 2) Fall back to the raw string fields parsed via email.utils.
    for s_field in ("published", "updated", "created"):
        raw = _safe_str(entry, s_field)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        # Try ISO-8601 (Atom).
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _extract_tags(entry: Any) -> list[str]:
    """Pull a flat list of tag strings off an entry."""
    raw = None
    if isinstance(entry, dict):
        raw = entry.get("tags") or entry.get("categories")
    else:
        raw = getattr(entry, "tags", None) or getattr(entry, "categories", None)
    if not raw:
        return []
    out: list[str] = []
    for t in raw:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict):
            term = t.get("term") or t.get("label") or t.get("name")
            if isinstance(term, str):
                out.append(term)
        else:
            term = getattr(t, "term", None) or getattr(t, "label", None)
            if isinstance(term, str):
                out.append(term)
    return out


def _extract_content_html(entry: Any) -> str:
    """Prefer entry.content[0].value (Atom + extended RSS) over summary."""
    content = None
    if isinstance(entry, dict):
        content = entry.get("content")
    else:
        content = getattr(entry, "content", None)
    if not content:
        return ""
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return str(first.get("value") or "")
        return str(getattr(first, "value", "") or "")
    if isinstance(content, str):
        return content
    return ""


__all__ = [
    "RSSConfig",
    "RSSSourceHandler",
]
