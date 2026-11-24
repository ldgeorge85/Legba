# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite talks only to a Postgres server that proves it is a TEST server.

N-V (2026-11-13). On the deployment host the LIVE Postgres server
(``legba-postgres-1``) is published on ``127.0.0.1:5432`` — the very address
the conftests hardcode. Inside the container runner that address is the
isolated test stack's Postgres (deploy/compose.test.yml); on the host it is
production. Host-side pytest reached the live server on at least three days
(11-07, 11-11, 11-13), created ``legba_test_<uuid>`` databases there and ran
the bootstrap's migrations against a ``legba_pivot_test`` database beside the
production ``legba``. Nothing wrote a live table, but nothing stopped it.

Host and port cannot tell the two apart, so the guard asks the SERVER:

1. **Declared isolation** — ``LEGBA_TEST_PG_ISOLATED=1`` must be set. Only the
   container runner sets it (isolated stack). Unset — a bare host ``pytest`` —
   every Postgres connection is refused BEFORE any socket is opened: fail
   closed, never "try localhost".
2. **The server's own marker** — the test stack's Postgres starts with
   ``-c legba.test_stack=on`` on its command line, so
   ``current_setting('legba.test_stack', true)`` reads ``on`` in every fresh
   session. The live server never carries it (setting it would take an
   ``ALTER SYSTEM`` / ``ALTER DATABASE`` by hand, never a test).
3. **Production fingerprints, refused even if 1 and 2 pass** — ``fsync``
   must be ``off`` from the ``command line`` (the disposable stack runs
   fsync-off on tmpfs; production never does), and no database on the server
   may exceed ``LEGBA_TEST_PG_MAX_DB_BYTES`` (default 3 GiB; the stack's whole
   tmpfs is 4 GiB, the live ``legba`` database is ~15 GB).

The check runs once per (host, port), at the first connection the session
makes to it (every ``asyncpg.connect`` and every pool connection go through
the wrapper :func:`install_asyncpg_guard` puts in place), and explicitly in
the session bootstrap before any ``CREATE DATABASE`` or migration. A refusal
raises :class:`TestServerRefusedError` — a ``ConnectionRefusedError``, so
the suite's existing "Postgres not reachable" paths treat the server as
absent (and strict mode turns those skips into named failures). A server the
probe cannot reach at all is passed through untouched: the real connect then
fails exactly as it always did.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse

ISOLATED_ENV = "LEGBA_TEST_PG_ISOLATED"
MAX_DB_BYTES_ENV = "LEGBA_TEST_PG_MAX_DB_BYTES"
MARKER_SETTING = "legba.test_stack"
DEFAULT_MAX_DB_BYTES = 3 * 1024 ** 3

_PROBE_SQL = f"""
SELECT current_setting('{MARKER_SETTING}', true) AS marker,
       (SELECT setting FROM pg_settings WHERE name = 'fsync') AS fsync,
       (SELECT source FROM pg_settings WHERE name = 'fsync') AS fsync_source,
       (SELECT max(pg_database_size(oid)) FROM pg_database
         WHERE datallowconn) AS biggest_db_bytes,
       (SELECT datname FROM pg_database WHERE datallowconn
         ORDER BY pg_database_size(oid) DESC LIMIT 1) AS biggest_db
"""

HOW_TO_RUN = (
    "Database-backed tests run only in the isolated test stack: "
    "`LEGBA_REPO_ROOT=$PWD bash scripts/run_tests_in_container.sh <paths>` "
    "(CONTRIBUTING.md, 'Host-side database tests are not supported')."
)


class TestServerRefusedError(ConnectionRefusedError):
    """The Postgres server is not a verified test server."""

    __test__ = False  # not a pytest test class despite the name


#: (host, port) -> None (verified test server) or the refusal reason.
_VERDICTS: dict[tuple[str, int], str | None] = {}


def isolation_declared() -> bool:
    return os.environ.get(ISOLATED_ENV, "").strip() == "1"


def _max_db_bytes() -> int:
    raw = os.environ.get(MAX_DB_BYTES_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_DB_BYTES
    except ValueError:
        return DEFAULT_MAX_DB_BYTES


def judge_probe(row: Any, *, max_db_bytes: int | None = None) -> str | None:
    """None when the probe row identifies a test server, else the reason."""
    cap = _max_db_bytes() if max_db_bytes is None else max_db_bytes
    marker = (row["marker"] or "").strip().lower()
    if marker != "on":
        return (
            f"the server does not carry the test-stack marker "
            f"({MARKER_SETTING}={row['marker']!r}; the isolated test stack "
            "starts Postgres with `-c legba.test_stack=on`)"
        )
    if (row["fsync"], row["fsync_source"]) != ("off", "command line"):
        return (
            f"the server runs fsync={row['fsync']} (source "
            f"{row['fsync_source']}); the disposable test stack runs fsync=off "
            "from its command line — a durable server is not a test server"
        )
    biggest = int(row["biggest_db_bytes"] or 0)
    if biggest > cap:
        return (
            f"database {row['biggest_db']!r} on the server holds "
            f"{biggest / 1024 ** 3:.1f} GiB (cap {cap / 1024 ** 3:.1f} GiB, "
            f"{MAX_DB_BYTES_ENV}); that is a production-sized database"
        )
    return None


def _target(args: tuple, kwargs: dict) -> tuple[str, int, dict]:
    """(host, port, probe kwargs) for an ``asyncpg.connect`` call."""
    dsn = kwargs.get("dsn") if "dsn" in kwargs else (args[0] if args else None)
    host = kwargs.get("host")
    port = kwargs.get("port")
    user = kwargs.get("user")
    password = kwargs.get("password")
    database = kwargs.get("database")
    if isinstance(dsn, str) and dsn:
        parsed = urlparse(dsn)
        host = host or parsed.hostname
        port = port or parsed.port
        user = user or (unquote(parsed.username) if parsed.username else None)
        password = password or (
            unquote(parsed.password) if parsed.password else None
        )
        database = database or (parsed.path.lstrip("/") or None)
    if isinstance(host, (list, tuple)):
        host = host[0] if host else None
    if isinstance(port, (list, tuple)):
        port = port[0] if port else None
    host = str(host or os.environ.get("PGHOST") or "localhost")
    port = int(port or os.environ.get("PGPORT") or 5432)
    probe = {"host": host, "port": port, "user": user, "password": password,
             "database": database}
    return host, port, probe


async def verdict(
    connect: Callable[..., Awaitable[Any]],
    host: str,
    port: int,
    *,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> str | None:
    """None to allow, a reason to refuse. Cached per (host, port).

    ``connect`` is the REAL ``asyncpg.connect``. An unreachable server (or one
    that rejects the probe's login) is allowed through uncached: the caller's
    own connect then fails exactly as it would have without the guard.
    """
    if not isolation_declared():
        return (
            f"the isolation flag {ISOLATED_ENV} is not '1', so this process "
            "is not inside the isolated test stack and no Postgres server is "
            "trusted (on the deployment host 127.0.0.1:5432 is the LIVE "
            "server)"
        )
    key = (host, port)
    if key in _VERDICTS:
        return _VERDICTS[key]
    conn = None
    for db in dict.fromkeys(("postgres", database)):
        if db is None:
            continue
        try:
            conn = await connect(
                host=host, port=port, user=user, password=password,
                database=db, timeout=10,
            )
            break
        except Exception:  # unreachable / auth — not ours to judge
            continue
    if conn is None:
        return None
    try:
        try:
            row = await conn.fetchrow(_PROBE_SQL)
            reason = judge_probe(row)
        except Exception as exc:
            reason = f"the test-server probe failed ({exc!r}); refusing"
    finally:
        await conn.close()
    _VERDICTS[key] = reason
    return reason


def refusal(host: str, port: int, reason: str) -> TestServerRefusedError:
    return TestServerRefusedError(
        f"REFUSED Postgres {host}:{port} — not a verified test server: "
        f"{reason}. {HOW_TO_RUN}"
    )


async def assert_test_server(
    connect: Callable[..., Awaitable[Any]] | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 5432,
    user: str | None = "legba",
    password: str | None = "legba",
) -> None:
    """Raise :class:`TestServerRefusedError` unless host:port is a test server.

    Used by the session bootstrap BEFORE any ``CREATE DATABASE`` / migration.
    """
    real = connect or _REAL.get("connect")
    if real is None:
        import asyncpg

        real = asyncpg.connect
    reason = await verdict(real, host, port, user=user, password=password)
    if reason is not None:
        raise refusal(host, port, reason)


_REAL: dict[str, Any] = {}


def install_asyncpg_guard() -> None:
    """Route every ``asyncpg`` connection (direct and pooled) through the
    guard. Idempotent. Pools bind ``asyncpg.connection.connect`` when they are
    CREATED, so this must run before any pool exists (root conftest import)."""
    import asyncpg
    import asyncpg.connection as _conn_mod

    if _REAL:
        return
    real = _conn_mod.connect
    _REAL["connect"] = real

    async def guarded_connect(*args: Any, **kwargs: Any) -> Any:
        host, port, probe = _target(args, kwargs)
        reason = await verdict(
            real, host, port,
            user=probe["user"], password=probe["password"],
            database=probe["database"],
        )
        if reason is not None:
            raise refusal(host, port, reason)
        return await real(*args, **kwargs)

    guarded_connect.__wrapped__ = real  # type: ignore[attr-defined]
    guarded_connect.__legba_test_server_guard__ = True  # type: ignore[attr-defined]
    _conn_mod.connect = guarded_connect
    asyncpg.connect = guarded_connect


__all__ = [
    "HOW_TO_RUN",
    "ISOLATED_ENV",
    "MARKER_SETTING",
    "TestServerRefusedError",
    "assert_test_server",
    "install_asyncpg_guard",
    "isolation_declared",
    "judge_probe",
    "verdict",
]
