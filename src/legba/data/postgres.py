# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.postgres — async asyncpg pool with AGE-aware Cypher helper.

The pool is the primary substrate store: relational tables + AGE graph
(`legba_graph`). AGE requires:
  * `LOAD 'age'` on every connection (session-scoped, persists in session).
  * `SET search_path = ag_catalog, "$user", public` (session-scoped, resets
    when a connection is released back to an asyncpg pool — must reapply on
    acquire).

This module's `PostgresStore.cypher()` helper takes care of both.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Mapping

import asyncpg

from .config import PostgresConfig

logger = logging.getLogger(__name__)


class PostgresStore:
    """Async wrapper around `asyncpg.Pool` with AGE helpers.

    Usage:
        store = PostgresStore.from_env()
        await store.connect()
        async with store.acquire() as conn:
            rows = await conn.fetch("SELECT 1")
        await store.close()
    """

    GRAPH_NAME = "legba_graph"

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

    @staticmethod
    async def _prepare_for_cypher(conn: asyncpg.Connection) -> None:
        """Reapply session state needed before any cypher() call.

        Kept for backward compatibility — `_setup_connection` already
        re-applies search_path on every acquire, but explicit Cypher
        callers may still want to re-`LOAD 'age'` defensively.
        """
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')

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
    # Cypher helper
    # ------------------------------------------------------------------

    async def cypher(
        self,
        query: str,
        *,
        cols: str = "v agtype",
        graph_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query against the AGE graph and parse results.

        `cols` is the AS column list — e.g. `"v agtype"` for `RETURN n AS v`,
        or `"a agtype, b agtype"` for `RETURN a, b`. Values are parsed into
        Python dicts/scalars via `_parse_agtype`.
        """
        graph = graph_name or self.GRAPH_NAME
        async with self.acquire() as conn:
            await self._prepare_for_cypher(conn)
            sql = (
                f"SELECT * FROM cypher('{graph}', $$ {query} $$) AS ({cols})"
            )
            rows = await conn.fetch(sql)
            col_names = [c.strip().split()[0] for c in cols.split(",")]
            return [
                {name: self._parse_agtype(row[name]) for name in col_names}
                for row in rows
            ]

    # ------------------------------------------------------------------
    # agtype parsing
    # ------------------------------------------------------------------

    _VERTEX_RE = re.compile(r"::vertex$")
    _EDGE_RE = re.compile(r"::edge$")
    _PATH_RE = re.compile(r"::path$")

    @classmethod
    def _parse_agtype(cls, value: Any) -> Any:
        """Parse an AGE agtype string into a Python object.

        agtype literals look like:
            {"id": 1, "label": "Entity", "properties": {…}}::vertex
            {"id": 1, "label": "AlliedWith", "start_id": 2, "end_id": 3,
             "properties": {…}}::edge
        Strip the trailing `::type` tag and JSON-decode.
        """
        if value is None or not isinstance(value, str):
            return value

        s = value.strip()
        # Strip type suffix
        for suffix in ("::vertex", "::edge", "::path", "::numeric"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break

        # Try JSON first (covers most cases — objects, arrays, strings).
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            pass

        # Numeric literal
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass

        # Cypher escapes quote strings as `"foo"`; fall back to stripped.
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        return s

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

    async def graph_labels(self) -> dict[str, list[str]]:
        """Return AGE graph labels grouped by kind.

        Returns {'vertex': [...], 'edge': [...]} for the legba_graph.
        """
        async with self.acquire() as conn:
            await self._prepare_for_cypher(conn)
            rows = await conn.fetch(
                """
                SELECT l.name AS label_name, l.kind::text AS label_kind
                FROM ag_catalog.ag_label l
                JOIN ag_catalog.ag_graph g ON l.graph = g.graphid
                WHERE g.name = $1
                ORDER BY l.kind, l.name
                """,
                self.GRAPH_NAME,
            )
            out: dict[str, list[str]] = {"vertex": [], "edge": []}
            for row in rows:
                k = row["label_kind"]
                kind = (
                    "vertex" if k == "v"
                    else "edge" if k == "e"
                    else str(k)
                )
                if kind in out:
                    out[kind].append(row["label_name"])
            return out
