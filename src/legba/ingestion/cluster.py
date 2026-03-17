"""Signal-to-event clustering engine.

Deterministic (no LLM). Groups related signals into derived events using
entity overlap, title similarity, temporal proximity, and category matching.
Runs periodically in the ingestion service tick alongside batch entity linking.

Clustering strategy:
  1. Fetch recent unclustered signals (no signal_event_links entry).
  2. Extract features: actors, locations, title words, timestamp.
  3. Score pairwise similarity (entity + title + temporal + category).
  4. Single-linkage clustering with threshold.
  5. Create or merge-into events for clusters of 2+ signals.
  6. Auto-create 1:1 events for singletons from structured sources.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean
from uuid import UUID, uuid4

import asyncpg

from .dedup import _title_words, _jaccard

logger = logging.getLogger(__name__)

# Structured sources that always produce real events (clean typed data).
# Singletons from these sources get auto-promoted to 1:1 events.
_STRUCTURED_SOURCES = frozenset({
    "NWS Active Alerts",
    "USGS Earthquakes 4.5+",
    "USGS Significant Earthquakes",
    "GDACS Alerts",
    "NASA EONET",
    "EMSC Seismology",
    "IFRC Emergencies",
    "ACLED Conflict Events",
})

# Similarity threshold for clustering (single-linkage)
_CLUSTER_THRESHOLD = 0.4

# Max auto-created event confidence
_AUTO_CONFIDENCE_CAP = 0.6
_REINFORCED_CONFIDENCE_CAP = 0.8

# Signal count milestones that trigger reinforcement alerts
_REINFORCEMENT_THRESHOLDS = frozenset({3, 5, 10, 20})


def _entity_set(data: dict) -> set[str]:
    """Extract a lowercase set of actors + locations from signal JSONB data."""
    actors = data.get("actors") or []
    locations = data.get("locations") or []
    if isinstance(actors, str):
        actors = [a.strip() for a in actors.split(",")]
    if isinstance(locations, str):
        locations = [loc.strip() for loc in locations.split(",")]
    return {e.lower() for e in actors + locations if e and len(e) >= 3}


def _similarity(
    a_entities: set[str],
    b_entities: set[str],
    a_words: set[str],
    b_words: set[str],
    a_ts: datetime | None,
    b_ts: datetime | None,
    a_cat: str,
    b_cat: str,
) -> float:
    """Composite similarity between two signals."""
    entity_sim = _jaccard(a_entities, b_entities)
    title_sim = _jaccard(a_words, b_words)

    # Temporal proximity: linear decay over 48h
    if a_ts and b_ts:
        hours_apart = abs((a_ts - b_ts).total_seconds()) / 3600
        temporal_sim = max(0.0, 1.0 - hours_apart / 48.0)
    else:
        temporal_sim = 0.5  # Unknown timestamps: neutral

    category_sim = 1.0 if a_cat == b_cat else 0.0

    return (
        0.3 * entity_sim
        + 0.3 * title_sim
        + 0.2 * temporal_sim
        + 0.2 * category_sim
    )


def _single_linkage_cluster(
    n: int,
    sim_fn,
    threshold: float,
) -> list[list[int]]:
    """Single-linkage clustering. Returns list of clusters (lists of indices)."""
    # Union-Find
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if sim_fn(i, j) >= threshold:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters.setdefault(root, []).append(i)

    return list(clusters.values())


class SignalClusterer:
    """Clusters unclustered signals into derived events."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def cluster(
        self,
        window_hours: int = 6,
        max_signals: int = 500,
    ) -> int:
        """Run one clustering pass. Returns count of events created/updated."""
        signals = await self._fetch_unclustered(window_hours, max_signals)
        if not signals:
            return 0

        # Extract features
        features = []
        for s in signals:
            data = json.loads(s["data"]) if isinstance(s["data"], str) else s["data"]
            features.append({
                "id": s["id"],
                "title": s["title"],
                "category": s["category"],
                "timestamp": s["event_timestamp"],
                "source_name": s.get("source_name", ""),
                "entities": _entity_set(data),
                "words": _title_words(s["title"]),
                "confidence": s.get("confidence", 0.5),
                "data": data,
            })

        # Build similarity function
        def sim_fn(i: int, j: int) -> float:
            a, b = features[i], features[j]
            # Skip if neither has entities — can't cluster on title alone reliably
            if not a["entities"] and not b["entities"]:
                return 0.0
            return _similarity(
                a["entities"], b["entities"],
                a["words"], b["words"],
                a["timestamp"], b["timestamp"],
                a["category"], b["category"],
            )

        # Cluster
        clusters = _single_linkage_cluster(len(features), sim_fn, _CLUSTER_THRESHOLD)

        events_affected = 0

        for cluster_indices in clusters:
            cluster_feats = [features[i] for i in cluster_indices]

            if len(cluster_indices) >= 2:
                # Multi-signal cluster → create or merge event
                events_affected += await self._handle_cluster(cluster_feats)
            else:
                # Singleton → auto-promote if from structured source
                feat = cluster_feats[0]
                if feat["source_name"] in _STRUCTURED_SOURCES:
                    events_affected += await self._create_singleton_event(feat)

        if events_affected:
            logger.info(
                "Signal clusterer: %d events created/updated from %d signals",
                events_affected, len(signals),
            )

        return events_affected

    async def _fetch_unclustered(
        self, window_hours: int, limit: int,
    ) -> list[dict]:
        """Fetch recent signals with no event link, excluding 'other' category."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        try:
            rows = await self._pool.fetch(
                """
                SELECT s.id, s.title, s.data, s.category, s.event_timestamp,
                       s.confidence, src.name AS source_name
                FROM signals s
                LEFT JOIN signal_event_links sel ON s.id = sel.signal_id
                LEFT JOIN sources src ON s.source_id = src.id
                WHERE sel.signal_id IS NULL
                  AND s.created_at > $1
                  AND s.category != 'other'
                ORDER BY s.created_at DESC
                LIMIT $2
                """,
                cutoff, limit,
            )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("Failed to fetch unclustered signals: %s", e)
            return []

    async def _handle_cluster(self, feats: list[dict]) -> int:
        """Create or merge a multi-signal cluster into an event."""
        # Collect cluster-level features
        all_actors = set()
        all_locations = set()
        timestamps = []
        categories = []
        confidences = []

        for f in feats:
            data = f["data"]
            for a in (data.get("actors") or []):
                if a and len(a) >= 3:
                    all_actors.add(a)
            for loc in (data.get("locations") or []):
                if loc and len(loc) >= 3:
                    all_locations.add(loc)
            if f["timestamp"]:
                timestamps.append(f["timestamp"])
            categories.append(f["category"])
            confidences.append(f["confidence"])

        # Check for existing event to merge into
        existing = await self._find_merge_target(
            list(all_actors), list(all_locations),
            min(timestamps) if timestamps else None,
            max(timestamps) if timestamps else None,
            Counter(categories).most_common(1)[0][0],
        )

        if existing:
            return await self._reinforce_event(existing, feats, all_actors, all_locations, timestamps)

        # Create new event
        return await self._create_event_from_cluster(
            feats, all_actors, all_locations, timestamps, categories, confidences,
        )

    async def _find_merge_target(
        self,
        actors: list[str],
        locations: list[str],
        time_start: datetime | None,
        time_end: datetime | None,
        category: str,
    ) -> dict | None:
        """Find an existing event that this cluster should merge into."""
        try:
            conditions = ["1=1"]
            params: list = []
            idx = 1

            if time_start:
                conditions.append(f"(time_end IS NULL OR time_end >= ${idx})")
                params.append(time_start - timedelta(hours=24))
                idx += 1
            if time_end:
                conditions.append(f"(time_start IS NULL OR time_start <= ${idx})")
                params.append(time_end + timedelta(hours=24))
                idx += 1
            if category:
                conditions.append(f"category = ${idx}")
                params.append(category)
                idx += 1

            where = "WHERE " + " AND ".join(conditions)
            rows = await self._pool.fetch(
                f"SELECT id, data, signal_count FROM events {where} "
                f"ORDER BY created_at DESC LIMIT 50",
                *params,
            )

            our_entities = {e.lower() for e in actors + locations if e}
            if not our_entities:
                return None

            for row in rows:
                data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                ev_actors = set((a.lower() for a in (data.get("actors") or []) if a))
                ev_locs = set((loc.lower() for loc in (data.get("locations") or []) if loc))
                ev_entities = ev_actors | ev_locs
                if not ev_entities:
                    continue
                overlap = len(our_entities & ev_entities) / len(our_entities | ev_entities)
                if overlap >= 0.3:
                    return {
                        "id": row["id"],
                        "data": data,
                        "signal_count": row["signal_count"],
                        "overlap": overlap,
                    }
        except Exception as e:
            logger.debug("find_merge_target error: %s", e)
        return None

    async def _reinforce_event(
        self,
        existing: dict,
        feats: list[dict],
        new_actors: set[str],
        new_locations: set[str],
        timestamps: list[datetime],
    ) -> int:
        """Merge new signals into an existing event (reinforcement)."""
        try:
            event_id = existing["id"]
            old_data = existing["data"]
            old_count = existing["signal_count"]
            new_count = old_count + len(feats)

            # Extend actors/locations
            merged_actors = list(set(old_data.get("actors") or []) | new_actors)
            merged_locations = list(set(old_data.get("locations") or []) | new_locations)

            # Extend time window
            time_end = max(timestamps) if timestamps else None

            # Confidence boost with reinforcement (capped)
            confidence = min(_REINFORCED_CONFIDENCE_CAP, 0.4 + 0.05 * new_count)

            # Update the JSONB data
            old_data["actors"] = merged_actors
            old_data["locations"] = merged_locations
            old_data["signal_count"] = new_count
            old_data["confidence"] = confidence

            await self._pool.execute(
                """
                UPDATE events SET
                    data = $2,
                    signal_count = $3,
                    confidence = $4,
                    time_end = GREATEST(time_end, $5),
                    updated_at = NOW()
                WHERE id = $1
                """,
                event_id,
                json.dumps(old_data),
                new_count,
                confidence,
                time_end,
            )

            # Link signals
            for f in feats:
                await self._pool.execute(
                    "INSERT INTO signal_event_links (signal_id, event_id, relevance) "
                    "VALUES ($1, $2, 1.0) ON CONFLICT DO NOTHING",
                    f["id"], event_id,
                )

            # Check reinforcement thresholds
            crossed = {t for t in _REINFORCEMENT_THRESHOLDS if old_count < t <= new_count}
            if crossed:
                logger.info(
                    "Event %s reinforced to %d signals (crossed: %s): %s",
                    str(event_id)[:8], new_count, crossed,
                    old_data.get("title", ""),
                )

            return 1
        except Exception as e:
            logger.warning("reinforce_event failed: %s", e)
            return 0

    async def _create_event_from_cluster(
        self,
        feats: list[dict],
        all_actors: set[str],
        all_locations: set[str],
        timestamps: list[datetime],
        categories: list[str],
        confidences: list[float],
    ) -> int:
        """Create a new derived event from a multi-signal cluster."""
        try:
            event_id = uuid4()

            # Title: use highest-confidence signal's title
            best = max(feats, key=lambda f: f["confidence"])
            title = best["title"]

            # Category: modal
            category = Counter(categories).most_common(1)[0][0]

            # Time window
            time_start = min(timestamps) if timestamps else None
            time_end = max(timestamps) if timestamps else None

            # Confidence: mean, capped
            confidence = min(_AUTO_CONFIDENCE_CAP, mean(confidences) if confidences else 0.5)

            signal_count = len(feats)

            data = {
                "id": str(event_id),
                "title": title,
                "summary": "",
                "category": category,
                "event_type": "incident",
                "severity": "medium",
                "time_start": time_start.isoformat() if time_start else None,
                "time_end": time_end.isoformat() if time_end else None,
                "actors": list(all_actors),
                "locations": list(all_locations),
                "tags": [],
                "geo_countries": [],
                "geo_coordinates": [],
                "confidence": confidence,
                "signal_count": signal_count,
                "source_method": "auto",
                "source_cycle": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            await self._pool.execute(
                """
                INSERT INTO events (id, data, title, summary, category,
                                            event_type, severity, time_start, time_end,
                                            confidence, signal_count, source_method,
                                            created_at, updated_at)
                VALUES ($1, $2, $3, '', $4, 'incident', 'medium', $5, $6, $7, $8, 'auto', NOW(), NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                event_id,
                json.dumps(data),
                title,
                category,
                time_start,
                time_end,
                confidence,
                signal_count,
            )

            # Link signals
            for f in feats:
                await self._pool.execute(
                    "INSERT INTO signal_event_links (signal_id, event_id, relevance) "
                    "VALUES ($1, $2, 1.0) ON CONFLICT DO NOTHING",
                    f["id"], event_id,
                )

            return 1
        except Exception as e:
            logger.warning("create_event_from_cluster failed: %s", e)
            return 0

    async def _create_singleton_event(self, feat: dict) -> int:
        """Create a 1:1 event for a singleton from a structured source."""
        try:
            event_id = uuid4()
            data = feat["data"]

            event_data = {
                "id": str(event_id),
                "title": feat["title"],
                "summary": data.get("summary", ""),
                "category": feat["category"],
                "event_type": "incident",
                "severity": "medium",
                "time_start": feat["timestamp"].isoformat() if feat["timestamp"] else None,
                "time_end": feat["timestamp"].isoformat() if feat["timestamp"] else None,
                "actors": data.get("actors") or [],
                "locations": data.get("locations") or [],
                "tags": data.get("tags") or [],
                "geo_countries": data.get("geo_countries") or [],
                "geo_coordinates": data.get("geo_coordinates") or [],
                "confidence": min(_AUTO_CONFIDENCE_CAP, feat["confidence"]),
                "signal_count": 1,
                "source_method": "auto",
                "source_cycle": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            await self._pool.execute(
                """
                INSERT INTO events (id, data, title, summary, category,
                                            event_type, severity, time_start, time_end,
                                            confidence, signal_count, source_method,
                                            created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, 'incident', 'medium', $6, $7, $8, 1, 'auto', NOW(), NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                event_id,
                json.dumps(event_data),
                feat["title"],
                data.get("summary", ""),
                feat["category"],
                feat["timestamp"],
                feat["timestamp"],
                min(_AUTO_CONFIDENCE_CAP, feat["confidence"]),
            )

            # Link signal to event
            await self._pool.execute(
                "INSERT INTO signal_event_links (signal_id, event_id, relevance) "
                "VALUES ($1, $2, 1.0) ON CONFLICT DO NOTHING",
                feat["id"], event_id,
            )

            return 1
        except Exception as e:
            logger.warning("create_singleton_event failed: %s", e)
            return 0
