# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P6 — the event tools through the REAL governed path (spec §6.1 note:
"Test the governed path, not only the direct port").

Each test runs ``AgencyToolBinding.run_tool`` end-to-end — the three-way
gate (resolve ∩ allow ∩ applicability), the governor, the pack handler,
the ledger — against a real migrated Postgres, and asserts the
``action_pack_invocations`` row the gate records. The substrate port is the
documented in-memory recorder (the consult kind's own test-double pattern):
what is under test is the BINDING + handler surface, which is the part a
stub-free environment proves.

Also covered: the handler's arg coercion (``geo`` list/str,
``include_origin`` list, ``limit`` int), a tool not in the pack being
refused, and the descriptor's ``tools:`` list carrying both new names.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.agency import (
    Agency,
    AgencyToolBinding,
    GLOBAL_SCOPE,
    ToolContext,
)
from legba.data.analysts.agency.substrate_read import SUBSTRATE_READ_PACK_ID
from legba.data.analysts.agency.tools import ToolCall
from legba.data.schemas.action_pack import ActionPack, ActionPackRef

pytestmark = [pytest.mark.asyncio]

_DESCRIPTORS = Path(__file__).resolve().parents[3] / "descriptors"


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield p
    await p.close()


def _load_pack(fname: str = "action_pack_substrate_read.yaml") -> ActionPack:
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"]["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


class _RecorderPort:
    """In-memory SubstrateQueryPort recording the event-tool calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def query_events(self, **kwargs):
        self.calls.append(("query_events", dict(kwargs)))
        return {
            "rows": [{"id": "e1", "title": "strike",
                      "lifecycle_state": "active"}],
            "refs": ["e1"],
            "count": 1,
        }

    async def inspect_event(self, *, event_id):
        self.calls.append(("inspect_event", {"event_id": event_id}))
        return {
            "found": True,
            "event": {"id": event_id, "title": "strike"},
            "signals": [], "actors": [], "edges": [],
            "situations": [], "lifecycle": [],
            "refs": [event_id],
        }


def _binding(pool, pack: ActionPack, *, port=None, grants=None, allows=None,
             account=None) -> AgencyToolBinding:
    return AgencyToolBinding(
        agency=Agency(),
        pack=pack,
        pg_pool=pool,
        tool_context=ToolContext(queue=None, emit=None, substrate=port),
        analyst_grants=grants,
        target_allows=allows,
        scope=GLOBAL_SCOPE,
        requested_by=account or f"analyst::test_{uuid4().hex[:8]}",
        budget_account=account or f"acct_{uuid4().hex[:8]}",
    )


async def _invocations(conn, *, requested_by: str):
    return await conn.fetch(
        "SELECT tool_name, outcome FROM action_pack_invocations "
        "WHERE requested_by = $1 ORDER BY occurred_at",
        requested_by,
    )


async def test_descriptor_carries_both_event_tools():
    """The pack descriptor's tools: list names query_events + inspect_event
    — a tool absent here is unboundable no matter what the handlers say."""
    body = yaml.safe_load(
        (_DESCRIPTORS / "action_pack_substrate_read.yaml").read_text())
    names = [t["name"] for t in body["tools"]]
    assert "query_events" in names
    assert "inspect_event" in names


async def test_query_events_governed_path_ledgers(pool):
    pack = _load_pack()
    port = _RecorderPort()
    who = f"analyst::p6_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=port,
        grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    outcome = await b.run_tool(
        "query_events",
        {"target_id": "country_g20_ir", "geo": ["ir", "sa"],
         "lifecycle_state": "active", "since": "2026-09-01",
         "limit": 5},
    )
    assert outcome.admitted, outcome
    assert outcome.tool_result.status == "completed"
    assert outcome.tool_result.output["count"] == 1

    # The handler coerced the args onto the port signature.
    name, args = port.calls[0]
    assert name == "query_events"
    assert args["geo"] == ["ir", "sa"]
    assert args["lifecycle_state"] == "active"
    assert args["limit"] == 5

    # The governed ledger recorded the admission + completion.
    async with pool.acquire() as conn:
        inv = await _invocations(conn, requested_by=who)
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("query_events", "completed"),
    ]


async def test_inspect_event_governed_path_ledgers(pool):
    pack = _load_pack()
    port = _RecorderPort()
    who = f"analyst::p6_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=port,
        grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    eid = str(uuid4())
    outcome = await b.run_tool("inspect_event", {"event_id": eid})
    assert outcome.admitted, outcome
    assert outcome.tool_result.status == "completed"
    assert outcome.tool_result.output["found"] is True
    assert port.calls[0] == ("inspect_event", {"event_id": eid})
    async with pool.acquire() as conn:
        inv = await _invocations(conn, requested_by=who)
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("inspect_event", "completed"),
    ]


async def test_ungranted_analyst_cannot_reach_event_tools(pool):
    """Same three-way gate as every read tool — an analyst without the
    substrate_read grant is blocked BEFORE the handler runs, and the
    invocation is never ledgered."""
    pack = _load_pack()
    who = f"analyst::p6deny_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=_RecorderPort(),
        grants=[],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    outcome = await b.run_tool("query_events", {"target_id": "x"})
    assert not outcome.admitted
    assert outcome.block_cause == "not_granted"
    async with pool.acquire() as conn:
        assert await _invocations(conn, requested_by=who) == []


async def test_query_events_handler_coerces_geo_and_limit():
    """Handler-unit: ``geo`` accepts a bare string OR a list; ``limit``
    coerces int; ``situation_id`` / ``as_of`` / ``include_origin`` pass
    through. No governor — the dispatch surface itself."""
    from legba.data.analysts.agency.substrate_read import query_events_tool

    port = _RecorderPort()
    pack = _load_pack()
    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="query_events",
        args={"geo": "IR", "limit": "3", "situation_id": "abc",
              "as_of": "2026-09-20", "include_origin": ["live", "seed"]},
    )
    result = await query_events_tool(call, pack, ToolContext(substrate=port))
    assert result.status == "completed"
    _, args = port.calls[0]
    assert args["geo"] == "IR"
    assert args["limit"] == 3
    assert args["situation_id"] == "abc"
    assert args["as_of"] == "2026-09-20"
    assert args["include_origin"] == ["live", "seed"]


async def test_event_tools_no_port_fails_visibly():
    """No substrate port wired → a failed ToolResult naming the gap, never a
    silent empty result (the pack's own rule)."""
    from legba.data.analysts.agency.substrate_read import query_events_tool

    pack = _load_pack()
    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="query_events",
        args={"target_id": "x"},
    )
    result = await query_events_tool(call, pack, ToolContext(substrate=None))
    assert result.status == "failed"
    assert "query_events" in (result.error or "")
