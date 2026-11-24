# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/graph/arcs`` with ``direction=both``: the anchor's two arms are ONE
term of the WHERE clause (roll 84, found by lane P-7).

Before the fix the anchor clause was ``(out-arm) OR (in-arm)`` joined to the
plane and time filters with AND, so SQL read it as ``plane AND time AND
out-arm OR in-arm`` — every plane's incoming arcs leaked into the result.
Pure: the SQL text is checked, no database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from legba.data.registry.graph_arcs_api import _ego_sql


def _where(sql: str) -> str:
    return sql.split(" WHERE ", 1)[1].rsplit(" LIMIT ", 1)[0]


def _top_level_ors(where: str) -> int:
    """How many ``OR`` sit at parenthesis depth 0 (each would bind looser
    than the AND-joined filters around it)."""
    depth, n, i = 0, 0, 0
    while i < len(where):
        c = where[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and where.startswith(" OR ", i):
            n += 1
        i += 1
    return n


@pytest.mark.parametrize("direction", ["both", "out", "in"])
@pytest.mark.parametrize("as_of", [None, datetime(2026, 11, 14, tzinfo=timezone.utc)])
@pytest.mark.parametrize("seeds", [None, [("finding", uuid4())]])
def test_no_or_escapes_the_plane_and_time_filters(direction, as_of, seeds) -> None:
    args: list = []
    sql = _ego_sql(plane="world", as_of=as_of, direction=direction, arc_types=["cites"],
                   kinds=["signal"], anchor=("finding", uuid4()), seeds=seeds, args=args)
    assert _top_level_ors(_where(sql)) == 0, sql
    assert "a.plane = $1" in sql


def test_both_directions_are_one_parenthesised_term() -> None:
    args: list = []
    sql = _ego_sql(plane="world", as_of=None, direction="both", arc_types=[], kinds=[],
                   anchor=("finding", uuid4()), seeds=None, args=args)
    terms = _where(sql).split(" AND (")
    assert any(t.startswith("(a.from_kind = $") and " OR (a.to_kind = $" in t for t in terms), sql
