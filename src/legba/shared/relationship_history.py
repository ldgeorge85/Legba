"""Event-source relationship changes to TimescaleDB.

Every edge create/update/delete writes an immutable transition record.
Enables temporal reconstruction and trend analysis.

Uses the existing MetricsClient infrastructure -- no new tables needed.
The TimescaleDB metrics table handles arbitrary metric/dimension/value
tuples with timestamps.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level metrics client singleton (lazy-initialized)
_metrics_client = None


async def _get_metrics_client():
    """Get or create a shared MetricsClient instance.

    Lazily connects on first use.  If TimescaleDB is unavailable the
    client's ``available`` flag will be False and writes become no-ops.
    """
    global _metrics_client
    if _metrics_client is None:
        from .metrics import MetricsClient
        _metrics_client = MetricsClient()
        await _metrics_client.connect()
    return _metrics_client


async def record_edge_change(
    source_entity: str,
    target_entity: str,
    rel_type: str,
    action: str,  # "create", "update", "delete"
    weight: float = 0.5,
    confidence: float = 0.5,
    source_cycle: int = 0,
) -> None:
    """Write a relationship transition to TimescaleDB metrics.

    Args:
        source_entity: Name of the source vertex.
        target_entity: Name of the target vertex.
        rel_type: Edge label (e.g. ``'AlliedWith'``, ``'INVOLVED_IN'``).
        action: One of ``'create'``, ``'update'``, ``'delete'``.
        weight: Edge weight at time of change (0.0-1.0).
        confidence: Edge confidence at time of change (0.0-1.0).
        source_cycle: Cycle number that triggered the change (0 = unknown).

    The record is written as:
        metric:    ``"relationship_change"``
        dimension: ``"{action}:{rel_type}:{source_entity}->{target_entity}"``
        value:     ``weight``
    """
    try:
        client = await _get_metrics_client()
        if not client.available:
            return
        dimension = f"{action}:{rel_type}:{source_entity}->{target_entity}"
        await client.write("relationship_change", dimension, weight)
    except Exception as e:
        logger.debug("relationship_history write failed: %s", e)
