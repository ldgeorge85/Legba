# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Common Crawl CC-NEWS source kind handler (L-138).

Pulls WARC dumps from the public ``commoncrawl`` S3 bucket under
``crawl-data/CC-NEWS/<YYYY>/<MM>/``. Free, anonymous read; daily refresh;
~1000 news sites per WARC file. Primary use case is historical backfill
when a new country target lights up — replay a week or month of WARCs
and let downstream filters (language detect, geocode, NER) re-emit the
relevant slice.

Contract: L-102 §2 source-kind ``SourceHandler``. ``kind`` =
``"common_crawl_news"``. Cursor state = last processed WARC S3 key
(string). Idempotent re-emission across overlapping windows is fine —
downstream dedupe (L-151) collapses duplicates by ``content_hash`` +
``canonical_url``.

External dependencies (declared as new repo deps in summary):

* ``warcio>=1.7`` — synchronous WARC record parsing. The library doesn't
  ship an async iterator, so we drive it inside ``asyncio.to_thread``
  against an in-memory buffer streamed from S3.
* ``aiobotocore>=2.7`` — async S3 client. We use it with ``UNSIGNED``
  signature config because the CC bucket is public-read; no AWS
  credentials needed and none should be supplied (signed reads on an
  anonymous-only bucket fail).

Healthcheck: HEAD an anchor WARC file (the index gz at the bucket root,
or the configurable ``healthcheck_key``) to verify S3 reachability.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, ClassVar, Iterable, Sequence
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import Signal, SourceContext, SourceHealth, StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class CommonCrawlNewsConfig(BaseModel):
    """Configuration for the CC-NEWS source handler.

    All fields have sensible defaults so a minimally-specified instance
    pointed at the public bucket works. Per-target instances typically
    override ``host_filter`` (country-scoped) and ``language_filter``.
    """

    model_config = ConfigDict(extra="forbid")

    s3_bucket: str = Field(
        default="commoncrawl",
        description=(
            "S3 bucket holding CC-NEWS WARC dumps. Defaults to the public "
            "bucket; override only for private mirrors."
        ),
    )
    s3_region: str = Field(
        default="us-east-1",
        description="S3 region for the bucket. Public CC bucket is us-east-1.",
    )
    s3_endpoint_url: str | None = Field(
        default=None,
        description=(
            "Optional S3 endpoint URL (e.g. for a MinIO mirror or LocalStack). "
            "When None, aiobotocore uses the default AWS endpoint."
        ),
    )
    prefix: str = Field(
        default="crawl-data/CC-NEWS",
        description="Key prefix below which monthly partitions live.",
    )
    lookback_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description=(
            "How many days back from `since` (or now) to scan. Caps the "
            "month-partitions we list when no cursor is available."
        ),
    )
    language_filter: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of ISO 639-3 codes (e.g. ['eng', 'spa']) to keep. "
            "Filtered against the `WARC-Identified-Content-Language` header. "
            "None = keep all languages. Records lacking the header are dropped "
            "iff this list is set (conservative)."
        ),
    )
    host_filter: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of regex patterns matched against the URI hostname. "
            "E.g. ['.*\\.br$'] for Brazil-scoped pulls. None = keep all hosts."
        ),
    )
    max_records_per_run: int = Field(
        default=1000,
        ge=1,
        description=(
            "Hard cap on Signals emitted per pull invocation. Bounds runtime "
            "and downstream pressure on a large WARC."
        ),
    )
    max_warc_files_per_run: int = Field(
        default=4,
        ge=1,
        description=(
            "Hard cap on WARC files streamed per pull invocation. Each WARC "
            "is ~1 GB; the runtime can re-invoke `pull` to continue."
        ),
    )
    max_body_bytes: int = Field(
        default=1_000_000,
        ge=1024,
        description="Truncate WARC response payload bodies above this size (bytes).",
    )
    healthcheck_key: str | None = Field(
        default=None,
        description=(
            "S3 key to HEAD for `health_check`. When None, the handler picks "
            "the most recent WARC it has seen, falling back to a list+head of "
            "the prefix root."
        ),
    )

    @field_validator("host_filter")
    @classmethod
    def _compile_host_patterns(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        # Validate each pattern at config time so we fail-fast on a bad regex.
        for pattern in v:
            re.compile(pattern)
        return v


# ---------------------------------------------------------------------------
# Cursor model — what we persist between pulls
# ---------------------------------------------------------------------------


_CURSOR_KEY = "cc_news_cursor"


class _Cursor(BaseModel):
    """Persisted cursor for the CC-NEWS handler.

    ``last_warc_key``: last fully-processed WARC S3 key. On next pull we
    list keys strictly > last_warc_key (lexicographic order matches CC-NEWS
    timestamp order because file names are ISO-ish timestamps).
    ``last_record_id_in_warc``: optional resume-within-file marker for
    handlers that get interrupted mid-WARC. Not used for cross-pull
    progress (we just restart at the next WARC).
    """

    model_config = ConfigDict(extra="forbid")

    last_warc_key: str | None = None
    last_record_id_in_warc: str | None = None
    last_pulled_at: datetime | None = None


# ---------------------------------------------------------------------------
# S3 client factory — kept as a swappable hook so tests can inject
# ---------------------------------------------------------------------------


class S3Client:
    """Minimal interface the handler needs from an S3 client.

    aiobotocore's client satisfies this structurally; tests can supply a
    fake that yields fixture WARC bytes without an actual S3 hop. This is
    NOT a mock in the "no mocks" sense — it's a substitutable in-process
    backend conforming to a public protocol (cf. boto3's UNSIGNED config
    pattern), the same way :class:`InMemoryStateStore` substitutes for
    Redis/Postgres in unit tests.
    """

    async def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - protocol
        raise NotImplementedError

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - protocol
        raise NotImplementedError

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - protocol
        raise NotImplementedError


def _make_aiobotocore_client_factory(config: CommonCrawlNewsConfig):
    """Return an async context manager that yields a configured S3 client.

    Uses anonymous (UNSIGNED) reads because the CC bucket is public-read;
    signing an anonymous request would actually fail with
    InvalidAccessKeyId on a no-creds environment.
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        # Imported here so callers that hand-inject an :class:`S3Client`
        # for tests don't need aiobotocore installed in their environment.
        from aiobotocore.session import get_session
        from botocore import UNSIGNED
        from botocore.config import Config as BotoConfig

        session = get_session()
        client_ctx = session.create_client(
            "s3",
            region_name=config.s3_region,
            endpoint_url=config.s3_endpoint_url,
            config=BotoConfig(signature_version=UNSIGNED),
        )
        async with client_ctx as client:
            yield client

    return factory


# ---------------------------------------------------------------------------
# WARC streaming helpers
# ---------------------------------------------------------------------------


def _parse_warc_response_records(
    warc_bytes: bytes,
    *,
    language_filter: Sequence[str] | None,
    host_patterns: Sequence[re.Pattern[str]] | None,
    max_body_bytes: int,
    skip_until_record_id: str | None = None,
) -> list[dict[str, Any]]:
    """Synchronous WARC parse — returns list of record dicts.

    Runs inside :func:`asyncio.to_thread` from the async caller because
    warcio is sync. Returns dicts so the caller can shape Signals without
    holding warcio types in the async path.
    """
    from warcio.archiveiterator import ArchiveIterator

    out: list[dict[str, Any]] = []
    skipping = skip_until_record_id is not None
    stream = io.BytesIO(warc_bytes)
    for record in ArchiveIterator(stream):
        if record.rec_type != "response":
            continue
        rec_id = record.rec_headers.get_header("WARC-Record-ID") or ""
        if skipping:
            # Resume-at semantics: stop skipping once we see the marker
            # record AND include it in the emitted output (the marker
            # represents the next record to emit, not the last one
            # already emitted).
            if rec_id == skip_until_record_id:
                skipping = False
            else:
                continue

        target_uri = record.rec_headers.get_header("WARC-Target-URI") or ""
        if not target_uri:
            continue

        if host_patterns is not None:
            host = urlparse(target_uri).hostname or ""
            if not any(p.search(host) for p in host_patterns):
                continue

        lang = record.rec_headers.get_header("WARC-Identified-Content-Language")
        if language_filter is not None:
            if not lang:
                # Conservative: drop unidentified-language records when a
                # filter is set, rather than blast them downstream.
                continue
            # CC-NEWS sometimes emits comma-separated language hints
            # (e.g. "eng,spa") — split + intersect.
            langs = {x.strip().lower() for x in lang.split(",") if x.strip()}
            wanted = {x.lower() for x in language_filter}
            if not (langs & wanted):
                continue

        warc_date = record.rec_headers.get_header("WARC-Date") or ""
        try:
            content = record.content_stream().read(max_body_bytes + 1)
        except Exception as exc:  # noqa: BLE001 - record-level resilience
            logger.warning("cc_news: skip record %s body read failed: %s", rec_id, exc)
            continue
        truncated = len(content) > max_body_bytes
        if truncated:
            content = content[:max_body_bytes]

        # HTTP status if recoverable from the embedded HTTP headers.
        http_status: int | None = None
        try:
            if record.http_headers is not None:
                statusline = record.http_headers.statusline or ""
                http_status = int(statusline.split()[0]) if statusline else None
        except Exception:  # noqa: BLE001 - malformed statusline is fine
            http_status = None

        out.append(
            {
                "record_id": rec_id,
                "target_uri": target_uri,
                "warc_date": warc_date,
                "language": lang,
                "body": content,
                "body_truncated": truncated,
                "http_status": http_status,
            }
        )
    return out


def _parse_warc_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # WARC dates are RFC 3339 with trailing Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_signal(
    record: dict[str, Any],
    *,
    target_id: str,
    source_id: str,
) -> Signal:
    body: bytes = record["body"]
    target_uri: str = record["target_uri"]
    content_hash = hashlib.sha256(target_uri.encode("utf-8") + b"\n" + body).hexdigest()
    published_at = _parse_warc_date(record["warc_date"])
    return Signal(
        signal_id=uuid4(),
        source_id=source_id,
        modality="text",
        fetched_at=datetime.now(tz=timezone.utc),
        payload={
            "url": target_uri,
            "raw_body": body,
            "body_truncated": record.get("body_truncated", False),
            "published_at": published_at.isoformat() if published_at else None,
            "language": record.get("language"),
            "http_status": record.get("http_status"),
        },
        content_hash=content_hash,
        canonical_url=target_uri,
        language_hint=(record.get("language") or "").split(",")[0].strip() or None,
        raw_provenance={
            "warc_record_id": record["record_id"],
            "warc_date": record["warc_date"],
        },
    )


# ---------------------------------------------------------------------------
# Month partition enumeration
# ---------------------------------------------------------------------------


def _month_partitions(since: datetime, until: datetime, prefix: str) -> list[str]:
    """List ``<prefix>/<YYYY>/<MM>/`` partition prefixes covering [since, until]."""
    if until < since:
        return []
    out: list[str] = []
    cursor = datetime(since.year, since.month, 1, tzinfo=timezone.utc)
    last = datetime(until.year, until.month, 1, tzinfo=timezone.utc)
    while cursor <= last:
        out.append(f"{prefix.rstrip('/')}/{cursor.year:04d}/{cursor.month:02d}/")
        # advance one month
        year, month = cursor.year, cursor.month + 1
        if month == 13:
            year, month = year + 1, 1
        cursor = datetime(year, month, 1, tzinfo=timezone.utc)
    return out


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass
class _RunStats:
    records_in: int = 0
    records_out: int = 0
    warcs_streamed: int = 0
    last_error: str | None = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class CommonCrawlNewsSourceHandler:
    """Source-kind handler for Common Crawl CC-NEWS WARC dumps.

    Conforms structurally to :class:`legba.data.sources._contract.SourceHandler`.
    """

    kind: ClassVar[str] = "common_crawl_news"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.common_crawl_news/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = CommonCrawlNewsConfig

    def __init__(
        self,
        config: CommonCrawlNewsConfig,
        *,
        s3_client_factory: Any | None = None,
    ) -> None:
        """Construct a handler.

        ``s3_client_factory`` is an async-context-manager factory yielding
        an object satisfying :class:`S3Client`. Production wires this from
        :func:`_make_aiobotocore_client_factory`; tests pass an in-process
        substitute. When None, the aiobotocore-backed factory is built
        lazily on first call.
        """
        self.config = config
        self._s3_client_factory = s3_client_factory or _make_aiobotocore_client_factory(config)
        self._host_patterns: list[re.Pattern[str]] | None = (
            [re.compile(p) for p in config.host_filter] if config.host_filter else None
        )
        self._language_filter: list[str] | None = (
            list(config.language_filter) if config.language_filter else None
        )
        # Internal counters surfaced via health_check / detail.
        self._stats = _RunStats()
        self._last_success_at: datetime | None = None
        self._last_cursor: _Cursor | None = None

    # ---- Cursor I/O -----------------------------------------------------

    async def _load_cursor(self, state: StateStore) -> _Cursor:
        raw = await state.get(_CURSOR_KEY)
        if not raw:
            return _Cursor()
        if isinstance(raw, _Cursor):
            return raw
        try:
            return _Cursor.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - cursor corruption fallback
            logger.warning("cc_news: cursor decode failed (%s); resetting", exc)
            return _Cursor()

    async def _save_cursor(self, state: StateStore, cursor: _Cursor) -> None:
        await state.set(_CURSOR_KEY, cursor.model_dump(mode="json"))
        self._last_cursor = cursor

    # ---- S3 listing -----------------------------------------------------

    async def _list_warcs(
        self,
        client: Any,
        partitions: Iterable[str],
        *,
        start_after: str | None,
        include_start: bool = False,
    ) -> list[str]:
        """List WARC keys across the partition prefixes, lexicographically sorted.

        ``start_after``: drop keys ``<= start_after`` (strict skip) unless
        ``include_start`` is True, in which case drop keys ``< start_after``
        (so ``start_after`` itself remains in the list — needed for mid-WARC
        resume).
        """
        keys: list[str] = []
        for prefix in partitions:
            continuation: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "Bucket": self.config.s3_bucket,
                    "Prefix": prefix,
                }
                if continuation:
                    kwargs["ContinuationToken"] = continuation
                resp = await client.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []) or []:
                    key = obj["Key"]
                    if not key.endswith(".warc.gz") and not key.endswith(".warc"):
                        continue
                    if start_after is not None:
                        if include_start:
                            if key < start_after:
                                continue
                        else:
                            if key <= start_after:
                                continue
                    keys.append(key)
                if resp.get("IsTruncated"):
                    continuation = resp.get("NextContinuationToken")
                    if not continuation:
                        break
                else:
                    break
        keys.sort()
        return keys

    async def _fetch_warc_bytes(self, client: Any, key: str) -> bytes:
        """GET an entire WARC into memory.

        At ~1 GB per WARC the runtime should treat this as a bounded
        I/O operation; for streaming-into-warcio we read in full because
        ArchiveIterator wants a file-like with proper gzip framing.
        """
        resp = await client.get_object(Bucket=self.config.s3_bucket, Key=key)
        body = resp["Body"]
        chunks: list[bytes] = []
        # aiobotocore's StreamingBody supports `async for` over `iter_chunks`
        # but tests often supply a simpler async-readable; both work.
        if hasattr(body, "read"):
            data = await body.read()
            chunks.append(data)
        else:  # pragma: no cover - defensive
            async for chunk in body:  # type: ignore[union-attr]
                chunks.append(chunk)
        if hasattr(body, "close"):
            close = body.close()
            if asyncio.iscoroutine(close):
                await close
        return b"".join(chunks)

    # ---- Pull -----------------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Yield Signals from WARC files newer than the persisted cursor.

        ``since`` is treated as a hint that narrows the month partitions
        we list. The cursor is authoritative for per-file progress; we
        reprocess overlapping windows freely (downstream dedupe handles
        duplicates per L-102 §2 idempotency note).
        """
        stats = _RunStats()
        cursor = await self._load_cursor(ctx.state_store)
        until = datetime.now(tz=timezone.utc)
        window_start = since or (until - timedelta(days=self.config.lookback_days))
        # Ensure window covers prior partitions so a stale cursor still
        # finds its place.
        partitions = _month_partitions(window_start, until, self.config.prefix)

        ctx.logger.info(
            "cc_news pull: partitions=%d cursor=%s window=[%s, %s]",
            len(partitions),
            cursor.last_warc_key,
            window_start.isoformat(),
            until.isoformat(),
        )

        try:
            async with self._s3_client_factory() as client:
                # If the cursor is mid-WARC (resume marker set), we must
                # re-list the same WARC and pick up at the marker. Otherwise
                # start strictly after the last fully-consumed key.
                keys = await self._list_warcs(
                    client,
                    partitions,
                    start_after=cursor.last_warc_key,
                    include_start=cursor.last_record_id_in_warc is not None,
                )
                keys = keys[: self.config.max_warc_files_per_run]
                emitted = 0
                for key in keys:
                    if emitted >= self.config.max_records_per_run:
                        break
                    ctx.logger.debug("cc_news: streaming %s", key)
                    warc_bytes = await self._fetch_warc_bytes(client, key)
                    stats.warcs_streamed += 1
                    skip_until = (
                        cursor.last_record_id_in_warc
                        if key == cursor.last_warc_key
                        else None
                    )
                    records = await asyncio.to_thread(
                        _parse_warc_response_records,
                        warc_bytes,
                        language_filter=self._language_filter,
                        host_patterns=self._host_patterns,
                        max_body_bytes=self.config.max_body_bytes,
                        skip_until_record_id=skip_until,
                    )
                    for rec in records:
                        stats.records_in += 1
                        if emitted >= self.config.max_records_per_run:
                            # Persist mid-file resume cursor.
                            cursor = _Cursor(
                                last_warc_key=key,
                                last_record_id_in_warc=rec["record_id"],
                                last_pulled_at=datetime.now(tz=timezone.utc),
                            )
                            await self._save_cursor(ctx.state_store, cursor)
                            stats.records_out = emitted
                            self._stats = stats
                            return
                        sig = _build_signal(
                            rec,
                            target_id=ctx.target_id,
                            source_id=ctx.source_id,
                        )
                        emitted += 1
                        stats.records_out = emitted
                        yield sig
                    # File fully consumed — advance cursor.
                    cursor = _Cursor(
                        last_warc_key=key,
                        last_record_id_in_warc=None,
                        last_pulled_at=datetime.now(tz=timezone.utc),
                    )
                    await self._save_cursor(ctx.state_store, cursor)
                self._last_success_at = datetime.now(tz=timezone.utc)
        except Exception as exc:  # noqa: BLE001 - propagate via health, raise
            stats.last_error = repr(exc)
            self._stats = stats
            raise
        else:
            self._stats = stats

    # ---- Health ---------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Probe S3 reachability by HEAD-ing a known WARC key.

        Strategy:
          1. If ``healthcheck_key`` is configured, HEAD it.
          2. Else if the cursor has a ``last_warc_key``, HEAD that.
          3. Else LIST the prefix and HEAD the first object found.

        On success, state is ``healthy``; on a 404 or list-empty,
        ``degraded``; on any other exception, ``unhealthy``.
        """
        state = "unhealthy"
        last_error: str | None = None
        detail: dict[str, Any] = {
            "bucket": self.config.s3_bucket,
            "prefix": self.config.prefix,
            "records_in_last_run": self._stats.records_in,
            "records_out_last_run": self._stats.records_out,
            "warcs_streamed_last_run": self._stats.warcs_streamed,
        }
        cursor = self._last_cursor
        if cursor is None:
            try:
                cursor = await self._load_cursor(ctx.state_store)
            except Exception:  # noqa: BLE001 - health is best-effort
                cursor = _Cursor()

        try:
            async with self._s3_client_factory() as client:
                probe_key = self.config.healthcheck_key or cursor.last_warc_key
                if probe_key is None:
                    # List the prefix and HEAD the first hit.
                    resp = await client.list_objects_v2(
                        Bucket=self.config.s3_bucket,
                        Prefix=self.config.prefix.rstrip("/") + "/",
                        MaxKeys=1,
                    )
                    contents = resp.get("Contents") or []
                    if not contents:
                        return SourceHealth(
                            state="degraded",
                            last_success_at=self._last_success_at,
                            last_error="prefix returned no objects",
                            last_cursor=cursor.last_warc_key,
                            detail=detail,
                        )
                    probe_key = contents[0]["Key"]
                detail["probe_key"] = probe_key
                head = await client.head_object(
                    Bucket=self.config.s3_bucket, Key=probe_key
                )
                detail["content_length"] = head.get("ContentLength")
                state = "healthy"
        except Exception as exc:  # noqa: BLE001 - health is the catcher
            last_error = repr(exc)

        return SourceHealth(
            state=state,
            last_success_at=self._last_success_at,
            last_error=last_error,
            rows_pulled_24h=self._stats.records_out,
            last_cursor=cursor.last_warc_key,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# Plugin registration factory (L-102 §8 path 2)
# ---------------------------------------------------------------------------


def handler() -> type[CommonCrawlNewsSourceHandler]:
    """Entry-point factory returning the handler class."""
    return CommonCrawlNewsSourceHandler


__all__ = [
    "CommonCrawlNewsConfig",
    "CommonCrawlNewsSourceHandler",
    "S3Client",
    "handler",
]
