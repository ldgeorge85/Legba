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
import re
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

# Conditional-GET stale-edge guard (Fix C). A stale CDN edge (observed:
# Cloudflare on crisisgroup.latest) can pin a CONSTANT ETag and return a
# perpetual 304 even as the origin content changes, silencing the feed
# forever. After this many CONSECUTIVE 304s we drop the conditional-GET
# headers for ONE pull to force an unconditional refetch, so a pinned edge
# ETag can't permanently mute the feed. Conditional-GET is otherwise kept
# (it is load-bearing for bandwidth); the forced refetch is the exception,
# not the rule. The consecutive-304 count is persisted in the cursor.
_MAX_CONSECUTIVE_304 = 12


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
          ``ctx.state_store[_RSS_CURSOR_KEY] =
            {"etag", "last_modified", "consecutive_304"}``
        """
        cursor = await self._load_cursor(ctx)

        # Fix C — stale-edge guard. If we've taken too many CONSECUTIVE 304s a
        # CDN edge may be pinning a constant ETag/Last-Modified and muting an
        # actually-changing origin. Force ONE unconditional refetch (drop the
        # conditional-GET headers) to break the pin. Conditional-GET resumes on
        # the next pull from whatever the unconditional response validates to.
        consecutive_304 = _coerce_int(cursor.get("consecutive_304"))
        force_unconditional = consecutive_304 >= _MAX_CONSECUTIVE_304
        if force_unconditional:
            headers: dict[str, str] = {}
            ctx.logger.info(
                "rss.conditional.force_refetch url=%s consecutive_304=%d "
                "(stale-edge guard)",
                self._config.url, consecutive_304,
            )
        else:
            headers = self._build_conditional_headers(cursor)

        response = await self._fetch_with_retry(headers=headers, ctx=ctx)
        if response is None:
            # Transient + retry exhausted; health probe surfaces the cause.
            return

        if response.status_code == 304:
            # Count consecutive 304s so the stale-edge guard above can fire.
            # A forced (unconditional) pull that STILL 304s is genuinely
            # unchanged — reset the counter so we don't refetch every pull.
            next_304 = 0 if force_unconditional else consecutive_304 + 1
            await ctx.state_store.set(
                _RSS_CURSOR_KEY,
                {
                    "etag": str(cursor.get("etag") or ""),
                    "last_modified": str(cursor.get("last_modified") or ""),
                    "consecutive_304": next_304,
                },
            )
            await self._record_health(
                ctx,
                state="healthy",
                last_success_at=datetime.now(tz=timezone.utc),
                detail={
                    "status": 304,
                    "note": "not modified",
                    "consecutive_304": next_304,
                },
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

        # Update cursor with whatever the server gave us (only on 200). A 200
        # is fresh content, so the consecutive-304 stale-edge counter resets.
        new_cursor = {
            "etag": response.headers.get("etag", "") or "",
            "last_modified": response.headers.get("last-modified", "") or "",
            "consecutive_304": 0,
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

    async def _load_cursor(self, ctx: SourceContext) -> dict[str, Any]:
        raw = await ctx.state_store.get(_RSS_CURSOR_KEY)
        if not isinstance(raw, dict):
            return {}
        # Defensive: only carry strings forward (plus the int 304 counter).
        return {
            "etag": str(raw.get("etag") or ""),
            "last_modified": str(raw.get("last_modified") or ""),
            "consecutive_304": _coerce_int(raw.get("consecutive_304")),
        }

    @staticmethod
    def _build_conditional_headers(cursor: dict[str, Any]) -> dict[str, str]:
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


def _coerce_int(val: Any) -> int:
    """Coerce a (possibly stringified / absent) counter to a non-negative int.

    State-store backends may round-trip the consecutive-304 counter through
    JSON; tolerate ints, numeric strings, and missing/garbage values (→ 0).
    """
    try:
        n = int(val)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


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


# Map each feedparser ``*_parsed`` struct_time field to the raw-string field
# it was parsed from, so the midnight-collapse recovery (below) can re-parse
# the SAME field's raw text when feedparser dropped the time-of-day.
_PARSED_TO_RAW = {
    "published_parsed": "published",
    "updated_parsed": "updated",
    "created_parsed": "created",
}

# An explicit clock time anywhere in a raw date string — e.g. "13:30" in the
# iaea "26-06-26  13:30" shape. A trailing all-zero time ("00:00[:00]") is
# excluded so a feed that really publishes at midnight is NOT mistaken for a
# time feedparser lost (it would re-parse to the same midnight anyway, but we
# avoid the extra work + keep the struct_time fast path for true-midnight).
_HHMM_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b")


def _struct_time_is_midnight(st: struct_time) -> bool:
    """True iff a feedparser struct_time sits at exactly 00:00:00.

    This is the signature of feedparser dropping the time-of-day on a date
    shape it parsed only to day precision (the iaea ``YY-MM-DD  HH:MM`` case).
    """
    return st.tm_hour == 0 and st.tm_min == 0 and st.tm_sec == 0


def _raw_has_explicit_time(raw: str) -> bool:
    """True iff the raw string carries an explicit, non-midnight HH:MM."""
    m = _HHMM_RE.search(raw)
    if m is None:
        return False
    hh, mm, ss = m.group(1), m.group(2), m.group(3)
    return not (hh in ("0", "00") and mm == "00" and (ss in (None, "00")))


def _parse_tolerant_datetime(raw: str) -> datetime | None:
    """Best-effort parse for whitespace-mangled / 2-digit-year date shapes.

    Recovers the iaea-style ``"26-06-26  13:30"`` (2-digit year, no day-name,
    no timezone, double-spaced) that feedparser collapses to midnight. The
    string is whitespace-normalized, then matched against a small set of
    explicit ``strptime`` patterns. A bare 2-digit year is expanded to 20YY
    (these are current-news feeds — a 2-digit year is this century). Returns a
    UTC-aware datetime, or ``None`` when no pattern matches (caller falls
    through to the standard RFC822 / ISO parsers).
    """
    if not raw:
        return None
    # Collapse runs of whitespace (the iaea shape is double-spaced) and trim.
    norm = re.sub(r"\s+", " ", raw).strip()
    if not norm:
        return None

    # Patterns are tried most-specific first. Each is a (strptime fmt,
    # two_digit_year) pair; the flag tells us to expand a %y century-window
    # ambiguity deterministically to 20YY rather than rely on strptime's
    # 1969-cutover pivot.
    patterns: tuple[tuple[str, bool], ...] = (
        ("%y-%m-%d %H:%M:%S", True),
        ("%y-%m-%d %H:%M", True),
        ("%Y-%m-%d %H:%M:%S", False),
        ("%Y-%m-%d %H:%M", False),
        ("%d-%m-%y %H:%M:%S", True),
        ("%d-%m-%y %H:%M", True),
    )
    for fmt, two_digit_year in patterns:
        try:
            dt = datetime.strptime(norm, fmt)
        except ValueError:
            continue
        if two_digit_year and dt.year < 100:
            # strptime already pivots %y, but pin to this century explicitly so
            # a "26" can never resolve to 1926 across a future strptime change.
            dt = dt.replace(year=2000 + (dt.year % 100))
        return dt.replace(tzinfo=timezone.utc)
    return None


def _extract_published(entry: Any) -> datetime | None:
    """Pull a UTC-aware datetime from feedparser's various published fields."""
    # 1) The struct_time fields (feedparser populates these from the wire).
    #    feedparser documents these as UTC struct_time, so use
    #    ``calendar.timegm`` (UTC-correct) rather than ``time.mktime``
    #    (which treats input as local time and would skew by tz offset).
    #
    #    MIDNIGHT-COLLAPSE RECOVERY (Fix A): some feeds carry a date shape
    #    feedparser parses only to DAY precision (e.g. iaea ``YY-MM-DD HH:MM``,
    #    2-digit year, no day-name, no tz, double-spaced) — it sets the
    #    struct_time to 00:00:00, silently dropping the real time-of-day. When
    #    the struct_time is exactly midnight AND the field's raw string carries
    #    an explicit non-midnight HH:MM, we re-parse the raw text with a
    #    tolerant parser to recover the true time. A well-formed feed (the time
    #    survived in the struct_time, or it genuinely publishes at 00:00) keeps
    #    the existing fast path unchanged.
    for st_field in ("published_parsed", "updated_parsed", "created_parsed"):
        st = getattr(entry, st_field, None)
        if not isinstance(st, struct_time) and isinstance(entry, dict):
            cand = entry.get(st_field)
            st = cand if isinstance(cand, struct_time) else None
        if not isinstance(st, struct_time):
            continue
        if _struct_time_is_midnight(st):
            raw = _safe_str(entry, _PARSED_TO_RAW[st_field])
            if raw and _raw_has_explicit_time(raw):
                recovered = _parse_tolerant_datetime(raw)
                if recovered is not None:
                    return recovered.astimezone(timezone.utc)
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
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
            pass
        # Try ISO-8601 (Atom).
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
        # Last resort: the tolerant parser for whitespace-mangled / 2-digit-year
        # shapes that arrive ONLY as a raw string (no struct_time at all).
        recovered = _parse_tolerant_datetime(raw)
        if recovered is not None:
            return recovered.astimezone(timezone.utc)
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
