# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.migrate — apply SQL migrations.

  * `apply_primary_migrations` — runs the numbered `0001_*.sql` … files
    against the primary Postgres+AGE cluster.

Each migration is recorded in `legba_data_migrations(name, sha256, …)` on the
primary cluster; re-runs skip already-applied files.

All migrations are CREATE-only per Lewis's clean-restart decision. The runner
will not delete or alter pre-existing tables — if a CREATE-only migration
fails because something already exists with a different shape, we surface
that explicitly rather than silently coercing.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Sequence

import asyncpg

from .config import DataConfig, PostgresConfig
from .migrations import MIGRATIONS_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def _discover() -> list[Path]:
    """Return sorted list of `*.sql` files under MIGRATIONS_DIR."""
    return list(sorted(MIGRATIONS_DIR.glob("*.sql")))


def primary_migration_files() -> list[Path]:
    """Migrations for the primary Postgres+AGE cluster (numbered)."""
    return [p for p in _discover() if p.stem[:1].isdigit()]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _ensure_ledger(conn: asyncpg.Connection) -> None:
    """Make sure the ledger table exists (in case 0001 hasn't run yet)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS legba_data_migrations (
            name        TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sha256      TEXT NOT NULL,
            notes       TEXT
        );
        """
    )


async def _is_applied(conn: asyncpg.Connection, name: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM legba_data_migrations WHERE name = $1", name
    )
    return row is not None


async def _apply_file(
    conn: asyncpg.Connection, path: Path, *, dry_run: bool = False
) -> bool:
    """Apply a single SQL file. Returns True if applied (False if skipped).

    Each file runs in its own transaction. AGE migrations need LOAD 'age' and
    a search_path tweak — we apply both unconditionally; they're no-ops on
    non-AGE migrations.
    """
    name = path.name
    body = path.read_text(encoding="utf-8")
    digest = _sha256(body)

    if await _is_applied(conn, name):
        logger.debug("migration already applied: %s", name)
        return False

    if dry_run:
        logger.info("[dry-run] would apply %s (sha256=%s)", name, digest[:16])
        return False

    logger.info("applying migration %s (sha256=%s)", name, digest[:16])

    # LOAD 'age' is session-scoped and must run *outside* a transaction —
    # if AGE isn't installed it raises UndefinedFileError, which would
    # abort an enclosing transaction. We catch + swallow at session scope.
    try:
        await conn.execute("LOAD 'age'")
        age_loaded = True
    except Exception:
        age_loaded = False

    async with conn.transaction():
        # `public` first so post-AGE migrations create tables in the right
        # schema. ag_catalog kept on path so AGE's create_graph / etc.
        # resolve when migration 0004 runs.
        if age_loaded:
            await conn.execute("SET search_path = public, ag_catalog")
        else:
            await conn.execute("SET search_path = public")
        await conn.execute(body)
        if age_loaded:
            await conn.execute("SET search_path = public, ag_catalog")
        else:
            await conn.execute("SET search_path = public")
        await conn.execute(
            """
            INSERT INTO legba_data_migrations (name, sha256)
            VALUES ($1, $2)
            """,
            name,
            digest,
        )
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def apply_primary_migrations(
    pg: PostgresConfig | None = None,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Apply primary-cluster migrations. Returns names of files applied."""
    pg = pg or PostgresConfig.from_env()
    applied: list[str] = []
    conn = await asyncpg.connect(pg.dsn)
    try:
        await _ensure_ledger(conn)
        for path in primary_migration_files():
            if await _apply_file(conn, path, dry_run=dry_run):
                applied.append(path.name)
    finally:
        await conn.close()
    return applied


async def apply_all(
    cfg: DataConfig | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Apply the primary cluster's migrations. Returns {cluster: [applied_files]}."""
    cfg = cfg or DataConfig.from_env()
    out: dict[str, list[str]] = {}
    out["primary"] = await apply_primary_migrations(cfg.postgres, dry_run=dry_run)
    return out


def list_pending(applied: Sequence[str]) -> list[str]:
    """Return migration file names not in `applied`."""
    return [p.name for p in primary_migration_files() if p.name not in applied]


if __name__ == "__main__":  # pragma: no cover — manual invocation
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Apply legba.data migrations.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = asyncio.run(apply_all(dry_run=args.dry_run))
    for cluster, files in result.items():
        print(f"{cluster}: {files}")
