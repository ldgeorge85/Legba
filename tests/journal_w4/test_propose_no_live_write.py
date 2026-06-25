# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE GATING NO-LIVE-WRITE TEST (plan §7 / §12 Wave 4 — the safety precondition).

This is the safety CLAIM the whole propose-and-gate design rests on, expressed as
a TEST (not a comment): a ``propose_*`` call writes ONLY a ``journal_proposals``
row and NEVER a live ``facts`` / ``hypotheses`` / ``nexuses`` / ``situations`` /
``analyst_outputs`` row or a descriptor mutation. The journal SUGGESTED; only a
human (the accept-apply worker) ever CAUSES a live change.

Per the build mandate this test is built FIRST and kept green; no ``propose_*``
tool is registered anywhere until it passes. It runs against the DISPOSABLE
container (conftest), NEVER the live ``legba`` db.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from legba.data.analysts.agency.journal_propose import (
    propose_change_tool,
    propose_correction_tool,
    propose_self_revision_tool,
)
from legba.data.analysts.agency.tools import ToolCall, ToolContext, WritebackContext
from legba.data.provenance import AnalystContext

pytestmark = pytest.mark.asyncio

# Every live substrate table a propose_* call is forbidden to touch. If any of
# these grows a row during a propose_* call, the off-chain / suggested-not-caused
# invariant (§3.1 / §7.1) is BROKEN.
_FORBIDDEN_LIVE_TABLES = (
    "facts",
    "hypotheses",
    "nexuses",
    "situations",
    "analyst_outputs",
)


def _ctx_with_writeback(pg_pool) -> ToolContext:
    """A ToolContext carrying the per-run WritebackContext the actor injects (the
    run pg_pool + a fresh AnalystContext) — the SAME surface the live GATHER loop
    hands a propose tool. No SubstrateQueryPort / queue / emit — a propose tool
    must reach NONE of them."""
    analyst_ctx = AnalystContext(
        analyst_id="journal_assessor",
        analyst_version="0" * 64,
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )
    return ToolContext(
        writeback=WritebackContext(
            pg_pool=pg_pool, analyst_ctx=analyst_ctx, publish_fn=None
        )
    )


async def _count(pg_pool, table: str) -> int:
    async with pg_pool.acquire() as conn:
        return int(await conn.fetchval(f"SELECT count(*) FROM {table}"))


@pytest.mark.parametrize(
    "tool, expected_kind, diff",
    [
        (
            propose_correction_tool,
            "correction",
            {"op": "supersede_fact", "subject": "Postgres", "predicate": "status",
             "value": "up", "stale_value": "down"},
        ),
        (
            propose_change_tool,
            "change",
            {"op": "update_descriptor", "family": "analyst",
             "descriptor_id": "country_critic", "patch": {"cadence": {"cooldown_seconds": 21000}}},
        ),
        (
            propose_self_revision_tool,
            "self_revision",
            {"op": "revise_prompt", "target_analyst_id": "journal_assessor",
             "summary": "tighten the field-notes seam", "new_prompt_text": "..."},
        ),
    ],
)
async def test_propose_writes_only_a_journal_proposals_row(
    pg_pool, tool, expected_kind, diff
):
    """THE GATING ASSERTION. Each propose_* call lands EXACTLY ONE pending
    journal_proposals row and writes ZERO live substrate rows + ZERO descriptor
    mutations."""
    # Pre-state: every forbidden table is empty (the conftest truncates).
    before = {t: await _count(pg_pool, t) for t in _FORBIDDEN_LIVE_TABLES}
    assert all(v == 0 for v in before.values())
    assert await _count(pg_pool, "journal_proposals") == 0

    ctx = _ctx_with_writeback(pg_pool)
    call = ToolCall(
        pack_id="journal_propose",
        tool_name="propose",
        args={
            "rationale": "I noticed this from my own run health and it warrants a fix.",
            "diff": diff,
            "cited_substrate_refs": [str(uuid4())],
        },
        requested_by="analyst::journal_assessor",
    )

    result = await tool(call, _PACK, ctx)

    # The tool succeeded and reports a PENDING proposal — never an applied change.
    assert result.status == "completed", result.error
    assert result.output["status"] == "pending"
    assert result.output["proposal_kind"] == expected_kind
    assert "proposal_id" in result.output

    # EXACTLY ONE pending journal_proposals row, of the right kind, stamped to the
    # run — and its diff round-trips.
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM journal_proposals")
    assert len(rows) == 1
    row = rows[0]
    assert row["proposal_kind"] == expected_kind
    assert row["status"] == "pending"
    assert row["proposed_by_analyst_id"] == "journal_assessor"
    assert row["decided_by"] is None
    stored_diff = row["diff"]
    if isinstance(stored_diff, str):
        stored_diff = json.loads(stored_diff)
    assert stored_diff == diff

    # THE SAFETY CLAIM: NOT ONE live substrate row was written by the propose call.
    after = {t: await _count(pg_pool, t) for t in _FORBIDDEN_LIVE_TABLES}
    assert after == before, (
        f"propose_{expected_kind} wrote a LIVE substrate row — the off-chain / "
        f"suggested-not-caused invariant is BROKEN. before={before} after={after}"
    )


async def test_propose_requires_rationale_and_diff(pg_pool):
    """A propose call with no rationale / no diff is a clean failure — and STILL
    writes no row (the guard runs before the INSERT)."""
    ctx = _ctx_with_writeback(pg_pool)

    r1 = await propose_correction_tool(
        ToolCall(pack_id="journal_propose", tool_name="propose_correction",
                 args={"diff": {"op": "x"}}, requested_by="a"),
        _PACK, ctx,
    )
    assert r1.status == "failed" and "rationale" in (r1.error or "")

    r2 = await propose_change_tool(
        ToolCall(pack_id="journal_propose", tool_name="propose_change",
                 args={"rationale": "why"}, requested_by="a"),
        _PACK, ctx,
    )
    assert r2.status == "failed" and "diff" in (r2.error or "")

    assert await _count(pg_pool, "journal_proposals") == 0


async def test_propose_without_writeback_is_a_clean_failure():
    """No ctx.writeback wired → a clean failed ToolResult naming the missing
    surface, NEVER a raise and NEVER a silent no-op."""
    ctx = ToolContext()  # no writeback
    r = await propose_correction_tool(
        ToolCall(pack_id="journal_propose", tool_name="propose_correction",
                 args={"rationale": "w", "diff": {"op": "x"}}, requested_by="a"),
        _PACK,
        ctx,
    )
    assert r.status == "failed"
    assert "writeback" in (r.error or "")


# A minimal ActionPack stand-in (the handlers only read pack.identity.id).
from legba.data.schemas.action_pack import ActionPack  # noqa: E402

_PACK = ActionPack.model_validate(
    {
        "identity": {
            "id": "journal_propose",
            "name": "journal_propose",
            "schema_uri": "legba/action_pack/1.0.0",
            "version": "a" * 16,
            "state": "active",
            "owner": "journal_revival",
            "created": "2026-06-25T00:00:00Z",
        },
        "tools": [
            {"name": "propose_correction"},
            {"name": "propose_change"},
            {"name": "propose_self_revision"},
        ],
    },
    strict=False,
)
