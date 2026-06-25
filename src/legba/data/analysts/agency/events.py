# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator-visible governor events (P-11 hard-gate audit surface).

Every governor DECISION — both ALLOW and BLOCK — lands a row in
``governor_events`` (migration 0025) and, when a NATS publisher is wired,
fans out on a coarse control subject so an operator dashboard sees a blocked
call in real time without grepping logs.

This is the "operator-visible governor event" the P-11 acceptance demands: an
over-budget / over-rate / not-allowed call is demonstrably blocked AND the
block is queryable (``SELECT … FROM governor_events WHERE decision='block'``)
and streamable.

The DB write is the source of truth (durable, queryable); the NATS publish is
best-effort telemetry (a publish failure never blocks the gate decision).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Literal

import asyncpg

logger = logging.getLogger(__name__)

# Coarse control subject (tenant / pack) — the structured fields ride the
# payload, matching the PIVOT coarse-subject rule. A dashboard binds
# ``governor.events.>`` (or a per-tenant filter) to see live decisions.
GOVERNOR_EVENTS_SUBJECT_PREFIX = "governor.events"

Decision = Literal["allow", "block"]
# Block causes (machine-readable). 'ok' is the allow cause.
GovernorCause = str

NatsPublish = Callable[[str, bytes], Awaitable[None]]


@dataclass(frozen=True)
class GovernorEvent:
    """One governor decision. Immutable; written once."""

    pack_id: str
    decision: Decision
    cause: GovernorCause = "ok"
    tool_name: str | None = None
    budget_account: str = "system"
    requested_by: str = "system"
    tenant_id: str = "default"
    cap_dimension: str | None = None
    cap_limit: float | None = None
    observed_value: float | None = None
    detail: str = ""
    occurred_at: datetime | None = None

    @property
    def blocked(self) -> bool:
        return self.decision == "block"

    def subject(self) -> str:
        return f"{GOVERNOR_EVENTS_SUBJECT_PREFIX}.{self.tenant_id}.{self.pack_id}.{self.decision}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "decision": self.decision,
            "cause": self.cause,
            "tool_name": self.tool_name,
            "budget_account": self.budget_account,
            "requested_by": self.requested_by,
            "tenant_id": self.tenant_id,
            "cap_dimension": self.cap_dimension,
            "cap_limit": self.cap_limit,
            "observed_value": self.observed_value,
            "detail": self.detail,
            "occurred_at": (self.occurred_at or datetime.now(tz=timezone.utc)).isoformat(),
        }


def _dec(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(v))


async def record_governor_event(
    conn: asyncpg.Connection,
    event: GovernorEvent,
    *,
    nats_publish: NatsPublish | None = None,
) -> None:
    """Write the event to ``governor_events`` and (best-effort) publish on NATS.

    The DB write is mandatory + transactional with the caller's conn. The NATS
    publish is fire-and-forget telemetry — a broker hiccup must not undo the
    durable record nor change the gate's decision.
    """
    await conn.execute(
        """
        INSERT INTO governor_events (
            pack_id, tool_name, budget_account, requested_by, tenant_id,
            decision, cause, cap_dimension, cap_limit, observed_value, detail
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        event.pack_id,
        event.tool_name,
        event.budget_account,
        event.requested_by,
        event.tenant_id,
        event.decision,
        event.cause,
        event.cap_dimension,
        _dec(event.cap_limit),
        _dec(event.observed_value),
        event.detail,
    )
    if nats_publish is not None:
        try:
            await nats_publish(
                event.subject(),
                json.dumps(event.to_payload()).encode("utf-8"),
            )
        except Exception as exc:  # telemetry only — never block the gate
            logger.warning(
                "governor_event publish failed pack=%s decision=%s: %s",
                event.pack_id, event.decision, exc,
            )
    if event.blocked:
        logger.info(
            "governor.block pack=%s tool=%s cause=%s cap=%s limit=%s observed=%s "
            "account=%s",
            event.pack_id, event.tool_name, event.cause, event.cap_dimension,
            event.cap_limit, event.observed_value, event.budget_account,
        )


async def recent_events(
    conn: asyncpg.Connection,
    *,
    pack_id: str | None = None,
    decision: Decision | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Operator query helper — recent governor decisions, newest first."""
    clauses: list[str] = []
    params: list[Any] = []
    if pack_id is not None:
        params.append(pack_id)
        clauses.append(f"pack_id = ${len(params)}")
    if decision is not None:
        params.append(decision)
        clauses.append(f"decision = ${len(params)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT pack_id, tool_name, budget_account, requested_by, tenant_id,
               decision, cause, cap_dimension, cap_limit, observed_value,
               detail, occurred_at
        FROM governor_events{where}
        ORDER BY occurred_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


__all__ = [
    "GOVERNOR_EVENTS_SUBJECT_PREFIX",
    "GovernorEvent",
    "NatsPublish",
    "recent_events",
    "record_governor_event",
]
