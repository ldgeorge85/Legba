# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nexus_decay`` sub-handler — L-203 migration of ``legba.maintenance.nexus_decay``.

Nexuses not evidenced (created_at) in 30 days get confidence decayed by 0.05
per run, floored at 0.1. No LLM, pure SQL.

Output ``data`` keys:
    decayed_count   int — nexuses with confidence reduced
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from ._graph_metrics_sink import write_graph_metric

logger = logging.getLogger(__name__)

_STALE_DAYS = 30
_DECAY_AMOUNT = 0.05
_CONFIDENCE_FLOOR = 0.1


async def _decay_stale_nexuses(pool: Any) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""
            UPDATE nexuses SET
                confidence = GREATEST(confidence - {_DECAY_AMOUNT}, {_CONFIDENCE_FLOOR})
            WHERE confidence > {_CONFIDENCE_FLOOR}
              AND created_at < NOW() - INTERVAL '{_STALE_DAYS} days'
              AND (valid_until IS NULL OR valid_until > NOW())
            """
        )
        return int(result.split()[-1]) if result else 0


def _build_finding(
    *,
    decayed_count: int,
    target_id: str | None,
) -> FindingPayload:
    title = f"Nexus decay: {decayed_count} nexuses confidence-decayed"
    if target_id:
        title = f"{title} for {target_id}"
    tags = ["deterministic", "nexus_decay"]
    if decayed_count:
        tags.append("nexuses_modified")
    return FindingPayload(
        title=title[:2048],
        body=f"decayed_count={decayed_count}"[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "nexus_decay",
            "decayed_count": decayed_count,
        },
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring."""
    decayed = 0
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        try:
            decayed = await _decay_stale_nexuses(pool)
        except Exception as exc:
            logger.warning("nexus_decay.failed err=%s", exc)

    # FIX P2-1: persist the run's decay metric to the graph_metrics sink (the
    # table had no writer). Best-effort — never fails the run.
    await write_graph_metric(
        deps,
        options,
        metric_kind="nexus_decay",
        payload={
            "decayed_count": decayed,
            "target_id": options.get("target_id"),
        },
    )

    finding = _build_finding(
        decayed_count=decayed,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
