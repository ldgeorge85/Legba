# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The transport seam of the SUBSCRIBE connector (lane I-4).

A transport holds ONE connection to an upstream and turns what arrives into
:class:`~legba.data.sources.stream_event.StreamEvent` values (plus
:class:`Activity` markers for keep-alives and server reconnect hints). It
knows nothing of sinks, cursors or receipts — the connector
(:mod:`.stream_connector`) owns those, so every transport lands records on
the same spine.

The interface is small on purpose, so a second transport is one module:

* ``name`` and ``supports_resume`` (class attributes);
* ``open(resume_id=...)`` — an async context manager that connects (raising
  :class:`TransportError` on a refused or failed connect) and yields an async
  iterator of ``StreamEvent | Activity``. The iterator raises
  :class:`StreamStalled` when no byte arrived for the idle timeout and
  :class:`TransportError` when the connection drops; it ends when the
  upstream closes the stream cleanly.

Built: ``sse`` (:mod:`.stream_sse`). Declared and refused at save
(``SourceSubscribe``, SEAMS #78): ``websocket``, ``nats``. :func:`build_transport`
refuses an unbuilt name loudly too, so a descriptor that slipped past the
save check still cannot reach a connect.
"""

from __future__ import annotations

import importlib
import random
from dataclasses import dataclass
from typing import Any, Protocol

from ..data.schemas.source_subscribe import BUILT_TRANSPORTS, SUBSCRIBE_SEAM

__all__ = [
    "Activity",
    "StreamStalled",
    "StreamTransport",
    "TransportError",
    "TransportNotBuilt",
    "backoff_delay",
    "build_transport",
]

#: name -> "module:Class" (resolved lazily so the registry never imports a
#: transport's client library).
_TRANSPORT_TABLE: dict[str, str] = {
    "sse": "legba.runtime.stream_sse:SseTransport",
}


class TransportError(Exception):
    """A connect was refused or a held connection failed.

    ``permanent`` = retrying soon cannot help (a 4xx other than 408/429, a
    blocked host, a missing credential): the connector backs off to its
    maximum and reports the source ``refused`` instead of ``reconnecting``.
    ``retry_after`` (seconds) is an upstream hint the backoff honours as a
    floor.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 permanent: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.permanent = permanent
        self.retry_after = retry_after


class TransportNotBuilt(TransportError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"subscribe transport {name!r} is declared but not built ({SUBSCRIBE_SEAM}); "
            f"built: {sorted(BUILT_TRANSPORTS)}", permanent=True,
        )


class StreamStalled(Exception):
    """No byte (event or keep-alive) arrived within the idle timeout."""


@dataclass
class Activity:
    """Bytes arrived that are not an event: a keep-alive comment
    (``kind='heartbeat'``) or a server reconnect hint (``kind='retry'``,
    ``retry_ms``). Resets the idle timer; never reaches a sink."""

    kind: str
    retry_ms: int | None = None


class StreamTransport(Protocol):
    name: str
    supports_resume: bool

    def open(self, *, resume_id: str | None) -> Any: ...


def build_transport(name: str, subscribe: Any, *, headers: dict[str, str],
                    client_factory: Any = None) -> StreamTransport:
    """The transport for ``subscribe.transport`` (fails loud when not built)."""
    target = _TRANSPORT_TABLE.get(name)
    if target is None or name not in BUILT_TRANSPORTS:
        raise TransportNotBuilt(name)
    module, _, cls_name = target.partition(":")
    cls = getattr(importlib.import_module(module), cls_name)
    return cls(subscribe, headers=headers, client_factory=client_factory)


def backoff_delay(attempt: int, *, initial: float, cap: float, floor: float = 0.0,
                  rng: random.Random | None = None) -> float:
    """Reconnect delay before ``attempt`` (1-based): FULL JITTER over
    ``min(cap, initial * 2**(attempt-1))`` — uniform in ``[0, ceiling]`` — so a
    fleet of connectors dropped by one upstream outage does not reconnect in
    lock-step; never below ``floor`` (an upstream ``retry``/``Retry-After``
    hint), never above ``cap`` unless the floor says so."""
    ceiling = min(cap, initial * (2 ** max(0, attempt - 1)))
    draw = (rng or random).uniform(0.0, ceiling)
    return max(draw, floor)
