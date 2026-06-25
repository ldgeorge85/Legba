# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``adversarial_signals`` sub-handler — L-203 migration of ``legba.maintenance.adversarial``.

Coordinated-inauthentic-behavior detection. Three heuristics (no LLM):

  1. **Velocity spike** — 3+ low-quality sources (score < 0.4) publish about
     the same entity within a 6h window with no high-quality (> 0.7)
     corroboration.
  2. **Semantic echo** — 3+ signals from independent sources in a 4h
     bucket with title Jaccard similarity >= 0.6, where the sources do NOT
     share an ownership_type or geo_origin.
  3. **Provenance cluster** — sources sharing (ownership_type, geo_origin)
     converging on the same entity at >= 50% group coverage.

For each detection, flags the relevant signal IDs in
``signals.data.adversarial_flags`` (JSONB array).

The handler also accepts a **synthetic** input mode for unit tests: when
``deps.pg_pool`` is absent, processes pre-shaped signal rows directly. See
the test file for the synthetic row shape.

Output ``data`` keys:
    velocity_flags      [{...}]
    semantic_echo_flags [{...}]
    provenance_flags    [{...}]
    signals_flagged     int (total DB writes attempted)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Mapping
from uuid import UUID

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

# Detection parameters — match legacy AdversarialDetector class attributes.
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


# ---------------------------------------------------------------------------
# Title tokenization + Jaccard (lightweight; avoid importing ingestion.dedup)
# ---------------------------------------------------------------------------


_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "was", "are", "were", "be", "been",
    "has", "had", "have", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "not", "no", "so", "if",
    "as", "its", "it", "he", "she", "they", "them", "their", "his", "her",
    "this", "that", "these", "those", "than", "then", "says", "said",
    "after", "over", "up", "out", "new", "more",
})


def _title_words(title: str) -> set[str]:
    words: set[str] = set()
    for w in (title or "").lower().split():
        w = w.strip(".,;:!?\"'()[]{}—–-")
        if w and len(w) > 1 and w not in _STOPWORDS:
            words.add(w)
    return words


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Detectors (live mode)
# ---------------------------------------------------------------------------


async def _detect_velocity_spikes(pool: Any) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=VELOCITY_WINDOW_HOURS)
    try:
        async with pool.acquire() as conn:
            low_rows = await conn.fetch(
                """
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
                """,
                cutoff, VELOCITY_LOW_QUALITY_THRESHOLD, VELOCITY_MIN_LOW_SOURCES,
            )
            for row in low_rows:
                entity_id = row["entity_id"]
                high_count = await conn.fetchval(
                    """
                    SELECT COUNT(DISTINCT src.id)
                    FROM signals s
                    JOIN sources src ON s.source_id = src.id
                    JOIN signal_entity_links sel ON sel.signal_id = s.id
                    WHERE s.created_at > $1
                      AND sel.entity_id = $2
                      AND src.source_quality_score > $3
                    """,
                    cutoff, entity_id, VELOCITY_HIGH_QUALITY_THRESHOLD,
                )
                if (high_count or 0) == 0:
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
                            f"no high-quality corroboration in "
                            f"{VELOCITY_WINDOW_HOURS}h window"
                        ),
                    })
    except Exception as exc:
        logger.warning("adversarial.velocity.failed err=%s", exc)
    return flags


async def _detect_semantic_echoes(pool: Any) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ECHO_WINDOW_HOURS)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.id, s.title, s.source_id, s.created_at,
                       src.name AS source_name,
                       src.geo_origin,
                       src.data->>'ownership_type' AS ownership_type
                FROM signals s
                JOIN sources src ON s.source_id = src.id
                WHERE s.created_at > $1
                ORDER BY s.created_at
                """,
                cutoff,
            )
        if len(rows) < ECHO_MIN_CLUSTER_SIZE:
            return flags
        bucket_size = timedelta(hours=ECHO_BUCKET_HOURS)
        buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            elapsed = (row["created_at"] - cutoff).total_seconds()
            key = int(elapsed // bucket_size.total_seconds())
            buckets[key].append({
                "id": str(row["id"]),
                "title": row["title"],
                "words": _title_words(row["title"] or ""),
                "source_id": str(row["source_id"]),
                "source_name": row["source_name"],
                "geo_origin": row["geo_origin"] or "",
                "ownership_type": row["ownership_type"] or "independent",
            })
        for signals in buckets.values():
            flags.extend(_echo_clusters_in_bucket(signals))
    except Exception as exc:
        logger.warning("adversarial.echo.failed err=%s", exc)
    return flags


def _echo_clusters_in_bucket(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(signals) < ECHO_MIN_CLUSTER_SIZE:
        return []
    out: list[dict[str, Any]] = []
    adjacency: dict[int, set[int]] = defaultdict(set)
    for i, j in combinations(range(len(signals)), 2):
        si, sj = signals[i], signals[j]
        if si["source_id"] == sj["source_id"]:
            continue
        if _jaccard(si["words"], sj["words"]) >= ECHO_JACCARD_THRESHOLD:
            adjacency[i].add(j)
            adjacency[j].add(i)
    visited: set[int] = set()
    for start in list(adjacency):
        if start in visited:
            continue
        cluster: set[int] = set()
        queue = [start]
        while queue:
            node = queue.pop(0)
            if node in cluster:
                continue
            cluster.add(node)
            visited.add(node)
            for nbr in adjacency.get(node, set()):
                if nbr not in cluster:
                    queue.append(nbr)
        if len(cluster) < ECHO_MIN_CLUSTER_SIZE:
            continue
        cluster_signals = [signals[i] for i in cluster]
        unique_sources = {s["source_id"] for s in cluster_signals}
        if len(unique_sources) < ECHO_MIN_CLUSTER_SIZE:
            continue
        ownership_types = {s["ownership_type"] for s in cluster_signals}
        geo_origins = {s["geo_origin"] for s in cluster_signals if s["geo_origin"]}
        shared_provenance = (
            len(ownership_types) == 1 and ownership_types != {"independent"}
        ) or (
            len(geo_origins) == 1 and len(geo_origins) > 0
        )
        if shared_provenance:
            continue
        out.append({
            "signal_ids": [s["id"] for s in cluster_signals],
            "source_count": len(unique_sources),
            "source_names": list({s["source_name"] for s in cluster_signals}),
            "ownership_types": list(ownership_types),
            "geo_origins": list(geo_origins),
            "sample_titles": [s["title"][:80] for s in cluster_signals[:3]],
            "severity": "high" if len(unique_sources) >= 5 else "medium",
            "description": (
                f"{len(cluster_signals)} signals with similar titles from "
                f"{len(unique_sources)} independent sources — potential echo campaign"
            ),
        })
    return out


async def _detect_provenance_clusters(pool: Any) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PROVENANCE_WINDOW_HOURS)
    try:
        async with pool.acquire() as conn:
            groups = await conn.fetch(
                """
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
                """,
                PROVENANCE_MIN_GROUP_SIZE,
            )
            for group in groups:
                source_ids = list(group["source_ids"])
                ownership = group["ownership_type"]
                geo = group["geo_origin"]
                entity_rows = await conn.fetch(
                    """
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
                    """,
                    cutoff, source_ids, max(2, len(source_ids) // 2),
                )
                for er in entity_rows:
                    ratio = er["covering_sources"] / len(source_ids)
                    if ratio < PROVENANCE_ENTITY_OVERLAP_THRESHOLD:
                        continue
                    signal_ids = [str(sid) for sid in er["signal_ids"]]
                    flags.append({
                        "signal_ids": signal_ids,
                        "entity_id": str(er["entity_id"]),
                        "entity_name": er["canonical_name"],
                        "provenance_group": f"{ownership}/{geo}",
                        "source_names": list(group["source_names"]),
                        "coverage_ratio": round(ratio, 2),
                        "signal_count": er["signal_count"],
                        "severity": "low" if ratio < 0.8 else "medium",
                        "description": (
                            f"{er['covering_sources']}/{len(source_ids)} "
                            f"{ownership}/{geo} sources covering "
                            f"'{er['canonical_name']}' with {er['signal_count']} "
                            f"signals in {PROVENANCE_WINDOW_HOURS}h window"
                        ),
                    })
    except Exception as exc:
        logger.warning("adversarial.provenance.failed err=%s", exc)
    return flags


async def _flag_signals(
    pool: Any, signal_ids: list[str], flag_type: str, details: dict[str, Any],
) -> int:
    if not signal_ids:
        return 0
    flagged = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        "type": flag_type,
        "detected_at": now_iso,
        "severity": details.get("severity", "medium"),
        "description": details.get("description", ""),
    }
    if details.get("entity_name"):
        record["entity_name"] = details["entity_name"]
    if details.get("provenance_group"):
        record["provenance_group"] = details["provenance_group"]
    payload = json.dumps(record)
    try:
        async with pool.acquire() as conn:
            for sid in signal_ids:
                try:
                    uid = UUID(sid)
                    result = await conn.execute(
                        """
                        UPDATE signals SET
                            data = jsonb_set(
                                data,
                                '{adversarial_flags}',
                                COALESCE(data->'adversarial_flags', '[]'::jsonb) || $1::jsonb,
                                true
                            ),
                            updated_at = NOW()
                        WHERE id = $2
                        """,
                        payload, uid,
                    )
                    if "UPDATE 1" in result:
                        flagged += 1
                except Exception as exc:
                    logger.debug(
                        "adversarial.flag_signal_failed signal_id=%s err=%s",
                        sid, exc,
                    )
    except Exception as exc:
        logger.warning("adversarial.flag_storage_failed err=%s", exc)
    return flagged


# ---------------------------------------------------------------------------
# Synthetic (unit-test) detector — only the echo path is testable without
# substrate. Inputs shape:
#   {"signal_id": str, "title": str, "source_id": str, "geo_origin": str,
#    "ownership_type": str}
# ---------------------------------------------------------------------------


def _synthetic_echo(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(rows) < ECHO_MIN_CLUSTER_SIZE:
        return []
    shaped = [
        {
            "id": str(r["signal_id"]),
            "title": str(r.get("title") or ""),
            "words": _title_words(str(r.get("title") or "")),
            "source_id": str(r.get("source_id") or r["signal_id"]),
            "source_name": str(r.get("source_name") or r.get("source_id") or "src"),
            "geo_origin": str(r.get("geo_origin") or ""),
            "ownership_type": str(r.get("ownership_type") or "independent"),
        }
        for r in rows
    ]
    return _echo_clusters_in_bucket(shaped)


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    velocity_flags: list[dict[str, Any]],
    semantic_echo_flags: list[dict[str, Any]],
    provenance_flags: list[dict[str, Any]],
    signals_flagged: int,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Adversarial: velocity={len(velocity_flags)} "
        f"echo={len(semantic_echo_flags)} "
        f"provenance={len(provenance_flags)} "
        f"signals_flagged={signals_flagged}"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body_lines = [
        f"velocity_flags={len(velocity_flags)}",
        f"semantic_echo_flags={len(semantic_echo_flags)}",
        f"provenance_flags={len(provenance_flags)}",
        f"signals_flagged={signals_flagged}",
    ]
    tags = ["deterministic", "adversarial_signals"]
    if velocity_flags or semantic_echo_flags or provenance_flags:
        tags.append("adversarial_flags_present")
    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "adversarial_signals",
            "velocity_flags": velocity_flags,
            "semantic_echo_flags": semantic_echo_flags,
            "provenance_flags": provenance_flags,
            "signals_flagged": signals_flagged,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring."""
    velocity_flags: list[dict[str, Any]] = []
    echo_flags: list[dict[str, Any]] = []
    provenance_flags: list[dict[str, Any]] = []
    flagged_total = 0

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        if bool(options.get("run_velocity", True)):
            velocity_flags = await _detect_velocity_spikes(pool)
        if bool(options.get("run_echo", True)):
            echo_flags = await _detect_semantic_echoes(pool)
        if bool(options.get("run_provenance", True)):
            provenance_flags = await _detect_provenance_clusters(pool)
        # Persist flags on signals
        for flag_type, flags in (
            ("velocity_spike", velocity_flags),
            ("semantic_echo", echo_flags),
            ("provenance_cluster", provenance_flags),
        ):
            for f in flags:
                flagged_total += await _flag_signals(
                    pool, f.get("signal_ids", []), flag_type, f,
                )
    else:
        # Synthetic unit-test mode — only echo detection is reachable.
        echo_flags = _synthetic_echo(list(inputs))

    finding = _build_finding(
        velocity_flags=velocity_flags,
        semantic_echo_flags=echo_flags,
        provenance_flags=provenance_flags,
        signals_flagged=flagged_total,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
