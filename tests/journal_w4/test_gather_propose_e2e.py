# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end GATHER → propose → queue (plan §7 / §4.9 / Wave 4).

Proves the FULL governed path the runtime wires: the journal's GATHER loop
recognises a propose tool as an ``extra_write_tools`` name, routes it through the
journal_propose pack's per-tool binding (NOT the read binding), the three-way gate
ADMITS it (the META path self-allows the granted pack), the handler reaches the
per-run WritebackContext, and EXACTLY ONE pending journal_proposals row lands —
while the read binding still serves read tools. This is the wiring the
test_journal_assessor_wiring drift tests assert statically, exercised dynamically
against the disposable container."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from legba.data.analysts.agency import (
    Agency,
    AgencyToolBinding,
    ToolContext,
    WritebackContext,
)
from legba.data.analysts.agency.journal_propose import JOURNAL_PROPOSE_PACK_ID
from legba.data.analysts.agency.resolution import TargetScopeView
from legba.data.analysts.inline_target import InlineTargetDeps, _gather
from legba.data.provenance import AnalystContext
from legba.data.schemas.action_pack import ActionPack, ActionPackRef

pytestmark = pytest.mark.asyncio


def _propose_pack() -> ActionPack:
    return ActionPack.model_validate(
        {
            "identity": {
                "id": JOURNAL_PROPOSE_PACK_ID, "name": "journal_propose",
                "schema_uri": "legba/action_pack/1.0.0", "version": "a" * 16,
                "state": "active", "owner": "journal_revival",
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


class _ScriptedLLM:
    """Emits a propose_change tool call, then {"done": true}. subprovider lets
    the inline_target temperature-drop logic run unbothered."""

    subprovider = "stub-test"

    def __init__(self) -> None:
        self._turns = [
            json.dumps({
                "tool": "propose_change",
                "args": {
                    "rationale": "country_critic cadence looks halved — propose a fix",
                    "diff": {"op": "update_descriptor", "family": "analyst",
                             "descriptor_id": "country_critic",
                             "patch": {"cadence": {"cooldown_seconds": 21000}}},
                    "cited_substrate_refs": [str(uuid4())],
                },
            }),
            json.dumps({"done": True}),
        ]
        self._i = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        out = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1

        class _R:
            content = out
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "reasoning_tokens": 0}

        return _R()


async def test_gather_routes_propose_through_journal_propose_binding(pg_pool):
    """A propose tool emitted in GATHER lands ONE pending journal_proposals row via
    the governed journal_propose binding — proving extra_write_tools routing +
    the writeback injection + the three-way gate admit."""
    agency = Agency()
    pack = _propose_pack()
    grants = [ActionPackRef(pack_id=JOURNAL_PROPOSE_PACK_ID)]

    # The per-run WritebackContext the actor injects for a write pack.
    analyst_ctx = AnalystContext(
        analyst_id="journal_assessor", analyst_version="0" * 64,
        run_id=uuid4(), target_id=None, target_version=None,
    )
    propose_ctx = ToolContext(
        writeback=WritebackContext(pg_pool=pg_pool, analyst_ctx=analyst_ctx, publish_fn=None)
    )
    # META self-allow: target_allows includes the granted pack (the
    # _gather_binding_for_target META path does this in production).
    propose_binding = AgencyToolBinding(
        agency=agency, pack=pack, pg_pool=pg_pool, tool_context=propose_ctx,
        analyst_grants=grants, target_allows=grants,
        scope=TargetScopeView(target_id="__global__"),
        requested_by="analyst::journal_assessor", budget_account="journal_propose",
    )

    deps = InlineTargetDeps(llm=_ScriptedLLM(), max_tokens=256, max_rounds=4)

    # The journal run_method builds these per-tool bindings into gather_tool_bindings.
    from legba.data.analysts.agency.journal_propose import JOURNAL_PROPOSE_TOOLS
    tool_bindings = {t: propose_binding for t in JOURNAL_PROPOSE_TOOLS}

    steps: list[dict] = []
    await _gather(
        deps,
        binding=None,            # no read binding needed for this path
        user_prompt="reflect",
        target_id=None,
        analyst_id="journal_assessor",
        steps=steps,
        tool_bindings=tool_bindings,
        extra_write_tools=JOURNAL_PROPOSE_TOOLS,
    )

    # EXACTLY ONE pending proposal landed via the governed path.
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch("SELECT proposal_kind, status FROM journal_proposals")
    assert len(rows) == 1
    assert rows[0]["proposal_kind"] == "change"
    assert rows[0]["status"] == "pending"

    # The GATHER trace recorded an ADMITTED tool call for the propose tool.
    tool_steps = [s for s in steps if s.get("kind") == "tool_call"]
    assert any(s.get("tool") == "propose_change" and s.get("admitted") for s in tool_steps), (
        f"propose_change was not admitted through the journal_propose binding: {steps}"
    )
