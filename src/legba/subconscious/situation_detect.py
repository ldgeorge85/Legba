"""SLM-based situation detection for the subconscious service.

JDL Level 2: SLM situation detection.

Replaces the mechanical entity-concatenation approach in the maintenance
daemon with an SLM that reasons about whether event clusters represent
coherent situations and produces human-quality names and descriptions.

Flow:
1. Query events from last 48h grouped by (category, primary_region)
2. Filter to clusters with 8+ events
3. Ask the SLM to evaluate each cluster for narrative coherence
4. Dedup against existing situations via Jaccard similarity on name
5. Insert passing clusters as proposed situations
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg

from .config import SubconsciousConfig
from .prompts import (
    SITUATION_DETECTION_PROMPT,
    SITUATION_DETECTION_SCHEMA,
    SITUATION_DETECTION_SYSTEM,
)
from .provider import BaseSLMProvider, SLMError
from .schemas import SituationDetectionVerdict

logger = logging.getLogger("legba.subconscious.situation_detect")

# Minimum events in a cluster to be considered
MIN_CLUSTER_SIZE = 8

# Jaccard similarity threshold for dedup against existing situations
JACCARD_DEDUP_THRESHOLD = 0.5

# Maximum events to include in the SLM prompt (keeps token count sane)
MAX_EVENTS_IN_PROMPT = 30


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

async def detect_situations(
    pool: asyncpg.Pool,
    provider: BaseSLMProvider,
    config: SubconsciousConfig,
) -> int:
    """Run SLM-based situation detection.

    Returns the number of proposed situations created.
    """
    proposed = 0

    try:
        # 1. Fetch recent events
        events = await _fetch_recent_events(pool)
        if not events:
            logger.debug("Situation detection: no recent events")
            return 0

        # 2. Group by (category, primary_region)
        groups = _group_events(events)

        # 3. Fetch existing situations for dedup
        existing_names = await _fetch_existing_situation_names(pool)

        # 4. Evaluate each qualifying cluster
        for (category, region), cluster in groups.items():
            if len(cluster) < MIN_CLUSTER_SIZE:
                continue

            # Gather cluster metadata for the prompt
            event_list = _format_event_list(cluster)

            # Ask the SLM
            verdict = await _evaluate_cluster(
                provider, config, category, region, len(cluster), event_list,
            )
            if verdict is None:
                continue

            if not verdict.is_situation:
                logger.debug(
                    "Situation detection: cluster %s/%s (%d events) "
                    "rejected by SLM",
                    category, region, len(cluster),
                )
                continue

            if not verdict.name or not verdict.name.strip():
                logger.warning(
                    "Situation detection: SLM returned is_situation=True "
                    "but empty name for %s/%s",
                    category, region,
                )
                continue

            # Dedup against existing situations
            if _is_duplicate(verdict.name, existing_names):
                logger.debug(
                    "Situation detection: '%s' is duplicate of existing "
                    "situation (Jaccard >= %.2f)",
                    verdict.name, JACCARD_DEDUP_THRESHOLD,
                )
                continue

            # Create the proposed situation
            created = await _create_proposed_situation(
                pool, category, region, cluster, verdict,
            )
            if created:
                proposed += 1
                # Add to existing names so subsequent clusters in this
                # run also dedup correctly
                existing_names.append(verdict.name)

    except Exception as exc:
        logger.error("Situation detection failed: %s", exc)

    if proposed:
        logger.info("Situation detection: %d new proposals created", proposed)
    return proposed


# ------------------------------------------------------------------
# Data gathering
# ------------------------------------------------------------------

async def _fetch_recent_events(pool: asyncpg.Pool) -> list[dict]:
    """Fetch events from the last 48 hours with metadata."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    try:
        rows = await pool.fetch(
            """
            SELECT
                e.id,
                e.title,
                e.category,
                e.data,
                e.created_at,
                e.event_count
            FROM events e
            WHERE e.created_at > $1
              AND e.category != 'other'
            ORDER BY e.created_at DESC
            LIMIT 500
            """,
            cutoff,
        )

        events: list[dict] = []
        for row in rows:
            raw = row["data"]
            data = (
                raw
                if isinstance(raw, dict)
                else json.loads(raw) if isinstance(raw, str) else {}
            )
            events.append({
                "id": row["id"],
                "title": row["title"],
                "category": row["category"],
                "created_at": row["created_at"],
                "signal_count": row.get("event_count") or data.get("signal_count", 0),
                "severity": data.get("severity", "medium"),
                "actors": data.get("actors") or [],
                "locations": data.get("locations") or [],
                "geo_countries": data.get("geo_countries") or [],
            })
        return events

    except Exception as exc:
        logger.warning("Failed to fetch recent events: %s", exc)
        return []


def _group_events(
    events: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    """Group events by (category, primary_region)."""
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


def _format_event_list(cluster: list[dict]) -> str:
    """Format a cluster of events into a readable list for the SLM prompt.

    Caps at MAX_EVENTS_IN_PROMPT to keep token count manageable.
    """
    lines: list[str] = []
    for evt in cluster[:MAX_EVENTS_IN_PROMPT]:
        actors = ", ".join(evt.get("actors") or [])
        severity = evt.get("severity", "medium")
        sig_count = evt.get("signal_count", 0)
        parts = [f"- {evt['title']}"]
        if actors:
            parts.append(f"  Actors: {actors}")
        parts.append(f"  Severity: {severity}, Signals: {sig_count}")
        lines.append("\n".join(parts))

    if len(cluster) > MAX_EVENTS_IN_PROMPT:
        lines.append(
            f"... and {len(cluster) - MAX_EVENTS_IN_PROMPT} more events"
        )

    return "\n".join(lines)


async def _fetch_existing_situation_names(pool: asyncpg.Pool) -> list[str]:
    """Fetch names of existing active/proposed situations for dedup."""
    try:
        rows = await pool.fetch(
            "SELECT name FROM situations "
            "WHERE status IN ('active', 'escalating', 'proposed')"
        )
        return [row["name"] for row in rows if row["name"]]
    except Exception as exc:
        logger.warning("Failed to fetch existing situations: %s", exc)
        return []


# ------------------------------------------------------------------
# SLM evaluation
# ------------------------------------------------------------------

async def _evaluate_cluster(
    provider: BaseSLMProvider,
    config: SubconsciousConfig,
    category: str,
    region: str,
    event_count: int,
    event_list: str,
) -> SituationDetectionVerdict | None:
    """Ask the SLM whether a cluster is a coherent situation."""
    prompt = SITUATION_DETECTION_PROMPT.format(
        event_count=event_count,
        region=region.title(),
        category=category,
        event_list=event_list,
        schema=json.dumps(SITUATION_DETECTION_SCHEMA, indent=2),
    )

    try:
        result = await provider.complete(
            prompt=prompt,
            system=SITUATION_DETECTION_SYSTEM,
            json_schema=SITUATION_DETECTION_SCHEMA,
        )
        return SituationDetectionVerdict.model_validate(result)
    except SLMError as exc:
        logger.warning(
            "SLM situation evaluation failed for %s/%s: %s",
            category, region, exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Situation detection parse error for %s/%s: %s",
            category, region, exc,
        )
        return None


# ------------------------------------------------------------------
# Dedup
# ------------------------------------------------------------------

def _jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two strings (word-level tokens)."""
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _is_duplicate(name: str, existing_names: list[str]) -> bool:
    """Check if a proposed name is too similar to any existing situation."""
    for existing in existing_names:
        if _jaccard_similarity(name, existing) >= JACCARD_DEDUP_THRESHOLD:
            return True
    return False


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------

async def _create_proposed_situation(
    pool: asyncpg.Pool,
    category: str,
    region: str,
    cluster: list[dict],
    verdict: SituationDetectionVerdict,
) -> bool:
    """Insert a proposed situation from the SLM verdict."""
    now = datetime.now(timezone.utc)
    sit_id = uuid4()

    # Collect unique entities from the cluster for key_entities
    entity_counts: dict[str, int] = defaultdict(int)
    for evt in cluster:
        for actor in evt.get("actors") or []:
            if actor and len(actor) >= 3:
                entity_counts[actor.lower()] += 1
    # Top entities by frequency
    top_entities = sorted(entity_counts, key=entity_counts.get, reverse=True)[:10]

    # Severity distribution
    sev_counts: dict[str, int] = defaultdict(int)
    for evt in cluster:
        sev_counts[evt.get("severity", "medium")] += 1

    data = {
        "id": str(sit_id),
        "name": verdict.name.strip(),
        "description": verdict.description.strip(),
        "status": "proposed",
        "category": category,
        "key_entities": [e.title() for e in top_entities],
        "regions": [region.title()],
        "tags": ["slm-detected"],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "event_count": len(cluster),
        "intensity_score": 0.0,
        "slm_confidence": verdict.confidence,
        "severity_distribution": dict(sev_counts),
        "proposed_event_ids": [str(e["id"]) for e in cluster[:20]],
    }

    try:
        await pool.execute(
            "INSERT INTO situations (id, data, name, status, category, "
            "created_at, updated_at, event_count) "
            "VALUES ($1, $2::jsonb, $3, 'proposed', $4, $5, $5, $6) "
            "ON CONFLICT DO NOTHING",
            sit_id,
            json.dumps(data, default=str),
            verdict.name.strip(),
            category,
            now,
            len(cluster),
        )
        logger.info(
            "Proposed situation: '%s' (%d events, confidence=%.2f, "
            "region=%s, category=%s)",
            verdict.name.strip(),
            len(cluster),
            verdict.confidence,
            region,
            category,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to create proposed situation: %s", exc)
        return False
