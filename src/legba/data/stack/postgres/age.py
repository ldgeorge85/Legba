# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-125 AGE-aware Postgres-cluster stack handler.

Concrete handler conforming to `legba_kind_contracts.md` §1 (KindHandler
base shape) for the `postgres` family of stack components. Built on top of
the L-001 `legba.data.postgres.PostgresStore` wrapper so the AGE codec /
search_path management lives in one place.

Highlights:

  * `kind = "postgres"`, `family = "stack"` (stack components are
    *substrate dependencies* used by source / filter / analyst handlers —
    see `registry/health.py` lead comment). The L-102 base envelope still
    applies: `config_schema`, `schema_version`, `handler_version`,
    lifecycle hooks, `health_check`.
  * Per-tenant flow: `acquire(schema=...)` switches `search_path` on the
    acquired connection; `create_user_schema` / `drop_user_schema` give
    operators an explicit knob (per DM-7: shared schema is default, but
    per-tenant is supported when a use case demands it).
  * AGE Cypher helper: `cypher(graph, query, params=None)` wraps the
    `SELECT * FROM cypher(...)` boilerplate and routes through
    `PostgresStore.cypher()` for agtype parsing.
  * AGE vocabulary helpers: `register_vertex_label` /
    `register_edge_label` idempotently create labels (matches the
    `create_vlabel` / `create_elabel` pattern in migration 0004).
  * Healthcheck combines `SELECT 1` + `SELECT extversion FROM
    pg_extension WHERE extname='age'` so the runtime can distinguish a
    bare Postgres (no AGE) from an AGE-enabled cluster.

Implementation note. The L-102 protocol uses pydantic dataclasses for
`ConfigureContext` / `RuntimeContext` whose full shape is owned by L-103
(not yet landed in code). For the L-125 drop we accept an optional
`ConfigureContext`-shaped object and degrade gracefully when fields are
absent — this matches the precedent set by the Phase-1
`StackHealthChecker` implementations in `registry/health.py`, which work
against `ResolvedConfig` without the runtime present.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, ClassVar, Mapping

import asyncpg

from ...config import PostgresConfig
from ...postgres import PostgresStore
from ...schemas.stack import PostgresCluster, PostgresClusterConfig
from ...vocabulary import (
    ENTITY_CLASSES,
    RELATIONSHIP_TYPES,
    normalize_relationship,
    vertex_label,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health shape — matches the L-102 HandlerHealth dataclass surface.
# ---------------------------------------------------------------------------


@dataclass
class PostgresHandlerHealth:
    """Result of a handler-level healthcheck.

    Shape mirrors L-102 §1 `HandlerHealth` (state / last_success_at /
    last_error / detail) plus Postgres-specific fields.
    """

    state: str  # "healthy" | "degraded" | "unhealthy"
    last_success_at: datetime | None
    last_error: str | None
    detail: Mapping[str, Any] = field(default_factory=dict)

    # Postgres-specifics
    age_version: str | None = None
    pool_size: int | None = None


# ---------------------------------------------------------------------------
# Identifier validation — schemas and labels must be safe to interpolate.
# ---------------------------------------------------------------------------


_VALID_IDENT_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_"
)


def _validate_identifier(name: str, *, kind: str = "identifier") -> str:
    """Reject anything that isn't a plain `[A-Za-z_][A-Za-z0-9_]*`.

    Schemas and AGE labels are interpolated into DDL strings (asyncpg does
    not parameterize DDL); the guard here is the only sanitization layer.
    """
    if not name:
        raise ValueError(f"{kind} must be non-empty")
    if not (name[0].isalpha() or name[0] == "_"):
        raise ValueError(f"{kind} {name!r} must start with letter or underscore")
    bad = [c for c in name if c not in _VALID_IDENT_CHARS]
    if bad:
        raise ValueError(f"{kind} {name!r} contains invalid character(s) {bad!r}")
    if len(name) > 63:  # PG NAMEDATALEN-1
        raise ValueError(f"{kind} {name!r} exceeds 63-char limit")
    return name


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class PostgresClusterHandler:
    """AGE-aware Postgres-cluster stack handler (L-125).

    Reuses `legba.data.postgres.PostgresStore` for the asyncpg pool, AGE
    codec registration, and Cypher boilerplate. This layer adds the L-102
    lifecycle wrapper, descriptor parsing, multi-tenant schema flow, and
    AGE vocabulary helpers.

    Construction.
        cfg can be a `PostgresClusterConfig` (descriptor-shape — factory
        values wrapping `raw` payloads) OR a plain `PostgresConfig` (the
        L-001 env-shape). The handler normalises to a `PostgresConfig`
        internally so the store wrapper sees a familiar shape.

        Secret resolution: when constructed from a `PostgresClusterConfig`
        the `password` field is a `Property.Secret` whose `raw` is a
        credential id, not the cleartext. Tests pass cleartext via the
        `password_override` kwarg; the runtime will inject resolved bytes
        via the configure hook (`ctx.secrets.resolve(cfg.password)`).
    """

    # --------------------------------------------------------------------
    # Identity / registration (L-102 §1)
    # --------------------------------------------------------------------

    kind: ClassVar[str] = "postgres"
    family: ClassVar[str] = "stack"
    schema_version: ClassVar[str] = "legba/stack/postgres/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type] = PostgresClusterConfig

    DEFAULT_GRAPH_NAME: ClassVar[str] = PostgresStore.GRAPH_NAME

    def __init__(
        self,
        cfg: PostgresClusterConfig | PostgresConfig,
        *,
        component_id: str = "pg.cluster_main",
        password_override: str | None = None,
        graph_name: str | None = None,
    ) -> None:
        self.component_id = component_id
        self.graph_name = graph_name or self.DEFAULT_GRAPH_NAME

        self._descriptor_config: PostgresClusterConfig | None = None
        self._pg_config: PostgresConfig

        if isinstance(cfg, PostgresClusterConfig):
            self._descriptor_config = cfg
            self._pg_config = self._descriptor_to_pgconfig(cfg, password_override)
        else:
            self._pg_config = cfg

        self._store: PostgresStore | None = None
        self._state: str = "draft"
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None

    # --------------------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------------------

    @staticmethod
    def _descriptor_to_pgconfig(
        cfg: PostgresClusterConfig,
        password_override: str | None,
    ) -> PostgresConfig:
        """Translate a descriptor-shape config (Property-wrapped fields) to
        the L-001 `PostgresConfig` dataclass the store wrapper expects."""
        password = password_override
        if password is None:
            # `cfg.password` is a `Property.Secret` whose `raw` is a
            # credential id, not cleartext. The runtime is responsible for
            # resolving it via `ctx.secrets`; outside the runtime the
            # caller must pass `password_override`.
            raise ValueError(
                "PostgresClusterConfig.password is a Secret reference; "
                "pass password_override or invoke on_configure with a runtime context "
                "that resolves the secret."
            )

        pool_size = int(cfg.pool_size.raw)
        # Honour the descriptor's `pool_size` as the pool's max; min stays 1.
        return PostgresConfig(
            host=str(cfg.host.raw),
            port=int(cfg.port.raw),
            user=str(cfg.user.raw),
            password=password,
            database=str(cfg.database.raw),
            pool_min=1,
            pool_max=pool_size,
        )

    @property
    def store(self) -> PostgresStore:
        if self._store is None:
            raise RuntimeError(
                f"PostgresClusterHandler({self.component_id}) is not connected; "
                f"call on_configure() / on_activate() first"
            )
        return self._store

    @property
    def state(self) -> str:
        return self._state

    @property
    def pg_config(self) -> PostgresConfig:
        return self._pg_config

    # --------------------------------------------------------------------
    # Lifecycle hooks (L-102 §1)
    # --------------------------------------------------------------------

    async def on_configure(self, ctx: Any = None) -> None:
        """Verify connectivity + AGE loaded + graph exists.

        ``ctx`` is the L-103 ConfigureContext when invoked under the
        runtime — its `secrets` resolver will be used to dereference the
        descriptor's `password` SecretRef if the handler was constructed
        from a `PostgresClusterConfig` without a `password_override`.
        """
        if self._descriptor_config is not None and ctx is not None:
            # Resolve the Secret via the runtime resolver if available.
            secrets = getattr(ctx, "secrets", None)
            if secrets is not None:
                secret_id = self._descriptor_config.password.raw
                resolved = await secrets.resolve(secret_id)
                if resolved is not None:
                    pw = (
                        resolved.decode("utf-8")
                        if isinstance(resolved, (bytes, bytearray))
                        else str(resolved)
                    )
                    self._pg_config = self._descriptor_to_pgconfig(
                        self._descriptor_config, pw
                    )

        # Bring up the store (creates a small bootstrap pool + the main pool).
        store = PostgresStore(self._pg_config)
        await store.connect()
        self._store = store

        # Probe connectivity, AGE presence, and graph existence.
        try:
            async with store.acquire() as conn:
                val = await conn.fetchval("SELECT 1")
                if val != 1:
                    raise RuntimeError(f"connectivity probe returned {val!r}")
                age_version = await conn.fetchval(
                    "SELECT extversion FROM pg_extension WHERE extname='age'"
                )
                if not age_version:
                    raise RuntimeError(
                        "AGE extension is not installed on the target database"
                    )
                graph_present = await conn.fetchval(
                    "SELECT 1 FROM ag_catalog.ag_graph WHERE name=$1",
                    self.graph_name,
                )
                if not graph_present:
                    raise RuntimeError(
                        f"AGE graph {self.graph_name!r} not present; "
                        f"expected migration 0004 to have created it"
                    )
        except Exception:
            # On configure failure, tear down the store so a retry starts clean.
            try:
                await store.close()
            finally:
                self._store = None
            raise

        self._state = "configured"
        self._last_success_at = _now()
        self._last_error = None

    async def on_activate(self, ctx: Any = None) -> None:
        """Warm the pool by acquiring + releasing N connections in parallel.

        N = the descriptor's pool_size (or `pool_min` for env-config-only
        constructions). The runtime calls activate after configure; the
        warm here just guarantees the codec callback ran on every pooled
        connection before traffic arrives.
        """
        store = self.store  # raises if not configured
        target = max(1, int(self._pg_config.pool_min))

        async def _warm() -> None:
            async with store.acquire() as conn:
                await conn.fetchval("SELECT 1")

        await asyncio.gather(*[_warm() for _ in range(target)])

        self._state = "active"
        self._last_success_at = _now()

    async def on_pause(self, ctx: Any = None) -> None:
        """Mark paused. The pool is kept warm so resume is cheap."""
        self._state = "paused"

    async def on_resume(self, ctx: Any = None) -> None:
        """Resume from paused. Healthcheck before returning to active."""
        h = await self.health_check(ctx)
        if h.state != "healthy":
            raise RuntimeError(
                f"cannot resume {self.component_id}: health={h.state} detail={h.detail}"
            )
        self._state = "active"

    async def on_retire(self, ctx: Any = None) -> None:
        """Tear down the pool. Terminal state."""
        if self._store is not None:
            try:
                await self._store.close()
            finally:
                self._store = None
        self._state = "retired"

    # --------------------------------------------------------------------
    # Health (L-102 §1)
    # --------------------------------------------------------------------

    async def health_check(self, ctx: Any = None) -> PostgresHandlerHealth:
        if self._store is None:
            return PostgresHandlerHealth(
                state="unhealthy",
                last_success_at=self._last_success_at,
                last_error="store not connected",
                detail={"component_id": self.component_id},
            )
        try:
            async with self._store.acquire() as conn:
                # Liveness — minimal cost, no AGE-specific call.
                val = await conn.fetchval("SELECT 1")
                # Readiness — AGE extension present?
                age_version: str | None = await conn.fetchval(
                    "SELECT extversion FROM pg_extension WHERE extname='age'"
                )
            now = _now()
            self._last_success_at = now
            self._last_error = None
            return PostgresHandlerHealth(
                state="healthy" if val == 1 else "degraded",
                last_success_at=now,
                last_error=None,
                detail={
                    "component_id": self.component_id,
                    "select_one": val,
                    "graph_name": self.graph_name,
                },
                age_version=age_version,
                pool_size=int(self._pg_config.pool_max),
            )
        except Exception as exc:  # noqa: BLE001 — propagate detail
            self._last_error = str(exc)
            return PostgresHandlerHealth(
                state="unhealthy",
                last_success_at=self._last_success_at,
                last_error=str(exc),
                detail={"component_id": self.component_id, "error": str(exc)},
            )

    # --------------------------------------------------------------------
    # Connection management — `acquire` with optional per-acquire schema
    # --------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self, schema: str | None = None
    ) -> AsyncIterator[asyncpg.Connection]:
        """Async context manager yielding a connection.

        If `schema` is provided, `SET search_path TO <schema>, ag_catalog,
        "$user", public` is applied for the duration of the held
        connection. asyncpg resets `SET` on release, so callers don't need
        to clean up.
        """
        async with self.store.acquire() as conn:
            if schema is not None:
                _validate_identifier(schema, kind="schema")
                # Keep AGE catalog in the search path so the agtype codec
                # and Cypher helpers continue to work for tenants.
                await conn.execute(
                    f'SET search_path TO "{schema}", ag_catalog, "$user", public'
                )
            yield conn

    # --------------------------------------------------------------------
    # Query wrappers — pass-throughs onto an acquired connection.
    # --------------------------------------------------------------------

    async def execute(self, query: str, *params: Any) -> str:
        async with self.acquire() as conn:
            return await conn.execute(query, *params)

    async def fetch(self, query: str, *params: Any) -> list[asyncpg.Record]:
        async with self.acquire() as conn:
            return await conn.fetch(query, *params)

    async def fetchrow(
        self, query: str, *params: Any
    ) -> asyncpg.Record | None:
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *params)

    async def fetchval(self, query: str, *params: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(query, *params)

    # --------------------------------------------------------------------
    # search_path management on a held connection
    # --------------------------------------------------------------------

    async def set_schema(self, connection: asyncpg.Connection, name: str) -> None:
        """`SET search_path TO <name>, ag_catalog, "$user", public` on the
        given connection.

        Useful when a caller already holds a connection from `acquire()`
        and wants to switch tenant mid-flight (e.g., when iterating over
        per-tenant operations on a single transaction).
        """
        _validate_identifier(name, kind="schema")
        await connection.execute(
            f'SET search_path TO "{name}", ag_catalog, "$user", public'
        )

    # --------------------------------------------------------------------
    # Multi-tenant schema lifecycle
    # --------------------------------------------------------------------

    async def create_user_schema(self, name: str) -> None:
        """Idempotently create a Postgres schema for a tenant scope.

        Per DM-7 (data-mapping audit): per-tenant schema is supported but
        not the default. The descriptor model uses `target_id`-tagged rows
        in the shared schema; this helper exists for the cases where a
        target legitimately wants its own schema (e.g., partner-scoped
        data with separate access control).
        """
        _validate_identifier(name, kind="schema")
        async with self.acquire() as conn:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{name}"')

    async def drop_user_schema(self, name: str, *, cascade: bool = False) -> None:
        """Drop a tenant schema. `cascade=True` drops contained objects too."""
        _validate_identifier(name, kind="schema")
        suffix = " CASCADE" if cascade else ""
        async with self.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{name}"{suffix}')

    async def schema_exists(self, name: str) -> bool:
        _validate_identifier(name, kind="schema")
        async with self.acquire() as conn:
            val = await conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1",
                name,
            )
        return val is not None

    # --------------------------------------------------------------------
    # AGE Cypher helper
    # --------------------------------------------------------------------

    async def cypher(
        self,
        graph: str,
        query: str,
        params: Mapping[str, Any] | None = None,
        *,
        cols: str = "v agtype",
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query against `graph` and return parsed rows.

        ``params`` is an optional mapping of substitutions applied to the
        Cypher source via Python `str.format` — AGE does not bind Cypher
        parameters through asyncpg, and the legacy `ag_catalog.cypher`
        function takes a parameter map only when run as a function
        invocation (incompatible with the `SELECT * FROM cypher(...)`
        form). Callers MUST supply only trusted / sanitised values.

        Each row is returned as a dict keyed by `cols`. `cols` defaults to
        ``"v agtype"`` (matches ``RETURN n AS v``). Multi-column returns
        require an explicit `cols` string, e.g. ``"a agtype, b agtype"``.
        """
        if params:
            try:
                query = query.format(**params)
            except (KeyError, IndexError) as exc:
                raise ValueError(
                    f"cypher() params do not satisfy template: {exc}"
                ) from exc

        # Delegate to the store helper for graph + agtype parsing.
        return await self.store.cypher(query, cols=cols, graph_name=graph)

    # --------------------------------------------------------------------
    # AGE vocabulary helpers — idempotent label creation
    # --------------------------------------------------------------------

    async def register_vertex_label(self, label: str) -> bool:
        """Idempotently create an AGE vertex label.

        Returns True if the label was created in this call, False if it
        already existed. Label naming follows the L-090 §4.5 PascalCase
        convention.
        """
        return await self._register_label(label, kind="v")

    async def register_edge_label(self, label: str) -> bool:
        """Idempotently create an AGE edge label.

        UPPER_SNAKE legacy edge names normalise to PascalCase via
        `vocabulary.RELATIONSHIP_ALIASES` before creation, matching the
        Cypher-write-time normalization Lewis confirmed in DM-6.
        """
        canonical = normalize_relationship(label)
        return await self._register_label(canonical, kind="e")

    async def _register_label(self, label: str, *, kind: str) -> bool:
        """Implementation shared by vertex / edge label registration."""
        _validate_identifier(label, kind=f"AGE {('vertex' if kind=='v' else 'edge')} label")

        async with self.acquire() as conn:
            # Ensure AGE session state is loaded on this connection.
            await PostgresStore._prepare_for_cypher(conn)
            already = await conn.fetchval(
                """
                SELECT 1
                FROM ag_catalog.ag_label l
                JOIN ag_catalog.ag_graph g ON l.graph = g.graphid
                WHERE g.name = $1 AND l.name = $2
                """,
                self.graph_name,
                label,
            )
            if already:
                return False
            func = "create_vlabel" if kind == "v" else "create_elabel"
            await conn.execute(
                f"SELECT ag_catalog.{func}($1, $2)", self.graph_name, label
            )
            return True

    # --------------------------------------------------------------------
    # Convenience — seed the L-090 §4.5 trimmed vocabulary (9 + 14)
    # --------------------------------------------------------------------

    async def ensure_seed_vocabulary(self) -> dict[str, list[str]]:
        """Ensure all 9 entity_classes and 14 relationship_types exist.

        Returns a dict of `{'vertex': [created...], 'edge': [created...]}
        ` listing labels actually created (vs already-present). Used by
        the smoke flow + the L-001 migration in tests where AGE was set
        up from scratch.
        """
        created: dict[str, list[str]] = {"vertex": [], "edge": []}
        for ec in ENTITY_CLASSES:
            label = vertex_label(ec)
            if await self.register_vertex_label(label):
                created["vertex"].append(label)
        for rt in RELATIONSHIP_TYPES:
            if await self.register_edge_label(rt):
                created["edge"].append(rt)
        return created


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def handler() -> type[PostgresClusterHandler]:
    """L-102 §1 factory function — returns the handler class.

    Plugin entry points (`legba.handlers` group, "stack.postgres" key) and
    the in-tree bootstrap (`registry/handlers.py` once it lands) call this
    factory at process start to register the handler.
    """
    return PostgresClusterHandler
