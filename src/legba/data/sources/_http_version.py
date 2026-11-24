# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-source HTTP version for source fetches — ``fetch.http_version`` (N-L).

Measured: a public official site's CDN firewall answers the platform's default
client 403 over HTTP/2 and 200 over HTTP/1.1 with the SAME client string — the
block keys on the HTTP/2 fingerprint (the evidence archiver already runs
``http2=False`` for this reason; ``_fetch_policy._HOST_POLICIES`` holds the
hard-coded hosts). This makes the protocol a per-source, declared choice:

* ``h2`` — the platform's normal client (HTTP/2 offered over ALPN); never
  falls back.
* ``http1.1`` — never offers HTTP/2.
* ``auto`` (default) — the normal client first; ONCE per request, retry over
  HTTP/1.1 only when the failure looks protocol-level: a 403/503 that carries
  a CDN/WAF challenge or block marker (``cf-mitigated: challenge``, or a body
  tell from the archiver's ``_challenge_detect.detect_challenge_page``), or an
  HTTP/2 protocol/stream error. A plain 401/403/404/429 from the origin does
  NOT fall back — that is a real refusal. A fallback that succeeds is
  remembered for the host for ``REMEMBER`` (7 days) so later polls go straight
  to HTTP/1.1; after that HTTP/2 is tried again.

Only the PROTOCOL changes: same User-Agent, same headers, same TLS context
(the HTTP/1.1 transport is built from the guarded transport's own arguments).
No global kill switch is touched. A host in ``_HOST_POLICIES`` keeps its own
policy for the archiver/web_fetch paths; for a SOURCE poll the descriptor's
setting is what applies.

Mechanics: the source actor opens a :class:`ProtocolPosture` scope around the
handler's steps (the same scope as the fetch-capture tee); the guarded
transport consults it. Outside a poll there is no posture and the transport is
exactly what it was.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterator

import httpx

__all__ = [
    "HTTP_VERSIONS",
    "ProtocolPosture",
    "REMEMBER",
    "current_posture",
    "looks_like_protocol_block",
    "posture_scope",
]

HTTP_VERSIONS = ("auto", "h2", "http1.1")
REMEMBER = timedelta(days=7)
H1, H2 = "http/1.1", "h2"

_POSTURE: contextvars.ContextVar["ProtocolPosture | None"] = contextvars.ContextVar(
    "legba_fetch_protocol_posture", default=None,
)

#: Exceptions that mean "the HTTP/2 layer broke", not "the origin refused".
_H2_ERRORS: tuple[type[BaseException], ...] = (httpx.RemoteProtocolError, httpx.LocalProtocolError)


def looks_like_protocol_block(response: httpx.Response, body: bytes | None) -> str | None:
    """The CDN/WAF block marker on a 403/503, else ``None`` (a real refusal)."""
    if response.status_code not in (403, 503):
        return None
    if response.headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return "header:cf-mitigated"
    from ..analysts.deterministic_handlers._challenge_detect import (  # noqa: PLC0415 — rare path
        detect_challenge_page,
    )

    return detect_challenge_page(body, response.headers.get("content-type"))


@dataclass
class ProtocolPosture:
    """One poll's protocol choice and what happened under it."""

    mode: str = "auto"
    memory: dict[str, str] = field(default_factory=dict)    # host -> remember-until ISO
    used: dict[str, int] = field(default_factory=dict)
    fallbacks: int = 0
    fallback_markers: list[str] = field(default_factory=list)

    def _note(self, proto: str) -> None:
        self.used[proto] = self.used.get(proto, 0) + 1

    def remembered_h1(self, host: str, now: datetime) -> bool:
        until = self.memory.get(host)
        if not until:
            return False
        try:
            return datetime.fromisoformat(until) > now
        except ValueError:
            return False

    def eventful(self) -> bool:
        """Anything worth a poll-outcome note (default h2 polls stay silent)."""
        return bool(self.fallbacks or self.used.get(H1) or self.mode != "auto")

    def outcome(self) -> dict[str, Any]:
        return {"http_version": self.mode, "used": dict(self.used), "fallbacks": self.fallbacks,
                **({"markers": self.fallback_markers[:5]} if self.fallback_markers else {})}

    async def send(
        self, request: httpx.Request, *,
        h2: Callable[[httpx.Request], Awaitable[httpx.Response]],
        h1: Callable[[httpx.Request], Awaitable[httpx.Response]],
        h2_offered: bool = True,
    ) -> httpx.Response:
        host = (request.url.host or "").lower()
        now = datetime.now(tz=timezone.utc)
        if self.mode == "http1.1" or (self.mode == "auto" and self.remembered_h1(host, now)):
            self._note(H1)
            return await h1(request)
        if self.mode == "h2" or not h2_offered:
            self._note(H2)
            return await h2(request)
        # auto: the normal client first, one HTTP/1.1 retry on a protocol block.
        try:
            resp = await h2(request)
        except _H2_ERRORS as exc:
            return await self._fallback(request, h1, host, now, f"h2_error:{type(exc).__name__}")
        if resp.status_code in (403, 503):
            body = await resp.aread()
            marker = looks_like_protocol_block(resp, body)
            if marker is not None:
                await resp.aclose()
                return await self._fallback(request, h1, host, now, marker)
        self._note(H2)
        return resp

    async def _fallback(self, request: httpx.Request, h1: Callable, host: str, now: datetime,
                        marker: str) -> httpx.Response:
        self.fallbacks += 1
        self.fallback_markers.append(marker)
        self._note(H1)
        resp = await h1(request)
        if 200 <= resp.status_code < 400:
            self.memory[host] = (now + REMEMBER).isoformat()
        return resp


def current_posture() -> ProtocolPosture | None:
    return _POSTURE.get()


@contextlib.contextmanager
def posture_scope(posture: ProtocolPosture | None) -> Iterator[None]:
    token = _POSTURE.set(posture)
    try:
        yield
    finally:
        _POSTURE.reset(token)
