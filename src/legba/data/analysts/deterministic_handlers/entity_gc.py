# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``entity_gc`` sub-handler — L-203 migration of ``legba.maintenance.entity_gc``.

Entity garbage-collection family. No LLM. Four operations:

  1. Mark entities with no signal_entity_links in 30d as ``gc_status=dormant``.
  2. Flag name-similar entity pairs (trigram similarity > 0.6) with
     co-occurring signals as ``duplicate_candidate``.
  3. Delete orphan signal_entity_links.
  4. Auto-pause sources with > 20 consecutive failures.

Output ``data`` keys:
    dormant_entities    int
    duplicate_flags     int
    orphan_edges        int
    sources_paused      int
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

_DORMANT_DAYS = 30
_DUP_TRIGRAM_THRESHOLD = 0.6
_DUP_MAX_PAIRS = 50
_SOURCE_FAILURE_THRESHOLD = 20


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


async def _mark_dormant(pool: Any) -> int:
    dormant = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ep.id
            FROM entity_profiles ep
            WHERE NOT EXISTS (
                SELECT 1 FROM signal_entity_links sel
                WHERE sel.entity_id = ep.id
                  AND sel.created_at > NOW() - INTERVAL '%d days'
            )
            AND ep.created_at < NOW() - INTERVAL '%d days'
            AND COALESCE(ep.data->>'gc_status', 'active') != 'dormant'
            """ % (_DORMANT_DAYS, _DORMANT_DAYS)
        )
        for row in rows:
            await conn.execute(
                """
                UPDATE entity_profiles SET
                    data = jsonb_set(
                        COALESCE(data, '{}'::jsonb),
                        '{gc_status}',
                        '"dormant"'
                    ),
                    updated_at = NOW()
                WHERE id = $1
                """,
                row["id"],
            )
            dormant += 1
    return dormant


async def _flag_duplicates(pool: Any) -> int:
    flagged = 0
    async with pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        except Exception:
            logger.debug("entity_gc.pg_trgm_unavailable")
            return 0
        rows = await conn.fetch(
            """
            SELECT DISTINCT
                a.id AS id_a, a.canonical_name AS name_a,
                b.id AS id_b, b.canonical_name AS name_b,
                similarity(LOWER(a.canonical_name), LOWER(b.canonical_name)) AS sim
            FROM entity_profiles a
            JOIN entity_profiles b ON a.id < b.id
            WHERE similarity(LOWER(a.canonical_name), LOWER(b.canonical_name)) > $1
              AND a.entity_type = b.entity_type
              AND COALESCE(a.data->>'gc_status', 'active') != 'dormant'
              AND COALESCE(b.data->>'gc_status', 'active') != 'dormant'
              AND COALESCE(a.data->>'duplicate_candidate', 'false') != 'true'
            LIMIT $2
            """,
            _DUP_TRIGRAM_THRESHOLD, _DUP_MAX_PAIRS,
        )
        for row in rows:
            cooc = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                    SELECT sel_a.signal_id
                    FROM signal_entity_links sel_a
                    JOIN signal_entity_links sel_b
                      ON sel_a.signal_id = sel_b.signal_id
                    WHERE sel_a.entity_id = $1
                      AND sel_b.entity_id = $2
                    LIMIT 1
                ) sub
                """,
                row["id_a"], row["id_b"],
            )
            if cooc and cooc > 0:
                await conn.execute(
                    """
                    UPDATE entity_profiles SET
                        data = jsonb_set(
                            jsonb_set(
                                COALESCE(data, '{}'::jsonb),
                                '{duplicate_candidate}',
                                '"true"'
                            ),
                            '{duplicate_of}',
                            to_jsonb($2::text)
                        ),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    row["id_b"], str(row["id_a"]),
                )
                flagged += 1
    return flagged


async def _clean_orphan_edges(pool: Any) -> int:
    removed = 0
    async with pool.acquire() as conn:
        for sql in (
            """
            DELETE FROM signal_entity_links sel
            WHERE NOT EXISTS (
                SELECT 1 FROM entity_profiles ep
                WHERE ep.id = sel.entity_id
            )
            """,
            """
            DELETE FROM signal_entity_links sel
            WHERE EXISTS (
                SELECT 1 FROM entity_profiles ep
                WHERE ep.id = sel.entity_id
                  AND ep.data->>'gc_status' = 'merged'
            )
            """,
        ):
            result = await conn.execute(sql)
            removed += int(result.split()[-1]) if result else 0
    return removed


async def _pause_failing_sources(pool: Any) -> int:
    paused = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, consecutive_failures, data
            FROM sources
            WHERE status = 'active'
              AND consecutive_failures > $1
            """,
            _SOURCE_FAILURE_THRESHOLD,
        )
        for row in rows:
            data = row["data"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    data = {}
            elif data is None:
                data = {}
            else:
                data = dict(data)
            data["auto_paused_at"] = datetime.now(timezone.utc).isoformat()
            data["auto_paused_reason"] = (
                f"Exceeded {_SOURCE_FAILURE_THRESHOLD} consecutive failures "
                f"({row['consecutive_failures']})"
            )
            await conn.execute(
                """
                UPDATE sources SET
                    status = 'paused',
                    data = $2,
                    updated_at = NOW()
                WHERE id = $1
                """,
                row["id"], json.dumps(data),
            )
            paused += 1
    return paused


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    dormant_entities: int,
    duplicate_flags: int,
    orphan_edges: int,
    sources_paused: int,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Entity GC: {dormant_entities} dormant, {duplicate_flags} duplicates, "
        f"{orphan_edges} orphan edges, {sources_paused} sources paused"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"dormant_entities={dormant_entities}",
        f"duplicate_flags={duplicate_flags}",
        f"orphan_edges={orphan_edges}",
        f"sources_paused={sources_paused}",
    ])
    tags = ["deterministic", "entity_gc"]
    if dormant_entities or duplicate_flags or orphan_edges or sources_paused:
        tags.append("gc_actions_taken")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "entity_gc",
            "dormant_entities": dormant_entities,
            "duplicate_flags": duplicate_flags,
            "orphan_edges": orphan_edges,
            "sources_paused": sources_paused,
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
    dormant = 0
    duplicates = 0
    orphans = 0
    paused = 0

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        run_dormant = bool(options.get("run_dormant", True))
        run_duplicates = bool(options.get("run_duplicates", True))
        run_orphans = bool(options.get("run_orphans", True))
        run_pause = bool(options.get("run_source_pause", True))
        if run_dormant:
            try:
                dormant = await _mark_dormant(pool)
            except Exception as exc:
                logger.warning("entity_gc.dormant_failed err=%s", exc)
        if run_duplicates:
            try:
                duplicates = await _flag_duplicates(pool)
            except Exception as exc:
                logger.warning("entity_gc.duplicates_failed err=%s", exc)
        if run_orphans:
            try:
                orphans = await _clean_orphan_edges(pool)
            except Exception as exc:
                logger.warning("entity_gc.orphans_failed err=%s", exc)
        if run_pause:
            try:
                paused = await _pause_failing_sources(pool)
            except Exception as exc:
                logger.warning("entity_gc.source_pause_failed err=%s", exc)

    finding = _build_finding(
        dormant_entities=dormant,
        duplicate_flags=duplicates,
        orphan_edges=orphans,
        sources_paused=paused,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
