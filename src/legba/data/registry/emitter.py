# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATS event emission for the registries.

A thin async wrapper that:
  * formats payload as canonical JSON,
  * publishes to a JetStream subject,
  * swallows + logs publish failures (the registry already wrote to
    Postgres; losing a NATS event is non-fatal but loud).

Tests use `NullEventEmitter` to capture publishes without a live NATS.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from ..nats import NatsStore
from ..provenance import canonical_json

logger = logging.getLogger(__name__)


@runtime_checkable
class RegistryEventEmitter(Protocol):
    """Surface registries depend on (real NATS or test capture)."""

    async def publish(self, subject: str, payload: dict[str, Any]) -> None: ...


class NATSEventEmitter:
    """JetStream-backed implementation.

    Best-effort publish: failures are logged but do not propagate, because
    the mutation has already landed in Postgres. The reconciliation loop
    (L-103) will catch any consumer that misses an event by polling the
    table at startup.
    """

    def __init__(self, nats_store: NatsStore):
        self._store = nats_store

    async def publish(self, subject: str, payload: dict[str, Any]) -> None:
        body = canonical_json(payload)
        try:
            await self._store.publish_json(subject, body)
        except Exception as exc:
            logger.warning(
                "nats publish failed subject=%s err=%s len=%d",
                subject, exc, len(body),
            )


class NullEventEmitter:
    """In-memory capture for tests. Records publish calls in `.published`."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, dict(payload)))

    def clear(self) -> None:
        self.published.clear()

    def subjects(self) -> list[str]:
        return [s for s, _ in self.published]
