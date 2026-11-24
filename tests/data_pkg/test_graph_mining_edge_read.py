# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roll 85 — graph mining reads its edge set deterministically.

The augment read ``LIMIT 20000`` with no ORDER BY while 36,443 world edges
matched on live, so a plan-dependent ~45% of the brokerage graph was dropped
each run. Both reads (the projection and the flag-off substrate read) now
sort newest first with a stable tie-break and take a declared safety cap far
above the live count; reaching it is logged.
"""
from __future__ import annotations

import logging

import networkx as nx
import pytest

from legba.data.analysts.deterministic_handlers import graph_mining as gm


class _Conn:
    def __init__(self, rows=()):
        self.calls: list[tuple[str, tuple]] = []
        self._rows = list(rows)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._rows


class _Deps:
    def __init__(self, conn):
        class _Pool:
            def acquire(self_inner):
                class _Cm:
                    async def __aenter__(self_cm):
                        return conn

                    async def __aexit__(self_cm, *exc):
                        return False
                return _Cm()
        self.pg_pool = _Pool()


def _state(value):
    async def _f(conn):
        return value, None
    return _f


@pytest.mark.asyncio
@pytest.mark.parametrize("state,table", [("ok", "graph_arcs"), ("projection_disabled", "entity_edges e")])
async def test_both_reads_sort_newest_first_and_take_the_declared_cap(monkeypatch, state, table) -> None:
    monkeypatch.setattr(gm, "graph_projection_state", _state(state))
    conn = _Conn()
    assert await gm._augment_from_nexuses(_Deps(conn), nx.MultiDiGraph()) == 0
    (sql, args), = conn.calls
    assert table in sql
    assert "LIMIT 20000" not in sql
    assert "ORDER BY" in sql and "valid_from DESC NULLS LAST" in sql and "LIMIT $2" in sql
    assert args == (list(gm.MINING_FAMILIES), gm.MAX_MINING_EDGES)


def test_the_cap_is_far_above_the_live_edge_count() -> None:
    # 36,443 matching world edges on live, 2026-11-15
    assert gm.MAX_MINING_EDGES >= 2 * 36_443


@pytest.mark.asyncio
async def test_reaching_the_cap_is_logged(monkeypatch, caplog) -> None:
    monkeypatch.setattr(gm, "graph_projection_state", _state("ok"))
    monkeypatch.setattr(gm, "MAX_MINING_EDGES", 2)
    rows = [{"subject": "A", "object": "B", "intermediary": None, "polarity": -1, "rel_type": "hostile_to"},
            {"subject": "B", "object": "C", "intermediary": "D", "polarity": 1, "rel_type": "allied_with"}]
    conn = _Conn(rows)
    g = nx.MultiDiGraph()
    with caplog.at_level(logging.WARNING, logger=gm.logger.name):
        assert await gm._augment_from_nexuses(_Deps(conn), g) == 3
    assert "graph_mining.edge.cap_reached" in caplog.text
    assert conn.calls[0][1][1] == 2
