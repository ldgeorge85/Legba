# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Browser-FINGERPRINT impersonation for the platform fetcher (FETCH_REVIEW (a)).

**What this is.** An ``httpx``-shaped adapter over
``curl_cffi.requests.AsyncSession`` that presents a real Chrome TLS/JA3 +
HTTP/2 + header-order fingerprint. The FETCH_REVIEW probes (12 GETs, 2026-09-16)
showed the four named blockers return the SAME status to our bot UA and to a
desktop Chrome UA — AP 403/403, Times of Israel 403/403, Reuters 401/401,
Haaretz 200/200. **This is not a User-Agent problem.** A UA change fixes
nothing; what AP and ToI refuse is a non-browser TLS stack.

**Fingerprint only — the UA stays ours.** The caller's ``headers=`` is applied
AFTER curl_cffi's impersonation profile, so ``legba-research/1.0`` (or
``legba-evidence-archiver/0.1``, or ``legba-web-tools/1.0``) is what the
publisher's log records, exactly as before. Sending a Chrome UA string would
make the robots.txt decision — which ``robots.py`` computes for OUR token —
apply to a different agent than the one that showed up, and that is
misrepresentation rather than compatibility (FETCH_REVIEW §3(a), §3(e) "no
split identity"). A WAF that cross-checks UA against TLS may refuse us anyway.
That is the publisher's answer and we take it.

**The guard does not travel, so it is re-expressed here.**
``SsrfGuardedTransport`` is an *httpx transport*; ``curl_cffi`` cannot accept
one. So this module calls :func:`~legba.data.sources._egress.assert_public_host`
(imported, NEVER reimplemented) before the request, sets
``allow_redirects=False``, and hand-walks each hop re-asserting the resolved
``Location`` — the same property httpx gave us for free by re-invoking the
transport per hop. Refusals raise the EXISTING
:class:`~legba.data.sources._egress.EgressBlockedError`, so every
``except EgressBlockedError`` already written at the call sites keeps catching.

**Errors are translated into the httpx vocabulary** (``HTTPStatusError``,
``ConnectError``, ``TooManyRedirects``) because the call sites catch
``httpx.HTTPError``. A curl_cffi-native exception escaping this module would
turn a handled network failure into an unhandled crash.

**Not reached unless an operator turns it on.** ``LEGBA_FETCH_IMPERSONATE``
defaults to ``off`` and the ``off`` branch of
:func:`~legba.data.sources._egress.fetch_client` never imports this module —
so a runtime image without ``curl_cffi`` is unaffected by its presence, and an
image without it FAILS LOUD (naming the dep and the flag) the moment the flag
is turned on. There is no silent degrade-to-plain path.

**Scope.** Nothing here touches robots.txt (``robots.py`` keeps its own plain
guarded client — robots.txt is served to everyone), the licence gate
(``research_evidence.depth_for_license`` is the operator's), or the never-fetch
rule for hosts whose robots.txt disallows us.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlsplit

import httpx

from ._egress import (
    FETCH_IMPERSONATE_ENV,
    EgressBlockedError,
    assert_public_host,
)

logger = logging.getLogger(__name__)

#: The env flag — defined in ``_egress`` (re-exported here for callers that
#: only import this module). ``off`` (default) / ``on`` / ``fallback``.
MODE_OFF = "off"
MODE_ON = "on"
MODE_FALLBACK = "fallback"
VALID_MODES = frozenset({MODE_OFF, MODE_ON, MODE_FALLBACK})

#: curl_cffi's ROLLING latest-Chrome alias, not a pinned ``chrome131``: the
#: profile then ages with the library instead of with this line. Pin a specific
#: build only if a named target demands it (FETCH_REVIEW §5).
IMPERSONATE_PROFILE = "chrome"

#: Hop ceiling for the hand-walked redirect loop. httpx's own default is 20;
#: 5 is the review's number and is ample for an article URL.
MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Statuses that make the ``fallback`` mode re-issue through impersonation.
#: Status-only on purpose: a streaming first attempt has no body to inspect
#: without buying a second read, and the two hosts this exists for (AP, ToI)
#: both answer 403.
FALLBACK_RETRY_STATUSES = frozenset({401, 403, 429})


def impersonate_mode() -> str:
    """The configured mode. Unrecognised values WARN and fall back to ``off``.

    A typo must never move a fetch posture silently in either direction — the
    same discipline ``evidence_archiver``'s licence gates already use.
    """
    raw = str(os.environ.get(FETCH_IMPERSONATE_ENV, "") or "").strip().lower()
    if not raw:
        return MODE_OFF
    if raw in VALID_MODES:
        return raw
    if raw in ("1", "true", "yes"):
        return MODE_ON
    if raw in ("0", "false", "no"):
        return MODE_OFF
    logger.warning(
        "egress.impersonate.bad_value value=%r — keeping the default (%s). "
        "Valid: %s",
        raw, MODE_OFF, sorted(VALID_MODES),
    )
    return MODE_OFF


def _require_curl_cffi() -> Any:
    """The ``curl_cffi.requests`` module, or a LOUD failure naming the gap.

    No stub, no silent degrade to the plain client: an operator who turned the
    flag on and got plain httpx anyway would be measuring the wrong thing.
    """
    try:
        from curl_cffi import requests as curl_requests  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised by image builds
        raise RuntimeError(
            f"{FETCH_IMPERSONATE_ENV} is set but curl_cffi is not installed in "
            "this image. Add 'curl_cffi>=0.16' (it is declared in "
            "pyproject.toml and docker/Dockerfile.runtime) and rebuild, or "
            f"unset {FETCH_IMPERSONATE_ENV}."
        ) from exc
    return curl_requests


def _curl_request_errors() -> tuple[type[BaseException], ...]:
    """curl_cffi's request-error base, across its 0.15/0.16 module rename."""
    errs: list[type[BaseException]] = []
    try:
        from curl_cffi.requests.exceptions import (  # noqa: PLC0415
            RequestException,
        )

        errs.append(RequestException)
    except ImportError:
        pass
    try:
        from curl_cffi.requests.errors import RequestsError  # noqa: PLC0415

        errs.append(RequestsError)
    except ImportError:
        pass
    try:
        from curl_cffi import CurlError  # noqa: PLC0415

        errs.append(CurlError)
    except ImportError:
        pass
    return tuple(errs) or (Exception,)


def _assert_hop(url: str) -> None:
    """Scheme check + SSRF guard for ONE hop. The guard, not a copy of it."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressBlockedError(f"egress blocked: unsupported scheme {parts.scheme!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    assert_public_host(parts.hostname or "", port)


class ImpersonatedResponse:
    """The httpx-shaped slice of a curl_cffi response the callers consume.

    Exactly the surface FETCH_REVIEW §5 enumerates — ``status_code``,
    ``headers``, ``url``, ``text``, ``charset_encoding``, ``raise_for_status()``
    and ``aiter_bytes()`` — and nothing more. A wider adapter would be a
    promise we have not tested.
    """

    __slots__ = ("_resp", "_url")

    def __init__(self, resp: Any, url: str) -> None:
        self._resp = resp
        self._url = url

    @property
    def status_code(self) -> int:
        return int(self._resp.status_code)

    @property
    def headers(self) -> Any:
        return self._resp.headers

    @property
    def url(self) -> str:
        """The FINAL url after the hand-walked hops (a ``str``; callers all
        wrap this in ``str(...)`` already, so a str is a drop-in)."""
        return self._url

    @property
    def text(self) -> str:
        return self._resp.text

    @property
    def content(self) -> bytes:
        return self._resp.content

    @property
    def charset_encoding(self) -> str | None:
        """httpx's name for "the charset the HEADER declared, or None"."""
        return getattr(self._resp, "charset_encoding", None)

    def raise_for_status(self) -> None:
        """Raise ``httpx.HTTPStatusError`` on 4xx/5xx — the httpx vocabulary,
        because that is what every call site catches."""
        code = self.status_code
        if code < 400:
            return
        request = httpx.Request("GET", self._url)
        response = httpx.Response(code, request=request)
        raise httpx.HTTPStatusError(
            f"Server error '{code}' for url '{self._url}'",
            request=request,
            response=response,
        )

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        """Stream the body. Named for httpx; curl_cffi calls it
        ``aiter_content``.

        A mid-stream libcurl failure is translated to ``httpx.ReadError`` for
        the same reason the rest of this module translates: the call sites
        catch ``httpx.HTTPError``, and a curl-native exception escaping here
        would turn a handled network failure into an unhandled crash.
        """
        try:
            async for chunk in self._resp.aiter_content():
                yield chunk
        except _curl_request_errors() as exc:
            raise httpx.ReadError(f"impersonated stream failed: {exc}") from exc


class ImpersonatingAsyncClient:
    """An ``httpx.AsyncClient``-shaped client with a Chrome fingerprint.

    Opened by the same ``async with fetch_client(...) as client:`` the call
    sites already write. Every request — and every redirect hop — passes
    :func:`_assert_hop` first.
    """

    def __init__(
        self,
        *,
        mode: str = MODE_ON,
        guarded: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
        transport: Any = None,  # accepted + ignored: httpx-only kwarg
        **_ignored: Any,
    ) -> None:
        self._mode = mode
        self._guarded = guarded
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._follow_redirects = bool(follow_redirects)
        self._session: Any = None
        self._plain: httpx.AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "ImpersonatingAsyncClient":
        curl_requests = _require_curl_cffi()
        self._session = curl_requests.AsyncSession()
        if self._mode == MODE_FALLBACK:
            guarded = self._guarded
            if guarded is None:
                # Lazy import: _egress imports THIS module, never the reverse.
                from ._egress import guarded_async_client  # noqa: PLC0415

                guarded = guarded_async_client
            self._plain = guarded(
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=self._follow_redirects,
            )
            await self._plain.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._plain is not None:
            await self._plain.__aexit__(None, None, None)
            self._plain = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- the hop walk ------------------------------------------------------

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            # curl_cffi applies the profile's headers FIRST; ours win. This is
            # the line that keeps the identifying UA (module docstring).
            "headers": self._headers,
            "impersonate": IMPERSONATE_PROFILE,
            # The guard walks hops itself — curl must not follow any.
            "allow_redirects": False,
        }
        if self._timeout is not None:
            kwargs["timeout"] = self._timeout
        return kwargs

    @asynccontextmanager
    async def _open(self, method: str, url: str) -> AsyncIterator[Any]:
        """One curl_cffi streaming response for ONE hop, guard asserted."""
        _assert_hop(url)
        errors = _curl_request_errors()
        cm = self._session.stream(method, url, **self._request_kwargs())
        try:
            resp = await cm.__aenter__()
        except EgressBlockedError:
            # Re-raised explicitly because ``_curl_request_errors()`` degrades
            # to ``(Exception,)`` when curl_cffi's error module moves again —
            # an egress refusal must never be swallowed into a ConnectError.
            raise
        except errors as exc:
            raise httpx.ConnectError(f"impersonated fetch failed: {exc}") from exc
        try:
            yield resp
        finally:
            await cm.__aexit__(None, None, None)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **_: Any) -> AsyncIterator[Any]:
        """``async with client.stream("GET", url) as resp:`` — hops hand-walked.

        Each hop is guard-asserted BEFORE the connection, which is the property
        ``SsrfGuardedTransport`` provided by being re-invoked per redirect. A
        ``Location`` pointing at ``127.0.0.1`` / ``169.254.169.254`` / any
        private range raises :class:`EgressBlockedError` here.
        """
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            async with self._open(method, current) as resp:
                status = int(resp.status_code)
                location = resp.headers.get("location") if status in _REDIRECT_STATUSES else None
                if location and self._follow_redirects:
                    current = urljoin(current, location)
                    continue
                if (
                    self._mode == MODE_FALLBACK
                    and status in FALLBACK_RETRY_STATUSES
                ):
                    # Reached only via _FallbackClient's retry leg: the plain
                    # client was refused and so was the Chrome fingerprint.
                    # Worth a line — it is the publisher's final answer.
                    logger.info(
                        "egress.impersonate.fallback_still_blocked url=%s status=%s",
                        current, status,
                    )
                yield ImpersonatedResponse(resp, current)
                return
        raise httpx.TooManyRedirects(
            f"exceeded {MAX_REDIRECTS} redirects fetching {url}"
        )

    async def get(self, url: str, **_: Any) -> ImpersonatedResponse:
        """A buffered GET through the same hop walk."""
        async with self.stream("GET", url) as resp:
            await resp._resp.acontent()  # noqa: SLF001 — our own wrapper
            return resp


def impersonating_async_client(
    *, mode: str = MODE_ON, **kwargs: Any,
) -> ImpersonatingAsyncClient:  # noqa: D401
    """The ``guarded_async_client``-shaped constructor for the impersonated path.

    In ``fallback`` mode the plain guarded client is tried first and only a
    401/403/429 is re-issued with the Chrome fingerprint, so a host that
    already answers us is never touched by the new code path.
    """
    if mode == MODE_FALLBACK:
        return _FallbackClient(**kwargs)
    return ImpersonatingAsyncClient(mode=mode, **kwargs)


class _FallbackClient(ImpersonatingAsyncClient):
    """Plain guarded client first; impersonate ONLY a 401/403/429.

    The measurement bar in FETCH_REVIEW §5 is "≥1/3 of challenge-class URLs
    become ok, with ZERO regressions". This mode makes the regression half
    structurally true: a URL the plain client already fetches is fetched by
    the plain client, byte for byte.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(mode=MODE_FALLBACK, **kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **_: Any) -> AsyncIterator[Any]:
        assert self._plain is not None, "_FallbackClient used outside `async with`"
        async with self._plain.stream(method, url) as resp:
            if int(resp.status_code) not in FALLBACK_RETRY_STATUSES:
                yield resp
                return
            logger.info(
                "egress.impersonate.retrying url=%s plain_status=%s",
                url, resp.status_code,
            )
        async with ImpersonatingAsyncClient.stream(self, method, url) as resp:
            yield resp

    async def get(self, url: str, **_: Any) -> Any:
        async with self.stream("GET", url) as resp:
            reader = getattr(resp, "aread", None)
            if reader is not None:          # a real httpx.Response
                await reader()
            else:                           # our adapter
                await resp._resp.acontent()  # noqa: SLF001
            return resp


__all__ = [
    "FETCH_IMPERSONATE_ENV",
    "IMPERSONATE_PROFILE",
    "MAX_REDIRECTS",
    "MODE_FALLBACK",
    "MODE_OFF",
    "MODE_ON",
    "ImpersonatedResponse",
    "ImpersonatingAsyncClient",
    "impersonate_mode",
    "impersonating_async_client",
]
