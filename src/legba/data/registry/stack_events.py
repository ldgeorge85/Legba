# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATS subject naming + payload helpers for the stack registry (L-111).

Subjects follow the convention agreed in the L-111 brief:

    stack.component.<action>.<kind>.<component_id>

For dead-letter (`design/legba_observability.md` §6, `stack` namespace):

    legba.dlq.stack.<kind>.<component_id>

Health-change events are a separate sub-tree so consumers can subscribe
selectively without seeing every register/update:

    stack.component.health_changed.<kind>.<component_id>
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

STACK_TOPIC_PREFIX = "stack.component"
STACK_DLQ_PREFIX = "legba.dlq.stack"

StackAction = Literal[
    "registered",
    "updated",
    "retired",
    "configured",
    "activated",
    "paused",
    "resumed",
    "promoted",
    "rolled_back",
    "health_changed",
]


def stack_subject(action: StackAction, kind: str, component_id: str) -> str:
    """Per-action NATS subject for a stack component."""
    return f"{STACK_TOPIC_PREFIX}.{action}.{kind}.{component_id}"


def stack_health_subject(kind: str, component_id: str) -> str:
    return stack_subject("health_changed", kind, component_id)


def stack_dead_letter_subject(kind: str, component_id: str | None) -> str:
    """DLQ subject. `component_id` may be unknown if the payload didn't parse."""
    return f"{STACK_DLQ_PREFIX}.{kind}.{component_id or '__unknown__'}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def stack_event_payload(
    *,
    action: StackAction,
    kind: str,
    component_id: str,
    actor: str,
    version: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    schema_uri: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Payload published on `stack.component.<action>.*` NATS subjects."""
    payload: dict[str, Any] = {
        "component_id": component_id,
        "kind": kind,
        "action": action,
        "actor": actor,
        "timestamp": _now_iso(),
    }
    if version is not None:
        payload["version"] = version
    if from_version is not None:
        payload["from_version"] = from_version
    if to_version is not None:
        payload["to_version"] = to_version
    if schema_uri is not None:
        payload["schema_uri"] = schema_uri
    if extra:
        payload.update(extra)
    return payload


def stack_dlq_event_payload(
    *,
    kind: str,
    component_id: str | None,
    actor: str,
    declared_schema_uri: str | None,
    error_kind: str,
    error_summary: str,
    dead_letter_id: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "component_id": component_id,
        "actor": actor,
        "declared_schema_uri": declared_schema_uri,
        "error_kind": error_kind,
        "error_summary": error_summary,
        "dead_letter_id": dead_letter_id,
        "timestamp": _now_iso(),
    }
