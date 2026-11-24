# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-V — the suite refuses any Postgres server that is not the test stack.

Host-side pytest reached the LIVE server on 127.0.0.1:5432 on 11-07, 11-11
and 11-13 and created/migrated databases there. These tests pin the guard
with fakes only: no test here opens a socket to any Postgres server.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import asyncpg
import pytest
import yaml

from tests import _pg_test_server_guard as guard

REPO = Path(__file__).resolve().parents[1]

_TEST_STACK_ROW = {
    "marker": "on", "fsync": "off", "fsync_source": "command line",
    "biggest_db_bytes": 40 * 1024 ** 2, "biggest_db": "legba_pivot_test",
}
#: What the live server answered (read-only probe, 2026-11-13).
_LIVE_ROW = {
    "marker": None, "fsync": "on", "fsync_source": "default",
    "biggest_db_bytes": 15 * 1024 ** 3, "biggest_db": "legba",
}


class _FakeConn:
    def __init__(self, row):
        self._row = row
        self.closed = False

    async def fetchrow(self, _sql):
        return self._row

    async def close(self):
        self.closed = True


def _fake_connect(row=None, *, fail: Exception | None = None):
    calls: list[dict] = []

    async def connect(**kwargs):
        calls.append(kwargs)
        if fail is not None:
            raise fail
        return _FakeConn(row)

    return connect, calls


@pytest.fixture(autouse=True)
def _fresh_verdicts():
    saved = dict(guard._VERDICTS)
    guard._VERDICTS.clear()
    yield
    guard._VERDICTS.clear()
    guard._VERDICTS.update(saved)


def test_judge_accepts_only_the_marked_disposable_server() -> None:
    assert guard.judge_probe(_TEST_STACK_ROW) is None
    assert "marker" in guard.judge_probe(_LIVE_ROW)
    assert "fsync" in guard.judge_probe({**_TEST_STACK_ROW, "fsync": "on",
                                         "fsync_source": "default"})
    # A marker set by hand next to a durable fsync is still refused.
    assert "fsync" in guard.judge_probe(
        {**_TEST_STACK_ROW, "fsync_source": "configuration file"}
    )
    assert "production-sized" in guard.judge_probe(
        {**_TEST_STACK_ROW, "biggest_db_bytes": 15 * 1024 ** 3,
         "biggest_db": "legba"}
    )


def test_no_isolation_declared_refuses_without_connecting(monkeypatch) -> None:
    monkeypatch.delenv(guard.ISOLATED_ENV, raising=False)
    connect, calls = _fake_connect(_TEST_STACK_ROW)
    reason = asyncio.run(guard.verdict(connect, "127.0.0.1", 5432))
    assert reason is not None and guard.ISOLATED_ENV in reason
    assert calls == []          # fail closed: not even a probe


def test_the_live_server_is_refused_and_the_verdict_cached(monkeypatch) -> None:
    monkeypatch.setenv(guard.ISOLATED_ENV, "1")
    connect, calls = _fake_connect(_LIVE_ROW)
    first = asyncio.run(guard.verdict(connect, "127.0.0.1", 5432,
                                      user="legba", password="legba"))
    second = asyncio.run(guard.verdict(connect, "127.0.0.1", 5432))
    assert first is not None and first == second
    assert len(calls) == 1 and calls[0]["database"] == "postgres"
    with pytest.raises(guard.TestServerRefusedError, match="REFUSED Postgres"):
        asyncio.run(guard.assert_test_server(connect))


def test_the_test_stack_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv(guard.ISOLATED_ENV, "1")
    connect, _calls = _fake_connect(_TEST_STACK_ROW)
    assert asyncio.run(guard.verdict(connect, "127.0.0.1", 5432)) is None
    asyncio.run(guard.assert_test_server(connect))


def test_an_unreachable_server_passes_through_uncached(monkeypatch) -> None:
    monkeypatch.setenv(guard.ISOLATED_ENV, "1")
    connect, calls = _fake_connect(fail=OSError("connection refused"))
    assert asyncio.run(guard.verdict(connect, "127.0.0.1", 5432,
                                     database="legba_w1")) is None
    assert [c["database"] for c in calls] == ["postgres", "legba_w1"]
    assert ("127.0.0.1", 5432) not in guard._VERDICTS


def test_a_failing_probe_query_is_a_refusal(monkeypatch) -> None:
    monkeypatch.setenv(guard.ISOLATED_ENV, "1")

    class _Broken(_FakeConn):
        async def fetchrow(self, _sql):
            raise RuntimeError("permission denied for pg_database_size")

    async def connect(**_kw):
        return _Broken(None)

    reason = asyncio.run(guard.verdict(connect, "127.0.0.1", 5432))
    assert reason is not None and "probe failed" in reason


def test_the_installed_wrapper_refuses_direct_and_pooled_connects(monkeypatch) -> None:
    """The root conftest installed the guard; with isolation undeclared both
    entry points raise before any socket opens (a socket to 127.0.0.1:5432 on
    this host would be the LIVE server)."""
    assert getattr(asyncpg.connect, "__legba_test_server_guard__", False)
    monkeypatch.delenv(guard.ISOLATED_ENV, raising=False)

    async def go():
        with pytest.raises(guard.TestServerRefusedError):
            await asyncpg.connect("postgresql://legba:legba@127.0.0.1:5432/legba")
        with pytest.raises(guard.TestServerRefusedError):
            await asyncpg.create_pool(
                host="127.0.0.1", port=5432, user="legba", password="legba",
                database="legba", min_size=1, max_size=1,
            )

    asyncio.run(go())


@pytest.mark.parametrize(
    "args,kwargs,expected",
    [
        (("postgresql://u:p%40w@db.example:6543/x",), {}, ("db.example", 6543, "u", "p@w", "x")),
        ((), {"host": "127.0.0.1", "port": 5544, "user": "legba", "database": "legba_w1"},
         ("127.0.0.1", 5544, "legba", None, "legba_w1")),
        ((), {"dsn": "postgresql://legba@localhost/legba"}, ("localhost", 5432, "legba", None, "legba")),
    ],
)
def test_target_parsing(args, kwargs, expected) -> None:
    host, port, probe = guard._target(args, kwargs)
    assert (host, port, probe["user"], probe["password"], probe["database"]) == expected


def test_the_test_stack_postgres_services_carry_the_marker() -> None:
    doc = yaml.safe_load((REPO / "deploy" / "compose.test.yml").read_text())
    for name in ("pg", "age-w1"):
        command = " ".join(str(x) for x in doc["services"][name]["command"])
        assert "legba.test_stack=on" in command.replace("-c ", ""), name
        assert "fsync=off" in command, name


def test_the_runner_declares_isolation_and_takes_the_nightly_lock() -> None:
    text = (REPO / "scripts" / "run_tests_in_container.sh").read_text()
    isolated_block = text.split('if [ "${LEGBA_TEST_STACK}" = "isolated" ]; then', 1)[1]
    assert "LEGBA_TEST_PG_ISOLATED=1" in isolated_block.split("fi", 1)[0]
    assert "/tmp/legba-container-tests.lock" in text
    assert re.search(r"flock -n 8", text) and re.search(r"flock -w .* 8", text)
    # The lock is taken after the lint early-exit and before the stack is
    # brought up (a FRESH teardown under a running suite is the collision).
    assert text.index("flock -n 8") > text.index("-m ruff check")
    assert text.index("flock -n 8") < text.index('"${TEST_COMPOSE[@]}" up -d')
    nightly = (REPO / "scripts" / "host_nightly_suite.sh").read_text()
    assert "/tmp/legba-container-tests.lock" in nightly
