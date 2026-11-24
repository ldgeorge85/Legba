# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trigger scan's alerts, raised through the ONE alert path (L-1).

Extracted from ``alert_trigger_scan`` (module-size gate). The scan already
decides, per alert row, whether it is a push this tick (the steady-state
guard, the per-desk cap, the daily page budget, the band batch). This module
turns those rows into :func:`legba.data.alerts.raise_alert.raise_alert` calls:

* each outward payload of the tick (the band moves folded into ONE batch) is
  raised with its own payload, keyed by its alert row, and the page rule
  decides the push;
* each guard-suppressed or budget-deferred row is raised HELD with the scan's
  reason — logged on the alert log, never paged (it never was);
* with no dispatcher wired, every raise is held with that reason (the rows
  stay internal this run, as before).

Never raises (an unknown kind is a programming error and is logged loudly per
row, never allowed to cost the tick's other alerts).
"""
from __future__ import annotations

import logging
from typing import Any, Callable
from uuid import UUID

from . import _band_push_batch

logger = logging.getLogger(__name__)

RAISED_BY = "alert_trigger_scan"


def _hold_reason(cand: Any) -> str:
    data = getattr(cand, "data", None) or {}
    if data.get("guard_suppressed"):
        return f"steady-state guard: {data.get('guard_suppression_reason') or 'suppressed'}"
    if data.get("budget_deferred"):
        return "daily page budget: deferred"
    return "not pushed this tick"


async def _raise_one(pool: Any, dispatcher: Any, **kwargs: Any) -> bool:
    from ...alerts.raise_alert import raise_alert

    try:
        await raise_alert(pool, dispatcher=dispatcher, raised_by=RAISED_BY, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 — one bad row costs only itself
        logger.error(
            "alert_trigger_scan.raise_failed kind=%s err=%s",
            kwargs.get("kind"), exc,
        )
        return False


async def raise_trigger_alerts(
    pool: Any,
    dispatcher: Any,
    *,
    to_fan_out: list[tuple[UUID, Any]],
    held: list[tuple[UUID, Any]],
    sink_payload: Callable[[UUID, Any], Any],
) -> tuple[int, int, int]:
    """Raise this tick's alerts. Returns ``(raised_ok, raised_failed, band_members)``."""
    ok = failed = 0
    band_batched = 0
    no_dispatcher = dispatcher is None
    if no_dispatcher and to_fan_out:
        logger.warning(
            "alert_trigger_scan.fanout_unavailable alerts=%d — no "
            "alert_sink_dispatcher wired (raised held; nothing pushed this run)",
            len(to_fan_out),
        )
    pushes: list[Any] = []
    if to_fan_out:
        try:
            pushes, band_batched = _band_push_batch.group_pushes(
                to_fan_out, sink_payload=sink_payload
            )
        except Exception as exc:  # noqa: BLE001 — shaping must never cost a page
            logger.warning("alert_trigger_scan.band_batch_failed err=%s", exc)
            pushes = []
            for row_id, cand in to_fan_out:
                try:
                    pushes.append(sink_payload(row_id, cand))
                except Exception as exc2:  # noqa: BLE001
                    failed += 1
                    logger.warning(
                        "alert_trigger_scan.fanout_failed alert_row=%s err=%s",
                        row_id, exc2,
                    )
    for payload in pushes:
        members = list(payload.batch_alert_ids) or (
            [payload.alert_row_id] if payload.alert_row_id else []
        )
        anchor = members[0] if members else payload.summary[:60]
        landed = await _raise_one(
            pool, dispatcher,
            kind=payload.kind,
            severity=payload.severity,
            summary=payload.summary,
            detail=payload.detail,
            subject_id=payload.target_id or "",
            dedupe_key=f"{payload.kind}:alert:{anchor}",
            evidence_refs=members,
            payload=payload,
            hold="no alert dispatcher wired this run" if no_dispatcher else None,
        )
        ok, failed = (ok + 1, failed) if landed else (ok, failed + 1)
    for row_id, cand in held:
        await _raise_one(
            pool, dispatcher,
            kind=cand.trigger_class,
            severity=cand.severity,
            summary=cand.title,
            detail=cand.body,
            subject_id=cand.target_id or "",
            dedupe_key=f"{cand.trigger_class}:alert:{row_id}",
            evidence_refs=[str(row_id)],
            hold=_hold_reason(cand),
        )
    return ok, failed, band_batched


__all__ = ["RAISED_BY", "raise_trigger_alerts"]
