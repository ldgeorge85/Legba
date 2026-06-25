# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-analyst receipt-chain state.

Each analyst maintains a tamper-evident SHA-256 chain over canonical-JSON
of its run receipts (L-107 §7; Mnemosyne D5 alignment).

``RuntimeReceiptChain`` is a single in-process tracker that:

  1. Loads the head hash for an analyst from the latest ``analyst_traces``
     row at startup (or ZERO_HASH if none).
  2. On each new run, computes ``receipt_hash`` chaining the prev head.
  3. Writes the ``analyst_traces`` row carrying the new ``receipt_hash`` +
     ``prev_receipt_hash``.
  4. Updates the in-process head pointer.

Concurrency: each analyst's chain head is guarded by an ``asyncio.Lock`` so
two concurrent runs of the same analyst chain sequentially. Cross-analyst
runs proceed concurrently. The lock is per (analyst_id) — independent of
analyst_version, since version bumps still extend the same chain (the
prompt/version moves in the canonical receipt, so the hash captures it).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID

import asyncpg

from ._core import (
    ZERO_HASH,
    compute_receipt_hash,
)


class RuntimeReceiptChain:
    """Tracks per-analyst head hashes across runs in a single process.

    Multiple processes coordinating on the same analyst chain need
    out-of-band coordination (Phase 5 runtime — the runtime control plane
    will mint a single chain head per analyst per shard). This class is the
    in-process state holder; the checkpointer signs whichever head it sees
    at checkpoint time.
    """

    def __init__(self, pg_pool: asyncpg.Pool):
        self._pool = pg_pool
        self._heads: dict[str, str] = {}              # analyst_id → hash
        self._counts: dict[str, int] = {}             # analyst_id → trace count
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, analyst_id: str) -> asyncio.Lock:
        lock = self._locks.get(analyst_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[analyst_id] = lock
        return lock

    async def head(self, analyst_id: str) -> str:
        """Return the current head hash, hydrating from DB on first ask.

        Callers that already hold the per-analyst lock should use
        ``_head_locked`` to avoid the non-reentrant double-acquire.
        """
        if analyst_id in self._heads:
            return self._heads[analyst_id]
        async with self._lock(analyst_id):
            return await self._head_locked(analyst_id)

    async def _head_locked(self, analyst_id: str) -> str:
        """Caller must hold ``self._lock(analyst_id)``."""
        if analyst_id in self._heads:
            return self._heads[analyst_id]
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT receipt_hash
                FROM analyst_traces
                WHERE analyst_id = $1
                ORDER BY run_started_at DESC
                LIMIT 1
                """,
                analyst_id,
            )
            self._heads[analyst_id] = row["receipt_hash"] if row else ZERO_HASH
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM analyst_traces WHERE analyst_id = $1",
                analyst_id,
            )
            self._counts[analyst_id] = int(cnt or 0)
        return self._heads[analyst_id]

    async def count(self, analyst_id: str) -> int:
        if analyst_id not in self._counts:
            await self.head(analyst_id)
        return self._counts.get(analyst_id, 0)

    async def record(
        self,
        *,
        run_id: UUID,
        analyst_id: str,
        analyst_version: str,
        cadence_trigger: str,
        target_id: str | None,
        input_row_refs: Sequence[UUID],
        input_payload: dict[str, Any] | None,
        prompt_module_hash: str | None,
        prompt_rendered: str | None,
        output_row_refs: Sequence[UUID],
        output_payload: Any,
        run_started_at: datetime,
        run_ended_at: datetime,
        status: str = "success",
        intermediate_steps: list[dict[str, Any]] | None = None,
        llm_calls: list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        error_payload: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Compute receipt hash, write analyst_traces row, advance head.

        Returns ``(receipt_hash, prev_receipt_hash)``. Raises ``asyncpg``
        errors on write failure — caller decides DLQ vs retry.

        Per-analyst lock guarantees sequential chaining for a single
        analyst across concurrent calls; cross-analyst writes proceed
        independently. The runtime actor wires this AFTER the analyst-
        output INSERT so the chain row carries ``output_row_refs`` for
        the row(s) just produced (per L-107 §7 lineage-into-chain).
        """
        async with self._lock(analyst_id):
            prev = await self._head_locked(analyst_id)
            receipt = compute_receipt_hash(
                run_id=run_id,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                input_row_refs=list(input_row_refs),
                prompt_module_hash=prompt_module_hash,
                prompt_rendered=prompt_rendered,
                output_row_refs=list(output_row_refs),
                output_payload=output_payload,
                run_ended_at=run_ended_at,
                prev_receipt_hash=prev,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO analyst_traces (
                        run_id, analyst_id, analyst_version,
                        target_id, cadence_trigger,
                        input_row_refs, input_payload,
                        prompt_module_hash, prompt_rendered,
                        intermediate_steps, llm_calls, tool_calls,
                        output_row_refs, output_payload,
                        status, error_payload,
                        run_started_at, run_ended_at,
                        receipt_hash, prev_receipt_hash
                    ) VALUES (
                        $1, $2, $3,
                        $4, $5,
                        $6, $7::jsonb,
                        $8, $9,
                        $10::jsonb, $11::jsonb, $12::jsonb,
                        $13, $14::jsonb,
                        $15, $16::jsonb,
                        $17, $18,
                        $19, $20
                    )
                    """,
                    run_id, analyst_id, analyst_version,
                    target_id, cadence_trigger,
                    list(input_row_refs),
                    json.dumps(input_payload or {}, default=_json_default),
                    prompt_module_hash, prompt_rendered,
                    json.dumps(intermediate_steps or [], default=_json_default),
                    json.dumps(llm_calls or [], default=_json_default),
                    json.dumps(tool_calls or [], default=_json_default),
                    list(output_row_refs),
                    json.dumps(output_payload or {}, default=_json_default),
                    status,
                    (
                        json.dumps(error_payload, default=_json_default)
                        if error_payload is not None
                        else None
                    ),
                    run_started_at, run_ended_at,
                    receipt, prev,
                )
            self._heads[analyst_id] = receipt
            self._counts[analyst_id] = self._counts.get(analyst_id, 0) + 1
            return receipt, prev

    async def append_run(
        self,
        *,
        run_id: UUID,
        analyst_id: str,
        analyst_version: str,
        cadence_trigger: str,
        target_id: str | None,
        input_row_refs: Sequence[UUID],
        input_payload: dict[str, Any] | None,
        prompt_module_hash: str | None,
        prompt_rendered: str | None,
        output_row_refs: Sequence[UUID],
        output_payload: Any,
        run_started_at: datetime,
        run_ended_at: datetime,
        status: str = "success",
        intermediate_steps: list[dict[str, Any]] | None = None,
        llm_calls: list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        error_payload: dict[str, Any] | None = None,
    ) -> str:
        """Back-compat wrapper around :meth:`record`.

        Returns only the new ``receipt_hash`` (the previous-hash slot is
        discarded). New callers should prefer :meth:`record` to access
        the ``prev_receipt_hash`` for downstream propagation (analyst-
        output envelopes, audit checkpoints, etc.).
        """
        receipt, _prev = await self.record(
            run_id=run_id,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            cadence_trigger=cadence_trigger,
            target_id=target_id,
            input_row_refs=input_row_refs,
            input_payload=input_payload,
            prompt_module_hash=prompt_module_hash,
            prompt_rendered=prompt_rendered,
            output_row_refs=output_row_refs,
            output_payload=output_payload,
            run_started_at=run_started_at,
            run_ended_at=run_ended_at,
            status=status,
            intermediate_steps=intermediate_steps,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            error_payload=error_payload,
        )
        return receipt


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)
