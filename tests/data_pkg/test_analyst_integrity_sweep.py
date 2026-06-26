# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``integrity_sweep`` deterministic sub-handler (DIRECTION §9).

Covers the two properties that distinguish it from the 2.4-deleted
``integrity_verification`` predecessor: it **refuses loud** (a missing relation
or absent pool raises rather than zeroing) and it emits an **honest** summary
(a 0-issue finding only ever means the checks genuinely ran clean).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import integrity_sweep
from legba.runtime.analyst_method import AnalystMethodResult


class _FakeConn:
    def __init__(self, values: list[Any]):
        self._values = list(values)
        self.calls = 0

    async def fetchval(self, sql: str, *args: Any) -> Any:
        v = self._values[self.calls]
        self.calls += 1
        if isinstance(v, Exception):
            raise v
        return v


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, values: list[Any]):
        self.conn = _FakeConn(values)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


class _FakeDeps:
    def __init__(self, pool: Any):
        self.pg_pool = pool


def _run(values: list[Any] | None, options: dict[str, Any] | None = None):
    deps = _FakeDeps(_FakePool(values) if values is not None else None)
    return asyncio.run(integrity_sweep.handle([], options or {}, deps))


def test_registered_in_dispatch_tables() -> None:
    assert SUB_HANDLERS.get("integrity_sweep") is integrity_sweep.handle
    assert "integrity_sweep" in OUTPUT_KIND_BY_SUB_HANDLER


def test_clean_sweep_emits_honest_zero_finding() -> None:
    res = _run([0, 0, 0, 0, 0, 0, 0])  # all seven checks clean
    assert isinstance(res, AnalystMethodResult)
    assert res.finding.data["total_issues"] == 0
    assert "integrity_clean" in res.finding.tags
    assert "integrity_issues_present" not in res.finding.tags
    assert len(res.finding.data["issues"]) == 7  # every check ran
    assert res.usage["completion_tokens"] == 0  # deterministic: zero LLM spend


def test_issues_surface_with_counts_and_tag() -> None:
    res = _run([0, 0, 7, 24, 0, 0, 0])  # the live proposed_edges orphan counts
    assert res.finding.data["total_issues"] == 31
    assert res.finding.data["issues"]["orphan_proposed_edges_source"] == 7
    assert res.finding.data["issues"]["orphan_proposed_edges_target"] == 24
    assert "integrity_issues_present" in res.finding.tags
    assert "integrity_clean" not in res.finding.tags


def test_dangling_derived_from_check_registered_and_counted() -> None:
    """D23/D10: the dangling-derived_from audit is the LAST check and its count
    surfaces under the documented issue key."""
    # Check order: signal/signal, signal/entity, pe/source, pe/target,
    # facts_no_evidence, broken_supersession, dangling_derived_from.
    res = _run([0, 0, 0, 0, 0, 0, 101506])
    issues = res.finding.data["issues"]
    assert "dangling_analyst_output_derived_from" in issues
    assert issues["dangling_analyst_output_derived_from"] == 101506
    assert res.finding.data["total_issues"] == 101506
    assert "integrity_issues_present" in res.finding.tags


def test_dangling_derived_from_check_is_last_check_key() -> None:
    """Pin the new check's identity + position so ordering drift is caught."""
    keys = [k for k, _sql in integrity_sweep._CHECKS]
    assert keys[-1] == "dangling_analyst_output_derived_from"
    assert len(keys) == 7
    # The SQL references analyst_outputs.derived_from + the lineage tables.
    sql = dict(integrity_sweep._CHECKS)["dangling_analyst_output_derived_from"]
    assert "unnest(ao.derived_from)" in sql
    for tbl in ("signals", "analyst_outputs", "facts", "entity_profiles"):
        assert tbl in sql


def test_no_pool_refuses_loud() -> None:
    # No live substrate → raise, never a zeroed finding.
    with pytest.raises(RuntimeError, match="requires a live deps.pg_pool"):
        _run(None)


def test_missing_relation_refuses_loud() -> None:
    # A failing check (e.g. a dropped relation) MUST propagate, not be swallowed
    # into a zeroed clean finding — the exact predecessor bug this re-home fixes.
    boom = RuntimeError('relation "proposed_edges" does not exist')
    with pytest.raises(RuntimeError, match="does not exist"):
        _run([0, 0, boom, 0, 0, 0, 0])
