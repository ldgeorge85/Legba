"""Notification dispatch for alerts and event reinforcement.

Sends notifications when:
- Watchlist triggers fire on signal ingest
- Event reinforcement crosses thresholds (signal_count = 3, 5, 10, 20)
- New events are created by the clusterer

Channels:
- webhook: HTTP POST to configured URL(s)
- (future: slack, email)

Configuration via environment:
  NOTIFICATION_WEBHOOK_URL — comma-separated webhook URLs
  NOTIFICATION_ENABLED — "true" to enable (default: false)
  NOTIFICATION_MIN_SEVERITY — minimum severity to notify (default: high)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


class NotificationType(str, Enum):
    EVENT_CREATED = "event_created"
    EVENT_REINFORCED = "event_reinforced"
    WATCHLIST_TRIGGER = "watchlist_trigger"


class NotificationDispatcher:
    """Sends notifications to configured webhook endpoints."""

    def __init__(self):
        self._enabled = os.getenv("NOTIFICATION_ENABLED", "false").lower() == "true"
        self._webhook_urls = [
            u.strip() for u in os.getenv("NOTIFICATION_WEBHOOK_URL", "").split(",")
            if u.strip()
        ]
        self._min_severity = os.getenv("NOTIFICATION_MIN_SEVERITY", "high").lower()
        self._client: httpx.AsyncClient | None = None

        # Severity ranking for filtering
        self._severity_rank = {
            "critical": 4, "high": 3, "medium": 2, "low": 1, "routine": 0,
        }
        self._min_rank = self._severity_rank.get(self._min_severity, 3)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10))
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def is_enabled(self) -> bool:
        return self._enabled and len(self._webhook_urls) > 0

    async def notify_event_created(
        self,
        event_id: UUID,
        title: str,
        category: str,
        severity: str = "medium",
        signal_count: int = 1,
        source_method: str = "auto",
    ) -> None:
        """Notify that a new event was created."""
        if not self._should_notify(severity):
            return

        await self._dispatch({
            "type": NotificationType.EVENT_CREATED,
            "event_id": str(event_id),
            "title": title,
            "category": category,
            "severity": severity,
            "signal_count": signal_count,
            "source_method": source_method,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def notify_event_reinforced(
        self,
        event_id: UUID,
        title: str,
        category: str,
        severity: str,
        signal_count: int,
        threshold_crossed: int,
    ) -> None:
        """Notify that an event crossed a signal count threshold."""
        if not self._should_notify(severity):
            return

        await self._dispatch({
            "type": NotificationType.EVENT_REINFORCED,
            "event_id": str(event_id),
            "title": title,
            "category": category,
            "severity": severity,
            "signal_count": signal_count,
            "threshold_crossed": threshold_crossed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def notify_watchlist_trigger(
        self,
        watch_name: str,
        signal_title: str,
        priority: str,
        match_reasons: list,
    ) -> None:
        """Notify that a watchlist pattern was triggered."""
        # Watchlist uses priority not severity — map high→high, critical→critical
        if not self._should_notify(priority):
            return

        await self._dispatch({
            "type": NotificationType.WATCHLIST_TRIGGER,
            "watch_name": watch_name,
            "signal_title": signal_title,
            "priority": priority,
            "match_reasons": match_reasons,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _should_notify(self, severity: str) -> bool:
        """Check if this severity/priority meets the minimum threshold."""
        if not self.is_enabled():
            return False
        rank = self._severity_rank.get(severity.lower(), 2)
        return rank >= self._min_rank

    async def _dispatch(self, payload: dict) -> None:
        """Send payload to all configured webhook URLs."""
        if not self._webhook_urls:
            return

        client = await self._get_client()
        body = json.dumps(payload)

        for url in self._webhook_urls:
            try:
                resp = await client.post(
                    url,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code >= 400:
                    logger.warning("Webhook %s returned %d", url, resp.status_code)
                else:
                    logger.debug("Webhook delivered to %s: %s", url, payload.get("type"))
            except Exception as e:
                logger.warning("Webhook delivery failed to %s: %s", url, e)

    async def log_to_db(self, pool, payload: dict) -> None:
        """Record notification in the database for audit trail."""
        try:
            await pool.execute(
                """
                INSERT INTO notifications (id, type, payload, created_at)
                VALUES (gen_random_uuid(), $1, $2, NOW())
                """,
                payload.get("type", "unknown"),
                json.dumps(payload),
            )
        except Exception:
            pass  # Table may not exist yet — best effort
