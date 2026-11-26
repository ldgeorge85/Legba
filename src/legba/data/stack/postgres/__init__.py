# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.postgres — Postgres-cluster stack handler.

This package hosts the AGE-aware Postgres cluster stack component. The handler
conforms to the kind-handler base shape (`kind` ClassVar, `config_schema`, lifecycle hooks,
`health_check`, `telemetry`) and adds Postgres-specific operations:

  * `acquire(schema=None)` — async context manager yielding an asyncpg
    connection with optional per-acquire `search_path` switching.
  * `execute / fetch / fetchrow / fetchval` — async query wrappers.
  * `cypher(graph, query, params=None)` — AGE Cypher helper that wraps the
    `SELECT * FROM cypher(...)` boilerplate.
  * `set_schema(connection, name)` — `SET search_path TO ...` on a held
    connection.
  * `create_user_schema(name) / drop_user_schema(name)` — multi-tenant
    schema lifecycle.
  * `register_vertex_label(label) / register_edge_label(label)` —
    idempotent AGE label creation.

The handler reuses `legba.data.postgres.PostgresStore` for connection pool
+ AGE codec init. The L-125 layer adds the per-handler lifecycle, the
PostgresCluster descriptor parsing, the per-tenant schema flow, and the
AGE-vocabulary helpers.

"""

from __future__ import annotations

from .age import (
    PostgresClusterHandler,
    PostgresHandlerHealth,
    handler,
)

__all__ = [
    "PostgresClusterHandler",
    "PostgresHandlerHealth",
    "handler",
]
