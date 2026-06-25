# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Job-plane substrate writes — idempotency ledger + derived-signal landing.

Two persistence responsibilities live here, both against the source-first
substrate (the new ``signals`` table from migration 0024 + a job-local
idempotency ledger):

  1. **Idempotency ledger** (``legba_jobs``). A job's ``idempotency_key`` is
     claimed once; a redelivery / re-enqueue of the same key observes the
     prior terminal result instead of doing the work twice. This is the
     P-07 acceptance criterion "jobs idempotent on idempotency_key".

     The ledger table is NOT in the frozen 0024 migration (the job plane is a
     W2 fan-out module, not part of the substrate-trunk freeze), so this
     module owns it and creates it idempotently via ``ensure_schema`` —
     ``CREATE TABLE IF NOT EXISTS``, additive, collision-free with the frozen
     migration chain. No edit to ``migrations/`` (frozen).

  2. **Derived-signal landing** (``signals``). A ``process_media`` worker's
     output lands as a DERIVED signal: ``produced_by_kind='job'``,
     ``produced_by_id=<job_id>``, ``derived_from=[<raw signal id>]`` — the
     P-01 provenance contract. Lineage walks finding → derived → raw.

The insert maps :class:`legba.data.sources._contract.Signal` (the frozen
re-cut contract) onto the new ``signals`` columns. There is no shared
Signal→row converter in the trunk yet (P-04 only landed a descriptor-row
converter), so this module owns the mapping for the derived rows it writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from ..sources._contract import Signal
from .envelope import JobEnvelope, JobResult


# Idempotency-ledger states.
#   claimed   — a worker holds the key and is doing the work.
#   completed — terminal success; the result is cached on the row.
#   failed    — terminal failure (after exhausting retries / fatal handler error).
#   released  — the claim was dropped for retry (transient error or a reaped
#               zombie whose delivery budget is NOT spent). A redelivery — or a
#               reaper-driven re-enqueue — RECLAIMS a 'released' row in place
#               (status flips back to 'claimed'); it is NOT terminal. The
#               reaper marks zombies 'released' rather than DELETEing them
#               (C-3 / 2.6) so the row + its cached envelope survive for the
#               re-enqueued delivery to reclaim — a DELETE here loses the
#               envelope and the job vanishes silently when no redelivery comes.
#
# Schema-qualified to ``public`` (C-3 / 2.7): the data pool pins
# ``search_path = ag_catalog, "$user", public`` for AGE, so an UNQUALIFIED
# ``CREATE TABLE legba_jobs`` resolves to the first writable schema —
# ``ag_catalog`` — not ``public``. A fresh deploy would then land the ledger
# in the graph schema, invisible to every ``public``-qualified reader and to
# the substrate's ``signals`` neighbours. Every statement below names
# ``public.legba_jobs`` explicitly so a fresh create lands in ``public``
# regardless of the pinned search_path. (An already-misplaced live table is
# relocated by an operator ``ALTER TABLE ... SET SCHEMA public`` step — not
# done here.)
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS public.legba_jobs (
    idempotency_key  TEXT PRIMARY KEY,
    job_id           UUID NOT NULL,
    job_kind         TEXT NOT NULL,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    requested_by     TEXT NOT NULL DEFAULT 'system',
    budget_account   TEXT NOT NULL DEFAULT 'system',
    status           TEXT NOT NULL DEFAULT 'claimed',
    attempts         INTEGER NOT NULL DEFAULT 0,
    result           JSONB,
    -- Full enqueued envelope, cached on claim so the reaper can RE-ENQUEUE a
    -- zombie's work (the ledger row alone lacks ``input_refs`` / ``deadline``,
    -- which the handler needs). C-3 / 2.6.
    envelope         JSONB,
    claimed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Additive column for ledgers created before the envelope cache existed.
ALTER TABLE public.legba_jobs ADD COLUMN IF NOT EXISTS envelope JSONB;
CREATE INDEX IF NOT EXISTS legba_jobs_job_id_idx   ON public.legba_jobs(job_id);
CREATE INDEX IF NOT EXISTS legba_jobs_status_idx   ON public.legba_jobs(status);
CREATE INDEX IF NOT EXISTS legba_jobs_kind_idx     ON public.legba_jobs(job_kind);
"""


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of an idempotency-key claim attempt."""

    acquired: bool                  # True → this caller does the work.
    status: str                     # claimed | completed | failed (current row state).
    prior_result: JobResult | None  # set when a terminal row already exists.


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


class JobStore:
    """Substrate-side helpers for the job plane.

    Holds no connection of its own — every method takes an ``asyncpg``
    connection so the caller controls pooling/transactions (matches the
    ``legba.data.provenance.writes`` convention).
    """

    @staticmethod
    async def ensure_schema(conn: asyncpg.Connection) -> None:
        """Create the idempotency ledger if absent. Idempotent + additive."""
        await conn.execute(LEDGER_DDL)

    # ------------------------------------------------------------------
    # Idempotency ledger
    # ------------------------------------------------------------------

    @staticmethod
    async def claim(
        conn: asyncpg.Connection, env: JobEnvelope
    ) -> ClaimResult:
        """Atomically claim the envelope's idempotency_key.

        Uses ``INSERT ... ON CONFLICT DO UPDATE`` with a guarded predicate:

          * No row yet → INSERT a ``claimed`` row (this caller wins,
            ``acquired=True``).
          * Row exists and is ``released`` (a retry-eligible zombie the
            reaper parked, or a transient-error release) → flip it back to
            ``claimed`` in the SAME statement and this caller RECLAIMS it
            (``acquired=True``). The cached envelope is refreshed and the
            claim clock resets so the reaper's lease is measured from this
            attempt.
          * Row exists and is ``claimed`` / ``completed`` / ``failed`` → the
            guarded UPDATE matches nothing, nothing is returned, and this
            caller observes the existing row (``acquired=False``). Terminal
            rows surface their cached result so the redelivery short-circuits.

        Competing-consumer-safe: the conflicting INSERT serialises on the PK,
        so exactly one of two racing workers wins the reclaim; the loser's
        guarded UPDATE no longer matches ``status = 'released'`` and it reads
        the winner's ``claimed`` row.

        The full envelope is cached on the row (``envelope`` column) so the
        reaper can re-enqueue a zombie's work — the ledger's scalar columns
        alone lack ``input_refs`` / ``deadline``.
        """
        env_json = env.model_dump_json()
        acquired_key = await conn.fetchval(
            """
            INSERT INTO public.legba_jobs (
                idempotency_key, job_id, job_kind, tenant_id,
                requested_by, budget_account, status, attempts, envelope
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'claimed', $7, $8::jsonb)
            ON CONFLICT (idempotency_key) DO UPDATE
               SET status     = 'claimed',
                   attempts   = EXCLUDED.attempts,
                   envelope   = EXCLUDED.envelope,
                   claimed_at = NOW(),
                   updated_at = NOW()
             WHERE public.legba_jobs.status = 'released'
            RETURNING idempotency_key
            """,
            env.idempotency_key,
            env.job_id,
            env.job_kind,
            env.tenant_id,
            env.requested_by,
            env.budget_account,
            env.attempts,
            env_json,
        )
        if acquired_key is not None:
            # Either a fresh INSERT or a reclaim of a 'released' row — this
            # caller now holds the claim.
            return ClaimResult(acquired=True, status="claimed", prior_result=None)

        # The guarded UPSERT matched nothing → a live 'claimed' or terminal
        # row already exists. Read its state for the caller's decision.
        row = await conn.fetchrow(
            "SELECT status, result FROM public.legba_jobs WHERE idempotency_key = $1",
            env.idempotency_key,
        )
        if row is None:  # pragma: no cover — vanishingly rare delete-race
            return ClaimResult(acquired=False, status="unknown", prior_result=None)
        prior = JobResult.from_json_row(row["result"]) if row["result"] else None
        return ClaimResult(
            acquired=False, status=row["status"], prior_result=prior
        )

    @staticmethod
    async def complete(
        conn: asyncpg.Connection, key: str, result: JobResult
    ) -> None:
        """Mark a claimed key terminal-success and cache its result."""
        await conn.execute(
            """
            UPDATE public.legba_jobs
               SET status = 'completed',
                   result = $2::jsonb,
                   attempts = attempts + 1,
                   updated_at = NOW()
             WHERE idempotency_key = $1
            """,
            key,
            json.dumps(result.model_dump(mode="json"), default=_json_default),
        )

    @staticmethod
    async def fail(
        conn: asyncpg.Connection, key: str, result: JobResult
    ) -> None:
        """Mark a claimed key terminal-failure and cache its result."""
        await conn.execute(
            """
            UPDATE public.legba_jobs
               SET status = 'failed',
                   result = $2::jsonb,
                   attempts = attempts + 1,
                   updated_at = NOW()
             WHERE idempotency_key = $1
            """,
            key,
            json.dumps(result.model_dump(mode="json"), default=_json_default),
        )

    @staticmethod
    async def release(conn: asyncpg.Connection, key: str) -> None:
        """Drop a claim so the work can be re-attempted.

        Used when a claimed job hits a transient error and should be retried
        by a later redelivery rather than cached as a terminal failure.

        The row is marked ``released`` (NOT deleted) so its cached envelope
        survives and :meth:`claim` can reclaim it in place when the
        redelivery — or a reaper-driven re-enqueue — arrives (C-3 / 2.6).
        Deleting it would lose the envelope and, if no redelivery comes,
        the job would vanish with no terminal ledger row.
        """
        await conn.execute(
            """
            UPDATE public.legba_jobs
               SET status = 'released',
                   updated_at = NOW()
             WHERE idempotency_key = $1 AND status = 'claimed'
            """,
            key,
        )

    @staticmethod
    async def reap_stale_claims(
        conn: asyncpg.Connection,
        *,
        lease_seconds: float,
        max_deliver: int,
        reaper_id: str = "reaper",
    ) -> list[dict[str, Any]]:
        """Expire rows stuck ``claimed`` past the delivery-budget lease.

        A worker that crashes mid-job leaves its row ``claimed`` forever:
        JetStream redeliveries can't claim it (the PK is held), and once
        ``max_deliver`` is exhausted no further redelivery arrives — the
        ledger row is a zombie. The lease is the redelivery budget,
        ``ack_wait × max_deliver`` seconds: past that, the claim holder is
        presumed dead and the broker has stopped redelivering.

        Disposition per stale row (the lease guard makes this race-safe —
        a live-but-slow worker would have acked/released within ack_wait):

          * ``attempts < max_deliver`` → **released back to retryable**
            (status flipped to ``released``, the row + its cached envelope
            KEPT — C-3 / 2.6). The reaper does NOT DELETE: with bare-nak
            redelivery the broker may already have spent its budget, so
            "a pending redelivery will claim it" is not guaranteed — the
            caller re-enqueues the returned envelope, and :meth:`claim`
            reclaims the ``released`` row in place. DELETEing here would
            drop the envelope and the job would vanish with no terminal
            ledger row.
          * ``attempts >= max_deliver`` → **terminal failed**: the
            delivery budget is spent, so the row is marked ``failed``
            with an explicit lease-expiry result (never silently
            dropped).

        Returns one dict per reaped row:
        ``{"idempotency_key", "job_id", "job_kind", "attempts",
        "disposition", "envelope"}`` (``disposition`` is ``"released"`` or
        ``"failed"``; ``envelope`` is the cached enqueue payload — a JSON
        string the caller re-enqueues for ``released`` rows, ``None`` for
        ``failed`` rows).

        Both dispositions are single atomic statements whose WHERE
        re-checks ``status = 'claimed'`` — concurrent completion by a
        slow-but-alive worker wins the race (its UPDATE flips the status
        first and the reaper's predicate no longer matches, or vice
        versa); nothing is reaped twice.
        """
        reaped: list[dict[str, Any]] = []

        # Terminal branch first: delivery budget spent → failed, with an
        # explicit lease-expiry JobResult cached on the row (never a
        # silent drop). The result blob is built in SQL so the whole
        # disposition is one atomic statement per row set.
        failed_rows = await conn.fetch(
            """
            UPDATE public.legba_jobs
               SET status = 'failed',
                   result = jsonb_build_object(
                       'job_id', job_id::text,
                       'job_kind', job_kind,
                       'status', 'failed',
                       'output_refs', '{}'::jsonb,
                       'error', 'lease expired: claimed > '
                                || $1::numeric::text
                                || 's (ack_wait x max_deliver) with delivery '
                                || 'budget spent (attempts=' || attempts
                                || ' >= max_deliver=' || $2::int
                                || '); claim holder presumed dead',
                       'worker_id', $3::text,
                       'finished_at', NOW()
                   ),
                   updated_at = NOW()
             WHERE status = 'claimed'
               AND claimed_at < NOW() - make_interval(secs => $1)
               AND attempts >= $2
            RETURNING idempotency_key, job_id, job_kind, attempts
            """,
            float(lease_seconds),
            int(max_deliver),
            reaper_id,
        )
        for row in failed_rows:
            reaped.append(
                {
                    "idempotency_key": row["idempotency_key"],
                    "job_id": row["job_id"],
                    "job_kind": row["job_kind"],
                    "attempts": int(row["attempts"] or 0),
                    "disposition": "failed",
                    "envelope": None,
                }
            )

        # Retryable branch: delivery budget not spent → mark the row
        # ``released`` (NOT DELETE — C-3 / 2.6) and hand the cached envelope
        # back so the caller re-enqueues the work. A bare-nak storm can spend
        # the broker's redelivery budget before the reaper runs, so we can't
        # rely on "a pending redelivery will claim it"; re-enqueue guarantees a
        # fresh delivery, and :meth:`claim` reclaims the surviving ``released``
        # row in place. Keeping the row also preserves the audit trail.
        released_rows = await conn.fetch(
            """
            UPDATE public.legba_jobs
               SET status = 'released',
                   updated_at = NOW()
             WHERE status = 'claimed'
               AND claimed_at < NOW() - make_interval(secs => $1)
               AND attempts < $2
            RETURNING idempotency_key, job_id, job_kind, attempts, envelope
            """,
            float(lease_seconds),
            int(max_deliver),
        )
        for row in released_rows:
            reaped.append(
                {
                    "idempotency_key": row["idempotency_key"],
                    "job_id": row["job_id"],
                    "job_kind": row["job_kind"],
                    "attempts": int(row["attempts"] or 0),
                    "disposition": "released",
                    "envelope": row["envelope"],
                }
            )
        return reaped

    # ------------------------------------------------------------------
    # Raw-signal read + derived-signal landing
    # ------------------------------------------------------------------

    @staticmethod
    async def get_signal(
        conn: asyncpg.Connection, signal_id: UUID
    ) -> dict[str, Any] | None:
        """Fetch a raw signal row (the lineage parent for a derived signal)."""
        row = await conn.fetchrow(
            "SELECT * FROM signals WHERE id = $1", signal_id
        )
        return dict(row) if row is not None else None

    @staticmethod
    async def land_derived_signal(
        conn: asyncpg.Connection, signal: Signal
    ) -> UUID:
        """Insert a DERIVED signal into the source-first ``signals`` table.

        The mapping is column-for-column with migration 0024's ``signals``.
        ``signal.signal_id`` is the row PK; provenance (``produced_by_kind`` /
        ``produced_by_id`` / ``derived_from``) is carried straight through so
        lineage walks derived → raw. The write is idempotent on the PK — a
        replayed job that reuses the same ``signal_id`` (the worker derives it
        deterministically from job_id) is a no-op via ``ON CONFLICT``.
        """
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_id, produced_by_kind,
                fetched_at, owner_tenant, modality, mime_type, media_ref,
                embedding_ref, retention_class, media_ref_expires_at, object_ref,
                payload, canonical_url, language_hint, raw_provenance,
                language, geo, tags, entity_classes, source_credibility,
                content_hash, canonical_signal_id, derived_from, schema_uri
            )
            VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12, $13, $14,
                $15::jsonb, $16, $17, $18::jsonb,
                $19, $20, $21, $22, $23,
                $24, $25, $26, $27
            )
            ON CONFLICT (id) DO NOTHING
            """,
            signal.signal_id,
            signal.source_id,
            signal.source_version,
            signal.produced_by_id,
            signal.produced_by_kind,
            signal.fetched_at,
            signal.owner_tenant,
            signal.modality,
            signal.mime_type,
            signal.media_ref,
            signal.embedding_ref,
            signal.retention_class,
            signal.media_ref_expires_at,
            signal.object_ref,
            json.dumps(signal.payload, default=_json_default),
            signal.canonical_url,
            signal.language_hint,
            json.dumps(signal.raw_provenance, default=_json_default),
            signal.language,
            list(signal.geo),
            list(signal.tags),
            list(signal.entity_classes),
            signal.source_credibility,
            signal.content_hash,
            signal.canonical_signal_id,
            list(signal.derived_from),
            signal.schema_uri,
        )
        return signal.signal_id


__all__ = ["ClaimResult", "JobStore", "LEDGER_DDL"]
