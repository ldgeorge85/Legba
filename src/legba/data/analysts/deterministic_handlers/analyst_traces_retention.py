# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``analyst_traces_retention`` sub-handler — TTL purge of aged analyst traces.

S-6 (disk-creep remediation). ``analyst_traces`` is the per-run debug/telemetry
receipt table (one row per analyst run: prompt, llm_calls, tool_calls,
output_payload, receipt chain). It had NO retention and grew forever
(~470MB / 164k rows, +5.4k/day), feeding the disk pressure that breached
OpenSearch's watermark. The answer MIRRORS the ``signals_retention`` precedent
(migration 0036): a scheduled TTL PURGE on the deterministic-analyst
maintenance cadence, NOT a range-partition.

WHAT IT DOES (one transaction per batch, idempotent, degrade-not-drop):
  * Selects traces whose ``run_started_at`` is older than ``ttl_days``,
    oldest-first, ``LIMIT batch_limit`` per run (bounds lock time at scale;
    supported by the ``run_started_at`` index from migration 0101).
  * DELETEs the trace rows. The two FK children are DB-handled, so the purge
    never orphans a row and needs no explicit child cleanup:
      - ``analyst_critiques.trace_id`` → ON DELETE CASCADE (an aged trace's
        linked critique is dropped WITH it, in the same transaction),
      - ``output_dead_letter.run_id``  → ON DELETE SET NULL (the DLQ row is
        preserved; only its ``run_id`` back-pointer is nulled).
    We COUNT the cascading critiques before the delete so the summary is
    honest about that side-effect.

NOT touched (by design): the receipt-chain ``prev_receipt_hash`` of the oldest
SURVIVING trace will point at a now-deleted ancestor (log-rotation semantics).
The audit checkpointer signs only the current chain HEAD (latest row per
analyst), so nothing does a mandatory full-history walk that hard-fails on a
missing ancestor — the same "lineage terminates at a missing row" trade-off
``signals_retention`` accepts for ``derived_from``.

CADENCE-HEALTH SAFETY: the System Status / telemetry read
(``runtime_telemetry_api``) aggregates a 7-DAY window over ``analyst_traces``,
and the liveness watchdog reads ``max(run_started_at)`` per analyst. A TTL that
purges inside that window would blind the health surfaces, so ``ttl_days`` must
stay WELL ABOVE 7 (30+ recommended). The job ships DISABLED (``ttl_days <= 0``)
so it is inert until an operator sets a deliberately generous positive TTL.

Output ``data`` keys:
    traces_purged       int — analyst_traces rows deleted this run
    critiques_cascaded  int — analyst_critiques rows CASCADE-deleted with them
    ttl_days            int — the effective TTL (0 = disabled)
"""

from __future__ import annotations

import os
import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "analyst_traces_retention"

#: Disabled by default — ship inert, operator opts in with a positive TTL.
#: Must stay well above the 7-day cadence-health window (see module docstring).
_DEFAULT_TTL_DAYS = 0
_DEFAULT_BATCH = 5_000


def _row_count(status: str | None) -> int:
    """Parse the trailing integer from an asyncpg command tag (``DELETE 7``)."""
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


async def _purge(pool: Any, *, ttl_days: int, batch_limit: int) -> dict[str, int]:
    """Purge one batch of aged traces (+ their CASCADE-linked critiques).

    All work for a batch runs in ONE transaction: the count of cascading
    critiques and the trace delete are read/applied atomically so the honest
    summary matches exactly what the DB removed.
    """
    traces_purged = 0
    critiques_cascaded = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            ids = [
                r["run_id"]
                for r in await conn.fetch(
                    """
                    SELECT run_id
                      FROM analyst_traces
                     WHERE run_started_at < NOW() - ($1::int * INTERVAL '1 day')
                     ORDER BY run_started_at ASC
                     LIMIT $2
                    """,
                    ttl_days,
                    batch_limit,
                )
            ]
            if not ids:
                return {"traces_purged": 0, "critiques_cascaded": 0}

            # Honest disclosure of the ON DELETE CASCADE side-effect: count the
            # linked critiques the trace delete is about to drop (the DB does
            # the delete itself via the FK — we only count).
            critiques_cascaded = int(
                await conn.fetchval(
                    "SELECT count(*) FROM analyst_critiques "
                    "WHERE trace_id = ANY($1::uuid[])",
                    ids,
                )
                or 0
            )
            # output_dead_letter.run_id → ON DELETE SET NULL (rows preserved);
            # no explicit cleanup needed.
            traces_purged = _row_count(
                await conn.execute(
                    "DELETE FROM analyst_traces WHERE run_id = ANY($1::uuid[])",
                    ids,
                )
            )

    return {"traces_purged": traces_purged, "critiques_cascaded": critiques_cascaded}


def _build_finding(counters: Mapping[str, int], *, ttl_days: int) -> FindingPayload:
    tp = counters.get("traces_purged", 0)
    if ttl_days <= 0:
        title = "Analyst-traces retention: disabled (ttl_days<=0) — no purge"
    else:
        title = (
            f"Analyst-traces retention: purged {tp} trace(s) older than {ttl_days}d "
            f"({counters.get('critiques_cascaded', 0)} linked critiques cascaded)"
        )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "analyst_traces_retention"]
    if tp:
        tags.append("traces_purged")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, "ttl_days": ttl_days, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "all aged traces"). ``deps is None`` (unit
    path) or ``ttl_days <= 0`` (default) yields a zeroed, no-purge run.
    """
    counters: dict[str, int] = {"traces_purged": 0, "critiques_cascaded": 0}
    # TTL resolution: run options first (tests / explicit invocations), then the
    # env opt-in. Cadence fires carry ONLY ``sub_handler`` in options (the actor
    # injects nothing else) and the descriptor schema forbids a method.options
    # block — so the ENV var is the operator's real opt-in switch (the same
    # flag-driven pattern as the other gated behaviors). 0/unset = disabled.
    # NB: signals_retention has the same options-only read — same-class gap,
    # tracked for the same env treatment.
    raw_ttl = options.get("ttl_days")
    if raw_ttl is None:
        raw_ttl = os.getenv("LEGBA_ANALYST_TRACES_TTL_DAYS", "").strip() or _DEFAULT_TTL_DAYS
    ttl_days = int(raw_ttl)
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None and ttl_days > 0:
        batch_limit = int(options.get("batch_limit", _DEFAULT_BATCH))
        try:
            counters = await _purge(pool, ttl_days=ttl_days, batch_limit=batch_limit)
        except Exception as exc:
            logger.warning("analyst_traces_retention.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters, ttl_days=ttl_days),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
