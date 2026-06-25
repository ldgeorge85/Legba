# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared ``graph_metrics`` sink for the graph-analysis sub-handlers (FIX P2-1).

The ``structural_balance`` / ``graph_mining`` / ``nexus_decay`` deterministic
handlers each compute structured graph metrics every run, but the
``graph_metrics`` table (migration 0001) had no writer — it stayed at 0 rows.
This module is the single, best-effort writer those three handlers call to
persist ONE typed metric row per run.

Shape (mirrors the table — ``metric_kind`` text, ``computed_at`` now,
``analyst_id`` / ``analyst_version`` provenance, ``payload`` jsonb,
``schema_uri`` defaulted by the column). The writer is **best-effort**: any
failure (no pool, transient DB error) is logged and swallowed so a metrics-sink
hiccup never fails the analyst run — the finding still ships.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    """Best-effort JSON coercion for non-native payload values."""
    return str(value)


async def write_graph_metric(
    deps: Any | None,
    options: Mapping[str, Any],
    *,
    metric_kind: str,
    payload: Mapping[str, Any],
) -> bool:
    """Persist one ``graph_metrics`` row. Returns ``True`` on a write, ``False``
    when there is no pool or the write degraded.

    Best-effort by design: the caller's run already produced its finding; a
    metrics-sink failure must never propagate. ``analyst_id`` /
    ``analyst_version`` are read off the run ``options`` (the deterministic
    dispatcher threads them in), matching how the other side-writing handlers
    stamp provenance.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        return False
    analyst_id = options.get("analyst_id")
    analyst_version = options.get("analyst_version")
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO graph_metrics
                    (metric_kind, analyst_id, analyst_version, payload)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                str(metric_kind),
                str(analyst_id) if analyst_id is not None else None,
                str(analyst_version) if analyst_version is not None else None,
                json.dumps(dict(payload), default=_json_default),
            )
        return True
    except Exception as exc:  # pragma: no cover - best-effort sink
        logger.warning(
            "graph_metrics.sink_failed metric_kind=%s err=%s", metric_kind, exc,
        )
        return False


__all__ = ["write_graph_metric"]
