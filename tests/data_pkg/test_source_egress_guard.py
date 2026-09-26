# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSRF egress guard for HTTP source fetchers (legba.data.sources._egress).

Pure-logic coverage — IP literals + ``localhost`` resolve locally, so these
tests need no network."""
from __future__ import annotations

import pytest

from legba.data.sources._egress import EgressBlockedError, assert_public_host


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",          # loopback
        "10.0.0.1",           # RFC1918
        "172.16.0.1",         # RFC1918
        "192.168.1.1",        # RFC1918
        "169.254.169.254",    # cloud metadata (link-local)
        "0.0.0.0",            # unspecified
        "::1",                # v6 loopback
        "fc00::1",            # v6 unique-local
        "fe80::1",            # v6 link-local
        "::ffff:127.0.0.1",   # v4-mapped loopback
        "::ffff:10.0.0.5",    # v4-mapped RFC1918
    ],
)
def test_blocks_non_public(addr: str):
    with pytest.raises(EgressBlockedError):
        assert_public_host(addr, 443)


@pytest.mark.parametrize(
    "addr",
    ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"],
)
def test_allows_public(addr: str):
    # Must not raise.
    assert_public_host(addr, 443)


def test_localhost_hostname_resolves_and_blocks():
    with pytest.raises(EgressBlockedError):
        assert_public_host("localhost", 80)


def test_empty_host_blocked():
    with pytest.raises(EgressBlockedError):
        assert_public_host("", 80)


@pytest.mark.asyncio
async def test_guarded_transport_rejects_private_url():
    import httpx

    from legba.data.sources._egress import SsrfGuardedTransport

    transport = SsrfGuardedTransport()
    req = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")

    with pytest.raises(EgressBlockedError):
        await transport.handle_async_request(req)


# ---------------------------------------------------------------------------
# A7 — opt-in internal-host allowlist (RSSHub lane sidecar).
# ---------------------------------------------------------------------------


def test_internal_host_blocked_without_allowlist():
    """A private hostname (e.g. the RSSHub sidecar) is blocked by default —
    the allowlist is opt-in; unset means the guard is unchanged."""
    with pytest.raises(EgressBlockedError):
        # `localhost` stands in for any private-resolving internal name; it is
        # not on the (unset) allowlist, so the guard must still block it.
        assert_public_host("localhost", 1200)


def test_allowlisted_internal_host_permitted(monkeypatch):
    """With the sidecar hostname on LEGBA_EGRESS_ALLOW_HOSTS the exact-name
    match is permitted BEFORE resolution — so `rsshub` (private compose IP) is
    reachable while every other internal address stays blocked."""
    monkeypatch.setenv("LEGBA_EGRESS_ALLOW_HOSTS", "rsshub, other-sidecar")
    # Exact allowlisted names — permitted without a DNS round trip.
    assert_public_host("rsshub", 1200)
    assert_public_host("RSSHub", 1200)  # case-insensitive
    assert_public_host("other-sidecar", 8080)
    # A NON-allowlisted internal name is still blocked.
    with pytest.raises(EgressBlockedError):
        assert_public_host("localhost", 80)
    # A public IP remains allowed regardless of the allowlist.
    assert_public_host("8.8.8.8", 443)


# ---------------------------------------------------------------------------
# Wave O / o1 — the transport owns the handshake.
#
# httpx reads `verify=` and `http2=` off the TRANSPORT, and this module is what
# mounts one. Before o1 the transport was built with NO arguments, so both were
# silently dropped for every fetch in the tree and the handshake was whatever
# the base image's OpenSSL was built with: Debian 12 / OpenSSL 3.0.18 offers 60
# cipher suites, Debian 13 / OpenSSL 3.5.7 offers 17, and an Akamai-fronted
# host (usnews.com, carrying the Reuters wire) serves the first and tarpits the
# second. These tests pin the built client's CONFIGURATION — no network.
# ---------------------------------------------------------------------------


async def _closing(client):
    try:
        return client._transport  # noqa: SLF001 — the mounted transport is the subject
    finally:
        await client.aclose()


def _pool_of(transport):
    """httpx keeps the pool that owns the TLS context and the ALPN offer."""
    return transport._pool  # noqa: SLF001


@pytest.mark.asyncio
async def test_default_client_pins_the_cipher_list_and_offers_http2(monkeypatch):
    """The defaults reach the handshake: the pinned context object itself is on
    the pool, and h2 is offered in ALPN."""
    from legba.data.sources._egress import (
        fetch_tls_ciphers,
        fetch_verify_default,
        guarded_async_client,
    )

    monkeypatch.delenv("LEGBA_FETCH_TLS_CIPHERS", raising=False)
    monkeypatch.delenv("LEGBA_FETCH_HTTP2", raising=False)
    assert fetch_tls_ciphers() == "DEFAULT:@SECLEVEL=2"

    pool = _pool_of(await _closing(guarded_async_client(timeout=5.0)))
    assert pool._http2 is True, "the fetcher must offer h2 in ALPN"  # noqa: SLF001
    # The SAME context object the module pinned — not a fresh default one.
    assert pool._ssl_context is fetch_verify_default()  # noqa: SLF001
    # …and it really is the wide offer, not the image's build default.
    assert len(pool._ssl_context.get_ciphers()) > 17  # noqa: SLF001


@pytest.mark.asyncio
async def test_pinned_context_is_shared_not_per_host(monkeypatch):
    """The pin is host-agnostic — two clients get the one cached context, so
    there is nowhere for a per-site branch to hide."""
    from legba.data.sources._egress import guarded_async_client

    monkeypatch.delenv("LEGBA_FETCH_TLS_CIPHERS", raising=False)
    first = _pool_of(await _closing(guarded_async_client(timeout=5.0)))
    second = _pool_of(await _closing(guarded_async_client(timeout=9.0)))
    assert first._ssl_context is second._ssl_context  # noqa: SLF001


@pytest.mark.asyncio
async def test_caller_kwargs_reach_the_transport(monkeypatch):
    """A caller that names `verify=`/`http2=` gets exactly what it asked for.

    This is the regression the lane exists for: these two used to be accepted
    by `httpx.AsyncClient` and then ignored, because a mounted transport owns
    them.
    """
    import ssl as _ssl

    import httpx as _httpx

    from legba.data.sources._egress import guarded_async_client

    monkeypatch.delenv("LEGBA_FETCH_HTTP2", raising=False)
    mine: _ssl.SSLContext = _httpx.create_ssl_context()

    pool = _pool_of(await _closing(
        guarded_async_client(timeout=5.0, verify=mine, http2=False)))
    assert pool._ssl_context is mine  # noqa: SLF001
    assert pool._http2 is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_caller_transport_is_left_alone():
    """A caller that mounts its own transport owns the handshake completely."""
    from legba.data.sources._egress import SsrfGuardedTransport, guarded_async_client

    own = SsrfGuardedTransport()
    assert await _closing(guarded_async_client(timeout=5.0, transport=own)) is own


@pytest.mark.asyncio
async def test_http2_kill_switch(monkeypatch):
    from legba.data.sources._egress import fetch_http2_default, guarded_async_client

    for value in ("off", "OFF", "0", "false", "no"):
        monkeypatch.setenv("LEGBA_FETCH_HTTP2", value)
        assert fetch_http2_default() is False, value
    monkeypatch.setenv("LEGBA_FETCH_HTTP2", "off")
    pool = _pool_of(await _closing(guarded_async_client(timeout=5.0)))
    assert pool._http2 is False  # noqa: SLF001


def test_empty_cipher_env_inherits_the_build_default(monkeypatch):
    """The escape hatch: an operator can hand the handshake back to the image."""
    from legba.data.sources._egress import fetch_tls_ciphers, fetch_verify_default

    monkeypatch.setenv("LEGBA_FETCH_TLS_CIPHERS", "  ")
    assert fetch_tls_ciphers() == ""
    assert fetch_verify_default() is True


def test_unusable_cipher_string_degrades_loudly(monkeypatch, caplog):
    """A cipher list this OpenSSL rejects must not take the fetcher down — but
    it must say so, naming the knob."""
    from legba.data.sources import _egress

    monkeypatch.setenv("LEGBA_FETCH_TLS_CIPHERS", "NOT-A-CIPHER-SUITE")
    with caplog.at_level("WARNING"):
        assert _egress.fetch_verify_default() is True
    assert "LEGBA_FETCH_TLS_CIPHERS" in caplog.text


def test_missing_h2_degrades_loudly(monkeypatch, caplog):
    """`h2` is a declared dependency (httpx[http2]); if an image ever ships
    without it the fetcher falls back to HTTP/1.1 and NAMES the dependency
    rather than raising on every client construction."""
    from legba.data.sources import _egress

    monkeypatch.delenv("LEGBA_FETCH_HTTP2", raising=False)
    monkeypatch.setattr(_egress, "_h2_installed", lambda: False)
    with caplog.at_level("WARNING"):
        assert _egress.fetch_http2_default() is False
    assert "h2" in caplog.text
    assert "LEGBA_FETCH_HTTP2" in caplog.text
