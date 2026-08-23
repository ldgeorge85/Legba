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

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        *,
        analyst_id: str | None = None,
    ):
        self._pool = pg_pool
        # The analyst this chain is bound to, when the factory knows it. Purely
        # diagnostic (the per-analyst methods still take an explicit
        # ``analyst_id`` so the multi-analyst test shape is unchanged); lets the
        # D11 fork-tip diagnostic default to the bound id. Optional + keyword =
        # backward-compatible with the ``RuntimeReceiptChain(pool)`` call shape.
        self._analyst_id = analyst_id
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
        """Caller must hold ``self._lock(analyst_id)``.

        D11 — DETERMINISTIC, fork-safe head derivation. The head of a chain is
        the unique tip: the ``receipt_hash`` that NO other row in the chain
        references as its ``prev_receipt_hash`` (i.e. it has no successor). The
        old rule (``ORDER BY run_started_at DESC LIMIT 1``) is non-deterministic
        once the chain forks — two rows sharing a ``prev_receipt_hash`` (which
        happens across a process recreate that lost the in-memory head, or two
        concurrent same-analyst runs racing the prev pointer) leave two leaves,
        and "most recent" can flip between recreates, picking a NON-tip and
        deepening the fork on the next ``record()``.

        A healthy linear chain has exactly one tip → this returns it. A FORKED
        chain has several tips; we pick ONE deterministically (newest by
        ``run_started_at``, ties broken by ``run_id`` text) so the choice is
        STABLE across recreate and every future run extends the SAME tip rather
        than spraying new leaves. The actual fork relink is a forward-only
        migration (roadmap 0050); this stops the runtime from MAKING IT WORSE.
        """
        if analyst_id in self._heads:
            return self._heads[analyst_id]
        async with self._pool.acquire() as conn:
            # Tip = a receipt_hash that is not the prev_receipt_hash of any other
            # row for this analyst. ``DISTINCT`` on the inner set guards the
            # NOT IN against a NULL row exploding the predicate. There can be >1
            # tip after a fork; ORDER BY makes the pick deterministic (stable
            # across recreate) — NOT "most recent overall row".
            row = await conn.fetchrow(
                """
                SELECT t.receipt_hash
                FROM analyst_traces t
                WHERE t.analyst_id = $1
                  AND t.receipt_hash NOT IN (
                        SELECT DISTINCT p.prev_receipt_hash
                        FROM analyst_traces p
                        WHERE p.analyst_id = $1
                          AND p.prev_receipt_hash IS NOT NULL
                  )
                ORDER BY t.run_started_at DESC, t.run_id::text DESC
                LIMIT 1
                """,
                analyst_id,
            )
            if row is not None:
                self._heads[analyst_id] = row["receipt_hash"]
            else:
                # No tip resolves only when there are zero rows OR every row is
                # referenced as some row's prev (a cycle — never produced by
                # this writer, but degrade safely). Fall back to the latest row
                # so the chain still advances rather than re-rooting at ZERO.
                fallback = await conn.fetchrow(
                    """
                    SELECT receipt_hash
                    FROM analyst_traces
                    WHERE analyst_id = $1
                    ORDER BY run_started_at DESC, run_id::text DESC
                    LIMIT 1
                    """,
                    analyst_id,
                )
                self._heads[analyst_id] = (
                    fallback["receipt_hash"] if fallback else ZERO_HASH
                )
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

    async def head_tip_count(self, analyst_id: str | None = None) -> int:
        """Number of chain TIPS for an analyst — D11 fork observability.

        A tip is a ``receipt_hash`` referenced by no other row's
        ``prev_receipt_hash``. A healthy linear chain has exactly one tip; ``>1``
        means the chain has FORKED into a multi-tip tree (the D11 condition —
        e.g. cross_source_dedup at 27 tips, country_critic at 6). ``0`` means no
        rows yet. This is the runtime-side counterpart to the integrity_sweep
        dangling-edge audit: a cheap probe an operator / health check can call to
        confirm the deterministic head-derivation is holding the fork count flat
        rather than growing it.

        ``analyst_id`` defaults to the chain's bound analyst when one was passed
        to the constructor.
        """
        aid = analyst_id if analyst_id is not None else self._analyst_id
        if aid is None:
            raise ValueError(
                "head_tip_count requires an analyst_id (none bound to this chain)"
            )
        async with self._pool.acquire() as conn:
            n = await conn.fetchval(
                """
                SELECT count(*)
                FROM analyst_traces t
                WHERE t.analyst_id = $1
                  AND t.receipt_hash NOT IN (
                        SELECT DISTINCT p.prev_receipt_hash
                        FROM analyst_traces p
                        WHERE p.analyst_id = $1
                          AND p.prev_receipt_hash IS NOT NULL
                  )
                """,
                aid,
            )
        return int(n or 0)

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
        prompt_sha256: str | None = None,
    ) -> tuple[str, str]:
        """Compute receipt hash, write analyst_traces row, advance head.

        Returns ``(receipt_hash, prev_receipt_hash)``. Raises ``asyncpg``
        errors on write failure — caller decides DLQ vs retry.

        Per-analyst lock guarantees sequential chaining for a single
        analyst across concurrent calls; cross-analyst writes proceed
        independently. The runtime actor wires this AFTER the analyst-
        output INSERT so the chain row carries ``output_row_refs`` for
        the row(s) just produced (per L-107 §7 lineage-into-chain).

        ``prompt_sha256`` (RUST-5) is the sha256 of the FULL, untruncated
        prompt — see ``run_accounting.current_prompt_rendered`` — recorded
        alongside a possibly-capped ``prompt_rendered`` so a truncated row
        is still byte-verifiable against a re-render. Like ``llm_calls`` /
        ``tool_calls`` it is NOT part of the receipt-hash payload
        (supplementary provenance, not chain material) — only
        ``prompt_rendered`` itself feeds ``compute_receipt_hash``.
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
                        prompt_module_hash, prompt_rendered, prompt_sha256,
                        intermediate_steps, llm_calls, tool_calls,
                        output_row_refs, output_payload,
                        status, error_payload,
                        run_started_at, run_ended_at,
                        receipt_hash, prev_receipt_hash
                    ) VALUES (
                        $1, $2, $3,
                        $4, $5,
                        $6, $7::jsonb,
                        $8, $9, $10,
                        $11::jsonb, $12::jsonb, $13::jsonb,
                        $14, $15::jsonb,
                        $16, $17::jsonb,
                        $18, $19,
                        $20, $21
                    )
                    """,
                    run_id, analyst_id, analyst_version,
                    target_id, cadence_trigger,
                    list(input_row_refs),
                    json.dumps(input_payload or {}, default=_json_default),
                    prompt_module_hash, prompt_rendered, prompt_sha256,
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

    async def append_llm_calls(
        self,
        *,
        run_id: UUID,
        calls: Sequence[dict[str, Any]],
    ) -> int:
        """S-4 — append LLM calls to an ALREADY-WRITTEN trace's ``llm_calls``.

        Returns the number of entries appended (0 when ``calls`` is empty or
        the run_id matches no row).

        Why an UPDATE rather than a later ``record``: the analyst run must write
        its trace row BEFORE the faithfulness verify pass, because V-B's
        absence-slice check reads this run's ``input_row_refs`` back over the
        same connection. The judge calls therefore happen after the row exists.
        Reordering the write is not an option; appending to it is.

        **This does NOT touch the receipt chain.** ``compute_receipt_hash``
        covers run_id / analyst id+version / input_row_refs / prompt_module_hash
        / prompt_rendered / output_row_refs / output_payload / run_ended_at /
        prev_receipt_hash — ``llm_calls`` is not in the payload, by design (it is
        instrumentation, not provenance). So the row's ``receipt_hash`` stays
        valid and the chain stays verifiable, which is the whole reason this can
        be an in-place append instead of a schema or chain change.

        Idempotence is the CALLER's job: this appends unconditionally, so a
        retried caller double-counts. The one caller takes a per-run watermark
        and appends once.
        """
        if not calls:
            return 0
        async with self._pool.acquire() as conn:
            updated = await conn.execute(
                """
                UPDATE analyst_traces
                   SET llm_calls = COALESCE(llm_calls, '[]'::jsonb)
                                   || $2::jsonb
                 WHERE run_id = $1
                """,
                run_id,
                json.dumps(list(calls), default=_json_default),
            )
        # asyncpg returns the command tag ("UPDATE 1"); a 0 means the trace row
        # is missing, which happens legitimately on a TRACE_ONLY run or when the
        # receipt write itself failed earlier in the turn.
        if updated.endswith(" 0"):
            return 0
        return len(calls)

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
        prompt_sha256: str | None = None,
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
            prompt_sha256=prompt_sha256,
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
