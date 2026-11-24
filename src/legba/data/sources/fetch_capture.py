# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch capture — the bytes a poll ACTUALLY received, taken off the wire.

The operator's rule: a signal (or an observation) must rest on what the poll
received, not on a later re-fetch. Every HTTP source handler fetches through
:func:`legba.data.sources._egress.guarded_async_client`, whose transport is
:class:`~legba.data.sources._egress.SsrfGuardedTransport`; that transport calls
:func:`tee_response` on every response. With no capture scope active (the
archiver, web_fetch, any non-poll caller, or ``LEGBA_FETCH_RECEIPTS=0``) it
returns the response object untouched — the default path has no wrapper.

Inside a scope (the source actor opens one around each ``handler.pull`` step)
the response's byte stream is wrapped in a tee. The handler reads the body
exactly as before; when the stream closes the tee decodes the bytes it saw
(Content-Encoding only, by httpx's own decoders — the entity body the parser
consumed) and appends one :class:`FetchCapture` to the scope's log. Nothing
is written here: the actor persists captures through the receipt writer.

What is hashed: the sha256 of the DECODED ENTITY BODY — the bytes the handler
parsed. The wire encoding (gzip/br) depends on Accept-Encoding negotiation and
is recorded beside it (``content_encoding``), not hashed.

Not captured (recorded so, never guessed): redirect hops (3xx with a
Location — the final response is captured), a body the handler abandoned
before the end (``complete=False``, no bytes kept), and anything above
:data:`MAX_CAPTURE_BYTES` (hash of the wire bytes only, no bytes kept).

``role``: a handler that fetches an ARTICLE PAGE for content at ingest (as
opposed to the feed/API response) marks it with :func:`fetch_role` ``("page")``
so the page is linked to the signal as its ingest-time copy.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterator

import httpx

__all__ = [
    "CaptureLog",
    "FETCH_RECEIPTS_ENV",
    "FetchCapture",
    "MAX_CAPTURE_BYTES",
    "ReceiptTeeTransport",
    "capture_received",
    "capture_scope",
    "fetch_receipts_enabled",
    "fetch_role",
    "redact_secrets",
    "redacted",
    "tee_response",
]

#: Knob: fetch receipts for every poll, ON by default (operator decision).
FETCH_RECEIPTS_ENV = "LEGBA_FETCH_RECEIPTS"
#: Bodies above this keep only a hash of the wire bytes (no copy).
MAX_CAPTURE_BYTES = int(os.environ.get("LEGBA_FETCH_RECEIPT_MAX_BYTES", str(64 * 1024 * 1024)))

_ACTIVE: contextvars.ContextVar["CaptureLog | None"] = contextvars.ContextVar(
    "legba_fetch_capture_log", default=None,
)
_ROLE: contextvars.ContextVar[str] = contextvars.ContextVar("legba_fetch_role", default="response")
_SECRETS: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("legba_fetch_secrets", default=())
_REDACTED = "[redacted]"


def fetch_receipts_enabled() -> bool:
    return os.environ.get(FETCH_RECEIPTS_ENV, "1").strip().lower() not in {"0", "off", "false", "no"}


@dataclass
class FetchCapture:
    url: str
    method: str
    status: int
    content_type: str | None
    content_encoding: str | None
    role: str
    fetched_at: datetime
    complete: bool
    body: bytes | None
    sha256: str | None
    size: int
    hash_basis: str = "entity"     # entity | wire (too large) | none (incomplete)
    #: I-4: the receipt address when it is not the URL's canonical form — a
    #: SUBSCRIBED source's event is kept at ``<stream url>#event=<id>``
    #: (``runtime/stream_ingest.receipt_address``). None = canonical_url(url).
    address: str | None = None


@dataclass
class CaptureLog:
    """Captures in completion order, plus the yield bookkeeping the actor
    uses to link each record to the response it was parsed from."""

    captures: list[FetchCapture] = field(default_factory=list)
    _linked_upto: int = 0
    #: B-1: redirect hops seen in this scope, ``requested URL -> Location``.
    #: A capture records the FINAL response's URL; a handler that names the
    #: URL it asked for (gdelt_files' ``export_file_url`` is ``http://`` and
    #: the host answers 301 to ``https://``) is matched through these hops.
    redirects: dict[str, str] = field(default_factory=dict)

    def add(self, cap: FetchCapture) -> None:
        self.captures.append(cap)

    def note_redirect(self, from_url: str, to_url: str) -> None:
        if from_url and to_url and from_url != to_url:
            self.redirects[from_url] = to_url

    def resolve(self, url: str) -> str:
        """``url`` followed through the redirect hops this scope saw (bounded)."""
        seen = {url}
        while url in self.redirects and len(seen) <= 20:
            url = self.redirects[url]
            if url in seen:
                break
            seen.add(url)
        return url

    def latest_response(self, *, url: str | None = None) -> int | None:
        """Index of the newest complete 2xx ``response``-role capture (for
        ``url`` when given — the URL as asked for, or where its redirects
        led), or ``None``."""
        final = None if url is None else self.resolve(url)
        for i in range(len(self.captures) - 1, -1, -1):
            c = self.captures[i]
            if c.role != "response" or not c.complete or not (200 <= c.status < 300):
                continue
            if url is None or c.url == url or c.url == final:
                return i
        return None

    def pages_since_last_yield(self) -> list[int]:
        """``page``-role captures completed since the previous record."""
        out = [i for i in range(self._linked_upto, len(self.captures))
               if self.captures[i].role == "page" and self.captures[i].complete]
        self._linked_upto = len(self.captures)
        return out


@contextlib.contextmanager
def capture_scope(log: CaptureLog | None) -> Iterator[None]:
    """Make ``log`` the active capture log for the enclosed awaits."""
    token = _ACTIVE.set(log)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


@contextlib.contextmanager
def fetch_role(role: str) -> Iterator[None]:
    """Mark the enclosed fetches as ``role`` (``page`` for an article page)."""
    token = _ROLE.set(role)
    try:
        yield
    finally:
        _ROLE.reset(token)


@contextlib.contextmanager
def redact_secrets(*values: str) -> Iterator[None]:
    """Inside this scope a fetch's recorded URL never carries ``values`` (S-1).

    A keyed API that takes its key as a QUERY PARAMETER (EIA's open data API
    accepts it nowhere else) would otherwise write the key into the receipt's
    ``url`` / ``address`` and into the ``httpx`` request log line. The handler
    that resolved the secret wraps its request in this scope; the tee records
    the URL with each value (raw and URL-encoded) replaced by ``[redacted]``,
    and :class:`_RedactingLogFilter` does the same to the ``httpx`` log record.
    Values shorter than 4 characters are ignored (they would redact noise)."""
    from urllib.parse import quote  # noqa: PLC0415

    extra: list[str] = []
    for v in values:
        if isinstance(v, str) and len(v) >= 4:
            extra += [v, quote(v, safe="")]
    token = _SECRETS.set(_SECRETS.get() + tuple(dict.fromkeys(extra)))
    try:
        yield
    finally:
        _SECRETS.reset(token)


def redacted(text: str) -> str:
    """``text`` with every secret of the active :func:`redact_secrets` scope removed."""
    for v in _SECRETS.get():
        if v in text:
            text = text.replace(v, _REDACTED)
    return text


class _RedactingLogFilter(logging.Filter):
    """Scrub the active scope's secrets from ``httpx``'s request log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _SECRETS.get():
            msg = record.getMessage()
            clean = redacted(msg)
            if clean != msg:
                record.msg, record.args = clean, ()
        return True


if not any(isinstance(f, _RedactingLogFilter) for f in logging.getLogger("httpx").filters):
    logging.getLogger("httpx").addFilter(_RedactingLogFilter())


class _TeeStream(httpx.AsyncByteStream):
    def __init__(self, inner: Any, request: httpx.Request, response: httpx.Response,
                 log: CaptureLog, role: str) -> None:
        self._inner = inner
        self._request = request
        self._response = response
        self._log = log
        self._role = role
        self._chunks: list[bytes] = []
        self._size = 0
        self._wire = hashlib.sha256()
        self._overflow = False
        self._ended = False
        self._done = False
        self._url = redacted(str(request.url))   # taken now, inside the caller's scope

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._size += len(chunk)
            self._wire.update(chunk)
            if not self._overflow:
                if self._size > MAX_CAPTURE_BYTES:
                    self._overflow = True
                    self._chunks = []
                else:
                    self._chunks.append(chunk)
            yield chunk
        self._ended = True

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._finish()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        headers = self._response.headers
        body: bytes | None = None
        sha: str | None = None
        basis = "none"
        if self._ended and not self._overflow:
            raw = b"".join(self._chunks)
            try:
                body = httpx.Response(self._response.status_code, headers=headers, content=raw).content
            except Exception:  # undecodable encoding: keep the wire bytes, say so
                body, basis = raw, "wire"
            else:
                basis = "entity"
            sha = hashlib.sha256(body).hexdigest()
        elif self._ended:
            sha, basis = self._wire.hexdigest(), "wire"
        self._log.add(FetchCapture(
            url=self._url, method=self._request.method,
            status=self._response.status_code,
            content_type=headers.get("content-type"),
            content_encoding=headers.get("content-encoding"),
            role=self._role, fetched_at=datetime.now(tz=timezone.utc),
            complete=self._ended, body=body if basis != "none" else None,
            sha256=sha, size=len(body) if body is not None else self._size,
            hash_basis=basis,
        ))


def tee_response(request: httpx.Request, response: httpx.Response) -> httpx.Response:
    """Wrap ``response``'s stream in a capture tee when a scope is active.

    Identity (the same object, untouched) when no scope is active or the
    response is a redirect hop the client will follow."""
    log = _ACTIVE.get()
    if log is None:
        return response
    if 300 <= response.status_code < 400 and "location" in response.headers:
        try:   # B-1: remember the hop so a record naming the asked-for URL links
            log.note_redirect(redacted(str(request.url)),
                              redacted(str(request.url.join(response.headers["location"]))))
        except Exception:  # a malformed Location: the client will fail on it, not us
            pass
        return response
    if hasattr(response, "_content"):
        # A transport that handed back an already-read body (an in-memory
        # transport; never a network one): the decoded body is final now.
        body = response.content
        log.add(FetchCapture(
            url=redacted(str(request.url)), method=request.method, status=response.status_code,
            content_type=response.headers.get("content-type"),
            content_encoding=response.headers.get("content-encoding"),
            role=_ROLE.get(), fetched_at=datetime.now(tz=timezone.utc), complete=True,
            body=body if len(body) <= MAX_CAPTURE_BYTES else None,
            sha256=hashlib.sha256(body).hexdigest(), size=len(body),
        ))
        return response
    response.stream = _TeeStream(response.stream, request, response, log, _ROLE.get())
    return response


def capture_received(
    *, url: str, body: bytes, content_type: str | None, address: str | None = None,
    role: str = "response",
) -> bool:
    """Record what a NON-HTTP transport received (B-1: Telegram's MTProto) as
    one complete capture in the active scope — the same capture an HTTP
    response gets from the tee. ``False`` (nothing recorded) outside a scope
    or with receipts off. Never raises."""
    log = _ACTIVE.get()
    if log is None:
        return False
    try:
        log.add(FetchCapture(
            url=redacted(url), method="RECV", status=200, content_type=content_type,
            content_encoding=None, role=role, fetched_at=datetime.now(tz=timezone.utc),
            complete=True, body=body if len(body) <= MAX_CAPTURE_BYTES else None,
            sha256=hashlib.sha256(body).hexdigest(), size=len(body),
            hash_basis="entity" if len(body) <= MAX_CAPTURE_BYTES else "wire",
            address=address,
        ))
    except Exception:  # a receipt must never cost a record
        return False
    return True


class ReceiptTeeTransport(httpx.AsyncBaseTransport):
    """Wrap any transport with the capture tee — for clients that mount their
    own transport (tests' MockTransport, an impersonating client)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return tee_response(request, await self._inner.handle_async_request(request))

    async def aclose(self) -> None:
        await self._inner.aclose()
