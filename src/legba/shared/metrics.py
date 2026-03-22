"""Metrics client for TimescaleDB.

Shared by ingestion service and agent for writing time-series metrics.
Grafana reads from the same database.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# TimescaleDB connection
METRICS_DSN = os.getenv(
    "METRICS_DSN",
    "postgresql://legba_metrics:legba_metrics@timescaledb:5432/legba_metrics"
)


class MetricsClient:
    """Async client for writing and querying time-series metrics."""

    def __init__(self, dsn: str = ""):
        self._dsn = dsn or METRICS_DSN
        self._pool = None
        self._available = False

    async def connect(self) -> bool:
        """Connect to TimescaleDB. Returns True if successful."""
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
            await self._ensure_schema()
            self._available = True
            logger.info("Metrics client connected to TimescaleDB")
            return True
        except Exception as e:
            logger.info("TimescaleDB not available (metrics disabled): %s", e)
            self._available = False
            return False

    async def _ensure_schema(self):
        """Create the metrics hypertable if it doesn't exist."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE EXTENSION IF NOT EXISTS timescaledb;

                CREATE TABLE IF NOT EXISTS metrics (
                    time        TIMESTAMPTZ NOT NULL,
                    metric      TEXT NOT NULL,
                    dimension   TEXT NOT NULL,
                    value       DOUBLE PRECISION NOT NULL
                );

                -- Only create hypertable if not already one
                SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);

                CREATE INDEX IF NOT EXISTS idx_metrics_metric_dim
                    ON metrics (metric, dimension, time DESC);
            """)

    @property
    def available(self) -> bool:
        return self._available

    async def write(self, metric: str, dimension: str, value: float,
                    time: datetime | None = None) -> None:
        """Write a single metric data point."""
        if not self._available:
            return
        try:
            ts = time or datetime.now(timezone.utc)
            await self._pool.execute(
                "INSERT INTO metrics (time, metric, dimension, value) VALUES ($1, $2, $3, $4)",
                ts, metric, dimension, value,
            )
        except Exception as e:
            logger.debug("Metrics write failed: %s", e)

    async def write_batch(self, points: list[tuple[str, str, float]],
                          time: datetime | None = None) -> None:
        """Write multiple metrics at the same timestamp."""
        if not self._available or not points:
            return
        try:
            ts = time or datetime.now(timezone.utc)
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO metrics (time, metric, dimension, value) VALUES ($1, $2, $3, $4)",
                    [(ts, m, d, v) for m, d, v in points],
                )
        except Exception as e:
            logger.debug("Metrics batch write failed: %s", e)

    async def query(self, metric: str, dimension: str, hours: int = 24) -> list[dict]:
        """Query metric values over a time range."""
        if not self._available:
            return []
        try:
            rows = await self._pool.fetch(
                "SELECT time, value FROM metrics "
                "WHERE metric = $1 AND dimension = $2 "
                "AND time > NOW() - make_interval(hours => $3) "
                "ORDER BY time",
                metric, dimension, hours,
            )
            return [{"time": r["time"].isoformat(), "value": r["value"]} for r in rows]
        except Exception as e:
            logger.debug("Metrics query failed: %s", e)
            return []

    async def query_aggregate(self, metric: str, dimension: str,
                               hours: int = 168, bucket: str = "1 day") -> list[dict]:
        """Query aggregated metric values (for baselines)."""
        if not self._available:
            return []
        try:
            rows = await self._pool.fetch(
                f"SELECT time_bucket('{bucket}', time) AS bucket, "
                f"avg(value) AS avg_val, sum(value) AS sum_val, count(*) AS samples "
                f"FROM metrics "
                f"WHERE metric = $1 AND dimension = $2 "
                f"AND time > NOW() - make_interval(hours => $3) "
                f"GROUP BY bucket ORDER BY bucket",
                metric, dimension, hours,
            )
            return [
                {"time": r["bucket"].isoformat(), "avg": r["avg_val"],
                 "sum": r["sum_val"], "samples": r["samples"]}
                for r in rows
            ]
        except Exception as e:
            logger.debug("Metrics aggregate query failed: %s", e)
            return []

    async def close(self):
        if self._pool:
            await self._pool.close()
