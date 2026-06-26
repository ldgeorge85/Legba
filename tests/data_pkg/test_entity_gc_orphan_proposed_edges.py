# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W4 D2 + D25 tests — entity_gc orphan-proposed-edge quarantine + descriptor.

Pure, no-DB unit tests:

  * the new ``_quarantine_orphan_proposed_edges`` operation issues the expected
    UPDATE and returns the affected-row count parsed from the asyncpg command
    tag (via a fake pool/conn);
  * the ``handle`` envelope now carries the ``orphan_proposed_edges`` counter and
    tags the run ``gc_actions_taken`` when it acts;
  * the no-deps path stays zero (refuse-loud / safe default);
  * the authored ``descriptors/analyst_entity_gc.yaml`` is a structurally valid
    active deterministic META analyst that dispatches the ``entity_gc``
    sub-handler.
"""

from __future__ import annotations

import pathlib
from uuid import uuid4

import yaml

from legba.data.analysts.deterministic_handlers import entity_gc
from legba.data.analysts.deterministic import SUB_HANDLERS, OUTPUT_KIND_BY_SUB_HANDLER
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Fake asyncpg pool/conn — records SQL, returns a canned command tag.
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, command_tag: str = "UPDATE 0"):
        self._command_tag = command_tag
        self.executed: list[str] = []

    async def execute(self, sql: str, *args):
        self.executed.append(sql)
        return self._command_tag


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    """Only ``acquire()`` is exercised by _quarantine_orphan_proposed_edges."""

    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


class _FakeDeps:
    def __init__(self, pool):
        self.pg_pool = pool


# ---------------------------------------------------------------------------
# D25 — _quarantine_orphan_proposed_edges operation
# ---------------------------------------------------------------------------


async def test_quarantine_orphan_proposed_edges_parses_count():
    """Returns the affected-row count from the asyncpg ``UPDATE n`` tag."""
    conn = _FakeConn("UPDATE 42")
    pool = _FakePool(conn)
    n = await entity_gc._quarantine_orphan_proposed_edges(pool)
    assert n == 42
    # The single UPDATE was issued.
    assert len(conn.executed) == 1
    sql = conn.executed[0]
    # Only touches pending rows (never disturbs promoted/rejected/already-orphaned).
    assert "status = 'pending'" in sql
    # Flips to the new terminal orphaned status (non-destructive — no DELETE).
    assert "status = 'orphaned'" in sql
    assert "DELETE" not in sql.upper()
    # Orphan = source OR target entity absent from entity_profiles.canonical_name.
    assert "source_entity" in sql
    assert "target_entity" in sql
    assert "canonical_name" in sql
    assert "entity_profiles" in sql


async def test_quarantine_orphan_proposed_edges_zero_tag():
    conn = _FakeConn("UPDATE 0")
    n = await entity_gc._quarantine_orphan_proposed_edges(_FakePool(conn))
    assert n == 0


# ---------------------------------------------------------------------------
# handle() — envelope now carries orphan_proposed_edges + acts via fake pool
# ---------------------------------------------------------------------------


async def test_handle_emits_orphan_proposed_edges_counter_via_fake_pool():
    """Only the orphan-proposed-edges leg is enabled; others are toggled off so
    the test stays pure (no entity_profiles / sources reads)."""
    conn = _FakeConn("UPDATE 7")
    deps = _FakeDeps(_FakePool(conn))
    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": False,
            "run_orphan_proposed_edges": True,
        },
        deps,
    )
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["orphan_proposed_edges"] == 7
    # acted → tagged
    assert "gc_actions_taken" in result.finding.tags
    # other counters untouched / zero
    assert data["dormant_entities"] == 0
    assert data["duplicate_flags"] == 0
    assert data["orphan_edges"] == 0
    assert data["sources_paused"] == 0


async def test_handle_no_deps_orphan_proposed_edges_zero():
    """No-deps path leaves the new counter at zero and takes no action."""
    result = await entity_gc.handle([], {"sub_handler": "entity_gc"}, None)
    data = result.finding.data
    assert data["orphan_proposed_edges"] == 0
    assert "gc_actions_taken" not in result.finding.tags


async def test_handle_orphan_proposed_edges_failure_is_swallowed():
    """A failing leg is logged and degrades to zero — does NOT abort the run or
    poison the other (already-zero) counters."""

    class _BoomConn(_FakeConn):
        async def execute(self, sql, *args):
            raise RuntimeError("boom")

    deps = _FakeDeps(_FakePool(_BoomConn()))
    result = await entity_gc.handle(
        [],
        {
            "sub_handler": "entity_gc",
            "run_dormant": False,
            "run_duplicates": False,
            "run_orphans": False,
            "run_source_pause": False,
            "run_orphan_proposed_edges": True,
        },
        deps,
    )
    assert result.finding.data["orphan_proposed_edges"] == 0


async def test_handle_shape_contract_unchanged():
    result = await entity_gc.handle(
        [], {"sub_handler": "entity_gc", "run_id": uuid4()}, None,
    )
    assert isinstance(result.finding, FindingPayload)
    assert result.finding.data["sub_handler"] == "entity_gc"
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# D2 — the new active descriptor activates the entity_gc sub-handler
# ---------------------------------------------------------------------------


_DESCRIPTOR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "descriptors"
    / "analyst_entity_gc.yaml"
)


def test_entity_gc_descriptor_exists_and_is_valid():
    assert _DESCRIPTOR.is_file(), f"missing {_DESCRIPTOR}"
    body = yaml.safe_load(_DESCRIPTOR.read_text())

    ident = body["identity"]
    assert ident["id"] == "entity_gc"
    assert ident["kind"] == "deterministic"
    # ACTIVE so the cadence reminder is wired on register (a draft would sit dark).
    assert ident["state"] == "active"

    method = body["method"]
    assert method["kind"] == "deterministic"
    assert method["impl"] == "legba.data.analysts.deterministic:run_method"
    # Routes to the entity_gc sub-handler that is already in the dispatch table.
    assert method["sub_handler"] == "entity_gc"
    assert method["sub_handler"] in SUB_HANDLERS
    assert method["sub_handler"] in OUTPUT_KIND_BY_SUB_HANDLER

    # META analyst — no targets selector → single global cadence run.
    sub = body["subscription"]
    assert "targets" not in sub

    cadence = body["cadence"]
    assert isinstance(cadence["fallback_schedule"], str) and cadence["fallback_schedule"]
    assert isinstance(cadence["cooldown_seconds"], int)
