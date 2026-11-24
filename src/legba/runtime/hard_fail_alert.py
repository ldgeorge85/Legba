# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``retry.hard.strategy: dlq_and_alert`` -- the strategy the schema declared and
the actor never implemented.

THE GAP THIS CLOSES (2026-10-29, consult run bd1a8e8a). ``consult_default``
declared ``dlq_and_alert``; the actor's hard-failure branch handled it exactly
like ``drop`` (record HARD_FAIL, nothing else). A 19-minute, 162-tool-call
consult died on a vLLM 400 and nothing was dead-lettered and nobody was told;
only the journal's narrative noticed, 303 hours later.

What it does, on any hard failure under ``dlq_and_alert``:

  1. writes one ``descriptor_dead_letter`` row (``namespace='analyst_run'``) so
     the failure sits in the operator's DLQ list with the run, the descriptor
     version and the error text;
  2. raises one alert through the ONE alert path
     (:func:`legba.data.alerts.raise_alert.raise_alert`, L-1): the alert log
     records it and the page rule decides the push. No new transport.

Both legs are fail-safe: this runs inside an ``except`` already carrying the
real error, and a broken DLQ write or dead ntfy must never mask it.

A leaf module: ``dapr_actors`` sits under a pinned size ceiling, so the body
lives here and the actor keeps one call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: ``alert_sink_deliveries.channel_name`` / payload ``kind`` for this alert.
ALERT_CHANNEL_ANALYST_HARD_FAIL = "analyst_hard_fail"
DLQ_NAMESPACE = "analyst_run"

def alert_text(
    *, analyst_id: str, version: str, run_id: Any, exc: BaseException
) -> tuple[str, str]:
    err = f"{type(exc).__name__}: {exc}"
    title = f"Analyst hard failure: {analyst_id}"
    body = (
        f"Analyst '{analyst_id}' (version {str(version)[:8]}) run "
        f"{str(run_id)[:8]} died with a hard failure and its descriptor "
        "declares retry.hard.strategy=dlq_and_alert.\n\n"
        f"{err[:1500]}\n\n"
        "A dead-letter row was written (descriptor_dead_letter, namespace "
        "analyst_run); the run's analyst_traces row carries the full receipt."
    )
    return title, body


async def dead_letter_and_alert(
    pg_pool: Any,
    *,
    analyst_id: str,
    version: str,
    run_id: Any,
    actor_id: str,
    exc: BaseException,
    dispatcher: Any | None = None,
) -> dict[str, bool]:
    """Write the DLQ row and push the alert. Never raises; returns which legs landed."""
    landed = {"dlq": False, "alert": False}
    title, body = alert_text(
        analyst_id=analyst_id, version=version, run_id=run_id, exc=exc
    )
    if pg_pool is not None:
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO descriptor_dead_letter "
                    "(actor, namespace, attempted_payload, validation_error) "
                    "VALUES ($1, $2, $3::jsonb, $4::jsonb)",
                    actor_id,
                    DLQ_NAMESPACE,
                    json.dumps(
                        {
                            "analyst_id": analyst_id,
                            "analyst_version": str(version),
                            "run_id": str(run_id),
                        }
                    ),
                    json.dumps(
                        {
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:4000],
                            "strategy": "dlq_and_alert",
                        }
                    ),
                )
            landed["dlq"] = True
        except Exception as dlq_exc:  # noqa: BLE001 -- never mask the run's error
            logger.warning(
                "hard_fail_alert.dlq_write_failed analyst=%s err=%s",
                analyst_id, dlq_exc,
            )
    try:
        # L-1: the ONE alert path. One episode per analyst: a flapping analyst
        # pages once, later failures update the episode (the kind's quiet
        # window closes it after six failure-free hours).
        from ..data.alerts.raise_alert import raise_alert

        await raise_alert(
            pg_pool,
            kind=ALERT_CHANNEL_ANALYST_HARD_FAIL,
            severity="high",
            summary=title,
            detail=body,
            subject_id=analyst_id,
            evidence_refs=[f"analyst_run:{run_id}"],
            raised_by=f"dlq_and_alert:{actor_id}",
            dispatcher=dispatcher,
        )
        landed["alert"] = True
    except Exception as alert_exc:  # noqa: BLE001
        logger.warning(
            "hard_fail_alert.alert_failed analyst=%s err=%s", analyst_id, alert_exc,
        )
    return landed
