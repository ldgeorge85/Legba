"""
Metrics Query Tool — query time-series baselines from TimescaleDB.

Lets the agent ask: "what's the baseline conflict rate in Iran?"
or "how many signals per day this week vs last week?"
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


METRICS_QUERY_DEF = ToolDefinition(
    name="metrics_query",
    description="Query time-series metrics for baselines and trends. "
                "Returns historical data points for a metric+dimension over a time range. "
                "Use this to check: 'is this conflict level normal?', 'how does this week compare to last?', "
                "'what's the signal ingestion rate?'",
    parameters=[
        ToolParameter(name="metric", type="string",
                      description="Metric name: conflict_events, signals_stored, cycle_duration_s, etc."),
        ToolParameter(name="dimension", type="string",
                      description="Dimension filter: country:Iran, source:BBC, type:SURVEY, etc."),
        ToolParameter(name="hours", type="number",
                      description="Lookback hours (default 168 = 1 week)",
                      required=False),
        ToolParameter(name="aggregate", type="string",
                      description="Aggregation bucket: '1 hour', '1 day', '1 week' (default '1 day')",
                      required=False),
    ],
)


def register(registry: ToolRegistry) -> None:
    """Register metrics query tool."""

    async def metrics_query_handler(args: dict) -> str:
        metric = args.get("metric", "").strip()
        dimension = args.get("dimension", "").strip()
        if not metric or not dimension:
            return "Error: metric and dimension are required"

        hours = int(args.get("hours", 168))
        aggregate = args.get("aggregate", "1 day")

        try:
            from ....shared.metrics import MetricsClient
            client = MetricsClient()
            if not await client.connect():
                return "Error: TimescaleDB not available"

            if aggregate:
                data = await client.query_aggregate(metric, dimension, hours, aggregate)
            else:
                data = await client.query(metric, dimension, hours)

            await client.close()

            if not data:
                return f"No data found for metric={metric}, dimension={dimension}, hours={hours}"

            return json.dumps({
                "metric": metric,
                "dimension": dimension,
                "hours": hours,
                "aggregate": aggregate,
                "data_points": len(data),
                "data": data,
            }, indent=2, default=str)

        except Exception as e:
            return f"Error querying metrics: {e}"

    registry.register(METRICS_QUERY_DEF, metrics_query_handler)
