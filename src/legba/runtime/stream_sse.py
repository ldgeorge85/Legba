# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``sse`` transport: Server-Sent Events over HTTP.

WHY SSE FIRST: it is plain HTTP, so it needs no new dependency (httpx is
already the fetch client), it goes through the SAME SSRF egress guard and
TLS posture as every poll (:func:`~legba.data.sources._egress.guarded_async_client`),
and it is the one candidate with RESUME in the protocol: the client sends the
last event id it processed as ``Last-Event-ID`` and a conforming server
replays from there. Public firehoses that publish this way include change
feeds and alert streams. A WebSocket transport needs a new client library and
has no standard resume; a NATS subject is an internal bus here (and a
streamless JetStream subject costs ~5 s per publish, swallowed) — both stay
declared and refused at save (SEAMS #78).

THE PARSER follows the WHATWG event-stream rules: lines end in CRLF, LF or
CR; a blank line dispatches the event; ``:`` starts a comment (a keep-alive);
``data`` lines join with ``\\n``; ``id`` sets the last-event-id buffer (it
persists across events, and an id containing NUL is ignored); ``event`` sets
the type (``message`` when absent); ``retry`` is the server's reconnect hint.
Each dispatched event keeps the exact bytes of its block (``raw``) for the
receipt. An event over ``max_event_bytes`` is marked ``oversize`` (its bytes
are kept up to the bound and nothing past it is buffered), so one runaway
event cannot exhaust memory.

STALL: the read timeout IS the idle timeout — no byte (event or keep-alive)
for ``idle_timeout_seconds`` raises :class:`StreamStalled`.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

import httpx

from ..data.sources.stream_event import StreamEvent
from .stream_transport import Activity, StreamStalled, TransportError

__all__ = ["SseParser", "SseTransport"]

_CONNECT_TIMEOUT_S = 15.0


class SseParser:
    """Incremental event-stream parser: :meth:`feed` bytes, get items."""

    def __init__(self, *, max_event_bytes: int, initial_last_id: str | None = None) -> None:
        self._max = int(max_event_bytes)
        self._buf = b""
        self._last_id: str | None = initial_last_id
        self._reset_event()
        self._skip_line = False   # discarding the rest of an over-long line

    def _reset_event(self) -> None:
        self._raw = bytearray()
        self._data: list[str] = []
        self._type = ""
        self._oversize = False
        self._has_field = False

    @property
    def last_id(self) -> str | None:
        return self._last_id

    def feed(self, chunk: bytes, *, now: datetime | None = None) -> list[Any]:
        out: list[Any] = []
        self._buf += chunk
        while True:
            idx = _line_end(self._buf)
            if idx is None:
                if len(self._buf) > self._max:   # one line with no end in sight
                    self._note_raw(self._buf)
                    self._oversize = self._has_field = True
                    self._buf = b""
                    self._skip_line = True
                break
            end, term_len = idx
            line, self._buf = self._buf[:end], self._buf[end + term_len:]
            raw_line = line + b"\n"
            if self._skip_line:
                self._skip_line = False
                continue
            item = self._line(line, raw_line, now=now)
            if item is not None:
                out.append(item)
        return out

    def _note_raw(self, b: bytes) -> None:
        room = self._max - len(self._raw)
        if room > 0:
            self._raw += b[:room]
        if len(b) > room:
            self._oversize = True

    def _line(self, line: bytes, raw_line: bytes, *, now: datetime | None) -> Any:
        if not line:
            return self._dispatch(now)
        text = line.decode("utf-8", "replace")
        if text.startswith(":"):
            if not self._has_field:
                return Activity("heartbeat")
            self._note_raw(raw_line)
            return None
        name, sep, value = text.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        self._note_raw(raw_line)
        self._has_field = True
        if name == "data":
            if not self._oversize:
                self._data.append(value)
        elif name == "event":
            self._type = value
        elif name == "id":
            if "\x00" not in value:
                self._last_id = value or None
        elif name == "retry":
            if value.isdigit():
                return Activity("retry", retry_ms=int(value))
        return None

    def _dispatch(self, now: datetime | None) -> Any:
        if not self._has_field:
            return None
        if not self._data and not self._oversize:
            # A block with no data dispatches nothing (spec) — but an id it
            # carried has already moved the resume point.
            self._reset_event()
            return None
        ev = StreamEvent(
            id=self._last_id, event=self._type or "message",
            data="\n".join(self._data), raw=bytes(self._raw),
            received_at=now or datetime.now(tz=timezone.utc), oversize=self._oversize,
        )
        self._reset_event()
        return ev


def _line_end(buf: bytes) -> tuple[int, int] | None:
    """(index, terminator length) of the first line end, or None. A CR at the
    very end of the buffer waits for the next chunk (it may be half a CRLF)."""
    lf = buf.find(b"\n")
    cr = buf.find(b"\r")
    if cr == -1 and lf == -1:
        return None
    if cr == -1 or (lf != -1 and lf < cr):
        return lf, 1
    if cr + 1 == len(buf):
        return None
    return (cr, 2) if buf[cr + 1:cr + 2] == b"\n" else (cr, 1)


class SseTransport:
    """One SSE connection per :meth:`open`."""

    name = "sse"
    supports_resume = True

    def __init__(self, subscribe: Any, *, headers: dict[str, str],
                 client_factory: Callable[..., httpx.AsyncClient] | None = None) -> None:
        self._sub = subscribe
        self._headers = dict(headers)
        self._client_factory = client_factory

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(_CONNECT_TIMEOUT_S, read=float(self._sub.idle_timeout_seconds))
        if self._client_factory is not None:
            return self._client_factory(timeout=timeout)
        from ..data.sources._egress import guarded_async_client  # noqa: PLC0415

        # HTTP/1.1: an event stream is one long response; h2 buys nothing.
        return guarded_async_client(timeout=timeout, follow_redirects=True, http2=False)

    @contextlib.asynccontextmanager
    async def open(self, *, resume_id: str | None) -> AsyncIterator[AsyncIterator[Any]]:
        headers = {**self._headers, "Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if resume_id and self._sub.resume == "last_event_id":
            headers["Last-Event-ID"] = resume_id
        client = self._client()
        try:
            try:
                cm = client.stream("GET", self._sub.url, headers=headers)
                resp = await cm.__aenter__()
            except httpx.TimeoutException as exc:
                raise TransportError(f"connect timed out: {type(exc).__name__}") from exc
            except httpx.TransportError as exc:
                blocked = type(exc).__name__ == "EgressBlockedError"
                raise TransportError(f"connect failed: {exc}", permanent=blocked) from exc
            try:
                _check_response(resp)
                yield self._items(resp, resume_id)
            finally:
                with contextlib.suppress(Exception):
                    await cm.__aexit__(None, None, None)
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _items(self, resp: httpx.Response, resume_id: str | None) -> AsyncIterator[Any]:
        parser = SseParser(max_event_bytes=self._sub.max_event_bytes, initial_last_id=resume_id)
        try:
            async for chunk in resp.aiter_bytes():   # Content-Encoding decoded, like a poll's entity
                for item in parser.feed(chunk):
                    yield item
        except httpx.ReadTimeout as exc:
            raise StreamStalled(f"no bytes for {self._sub.idle_timeout_seconds:g}s") from exc
        except (httpx.TransportError, httpx.StreamError) as exc:
            raise TransportError(f"connection dropped: {type(exc).__name__}: {exc}") from exc


def _check_response(resp: httpx.Response) -> None:
    status = resp.status_code
    if status != 200:
        retry_after = None
        raw = resp.headers.get("retry-after")
        if raw and raw.strip().isdigit():
            retry_after = float(raw.strip())
        # 204 = the server asks the client to stop reconnecting (spec);
        # a 4xx other than 408/429 will not heal on its own.
        permanent = status == 204 or (400 <= status < 500 and status not in (408, 429))
        raise TransportError(f"HTTP {status}", status=status, permanent=permanent,
                             retry_after=retry_after)
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "text/event-stream":
        raise TransportError(f"not an event stream (content-type {ctype or 'absent'!r})",
                             status=status, permanent=True)
