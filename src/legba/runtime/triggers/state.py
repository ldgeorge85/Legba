# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crash-safe trigger accumulator store — Postgres-backed (P-10).

The coalescing trigger's dirty-state MUST survive a process restart (one of the
five P-10 acceptance behaviours): if the engine crashes mid-window, the pending
count + the last-fire time + the cooldown anchor + the seen-id set are still
there when it comes back, so a critical signal that arrived before the crash is
not lost and the cooldown is still honoured.

State lives in a ``trigger_state`` table. It is now formalized as migration
``0028_trigger_state.sql`` (a fresh ``down --volumes`` → ``migrate`` bring-up
has it before the runtime boots). This module's :meth:`ensure_schema` retains
the idempotent ``CREATE TABLE IF NOT EXISTS`` (DDL byte-identical to the
migration) as a belt-and-braces no-op so the runtime never depends on migration
ordering. Originally it lived only here, as an additive collision-free overlay
on the 0001-0024 chain (same pattern as ``legba_jobs`` for the job plane).

The row key is ``(analyst_id, target_id)`` — the unit a coalescing trigger
operates over (PIVOT §4.6: an analyst over a target). One analyst watching ten
targets has ten independent accumulators; one target feeding three analysts has
three.

Concurrency: ``claim_fire`` uses ``UPDATE … WHERE last_fired_at IS NOT
DISTINCT FROM <expected>`` as an optimistic compare-and-set so two engine
workers that both decide to fire the same pair don't double-dispatch — exactly
one wins the CAS, the other observes the changed anchor and backs off. This is
the trigger-plane analogue of the job ledger's idempotency claim.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .policy import TriggerAccumulator


SCHEMA = """
CREATE TABLE IF NOT EXISTS public.trigger_state (
    analyst_id       TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    tenant           TEXT NOT NULL DEFAULT 'default',
    pending_count    INTEGER NOT NULL DEFAULT 0,
    max_pending_rank INTEGER NOT NULL DEFAULT -1,
    last_fired_at    TIMESTAMPTZ,
    first_dirty_at   TIMESTAMPTZ,
    seen_signal_ids  JSONB NOT NULL DEFAULT '[]'::JSONB,
    fire_count       BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (analyst_id, target_id)
);
CREATE INDEX IF NOT EXISTS trigger_state_target_idx ON public.trigger_state(target_id);
CREATE INDEX IF NOT EXISTS trigger_state_pending_idx
    ON public.trigger_state(pending_count) WHERE pending_count > 0;
"""


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _acc_to_seen_json(acc: TriggerAccumulator) -> str:
    # Persist insertion order so the seen-cap eviction (oldest-first) is stable
    # across restarts.
    return json.dumps(list(acc._seen_order))


def _seen_from_json(raw: Any) -> tuple[set[str], list[str]]:
    if not raw:
        return set(), []
    data = raw if isinstance(raw, list) else json.loads(raw)
    order = [str(x) for x in data]
    return set(order), order


class TriggerStateStore:
    """Async accessor for the crash-safe ``trigger_state`` table.

    Holds the asyncpg pool; every method acquires its own connection so the
    store is safe to share across the engine's workers.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(
        self, analyst_id: str, target_id: str, *, seen_cap: int = 10_000
    ) -> TriggerAccumulator:
        """Load the accumulator for a pair; a never-seen pair returns the zero
        accumulator (pending 0, never fired)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT pending_count, max_pending_rank, last_fired_at,
                       first_dirty_at, seen_signal_ids
                  FROM public.trigger_state
                 WHERE analyst_id = $1 AND target_id = $2
                """,
                analyst_id,
                target_id,
            )
        if row is None:
            return TriggerAccumulator(seen_cap=seen_cap)
        seen, order = _seen_from_json(row["seen_signal_ids"])
        return TriggerAccumulator(
            pending_count=int(row["pending_count"]),
            max_pending_rank=int(row["max_pending_rank"]),
            last_fired_at=row["last_fired_at"],
            first_dirty_at=row["first_dirty_at"],
            seen_signal_ids=seen,
            seen_cap=seen_cap,
            _seen_order=order,
        )

    # ------------------------------------------------------------------
    # Write — dirty accumulation
    # ------------------------------------------------------------------

    async def save_dirty(
        self,
        analyst_id: str,
        target_id: str,
        acc: TriggerAccumulator,
        *,
        tenant: str = "default",
    ) -> None:
        """Upsert the accumulated dirty-state after a matching signal.

        Preserves ``last_fired_at`` (the cooldown/cadence anchor) — only the
        pending fields + seen-set move. ``fire_count`` is untouched here. The
        ``tenant`` is recorded so the cadence ticker (:meth:`list_dirty`) can
        re-fire a quiet pair with the correct tenancy seam.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.trigger_state (
                    analyst_id, target_id, tenant, pending_count, max_pending_rank,
                    last_fired_at, first_dirty_at, seen_signal_ids, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NOW())
                ON CONFLICT (analyst_id, target_id) DO UPDATE SET
                    tenant           = EXCLUDED.tenant,
                    pending_count    = EXCLUDED.pending_count,
                    max_pending_rank = EXCLUDED.max_pending_rank,
                    first_dirty_at   = EXCLUDED.first_dirty_at,
                    seen_signal_ids  = EXCLUDED.seen_signal_ids,
                    updated_at       = NOW()
                """,
                analyst_id,
                target_id,
                tenant,
                acc.pending_count,
                acc.max_pending_rank,
                acc.last_fired_at,
                acc.first_dirty_at,
                _acc_to_seen_json(acc),
            )

    # ------------------------------------------------------------------
    # Write — fire (optimistic compare-and-set on the fire anchor)
    # ------------------------------------------------------------------

    async def claim_fire(
        self,
        analyst_id: str,
        target_id: str,
        *,
        expected_last_fired_at: datetime | None,
        fired_at: datetime | None = None,
    ) -> bool:
        """Atomically reset the accumulator + advance the fire anchor.

        Compare-and-set on ``last_fired_at``: the caller passes the
        ``last_fired_at`` it read when it decided to fire; the UPDATE only
        commits if the row's anchor still matches (``IS NOT DISTINCT FROM``
        handles the NULL "never fired" case). Returns True iff THIS caller won
        the fire — competing engine workers that lost observe ``False`` and
        skip the dispatch (no double-fire). Clears pending + seen-set and bumps
        ``fire_count``.
        """
        fired_at = fired_at or _utcnow()
        async with self._pool.acquire() as conn:
            # Ensure a row exists for a pair that fired on its very first
            # signal (no prior dirty save) — insert the zero row, then CAS.
            await conn.execute(
                """
                INSERT INTO public.trigger_state (analyst_id, target_id)
                VALUES ($1, $2)
                ON CONFLICT (analyst_id, target_id) DO NOTHING
                """,
                analyst_id,
                target_id,
            )
            updated = await conn.fetchval(
                """
                UPDATE public.trigger_state
                   SET pending_count    = 0,
                       max_pending_rank = -1,
                       seen_signal_ids  = '[]'::jsonb,
                       first_dirty_at   = NULL,
                       last_fired_at    = $3,
                       fire_count       = fire_count + 1,
                       updated_at       = NOW()
                 WHERE analyst_id = $1 AND target_id = $2
                   AND last_fired_at IS NOT DISTINCT FROM $4
                RETURNING analyst_id
                """,
                analyst_id,
                target_id,
                fired_at,
                expected_last_fired_at,
            )
        return updated is not None

    # ------------------------------------------------------------------
    # Introspection (tests / operators)
    # ------------------------------------------------------------------

    async def fire_count(self, analyst_id: str, target_id: str) -> int:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT fire_count FROM public.trigger_state
                 WHERE analyst_id = $1 AND target_id = $2
                """,
                analyst_id,
                target_id,
            )
        return int(val) if val is not None else 0

    async def list_dirty(self, *, limit: int = 1000) -> list[tuple[str, str, str]]:
        """All ``(analyst_id, target_id, tenant)`` triples with pending dirt.

        The cadence ticker walks these so a quiet pair with no NATS traffic
        still gets its periodic re-evaluation, with the correct tenancy seam.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT analyst_id, target_id, tenant FROM public.trigger_state
                 WHERE pending_count > 0
                 ORDER BY updated_at ASC
                 LIMIT $1
                """,
                limit,
            )
        return [(r["analyst_id"], r["target_id"], r["tenant"]) for r in rows]


__all__ = ["TriggerStateStore", "SCHEMA"]
