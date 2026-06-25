# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output dead-letter routing per L-107 §6.

When a write helper sees a payload that fails its declared pydantic schema,
the row is *not* inserted into the target table; instead it lands in
``output_dead_letter`` with the full error context so an operator can fix
the analyst code (or schema bump) and resubmit (UI path L-092).

L-221 (live tail / DLQ panel): callers may pass a ``nats_publish`` closure
to :func:`route_to_output_dead_letter` to fire a
``legba.dlq.output.{row_id}`` JetStream event after the insert so the UI
panel can render new rows in real time. Wire this in by passing
``nats_publish=deps.nats_publish`` from
:class:`legba.runtime.deps.StandardDeps` at the analyst-actor callsite
(the runtime's ``bring_up_production_runtime`` already has the closure on
hand via the connected ``NatsStore``). The publish is best-effort and
swallowed on failure — the DLQ row write is the source of truth.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

import asyncpg
from pydantic import ValidationError

logger = logging.getLogger(__name__)

# L-221: per-row live-tail subject for the output DLQ.
DLQ_OUTPUT_SUBJECT_PREFIX = "legba.dlq.output"


def dlq_output_row_subject(row_id: UUID) -> str:
    """Compose the per-row DLQ NATS subject for output entries."""
    return f"{DLQ_OUTPUT_SUBJECT_PREFIX}.{row_id}"


# Type alias matching :class:`legba.runtime.deps.StandardDeps.nats_publish`.
NatsPublish = Callable[[str, bytes], Awaitable[None]]


@dataclass(frozen=True)
class OutputDeadLetterEntry:
    id: UUID
    run_id: UUID | None
    analyst_id: str
    analyst_version: str
    declared_schema_uri: str
    attempted_payload: dict[str, Any]
    validation_error: dict[str, Any]
    produced_at: datetime


def _serialize_validation_error(err: ValidationError) -> dict[str, Any]:
    """Pydantic ValidationError → JSONB-friendly dict.

    Includes both the structured error list and the rendered string for
    operator readability in the UI panel.
    """
    return {
        "errors": err.errors(),
        "rendered": str(err),
        "model": getattr(err, "title", None) or err.__class__.__name__,
    }


async def route_to_output_dead_letter(
    conn: asyncpg.Connection,
    *,
    analyst_id: str,
    analyst_version: str,
    run_id: UUID | None,
    declared_schema_uri: str,
    attempted_payload: dict[str, Any],
    error: ValidationError | dict[str, Any] | str,
    nats_publish: NatsPublish | None = None,
) -> OutputDeadLetterEntry:
    """Insert a row into ``output_dead_letter``. Returns the inserted entry.

    Accepts a pydantic ``ValidationError`` (preferred), a pre-built error
    dict, or a raw string (last resort — used for non-validation failures
    like unknown kinds).

    When ``nats_publish`` is supplied, a best-effort live-tail event is
    published on ``legba.dlq.output.{row_id}`` carrying the row id, the
    reason summary, the analyst id, and the declared schema uri. Failures
    are logged and do not propagate (the DLQ row write is the source of
    truth; the panel can backfill from Postgres if it missed an event).
    """
    if isinstance(error, ValidationError):
        err_payload = _serialize_validation_error(error)
    elif isinstance(error, dict):
        err_payload = error
    else:
        err_payload = {"rendered": str(error), "errors": [], "model": None}

    new_id = uuid4()
    produced_at = datetime.now(tz=timezone.utc)
    await conn.execute(
        """
        INSERT INTO output_dead_letter
          (id, produced_at, run_id, analyst_id, analyst_version,
           declared_schema_uri, attempted_payload, validation_error)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
        """,
        new_id,
        produced_at,
        run_id,
        analyst_id,
        analyst_version,
        declared_schema_uri,
        json.dumps(attempted_payload, default=_json_default),
        json.dumps(err_payload, default=_json_default),
    )

    # L-221: best-effort live-tail event for the DLQ panel.
    if nats_publish is not None:
        subject = dlq_output_row_subject(new_id)
        event_payload = {
            "id": str(new_id),
            "reason": _summarize_output_validation_error(err_payload),
            "analyst_id": analyst_id,
            "analyst_version": analyst_version,
            "schema_uri": declared_schema_uri,
            "run_id": str(run_id) if run_id is not None else None,
            "produced_at": produced_at.isoformat(),
        }
        try:
            await nats_publish(
                subject,
                json.dumps(event_payload, default=_json_default).encode("utf-8"),
            )
        except Exception as exc:
            logger.warning(
                "output-dlq live-tail publish failed subject=%s id=%s err=%s",
                subject, new_id, exc,
            )

    return OutputDeadLetterEntry(
        id=new_id,
        run_id=run_id,
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        declared_schema_uri=declared_schema_uri,
        attempted_payload=attempted_payload,
        validation_error=err_payload,
        produced_at=produced_at,
    )


def _summarize_output_validation_error(err: dict[str, Any] | None) -> str:
    """Short reason summary for the live-tail event payload."""
    if not err:
        return ""
    rendered = err.get("rendered") if isinstance(err, dict) else None
    if isinstance(rendered, str) and rendered:
        return rendered.splitlines()[0].strip()[:240]
    errors = err.get("errors") if isinstance(err, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        msg = first.get("msg") or first.get("message")
        if isinstance(msg, str):
            return msg[:240]
    return ""


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)
