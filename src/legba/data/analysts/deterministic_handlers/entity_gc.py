# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``entity_gc`` sub-handler — L-203 migration of ``legba.maintenance.entity_gc``.

Entity garbage-collection family. No LLM. Five operations:

  1. Mark entities with no signal_entity_links in 30d as ``gc_status=dormant``.
  2. Flag name-similar entity pairs (trigram similarity > 0.6) with
     co-occurring signals as ``duplicate_candidate``.
  3. Delete orphan signal_entity_links.
  4. Auto-pause sources with > 20 consecutive failed polls. The failure
     signal is the contiguous leading run of ``outcome='error'`` rows in
     ``source_poll_outcomes`` per ACTIVE ``source_descriptors`` head — there is
     NO ``sources`` table / ``consecutive_failures`` column (the original query
     hit a non-existent ``sources`` relation and logged
     ``source_pause_failed err=relation "sources" does not exist`` on EVERY run,
     D2). Pausing flips the head descriptor's lifecycle ``state`` 'active'→
     'paused' (mirroring ``discovered_materializer._pause_discovery``) and
     records the reason into ``body->>auto_paused_*``; the runtime actor loop
     observes the state change and stops polling.
  5. Quarantine orphan ``proposed_edges`` (D25) — pending edges whose
     ``source_entity`` / ``target_entity`` has no matching
     ``entity_profiles.canonical_name`` (the exact drift ``integrity_sweep``
     COUNTS as ``orphan_proposed_edges_source`` / ``orphan_proposed_edges_target``
     but never acts on). They can never promote into a CoOccursWith nexus
     (governance keys on canonical entities), so they accrete as permanently
     ``pending`` rows the sweep re-counts forever (406/678 and rising). We flip
     them to ``status='orphaned'`` — non-destructive, removes them from the
     governance ``status='pending'`` work-set, and clears the rising flag.

Output ``data`` keys:
    dormant_entities        int
    duplicate_flags         int
    orphan_edges            int
    sources_paused          int
    orphan_proposed_edges   int
"""

from __future__ import annotations

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
# How many recent poll-outcome rows per source the error-streak read pulls back.
# Must exceed _SOURCE_FAILURE_THRESHOLD so a qualifying leading error-run is
# never truncated by the LIMIT (a productive poll writes no outcome row, so the
# window only ever holds non-productive — empty/error — polls).
_SOURCE_STREAK_WINDOW = max(_SOURCE_FAILURE_THRESHOLD + 5, 25)


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


async def _quarantine_orphan_proposed_edges(pool: Any) -> int:
    """D25 — flip orphan ``pending`` proposed_edges to ``status='orphaned'``.

    An orphan edge is one whose ``source_entity`` OR ``target_entity`` has no
    matching ``entity_profiles.canonical_name`` — exactly the rows
    ``integrity_sweep`` COUNTS (``orphan_proposed_edges_source`` /
    ``orphan_proposed_edges_target``). Such an edge can never be promoted by
    ``proposed_edge_governance`` (which keys on canonical entities), so it sits
    ``pending`` forever and the sweep re-counts it every hour.

    We only touch rows still ``status='pending'`` so we never disturb already-
    promoted/rejected/orphaned edges or the supersession history. ``orphaned``
    is a NEW terminal status outside the governance ``pending`` work-set —
    non-destructive (the row is retained for audit, not deleted)."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE proposed_edges pe
               SET status = 'orphaned', reviewed_at = now()
             WHERE pe.status = 'pending'
               AND (
                   NOT EXISTS (
                       SELECT 1 FROM entity_profiles ep
                       WHERE ep.canonical_name = pe.source_entity
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM entity_profiles ep
                       WHERE ep.canonical_name = pe.target_entity
                   )
               )
            """
        )
    return int(result.split()[-1]) if result else 0


def _consecutive_error_streaks(
    rows: Any,
    *,
    threshold: int,
) -> list[tuple[str, int]]:
    """Pure per-source consecutive-``error``-poll decision — no DB, unit-testable.

    ``rows``: iterable of mappings carrying ``source_id`` and ``outcome``
    (``'error'`` | ``'empty'``), already grouped per source and ordered
    NEWEST-FIRST (the SQL guarantees this, mirroring the liveness watchdog's
    empty-streak read). For each source, count the contiguous LEADING run of
    ``outcome='error'`` rows; the run breaks on the first non-error row (an
    ``'empty'`` outcome, or — by ABSENCE — a productive poll, which writes no
    outcome row at all). Returns ``(source_id, streak_len)`` for every source
    whose leading error run is ``>= threshold``.

    A PRODUCTIVE poll writes NO ``source_poll_outcomes`` row (it is
    self-evidencing via its signals), so a recent success does not appear here
    to break the run — acceptable for an auto-pause guard whose whole point is a
    source that keeps ERRORING and never produces. The empty-streak (silent but
    HTTP-200) case is owned by the liveness watchdog; this leg keys only on hard
    errors so it never auto-pauses a merely-quiet feed."""
    if threshold <= 0:
        return []
    by_source: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in rows:
        sid = r.get("source_id")
        if not sid:
            continue
        if sid not in by_source:
            by_source[sid] = []
            order.append(sid)
        by_source[sid].append(r)
    failing: list[tuple[str, int]] = []
    for sid in order:
        streak = 0
        for r in by_source[sid]:
            if (r.get("outcome") or "") != "error":
                break
            streak += 1
        if streak >= threshold:
            failing.append((sid, streak))
    return failing


async def _pause_failing_sources(pool: Any) -> int:
    """Auto-pause ACTIVE sources with a leading run of >= threshold ``error``
    polls.

    There is NO ``sources`` table — the original query hit a non-existent
    ``sources`` relation (D2). The failure signal lives in
    ``source_poll_outcomes`` (one row per NON-productive poll, ``outcome`` in
    ``'empty'`` / ``'error'``); a source is a descriptor in ``source_descriptors``
    (head row keyed on ``descriptor_id`` + ``is_head``, lifecycle in ``state``,
    metadata in ``body`` jsonb). We read the last ``_SOURCE_STREAK_WINDOW``
    poll-outcomes per ACTIVE head source, compute the contiguous leading
    ``error`` run in pure Python, and flip ``state`` 'active'→'paused' (recording
    the reason in ``body``) for any source over threshold."""
    paused = 0
    window = int(_SOURCE_STREAK_WINDOW)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT po.source_id   AS source_id,
                   po.outcome     AS outcome,
                   po.occurred_at AS occurred_at
            FROM source_descriptors d
            JOIN LATERAL (
                SELECT source_id, outcome, occurred_at
                FROM source_poll_outcomes
                WHERE source_id = d.descriptor_id
                ORDER BY occurred_at DESC
                LIMIT {window}
            ) po ON TRUE
            WHERE d.is_head AND d.state = 'active'
            ORDER BY po.source_id, po.occurred_at DESC
            """
        )
        for source_id, streak in _consecutive_error_streaks(
            [dict(r) for r in rows], threshold=_SOURCE_FAILURE_THRESHOLD
        ):
            reason = (
                f"Exceeded {_SOURCE_FAILURE_THRESHOLD} consecutive failed polls "
                f"({streak} error outcomes)"
            )
            await conn.execute(
                """
                UPDATE source_descriptors SET
                    body = jsonb_set(
                        jsonb_set(
                            COALESCE(body, '{}'::jsonb),
                            '{auto_paused_at}',
                            to_jsonb($2::text)
                        ),
                        '{auto_paused_reason}',
                        to_jsonb($3::text)
                    ),
                    state = 'paused'
                WHERE descriptor_id = $1 AND is_head
                """,
                source_id,
                datetime.now(timezone.utc).isoformat(),
                reason,
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
    orphan_proposed_edges: int = 0,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Entity GC: {dormant_entities} dormant, {duplicate_flags} duplicates, "
        f"{orphan_edges} orphan edges, {sources_paused} sources paused, "
        f"{orphan_proposed_edges} orphan proposed-edges quarantined"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"dormant_entities={dormant_entities}",
        f"duplicate_flags={duplicate_flags}",
        f"orphan_edges={orphan_edges}",
        f"sources_paused={sources_paused}",
        f"orphan_proposed_edges={orphan_proposed_edges}",
    ])
    tags = ["deterministic", "entity_gc"]
    if (
        dormant_entities
        or duplicate_flags
        or orphan_edges
        or sources_paused
        or orphan_proposed_edges
    ):
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
            "orphan_proposed_edges": orphan_proposed_edges,
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
    orphan_proposed = 0

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        run_dormant = bool(options.get("run_dormant", True))
        run_duplicates = bool(options.get("run_duplicates", True))
        run_orphans = bool(options.get("run_orphans", True))
        run_pause = bool(options.get("run_source_pause", True))
        run_orphan_edges = bool(options.get("run_orphan_proposed_edges", True))
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
        if run_orphan_edges:
            try:
                orphan_proposed = await _quarantine_orphan_proposed_edges(pool)
            except Exception as exc:
                logger.warning("entity_gc.orphan_proposed_edges_failed err=%s", exc)

    finding = _build_finding(
        dormant_entities=dormant,
        duplicate_flags=duplicates,
        orphan_edges=orphans,
        sources_paused=paused,
        orphan_proposed_edges=orphan_proposed,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
