# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.postgres — the async asyncpg pool.

The pool is the primary substrate store (relational tables). The Apache AGE
graph (`legba_graph`) was removed on 2026-11-15 (it held no row), and the
cypher helper with it. The AGE *extension* is still installed, so each
connection still loads it and keeps ``ag_catalog`` on the search path until
the stock-Postgres image swap (see SEAMS).
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Mapping

import asyncpg

from .config import PostgresConfig

logger = logging.getLogger(__name__)


class PostgresStore:
    """Async wrapper around `asyncpg.Pool`.

    Usage:
        store = PostgresStore.from_env()
        await store.connect()
        async with store.acquire() as conn:
            rows = await conn.fetch("SELECT 1")
        await store.close()
    """

    def __init__(self, cfg: PostgresConfig):
        self._cfg = cfg
        self._pool: asyncpg.Pool | None = None

    @classmethod
    def from_env(cls) -> "PostgresStore":
        return cls(PostgresConfig.from_env())

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresStore not connected; call connect() first")
        return self._pool

    @property
    def cfg(self) -> PostgresConfig:
        return self._cfg

    async def connect(self) -> None:
        if self._pool is not None:
            return
        # Two-phase init: bootstrap pool installs AGE extension, then a
        # production pool registers the agtype codec per-connection.
        bootstrap = await asyncpg.create_pool(
            self._cfg.dsn, min_size=1, max_size=2,
        )
        async with bootstrap.acquire() as conn:
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS age")
            except Exception as exc:  # pragma: no cover — non-AGE PG would land here
                logger.warning("AGE extension not available: %s", exc)
        await bootstrap.close()

        self._pool = await asyncpg.create_pool(
            self._cfg.dsn,
            min_size=self._cfg.pool_min,
            max_size=self._cfg.pool_max,
            init=self._init_connection,
            setup=self._setup_connection,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Connection init / prep
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_json(value: Any) -> str:
        """JSON/JSONB param encoder.

        Pre-codec, asyncpg required ``json.dumps``-ed *strings* for jsonb
        params, so every existing write path passes str — those pass through
        untouched (identical wire bytes to the codec-less behaviour). Dicts /
        lists / scalars are serialized here, so new code can pass them
        directly.
        """
        if isinstance(value, str):
            return value
        return json.dumps(value, default=str)

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        """Pool init callback — register JSON + agtype codecs per connection."""
        # JSON/JSONB codec FIRST and unconditionally (G4 — envelope-vs-row
        # divergence class): every fetch path on this pool receives jsonb
        # columns as Python dicts/lists, matching the published-envelope
        # shape, instead of raw str on some paths and dict on others.
        # pg_catalog.jsonb always exists; a failure here is a real fault and
        # must sink pool bring-up loudly rather than silently reintroduce
        # str-typed payloads.
        for typename in ("json", "jsonb"):
            await conn.set_type_codec(
                typename,
                schema="pg_catalog",
                encoder=PostgresStore._encode_json,
                decoder=json.loads,
                format="text",
            )
        try:
            await conn.execute("LOAD 'age'")
            await conn.execute('SET search_path = ag_catalog, "$user", public')
            await conn.set_type_codec(
                "agtype",
                schema="ag_catalog",
                encoder=str,
                decoder=lambda x: x,
                format="text",
            )
        except Exception as exc:
            # AGE codec registration is best-effort; tests using bare PG still want a pool.
            logger.debug("AGE codec init skipped: %s", exc)

    @staticmethod
    async def _setup_connection(conn: asyncpg.Connection) -> None:
        """Pool setup callback — runs on every ``pool.acquire()``.

        asyncpg's `RESET ALL` (issued internally to scrub per-connection
        state between acquirers) clears `SET search_path`. Without this
        callback, only the *first* checkout of a fresh connection sees
        ag_catalog on the path; subsequent acquires get the default
        `"$user", public`, which breaks any code that issues unqualified
        Cypher / ag_catalog references and silently routes `CREATE TABLE`
        statements into whichever schema happens to be first.

        Re-applying the path on every acquire fixes the class of bug
        (`relation "<x>" does not exist` after a few minutes of runtime)
        for *all* callers, not just `cypher()`.
        """
        try:
            await conn.execute('SET search_path = ag_catalog, "$user", public')
        except Exception as exc:
            logger.debug("search_path reset skipped: %s", exc)

    # ------------------------------------------------------------------
    # Acquire / transaction helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    # ------------------------------------------------------------------
    # Convenience operations
    # ------------------------------------------------------------------

    async def list_tables(self, schema: str = "public") -> list[str]:
        """Return list of table names in the given schema."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tablename
                FROM pg_catalog.pg_tables
                WHERE schemaname = $1
                ORDER BY tablename
                """,
                schema,
            )
            return [r["tablename"] for r in rows]

    async def table_columns(
        self, table: str, schema: str = "public"
    ) -> list[dict[str, Any]]:
        """Return list of (column_name, data_type, is_nullable) for a table."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                ORDER BY ordinal_position
                """,
                schema,
                table,
            )
            return [dict(r) for r in rows]
