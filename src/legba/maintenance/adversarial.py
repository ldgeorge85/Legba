"""Adversarial signal detection — coordinated inauthentic behavior heuristics.

Detects potential information operations via three heuristic methods:
1. Source cluster velocity spike — low-quality sources suddenly converge on an entity
2. Semantic echo detection — suspiciously similar signals from 'independent' sources
3. Source provenance grouping — correlated publishing from shared-provenance sources

No LLM/SLM required — purely SQL + lightweight Python heuristics.

Existing schema columns used:
  - signals: id, title, source_id, data (JSONB), created_at
  - sources: id, name, source_quality_score, geo_origin, data (JSONB -> ownership_type)
  - signal_entity_links: signal_id, entity_id
  - entity_profiles: id, canonical_name
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from uuid import UUID

import asyncpg

logger = logging.getLogger("legba.maintenance.adversarial")


def _title_words(title: str) -> set[str]:
    """Normalize title to a set of content words for Jaccard comparison.

    Lightweight reimplementation to avoid importing from ingestion.dedup
    (which pulls in spaCy-adjacent code). Keeps the adversarial module
    independently testable.
    """
    _STOPWORDS = frozenset({
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
        "has", "had", "have", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "not", "no", "so", "if",
        "as", "its", "it", "he", "she", "they", "them", "their", "his", "her",
        "this", "that", "these", "those", "than", "then", "says", "said",
        "after", "over", "up", "out", "new", "more",
    })
    words = set()
    for w in title.lower().split():
        w = w.strip(".,;:!?\"'()[]{}—–-")
        if w and len(w) > 1 and w not in _STOPWORDS:
            words.add(w)
    return words


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two sets."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union > 0 else 0.0


class AdversarialDetector:
    """Detect potential coordinated inauthentic behavior in signals.

    Three heuristic detections (no ML/SLM needed):
    1. Source cluster velocity spike
    2. Semantic echo detection
    3. Source provenance grouping
    """

    # Detection window parameters
    VELOCITY_WINDOW_HOURS = 6
    VELOCITY_LOW_QUALITY_THRESHOLD = 0.4
    VELOCITY_HIGH_QUALITY_THRESHOLD = 0.7
    VELOCITY_MIN_LOW_SOURCES = 3

    ECHO_WINDOW_HOURS = 6
    ECHO_BUCKET_HOURS = 4
    ECHO_JACCARD_THRESHOLD = 0.6
    ECHO_MIN_CLUSTER_SIZE = 3

    PROVENANCE_WINDOW_HOURS = 12
    PROVENANCE_MIN_GROUP_SIZE = 2
    PROVENANCE_ENTITY_OVERLAP_THRESHOLD = 0.5

    def __init__(self, pg_pool: asyncpg.Pool, qdrant_client=None):
        self.pool = pg_pool
        self.qdrant = qdrant_client

    async def run_all(self) -> dict[str, list[dict]]:
        """Run all adversarial detections and return combined results.

        Returns a dict keyed by detection type with lists of flag dicts.
        """
        results: dict[str, list[dict]] = {}

        velocity_flags = await self.detect_velocity_spikes()
        if velocity_flags:
            results["velocity_spike"] = velocity_flags
            logger.info(
                "Adversarial: %d velocity spike flags detected",
                len(velocity_flags),
            )

        echo_flags = await self.detect_semantic_echoes()
        if echo_flags:
            results["semantic_echo"] = echo_flags
            logger.info(
                "Adversarial: %d semantic echo flags detected",
                len(echo_flags),
            )

        provenance_flags = await self.detect_provenance_clusters()
        if provenance_flags:
            results["provenance_cluster"] = provenance_flags
            logger.info(
                "Adversarial: %d provenance cluster flags detected",
                len(provenance_flags),
            )

        # Persist flags on the signals themselves
        total_flagged = 0
        for flag_type, flags in results.items():
            for flag in flags:
                signal_ids = flag.get("signal_ids", [])
                if signal_ids:
                    count = await self.flag_signals(signal_ids, flag_type, flag)
                    total_flagged += count

        if total_flagged:
            logger.info("Adversarial: flagged %d signals total", total_flagged)

        return results

    # ------------------------------------------------------------------
    # 1. Source cluster velocity spike
    # ------------------------------------------------------------------

    async def detect_velocity_spikes(self) -> list[dict]:
        """Find entities where low-reliability sources suddenly spike.

        Detection: In a 6-hour window, if 3+ sources with source_quality_score < 0.4
        all publish signals mentioning the same entity, AND high-reliability sources
        (score > 0.7) are NOT reporting the same entity in that window, flag it.

        TODO: SLM could assess whether the entity is genuinely newsworthy
        (e.g. a real disaster vs. a manufactured narrative).
        """
        flags: list[dict] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.VELOCITY_WINDOW_HOURS)

        try:
            async with self.pool.acquire() as conn:
                # Find entities mentioned by low-quality sources in the window
                low_quality_rows = await conn.fetch("""
                    SELECT ep.id AS entity_id,
                           ep.canonical_name,
                           COUNT(DISTINCT src.id) AS low_source_count,
                           array_agg(DISTINCT s.id) AS signal_ids,
                           array_agg(DISTINCT src.name) AS source_names
                    FROM signals s
                    JOIN sources src ON s.source_id = src.id
                    JOIN signal_entity_links sel ON sel.signal_id = s.id
                    JOIN entity_profiles ep ON ep.id = sel.entity_id
                    WHERE s.created_at > $1
                      AND src.source_quality_score < $2
                      AND src.source_quality_score > 0
                    GROUP BY ep.id, ep.canonical_name
                    HAVING COUNT(DISTINCT src.id) >= $3
                """, cutoff, self.VELOCITY_LOW_QUALITY_THRESHOLD,
                    self.VELOCITY_MIN_LOW_SOURCES)

                for row in low_quality_rows:
                    entity_id = row["entity_id"]

                    # Check if high-quality sources are also reporting this entity
                    high_count = await conn.fetchval("""
                        SELECT COUNT(DISTINCT src.id)
                        FROM signals s
                        JOIN sources src ON s.source_id = src.id
                        JOIN signal_entity_links sel ON sel.signal_id = s.id
                        WHERE s.created_at > $1
                          AND sel.entity_id = $2
                          AND src.source_quality_score > $3
                    """, cutoff, entity_id, self.VELOCITY_HIGH_QUALITY_THRESHOLD)

                    if (high_count or 0) == 0:
                        # Low-quality spike with no high-quality corroboration
                        signal_ids = [str(sid) for sid in row["signal_ids"]]
                        flags.append({
                            "entity_id": str(entity_id),
                            "entity_name": row["canonical_name"],
                            "low_source_count": row["low_source_count"],
                            "high_source_count": 0,
                            "source_names": list(row["source_names"]),
                            "signal_ids": signal_ids,
                            "severity": "medium" if row["low_source_count"] < 5 else "high",
                            "description": (
                                f"Entity '{row['canonical_name']}' mentioned by "
                                f"{row['low_source_count']} low-quality sources with "
                                f"no high-quality corroboration in {self.VELOCITY_WINDOW_HOURS}h window"
                            ),
                        })

        except Exception as e:
            logger.error("Velocity spike detection failed: %s", e)

        return flags

    # ------------------------------------------------------------------
    # 2. Semantic echo detection
    # ------------------------------------------------------------------

    async def detect_semantic_echoes(self) -> list[dict]:
        """Find suspiciously similar signals from 'independent' sources.

        Detection: If 3+ signals from different sources within a 4-hour window
        have title Jaccard similarity > 0.6, and the sources don't share an
        ownership_type or geo_origin, flag as potential echo.

        If all sources DO share ownership_type or geo_origin, it's less
        suspicious (same news wire / agency republishing).

        TODO: SLM could do deeper semantic comparison beyond title Jaccard,
        detecting paraphrased but structurally identical narratives.
        """
        flags: list[dict] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.ECHO_WINDOW_HOURS)

        try:
            async with self.pool.acquire() as conn:
                # Fetch recent signals with source metadata
                rows = await conn.fetch("""
                    SELECT s.id, s.title, s.source_id, s.created_at,
                           src.name AS source_name,
                           src.geo_origin,
                           src.data->>'ownership_type' AS ownership_type
                    FROM signals s
                    JOIN sources src ON s.source_id = src.id
                    WHERE s.created_at > $1
                    ORDER BY s.created_at
                """, cutoff)

                if len(rows) < self.ECHO_MIN_CLUSTER_SIZE:
                    return flags

                # Bucket signals by time window
                bucket_size = timedelta(hours=self.ECHO_BUCKET_HOURS)
                buckets: dict[int, list[dict]] = defaultdict(list)

                for row in rows:
                    # Bucket key: hours since cutoff divided by bucket size
                    elapsed = (row["created_at"] - cutoff).total_seconds()
                    bucket_key = int(elapsed // bucket_size.total_seconds())
                    buckets[bucket_key].append({
                        "id": str(row["id"]),
                        "title": row["title"],
                        "words": _title_words(row["title"]),
                        "source_id": str(row["source_id"]),
                        "source_name": row["source_name"],
                        "geo_origin": row["geo_origin"] or "",
                        "ownership_type": row["ownership_type"] or "independent",
                    })

                # Within each bucket, find clusters of similar titles
                for bucket_key, signals in buckets.items():
                    if len(signals) < self.ECHO_MIN_CLUSTER_SIZE:
                        continue

                    # Build adjacency: signals with Jaccard > threshold
                    # from different sources
                    adjacency: dict[int, set[int]] = defaultdict(set)
                    for i, j in combinations(range(len(signals)), 2):
                        si, sj = signals[i], signals[j]
                        # Must be from different sources
                        if si["source_id"] == sj["source_id"]:
                            continue
                        sim = _jaccard(si["words"], sj["words"])
                        if sim >= self.ECHO_JACCARD_THRESHOLD:
                            adjacency[i].add(j)
                            adjacency[j].add(i)

                    # Find connected components (clusters of similar signals)
                    visited: set[int] = set()
                    for start in adjacency:
                        if start in visited:
                            continue
                        # BFS to find connected component
                        cluster_indices: set[int] = set()
                        queue = [start]
                        while queue:
                            node = queue.pop(0)
                            if node in cluster_indices:
                                continue
                            cluster_indices.add(node)
                            visited.add(node)
                            for neighbor in adjacency.get(node, set()):
                                if neighbor not in cluster_indices:
                                    queue.append(neighbor)

                        if len(cluster_indices) < self.ECHO_MIN_CLUSTER_SIZE:
                            continue

                        cluster_signals = [signals[i] for i in cluster_indices]
                        unique_sources = {s["source_id"] for s in cluster_signals}
                        if len(unique_sources) < self.ECHO_MIN_CLUSTER_SIZE:
                            continue

                        # Check if sources share provenance (less suspicious if so)
                        ownership_types = {s["ownership_type"] for s in cluster_signals}
                        geo_origins = {s["geo_origin"] for s in cluster_signals if s["geo_origin"]}

                        shared_provenance = (
                            len(ownership_types) == 1 and ownership_types != {"independent"}
                        ) or (
                            len(geo_origins) == 1 and len(geo_origins) > 0
                        )

                        if shared_provenance:
                            # Same wire service or state media group — less suspicious
                            continue

                        signal_ids = [s["id"] for s in cluster_signals]
                        sample_titles = [s["title"][:80] for s in cluster_signals[:3]]
                        flags.append({
                            "signal_ids": signal_ids,
                            "source_count": len(unique_sources),
                            "source_names": list({s["source_name"] for s in cluster_signals}),
                            "ownership_types": list(ownership_types),
                            "geo_origins": list(geo_origins),
                            "sample_titles": sample_titles,
                            "severity": "high" if len(unique_sources) >= 5 else "medium",
                            "description": (
                                f"{len(cluster_signals)} signals with similar titles from "
                                f"{len(unique_sources)} independent sources — potential echo campaign"
                            ),
                        })

        except Exception as e:
            logger.error("Semantic echo detection failed: %s", e)

        return flags

    # ------------------------------------------------------------------
    # 3. Source provenance grouping
    # ------------------------------------------------------------------

    async def detect_provenance_clusters(self) -> list[dict]:
        """Find correlated publishing patterns from sources with shared provenance.

        Detection: Sources with same ownership_type AND geo_origin publishing
        on the same topic within a window, scored by correlation strength.

        This surfaces coordinated state media campaigns or corporate PR pushes
        where multiple outlets with shared ownership publish on the same entity
        simultaneously.

        TODO: SLM could assess whether the content is genuinely coordinated
        vs. natural coverage of a major event from regional outlets.
        """
        flags: list[dict] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.PROVENANCE_WINDOW_HOURS)

        try:
            async with self.pool.acquire() as conn:
                # Find source groups by shared provenance (ownership_type + geo_origin)
                # ownership_type is in JSONB data field, geo_origin is a column
                groups = await conn.fetch("""
                    SELECT src.data->>'ownership_type' AS ownership_type,
                           src.geo_origin,
                           array_agg(DISTINCT src.id) AS source_ids,
                           array_agg(DISTINCT src.name) AS source_names
                    FROM sources src
                    WHERE src.status = 'active'
                      AND src.geo_origin != ''
                      AND src.data->>'ownership_type' IS NOT NULL
                      AND src.data->>'ownership_type' != 'independent'
                    GROUP BY src.data->>'ownership_type', src.geo_origin
                    HAVING COUNT(DISTINCT src.id) >= $1
                """, self.PROVENANCE_MIN_GROUP_SIZE)

                for group in groups:
                    source_ids = list(group["source_ids"])
                    ownership = group["ownership_type"]
                    geo = group["geo_origin"]

                    # For each provenance group, find entities they converge on
                    entity_coverage = await conn.fetch("""
                        SELECT ep.id AS entity_id,
                               ep.canonical_name,
                               COUNT(DISTINCT s.source_id) AS covering_sources,
                               COUNT(DISTINCT s.id) AS signal_count,
                               array_agg(DISTINCT s.id) AS signal_ids
                        FROM signals s
                        JOIN signal_entity_links sel ON sel.signal_id = s.id
                        JOIN entity_profiles ep ON ep.id = sel.entity_id
                        WHERE s.created_at > $1
                          AND s.source_id = ANY($2::uuid[])
                        GROUP BY ep.id, ep.canonical_name
                        HAVING COUNT(DISTINCT s.source_id) >= $3
                    """, cutoff, source_ids,
                        max(2, len(source_ids) // 2))  # At least half the group

                    for entity_row in entity_coverage:
                        coverage_ratio = entity_row["covering_sources"] / len(source_ids)
                        if coverage_ratio < self.PROVENANCE_ENTITY_OVERLAP_THRESHOLD:
                            continue

                        signal_ids = [str(sid) for sid in entity_row["signal_ids"]]
                        flags.append({
                            "signal_ids": signal_ids,
                            "entity_id": str(entity_row["entity_id"]),
                            "entity_name": entity_row["canonical_name"],
                            "provenance_group": f"{ownership}/{geo}",
                            "source_names": list(group["source_names"]),
                            "coverage_ratio": round(coverage_ratio, 2),
                            "signal_count": entity_row["signal_count"],
                            "severity": "low" if coverage_ratio < 0.8 else "medium",
                            "description": (
                                f"{entity_row['covering_sources']}/{len(source_ids)} "
                                f"{ownership}/{geo} sources covering '{entity_row['canonical_name']}' "
                                f"with {entity_row['signal_count']} signals in "
                                f"{self.PROVENANCE_WINDOW_HOURS}h window"
                            ),
                        })

        except Exception as e:
            logger.error("Provenance cluster detection failed: %s", e)

        return flags

    # ------------------------------------------------------------------
    # Flag storage
    # ------------------------------------------------------------------

    async def flag_signals(
        self, signal_ids: list[str], flag_type: str, details: dict,
    ) -> int:
        """Store adversarial flags on signals.

        Writes to the signal's data JSONB: data->'adversarial_flags' array.
        Each flag entry includes the type, timestamp, and relevant details.
        Returns the number of signals successfully flagged.
        """
        if not signal_ids:
            return 0

        flagged = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        # Build a clean flag record (exclude signal_ids from details to avoid bloat)
        flag_record = {
            "type": flag_type,
            "detected_at": now_iso,
            "severity": details.get("severity", "medium"),
            "description": details.get("description", ""),
        }
        # Include entity info if present
        if details.get("entity_name"):
            flag_record["entity_name"] = details["entity_name"]
        if details.get("provenance_group"):
            flag_record["provenance_group"] = details["provenance_group"]

        flag_json = json.dumps(flag_record)

        try:
            async with self.pool.acquire() as conn:
                # Append to the adversarial_flags array in each signal's data JSONB.
                # If adversarial_flags doesn't exist yet, create it as an array.
                for signal_id in signal_ids:
                    try:
                        sid = UUID(signal_id)
                        result = await conn.execute("""
                            UPDATE signals SET
                                data = jsonb_set(
                                    data,
                                    '{adversarial_flags}',
                                    COALESCE(data->'adversarial_flags', '[]'::jsonb) || $1::jsonb,
                                    true
                                ),
                                updated_at = NOW()
                            WHERE id = $2
                        """, flag_json, sid)
                        if "UPDATE 1" in result:
                            flagged += 1
                    except (ValueError, Exception) as e:
                        logger.debug("Failed to flag signal %s: %s", signal_id, e)

        except Exception as e:
            logger.error("Flag storage failed: %s", e)

        return flagged
