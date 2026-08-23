# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""JetStream stream provisioning for the runtime event surface.

Per the post-bring-up review §3 + L-124 NATS adapter spec, the registry
publishes lifecycle events to NATS subjects (e.g. ``descriptor.updated.target.<id>``)
but until matching JetStream streams exist those publishes silently fail
— ``js.publish`` requires a stream covering the subject.

This module owns the five streams the runtime consumes:

  * ``LEGBA_DESCRIPTOR_EVENTS`` covering ``descriptor.>`` (all action ×
    family × id combos),
  * ``LEGBA_STACK_EVENTS`` covering ``stack.component.>`` (registry-side
    stack-component lifecycle events),
  * ``LEGBA_VOCABULARY_EVENTS`` covering ``vocabulary.updated.>``,
  * ``LEGBA_DLQ_EVENTS`` covering ``legba.dlq.>`` (per-row dead-letter
    events for the UI live-tail panel — L-221),
  * ``LEGBA_VAULT_EVENTS`` covering ``vault.secret.>`` (credential-vault
    rotation events — drives LLM handler-cache eviction so a rotated secret
    doesn't keep serving from a process-lifetime cache; see
    :mod:`legba.data.registry.vault_events`).

Retention is interest-based (auto-cleanup once every consumer ACKs); a
1-hour ``max_age`` is a safety net for events with no live consumer.

``ensure_runtime_event_streams`` is idempotent — re-running on a process
restart short-circuits via :meth:`NatsStore.ensure_stream` (returns
``False`` when the stream already exists).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..nats import NatsStore

logger = logging.getLogger(__name__)


# Stream names — uppercase + underscore per JetStream naming rules.
DESCRIPTOR_EVENTS_STREAM = "LEGBA_DESCRIPTOR_EVENTS"
STACK_EVENTS_STREAM = "LEGBA_STACK_EVENTS"
VOCABULARY_EVENTS_STREAM = "LEGBA_VOCABULARY_EVENTS"
DLQ_EVENTS_STREAM = "LEGBA_DLQ_EVENTS"
VAULT_EVENTS_STREAM = "LEGBA_VAULT_EVENTS"

# Subject filters — match every descriptor/stack/vocabulary event published
# by the registry. `>` is JetStream's recursive wildcard so this covers any
# token depth.
DESCRIPTOR_EVENTS_SUBJECTS = ["descriptor.>"]
STACK_EVENTS_SUBJECTS = ["stack.component.>"]
VOCABULARY_EVENTS_SUBJECTS = ["vocabulary.updated.>"]
# L-221 — per-row dead-letter events. Covers both
# ``legba.dlq.descriptor.<row_id>`` (DescriptorDeadLetter) and
# ``legba.dlq.output.<row_id>`` (route_to_output_dead_letter), plus any
# future ``legba.dlq.<namespace>.<id>`` subjects the DLQ helpers grow.
DLQ_EVENTS_SUBJECTS = ["legba.dlq.>"]
# Credential-vault rotation events (``vault.secret.rotated.<secret_id>`` —
# see vault_events.py). A separate stream from LEGBA_STACK_EVENTS on purpose:
# a vault secret is not a stack component (no ``kind`` token, no per-id
# eviction target), so mixing it into the stack stream would misname what
# that stream covers for anyone inspecting it via ``nats stream ls``.
VAULT_EVENTS_SUBJECTS = ["vault.secret.>"]

# 1 hour safety-net so a long-disconnected consumer can't accumulate
# unbounded backlog. Interest retention removes acked messages immediately.
_DEFAULT_MAX_AGE_SECONDS = 3600


@dataclass(frozen=True)
class StreamProvisionResult:
    """Outcome of one stream's ensure call.

    ``created`` is ``True`` when the stream did not previously exist (a
    fresh ``add_stream`` ran); ``False`` when the stream was already
    present and the call was a no-op.
    """

    name: str
    subjects: tuple[str, ...]
    retention: str
    max_age_seconds: int
    created: bool


async def ensure_runtime_event_streams(
    nats_store: NatsStore,
    *,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
) -> list[StreamProvisionResult]:
    """Idempotently create the five runtime event streams.

    The caller (registry server lifespan, or a bring-up CLI) must have
    already called :meth:`NatsStore.connect` so ``nats_store.js`` is
    available.

    Returns one :class:`StreamProvisionResult` per stream in declaration
    order so callers can log / surface the outcome.
    """
    specs = (
        (DESCRIPTOR_EVENTS_STREAM, DESCRIPTOR_EVENTS_SUBJECTS),
        (STACK_EVENTS_STREAM, STACK_EVENTS_SUBJECTS),
        (VOCABULARY_EVENTS_STREAM, VOCABULARY_EVENTS_SUBJECTS),
        (DLQ_EVENTS_STREAM, DLQ_EVENTS_SUBJECTS),
        (VAULT_EVENTS_STREAM, VAULT_EVENTS_SUBJECTS),
    )
    out: list[StreamProvisionResult] = []
    for name, subjects in specs:
        created = await nats_store.ensure_stream(
            name,
            subjects,
            retention="interest",
            max_age_seconds=max_age_seconds,
        )
        result = StreamProvisionResult(
            name=name,
            subjects=tuple(subjects),
            retention="interest",
            max_age_seconds=max_age_seconds,
            created=created,
        )
        out.append(result)
        logger.info(
            "registry.streams.ensure stream=%s subjects=%s retention=interest "
            "max_age_seconds=%d created=%s",
            name, subjects, max_age_seconds, created,
        )
    return out


__all__ = [
    "DESCRIPTOR_EVENTS_STREAM",
    "DESCRIPTOR_EVENTS_SUBJECTS",
    "DLQ_EVENTS_STREAM",
    "DLQ_EVENTS_SUBJECTS",
    "STACK_EVENTS_STREAM",
    "STACK_EVENTS_SUBJECTS",
    "StreamProvisionResult",
    "VAULT_EVENTS_STREAM",
    "VAULT_EVENTS_SUBJECTS",
    "VOCABULARY_EVENTS_STREAM",
    "VOCABULARY_EVENTS_SUBJECTS",
    "ensure_runtime_event_streams",
]
