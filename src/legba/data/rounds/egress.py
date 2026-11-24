# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An egress guard for the round / bake-off HARNESS process (lane G-6).

WHY. In lane G-5's first runs one tool read tried to reach the compose
service name ``opensearch`` from the HOST. On this host the resolver
qualifies a bare name with its search domain and that domain answers every
name with a public address, so ``opensearch`` resolved to a public machine.
The connection failed and nothing was sent — by luck. This guard makes it a
rule: the harness opens connections only to

* a LOOPBACK address (``127.0.0.0/8``, ``::1``; ``localhost``);
* a DECLARED CONTAINER NAME (a compose service) that resolves only to
  private addresses — a declared name that resolves to a public address is
  refused, naming the address, before any connection is opened;
* a declared GATEWAY host (the model gateway), at the addresses its own
  resolution returned;
* a Unix socket.

Anything else — an undeclared name, a raw public address — is refused with
:class:`EgressRefused` (an ``OSError``, so clients fail the way an
unreachable host fails).

HOW. :func:`install` wraps ``socket.getaddrinfo`` (every client here —
asyncio, httpx, asyncpg — resolves through it) to vet the NAME and record the
addresses a vetted name resolved to, and adds an audit hook on
``socket.connect`` to vet the ADDRESS actually connected to. An audit hook
cannot be removed: install it only in a harness process, never in a server.
:func:`vet_name` and :func:`vet_address` are the pure rules (tested
directly); the install is tested in a subprocess.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any


class EgressRefused(ConnectionRefusedError):
    """The harness refused to resolve or connect to a destination."""


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


def is_loopback(addr: str) -> bool:
    ip = _ip(addr)
    return ip is not None and ip.is_loopback


def is_private(addr: str) -> bool:
    """A private, non-global address (RFC 1918, ULA, link-local) — never a
    public one. Loopback counts as private."""
    ip = _ip(addr)
    return ip is not None and (ip.is_loopback or (ip.is_private and not ip.is_global)
                               or ip.is_link_local)


@dataclass
class EgressPolicy:
    """What the harness may reach. ``allowed`` grows as vetted names resolve."""

    gateway_hosts: frozenset[str] = frozenset()
    container_names: frozenset[str] = frozenset()
    allowed: set[str] = field(default_factory=set)
    refused: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note(self, kind: str, target: str, reason: str) -> None:
        with self._lock:
            self.refused.append({"kind": kind, "target": target, "reason": reason})


def vet_name(policy: EgressPolicy, host: str, addrs: Iterable[str]) -> tuple[bool, str]:
    """May the harness use ``host``, which resolved to ``addrs``? Records the
    addresses of an allowed name as connectable."""
    name = (host or "").strip().rstrip(".").lower()
    addrs = [a for a in addrs if a]
    if _ip(name) is not None:
        ok = is_loopback(name) or name in policy.allowed
        return ok, "loopback_address" if ok else "address_not_declared"
    if name == "localhost":
        ok = all(is_loopback(a) for a in addrs)
        if ok:
            policy.allowed.update(addrs)
        return ok, "localhost" if ok else "localhost_not_loopback"
    if name in policy.gateway_hosts:
        policy.allowed.update(addrs)
        return True, "gateway_host"
    if name in policy.container_names:
        public = [a for a in addrs if not is_private(a)]
        if public or not addrs:
            return False, (f"container_name_resolved_public:{','.join(public)}" if public
                           else "container_name_unresolved")
        policy.allowed.update(addrs)
        return True, "container_name_private"
    return False, "undeclared_host"


def vet_address(policy: EgressPolicy, addr: str) -> tuple[bool, str]:
    """May the harness connect to ``addr``?"""
    if is_loopback(addr):
        return True, "loopback"
    if addr in policy.allowed:
        return True, "vetted"
    return False, "address_not_vetted"


_installed: EgressPolicy | None = None


def install(policy: EgressPolicy, *,
            resolver: Callable[..., Any] | None = None) -> EgressPolicy:
    """Install the guard in THIS process (once; a second call returns the
    first policy). ``resolver`` replaces the real ``getaddrinfo`` (tests)."""
    global _installed
    if _installed is not None:
        return _installed
    real = resolver or socket.getaddrinfo

    def guarded(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        h = host.decode() if isinstance(host, bytes) else str(host or "")
        if h and _ip(h) is not None:
            ok, why = vet_name(policy, h, [h])
            if not ok:
                policy.note("resolve", h, why)
                raise EgressRefused(f"egress refused: {h} ({why})")
            return real(host, port, *args, **kwargs)
        infos = real(host, port, *args, **kwargs)
        addrs = sorted({str(i[4][0]) for i in infos if i and len(i) > 4 and i[4]})
        ok, why = vet_name(policy, h, addrs)
        if not ok:
            policy.note("resolve", h, why)
            raise EgressRefused(f"egress refused: {h!r} ({why})")
        return infos

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event != "socket.connect" or len(args) < 2:
            return
        sock, address = args[0], args[1]
        fam = getattr(sock, "family", None)
        if fam not in (socket.AF_INET, socket.AF_INET6):
            return
        if not isinstance(address, tuple) or not address:
            return
        addr = str(address[0])
        ok, why = vet_address(policy, addr)
        if not ok:
            policy.note("connect", addr, why)
            raise EgressRefused(f"egress refused: connect to {addr} ({why})")

    socket.getaddrinfo = guarded  # type: ignore[assignment]
    sys.addaudithook(hook)
    _installed = policy
    return policy


def installed() -> EgressPolicy | None:
    return _installed


__all__ = ["EgressPolicy", "EgressRefused", "install", "installed", "is_loopback", "is_private",
           "vet_address", "vet_name"]
