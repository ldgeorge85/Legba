# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acquisition-layer source protocols — PollSource / PushSource (P-06).

The source-first pivot splits acquisition by **how a source delivers**:

  * :class:`PollSource` — Legba *pulls* on a cadence (RSS, GDELT, ACLED,
    OpenSanctions, …). The :class:`~legba.runtime.source_actor.SourceActor`
    registers a Dapr Reminder from the descriptor cadence; on fire it calls
    ``pull(ctx, since)`` and runs the per-source baseline pipeline.
  * :class:`PushSource` — an upstream system *POSTs* events to us (Discord,
    GitHub, Stripe, a camera fleet's match webhook, …). The actor is woken by
    the shared inbound-webhook router (``webhook_router.py``); the handler
    parses the inbound request and yields the same :class:`Signal` shape.

Both refine the base structural-typing :class:`SourceHandler` contract in
``_contract.py``. They are *runtime-checkable Protocols*, not ABCs — a
first-party handler (e.g. :class:`~legba.data.sources.rss.RSSSourceHandler`)
satisfies :class:`PollSource` structurally without importing a base class.

A handler's ``acquisition`` mode comes from its descriptor
(:attr:`SourceDescriptor.acquisition` — ``"poll"`` | ``"push"``); these
protocols let the actor + tests assert that a handler *can* serve that mode:

    assert isinstance(rss_handler, PollSource)      # has pull()
    assert isinstance(discord_handler, PushSource)  # has ingest()

Provisioning (§4.2.1) is orthogonal to poll/push: a poll OR push source may
also need an outbound upstream registration at activation. The optional
:class:`ProvisioningSource` protocol captures that capability; the actor calls
its hooks via :mod:`legba.data.sources.provision`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ._contract import Signal, SourceContext, SourceHealth


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------


@runtime_checkable
class PollSource(Protocol):
    """A source Legba pulls on a cadence.

    The :class:`SourceActor` registers a Dapr Reminder from
    ``descriptor.cadence.schedule`` and, on each fire, drains ``pull``. The
    handler owns its cursor via ``ctx.state_store`` (crash-safe; survives
    actor eviction + sidecar restart).

    ``pull`` is an async generator; ``since`` is the last-pulled watermark
    (``None`` on first run). Handlers SHOULD treat ``since`` as a hint and
    let downstream dedup (P-09) absorb overlap, but MUST persist a cursor so
    a restart doesn't re-pull the whole feed.
    """

    kind: str
    config_schema: type

    def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]: ...

    async def health_check(self, ctx: SourceContext) -> SourceHealth: ...


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


@runtime_checkable
class PushSource(Protocol):
    """A source that POSTs events to Legba (inbound webhook).

    The actor does NOT poll a push source; the shared inbound-webhook router
    (``webhook_router.py``) receives the POST and hands the raw body to
    :meth:`ingest`, which verifies + parses it and yields zero or more
    :class:`Signal`. The actor then runs the baseline + writes + publishes —
    the same downstream path the poll branch uses.

    ``ingest`` takes the raw request bytes + headers so each push kind can
    implement its own signature scheme (Ed25519 for Discord, HMAC for
    GitHub, the fleet's scheme for facial-rec). A bad signature raises;
    the actor maps that to a 4xx via the router.
    """

    kind: str
    config_schema: type

    def ingest(
        self,
        ctx: SourceContext,
        body: bytes,
        headers: dict[str, str],
    ) -> AsyncIterator[Signal]: ...

    async def health_check(self, ctx: SourceContext) -> SourceHealth: ...


# ---------------------------------------------------------------------------
# Provisioning (orthogonal capability — §4.2.1)
# ---------------------------------------------------------------------------


@runtime_checkable
class ProvisioningSource(Protocol):
    """A source that registers an outbound upstream watch at activation.

    Optional capability layered on a poll OR push source. ``on_activate``
    fires the upstream register call (e.g. "watch face X, callback = our
    push URL"); ``on_retire`` deregisters. The full idempotent
    reconciliation/rollback machinery lives in
    :mod:`legba.data.sources.provision`; this protocol just marks that a
    handler participates in it.

    Handlers that don't provision simply omit these methods — the base
    :class:`SourceHandler` lifecycle hooks default to no-op.
    """

    async def on_activate(self, ctx: SourceContext) -> None: ...

    async def on_retire(self, ctx: SourceContext) -> None: ...


def acquisition_protocol_for(mode: str) -> type:
    """Map a descriptor ``acquisition`` mode to its protocol type.

    ``"poll" -> PollSource``, ``"push" -> PushSource``. Used by the actor
    + tests to assert a handler can serve the descriptor's declared mode.
    """
    if mode == "poll":
        return PollSource
    if mode == "push":
        return PushSource
    raise ValueError(f"unknown acquisition mode {mode!r}; expected poll|push")


__all__ = [
    "PollSource",
    "PushSource",
    "ProvisioningSource",
    "acquisition_protocol_for",
]
