"""Automated situation detection — maintenance daemon task.

Finds clusters of events that should be proposed as new situations based on
shared region, category, and entity overlap. Deterministic, no LLM.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger("legba.maintenance.situation_detect")


class SituationDetector:
    """Detect event clusters that should be proposed as situations."""

    def __init__(self, pg_pool: asyncpg.Pool):
        self.pool = pg_pool

    async def detect_situations(self) -> int:
        """Find event clusters that should be proposed as situations.

        Criteria:
        - 3+ events in same region + category within 7 days
        - sharing 2+ entities
        - no existing situation already covers them

        Returns count of proposed situations created.
        """
        proposed = 0

        try:
            # Fetch recent events with their entities and locations
            events = await self._fetch_recent_events()
            if not events:
                return 0

            # Group by (category, primary_region)
            groups = self._group_events(events)

            # For each qualifying group, check entity overlap
            for key, group_events in groups.items():
                if len(group_events) < 3:
                    continue

                category, region = key

                # Find shared entities (must share 2+ across the group)
                shared = self._find_shared_entities(group_events)
                if len(shared) < 2:
                    continue

                # Check if any existing situation already covers this
                covered = await self._check_existing_coverage(
                    shared, region, category,
                )
                if covered:
                    continue

                # Create proposed situation
                created = await self._create_proposed_situation(
                    category, region, shared, group_events,
                )
                if created:
                    proposed += 1

        except Exception as e:
            logger.error("Situation detection failed: %s", e)

        if proposed:
            logger.info("Situation detection: %d new proposals created", proposed)
        return proposed

    async def _fetch_recent_events(self) -> list[dict]:
        """Fetch events from the last 7 days with entity and location data."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        try:
            rows = await self.pool.fetch("""
                SELECT e.id, e.title, e.category, e.data, e.created_at
                FROM events e
                WHERE e.created_at > $1
                  AND e.category != 'other'
                ORDER BY e.created_at DESC
                LIMIT 500
            """, cutoff)

            events = []
            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
                events.append({
                    "id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "created_at": row["created_at"],
                    "actors": data.get("actors") or [],
                    "locations": data.get("locations") or [],
                    "geo_countries": data.get("geo_countries") or [],
                })
            return events
        except Exception as e:
            logger.warning("Failed to fetch recent events: %s", e)
            return []

    def _group_events(
        self, events: list[dict],
    ) -> dict[tuple[str, str], list[dict]]:
        """Group events by (category, primary_region).

        Primary region is the first geo_country or first location.
        """
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)

        for evt in events:
            category = (evt.get("category") or "").lower()
            if not category or category == "other":
                continue

            # Determine primary region
            region = ""
            geo_countries = evt.get("geo_countries") or []
            if geo_countries:
                region = geo_countries[0].lower()
            elif evt.get("locations"):
                region = evt["locations"][0].lower()

            if not region:
                continue

            groups[(category, region)].append(evt)

        return dict(groups)

    def _find_shared_entities(self, events: list[dict]) -> set[str]:
        """Find entities appearing in 2+ events within the group."""
        entity_counts: dict[str, int] = defaultdict(int)

        for evt in events:
            # Collect unique entities for this event
            evt_entities = set()
            for actor in evt.get("actors") or []:
                if actor and len(actor) >= 3:
                    evt_entities.add(actor.lower())
            for loc in evt.get("locations") or []:
                if loc and len(loc) >= 3:
                    evt_entities.add(loc.lower())

            for entity in evt_entities:
                entity_counts[entity] += 1

        return {e for e, count in entity_counts.items() if count >= 2}

    async def _check_existing_coverage(
        self,
        shared_entities: set[str],
        region: str,
        category: str,
    ) -> bool:
        """Check if an existing active situation already covers these entities + region."""
        try:
            rows = await self.pool.fetch(
                "SELECT id, data FROM situations WHERE status IN ('active', 'escalating')",
            )
            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}

                sit_entities = {
                    e.lower() for e in (data.get("key_entities") or []) if e
                }
                sit_regions = {
                    r.lower() for r in (data.get("regions") or []) if r
                }

                # Coverage: situation shares 2+ entities and overlaps on region
                entity_overlap = len(shared_entities & sit_entities)
                region_overlap = region in sit_regions or any(
                    region in sr or sr in region for sr in sit_regions
                )

                if entity_overlap >= 2 and region_overlap:
                    logger.debug(
                        "Situation %s already covers %s/%s (overlap: %d entities)",
                        str(row["id"])[:8], category, region, entity_overlap,
                    )
                    return True
        except Exception as e:
            logger.debug("Coverage check failed: %s", e)

        return False

    async def _create_proposed_situation(
        self,
        category: str,
        region: str,
        shared_entities: set[str],
        events: list[dict],
    ) -> bool:
        """Create a proposed situation for the agent to review.

        Stores with status='proposed' so the agent can promote or dismiss.
        """
        from uuid import uuid4

        now = datetime.now(timezone.utc)
        sit_id = uuid4()

        # Build a descriptive name from category + region + top entities
        top_entities = sorted(shared_entities)[:3]
        entity_str = ", ".join(e.title() for e in top_entities)
        name = f"{category.title()}: {region.title()} — {entity_str}"
        if len(name) > 120:
            name = name[:117] + "..."

        description = (
            f"Auto-detected cluster: {len(events)} events in {category}/{region} "
            f"sharing entities: {', '.join(sorted(shared_entities)[:5])}. "
            f"Detected by maintenance daemon situation detector."
        )

        data = {
            "id": str(sit_id),
            "name": name,
            "description": description,
            "status": "proposed",
            "category": category,
            "key_entities": sorted(shared_entities)[:10],
            "regions": [region],
            "tags": ["auto-detected"],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "event_count": len(events),
            "intensity_score": 0.0,
            "proposed_event_ids": [str(e["id"]) for e in events[:20]],
        }

        try:
            await self.pool.execute(
                "INSERT INTO situations (id, data, name, status, category, "
                "created_at, updated_at, event_count) "
                "VALUES ($1, $2::jsonb, $3, 'proposed', $4, $5, $5, $6) "
                "ON CONFLICT DO NOTHING",
                sit_id, json.dumps(data, default=str), name, category, now,
                len(events),
            )
            logger.info(
                "Proposed situation: %s (%d events, %d shared entities)",
                name[:80], len(events), len(shared_entities),
            )
            return True
        except Exception as e:
            logger.warning("Failed to create proposed situation: %s", e)
            return False
