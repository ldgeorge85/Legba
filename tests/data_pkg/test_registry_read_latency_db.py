# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The head-driven ``/v3/system/source-firing`` read equals the
whole-table GROUP BY it replaced, row for row, in one snapshot; and migration
0251 leaves its index on the (small) test database.

Rows are scoped by a unique source-id prefix and removed afterwards; the
comparison runs over the WHOLE answer (both statements see every head the
shared database holds), inside one REPEATABLE READ transaction so ``now()`` and
the data are the same for both.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from legba.data.config import PostgresConfig
from legba.data.registry.v3_api import _SOURCE_FIRING_SQL

#: The statement the route ran before the rewrite, verbatim (whole-table aggregates
#: unioned by source id, filtered to real heads).
_OLD_SOURCE_FIRING_SQL = """
    WITH heads AS (
        SELECT descriptor_id AS source_id, state,
               body->'cadence'->'schedule'->>'raw' AS cadence_raw
          FROM public.source_descriptors
         WHERE is_head
           AND descriptor_id NOT LIKE 'src_autowire_p13_%'
           AND descriptor_id NOT LIKE 'src_locked_p13_%'
           AND descriptor_id NOT LIKE 'src_template_p13_%'
           AND descriptor_id NOT LIKE 'src_tmpl_aw_%'
           AND descriptor_id NOT LIKE 'src_tmpl_ds_%'
           AND descriptor_id NOT LIKE 'src_disc_%'
    ),
    sig AS (
        SELECT source_id,
               count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS signals_24h,
               count(*) FILTER (WHERE created_at > now() - interval '7 days') AS signals_7d,
               max(created_at) AS last_seen_at
          FROM public.signals
         GROUP BY source_id
    ),
    poll AS (
        SELECT source_id,
               count(*) FILTER (
                   WHERE outcome = 'error'
                     AND occurred_at > now() - interval '24 hours'
               ) AS recent_error_count
          FROM public.source_poll_outcomes
         GROUP BY source_id
    ),
    latest_poll AS (
        SELECT DISTINCT ON (source_id)
               source_id, outcome AS last_poll_outcome
          FROM public.source_poll_outcomes
         ORDER BY source_id, occurred_at DESC
    ),
    ids AS (
        SELECT source_id FROM heads
        UNION
        SELECT source_id FROM sig
        UNION
        SELECT source_id FROM poll
    )
    SELECT i.source_id,
           h.state,
           h.cadence_raw,
           COALESCE(s.signals_24h, 0) AS signals_24h,
           COALESCE(s.signals_7d, 0) AS signals_7d,
           s.last_seen_at,
           EXTRACT(EPOCH FROM (now() - s.last_seen_at))::bigint AS age_seconds,
           (s.last_seen_at IS NOT NULL
            AND s.last_seen_at > now() - interval '48 hours') AS signals_recent,
           lp.last_poll_outcome,
           COALESCE(p.recent_error_count, 0) AS recent_error_count
      FROM ids i
      LEFT JOIN heads h USING (source_id)
      LEFT JOIN sig s USING (source_id)
      LEFT JOIN poll p USING (source_id)
      LEFT JOIN latest_poll lp USING (source_id)
     WHERE h.source_id IS NOT NULL
     ORDER BY signals_recent DESC, signals_24h DESC, i.source_id
     LIMIT 1000
"""


@pytest.mark.asyncio
async def test_head_driven_source_firing_equals_the_whole_table_read(
    migrated_pg: PostgresConfig,
) -> None:
    tag = f"src_r1lat_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(migrated_pg.dsn)
    sources = {
        f"{tag}_fresh": ("active", [1, 5, 30, 100], [("success", 1), ("error", 30)]),
        f"{tag}_stale": ("active", [24 * 3, 24 * 10], [("error", 2), ("error", 3)]),
        f"{tag}_never": ("active", [], []),
        f"{tag}_paused": ("paused", [2], [("empty", 1)]),
        f"{tag}_polls_only": ("active", [], [("empty", 5)]),
    }
    try:
        for sid, (state, sig_hours, polls) in sources.items():
            await conn.execute(
                """
                INSERT INTO source_descriptors
                    (descriptor_id, version, schema_uri, is_head, kind, state, owner,
                     name, body, created_at)
                VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', $2, 'test',
                        $1, $3::jsonb, now())
                """,
                sid, state,
                json.dumps({"cadence": {"schedule": {"raw": "*/30 * * * *"}}}),
            )
            for h in sig_hours:
                at = now - timedelta(hours=h)
                await conn.execute(
                    """
                    INSERT INTO signals
                        (id, source_id, source_version, payload, content_hash,
                         fetched_at, created_at, updated_at, schema_uri)
                    VALUES ($1, $2, 'v1', '{}'::jsonb, $3, $4, $4, $4,
                            'iglu:legba/signal/jsonschema/1-0-0')
                    """,
                    uuid4(), sid, f"h-{uuid4()}", at,
                )
            for outcome, h in polls:
                await conn.execute(
                    """
                    INSERT INTO source_poll_outcomes
                        (source_id, source_version, outcome, health_state,
                         signals_written, occurred_at)
                    VALUES ($1, 'v1', $2, 'healthy', 0, now() - make_interval(hours => $3))
                    """,
                    sid, outcome, h,
                )
        # junk prefixes stay excluded by both
        await conn.execute(
            """
            INSERT INTO source_descriptors
                (descriptor_id, version, schema_uri, is_head, kind, state, owner,
                 name, body, created_at)
            VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', 'paused', 'test',
                    $1, '{}'::jsonb, now())
            """,
            f"src_disc_{tag}",
        )

        async with conn.transaction(isolation="repeatable_read", readonly=True):
            old = [tuple(r) for r in await conn.fetch(_OLD_SOURCE_FIRING_SQL)]
            # Appended last_poll_newest_entry_at: the old form's ten columns compare
            new = [tuple(r)[:10] for r in await conn.fetch(_SOURCE_FIRING_SQL)]
        assert new == old
        mine = {r[0]: r for r in new if r[0].startswith(tag)}
        assert set(mine) == set(sources)  # the junk head is out, every real head in
        fresh = mine[f"{tag}_fresh"]
        assert (fresh[3], fresh[4]) == (2, 4)  # 24 h, 7 days
        assert fresh[8] == "success" and fresh[9] == 0  # the 30 h error is outside 24 h
        assert mine[f"{tag}_stale"][9] == 2 and mine[f"{tag}_stale"][7] is False
        assert mine[f"{tag}_never"][5] is None and mine[f"{tag}_never"][8] is None

        has_index = await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_signals_source_created_at'"
        )
        assert has_index == 1  # 0251 builds it inline on a small table
    finally:
        await conn.execute(
            "DELETE FROM signals WHERE source_id LIKE $1", f"{tag}%")
        await conn.execute(
            "DELETE FROM source_poll_outcomes WHERE source_id LIKE $1", f"{tag}%")
        await conn.execute(
            "DELETE FROM source_descriptors WHERE descriptor_id LIKE $1 OR descriptor_id = $2",
            f"{tag}%", f"src_disc_{tag}")
        await conn.close()
