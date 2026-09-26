# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE REAL-BINDING-PATH tests for the ``inquiry_state`` pack (Program 5
lane 1, planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §3).

Per the operator's "tests must traverse the REAL binding path" rule (memory:
tests-must-traverse-real-binding — the journal_propose precedent in
tests/journal_w4/test_propose_real_binding_path.py): every tool call here
goes through :meth:`legba.data.analysts.agency.agency.Agency.run_pack_tool`
via :class:`~legba.data.analysts.agency.binding.AgencyToolBinding` — the SAME
resolve → govern → record → dispatch → settle pipeline production runs, not a
hand-called handler — and every assertion that a call actually happened reads
the ``action_pack_invocations`` ledger, the durable proof of life.

No analyst kind exists yet to wire the pack in (lane 2's job), so these
bindings are built the way lane 2's deps builder WILL build them: a granted +
allowed ``inquiry_state`` pack, a per-run ``ToolContext(writeback=...)``
carrying an :class:`AnalystContext` — the SAME channel
``journal_propose._insert_proposal`` stamps every proposal with
(``ctx.analyst_id`` == ``wb.analyst_ctx.analyst_id``).

THE SCOPE FENCE is the centerpiece: two descriptors, A and B, share the pack;
B must never read, close, or otherwise observe A's rows.

Runs against the DISPOSABLE test container (``migrated_pg``), never the live db.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.agency import Agency, AgencyToolBinding, ToolContext
from legba.data.analysts.agency.binding import GLOBAL_SCOPE
from legba.data.analysts.agency.inquiry_state import (
    INQUIRY_STATE_PACK_ID,
    INQUIRY_STATE_TOOLS,
)
from legba.data.analysts.agency.tools import WritebackContext
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext
from legba.data.schemas.action_pack import ActionPack

pytestmark = pytest.mark.asyncio

_TAG = "inqpack"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM inquiry_ledger WHERE descriptor_id LIKE $1",
                f"{_TAG}_%",
            )
        await pool.close()


def _pack() -> ActionPack:
    """Exactly as ``descriptors/action_pack_inquiry_state.yaml`` ships."""
    return ActionPack.model_validate(
        {
            "identity": {
                "id": INQUIRY_STATE_PACK_ID,
                "name": "Inquiry State (the ledger an inquiry carries)",
                "schema_uri": "legba/action_pack/1.0.0",
                "version": "b" * 16,
                "state": "active",
                "owner": "program5_inquiry",
                "created": "2026-09-24T00:00:00Z",
            },
            "tools": [{"name": t} for t in INQUIRY_STATE_TOOLS],
        },
        strict=False,
    )


def _binding(pg_pool, *, analyst_id: str) -> AgencyToolBinding:
    """The REAL production shape: granted ∩ allowed ∩ universally-applicable
    (no applies_to_tags/predicate on the pack) resolves EFFECTIVE, and the
    per-run ``ToolContext.writeback`` carries the calling descriptor's own
    identity — the ONE thing the scope fence reads."""
    wb = WritebackContext(
        pg_pool=pg_pool,
        analyst_ctx=AnalystContext(
            analyst_id=analyst_id, analyst_version="0" * 16, run_id=uuid4(),
        ),
    )
    return AgencyToolBinding(
        agency=Agency(),
        pack=_pack(),
        pg_pool=pg_pool,
        tool_context=ToolContext(writeback=wb),
        analyst_grants=[{"pack_id": INQUIRY_STATE_PACK_ID}],
        target_allows=[{"pack_id": INQUIRY_STATE_PACK_ID}],
        scope=GLOBAL_SCOPE,
        requested_by=f"analyst::{analyst_id}",
        budget_account="inquiry_state",
    )


async def _invocation_marker(pg_pool) -> set[str]:
    async with pg_pool.acquire() as conn:
        return {
            str(r["id"])
            for r in await conn.fetch("SELECT id FROM action_pack_invocations")
        }


async def _invocations_since(pg_pool, marker: set[str]) -> list[tuple[str, str, str]]:
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, pack_id, tool_name, outcome FROM action_pack_invocations "
            "ORDER BY occurred_at, id",
        )
    return [
        (r["pack_id"], r["tool_name"], r["outcome"])
        for r in rows
        if str(r["id"]) not in marker
    ]


# ---------------------------------------------------------------------------
# The real pipeline: resolve -> govern -> record -> dispatch -> settle
# ---------------------------------------------------------------------------


async def test_ledger_write_then_read_round_trip_through_the_real_binding(pg_pool):
    analyst_id = f"{_TAG}_pilot"
    binding = _binding(pg_pool, analyst_id=analyst_id)
    marker = await _invocation_marker(pg_pool)

    write_outcome = await binding.run_tool(
        "ledger_write",
        {
            "kind": "observation",
            "text": "the refinery campaign is widening past Rosneft assets",
        },
    )
    assert write_outcome.admitted is True
    assert write_outcome.tool_result.status == "completed"
    entry_id = write_outcome.tool_result.output["id"]

    read_outcome = await binding.run_tool("ledger_read", {})
    assert read_outcome.admitted is True
    entries = read_outcome.tool_result.output["entries"]
    assert [e["id"] for e in entries] == [entry_id]
    assert entries[0]["kind"] == "observation"
    assert entries[0]["status"] == "open"
    assert entries[0]["cited_refs"] == []

    # THE PROOF OF LIFE — the journal_propose precedent's own standard.
    invocations = await _invocations_since(pg_pool, marker)
    assert (INQUIRY_STATE_PACK_ID, "ledger_write", "completed") in invocations
    assert (INQUIRY_STATE_PACK_ID, "ledger_read", "completed") in invocations


async def test_ledger_write_lands_the_opening_cited_refs(pg_pool):
    """Merge fix (2026-09-24): the inquiry kind sends the refs its entry already
    cites at WRITE time (the yield instrument's "anticipated" test needs them);
    ledger_close may later replace them with the closing warrant."""
    from uuid import uuid4
    analyst_id = f"{_TAG}_refs"
    binding = _binding(pg_pool, analyst_id=analyst_id)
    ref = str(uuid4())
    write_outcome = await binding.run_tool(
        "ledger_write",
        {"kind": "observation", "text": "opened on a cited row", "cited_refs": [ref]},
    )
    assert write_outcome.admitted is True
    assert write_outcome.tool_result.status == "completed", write_outcome.tool_result.error
    read_outcome = await binding.run_tool("ledger_read", {})
    entries = read_outcome.tool_result.output["entries"]
    assert [e["cited_refs"] for e in entries] == [[ref]]


async def test_ledger_read_status_filter(pg_pool):
    analyst_id = f"{_TAG}_filter"
    binding = _binding(pg_pool, analyst_id=analyst_id)
    opened = await binding.run_tool(
        "ledger_write",
        {"kind": "hypothesis", "text": "h1", "resolution_test": "t1"},
    )
    entry_id = opened.tool_result.output["id"]
    closed = await binding.run_tool(
        "ledger_write",
        {"kind": "hypothesis", "text": "h2", "resolution_test": "t2"},
    )
    closed_id = closed.tool_result.output["id"]
    close_outcome = await binding.run_tool(
        "ledger_close",
        {"id": closed_id, "status": "refuted", "reason": "counter-evidence arrived"},
    )
    assert close_outcome.tool_result.status == "completed"

    open_only = await binding.run_tool("ledger_read", {"status": "open"})
    assert [e["id"] for e in open_only.tool_result.output["entries"]] == [entry_id]

    refuted_only = await binding.run_tool("ledger_read", {"status": "refuted"})
    assert [e["id"] for e in refuted_only.tool_result.output["entries"]] == [closed_id]


async def test_bad_status_filter_is_a_clean_failure_not_a_500(pg_pool):
    binding = _binding(pg_pool, analyst_id=f"{_TAG}_badstatus")
    outcome = await binding.run_tool("ledger_read", {"status": "nonsense"})
    assert outcome.admitted is True  # governed and dispatched — a handler refusal
    assert outcome.tool_result.status == "failed"
    assert "status must be one of" in outcome.tool_result.error


# ---------------------------------------------------------------------------
# THE SEALED-LEDGER DISCIPLINE, at the pack layer (the DB CHECK is the
# belt-and-suspenders in test_migration_0215_inquiry_ledger.py)
# ---------------------------------------------------------------------------


async def test_hypothesis_without_resolution_test_is_refused_by_the_handler(pg_pool):
    analyst_id = f"{_TAG}_hyp"
    binding = _binding(pg_pool, analyst_id=analyst_id)
    marker = await _invocation_marker(pg_pool)

    outcome = await binding.run_tool(
        "ledger_write", {"kind": "hypothesis", "text": "no test attached"},
    )
    assert outcome.admitted is True
    assert outcome.tool_result.status == "failed"
    assert "resolution_test" in outcome.tool_result.error

    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM inquiry_ledger WHERE descriptor_id = $1",
            analyst_id,
        )
    assert n == 0, "a refused hypothesis must write NOTHING"

    # Still ledgered as a real (failed) invocation — a refusal is not a no-op
    # the governor never saw.
    invocations = await _invocations_since(pg_pool, marker)
    assert (INQUIRY_STATE_PACK_ID, "ledger_write", "failed") in invocations


# ---------------------------------------------------------------------------
# THE SCOPE FENCE — two descriptors, proven
# ---------------------------------------------------------------------------


async def test_scope_fence_read_never_crosses_descriptors(pg_pool):
    a = f"{_TAG}_fence_a"
    b = f"{_TAG}_fence_b"
    binding_a = _binding(pg_pool, analyst_id=a)
    binding_b = _binding(pg_pool, analyst_id=b)

    await binding_a.run_tool(
        "ledger_write", {"kind": "observation", "text": "A's own observation"},
    )

    a_reads = await binding_a.run_tool("ledger_read", {})
    b_reads = await binding_b.run_tool("ledger_read", {})

    assert len(a_reads.tool_result.output["entries"]) == 1
    assert b_reads.tool_result.output["entries"] == [], (
        "descriptor B must never see descriptor A's ledger rows"
    )


async def test_scope_fence_close_never_crosses_descriptors(pg_pool):
    a = f"{_TAG}_fenceclose_a"
    b = f"{_TAG}_fenceclose_b"
    binding_a = _binding(pg_pool, analyst_id=a)
    binding_b = _binding(pg_pool, analyst_id=b)

    written = await binding_a.run_tool(
        "ledger_write",
        {"kind": "hypothesis", "text": "A's hypothesis", "resolution_test": "t"},
    )
    a_entry_id = written.tool_result.output["id"]

    # B tries to close A's row.
    hijack = await binding_b.run_tool(
        "ledger_close",
        {"id": a_entry_id, "status": "confirmed", "reason": "B trying to close A's row"},
    )
    assert hijack.admitted is True
    assert hijack.tool_result.status == "failed"
    assert "no open row" in hijack.tool_result.error

    # A's row is untouched — still open.
    a_reads = await binding_a.run_tool("ledger_read", {"status": "open"})
    assert [e["id"] for e in a_reads.tool_result.output["entries"]] == [a_entry_id]

    # A can close its own row.
    own_close = await binding_a.run_tool(
        "ledger_close",
        {"id": a_entry_id, "status": "confirmed", "reason": "verified by a later read"},
    )
    assert own_close.tool_result.status == "completed"


async def test_scope_fence_write_always_lands_under_the_calling_descriptor(pg_pool):
    """A cannot mint a row under B's name even if it tried to (no such arg
    exists on ledger_write — this proves the DB row, not just the absence of
    an argument)."""
    a = f"{_TAG}_fencewrite_a"
    binding_a = _binding(pg_pool, analyst_id=a)
    outcome = await binding_a.run_tool(
        "ledger_write", {"kind": "expectation", "text": "an expectation"},
    )
    entry_id = outcome.tool_result.output["id"]
    async with pg_pool.acquire() as conn:
        descriptor_id = await conn.fetchval(
            "SELECT descriptor_id FROM inquiry_ledger WHERE id = $1", entry_id,
        )
    assert descriptor_id == a


# ---------------------------------------------------------------------------
# ledger_close: cited_refs is the ONLY writer of that column, and the reason
# rides the row's own text.
# ---------------------------------------------------------------------------


async def test_ledger_close_sets_cited_refs_and_appends_the_reason(pg_pool):
    analyst_id = f"{_TAG}_close_refs"
    binding = _binding(pg_pool, analyst_id=analyst_id)

    written = await binding.run_tool(
        "ledger_write",
        {"kind": "observation", "text": "an observation about a refinery strike"},
    )
    entry_id = written.tool_result.output["id"]
    ref = str(uuid4())

    closed = await binding.run_tool(
        "ledger_close",
        {
            "id": entry_id,
            "status": "confirmed",
            "reason": "a later finding cited the same evidence",
            "cited_refs": [ref],
        },
    )
    assert closed.tool_result.status == "completed"

    read = await binding.run_tool("ledger_read", {})
    entry = read.tool_result.output["entries"][0]
    assert entry["status"] == "confirmed"
    assert entry["cited_refs"] == [ref]
    assert entry["closed_at"] is not None
    assert "a later finding cited the same evidence" in entry["text"]
    assert "an observation about a refinery strike" in entry["text"]


async def test_ledger_close_requires_a_terminal_status(pg_pool):
    analyst_id = f"{_TAG}_close_status"
    binding = _binding(pg_pool, analyst_id=analyst_id)
    written = await binding.run_tool(
        "ledger_write", {"kind": "observation", "text": "x"},
    )
    entry_id = written.tool_result.output["id"]

    outcome = await binding.run_tool(
        "ledger_close", {"id": entry_id, "status": "open", "reason": "nope"},
    )
    assert outcome.tool_result.status == "failed"
    assert "never sets 'open'" in outcome.tool_result.error


async def test_double_close_is_refused(pg_pool):
    analyst_id = f"{_TAG}_double_close"
    binding = _binding(pg_pool, analyst_id=analyst_id)
    written = await binding.run_tool(
        "ledger_write", {"kind": "observation", "text": "x"},
    )
    entry_id = written.tool_result.output["id"]
    first = await binding.run_tool(
        "ledger_close", {"id": entry_id, "status": "confirmed", "reason": "r1"},
    )
    assert first.tool_result.status == "completed"

    second = await binding.run_tool(
        "ledger_close", {"id": entry_id, "status": "withdrawn", "reason": "r2"},
    )
    assert second.tool_result.status == "failed"
    assert "no open row" in second.tool_result.error


# ---------------------------------------------------------------------------
# No writeback wired -> a clean failed ToolResult, not a crash
# ---------------------------------------------------------------------------


async def test_no_writeback_is_a_clean_failure_on_every_tool(pg_pool):
    binding = AgencyToolBinding(
        agency=Agency(),
        pack=_pack(),
        pg_pool=pg_pool,
        tool_context=ToolContext(),  # no writeback
        analyst_grants=[{"pack_id": INQUIRY_STATE_PACK_ID}],
        target_allows=[{"pack_id": INQUIRY_STATE_PACK_ID}],
        scope=GLOBAL_SCOPE,
        requested_by="analyst::no_writeback",
    )
    for tool_name, args in (
        ("ledger_read", {}),
        ("ledger_write", {"kind": "observation", "text": "x"}),
        ("ledger_close", {"id": str(uuid4()), "status": "confirmed", "reason": "r"}),
    ):
        outcome = await binding.run_tool(tool_name, args)
        assert outcome.tool_result.status == "failed"
        assert "no writeback surface wired" in outcome.tool_result.error
