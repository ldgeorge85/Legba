# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audit-chain checkpointer — Ed25519 signs the per-analyst chain head.

Per L-107 §7 / topology §9.1: every minute (or per-N-traces, whichever
fires first per OBS-6) the runtime control plane signs the current chain
head per analyst per stream and writes one row to ``audit_checkpoints``.
Optionally publishes on NATS for cross-pillar verifiers (e.g. Axis).

Phase 1 scope: the asyncio background task, the Ed25519 signing wrapper,
and the row writer. Phase 5 runtime starts it (one task per shard).

The signing key is **deployment-level**, not per-analyst. It reuses the
existing A2A-envelope signing key per L-107 §8 once L-110's signing helper
lands; for L-114 testing it accepts a raw 32-byte seed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

import asyncpg
from nacl.signing import SigningKey, VerifyKey

from ._core import canonical_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ed25519 signing wrapper (matches Mnemosyne D5 pattern; reuses cryptography
# rather than introducing a new dependency).
# ---------------------------------------------------------------------------


class Ed25519Signer:
    """Wraps a 32-byte seed → Ed25519 private key.

    Constructed by the runtime control plane from the deployment signing
    key store (vault / keyring). For tests, callers pass a deterministic
    seed.
    """

    def __init__(self, seed: bytes, *, did: str):
        if len(seed) != 32:
            raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(seed)}")
        self._key: SigningKey = SigningKey(seed)
        self._did = did

    @property
    def did(self) -> str:
        return self._did

    def public_key(self) -> VerifyKey:
        return self._key.verify_key

    def sign(self, data: bytes) -> bytes:
        """Return the 64-byte raw Ed25519 signature (not signed message)."""
        return bytes(self._key.sign(data).signature)


# ---------------------------------------------------------------------------
# Checkpointer
# ---------------------------------------------------------------------------


@dataclass
class CheckpointerConfig:
    interval_seconds: float = 60.0          # per-minute floor (OBS-6)
    max_traces_between: int = 100           # per-100-runs ceiling (OBS-6)
    nats_subject_pattern: str | None = "legba.audit.checkpoint.{analyst_id}"


# Callable[[subject, payload_bytes], Awaitable[None]] — pluggable so tests
# can capture without a NATS connection.
NatsPublishFn = Callable[[str, bytes], Awaitable[None]]


@dataclass
class _AnalystCheckpointState:
    last_head: str | None = None
    last_trace_count: int = 0
    last_signed_at: datetime | None = None


class AuditCheckpointer:
    """Asyncio background task: signs + persists per-analyst chain heads.

    Lifecycle:
        cp = AuditCheckpointer(pool, signer, cfg, publish=publish_fn)
        await cp.start()    # spawn the task
        ...
        await cp.stop()     # cancel + join

    The task wakes on the lesser of (interval, OS scheduler) and reads
    each analyst's current chain head from ``analyst_traces`` (latest row
    per analyst). If head changed OR ``max_traces_between`` runs have
    accumulated since last checkpoint, sign + write + publish.

    Phase 1 sweeps the whole ``analyst_traces`` table per tick (small N at
    expected scale); Phase 5 may swap this for a NATS-driven push if the
    sweep grows expensive.
    """

    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        signer: Ed25519Signer,
        config: CheckpointerConfig | None = None,
        *,
        publish: NatsPublishFn | None = None,
    ):
        self._pool = pg_pool
        self._signer = signer
        self._cfg = config or CheckpointerConfig()
        self._publish = publish
        self._state: dict[str, _AnalystCheckpointState] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("AuditCheckpointer already running")
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="legba-audit-checkpointer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None

    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick()
                except Exception:  # pragma: no cover — defensive
                    logger.exception("audit checkpointer tick failed")
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._cfg.interval_seconds
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Per-tick logic (also exposed for tests to drive deterministically).
    # ------------------------------------------------------------------

    async def tick(self) -> list[UUID]:
        """Run one checkpoint pass; return ids of newly written checkpoints."""
        written: list[UUID] = []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    t.analyst_id,
                    t.receipt_hash      AS head,
                    counts.n            AS trace_count
                FROM (
                    SELECT DISTINCT ON (analyst_id)
                        analyst_id, receipt_hash, run_started_at
                    FROM analyst_traces
                    ORDER BY analyst_id, run_started_at DESC
                ) t
                JOIN (
                    SELECT analyst_id, COUNT(*)::BIGINT AS n
                    FROM analyst_traces
                    GROUP BY analyst_id
                ) counts USING (analyst_id)
                """
            )
            for row in rows:
                analyst_id: str = row["analyst_id"]
                head: str = row["head"]
                trace_count: int = int(row["trace_count"])
                state = self._state.setdefault(
                    analyst_id, _AnalystCheckpointState()
                )

                head_changed = state.last_head != head
                runs_since = trace_count - state.last_trace_count
                ceiling_hit = runs_since >= self._cfg.max_traces_between

                if not (head_changed or ceiling_hit):
                    continue
                if not head_changed and runs_since == 0:
                    continue

                checkpoint_id = await self._sign_and_write(
                    conn, analyst_id=analyst_id, head=head, trace_count=trace_count
                )
                state.last_head = head
                state.last_trace_count = trace_count
                state.last_signed_at = datetime.now(tz=timezone.utc)
                written.append(checkpoint_id)
        return written

    async def _sign_and_write(
        self,
        conn: asyncpg.Connection,
        *,
        analyst_id: str,
        head: str,
        trace_count: int,
    ) -> UUID:
        checkpointed_at = datetime.now(tz=timezone.utc)
        payload = {
            "analyst_id": analyst_id,
            "chain_head_hash": head,
            "trace_count": trace_count,
            "checkpointed_at": checkpointed_at.isoformat(),
            "signer_did": self._signer.did,
        }
        signature = self._signer.sign(canonical_json(payload))

        new_id = await conn.fetchval(
            """
            INSERT INTO audit_checkpoints
              (analyst_id, chain_head_hash, trace_count,
               checkpointed_at, signature, signer_did)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            analyst_id, head, trace_count,
            checkpointed_at, signature, self._signer.did,
        )

        if self._publish is not None and self._cfg.nats_subject_pattern:
            subject = self._cfg.nats_subject_pattern.format(analyst_id=analyst_id)
            envelope = {
                **payload,
                "signature_b64": signature.hex(),
                "checkpoint_id": str(new_id),
            }
            try:
                await self._publish(subject, json.dumps(envelope).encode("utf-8"))
            except Exception:  # pragma: no cover — NATS optional
                logger.exception("checkpoint publish failed analyst=%s", analyst_id)

        return new_id
