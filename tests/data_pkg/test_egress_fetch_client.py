# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FETCH_REVIEW (a) — ``fetch_client`` dispatch + the re-expressed SSRF guard.

Two properties, and they are the two that decide whether this lane was safe:

  1. **Flag OFF is the old code.** ``LEGBA_FETCH_IMPERSONATE`` unset ⇒
     ``fetch_client`` returns the caller's own ``guarded_async_client(**kwargs)``
     — same class, same transport, same kwargs — and the impersonation module
     is not consulted at all. The evidential half of that claim is that ZERO
     existing test files were edited for this change (the D-5 region-tier
     precedent); this file is the structural half.

  2. **The guard did not get lost in the port.** ``SsrfGuardedTransport`` is an
     *httpx transport* and cannot travel to ``curl_cffi``. The impersonated
     path re-expresses it — ``assert_public_host`` before the request,
     ``allow_redirects=False``, hand-walked hops re-asserting every
     ``Location``. The tests below drive that hop walk against a stub session
     so the refusals are proven without a network, and prove the refusal is
     still the EXISTING ``EgressBlockedError`` every call site already catches.

Nothing here touches robots.txt (``robots.py`` keeps its own plain client on
purpose) or ``depth_for_license`` (the licence gate is the operator's).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from legba.data.sources import _egress_impersonate as imp
from legba.data.sources._egress import (
    FETCH_IMPERSONATE_ENV,
    EgressBlockedError,
    SsrfGuardedTransport,
    fetch_client,
    guarded_async_client,
)

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# A stub curl_cffi session. The hop walk is OURS; curl is not under test.
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code: int, headers: dict | None = None,
                 body: bytes = b"") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.charset_encoding = "utf-8"
        self.url = ""

    async def acontent(self) -> bytes:
        return self.content

    async def aiter_content(self):
        yield self.content


class _StubSession:
    """Serves a canned response per URL and records what was requested."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.seen: list[str] = []

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.seen.append(url)
        resp = self.routes.get(url)
        if resp is None:
            resp = _StubResponse(200, {"content-type": "text/html"}, b"<html/>")
        yield resp

    async def close(self) -> None:
        return None


def _client(routes: dict, **kw) -> imp.ImpersonatingAsyncClient:
    """An impersonating client with a STUB session — no curl_cffi, no network."""
    client = imp.ImpersonatingAsyncClient(
        mode=imp.MODE_ON, headers={"User-Agent": "legba-research/1.0"},
        timeout=5.0, follow_redirects=True, **kw,
    )
    client._session = _StubSession(routes)  # noqa: SLF001 — the seam under test
    return client


# ---------------------------------------------------------------------------
# 1. Flag OFF — byte-identity
# ---------------------------------------------------------------------------


async def test_flag_unset_yields_the_same_guarded_client(monkeypatch):
    """The DEFAULT deployment gets ``httpx.AsyncClient`` + the guarded
    transport, exactly as ``guarded_async_client`` has always returned."""
    monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)
    kwargs = dict(
        follow_redirects=True, timeout=20.0,
        headers={"User-Agent": "legba-research/1.0"},
    )
    client = fetch_client(**kwargs)
    reference = guarded_async_client(**kwargs)
    try:
        assert type(client) is type(reference) is httpx.AsyncClient
        assert isinstance(client._transport, SsrfGuardedTransport)
        assert client.follow_redirects == reference.follow_redirects is True
        assert client.timeout == reference.timeout
        assert client.headers["user-agent"] == reference.headers["user-agent"]
        # …and the UA is OURS, not a browser's. The whole licence posture of
        # this lane rests on that one string staying put.
        assert client.headers["user-agent"] == "legba-research/1.0"
    finally:
        await client.aclose()
        await reference.aclose()


async def test_flag_off_calls_the_call_site_s_own_guarded_factory(monkeypatch):
    """``guarded=`` is the monkeypatch seam four existing e2e suites use.

    If ``fetch_client`` ignored it and reached for ``_egress``'s own name,
    every ``monkeypatch.setattr(research_tools, "guarded_async_client", …)``
    in the tree would silently stop intercepting the page fetch and those
    suites would start hitting the network. This asserts the pass-through.
    """
    monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)
    seen: list[dict] = []
    sentinel = object()

    def factory(**kwargs):
        seen.append(kwargs)
        return sentinel

    got = fetch_client(guarded=factory, timeout=7.0, follow_redirects=True)
    assert got is sentinel
    assert seen == [{"timeout": 7.0, "follow_redirects": True}]


async def test_flag_off_never_consults_the_impersonation_module(monkeypatch):
    """The off branch must not even IMPORT the impersonated path.

    Sabotaging ``impersonate_mode`` proves it: if the off branch called into
    the module, this would raise instead of returning a client.
    """
    monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)

    def boom():  # pragma: no cover - the point is that it is never called
        raise AssertionError("the off path consulted the impersonation module")

    monkeypatch.setattr(imp, "impersonate_mode", boom)
    client = fetch_client(timeout=1.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "", "  "])
async def test_explicitly_off_values_yield_the_guarded_client(monkeypatch, value):
    monkeypatch.setenv(FETCH_IMPERSONATE_ENV, value)
    client = fetch_client(timeout=1.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


async def test_a_typo_keeps_the_default_instead_of_moving_the_posture(monkeypatch):
    """A typo must not move a fetch posture silently in EITHER direction."""
    monkeypatch.setenv(FETCH_IMPERSONATE_ENV, "impersonate-please")
    client = fetch_client(timeout=1.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert isinstance(client._transport, SsrfGuardedTransport)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("on", imp.ImpersonatingAsyncClient),
        ("1", imp.ImpersonatingAsyncClient),
        ("fallback", imp._FallbackClient),
    ],
)
async def test_flag_on_selects_the_impersonating_client(monkeypatch, value, expected):
    monkeypatch.setenv(FETCH_IMPERSONATE_ENV, value)
    client = fetch_client(timeout=1.0, headers={"User-Agent": "legba-research/1.0"})
    assert isinstance(client, expected)
    # Fingerprint only: the identifying UA is carried through untouched.
    assert client._headers == {"User-Agent": "legba-research/1.0"}
    assert imp.IMPERSONATE_PROFILE == "chrome"
    assert client._request_kwargs()["impersonate"] == "chrome"
    assert client._request_kwargs()["allow_redirects"] is False
    assert client._request_kwargs()["headers"]["User-Agent"] == "legba-research/1.0"


async def test_impersonate_kwarg_overrides_the_env_both_ways(monkeypatch):
    monkeypatch.setenv(FETCH_IMPERSONATE_ENV, "on")
    off = fetch_client(impersonate=False, timeout=1.0)
    try:
        assert isinstance(off, httpx.AsyncClient)
    finally:
        await off.aclose()
    monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)
    on = fetch_client(impersonate=True, timeout=1.0)
    assert isinstance(on, imp.ImpersonatingAsyncClient)


# ---------------------------------------------------------------------------
# 2. The guard, re-expressed — refusals on the IMPERSONATED path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://172.16.4.4/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
    ],
)
async def test_impersonated_client_refuses_a_non_public_target(url):
    """The pre-request assert. Refused BEFORE any connection is attempted —
    the stub session's ``seen`` list proves nothing was even requested."""
    client = _client({})
    with pytest.raises(EgressBlockedError):
        async with client.stream("GET", url):
            pass  # pragma: no cover - the guard raises first
    assert client._session.seen == []


async def test_impersonated_client_refuses_a_non_http_scheme():
    client = _client({})
    with pytest.raises(EgressBlockedError):
        async with client.stream("GET", "file:///etc/passwd"):
            pass  # pragma: no cover


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://10.1.2.3/",
    ],
)
async def test_hop_walk_refuses_a_REDIRECT_into_a_private_target(target):
    """THE piece most likely to be lost in the port.

    httpx gave us per-hop checking for free by re-invoking the transport on
    every redirect. curl_cffi does not, so the hop walk re-asserts each
    ``Location`` itself. A public URL that 302s at the edge into loopback or
    the cloud metadata endpoint must still raise ``EgressBlockedError``.
    """
    start = "https://example.org/article"
    client = _client({start: _StubResponse(302, {"location": target})})
    with pytest.raises(EgressBlockedError):
        async with client.stream("GET", start):
            pass  # pragma: no cover
    # The FIRST hop was made (it was public); the second never was.
    assert client._session.seen == [start]


async def test_hop_walk_refuses_a_RELATIVE_redirect_resolved_into_a_bad_host():
    """A ``Location`` is resolved against the current URL before the check, so
    a relative hop cannot smuggle past it by being unparseable on its own."""
    start = "https://example.org/a"
    client = _client({
        start: _StubResponse(301, {"location": "//127.0.0.1/b"}),
    })
    with pytest.raises(EgressBlockedError):
        async with client.stream("GET", start):
            pass  # pragma: no cover


async def test_hop_walk_follows_a_public_redirect_and_reports_the_final_url():
    first, second = "https://example.org/a", "https://example.org/b"
    client = _client({
        first: _StubResponse(302, {"location": second}),
        second: _StubResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
    })
    async with client.stream("GET", first) as resp:
        assert resp.status_code == 200
        assert resp.url == second
        chunks = [c async for c in resp.aiter_bytes()]
    assert b"".join(chunks) == b"<html>ok</html>"
    assert client._session.seen == [first, second]


async def test_hop_walk_stops_at_the_redirect_ceiling():
    routes = {
        f"https://example.org/{n}": _StubResponse(
            302, {"location": f"https://example.org/{n + 1}"},
        )
        for n in range(20)
    }
    client = _client(routes)
    with pytest.raises(httpx.TooManyRedirects):
        async with client.stream("GET", "https://example.org/0"):
            pass  # pragma: no cover
    assert len(client._session.seen) == imp.MAX_REDIRECTS + 1


async def test_redirects_are_NOT_followed_when_the_caller_said_not_to():
    start = "https://example.org/a"
    client = imp.ImpersonatingAsyncClient(mode=imp.MODE_ON, follow_redirects=False)
    client._session = _StubSession({start: _StubResponse(302, {"location": "/b"})})
    async with client.stream("GET", start) as resp:
        assert resp.status_code == 302


async def test_raise_for_status_speaks_httpx_not_curl():
    """Every call site catches ``httpx.HTTPError``. A curl-native exception
    escaping the adapter would turn a handled 403 into an unhandled crash."""
    url = "https://example.org/blocked"
    client = _client({url: _StubResponse(403, {}, b"<html>Just a moment...</html>")})
    async with client.stream("GET", url) as resp:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            resp.raise_for_status()
    assert caught.value.response.status_code == 403
    assert isinstance(caught.value, httpx.HTTPError)


async def test_egress_blocked_error_is_still_an_httpx_transport_error():
    """``EgressBlockedError`` subclasses ``httpx.TransportError``, which is why
    ``except EgressBlockedError`` at the call sites keeps working unchanged on
    the new path — and why a site that only catches ``httpx.HTTPError`` still
    catches it."""
    assert issubclass(EgressBlockedError, httpx.HTTPError)


# ---------------------------------------------------------------------------
# 3. The size cap holds THROUGH the adapter (review §5 test 3)
# ---------------------------------------------------------------------------


async def test_page_byte_cap_holds_against_a_lying_content_length():
    """``_fetch_page``'s two-stage cap — declared Content-Length first, then
    the actual stream — must still fire when the client is the impersonated
    adapter rather than httpx."""
    from legba.data.analysts.agency.research_tools import (
        _PageTooLargeError,
        _fetch_page,
    )

    url = "https://example.org/huge"
    body = b"x" * 5_000
    client = _client({
        url: _StubResponse(
            200, {"content-type": "text/html", "content-length": "10"}, body,
        ),
    })
    with pytest.raises(_PageTooLargeError):
        await _fetch_page(client, url, max_bytes=1_000)


async def test_get_through_the_adapter_is_web_fetch_shaped():
    """``web_tools.web_fetch`` calls ``client.get(url)`` and then reads
    ``response.text`` / ``.status_code`` / ``.headers`` / ``.url`` AFTER the
    call returns — i.e. after the stream is closed. The buffered read has to
    happen inside ``get``, or the tool gets an empty body on this path."""
    url = "https://example.org/page"
    client = _client({
        url: _StubResponse(
            200, {"content-type": "text/html"}, b"<html>body text</html>",
        ),
    })
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.text == "<html>body text</html>"
    assert str(resp.url) == url
    assert resp.headers.get("content-type") == "text/html"


async def test_get_walks_redirects_and_still_refuses_a_private_hop():
    start = "https://example.org/go"
    client = _client({start: _StubResponse(302, {"location": "http://10.0.0.9/x"})})
    with pytest.raises(EgressBlockedError):
        await client.get(start)


async def test_page_fetch_through_the_adapter_returns_the_httpx_shaped_tuple():
    from legba.data.analysts.agency.research_tools import _fetch_page

    url = "https://example.org/ok"
    client = _client({
        url: _StubResponse(
            200, {"content-type": "text/html; charset=utf-8"}, b"<html>hi</html>",
        ),
    })
    body, ctype, encoding, status, final = await _fetch_page(
        client, url, max_bytes=1_000_000,
    )
    assert body == b"<html>hi</html>"
    assert ctype == "text/html; charset=utf-8"
    assert encoding == "utf-8"
    assert status == 200
    assert final == url


# ---------------------------------------------------------------------------
# Wave O / o1 — the PAGE fetch inherits the pinned handshake.
#
# ``fetch_client``'s flag-off path is still the one statement
# ``guarded(**kwargs)``, so whatever the transport factory pins, the page
# fetcher gets. The cipher pin is the difference between the reference builder
# reading Akamai-fronted syndication and burning its whole timeout twice on an
# empty-message ReadTimeout.
# ---------------------------------------------------------------------------


async def test_page_fetch_carries_the_pinned_ciphers_and_http2(monkeypatch):
    from legba.data.sources._egress import fetch_verify_default

    monkeypatch.delenv(FETCH_IMPERSONATE_ENV, raising=False)
    monkeypatch.delenv("LEGBA_FETCH_TLS_CIPHERS", raising=False)
    monkeypatch.delenv("LEGBA_FETCH_HTTP2", raising=False)

    client = fetch_client(
        guarded=guarded_async_client,
        follow_redirects=True, timeout=20.0,
        headers={"User-Agent": "legba-web-tools/1.0"},
    )
    try:
        transport = client._transport  # noqa: SLF001
        assert isinstance(transport, SsrfGuardedTransport)
        pool = transport._pool  # noqa: SLF001
        assert pool._ssl_context is fetch_verify_default()  # noqa: SLF001
        assert pool._http2 is True  # noqa: SLF001
        # The UA is untouched by all of this — the robots decision and the
        # name in the publisher's log stay the same string.
        assert client.headers["user-agent"] == "legba-web-tools/1.0"
    finally:
        await client.aclose()
