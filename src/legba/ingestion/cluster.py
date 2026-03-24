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

# Cognitive architecture imports — lifecycle state machine + confidence scoring
try:
    from legba.shared.lifecycle import check_transition, EventLifecycleStatus
    from legba.shared.confidence import compute_corroboration
    _LIFECYCLE_AVAILABLE = True
except ImportError:
    _LIFECYCLE_AVAILABLE = False

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
# Raised from 0.5 to 0.6 to prevent mega-bucket events from loose entity overlap
_CLUSTER_THRESHOLD = 0.6

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
    max_cluster_size: int = 30,
) -> list[list[int]]:
    """Single-linkage clustering with size cap.

    Returns list of clusters (lists of indices). Clusters are capped at
    max_cluster_size to prevent mega-buckets from high-frequency entities
    like 'Iran' or 'US' absorbing everything.
    """
    # Union-Find
    parent = list(range(n))
    size = [1] * n  # Track cluster sizes

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # Cap: don't merge if result would exceed max_cluster_size
            if size[ra] + size[rb] > max_cluster_size:
                return
            if size[ra] < size[rb]:
                ra, rb = rb, ra  # Merge smaller into larger
            parent[rb] = ra
            size[ra] += size[rb]

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

    def __init__(self, pool: asyncpg.Pool, qdrant_client=None, notifier=None):
        self._pool = pool
        self._qdrant = qdrant_client
        self._notifier = notifier

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
        signal_ids = []
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
                "vector": None,  # filled below if available
            })
            signal_ids.append(str(s["id"]))

        # Fetch vectors from Qdrant if available
        vectors_available = False
        if self._qdrant and signal_ids:
            try:
                from legba.ingestion.storage import SIGNALS_COLLECTION
                points = await self._qdrant.retrieve(
                    collection_name=SIGNALS_COLLECTION,
                    ids=signal_ids,
                    with_vectors=True,
                )
                vec_map = {p.id: p.vector for p in points if p.vector}
                for feat in features:
                    feat["vector"] = vec_map.get(str(feat["id"]))
                vectors_available = sum(1 for f in features if f["vector"]) > len(features) * 0.5
                if vectors_available:
                    logger.debug("Vector clustering: %d/%d signals have vectors",
                                 sum(1 for f in features if f["vector"]), len(features))
            except Exception as e:
                logger.debug("Qdrant vector fetch failed, falling back to keyword similarity: %s", e)

        # Build similarity function
        def sim_fn(i: int, j: int) -> float:
            a, b = features[i], features[j]

            # Vector similarity (primary if available)
            if vectors_available and a["vector"] and b["vector"]:
                # Cosine similarity between embedding vectors
                va, vb = a["vector"], b["vector"]
                dot = sum(x * y for x, y in zip(va, vb))
                norm_a = sum(x * x for x in va) ** 0.5
                norm_b = sum(x * x for x in vb) ** 0.5
                cosine = dot / (norm_a * norm_b) if norm_a > 0 and norm_b > 0 else 0.0

                # Temporal proximity
                if a["timestamp"] and b["timestamp"]:
                    hours_apart = abs((a["timestamp"] - b["timestamp"]).total_seconds()) / 3600
                    temporal = max(0.0, 1.0 - hours_apart / 48.0)
                else:
                    temporal = 0.5

                category_match = 1.0 if a["category"] == b["category"] else 0.0

                # Vector-weighted: 60% cosine, 20% temporal, 20% category
                return 0.6 * cosine + 0.2 * temporal + 0.2 * category_match

            # Fallback: keyword-based similarity
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
                # Singleton → auto-promote if from structured source AND substantive
                # Skip routine weather (environment category = Moderate/Minor NWS alerts)
                feat = cluster_feats[0]
                if feat["source_name"] in _STRUCTURED_SOURCES and feat["category"] != "environment":
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
        # Collect cluster-level features including geo
        all_actors = set()
        all_locations = set()
        all_geo_countries = set()
        all_geo_coords = []
        seen_coord_keys = set()
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
            for gc in (data.get("geo_countries") or []):
                if gc:
                    all_geo_countries.add(gc)
            for coord in (data.get("geo_coordinates") or []):
                if coord and coord.get("lat") is not None:
                    key = f"{coord.get('lat'):.4f},{coord.get('lon'):.4f}"
                    if key not in seen_coord_keys:
                        seen_coord_keys.add(key)
                        all_geo_coords.append(coord)
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
            all_geo_countries, all_geo_coords,
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
            old_data_actors_before = set(old_data.get("actors") or [])
            merged_actors = list(old_data_actors_before | new_actors)
            merged_locations = list(set(old_data.get("locations") or []) | new_locations)

            # Extend geo from new signals
            existing_countries = set(old_data.get("geo_countries") or [])
            existing_coords = old_data.get("geo_coordinates") or []
            seen_keys = {f"{c.get('lat',0):.4f},{c.get('lon',0):.4f}" for c in existing_coords if c}
            for f in feats:
                fd = f["data"]
                for gc in (fd.get("geo_countries") or []):
                    if gc:
                        existing_countries.add(gc)
                for coord in (fd.get("geo_coordinates") or []):
                    if coord and coord.get("lat") is not None:
                        key = f"{coord.get('lat'):.4f},{coord.get('lon'):.4f}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            existing_coords.append(coord)

            # Extend time window
            time_end = max(timestamps) if timestamps else None

            # Confidence boost with reinforcement (capped)
            confidence = min(_REINFORCED_CONFIDENCE_CAP, 0.4 + 0.05 * new_count)

            # Update the JSONB data
            old_data["actors"] = merged_actors
            old_data["locations"] = merged_locations
            old_data["geo_countries"] = list(existing_countries)
            old_data["geo_coordinates"] = existing_coords[:10]
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

            # Cognitive architecture: check lifecycle transition after reinforcement
            if _LIFECYCLE_AVAILABLE:
                try:
                    event_row = await self._pool.fetchrow(
                        "SELECT signal_count, confidence, lifecycle_status, "
                        "created_at, time_end AS last_signal_at "
                        "FROM events WHERE id = $1",
                        event_id,
                    )
                    if event_row and event_row.get("lifecycle_status"):
                        event_dict = {
                            "signal_count": event_row["signal_count"],
                            "confidence": event_row["confidence"],
                            "lifecycle_status": event_row["lifecycle_status"],
                            "created_at": event_row["created_at"],
                            "last_signal_at": event_row["last_signal_at"],
                        }
                        new_status = check_transition(event_dict)
                        if new_status:
                            await self._pool.execute(
                                "UPDATE events SET lifecycle_status = $1, "
                                "lifecycle_changed_at = NOW() WHERE id = $2",
                                new_status.value, event_id,
                            )
                            logger.info(
                                "Event %s lifecycle: %s -> %s",
                                str(event_id)[:8],
                                event_row["lifecycle_status"],
                                new_status.value,
                            )
                except Exception as e:
                    logger.debug("Lifecycle transition check failed (non-fatal): %s", e)

            # Cognitive architecture: update corroboration after reinforcement
            await self._update_corroboration(event_id)

            # Event graph: link newly added actors to event vertex
            try:
                old_actor_set = set(old_data_actors_before) if old_data_actors_before else set()
                truly_new_actors = new_actors - old_actor_set
                if truly_new_actors:
                    from legba.shared.graph_events import link_entity_to_event
                    event_title = old_data.get("title", "")
                    for actor in list(truly_new_actors)[:10]:
                        try:
                            await link_entity_to_event(
                                self._pool, 'legba_graph',
                                entity_name=actor,
                                event_title=event_title,
                                role='actor',
                                confidence=0.6,
                            )
                        except Exception:
                            pass
            except Exception:
                pass

            # Check reinforcement thresholds
            crossed = {t for t in _REINFORCEMENT_THRESHOLDS if old_count < t <= new_count}
            if crossed:
                logger.info(
                    "Event %s reinforced to %d signals (crossed: %s): %s",
                    str(event_id)[:8], new_count, crossed,
                    old_data.get("title", ""),
                )

            # Auto-link to active situations (merged entity set)
            category = old_data.get("category", "")
            await self._auto_link_situations(event_id, new_actors, new_locations, category)

            # Check watchlist triggers on reinforcement
            await self._check_watchlist_triggers(
                event_id, old_data.get("title", ""), category,
                new_actors, new_locations,
            )

            # Notify on threshold crossings
            if self._notifier and crossed:
                for t in crossed:
                    await self._notifier.notify_event_reinforced(
                        event_id=event_id,
                        title=old_data.get("title", ""),
                        category=category,
                        severity=old_data.get("severity", "medium"),
                        signal_count=new_count,
                        threshold_crossed=t,
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
        all_geo_countries: set[str],
        all_geo_coords: list[dict],
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
                "geo_countries": list(all_geo_countries),
                "geo_coordinates": all_geo_coords[:10],  # cap at 10 coords per event
                "confidence": confidence,
                "signal_count": signal_count,
                "source_method": "auto",
                "source_cycle": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            # Cognitive architecture: new events start as EMERGING
            try:
                await self._pool.execute(
                    """
                    INSERT INTO events (id, data, title, summary, category,
                                                event_type, severity, time_start, time_end,
                                                confidence, signal_count, source_method,
                                                lifecycle_status, lifecycle_changed_at,
                                                created_at, updated_at)
                    VALUES ($1, $2, $3, '', $4, 'incident', 'medium', $5, $6, $7, $8, 'auto',
                            'emerging', NOW(), NOW(), NOW())
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
            except Exception:
                # Fallback if lifecycle columns don't exist yet
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

            # Cognitive architecture: compute corroboration from independent sources
            await self._update_corroboration(event_id)

            # Event graph: create Event vertex and link actors in AGE graph
            try:
                from legba.shared.graph_events import upsert_event_vertex, link_entity_to_event
                await upsert_event_vertex(
                    self._pool, 'legba_graph',
                    event_id=str(event_id),
                    event_title=title,
                    category=category,
                    lifecycle_status='emerging',
                )
                for actor in list(all_actors)[:10]:
                    try:
                        await link_entity_to_event(
                            self._pool, 'legba_graph',
                            entity_name=actor,
                            event_title=title,
                            role='actor',
                            confidence=0.6,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("Event graph vertex creation failed: %s", e)

            # Auto-link to active situations
            await self._auto_link_situations(event_id, all_actors, all_locations, category)

            # Check watchlist triggers for new events
            await self._check_watchlist_triggers(event_id, title, category, all_actors, all_locations)

            # Notify on event creation
            if self._notifier:
                await self._notifier.notify_event_created(
                    event_id=event_id, title=title, category=category,
                    signal_count=signal_count, source_method="auto",
                )

            return 1
        except Exception as e:
            logger.warning("create_event_from_cluster failed: %s", e)
            return 0

    async def _auto_link_situations(
        self,
        event_id: UUID,
        actors: set[str] | list[str],
        locations: set[str] | list[str],
        category: str,
    ) -> None:
        """Link an event to active situations that share entities.

        Cognitive architecture: high-frequency entities (appearing in >10% of
        recent events) are excluded from matching to prevent over-linking.
        """
        try:
            rows = await self._pool.fetch(
                "SELECT id, data FROM situations WHERE status = 'active'"
            )
            if not rows:
                return

            event_entities = {e.lower() for e in list(actors) + list(locations) if e and len(e) >= 3}
            if not event_entities:
                return

            # Filter out high-frequency entities to prevent spurious situation links.
            # Entities appearing in >10% of recent events are too generic for matching.
            high_freq = set()
            try:
                total_events = await self._pool.fetchval(
                    "SELECT count(*) FROM events WHERE created_at > NOW() - interval '7 days'"
                )
                if total_events and total_events > 20:
                    freq_rows = await self._pool.fetch("""
                        SELECT LOWER(unnest(string_to_array(data->>'actors', ','))) as actor,
                               count(*) as cnt
                        FROM events WHERE created_at > NOW() - interval '7 days'
                        GROUP BY 1 HAVING count(*) > $1
                    """, int(total_events * 0.1))
                    high_freq = {r['actor'].strip() for r in freq_rows if r['actor']}
                    if high_freq:
                        logger.debug(
                            "Situation auto-link: excluding %d high-freq entities: %s",
                            len(high_freq), list(high_freq)[:5],
                        )
            except Exception as e:
                logger.debug("High-freq entity filter failed (non-fatal): %s", e)

            # Remove high-frequency entities from the matching set
            event_entities -= high_freq
            if not event_entities:
                return

            for row in rows:
                data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
                key_entities = data.get("key_entities") or []
                if not key_entities:
                    continue

                # Case-insensitive substring match: does any event entity appear
                # in a situation entity or vice versa?
                matched = False
                for ke in key_entities:
                    ke_low = ke.lower()
                    for ee in event_entities:
                        if ee in ke_low or ke_low in ee:
                            matched = True
                            break
                    if matched:
                        break

                if matched:
                    await self._pool.execute(
                        "INSERT INTO situation_events (situation_id, event_id) "
                        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        row["id"], event_id,
                    )
                    logger.info(
                        "Auto-linked event %s to situation %s",
                        str(event_id)[:8], str(row["id"])[:8],
                    )
        except Exception as e:
            logger.debug("auto_link_situations error: %s", e)

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

            # Cognitive architecture: singleton events also start as EMERGING
            try:
                await self._pool.execute(
                    """
                    INSERT INTO events (id, data, title, summary, category,
                                                event_type, severity, time_start, time_end,
                                                confidence, signal_count, source_method,
                                                lifecycle_status, lifecycle_changed_at,
                                                created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, 'incident', 'medium', $6, $7, $8, 1, 'auto',
                            'emerging', NOW(), NOW(), NOW())
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
            except Exception:
                # Fallback if lifecycle columns don't exist yet
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

            # Event graph: create Event vertex and link actors in AGE graph
            actors = data.get("actors") or []
            locations = data.get("locations") or []
            try:
                from legba.shared.graph_events import upsert_event_vertex, link_entity_to_event
                await upsert_event_vertex(
                    self._pool, 'legba_graph',
                    event_id=str(event_id),
                    event_title=feat["title"],
                    category=feat["category"],
                    lifecycle_status='emerging',
                )
                for actor in actors[:10]:
                    try:
                        await link_entity_to_event(
                            self._pool, 'legba_graph',
                            entity_name=actor,
                            event_title=feat["title"],
                            role='actor',
                            confidence=0.6,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("Singleton event graph vertex creation failed: %s", e)

            # Auto-link to active situations
            await self._auto_link_situations(event_id, set(actors), set(locations), feat["category"])

            # Check watchlist triggers for singleton events
            await self._check_watchlist_triggers(
                event_id, feat["title"], feat["category"],
                set(actors), set(locations),
            )

            return 1
        except Exception as e:
            logger.warning("create_singleton_event failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Cognitive architecture helpers
    # ------------------------------------------------------------------

    async def _update_corroboration(self, event_id: UUID) -> None:
        """Count independent sources for an event and store corroboration score.

        Cognitive architecture: corroboration is a key confidence component.
        Multiple independent sources reporting the same event increases reliability.
        """
        if not _LIFECYCLE_AVAILABLE:
            return
        try:
            source_count = await self._pool.fetchval("""
                SELECT count(DISTINCT s.source_id) FROM signals s
                JOIN signal_event_links sel ON sel.signal_id = s.id
                WHERE sel.event_id = $1 AND s.source_id IS NOT NULL
            """, event_id)
            corroboration = compute_corroboration(source_count or 0)

            # Store corroboration in the event's data JSONB (nested jsonb_set)
            await self._pool.execute("""
                UPDATE events SET
                    data = jsonb_set(
                        jsonb_set(data, '{corroboration}', to_jsonb($2::float)),
                        '{independent_source_count}', to_jsonb($3::int)
                    )
                WHERE id = $1
            """, event_id, corroboration, source_count or 0)
        except Exception as e:
            logger.debug("Corroboration update failed (non-fatal): %s", e)

    async def _check_watchlist_triggers(
        self,
        event_id: UUID,
        title: str,
        category: str,
        actors: set[str] | list[str],
        locations: set[str] | list[str],
    ) -> None:
        """Check if a new/reinforced event triggers any active watchlist patterns.

        This is the ingestion-side watchlist check — runs during clustering so
        triggers fire when events are created or reinforced, not just when the
        agent processes them. Fixes the gap where 0 triggers fired in 69 cycles
        because the agent-side check only runs when the agent explicitly creates
        events via event_create tool.
        """
        try:
            rows = await self._pool.fetch(
                "SELECT id, data, name, priority FROM watchlist WHERE active = true"
            )
            if not rows:
                return

            event_text = title.lower()
            event_actors_lower = {a.lower() for a in actors if a}
            event_locations_lower = {loc.lower() for loc in locations if loc}

            for row in rows:
                raw = row["data"]
                data = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}

                matched_criteria = []
                failed = False

                # Entity matching (AND across criteria types, OR within)
                watch_entities = [e.lower() for e in data.get("entities", [])]
                if watch_entities:
                    hit = next(
                        (we for we in watch_entities
                         if we in event_actors_lower or we in event_locations_lower or we in event_text),
                        None,
                    )
                    if hit:
                        matched_criteria.append(f"entity:{hit}")
                    else:
                        failed = True

                # Keyword matching
                if not failed:
                    watch_keywords = [k.lower() for k in data.get("keywords", [])]
                    if watch_keywords:
                        hit = next((kw for kw in watch_keywords if kw in event_text), None)
                        if hit:
                            matched_criteria.append(f"keyword:{hit}")
                        else:
                            failed = True

                # Category matching
                if not failed:
                    watch_categories = [c.lower() for c in data.get("categories", [])]
                    if watch_categories:
                        if category.lower() in watch_categories:
                            matched_criteria.append(f"category:{category}")
                        else:
                            failed = True

                # Region matching
                if not failed:
                    watch_regions = [r.lower() for r in data.get("regions", [])]
                    if watch_regions:
                        all_locs = event_locations_lower
                        hit = next(
                            (wr for wr in watch_regions if wr in all_locs or wr in event_text),
                            None,
                        )
                        if hit:
                            matched_criteria.append(f"region:{hit}")
                        else:
                            failed = True

                # Structured query evaluation (if keyword matching didn't fire)
                if failed or not matched_criteria:
                    structured = data.get("structured_query")
                    if structured:
                        try:
                            from legba.shared.watchlist_eval import evaluate_structured_query
                            sq = structured if isinstance(structured, dict) else json.loads(structured)
                            event_dict = {
                                "title": title,
                                "category": category,
                                "actors": list(actors),
                                "locations": list(locations),
                                "severity": "medium",  # default; not available at cluster time
                            }
                            result = evaluate_structured_query(sq, event_dict)
                            if result and result.get("matched"):
                                matched_criteria = result.get("reasons", [])
                                failed = False
                        except Exception as sq_err:
                            logger.debug("Structured query eval failed: %s", sq_err)

                if not failed and matched_criteria:
                    # Record the trigger
                    await self._pool.execute(
                        "INSERT INTO watch_triggers "
                        "(id, watch_id, signal_id, watch_name, event_title, match_reasons, priority) "
                        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) "
                        "ON CONFLICT DO NOTHING",
                        uuid4(), row["id"], event_id,
                        row["name"], title,
                        json.dumps(matched_criteria), row["priority"],
                    )
                    await self._pool.execute(
                        "UPDATE watchlist SET trigger_count = trigger_count + 1, "
                        "last_triggered_at = NOW() WHERE id = $1",
                        row["id"],
                    )
                    logger.info(
                        "Watchlist trigger: '%s' matched event '%s' (%s)",
                        row["name"], title[:60], matched_criteria,
                    )

                    # Notify via dispatcher if available
                    if self._notifier:
                        await self._notifier.notify_watchlist_trigger(
                            watch_name=row["name"],
                            signal_title=title,
                            priority=row["priority"],
                            match_reasons=matched_criteria,
                        )
        except Exception as e:
            logger.debug("Watchlist trigger check failed (non-fatal): %s", e)
