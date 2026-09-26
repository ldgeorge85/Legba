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

THE TRANSPORT OWNS THE TLS HANDSHAKE AND THE HTTP VERSION (wave O, lane o1)
--------------------------------------------------------------------------
``httpx`` configures TLS and ALPN on the TRANSPORT, not on the client: the
moment a caller mounts an explicit ``transport=``, the ``verify=`` and
``http2=`` arguments it passed to :class:`httpx.AsyncClient` are silently
dropped. :func:`guarded_async_client` mounts one on every fetch in the tree,
so before this module built the transport with arguments, NOTHING a caller
could write reached the handshake — the fetcher's ClientHello and its HTTP
version were whatever the base image's OpenSSL happened to be built with.

That is measurable, and it is what made the same URL behave differently on
the host and inside the images (2026-09-25 reference top-ups, ten desks):

* Debian 12 / OpenSSL 3.0.18 (the host) offers **60** cipher suites from a
  bare ``SSLContext(PROTOCOL_TLS_CLIENT)``; Debian 13 / OpenSSL 3.5.7 (the
  images) offers **17** from the same call. Neither list is configured by us
  — both are the build's compiled-in default.
* Akamai-fronted syndication (``usnews.com``, which carries the Reuters wire)
  fingerprints the ClientHello. With the 60-suite offer it serves the page in
  under a second; with the 17-suite offer it resets the HTTP/2 stream
  (``INTERNAL_ERROR``) and, over HTTP/1.1, accepts the request and then
  answers nothing at all — the empty-message ``httpx.ReadTimeout`` that burned
  the full timeout twice (the retry) per URL and reached the model as a
  blocked host.
* ``set_ciphers("DEFAULT:@SECLEVEL=2")`` restores the 60-suite offer on the
  image WITHOUT lowering the security level, and the image then behaves
  exactly as the host, cell for cell.

So the cipher list is pinned HERE rather than inherited from the base image:
the bytes Legba puts on the wire are Legba's, and a base-image bump can no
longer change which pages the reference builder can read. The pin is a knob
(``LEGBA_FETCH_TLS_CIPHERS``) and it is host-agnostic — there is no per-site
branch anywhere in this module.

HTTP/2 is the second half of the same measurement: over HTTP/1.1 this class
of host tarpits (a full timeout, twice, with an empty error message), while
over HTTP/2 it answers — or refuses in 0.1 s, which the caller can at least
tally honestly. ALPN negotiates it, so a server that does not offer h2 still
gets HTTP/1.1 and nothing changes for it. ``LEGBA_FETCH_HTTP2=off`` is the
kill switch.

What this does NOT change is the User-Agent. usnews.com refuses our UA over
every transport we can build — including the ``curl_cffi`` impersonation
path, which spoofs the fingerprint and deliberately keeps our name. Sending a
browser UA would divorce the robots.txt decision from the agent in the
publisher's log (FETCH_REVIEW §3(a)/§3(e)); that is a policy question owned
by :data:`FETCH_IMPERSONATE_ENV`, not a transport bug, and after this change
the host and the image give the same answer to it.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import ssl
from functools import lru_cache
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)


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


#: FETCH_REVIEW (a) — the browser-fingerprint impersonation flag. Lives HERE
#: (not in the sibling module) so the default ``off`` branch of
#: :func:`fetch_client` can be decided without importing anything. Values:
#: ``off`` (default) / ``on`` / ``fallback`` — parsed by
#: ``_egress_impersonate.impersonate_mode``.
FETCH_IMPERSONATE_ENV = "LEGBA_FETCH_IMPERSONATE"

#: The cipher list every guarded fetch OFFERS in its ClientHello. See the
#: module docstring for the measurement. Set to the empty string to inherit
#: the interpreter's own default context (i.e. whatever the base image's
#: OpenSSL was built with) — that is the pre-2026-09-26 behaviour, and it is
#: what makes a page's readability depend on the base image again.
FETCH_TLS_CIPHERS_ENV = "LEGBA_FETCH_TLS_CIPHERS"

#: ``DEFAULT`` is OpenSSL's own default list; ``@SECLEVEL=2`` holds the
#: security level Debian pins (no SHA-1 signatures, no sub-2048-bit keys), so
#: the pin widens the OFFER without weakening what we will ACCEPT. Measured
#: identical (60 suites) on OpenSSL 3.0.18 and 3.5.7.
DEFAULT_FETCH_TLS_CIPHERS = "DEFAULT:@SECLEVEL=2"

#: HTTP/2 over ALPN for guarded fetches. Default ON (see the docstring);
#: ``off``/``0``/``false``/``no`` turns it off. When the ``h2`` package is
#: absent the client would raise on construction, so the default degrades to
#: HTTP/1.1 — LOUDLY, once, naming the dependency.
FETCH_HTTP2_ENV = "LEGBA_FETCH_HTTP2"

_HTTP2_OFF_VALUES = frozenset({"0", "off", "false", "no"})

#: Sentinel for "the caller did not pass this kwarg at all" — distinct from a
#: caller that deliberately passed ``verify=False`` or ``http2=False``.
_UNSET: Any = object()


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


def fetch_tls_ciphers() -> str:
    """The cipher string to pin, read per call so a late env still applies.

    Empty (the operator set ``LEGBA_FETCH_TLS_CIPHERS=``) means "pin nothing".
    """
    raw = os.environ.get(FETCH_TLS_CIPHERS_ENV)
    return DEFAULT_FETCH_TLS_CIPHERS if raw is None else raw.strip()


@lru_cache(maxsize=4)
def _ssl_context_for(ciphers: str) -> ssl.SSLContext:
    """A verifying context offering ``ciphers``. Cached — contexts are reusable
    across clients and building one costs a certifi bundle parse."""
    context = httpx.create_ssl_context()
    context.set_ciphers(ciphers)
    return context


def fetch_verify_default() -> ssl.SSLContext | bool:
    """What ``verify=`` a guarded fetch gets when the caller names none."""
    ciphers = fetch_tls_ciphers()
    if not ciphers:
        return True
    try:
        return _ssl_context_for(ciphers)
    except ssl.SSLError:
        logger.warning(
            "egress: %s=%r is not a cipher list this OpenSSL accepts — falling "
            "back to the build default (the image, not Legba, then decides "
            "which hosts answer)", FETCH_TLS_CIPHERS_ENV, ciphers,
        )
        return True


@lru_cache(maxsize=1)
def _h2_installed() -> bool:
    try:
        import h2  # noqa: F401,PLC0415 - capability probe, once
    except ImportError:
        return False
    return True


def fetch_http2_default() -> bool:
    """Whether a guarded fetch offers ``h2`` in ALPN when the caller is silent."""
    if os.environ.get(FETCH_HTTP2_ENV, "").strip().lower() in _HTTP2_OFF_VALUES:
        return False
    if not _h2_installed():
        logger.warning(
            "egress: HTTP/2 is on by default but the 'h2' package is missing — "
            "fetching over HTTP/1.1. Install httpx[http2], or set %s=off to "
            "silence this. Akamai-fronted hosts tarpit HTTP/1.1 (a full "
            "timeout, no message) rather than refusing it.", FETCH_HTTP2_ENV,
        )
        return False
    return True


def guarded_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An :class:`httpx.AsyncClient` with the SSRF egress guard installed.

    Drop-in for ``httpx.AsyncClient(**kwargs)`` in source fetchers — all the
    usual kwargs (``timeout``, ``headers``, ``follow_redirects``, …) pass
    through; only the transport is swapped for the guarded one.

    ``verify=`` and ``http2=`` are the two that CANNOT simply pass through:
    httpx reads both off the transport, and this function is what mounts the
    transport. They are therefore lifted onto :class:`SsrfGuardedTransport`,
    where they take effect — a caller that names either gets exactly what it
    asked for, and a caller that names neither gets the pinned cipher list and
    ALPN h2 (module docstring). A caller that mounts its OWN ``transport=``
    owns the handshake completely and nothing is lifted.
    """
    if "transport" not in kwargs:
        verify = kwargs.pop("verify", _UNSET)
        http2 = kwargs.pop("http2", _UNSET)
        kwargs["transport"] = SsrfGuardedTransport(
            verify=fetch_verify_default() if verify is _UNSET else verify,
            http2=fetch_http2_default() if http2 is _UNSET else http2,
        )
    return httpx.AsyncClient(**kwargs)


def fetch_client(
    *,
    impersonate: bool | None = None,
    guarded: Callable[..., Any] = guarded_async_client,
    **kwargs: Any,
) -> Any:
    """The PAGE-fetch client: :func:`guarded_async_client`, or an impersonated
    one when ``LEGBA_FETCH_IMPERSONATE`` says so (FETCH_REVIEW §5).

    ``off`` (the default, and what every deployment gets until an operator
    changes it) is deliberately a SINGLE statement returning the existing
    function with the caller's kwargs untouched — same class, same transport,
    same headers, same redirect handling. There is no wrapper on the default
    path, which is what makes "flag off is byte-identical" a structural claim
    rather than a hopeful one (``tests/data_pkg/test_egress_fetch_client.py``
    asserts it, and zero existing test files were edited for this change).

    ``on`` / ``fallback`` import :mod:`._egress_impersonate` lazily, so an
    image without ``curl_cffi`` is completely unaffected while the flag is off
    and fails LOUD — naming the dep and the flag — the moment it is turned on.

    ``impersonate`` (bool) overrides the env for a caller that has already
    decided; ``None`` (the default) reads the env.

    ``guarded`` is the plain-client factory to use on the ``off`` path. Every
    call site passes its OWN module-level ``guarded_async_client``, which is
    what keeps the off path literally the same call it made before this
    function existed — and keeps the monkeypatch seam four existing e2e suites
    already use (``monkeypatch.setattr(research_tools, "guarded_async_client",
    …)``) pointing at the client the tool actually opens. That is why ZERO
    existing test files were edited for this change, which is the flag-off
    byte-identity claim (the D-5 region-tier precedent).
    """
    if impersonate is False or (
        impersonate is None
        and not os.environ.get(FETCH_IMPERSONATE_ENV, "").strip()
    ):
        # THE DEFAULT PATH. One statement, and not even an import of the
        # impersonation module — the unset-env deployment cannot be changed
        # by code it never loads.
        return guarded(**kwargs)

    from ._egress_impersonate import (  # noqa: PLC0415 - flag-gated, lazy
        MODE_OFF,
        MODE_ON,
        impersonate_mode,
        impersonating_async_client,
    )

    mode = MODE_ON if impersonate else impersonate_mode()
    if mode == MODE_OFF:
        return guarded(**kwargs)
    return impersonating_async_client(mode=mode, guarded=guarded, **kwargs)
