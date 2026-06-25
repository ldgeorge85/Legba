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


def test_guarded_transport_rejects_private_url():
    import httpx

    from legba.data.sources._egress import SsrfGuardedTransport

    transport = SsrfGuardedTransport()
    req = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
    import asyncio

    with pytest.raises(EgressBlockedError):
        asyncio.get_event_loop().run_until_complete(
            transport.handle_async_request(req)
        )
