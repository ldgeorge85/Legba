# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dead-letter writer for descriptor + stack registries.

Per `design/legba_observability.md` §6(a). A registration / update that
fails validation lands here so the operator (or the UI dead-letter panel,
L-092) can inspect + fix-and-resubmit.

Shared by L-110 (descriptor namespace) and L-111 (stack namespace) via the
`namespace` column on `descriptor_dead_letter`.

Each insert publishes a `legba.dlq.<namespace>.*` event (best-effort —
NATS failures don't roll back the DLQ insert).

L-221 (live tail / DLQ panel): when an :class:`RegistryEventEmitter` is
threaded into the constructor, every successful insert also emits a
``legba.dlq.descriptor.{row_id}`` JetStream event so the UI's dead-letter
panel can render new DLQ rows without round-tripping to Postgres. The
emitter is optional — legacy callers that don't supply one continue to
write rows with no NATS side effect.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from ..provenance import canonical_json
from .emitter import RegistryEventEmitter

logger = logging.getLogger(__name__)

# L-221: live-tail subject for the UI dead-letter panel.
# Keyed by the DLQ row id so the panel can match an event to a single row
# (the per-namespace ``legba.dlq.descriptor.<family>.<id>`` subject already
# published by ``DescriptorRegistry._publish_dlq_event`` is keyed by the
# *descriptor* id, not the row id, so it can't carry the row pkey).
DLQ_DESCRIPTOR_SUBJECT_PREFIX = "legba.dlq.descriptor"


def dlq_descriptor_row_subject(row_id: UUID) -> str:
    """Compose the per-row DLQ NATS subject for descriptor entries.

    Note this is *different* from
    :func:`legba.data.registry.events.dead_letter_subject`, which keys on
    ``<family>.<descriptor_id>`` for the upstream registry helper. This
    subject is keyed by the dead-letter row id (the only stable handle
    once a row exists) so the UI panel can render one event per row.
    """
    return f"{DLQ_DESCRIPTOR_SUBJECT_PREFIX}.{row_id}"


@dataclass(frozen=True)
class DLQEntry:
    id: UUID
    attempted_at: datetime
    actor: str
    namespace: str
    declared_schema_uri: str | None
    validation_error: dict[str, Any]
    resolution: str | None


class DescriptorDeadLetter:
    """Async writer for the `descriptor_dead_letter` table.

    When ``emitter`` is supplied, every successful insert publishes a
    payload on ``legba.dlq.descriptor.{row_id}`` carrying the row id, the
    reason (validation_error summary), the namespace, the descriptor id
    (best-effort — read from ``attempted_payload['descriptor_id']`` when
    present), and the declared schema uri. The publish is best-effort:
    failures are logged but don't fail the insert.
    """

    def __init__(
        self,
        store,
        *,
        emitter: RegistryEventEmitter | None = None,
    ) -> None:  # PostgresStore — typed loosely to avoid cycle
        self._store = store
        self._emitter = emitter

    async def record(
        self,
        *,
        actor: str,
        namespace: str,
        attempted_payload: dict[str, Any],
        validation_error: dict[str, Any],
        declared_schema_uri: str | None = None,
    ) -> DLQEntry:
        """Insert a dead-letter row. Returns the inserted entry."""
        row_id = uuid4()
        async with self._store.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO descriptor_dead_letter
                    (id, actor, namespace, attempted_payload,
                     declared_schema_uri, validation_error)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb)
                RETURNING id, attempted_at, actor, namespace,
                          declared_schema_uri, validation_error, resolution
                """,
                row_id,
                actor,
                namespace,
                canonical_json(attempted_payload).decode("utf-8"),
                declared_schema_uri,
                canonical_json(validation_error).decode("utf-8"),
            )
        ve = row["validation_error"]
        if isinstance(ve, str):
            try:
                ve = json.loads(ve)
            except Exception:
                ve = {"raw": ve}
        entry = DLQEntry(
            id=row["id"],
            attempted_at=row["attempted_at"],
            actor=row["actor"],
            namespace=row["namespace"],
            declared_schema_uri=row["declared_schema_uri"],
            validation_error=ve,
            resolution=row["resolution"],
        )
        logger.warning(
            "dead-letter recorded ns=%s actor=%s schema=%s id=%s",
            namespace, actor, declared_schema_uri, entry.id,
        )

        # L-221: best-effort live-tail event for the DLQ panel.
        if self._emitter is not None:
            await self._emit_row_event(
                entry=entry,
                attempted_payload=attempted_payload,
            )
        return entry

    async def _emit_row_event(
        self,
        *,
        entry: DLQEntry,
        attempted_payload: dict[str, Any],
    ) -> None:
        """Publish the per-row DLQ event on ``legba.dlq.descriptor.{id}``.

        Best-effort: any publish exception is swallowed by the emitter's
        own logging path (see :class:`NATSEventEmitter.publish`); we wrap
        here too so a misbehaving emitter can't corrupt the insert path.
        """
        subject = dlq_descriptor_row_subject(entry.id)
        # The validation_error JSONB body can be large; carry a "reason"
        # summary (rendered string when present) instead of the whole
        # error object so the live-tail event stays small. The UI panel
        # can fetch the full row via the registry API when the operator
        # opens it.
        reason = _summarize_validation_error(entry.validation_error)
        descriptor_id = attempted_payload.get("descriptor_id") if isinstance(
            attempted_payload, dict
        ) else None
        payload: dict[str, Any] = {
            "id": str(entry.id),
            "reason": reason,
            "namespace": entry.namespace,
            "descriptor_id": descriptor_id,
            "actor": entry.actor,
            "declared_schema_uri": entry.declared_schema_uri,
            "attempted_at": entry.attempted_at.isoformat(),
        }
        try:
            await self._emitter.publish(subject, payload)
        except Exception as exc:  # pragma: no cover — emitter already swallows
            logger.warning(
                "dlq live-tail publish failed subject=%s id=%s err=%s",
                subject, entry.id, exc,
            )

    async def list_open(
        self,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[DLQEntry]:
        params: list[Any] = []
        where = ["resolution IS NULL"]
        if namespace is not None:
            params.append(namespace)
            where.append(f"namespace = ${len(params)}")
        params.append(limit)
        sql = (
            "SELECT id, attempted_at, actor, namespace, declared_schema_uri, "
            "validation_error, resolution "
            "FROM descriptor_dead_letter "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY attempted_at DESC LIMIT ${len(params)}"
        )
        async with self._store.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        out: list[DLQEntry] = []
        for r in rows:
            ve = r["validation_error"]
            if isinstance(ve, str):
                try:
                    ve = json.loads(ve)
                except Exception:
                    ve = {"raw": ve}
            out.append(
                DLQEntry(
                    id=r["id"],
                    attempted_at=r["attempted_at"],
                    actor=r["actor"],
                    namespace=r["namespace"],
                    declared_schema_uri=r["declared_schema_uri"],
                    validation_error=ve or {},
                    resolution=r["resolution"],
                )
            )
        return out

    async def resolve(
        self,
        dl_id: UUID,
        resolution: str,
        resolution_ref: UUID | None = None,
    ) -> None:
        async with self._store.acquire() as conn:
            await conn.execute(
                """
                UPDATE descriptor_dead_letter
                SET resolution = $2,
                    resolution_at = NOW(),
                    resolution_ref = $3
                WHERE id = $1
                """,
                dl_id,
                resolution,
                resolution_ref,
            )


def _summarize_validation_error(err: dict[str, Any] | None) -> str:
    """Extract a short, human-readable reason from a JSONB error dict.

    Walks the conventional shape produced by
    :func:`legba.data.provenance.dlq._serialize_validation_error` first
    (``rendered`` key), then falls back to whatever scalar fields are
    available so the live-tail event always carries *some* signal.
    """
    if not err:
        return ""
    rendered = err.get("rendered") if isinstance(err, dict) else None
    if isinstance(rendered, str) and rendered:
        # Strip to a single line — Pydantic's rendered string is multi-line
        # and the UI panel renders it inline.
        first_line = rendered.splitlines()[0].strip()
        return first_line[:240]
    raw = err.get("raw") if isinstance(err, dict) else None
    if isinstance(raw, str) and raw:
        return raw[:240]
    # Final fallback — model + first error's message.
    errors = err.get("errors") if isinstance(err, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        msg = first.get("msg") or first.get("message")
        if isinstance(msg, str):
            return msg[:240]
    return ""
