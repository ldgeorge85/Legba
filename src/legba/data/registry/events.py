# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATS subject naming + audit payload helpers for the descriptor registry.

Subjects follow the convention agreed in the L-001 brief and topology
redesign §9 (event surface):

    descriptor.<action>.<family>.<descriptor_id>

For dead-letter (per `design/legba_observability.md` §6, also referenced as
`legba.dlq.descriptor.*`):

    legba.dlq.descriptor.<family>.<descriptor_id>

And the vocabulary refresh signal (L-101 §8, "emits NATS events on register
/ deprecate so listeners refresh their allow-lists"):

    vocabulary.updated.<family>

These constants are exported so consumers (UI, runtime supervisors,
observability sinks) can subscribe to a single, stable name.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

DESCRIPTOR_TOPIC_PREFIX = "descriptor"
DEAD_LETTER_TOPIC_PREFIX = "legba.dlq.descriptor"
VOCABULARY_UPDATED_TOPIC = "vocabulary.updated"


Action = Literal[
    "registered",
    "updated",
    "retired",
    "promoted",
    "rolled_back",
    "configured",
    "activated",
    "paused",
    "resumed",
]


def descriptor_subject(action: Action, family: str, descriptor_id: str) -> str:
    """Compose the per-action descriptor NATS subject.

    `family` is one of `target` / `analyst` / `wiring`; `descriptor_id` is
    the operator-chosen string id of the descriptor.
    """
    return f"{DESCRIPTOR_TOPIC_PREFIX}.{action}.{family}.{descriptor_id}"


def dead_letter_subject(family: str, descriptor_id: str | None) -> str:
    """Compose the dead-letter NATS subject.

    `descriptor_id` may be `None` when the failing payload didn't even parse
    far enough to read its id; the subject uses `__unknown__` in that case.
    """
    return f"{DEAD_LETTER_TOPIC_PREFIX}.{family}.{descriptor_id or '__unknown__'}"


def vocabulary_subject(family: str) -> str:
    return f"{VOCABULARY_UPDATED_TOPIC}.{family}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def audit_payload(
    *,
    action: str,
    family: str,
    descriptor_id: str,
    actor_id: str,
    actor_role: str = "operator",
    from_version: str | None = None,
    to_version: str | None = None,
    change_summary: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Compose the canonical audit-log payload.

    The dict returned is canonical-JSON-serialised for the Ed25519 signing
    step (`signing.sign_audit_payload`) and then persisted to
    `descriptor_audit_log`. Shape matches `design/legba_observability.md` §8.
    """
    return {
        "occurred_at": occurred_at or _now_iso(),
        "actor_id": actor_id,
        "actor_role": actor_role,
        "namespace": family,
        "descriptor_id": descriptor_id,
        "action": action,
        "from_version": from_version,
        "to_version": to_version,
        "change_summary": change_summary or {},
    }


def descriptor_event_payload(
    *,
    action: str,
    family: str,
    descriptor_id: str,
    actor: str,
    version: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    schema_uri: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Payload published to the `descriptor.<action>...` NATS subject."""
    payload: dict[str, Any] = {
        "descriptor_id": descriptor_id,
        "family": family,
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


def dead_letter_event_payload(
    *,
    family: str,
    descriptor_id: str | None,
    actor: str,
    declared_schema_uri: str | None,
    error_kind: str,
    error_summary: str,
    dead_letter_id: str | None = None,
) -> dict[str, Any]:
    return {
        "family": family,
        "descriptor_id": descriptor_id,
        "actor": actor,
        "declared_schema_uri": declared_schema_uri,
        "error_kind": error_kind,
        "error_summary": error_summary,
        "dead_letter_id": dead_letter_id,
        "timestamp": _now_iso(),
    }
