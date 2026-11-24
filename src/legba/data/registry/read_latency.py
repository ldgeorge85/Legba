# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-latency helpers for the routes the console's landing waits on (R-1).

Measured on live 2026-11-14 (one uvicorn worker, ~40 concurrent GETs on a
landing): the registry's event loop was busy with Python work, not waiting on
Postgres. Three tools live here, each small and each opt-in per route:

* :func:`fast_json_response` — serialise a pydantic model straight to JSON
  bytes in pydantic's Rust core. A route declared ``response_model=None`` that
  returns a model goes through FastAPI's ``jsonable_encoder`` — a redundant
  pure-Python walk of every nested value — before ``json.dumps``; on a 12.5 MB
  findings page that was 0.67 s of event-loop time against 0.23 s here, with
  the SAME bytes (pinned by ``tests/data_pkg/test_registry_read_latency.py``).
  Routes that declare a ``response_model`` already take FastAPI's fast path.
* :func:`page_json_response_off_loop` (P-5) — the same bytes again, built item
  by item on a worker thread, for a page whose Python work alone stalls the
  loop (a 22.8 MB findings page held it ~0.7-1.0 s; now ~0.14 s).
* :class:`TTLCache` — a short in-process cache with an explicit time to live
  and single flight (concurrent callers for one key share ONE computation).
  Only for answers that are the same for every caller and change slowly; the
  time the answer was computed travels with it so a route can show its age
  (:func:`stamp_as_of`). A failed computation is never cached.
* :class:`ConditionalGetMiddleware` — ``ETag`` + ``Cache-Control: private,
  max-age=<small>`` on successful GETs of an explicit path allowlist, and a
  ``304`` when the browser's ``If-None-Match`` matches. Repeated identical GETs
  within a few seconds then cost the browser nothing and the network nothing.

REGISTRY-SLIM: the stdlib, starlette and pydantic only (a deferred import of a
runtime module still fails on the slim image).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Generic, Hashable, Iterable, TypeVar

from pydantic import BaseModel
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

T = TypeVar("T")

#: Header naming when a cached answer was computed (ISO-8601 UTC). A route that
#: answers a list (no envelope to put an ``as_of`` field in) carries it here.
AS_OF_HEADER = "X-Legba-As-Of"


def fast_json_response(model: BaseModel, *, status_code: int = 200) -> Response:
    """``model`` as ``application/json`` bytes, BYTE-IDENTICAL to what FastAPI
    wrote for the same model on a ``response_model=None`` route.

    FastAPI's path there is ``jsonable_encoder(model)`` — which calls
    ``model_dump(mode="json")`` and then walks the result again in pure Python,
    to no effect on values that are already JSON primitives — and then
    ``JSONResponse.render`` (``json.dumps`` with ``ensure_ascii=False``,
    ``allow_nan=False``, compact separators). This keeps both real steps and
    drops the redundant walk: 0.23 s against 0.67 s on a live 12.5 MB findings
    page. (``model_dump_json`` is faster still, 0.10 s, but writes small floats
    differently — ``1e-05`` as ``0.00001`` — so it is not used: same value,
    different bytes.)
    """
    content = _dumps(model.model_dump(mode="json")).encode("utf-8")
    return Response(
        content=content, status_code=status_code, media_type="application/json"
    )


def _dumps(value: Any) -> str:
    """``JSONResponse.render``'s exact ``json.dumps`` settings."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    )


def page_json_bytes(model: BaseModel, *, list_field: str = "data") -> bytes:
    """``fast_json_response(model).body``, BYTE-IDENTICAL, built one list item
    at a time (P-5).

    Every call in :func:`fast_json_response` is one C or Rust call over the
    WHOLE page — ``model_dump`` and ``json.dumps`` each held the GIL for about
    0.35 s on a live 22.8 MB findings page, and nothing else in the process
    could run meanwhile, not even on another thread. Here each item of
    ``list_field`` is dumped and encoded on its own, so a page built on a
    worker thread (:func:`page_json_response_off_loop`) gives the event loop
    the GIL back between items. Same bytes: a compact ``json.dumps`` of a list
    is ``"[" + ",".join(item dumps) + "]"`` and of an object the same over
    ``"key":value`` pairs in declared field order (pinned against
    :func:`fast_json_response` by ``tests/data_pkg/test_registry_read_latency.py``).
    """
    parts: list[bytes] = []
    for name in type(model).model_fields:
        key = _dumps(name).encode("utf-8") + b":"
        if name == list_field:
            items = getattr(model, name)
            body = b",".join(
                _dumps(item.model_dump(mode="json")).encode("utf-8") for item in items
            )
            parts.append(key + b"[" + body + b"]")
        else:
            value = model.model_dump(mode="json", include={name})[name]
            parts.append(key + _dumps(value).encode("utf-8"))
    return b"{" + b",".join(parts) + b"}"


async def page_json_response_off_loop(
    build: Callable[[], BaseModel], *, list_field: str = "data",
) -> Response:
    """Run ``build()`` (row hydration) and :func:`page_json_bytes` on a worker
    thread and answer the bytes — for a page whose Python work is large enough
    to stall every other request on the loop (the full-weight findings list)."""
    content = await asyncio.to_thread(
        lambda: page_json_bytes(build(), list_field=list_field)
    )
    return Response(content=content, status_code=200, media_type="application/json")


# ---------------------------------------------------------------------------
# TTL cache with single flight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cached(Generic[T]):
    """A cached answer and the wall-clock instant it was computed."""

    value: T
    computed_at: datetime


class TTLCache(Generic[T]):
    """Keyed in-process cache: each entry lives ``ttl_s`` seconds.

    ``get_or_compute(key, factory)`` returns the live entry, or runs
    ``factory()`` ONCE for every concurrent caller of that key and caches its
    result. An exception from ``factory`` reaches every waiter and caches
    nothing. ``should_cache(value)`` (optional) lets a route keep a degraded
    answer — an honest "measured nothing" fallback — out of the cache.

    Per process: at ``LEGBA_REGISTRY_WORKERS = N`` each worker holds its own
    copy, which only means up to N computations per TTL, never a wrong answer.

    ``stale_s`` (default 0 = off) is STALE-WHILE-REVALIDATE: for ``stale_s``
    seconds after an entry's TTL runs out, a caller gets that entry at once
    and ONE background refresh is started (single flight, shared with any
    caller that arrives with nothing cached). The answer's ``computed_at``
    still says how old it is. Past ``ttl_s + stale_s`` the entry is gone and
    the next caller computes inline, as without ``stale_s``. A failed or
    degraded refresh leaves the last good entry in place until it ages out.
    """

    def __init__(
        self,
        ttl_s: float,
        *,
        stale_s: float = 0.0,
        max_entries: int = 64,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if stale_s < 0:
            raise ValueError("stale_s must not be negative")
        self.ttl_s = float(ttl_s)
        self.stale_s = float(stale_s)
        self._max = max_entries
        self._clock = clock
        self._wall = wall
        self._entries: dict[Hashable, tuple[float, Cached[T]]] = {}
        self._inflight: dict[Hashable, asyncio.Future[Cached[T]]] = {}
        #: Background refreshes, held so the loop cannot collect them mid-run.
        self._refreshing: set[asyncio.Task[Any]] = set()

    def clear(self) -> None:
        self._entries.clear()

    def peek(self, key: Hashable) -> Cached[T] | None:
        """The FRESH entry for ``key`` (inside its TTL), else None."""
        hit = self._entries.get(key)
        if hit is None:
            return None
        expires, cached = hit
        now = self._clock()
        if now >= expires:
            if now >= expires + self.stale_s:
                self._entries.pop(key, None)
            return None
        return cached

    def _stale(self, key: Hashable) -> Cached[T] | None:
        """An expired entry still inside its stale window, else None."""
        if self.stale_s <= 0:
            return None
        hit = self._entries.get(key)
        if hit is None:
            return None
        expires, cached = hit
        return cached if self._clock() < expires + self.stale_s else None

    async def get_or_compute(
        self,
        key: Hashable,
        factory: Callable[[], Awaitable[T]],
        *,
        should_cache: Callable[[T], bool] | None = None,
    ) -> Cached[T]:
        hit = self.peek(key)
        if hit is not None:
            return hit
        pending = self._inflight.get(key)
        stale = self._stale(key)
        if stale is not None:
            if pending is None:
                self.refresh_in_background(key, factory, should_cache=should_cache)
            return stale
        if pending is not None:
            return await asyncio.shield(pending)
        return await self._fill(key, self._begin(key), factory, should_cache)

    def refresh_in_background(
        self,
        key: Hashable,
        factory: Callable[[], Awaitable[T]],
        *,
        should_cache: Callable[[T], bool] | None = None,
    ) -> asyncio.Task[Any] | None:
        """Compute ``key`` on a background task (single flight: None when a
        computation for it is already running). Also the way to warm a key
        before anyone asks for it. A failure is swallowed here (it caches
        nothing, and any caller awaiting the same flight still receives it)."""
        if key in self._inflight:
            return None
        fut = self._begin(key)

        async def _run() -> None:
            try:
                await self._fill(key, fut, factory, should_cache)
            except Exception:  # noqa: BLE001 - never an unretrieved task error
                pass

        task = asyncio.get_running_loop().create_task(_run())
        self._refreshing.add(task)
        task.add_done_callback(self._refreshing.discard)
        return task

    def _begin(self, key: Hashable) -> asyncio.Future[Cached[T]]:
        """Register the single flight for ``key`` synchronously, so a second
        caller in the same loop turn joins it instead of starting another."""
        fut: asyncio.Future[Cached[T]] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        return fut

    async def _fill(
        self,
        key: Hashable,
        fut: asyncio.Future[Cached[T]],
        factory: Callable[[], Awaitable[T]],
        should_cache: Callable[[T], bool] | None,
    ) -> Cached[T]:
        try:
            computed_at = self._wall()
            value = await factory()
            cached = Cached(value=value, computed_at=computed_at)
            if should_cache is None or should_cache(value):
                if len(self._entries) >= self._max:
                    self._entries.pop(next(iter(self._entries)))
                self._entries[key] = (self._clock() + self.ttl_s, cached)
            fut.set_result(cached)
            return cached
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved: a lone caller must not warn
            raise
        finally:
            self._inflight.pop(key, None)


def stamp_as_of(response: Response, cached: Cached[Any]) -> None:
    """Put a cached answer's age on the response: ``X-Legba-As-Of`` (when it
    was computed) and ``Age`` (whole seconds since)."""
    response.headers[AS_OF_HEADER] = cached.computed_at.isoformat()
    age = (datetime.now(timezone.utc) - cached.computed_at).total_seconds()
    response.headers["Age"] = str(max(0, int(age)))


# ---------------------------------------------------------------------------
# ETag + Cache-Control on an allowlist of read routes
# ---------------------------------------------------------------------------

#: GET paths (exact, or a prefix ending in ``/``) whose 200 answers are the same
#: for every signed-in caller and may be reused by the browser for a few
#: seconds. Writes never pass through here (GET only), and nothing the console
#: writes and immediately re-reads is on the list.
DEFAULT_CACHEABLE_PATHS: tuple[str, ...] = (
    "/api/v1/findings",
    "/api/v1/signals",
    "/api/v1/situations",
    "/api/v1/registry/descriptors",
    "/api/v1/registry/sources",
    "/api/v1/registry/projection/",
    "/api/v1/v3/system/",
    "/api/v1/v3/eval/",
    "/api/v1/v3/absence",
    "/api/v1/v3/events",
    "/api/v1/v3/since",
)

DEFAULT_MAX_AGE_S = 5

#: Bodies larger than this are hashed on a worker thread.
_HASH_OFF_LOOP_BYTES = 256 * 1024


def _path_matches(path: str, allow: Iterable[str]) -> bool:
    for p in allow:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p:
            return True
    return False


def etag_for(body: bytes) -> str:
    """A weak validator over the exact body bytes."""
    return 'W/"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'


def _if_none_match(scope: Scope) -> list[str]:
    for k, v in scope.get("headers") or ():
        if k == b"if-none-match":
            return [t.strip() for t in v.decode("latin-1").split(",") if t.strip()]
    return []


class ConditionalGetMiddleware:
    """Pure ASGI: buffer an allowlisted GET's 200 JSON answer, tag it, and
    answer ``304`` (no body) when the caller already holds those bytes.

    The route still runs (the tag is over the body it produced), so this saves
    the transfer and, through ``max-age``, the browser's repeat requests; it
    never serves one caller's bytes to another. Non-200, non-JSON, streamed
    (more than one body message is still handled, by buffering) and
    non-allowlisted responses pass through untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: Iterable[str] = DEFAULT_CACHEABLE_PATHS,
        max_age_s: int = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.app = app
        self.paths = tuple(paths)
        self.cache_control = f"private, max-age={int(max_age_s)}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "GET"
            or not _path_matches(scope.get("path", ""), self.paths)
        ):
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def _send(message: Message) -> None:
            nonlocal start, passthrough
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers") or ())
                ctype = headers.get(b"content-type", b"")
                if message.get("status") != 200 or not ctype.startswith(b"application/json"):
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body" or passthrough or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            # hashlib releases the GIL: a multi-MB page is tagged on a worker
            # thread so the event loop keeps serving the landing's other asks.
            tag = (
                await asyncio.to_thread(etag_for, body)
                if len(body) > _HASH_OFF_LOOP_BYTES else etag_for(body)
            )
            headers = [
                (k, v) for (k, v) in (start.get("headers") or ())
                if k not in (b"etag", b"cache-control", b"content-length")
            ]
            headers += [(b"etag", tag.encode()), (b"cache-control", self.cache_control),
                        (b"vary", b"Authorization, Cookie")]
            inm = _if_none_match(scope)
            if tag in inm or "*" in inm:
                await send({"type": "http.response.start", "status": 304, "headers": headers})
                await send({"type": "http.response.body", "body": b""})
                return
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, _send)


__all__ = [
    "AS_OF_HEADER",
    "Cached",
    "ConditionalGetMiddleware",
    "DEFAULT_CACHEABLE_PATHS",
    "DEFAULT_MAX_AGE_S",
    "TTLCache",
    "etag_for",
    "fast_json_response",
    "page_json_bytes",
    "page_json_response_off_loop",
    "stamp_as_of",
]
