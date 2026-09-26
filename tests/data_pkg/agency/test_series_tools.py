# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""7g-2 — the COLLECTION series tools through the REAL governed path.

Every test here runs ``AgencyToolBinding.run_tool`` end to end — the
three-way gate (resolve ∩ allow ∩ applicability), the governor, the pack
handler — against a real migrated Postgres, and asserts the
``action_pack_invocations`` row the gate recorded. A green tool test with an
empty invocation ledger is precisely the shape that ships a dead capability,
so the ledger row IS the assertion.

The substrate port is the documented in-memory recorder (the pack tests' own
pattern): what is under test is the BINDING + the handler's arg coercion. The
SQL half runs against the real driver in
``tests/runtime/test_observations_read.py``.
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
from legba.data.analysts.agency.substrate_read import (
    SUBSTRATE_READ_PACK_ID,
    SUBSTRATE_READ_TOOLS,
)
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


def _load_pack() -> ActionPack:
    body = yaml.safe_load(
        (_DESCRIPTORS / "action_pack_substrate_read.yaml").read_text()
    )
    body["identity"]["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


class _RecorderPort:
    """In-memory SubstrateQueryPort recording the series-tool calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def series_history(self, **kwargs):
        self.calls.append(("series_history", dict(kwargs)))
        return {
            "rows": [{
                "observation_id": "11111111-1111-4111-8111-111111111111",
                "ref": "observation:11111111-1111-4111-8111-111111111111",
                "valid_from": "2016-01-01",
                "value_display": "2.8",
                "unit": "pct_per_year",
            }],
            "refs": ["11111111-1111-4111-8111-111111111111"],
            "count": 1,
        }

    async def series_compare(self, **kwargs):
        self.calls.append(("series_compare", dict(kwargs)))
        return {"rows": [], "refs": [], "count": 0}


def _binding(pool, pack, *, port=None, grants=None, allows=None, account=None):
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


# ---------------------------------------------------------------------------
# The four surfaces a tool has to exist on
# ---------------------------------------------------------------------------


async def test_the_pack_descriptor_names_both_series_tools():
    """A tool absent from the descriptor's ``tools:`` is unboundable no matter
    what the handlers say — the governed consult loop blocks it as
    unknown_tool."""
    body = yaml.safe_load(
        (_DESCRIPTORS / "action_pack_substrate_read.yaml").read_text()
    )
    names = [t["name"] for t in body["tools"]]
    assert "series_history" in names
    assert "series_compare" in names


async def test_the_tool_tuple_the_registry_and_the_descriptor_agree():
    from legba.data.analysts.agency.substrate_read import (
        register_substrate_read_tools,
    )
    from legba.data.analysts.agency.tools import ToolRegistry

    reg = ToolRegistry()
    register_substrate_read_tools(reg)
    assert {"series_history", "series_compare"} <= set(SUBSTRATE_READ_TOOLS)
    assert {"series_history", "series_compare"} <= set(reg.names)


async def test_consults_known_tool_set_covers_them():
    """Otherwise the governed consult path blocks them as unknown_tool — the
    exact way a shipped tool stays unreachable."""
    from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS

    assert {"series_history", "series_compare"} <= set(_KNOWN_TOOLS)


async def test_the_gather_loop_can_route_them_for_research():
    from legba.data.analysts.gather_surface import _GATHER_READ_TOOLS

    assert {"series_history", "series_compare"} <= set(_GATHER_READ_TOOLS)


async def test_both_carry_a_native_tool_spec_with_its_required_window():
    """``from``/``to`` are REQUIRED in the schema: a series read is always over
    a bounded valid-time window, and a planner that omits one must be told by
    the schema rather than by a refusal it has to spend a round on."""
    from legba.data.stack.llm.tool_rounds import TOOL_SCHEMAS, WRITE_TOOLS

    for name in ("series_history", "series_compare"):
        spec = TOOL_SCHEMAS[name]
        assert set(spec.json_schema["required"]) >= {"from", "to"}
        assert "as_of" in spec.json_schema["properties"]
        # READ tools — a name outside WRITE_TOOLS is what keeps them off the
        # serial dispatch path.
        assert name not in WRITE_TOOLS


# ---------------------------------------------------------------------------
# The governed path
# ---------------------------------------------------------------------------


async def test_series_history_governed_path_ledgers(pool):
    pack = _load_pack()
    port = _RecorderPort()
    who = f"analyst::7g2_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=port,
        grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    outcome = await b.run_tool(
        "series_history",
        {"series_id": "wb.gdp_growth_annual_pct", "subject": "US",
         "from": "2016", "to": "2025", "as_of": "2024-01-01", "limit": 50},
    )
    assert outcome.admitted, outcome
    assert outcome.tool_result.status == "completed"
    assert outcome.tool_result.output["count"] == 1

    name, args = port.calls[0]
    assert name == "series_history"
    assert args["series_id"] == "wb.gdp_growth_annual_pct"
    assert args["subject"] == "US"
    assert args["since"] == "2016"
    assert args["until"] == "2025"
    assert args["as_of"] == "2024-01-01"
    assert args["limit"] == 50

    # THE PROOF OF LIFE — the ledger row the gate records.
    async with pool.acquire() as conn:
        inv = await _invocations(conn, requested_by=who)
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("series_history", "completed"),
    ]


async def test_series_compare_governed_path_ledgers(pool):
    pack = _load_pack()
    port = _RecorderPort()
    who = f"analyst::7g2c_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=port,
        grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    outcome = await b.run_tool(
        "series_compare",
        {"series_id": "wb.gdp_growth_annual_pct",
         "subjects": ["US", "IL"], "from": "2016", "to": "2025"},
    )
    assert outcome.admitted, outcome
    assert outcome.tool_result.status == "completed"
    _, args = port.calls[0]
    assert args["subjects"] == ["US", "IL"]

    async with pool.acquire() as conn:
        inv = await _invocations(conn, requested_by=who)
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("series_compare", "completed"),
    ]


async def test_an_ungranted_analyst_cannot_reach_the_series_tools(pool):
    """Same three-way gate as every read tool — history is not a side door."""
    pack = _load_pack()
    who = f"analyst::7g2deny_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=_RecorderPort(),
        grants=[],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    outcome = await b.run_tool(
        "series_history",
        {"series_id": "x", "subject": "US", "from": "2016", "to": "2025"},
    )
    assert not outcome.admitted
    assert outcome.block_cause == "not_granted"
    async with pool.acquire() as conn:
        assert await _invocations(conn, requested_by=who) == []


# ---------------------------------------------------------------------------
# Handler-unit: the arg coercion
# ---------------------------------------------------------------------------


async def test_the_handler_accepts_since_until_as_well_as_from_to():
    """``from``/``to`` is the schema's spelling and ``since``/``until`` is what
    every other temporal tool in this pack calls the same thing. A planner that
    reaches for the familiar one should not lose a round to it."""
    from legba.data.analysts.agency.substrate_read import series_history_tool

    port = _RecorderPort()
    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="series_history",
        args={"series_id": "s", "subject": "US",
              "since": "2016", "until": "2025"},
    )
    result = await series_history_tool(call, _load_pack(), ToolContext(substrate=port))
    assert result.status == "completed"
    _, args = port.calls[0]
    assert args["since"] == "2016"
    assert args["until"] == "2025"


async def test_a_single_subject_string_is_read_as_one_subject():
    """A planner that wrote ``subjects: "US"`` meant one subject; refusing it
    would spend a round teaching it JSON."""
    from legba.data.analysts.agency.substrate_read import series_compare_tool

    port = _RecorderPort()
    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="series_compare",
        args={"series_id": "s", "subjects": "US, IL", "from": "2016", "to": "2025"},
    )
    await series_compare_tool(call, _load_pack(), ToolContext(substrate=port))
    _, args = port.calls[0]
    assert args["subjects"] == ["US", "IL"]


async def test_a_malformed_subjects_value_yields_an_empty_list_not_everything():
    from legba.data.analysts.agency.substrate_read import series_compare_tool

    port = _RecorderPort()
    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="series_compare",
        args={"series_id": "s", "subjects": {"nope": 1}, "from": "2016", "to": "2025"},
    )
    await series_compare_tool(call, _load_pack(), ToolContext(substrate=port))
    _, args = port.calls[0]
    # The port turns [] into a NAMED refusal; what matters here is that the
    # handler never widens a malformed filter into "all subjects".
    assert args["subjects"] == []


async def test_no_port_fails_visibly():
    from legba.data.analysts.agency.substrate_read import series_history_tool

    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="series_history",
        args={"series_id": "s", "subject": "US", "from": "2016", "to": "2025"},
    )
    result = await series_history_tool(call, _load_pack(), ToolContext(substrate=None))
    assert result.status == "failed"
    assert "series_history" in (result.error or "")
