"""Event lifecycle decay and situation dormancy.

Deterministic state transitions based on signal activity and temporal rules.
No LLM required — purely rule-based.

Existing schema columns used:
  - events: id, data (JSONB), signal_count, created_at, updated_at
  - signal_event_links: signal_id, event_id, created_at
  - situations: id, status, last_event_at, updated_at
  - situation_events: situation_id, event_id

TODO columns needed (not yet in schema):
  - events.lifecycle_status TEXT NOT NULL DEFAULT 'emerging'
    -- Tracks: emerging, developing, active, evolving, resolved, reactivated
  - events.lifecycle_changed_at TIMESTAMPTZ DEFAULT NOW()
    -- When the lifecycle_status last changed
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.lifecycle")


class LifecycleManager:
    """Deterministic event lifecycle transitions and situation decay."""

    def __init__(self, pg_pool: asyncpg.Pool):
        self._pool = pg_pool

    async def event_lifecycle_decay(self) -> int:
        """Walk events and apply deterministic lifecycle transition rules.

        Transition rules:
          - EMERGING with no new signals in 48h -> RESOLVED
          - DEVELOPING with no signals in 72h -> RESOLVED
          - ACTIVE with no signals in 7 days -> RESOLVED
          - ACTIVE with velocity change > 2x -> EVOLVING
          - EVOLVING with velocity stabilized -> ACTIVE
          - RESOLVED with new signal linked -> REACTIVATED -> DEVELOPING

        Returns the number of transitions applied.

        NOTE: Until lifecycle_status and lifecycle_changed_at columns are added
        to the events table, this uses the data JSONB field to track state.
        The data JSONB approach is a working fallback; once the dedicated columns
        exist, the queries should be updated to use them directly.
        """
        transitions = 0
        async with self._pool.acquire() as conn:
            # --- EMERGING -> RESOLVED (no signals in 48h) ---
            # Also advance EMERGING -> DEVELOPING when enough corroboration
            rows = await conn.fetch("""
                SELECT e.id, e.data, e.signal_count,
                       (SELECT MAX(sel.created_at)
                        FROM signal_event_links sel
                        WHERE sel.event_id = e.id) AS last_signal_at
                FROM events e
                WHERE COALESCE(e.lifecycle_status, e.data->>'lifecycle_status', 'emerging') = 'emerging'
            """)
            cutoff_48h = datetime.now(timezone.utc) - timedelta(hours=48)
            for row in rows:
                last_sig = row["last_signal_at"]
                signal_count = row["signal_count"] or 0
                # Advance to developing if enough corroboration
                if signal_count >= 3 and last_sig and last_sig >= cutoff_48h:
                    await self._transition(conn, row["id"], row["data"], "emerging", "developing")
                    transitions += 1
                # Resolve if stale
                elif last_sig is None or last_sig < cutoff_48h:
                    if row["data"] and (isinstance(row["data"], dict) and row["data"].get("lifecycle_status")) or (row["signal_count"] and row["signal_count"] > 0):
                        # Only resolve events that actually had some lifecycle set or signals
                        await self._transition(conn, row["id"], row["data"], "emerging", "resolved")
                        transitions += 1

            # --- DEVELOPING -> RESOLVED (no signals in 72h) ---
            # Also advance DEVELOPING -> ACTIVE when enough signals
            rows = await conn.fetch("""
                SELECT e.id, e.data, e.signal_count,
                       (SELECT MAX(sel.created_at)
                        FROM signal_event_links sel
                        WHERE sel.event_id = e.id) AS last_signal_at
                FROM events e
                WHERE COALESCE(e.lifecycle_status, e.data->>'lifecycle_status') = 'developing'
            """)
            cutoff_72h = datetime.now(timezone.utc) - timedelta(hours=72)
            for row in rows:
                last_sig = row["last_signal_at"]
                signal_count = row["signal_count"] or 0
                if signal_count >= 8 and last_sig and last_sig >= cutoff_72h:
                    await self._transition(conn, row["id"], row["data"], "developing", "active")
                    transitions += 1
                elif last_sig is None or last_sig < cutoff_72h:
                    await self._transition(conn, row["id"], row["data"], "developing", "resolved")
                    transitions += 1

            # --- ACTIVE -> RESOLVED (no signals in 7 days) ---
            # --- ACTIVE -> EVOLVING (velocity change > 2x) ---
            rows = await conn.fetch("""
                SELECT e.id, e.data, e.signal_count,
                       (SELECT MAX(sel.created_at)
                        FROM signal_event_links sel
                        WHERE sel.event_id = e.id) AS last_signal_at,
                       (SELECT COUNT(*) FROM signal_event_links sel
                        WHERE sel.event_id = e.id
                          AND sel.created_at > NOW() - INTERVAL '24 hours') AS recent_signals,
                       (SELECT COUNT(*) FROM signal_event_links sel
                        WHERE sel.event_id = e.id
                          AND sel.created_at > NOW() - INTERVAL '48 hours'
                          AND sel.created_at <= NOW() - INTERVAL '24 hours') AS prev_signals
                FROM events e
                WHERE COALESCE(e.lifecycle_status, e.data->>'lifecycle_status') = 'active'
            """)
            cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
            for row in rows:
                last_sig = row["last_signal_at"]
                recent = row["recent_signals"] or 0
                prev = row["prev_signals"] or 0
                if last_sig is None or last_sig < cutoff_7d:
                    await self._transition(conn, row["id"], row["data"], "active", "resolved")
                    transitions += 1
                elif prev > 0 and recent > 2 * prev and recent >= 3:
                    await self._transition(conn, row["id"], row["data"], "active", "evolving")
                    transitions += 1

            # --- EVOLVING -> ACTIVE (velocity stabilized) ---
            rows = await conn.fetch("""
                SELECT e.id, e.data, e.signal_count,
                       (SELECT MAX(sel.created_at)
                        FROM signal_event_links sel
                        WHERE sel.event_id = e.id) AS last_signal_at,
                       (SELECT COUNT(*) FROM signal_event_links sel
                        WHERE sel.event_id = e.id
                          AND sel.created_at > NOW() - INTERVAL '24 hours') AS recent_signals,
                       (SELECT COUNT(*) FROM signal_event_links sel
                        WHERE sel.event_id = e.id
                          AND sel.created_at > NOW() - INTERVAL '48 hours'
                          AND sel.created_at <= NOW() - INTERVAL '24 hours') AS prev_signals
                FROM events e
                WHERE COALESCE(e.lifecycle_status, e.data->>'lifecycle_status') = 'evolving'
            """)
            for row in rows:
                last_sig = row["last_signal_at"]
                recent = row["recent_signals"] or 0
                prev = row["prev_signals"] or 0
                if last_sig is None or last_sig < cutoff_7d:
                    await self._transition(conn, row["id"], row["data"], "evolving", "resolved")
                    transitions += 1
                elif prev == 0 or recent <= 2 * prev:
                    await self._transition(conn, row["id"], row["data"], "evolving", "active")
                    transitions += 1

            # --- RESOLVED -> DEVELOPING (new signal linked after resolution) ---
            rows = await conn.fetch("""
                SELECT e.id, e.data, e.signal_count,
                       (SELECT MAX(sel.created_at)
                        FROM signal_event_links sel
                        WHERE sel.event_id = e.id) AS last_signal_at
                FROM events e
                WHERE COALESCE(e.lifecycle_status, e.data->>'lifecycle_status') = 'resolved'
            """)
            for row in rows:
                last_sig = row["last_signal_at"]
                lifecycle_changed = None
                if isinstance(row["data"], dict):
                    lifecycle_changed = row["data"].get("lifecycle_changed_at")
                if last_sig and lifecycle_changed:
                    try:
                        changed_dt = datetime.fromisoformat(lifecycle_changed)
                        # Compare timezone-naive for safety (asyncpg may return naive)
                        last_naive = last_sig.replace(tzinfo=None)
                        changed_naive = changed_dt.replace(tzinfo=None)
                        if last_naive > changed_naive:
                            await self._transition(
                                conn, row["id"], row["data"], "resolved", "developing",
                            )
                            transitions += 1
                    except (ValueError, TypeError):
                        pass

        if transitions:
            logger.info("Event lifecycle: %d transitions applied", transitions)
        return transitions

    async def _transition(
        self,
        conn: asyncpg.Connection,
        event_id,
        current_data: dict | str,
        from_status: str,
        to_status: str,
    ) -> None:
        """Apply a lifecycle transition to an event.

        Updates the data JSONB with lifecycle_status and lifecycle_changed_at.
        Once dedicated columns exist, this should UPDATE those columns directly.

        TODO: When lifecycle_status and lifecycle_changed_at columns exist, change to:
            UPDATE events SET lifecycle_status = $2, lifecycle_changed_at = NOW(),
                              updated_at = NOW() WHERE id = $1
        """
        if isinstance(current_data, str):
            try:
                data = json.loads(current_data)
            except (json.JSONDecodeError, TypeError):
                data = {}
        else:
            data = dict(current_data) if current_data else {}

        now = datetime.now(timezone.utc).isoformat()
        data["lifecycle_status"] = to_status
        data["lifecycle_changed_at"] = now

        # Append to transition history
        history = data.get("lifecycle_history", [])
        history.append({
            "from": from_status,
            "to": to_status,
            "at": now,
        })
        data["lifecycle_history"] = history[-20:]  # Keep last 20 transitions

        # Update both the dedicated column AND the data JSONB
        try:
            await conn.execute(
                "UPDATE events SET lifecycle_status = $2, lifecycle_changed_at = NOW(), data = $3, updated_at = NOW() WHERE id = $1",
                event_id, to_status, json.dumps(data),
            )
        except Exception:
            # Fallback if columns don't exist
            await conn.execute(
                "UPDATE events SET data = $2, updated_at = NOW() WHERE id = $1",
                event_id, json.dumps(data),
            )
        logger.debug(
            "Event %s: %s -> %s", event_id, from_status, to_status,
        )

    async def situation_decay(self) -> int:
        """Mark situations as dormant if no linked events in 10 days.

        A situation is dormant when none of its linked events have received
        new signals in 10 days. This replaces the decision_surfacing DAG logic.

        Returns the number of situations marked dormant.
        """
        dormant_count = 0
        async with self._pool.acquire() as conn:
            # Find active situations where the most recent signal on any
            # linked event is older than 10 days
            rows = await conn.fetch("""
                SELECT s.id, s.name
                FROM situations s
                WHERE s.status = 'active'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM situation_events se
                      JOIN signal_event_links sel ON sel.event_id = se.event_id
                      WHERE se.situation_id = s.id
                        AND sel.created_at > NOW() - INTERVAL '10 days'
                  )
                  AND s.created_at < NOW() - INTERVAL '10 days'
            """)

            for row in rows:
                await conn.execute(
                    "UPDATE situations SET status = 'dormant', updated_at = NOW() WHERE id = $1",
                    row["id"],
                )
                dormant_count += 1
                logger.debug("Situation %s (%s) marked dormant", row["id"], row["name"])

        if dormant_count:
            logger.info("Situation decay: %d situations marked dormant", dormant_count)
        return dormant_count
