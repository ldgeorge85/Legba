# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Actor persistent state — Postgres-backed.

Mirrors the Dapr state-store contract (per legba_runtime_spec.md §2.2)
but lives in a plain ``actor_state`` table so the Phase 5a spike can run
end-to-end without a Dapr sidecar. When the spike hardens, a Dapr state-
store component pointed at the same table is a drop-in replacement.

Per-source cursors are nested inside the target actor's state (matching
spec §2.2's ``TargetActorState.source_cursors`` shape). Filter handlers
in the pipeline use their own ``state_store`` (per L-102) — those are
backed by the same table under a separate key prefix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import asyncpg


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class SourceCursor:
    """Per-source-binding cursor state. Open dict; sources own the shape."""

    source_id: str
    cursor: dict[str, Any] = field(default_factory=dict)
    last_pulled_at: datetime | None = None
    rows_pulled: int = 0
    last_error: str | None = None


@dataclass
class ActorStateRecord:
    """One row in `actor_state` — the typed-state pair per spec §2.2."""

    actor_id: str
    actor_kind: str           # "target" | "analyst"
    descriptor_id: str
    descriptor_version: str   # content hash
    lifecycle: str            # draft|configured|active|paused|retired|error
    last_run_at: datetime | None = None
    last_outcome: str | None = None
    cooldown_until: datetime | None = None
    error_count: int = 0
    last_error: str | None = None
    source_cursors: dict[str, SourceCursor] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)


SCHEMA = """
CREATE TABLE IF NOT EXISTS public.actor_state (
    actor_id            TEXT PRIMARY KEY,
    actor_kind          TEXT NOT NULL,
    descriptor_id       TEXT NOT NULL,
    descriptor_version  TEXT NOT NULL,
    lifecycle           TEXT NOT NULL,
    last_run_at         TIMESTAMPTZ,
    last_outcome        TEXT,
    cooldown_until      TIMESTAMPTZ,
    error_count         INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    source_cursors      JSONB NOT NULL DEFAULT '{}'::JSONB,
    extras              JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS actor_state_kind_idx ON public.actor_state(actor_kind);
CREATE INDEX IF NOT EXISTS actor_state_lifecycle_idx ON public.actor_state(lifecycle);
CREATE INDEX IF NOT EXISTS actor_state_descriptor_idx ON public.actor_state(descriptor_id);

CREATE TABLE IF NOT EXISTS public.actor_filter_state (
    -- per-pipeline-instance state for filter handlers (language_detect cursors,
    -- dedupe seen-hashes, etc.). The runtime constructs InMemoryStateStore over
    -- this when the filter's transform() runs.
    actor_id   TEXT NOT NULL,
    filter_id  TEXT NOT NULL,
    state_key  TEXT NOT NULL,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (actor_id, filter_id, state_key)
);
"""


def _serialize_cursors(cursors: Mapping[str, SourceCursor]) -> str:
    out = {}
    for source_id, cur in cursors.items():
        out[source_id] = {
            "source_id": cur.source_id,
            "cursor": cur.cursor,
            "last_pulled_at": cur.last_pulled_at.isoformat() if cur.last_pulled_at else None,
            "rows_pulled": cur.rows_pulled,
            "last_error": cur.last_error,
        }
    return json.dumps(out)


def _deserialize_cursors(raw: Any) -> dict[str, SourceCursor]:
    if not raw:
        return {}
    data = raw if isinstance(raw, dict) else json.loads(raw)
    out: dict[str, SourceCursor] = {}
    for source_id, entry in data.items():
        ts_raw = entry.get("last_pulled_at")
        ts = datetime.fromisoformat(ts_raw) if ts_raw else None
        out[source_id] = SourceCursor(
            source_id=entry.get("source_id", source_id),
            cursor=entry.get("cursor", {}) or {},
            last_pulled_at=ts,
            rows_pulled=int(entry.get("rows_pulled", 0)),
            last_error=entry.get("last_error"),
        )
    return out


class ActorStateStore:
    """Async accessor backed by Postgres ``actor_state`` table.

    Single-activation isn't enforced here — that's Dapr's job in production.
    For the spike each actor instance holds a per-actor asyncio.Lock and
    serializes its own concurrent invocations. Cross-process safety would
    require ``SELECT ... FOR UPDATE`` per row; that's a Phase 5 hardening
    item, called out explicitly in the actor classes.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def get(self, actor_id: str) -> ActorStateRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM public.actor_state WHERE actor_id = $1",
                actor_id,
            )
            if row is None:
                return None
            return ActorStateRecord(
                actor_id=row["actor_id"],
                actor_kind=row["actor_kind"],
                descriptor_id=row["descriptor_id"],
                descriptor_version=row["descriptor_version"],
                lifecycle=row["lifecycle"],
                last_run_at=row["last_run_at"],
                last_outcome=row["last_outcome"],
                cooldown_until=row["cooldown_until"],
                error_count=int(row["error_count"]),
                last_error=row["last_error"],
                source_cursors=_deserialize_cursors(row["source_cursors"]),
                extras=row["extras"] if isinstance(row["extras"], dict)
                       else (json.loads(row["extras"]) if row["extras"] else {}),
                updated_at=row["updated_at"],
            )

    async def upsert(self, rec: ActorStateRecord) -> None:
        rec.updated_at = _utcnow()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.actor_state (
                    actor_id, actor_kind, descriptor_id, descriptor_version,
                    lifecycle, last_run_at, last_outcome, cooldown_until,
                    error_count, last_error, source_cursors, extras, updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11::jsonb, $12::jsonb, $13
                )
                ON CONFLICT (actor_id) DO UPDATE SET
                    actor_kind = EXCLUDED.actor_kind,
                    descriptor_id = EXCLUDED.descriptor_id,
                    descriptor_version = EXCLUDED.descriptor_version,
                    lifecycle = EXCLUDED.lifecycle,
                    last_run_at = EXCLUDED.last_run_at,
                    last_outcome = EXCLUDED.last_outcome,
                    cooldown_until = EXCLUDED.cooldown_until,
                    error_count = EXCLUDED.error_count,
                    last_error = EXCLUDED.last_error,
                    source_cursors = EXCLUDED.source_cursors,
                    extras = EXCLUDED.extras,
                    updated_at = EXCLUDED.updated_at
                """,
                rec.actor_id,
                rec.actor_kind,
                rec.descriptor_id,
                rec.descriptor_version,
                rec.lifecycle,
                rec.last_run_at,
                rec.last_outcome,
                rec.cooldown_until,
                rec.error_count,
                rec.last_error,
                _serialize_cursors(rec.source_cursors),
                json.dumps(rec.extras),
                rec.updated_at,
            )

    async def list_live_siblings(
        self,
        *,
        actor_kind: str,
        descriptor_id: str,
        exclude_actor_id: str,
    ) -> list[ActorStateRecord]:
        """Non-retired rows for the same descriptor under a DIFFERENT actor_id.

        The actor_id grammar embeds ``content_hash[:16]``, so a descriptor
        edit mints a new actor_id while the old actor keeps running. The
        reconcile loop calls this before CREATE to find those version-drift
        leftovers and retire them — without it every descriptor edit leaves
        the prior actor double-running (review 2026-06 G1).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT actor_id FROM public.actor_state
                WHERE actor_kind = $1 AND descriptor_id = $2
                  AND actor_id <> $3 AND lifecycle <> 'retired'
                """,
                actor_kind,
                descriptor_id,
                exclude_actor_id,
            )
        out: list[ActorStateRecord] = []
        for r in rows:
            rec = await self.get(r["actor_id"])
            if rec is not None:
                out.append(rec)
        return out

    async def list_by_lifecycle(self, lifecycle: str) -> list[ActorStateRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT actor_id FROM public.actor_state WHERE lifecycle = $1",
                lifecycle,
            )
        out: list[ActorStateRecord] = []
        for r in rows:
            rec = await self.get(r["actor_id"])
            if rec is not None:
                out.append(rec)
        return out


class FilterStateStore:
    """Per-filter state — backed by ``actor_filter_state``.

    Satisfies :class:`legba.data.sources._contract.StateStore` for filter
    + source handlers. Keyed by (actor_id, filter_id) so the same filter
    in two different targets does not share state.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        actor_id: str,
        filter_id: str,
    ) -> None:
        self._pool = pool
        self._actor_id = actor_id
        self._filter_id = filter_id

    async def get(self, key: str) -> Any:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT value FROM public.actor_filter_state
                WHERE actor_id = $1 AND filter_id = $2 AND state_key = $3
                """,
                self._actor_id,
                self._filter_id,
                key,
            )
            if row is None:
                return None
            raw = row["value"]
            # The pool's jsonb codec (``PostgresStore._init_connection``,
            # registered unconditionally on every connection) already decodes
            # the ``jsonb`` column into a native Python value for EVERY JSON
            # shape — object, array, string, number, bool, null — not just
            # object/array. A bare scalar VALUE (e.g. a source handler that
            # persists a cursor as a plain ISO-timestamp string rather than a
            # dict) therefore already arrives as a plain ``str``/``int``/
            # ``float``/``bool``/``None`` here, and is NOT itself valid JSON
            # text (``"2026-07-24T09:45:00+00:00"`` has no wrapping quotes) —
            # re-running ``json.loads`` on it raises spuriously
            # (``json.decoder.JSONDecodeError: Extra data: ...``), which is
            # exactly what starved ``source.gdelt.files`` from 2026-07-24
            # onward (the first source handler to store a bare-string
            # cursor). Only a non-``str`` value is guaranteed already-decoded
            # (dict/list/int/float/bool/None all pass through untouched); a
            # ``str`` might still be raw undecoded JSON text if this store is
            # ever pointed at a codec-less connection, so attempt the decode
            # and fall back to the raw string when it isn't valid JSON on its
            # own — i.e. it was already decoded by the connection's codec.
            if not isinstance(raw, str):
                return raw
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return raw

    async def set(self, key: str, value: Any) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.actor_filter_state (
                    actor_id, filter_id, state_key, value, updated_at
                )
                VALUES ($1, $2, $3, $4::jsonb, NOW())
                ON CONFLICT (actor_id, filter_id, state_key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                self._actor_id,
                self._filter_id,
                key,
                json.dumps(value, default=str),
            )


__all__ = [
    "ActorStateRecord",
    "ActorStateStore",
    "FilterStateStore",
    "SCHEMA",
    "SourceCursor",
]
