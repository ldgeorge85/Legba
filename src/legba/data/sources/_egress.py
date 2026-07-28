# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSRF egress guard for HTTP source fetchers.

Source URLs come from descriptors — operator-registered, and (post P-13
polymorphic discovery) potentially auto-wired from a selector — so a fetcher
could be pointed, deliberately or accidentally, at an INTERNAL address
(``127.0.0.1``, ``10.x``/``172.16.x``/``192.168.x``, link-local, the cloud
metadata endpoint ``169.254.169.254``, IPv6 unique-local, …). Left unguarded
that is a Server-Side Request Forgery vector: the fetcher would happily GET an
internal admin API or the metadata service and ingest the response as a
"signal".

This module provides :class:`SsrfGuardedTransport` — an ``httpx`` transport that
resolves each request's host and REFUSES to connect when any resolved address
is non-public. Because ``httpx`` re-invokes the transport for every redirect
hop, redirects to internal hosts are covered too.

Limitation (documented, not hidden): the guard resolves-then-checks, and
``httpx`` re-resolves at connect time, so a determined DNS-rebinding attacker
controlling an authoritative server could return a public IP to the check and a
private one to the connect (TOCTOU). Closing that fully requires pinning the
socket to the validated IP (which breaks SNI/Host); for our threat model —
a descriptor/selector pointed at an internal address — the resolve-check closes
the realistic vector. A rebinding-hardened transport is a tracked follow-up.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

import httpx


class EgressBlockedError(httpx.TransportError):
    """A source fetch targeted a non-public address — blocked by the SSRF guard."""


def _allowed_internal_hosts() -> frozenset[str]:
    """Operator-declared trusted internal hostnames (opt-in allowlist).

    A co-located sidecar reachable only over the compose network — e.g. the
    RSSHub lane's ``rsshub:1200`` — resolves to a PRIVATE address that the SSRF
    guard below would otherwise refuse. ``LEGBA_EGRESS_ALLOW_HOSTS`` (comma-
    separated hostnames) lets an operator permit EXACTLY those service names.
    Empty/unset (the default for every deployment that hasn't opted in) means
    the guard behaves exactly as before — no internal host is ever permitted.
    Read per-call so a container that sets the env after import still applies.
    """
    raw = os.environ.get("LEGBA_EGRESS_ALLOW_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


# Cloud metadata endpoints (covered by is_link_local, but called out explicitly
# because they are the highest-value SSRF target).
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``ip`` is not a routable public address we'll fetch from."""
    # IPv4-mapped IPv6 (``::ffff:127.0.0.1``) — evaluate the embedded v4.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private          # 10/8, 172.16/12, 192.168/16, 127/8, fc00::/7, …
        or ip.is_loopback
        or ip.is_link_local    # 169.254/16, fe80::/10 (incl. the metadata IP)
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified   # 0.0.0.0 / ::
        or str(ip) in _METADATA_IPS
    )


def assert_public_host(host: str, port: int) -> None:
    """Raise :class:`EgressBlockedError` unless ``host`` is (resolves to) public.

    A bare IP literal is checked directly; a hostname is resolved via
    ``getaddrinfo`` and EVERY returned address must be public.
    """
    if not host:
        raise EgressBlockedError("egress blocked: empty host")
    # Trusted internal-sidecar allowlist (opt-in via LEGBA_EGRESS_ALLOW_HOSTS).
    # Permits an EXACT hostname match only — never a wildcard — so a co-located
    # service like the RSSHub lane's `rsshub` is reachable while every OTHER
    # internal address a descriptor/selector could name stays blocked.
    if host.lower() in _allowed_internal_hosts():
        return
    # Literal IP?
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_blocked(literal):
            raise EgressBlockedError(f"egress blocked: {host} is a non-public address")
        return
    # Hostname — resolve and check every address.
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EgressBlockedError(f"egress: cannot resolve host {host!r}: {exc}") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])  # strip v6 scope id
        except ValueError:
            continue
        if _ip_blocked(ip):
            raise EgressBlockedError(
                f"egress blocked: {host} resolves to non-public address {ip}"
            )


class SsrfGuardedTransport(httpx.AsyncHTTPTransport):
    """``httpx`` transport that validates the target is public before connecting."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise EgressBlockedError(f"egress blocked: unsupported scheme {url.scheme!r}")
        port = url.port or (443 if url.scheme == "https" else 80)
        assert_public_host(url.host, port)
        return await super().handle_async_request(request)


def guarded_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An :class:`httpx.AsyncClient` with the SSRF egress guard installed.

    Drop-in for ``httpx.AsyncClient(**kwargs)`` in source fetchers — all the
    usual kwargs (``timeout``, ``headers``, ``follow_redirects``, …) pass
    through; only the transport is swapped for the guarded one.
    """
    kwargs.setdefault("transport", SsrfGuardedTransport())
    return httpx.AsyncClient(**kwargs)
