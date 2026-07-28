# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest fixtures for legba.data integration tests.

Per L-001 brief item 6: tests run against real running containers (the
dormant `legba-postgres-1` etc.) — no mocks for substrate boundaries. If
containers aren't up, the fixture brings them up via `docker compose`.

Setup pattern:
  1. Ensure containers are healthy (start them if not).
  2. Apply migrations on a *fresh test database* (created per session)
     so the migration set doesn't depend on host-DB state.
  3. Yield connection bundles to each test.

Teardown: the test database is dropped at session end. The containers are
left running for the next test session — same approach as the L-091 audit.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

# Force the package under test to read from local docker port mappings.
os.environ.setdefault("LEGBA_DATA_PG_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_PG_PORT", "5432")
os.environ.setdefault("LEGBA_DATA_PG_USER", "legba")
os.environ.setdefault("LEGBA_DATA_PG_PASSWORD", "legba")
os.environ.setdefault("LEGBA_DATA_QDRANT_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_NATS_URL", "nats://127.0.0.1:4222")

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_primary_migrations


REPO_ROOT = Path("/usr/local/deployments/active/legba")


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_containers_up() -> None:
    """Start the substrate containers if any aren't reachable on their ports."""
    targets = {
        "postgres":   ("127.0.0.1", 5432),
        "qdrant":     ("127.0.0.1", 6333),
        "redis":      ("127.0.0.1", 6379),
        "nats":       ("127.0.0.1", 4222),
    }
    need_start = [name for name, (h, p) in targets.items() if not _port_open(h, p)]
    if not need_start:
        return

    subprocess.run(
        [
            "docker", "compose",
            "-f", str(REPO_ROOT / "docker-compose.yml"),
            "up", "-d",
            "redis", "postgres", "qdrant", "nats",
        ],
        check=True,
        cwd=REPO_ROOT,
    )

    # Wait up to 90s for ports to be reachable.
    deadline = time.time() + 90
    while time.time() < deadline:
        remaining = [n for n, (h, p) in targets.items() if not _port_open(h, p)]
        if not remaining:
            return
        time.sleep(2)
    pytest.skip(f"substrate containers not reachable: {remaining}")


@pytest.fixture(scope="session", autouse=True)
def substrate_up():
    _ensure_containers_up()
    _ensure_pivot_test_db()
    yield


@pytest.fixture(autouse=True)
def _pin_composition_floor_default(monkeypatch):
    """B0-1 (2026-07-10): the LIVE deploy sets ``LEGBA_COMPOSITION_VERIFY_FLOOR``
    in .env (0.50), and this suite's environment may inherit it — which would
    flip every floor-plumbing assertion written against the code DEFAULT (0.0).
    Strip it so tests are deterministic regardless of the host .env; a test that
    wants to exercise the override sets it explicitly via monkeypatch.setenv."""
    monkeypatch.delenv("LEGBA_COMPOSITION_VERIFY_FLOOR", raising=False)


@pytest.fixture(autouse=True)
def _pin_structural_verify_gate_default(monkeypatch):
    """Same class as ``_pin_composition_floor_default`` (P2-1 wave, 2026-07):
    the LIVE .env sets ``LEGBA_STRUCTURAL_VERIFY_GATE=1`` and
    ``legba.data.config._load_env`` auto-loads the live .env as a fallback, so
    the suite inherits the gate ON — flipping the C2b OFF-safe assertions
    written against the code default (compute-and-show, no demotion). Strip it;
    a test that wants the gate sets it explicitly via monkeypatch.setenv."""
    monkeypatch.delenv("LEGBA_STRUCTURAL_VERIFY_GATE", raising=False)


# ---------------------------------------------------------------------------
# Persistent pivot-test database (legba_pivot_test)
# ---------------------------------------------------------------------------
#
# A handful of acceptance tests (analyst cross-source dedup / finding
# supersession / entity resolution, source-actor acquisition, the
# subscription engine, the P-13 discovery rig) connect DIRECTLY to a fixed
# ``legba_pivot_test`` database rather than the per-session ephemeral
# ``legba_test_<uuid>`` DB, and SKIP themselves when it is unreachable. So
# the test recipe is self-standing — and not silently degraded to "lots of
# skips" — the session bootstrap creates + migrates ``legba_pivot_test`` if
# it is absent. Idempotent: an already-migrated DB (the live dev rig)
# applies no new migrations and is left untouched. Override the target name
# with ``LEGBA_PIVOT_PG_DB``.

_PIVOT_DB_NAME = os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test")


def _ensure_pivot_test_db() -> None:
    """Create + migrate ``legba_pivot_test`` if it does not already exist.

    Best-effort: if Postgres is unreachable we return quietly and let the
    individual pivot-DB tests hit their own skip path. Migrations are
    CREATE-only + idempotent, so re-running against the live dev-rig DB is
    a no-op.
    """
    asyncio.run(_ensure_pivot_test_db_async())


async def _ensure_pivot_test_db_async() -> None:
    admin_dsn = "postgresql://legba:legba@127.0.0.1:5432/postgres"
    try:
        conn = await asyncpg.connect(admin_dsn)
    except Exception:
        # Postgres not reachable — pivot-DB tests will skip on their own.
        return
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _PIVOT_DB_NAME
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction block; asyncpg
            # auto-commits single statements outside an explicit tx.
            await conn.execute(f'CREATE DATABASE "{_PIVOT_DB_NAME}"')
    finally:
        await conn.close()

    # Apply (idempotent) primary migrations against the pivot DB.
    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database=_PIVOT_DB_NAME,
    )
    try:
        await apply_primary_migrations(cfg)
    except Exception:
        # A partially-migrated or differently-shaped pre-existing DB
        # surfaces in the individual tests' substrate-presence checks
        # (which skip rather than error). Don't fail the whole session.
        return


# ---------------------------------------------------------------------------
# Fresh test database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def test_pg_config() -> PostgresConfig:
    """Create a fresh `legba_test_<uuid>` database, return its config, drop at session end."""
    admin_dsn = "postgresql://legba:legba@127.0.0.1:5432/postgres"
    db_name = f"legba_test_{uuid4().hex[:10]}"

    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database=db_name,
    )
    yield cfg

    # Teardown: drop the test database.
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def migrated_pg(test_pg_config: PostgresConfig) -> PostgresConfig:
    """Apply all primary migrations to the fresh test DB and return config."""
    applied = await apply_primary_migrations(test_pg_config)
    assert applied, "expected at least one migration to apply"
    return test_pg_config
