# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AGE :DerivedFrom output-lineage mirror (graph-and-data Wave-1b, item 2).

Pure-unit: a fake asyncpg connection records the SQL the hook issues, so we can
assert the Output vertex + DerivedFrom edges are MERGEd (one per parent) on the
SAME connection, self-loops skipped, and a cypher error is swallowed (the hook
must never fail the output write).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.provenance.output_graph import (
    make_conn_age_output_hook,
    upsert_derived_from_edges,
)


class _FakeConn:
    def __init__(self, *, fail: bool = False) -> None:
        self.statements: list[str] = []
        self._fail = fail

    async def execute(self, sql: str, *args):
        self.statements.append(sql)
        # The first two statements (LOAD age / search_path) always succeed; a
        # cypher MERGE may be configured to fail.
        if self._fail and "cypher(" in sql:
            raise RuntimeError("AGE not loaded")
        return "SELECT 1"


@pytest.mark.asyncio
async def test_merges_output_vertex_and_one_edge_per_parent():
    conn = _FakeConn()
    out = uuid4()
    parents = [uuid4(), uuid4()]

    written = await upsert_derived_from_edges(
        conn, output_id=out, derived_from=parents
    )
    assert written == 2

    merges = [s for s in conn.statements if "cypher(" in s]
    # 1 vertex MERGE + 2 edge MERGEs.
    assert len(merges) == 3
    edge_merges = [s for s in merges if "DerivedFrom" in s]
    assert len(edge_merges) == 2
    # Each parent id appears in an edge MERGE; the output id appears throughout.
    for p in parents:
        assert any(str(p) in s for s in edge_merges)
    assert all(str(out) in s for s in edge_merges)
    # Prep statements ran (LOAD age / search_path) on the same conn.
    assert any("LOAD 'age'" in s for s in conn.statements)


@pytest.mark.asyncio
async def test_root_output_merges_vertex_only_no_edges():
    conn = _FakeConn()
    out = uuid4()
    written = await upsert_derived_from_edges(conn, output_id=out, derived_from=[])
    assert written == 0
    merges = [s for s in conn.statements if "cypher(" in s]
    assert len(merges) == 1  # vertex only
    assert "DerivedFrom" not in merges[0]


@pytest.mark.asyncio
async def test_self_loop_parent_skipped():
    conn = _FakeConn()
    out = uuid4()
    # A parent equal to the output id must not produce a self-edge.
    written = await upsert_derived_from_edges(
        conn, output_id=out, derived_from=[out]
    )
    assert written == 0
    assert not [s for s in conn.statements if "DerivedFrom" in s]


@pytest.mark.asyncio
async def test_cypher_error_is_swallowed():
    """A graph hiccup must NEVER raise — the output write already committed."""
    conn = _FakeConn(fail=True)
    out = uuid4()
    written = await upsert_derived_from_edges(
        conn, output_id=out, derived_from=[uuid4()]
    )
    # No exception propagated; nothing counted as written.
    assert written == 0


@pytest.mark.asyncio
async def test_hook_factory_matches_age_edge_hook_signature():
    """make_conn_age_output_hook returns a (output_id, derived_from) coroutine
    callable — the AgeEdgeHook write_analyst_output expects."""
    conn = _FakeConn()
    hook = make_conn_age_output_hook(conn)
    out = uuid4()
    res = await hook(out, [uuid4()])
    assert res is None
    assert any("DerivedFrom" in s for s in conn.statements)
