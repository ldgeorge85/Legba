# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pack-tool dispatch robustness — per-call timeout + orphan reconcile.

Two guarantees layered on the P-11 hard gate (real migrated Postgres, no mocks):

  1. ``run_pack_tool`` bounds the handler dispatch with a wall-clock timeout: a
     wedged handler is caught at the tool boundary, the invocation row is settled
     ``failed`` (not left stuck ``admitted``), and the actor is reclaimed instead
     of pinned for the full actor-invoke window.
  2. ``PackGovernorEnforcer.reconcile_stale_admitted`` heals the one path the
     per-call timeout cannot reach — a process that DIED mid-dispatch, leaving a
     perpetual ``admitted`` row — by settling rows older than a threshold to
     ``failed`` (leader-gated in the runtime). Ledger/observability hygiene; it
     does NOT reclaim caps (the windows count all outcomes).

Plus a pure-unit check of the env-overridable timing resolvers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.data.analysts.agency import (
    Agency,
    PackGovernorEnforcer,
    TargetScopeView,
    ToolCall,
    ToolContext,
    ToolRegistry,
    ToolResult,
)
from legba.data.analysts.agency.governor import (
    pack_tool_stale_reconcile_seconds,
    pack_tool_timeout_seconds,
)

pytestmark = [pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    async with p.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('action_pack_invocations')")
    yield p
    await p.close()


def _pack(pid, *, tools, tags=None) -> ActionPack:
    return ActionPack.model_validate(
        {
            "identity": {
                "id": pid, "name": pid, "schema_uri": "legba/action_pack/1.0.0",
                "version": "a" * 16, "state": "active", "owner": "p11_agency",
                "created": datetime.now(timezone.utc).isoformat(),
            },
            "tools": [{"name": t} for t in tools],
            "applies_to_tags": tags or [],
        },
        strict=False,
    )


def _ref(pid) -> ActionPackRef:
    return ActionPackRef(pack_id=pid)


# ---------------------------------------------------------------------------
# 1) A wedged handler is caught by the per-call timeout → settled failed.
# ---------------------------------------------------------------------------


async def test_wedged_handler_times_out_and_settles_failed(pool, monkeypatch):
    # Shrink the budget so the test doesn't actually wait — the handler hangs
    # well past it. Patched on the agency module (the name it calls).
    monkeypatch.setattr(
        "legba.data.analysts.agency.agency.pack_tool_timeout_seconds",
        lambda: 0.1,
    )

    started = asyncio.Event()

    async def _hang(call, pack, ctx) -> ToolResult:
        started.set()
        await asyncio.sleep(30)          # never returns within the budget
        return ToolResult(status="completed")  # pragma: no cover

    registry = ToolRegistry()
    registry.register("hang_tool", _hang)
    agency = Agency(tool_registry=registry)

    pack = _pack("media_processing", tools=["hang_tool"], tags=["media"])
    scope = TargetScopeView(target_id="t_hang", tags=["media"])
    account = f"acct-{uuid4().hex[:8]}"
    call = ToolCall(
        pack_id="media_processing", tool_name="hang_tool",
        budget_account=account, requested_by="analyst.wedge", args={},
    )

    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("media_processing")],
            target_allows=[_ref("media_processing")],
            scope=scope, ctx=ToolContext(),
        )

    assert started.is_set()                       # the handler DID start
    assert outcome.admitted is True               # it passed the gate (admitted)
    assert outcome.tool_result is not None
    assert outcome.tool_result.status == "failed"
    assert "timeout" in (outcome.tool_result.error or "").lower()

    # The ledger row is settled failed — NOT left stuck `admitted`.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT outcome FROM action_pack_invocations WHERE budget_account=$1",
            account,
        )
    assert row is not None
    assert row["outcome"] == "failed"


async def test_fast_handler_under_budget_completes(pool, monkeypatch):
    # Same generous default path: a handler that returns promptly is unaffected.
    monkeypatch.setattr(
        "legba.data.analysts.agency.agency.pack_tool_timeout_seconds",
        lambda: 5,
    )

    async def _quick(call, pack, ctx) -> ToolResult:
        return ToolResult(status="emitted", output={"ok": True})

    registry = ToolRegistry()
    registry.register("quick_tool", _quick)
    agency = Agency(tool_registry=registry)

    pack = _pack("incident_response", tools=["quick_tool"], tags=["incident"])
    scope = TargetScopeView(target_id="t_quick", tags=["incident"])
    account = f"acct-{uuid4().hex[:8]}"
    call = ToolCall(
        pack_id="incident_response", tool_name="quick_tool",
        budget_account=account, requested_by="analyst.q", args={},
    )

    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("incident_response")],
            target_allows=[_ref("incident_response")],
            scope=scope, ctx=ToolContext(),
        )
        row = await conn.fetchrow(
            "SELECT outcome FROM action_pack_invocations WHERE budget_account=$1",
            account,
        )
    assert outcome.admitted is True
    assert outcome.tool_result.status == "emitted"
    assert row["outcome"] == "completed"


# ---------------------------------------------------------------------------
# 2) reconcile_stale_admitted heals orphaned admitted rows (crash path).
# ---------------------------------------------------------------------------


async def test_reconcile_settles_only_stale_admitted_rows(pool):
    pid = f"recon-{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        # An OLD admitted row (orphaned — process died mid-dispatch).
        old_admitted = await conn.fetchval(
            """
            INSERT INTO action_pack_invocations
                (pack_id, pack_version, tool_name, budget_account, requested_by,
                 tenant_id, cost_usd, units, outcome, occurred_at)
            VALUES ($1,'v','t',$1,'sys','default',0,1,'admitted',
                    now() - interval '10 minutes')
            RETURNING id
            """,
            pid,
        )
        # A RECENT admitted row (a call legitimately in flight — must survive).
        recent_admitted = await conn.fetchval(
            """
            INSERT INTO action_pack_invocations
                (pack_id, pack_version, tool_name, budget_account, requested_by,
                 tenant_id, cost_usd, units, outcome, occurred_at)
            VALUES ($1,'v','t',$1,'sys','default',0,1,'admitted', now())
            RETURNING id
            """,
            pid,
        )
        # An OLD completed row (already settled — must not be touched).
        old_completed = await conn.fetchval(
            """
            INSERT INTO action_pack_invocations
                (pack_id, pack_version, tool_name, budget_account, requested_by,
                 tenant_id, cost_usd, units, outcome, occurred_at)
            VALUES ($1,'v','t',$1,'sys','default',0,1,'completed',
                    now() - interval '10 minutes')
            RETURNING id
            """,
            pid,
        )

        swept = await PackGovernorEnforcer.reconcile_stale_admitted(
            conn, older_than_seconds=300,
        )

        outcomes = {
            r["id"]: r["outcome"]
            for r in await conn.fetch(
                "SELECT id, outcome FROM action_pack_invocations WHERE pack_id=$1",
                pid,
            )
        }

    assert swept >= 1                               # at least our orphan
    assert outcomes[old_admitted] == "failed"       # orphan healed
    assert outcomes[recent_admitted] == "admitted"  # in-flight call untouched
    assert outcomes[old_completed] == "completed"   # settled row untouched


# ---------------------------------------------------------------------------
# 3) Env-overridable timing resolvers (pure unit).
# ---------------------------------------------------------------------------


async def test_timeout_resolvers_default_and_override(monkeypatch):
    monkeypatch.delenv("LEGBA_PACK_TOOL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LEGBA_PACK_TOOL_STALE_RECONCILE_SECONDS", raising=False)
    assert pack_tool_timeout_seconds() == 60
    assert pack_tool_stale_reconcile_seconds() == 300

    monkeypatch.setenv("LEGBA_PACK_TOOL_TIMEOUT_SECONDS", "15")
    assert pack_tool_timeout_seconds() == 15

    # Malformed / non-positive fall back to the default (never silently disable).
    monkeypatch.setenv("LEGBA_PACK_TOOL_TIMEOUT_SECONDS", "not-a-number")
    assert pack_tool_timeout_seconds() == 60
    monkeypatch.setenv("LEGBA_PACK_TOOL_TIMEOUT_SECONDS", "0")
    assert pack_tool_timeout_seconds() == 60
    monkeypatch.setenv("LEGBA_PACK_TOOL_TIMEOUT_SECONDS", "-5")
    assert pack_tool_timeout_seconds() == 60
