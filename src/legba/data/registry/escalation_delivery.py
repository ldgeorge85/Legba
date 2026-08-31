# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""escalation-delivery route models + PURE reducer (no substrate).

Extracted from ``v3_api`` (H3, module-size gate — the escalation-delivery
route was the smallest self-contained route-helper cluster left in that file:
four response models plus two pure functions, used by exactly ONE route
(``GET /system/escalations``) and nothing else in the module. Mirrors the
``scorecard_reconcile`` precedent (B0-5 / audit W6) byte-for-byte: the names
below are re-imported and ALIASED back to their historical private spellings
in ``v3_api`` (``escalation_delivery_row as _escalation_delivery_row`` etc.),
so every call site — and the existing ``test_v3_escalations.py`` suite, which
imports these names straight off ``legba.data.registry.v3_api`` — stays
byte-identical. This is a no-behavior-change refactor.

Everything here is a PURE function of already-fetched, plain-Python shapes —
no asyncpg, no ORM, no I/O — so it is unit-testable without a substrate and
carries no import weight beyond pydantic + stdlib.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "EscalationDeliveriesResponse",
    "EscalationDeliveryRow",
    "EscalationDeliverySummary",
    "EscalationNonDelivery",
    "build_escalations_response",
    "escalation_delivery_row",
]


class EscalationDeliveryRow(BaseModel):
    """One ``alert_sink_deliveries`` audit row — a single escalation delivery.

    Mirrors the repurposed per-delivery audit table (migration 0061) that the
    ``ChannelEmitter`` escalate/incident edge writes one row into per emit. This
    is the human-visible answer to "who got alerted, and did it land?" — the
    delivery record that today lands durably in Postgres but is rendered NOWHERE
    (audit finding C3).

    ``status`` is the HONEST publish outcome, NOT a claimed success:

      * ``delivered``    — the NATS/webhook publish confirmed (``delivered_at``
        is stamped).
      * ``failed``       — a delivery attempt raised / permanently failed
        (``error_message`` carries the cause, e.g. pushover 552; no
        ``delivered_at``).
      * ``logged_only``  — the emit went NOWHERE: no publisher was wired, so the
        alert was logged and dropped (the silent-loss case the C3 edge exists to
        surface).
      * ``retrying``     — an in-flight attempt from the dormant alert-output-kind
        retry path (not counted as a non-delivery — it may still land).

    ``target_id`` / ``severity`` / ``effective_confidence`` are the channel-emit
    honesty columns (the country, the resolved severity, the verify-FOLDED
    confidence the escalate gate crossed); they read NULL for legacy
    alert-output-kind rows.
    """
    id: str
    alert_row_id: str | None = None
    channel_name: str | None = None
    sink_kind: str
    sink_target: str | None = None
    target_id: str | None = None
    severity: str | None = None
    effective_confidence: float | None = None
    status: str
    error_message: str | None = None
    attempt_number: int = 1
    attempted_at: datetime
    delivered_at: datetime | None = None
    payload_summary: dict[str, Any] = Field(default_factory=dict)


class EscalationNonDelivery(BaseModel):
    """One ``(sink_kind, status)`` non-delivery tally in the window.

    This is EXACTLY the per-``(sink_kind, status)`` breakdown the W1-T3
    integrity-sweep canary watches (``integrity_sweep._delivery_non_deliveries``):
    the two TERMINAL non-delivery statuses (``failed`` / ``logged_only``) counted
    over the last 24h, with one sample error. Surfacing the same signal in the UI
    means a human sees the failure the canary alarms on.
    """
    sink_kind: str
    status: Literal["failed", "logged_only"]
    n: int
    sample_error: str | None = None


class EscalationDeliverySummary(BaseModel):
    """Rollup of escalation deliveries over the last ``window_hours`` (default 24h).

    The window rollup is DELIBERATELY unfiltered — it is the honest global health
    signal (what failed or went nowhere recently) regardless of any status/target
    filter the operator applied to the row list. ``non_delivery`` (=
    ``failed + logged_only``) is the number the canary alarms on; a clean window
    is all-zero (no fabricated activity).
    """
    window_hours: int
    total: int = 0
    delivered: int = 0
    failed: int = 0
    logged_only: int = 0
    retrying: int = 0
    other: int = 0
    # failed + logged_only — the W1-T3 canary's non-delivery count.
    non_delivery: int = 0
    by_sink_status: list[EscalationNonDelivery] = Field(default_factory=list)


class EscalationDeliveriesResponse(BaseModel):
    """The ``GET /system/escalations`` payload: a 24h health summary + the
    recent (newest-first) delivery rows.

    Read-only and honest: an empty table returns a zeroed summary + no rows (a
    first-class "nothing has been escalated" state, never fabricated activity).
    """
    summary: EscalationDeliverySummary
    rows: list[EscalationDeliveryRow] = Field(default_factory=list)


def escalation_delivery_row(r: dict[str, Any]) -> EscalationDeliveryRow:
    """Shape one raw ``alert_sink_deliveries`` row into the response model.

    ``payload_summary`` is JSONB — asyncpg may hand it back as a str (raw) or a
    dict; parse defensively either way. UUID columns (``id`` / ``alert_row_id``)
    are stringified for the wire.
    """
    raw_summary = r.get("payload_summary")
    summary: dict[str, Any]
    if isinstance(raw_summary, str):
        try:
            parsed = json.loads(raw_summary)
        except (ValueError, TypeError):
            parsed = {}
        summary = parsed if isinstance(parsed, dict) else {}
    elif isinstance(raw_summary, dict):
        summary = dict(raw_summary)
    else:
        summary = {}

    alert_row_id = r.get("alert_row_id")
    conf = r.get("effective_confidence")
    return EscalationDeliveryRow(
        id=str(r["id"]),
        alert_row_id=str(alert_row_id) if alert_row_id is not None else None,
        channel_name=r.get("channel_name"),
        sink_kind=str(r.get("sink_kind") or ""),
        sink_target=r.get("sink_target"),
        target_id=r.get("target_id"),
        severity=r.get("severity"),
        effective_confidence=float(conf) if conf is not None else None,
        status=str(r.get("status") or ""),
        error_message=r.get("error_message"),
        attempt_number=int(r.get("attempt_number") or 1),
        attempted_at=r["attempted_at"],
        delivered_at=r.get("delivered_at"),
        payload_summary=summary,
    )


def build_escalations_response(
    delivery_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    *,
    window_hours: int,
) -> EscalationDeliveriesResponse:
    """Pure reducer: (recent rows, 24h grouped tallies) → the response model.

    Split out from the route handler so the shaping + the canary-aligned summary
    are unit-testable without a live substrate. ``summary_rows`` are the
    ``GROUP BY status, sink_kind`` tallies over the window (each carries
    ``status`` / ``sink_kind`` / ``n`` / ``sample_err``); ``delivery_rows`` are
    the recent rows already ordered newest-first by SQL (order is preserved here).
    """
    counts = {"delivered": 0, "failed": 0, "logged_only": 0, "retrying": 0}
    other = 0
    total = 0
    non_delivery_rows: list[EscalationNonDelivery] = []
    for r in summary_rows:
        st = str(r.get("status") or "")
        n = int(r.get("n") or 0)
        total += n
        if st in counts:
            counts[st] += n
        else:
            other += n
        if st in ("failed", "logged_only") and n > 0:
            non_delivery_rows.append(
                EscalationNonDelivery(
                    sink_kind=str(r.get("sink_kind") or ""),
                    status=st,  # type: ignore[arg-type]
                    n=n,
                    sample_error=r.get("sample_err") or r.get("sample_error"),
                )
            )
    # Worst-first: hard 'failed' before 'logged_only', then by volume desc, then
    # sink_kind for a stable order.
    non_delivery_rows.sort(
        key=lambda x: (0 if x.status == "failed" else 1, -x.n, x.sink_kind)
    )

    summary = EscalationDeliverySummary(
        window_hours=window_hours,
        total=total,
        delivered=counts["delivered"],
        failed=counts["failed"],
        logged_only=counts["logged_only"],
        retrying=counts["retrying"],
        other=other,
        non_delivery=counts["failed"] + counts["logged_only"],
        by_sink_status=non_delivery_rows,
    )
    return EscalationDeliveriesResponse(
        summary=summary,
        rows=[escalation_delivery_row(r) for r in delivery_rows],
    )
