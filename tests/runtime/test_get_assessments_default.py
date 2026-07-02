# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S8-T5(d): ``get_assessments`` defaults to the LIVE assessment producers.

The journal + consult read the platform's own conclusions through
``PostgresQdrantSubstrateQueryPort.get_assessments``. It used to default (no
explicit ``analyst_id``) to the RETIRED ``country_assessor`` monolith — a dead
surface. It now defaults to ``_ASSESSMENT_PRODUCER_ANALYSTS``: the four bounded
P2 units + the per-country and world compositions.

These are pure SQL-shape assertions over a fake pool that records the executed
query + params — no live substrate (that path is covered DB-gated in
tests/runtime/test_substrate_query_port.py).
"""
from __future__ import annotations

import pytest

from legba.runtime.substrate_query_port import (
    _ASSESSMENT_PRODUCER_ANALYSTS,
    PostgresQdrantSubstrateQueryPort,
)


class _FakeConn:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *params):
        self.fetch_calls.append((sql, params))
        return []


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _port(conn: _FakeConn) -> PostgresQdrantSubstrateQueryPort:
    return PostgresQdrantSubstrateQueryPort(pg_pool=_FakePool(conn), qdrant_client=None)


def test_producer_constant_is_the_live_set_not_the_monolith() -> None:
    producers = set(_ASSESSMENT_PRODUCER_ANALYSTS)
    # The two live compositions.
    assert {"country_composition", "world_assessor"} <= producers
    # The four bounded P2 units.
    assert {
        "leadership_transition",
        "energy_security",
        "escalation",
        "narrative_coordination",
    } <= producers
    # The retired first-order monolith is GONE from the default surface.
    assert "country_assessor" not in producers


@pytest.mark.asyncio
async def test_get_assessments_default_targets_live_producers() -> None:
    conn = _FakeConn()
    await _port(conn).get_assessments()
    assert len(conn.fetch_calls) == 1
    sql, params = conn.fetch_calls[0]
    # Default analyst filter is a parameterized text[] ANY(), not an inline
    # literal and not the retired monolith.
    assert "f.analyst_id = ANY($1::text[])" in sql
    assert "country_assessor" not in sql
    # First bound param is the live producer list.
    assert params[0] == list(_ASSESSMENT_PRODUCER_ANALYSTS)
    assert "country_assessor" not in params[0]


@pytest.mark.asyncio
async def test_get_assessments_explicit_analyst_id_overrides_default() -> None:
    conn = _FakeConn()
    await _port(conn).get_assessments(analyst_id="world_assessor")
    sql, params = conn.fetch_calls[0]
    # Explicit id → single-value equality, no ANY() default.
    assert "f.analyst_id = $1" in sql
    assert "ANY($1::text[])" not in sql
    assert params[0] == "world_assessor"
