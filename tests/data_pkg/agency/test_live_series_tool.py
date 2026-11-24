# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2 — ``live_series`` through the REAL governed path, on every surface.

``AgencyToolBinding.run_tool`` end to end — resolve ∩ allow ∩ applicability,
the governor, the pack handler — over the PRODUCTION port on a real migrated
Postgres, with one live ``observations`` row written the way the
ObservationSink writes it. The ``action_pack_invocations`` row the gate
records is the assertion (a green tool test with an empty ledger is the shape
that ships a dead capability). The surfaces a tool must be on — the pack
descriptor, the handler registry, the consult palette, the native schema — are
pinned here too.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.agency import Agency, AgencyToolBinding, GLOBAL_SCOPE, ToolContext
from legba.data.analysts.agency.substrate_read import SUBSTRATE_READ_PACK_ID, SUBSTRATE_READ_TOOLS
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

pytestmark = [pytest.mark.asyncio]

_DESCRIPTORS = Path(__file__).resolve().parents[3] / "descriptors"


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database, min_size=1, max_size=4,
    )
    yield p
    await p.close()


def _load_pack() -> ActionPack:
    body = yaml.safe_load((_DESCRIPTORS / "action_pack_substrate_read.yaml").read_text())
    body["identity"]["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


def _binding(pool, pack, port, *, who, grants):
    return AgencyToolBinding(
        agency=Agency(), pack=pack, pg_pool=pool,
        tool_context=ToolContext(queue=None, emit=None, substrate=port),
        analyst_grants=grants, target_allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        scope=GLOBAL_SCOPE, requested_by=who, budget_account=who,
    )


# ---------------------------------------------------------------------------
# The surfaces
# ---------------------------------------------------------------------------


async def test_live_series_is_on_every_surface():
    from legba.data.analysts.agency.substrate_read import register_substrate_read_tools
    from legba.data.analysts.agency.tools import ToolRegistry
    from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS
    from legba.data.analysts.consult_system_prompt import _SYSTEM_PROMPT
    from legba.data.stack.llm.tool_rounds import TOOL_SCHEMAS

    body = yaml.safe_load((_DESCRIPTORS / "action_pack_substrate_read.yaml").read_text())
    assert "live_series" in [t["name"] for t in body["tools"]]
    reg = ToolRegistry()
    register_substrate_read_tools(reg)
    assert "live_series" in set(reg.names) and "live_series" in SUBSTRATE_READ_TOOLS
    assert "live_series" in _KNOWN_TOOLS
    assert "  - live_series(" in _SYSTEM_PROMPT
    assert "live_series" in TOOL_SCHEMAS


async def test_no_frozen_bakeoff_arm_loads_it():
    """The frozen arms read their own packs/tool lists; none carries the tool,
    so their pinned prompts cannot change with it."""
    import sys

    from legba.data.analysts.consult_bakeoff import BAKEOFF_READ_PACKS

    for variant in BAKEOFF_READ_PACKS.values():
        assert "live_series" not in variant.tools
        assert "live_series(" not in variant.system_prompt
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "bakeoff"))
    import arm_open

    assert "live_series" not in arm_open.RECORD_TOOLS
    assert "live_series(" not in arm_open.record_system_prompt()


# ---------------------------------------------------------------------------
# The governed path, the real port, the ledger
# ---------------------------------------------------------------------------


async def test_live_series_governed_path_reads_live_rows_and_ledgers(pool):
    source = f"source.e2live.{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        obs = await conn.fetchval(
            "INSERT INTO observations (collection_id, series_id, subject_kind, subject, valid_from, "
            "valid_to, record_time, value, unit, source_url, sha256, origin_class, source_id, "
            "methodology_version, fetch_sha256, period, record_time_basis) "
            "VALUES ($1, 'e2.transits', 'global', 'hormuz', $2, $2, $3, $4, 'vessels', "
            "'https://x.invalid', $5, 'live', $6, 'm1', $5, 'day', 'first_fetch') RETURNING id",
            f"live:{source}", (now - timedelta(days=3)).date(), now - timedelta(days=2),
            Decimal("41"), "c" * 64, source)
    pack = _load_pack()
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    who = f"analyst::e2live_{uuid4().hex[:8]}"
    b = _binding(pool, pack, port, who=who, grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)])

    outcome = await b.run_tool("live_series", {"source_id": source})
    assert outcome.admitted, outcome
    cat = outcome.tool_result.output
    assert cat["mode"] == "catalogue" and cat["origin_class"] == "live"
    assert [(s["series_id"], s["subject"], s["unit"], s["period"]) for s in cat["series"]] == [
        ("e2.transits", "hormuz", "vessels", "day")]

    outcome = await b.run_tool("live_series", {"series_id": "e2.transits", "subject": "hormuz",
                                               "source_id": source, "limit": 50})
    assert outcome.admitted and outcome.tool_result.status == "completed"
    out = outcome.tool_result.output
    assert out["refs"] == [str(obs)]
    row = out["rows"][0]
    assert row["label"] == "LIVE" and row["source_id"] == source and row["record_time_basis"] == "first_fetch"
    assert row["record_time"] and row["loaded_at"] and row["value_display"] == "41"

    async with pool.acquire() as conn:
        inv = await conn.fetch(
            "SELECT tool_name, outcome FROM action_pack_invocations WHERE requested_by = $1 "
            "ORDER BY occurred_at", who)
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("live_series", "completed"), ("live_series", "completed")]


async def test_a_window_past_the_cap_is_refused_by_name_through_the_gate(pool):
    """The schema declares the bounds; the reader enforces them (the arg gate
    checks names and types, not maxima): a 2-year window comes back as a named
    refusal on a completed call, never as an unbounded read."""
    pack = _load_pack()
    who = f"analyst::e2livecap_{uuid4().hex[:8]}"
    b = _binding(pool, pack, PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None),
                 who=who, grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)])
    outcome = await b.run_tool("live_series", {"series_id": "s", "subject": "US", "from": "2024-01-01",
                                               "to": "2026-01-01", "limit": 100000})
    assert outcome.admitted and outcome.tool_result.status == "completed"
    assert "at most 366" in outcome.tool_result.output["error"]


async def test_an_ungranted_analyst_cannot_reach_it(pool):
    pack = _load_pack()
    who = f"analyst::e2livedeny_{uuid4().hex[:8]}"
    b = _binding(pool, pack, PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None),
                 who=who, grants=[])
    outcome = await b.run_tool("live_series", {})
    assert not outcome.admitted and outcome.block_cause == "not_granted"
