# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S6 end-to-end — external web tools + operator-gated write tools.

Drives the S6 tool surface through the REAL three-way agency gate
(``Agency.run_pack_tool``) against a fresh migrated Postgres — no mocks of the
gate, the egress guard, or the write path. The flow mirrors the GATHER loop's
``binding.run_tool`` call shape: an analyst grant + a target allow → an
EFFECTIVE pack → dispatch → settle.

What it proves:

  1. ``web_fetch`` egresses through ``SsrfGuardedTransport`` — a URL resolving
     to a PRIVATE / loopback / metadata address is REFUSED (clean ``failed``
     ToolResult, ``egress_blocked``), never a crash. (The security-critical
     property; a successful public fetch needs network the gate doesn't have.)
  2. ``web_search`` with no operator-pinned endpoint returns a clean failure
     naming the gap (no silent empty result), and rejects a non-http endpoint.
  3. ``propose_fact`` flows through the THREE-WAY gate → ``write_fact`` →
     a real ``facts`` row with ``source_type='proposed'`` and the run's
     provenance; an ALLOW governor event + a settled invocation ledger row land.
  4. A ``propose_fact`` with NO ``derived_from`` lineage is REFUSED at the
     handler (review S-1 — an uncited write cannot land).
  5. A junk triple is rejected (the ingest ``_is_junk_triple`` gate).
  6. A target that does NOT allow the write pack BLOCKS at resolution
     (``not_allowed``) — the operator's allow leg is the off switch.
  7. A bad (schema-invalid) payload routes to ``output_dead_letter`` and the
     tool reports a clean failure, rather than crashing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.agency import (
    Agency,
    TargetScopeView,
    ToolCall,
    ToolContext,
    WritebackContext,
    recent_events,
)
from legba.data.provenance import AnalystContext
from legba.data.schemas.action_pack import ActionPack, ActionPackRef

pytestmark = [pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    async with p.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('action_pack_invocations')")
        assert await conn.fetchval("SELECT to_regclass('facts')")
    yield p
    await p.close()


def _pack(pid, *, tools, governor=None) -> ActionPack:
    body = {
        "identity": {
            "id": pid, "name": pid, "schema_uri": "legba/action_pack/1.0.0",
            "version": "a" * 16, "state": "active", "owner": "s6_agency",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        # The tool config is permissive (extra='allow'); pass through any keys.
        "tools": [{"name": t} for t in tools],
        # Universal applicability (no tags / predicate) — mirrors substrate_read.
    }
    if governor:
        body["governor"] = governor
    return ActionPack.model_validate(body, strict=False)


def _ref(pid, **override) -> ActionPackRef:
    return ActionPackRef(pack_id=pid, **override)


def _analyst_ctx() -> AnalystContext:
    return AnalystContext(
        analyst_id="analyst_s6_test",
        analyst_version="b" * 16,
        run_id=uuid4(),
        target_id="t_s6",
        target_version="c" * 16,
    )


# ---------------------------------------------------------------------------
# 1) web_fetch — the SSRF egress guard refuses a private target.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",        # loopback
        "http://169.254.169.254/latest/meta",  # cloud metadata
        "http://10.0.0.5/internal",            # RFC-1918
    ],
)
async def test_web_fetch_egress_guard_blocks_private(pool, url):
    pack = _pack("web_access", tools=["web_fetch", "web_search"])
    scope = TargetScopeView(target_id="t_web")
    agency = Agency()
    call = ToolCall(
        pack_id="web_access", tool_name="web_fetch",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.web",
        args={"url": url},
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("web_access")],
            target_allows=[_ref("web_access")],
            scope=scope, ctx=ToolContext(),
        )
    # The gate ADMITS (the pack is effective) — the egress guard is what refuses,
    # and it does so as a CLEAN tool failure, not a crash.
    assert outcome.admitted is True
    assert outcome.tool_result is not None
    assert outcome.tool_result.status == "failed"
    assert "egress_blocked" in (outcome.tool_result.error or "")


async def test_web_fetch_rejects_non_http_scheme(pool):
    pack = _pack("web_access", tools=["web_fetch"])
    scope = TargetScopeView(target_id="t_web")
    agency = Agency()
    call = ToolCall(
        pack_id="web_access", tool_name="web_fetch",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.web",
        args={"url": "file:///etc/passwd"},
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("web_access")], target_allows=[_ref("web_access")],
            scope=scope, ctx=ToolContext(),
        )
    assert outcome.tool_result.status == "failed"
    assert "non-http" in (outcome.tool_result.error or "")


# ---------------------------------------------------------------------------
# 2) web_search — no operator endpoint → clean failure; bad endpoint rejected.
# ---------------------------------------------------------------------------


async def test_web_search_without_endpoint_fails_clean(pool, monkeypatch):
    monkeypatch.delenv("LEGBA_WEB_SEARCH_ENDPOINT", raising=False)
    pack = _pack("web_access", tools=["web_fetch", "web_search"])
    scope = TargetScopeView(target_id="t_web")
    agency = Agency()
    call = ToolCall(
        pack_id="web_access", tool_name="web_search",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.web",
        args={"query": "sanctions regime change"},
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("web_access")], target_allows=[_ref("web_access")],
            scope=scope, ctx=ToolContext(),
        )
    assert outcome.admitted is True
    assert outcome.tool_result.status == "failed"
    assert "no endpoint configured" in (outcome.tool_result.error or "")


async def test_web_search_rejects_private_endpoint_via_guard(pool, monkeypatch):
    # An operator-pinned endpoint that resolves private is still refused by the
    # egress guard (defense in depth — the endpoint is operator-authored, but a
    # mistaken internal address never reaches an internal service).
    monkeypatch.setenv("LEGBA_WEB_SEARCH_ENDPOINT", "http://127.0.0.1:8888/search")
    pack = _pack("web_access", tools=["web_search"])
    scope = TargetScopeView(target_id="t_web")
    agency = Agency()
    call = ToolCall(
        pack_id="web_access", tool_name="web_search",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.web",
        args={"query": "x"},
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("web_access")], target_allows=[_ref("web_access")],
            scope=scope, ctx=ToolContext(),
        )
    assert outcome.tool_result.status == "failed"
    assert "egress_blocked" in (outcome.tool_result.error or "")


# ---------------------------------------------------------------------------
# 3) propose_fact — full gate → write_fact → a real facts row.
# ---------------------------------------------------------------------------


async def test_propose_fact_writes_proposed_row_through_gate(pool):
    pack = _pack("propose_facts", tools=["propose_fact"],
                 governor={"max_invocations_per_hour": 100})
    scope = TargetScopeView(target_id="t_s6")
    account = f"acct-{uuid4().hex[:8]}"
    agency = Agency()
    actx = _analyst_ctx()
    ref = uuid4()  # the cited substrate ref (lineage)

    ctx = ToolContext(writeback=WritebackContext(pg_pool=pool, analyst_ctx=actx))
    call = ToolCall(
        pack_id="propose_facts", tool_name="propose_fact",
        budget_account=account, requested_by=f"analyst::{actx.analyst_id}",
        args={
            "subject": "Country Zeta", "predicate": "controls",
            "value": "Port Theta", "confidence": 0.9,  # clamped to ceiling
            "derived_from": [str(ref)],
        },
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("propose_facts")],
            target_allows=[_ref("propose_facts")],
            scope=scope, ctx=ctx, estimated_cost_usd=0.0,
        )
    assert outcome.admitted is True
    assert outcome.tool_result.status == "completed"
    fact_id = outcome.tool_result.output["fact_id"]
    # Confidence was clamped below the requested 0.9.
    assert outcome.tool_result.output["confidence"] <= 0.6

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT subject, value, source_type, confidence, analyst_id, "
            "       derived_from, run_id FROM facts WHERE id = $1",
            fact_id,
        )
        allows = await recent_events(conn, pack_id="propose_facts", decision="allow")
        inv = await conn.fetchrow(
            "SELECT tool_name, outcome FROM action_pack_invocations "
            "WHERE budget_account = $1", account,
        )
    assert row is not None
    assert row["source_type"] == "proposed"
    assert row["confidence"] <= 0.6 + 1e-6   # ceiling 0.6; facts.confidence is real/float32
    assert row["analyst_id"] == actx.analyst_id
    assert ref in row["derived_from"]          # lineage stamped
    assert row["run_id"] == actx.run_id        # provenance stamped to the run
    assert any(e["cause"] == "ok" for e in allows)
    assert inv["outcome"] == "completed"


# ---------------------------------------------------------------------------
# 4) propose_fact with no lineage is REFUSED (review S-1).
# ---------------------------------------------------------------------------


async def test_propose_fact_without_derived_from_refused(pool):
    pack = _pack("propose_facts", tools=["propose_fact"])
    scope = TargetScopeView(target_id="t_s6")
    agency = Agency()
    actx = _analyst_ctx()
    ctx = ToolContext(writeback=WritebackContext(pg_pool=pool, analyst_ctx=actx))
    call = ToolCall(
        pack_id="propose_facts", tool_name="propose_fact",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.x",
        args={"subject": "A", "predicate": "controls", "value": "B"},  # no derived_from
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("propose_facts")], target_allows=[_ref("propose_facts")],
            scope=scope, ctx=ctx,
        )
    assert outcome.admitted is True
    assert outcome.tool_result.status == "failed"
    assert "derived_from" in (outcome.tool_result.error or "")
    # Nothing was written.
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM facts WHERE subject = 'A' AND value = 'B'")
    assert n == 0


# ---------------------------------------------------------------------------
# 5) Junk triple rejected (the ingest _is_junk_triple gate).
# ---------------------------------------------------------------------------


async def test_propose_fact_junk_triple_rejected(pool):
    pack = _pack("propose_facts", tools=["propose_fact"])
    scope = TargetScopeView(target_id="t_s6")
    agency = Agency()
    actx = _analyst_ctx()
    ctx = ToolContext(writeback=WritebackContext(pg_pool=pool, analyst_ctx=actx))
    # subject == value (self-referential) → junk.
    call = ToolCall(
        pack_id="propose_facts", tool_name="propose_fact",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.x",
        args={"subject": "Putin", "predicate": "is", "value": "Putin",
              "derived_from": [str(uuid4())]},
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("propose_facts")], target_allows=[_ref("propose_facts")],
            scope=scope, ctx=ctx,
        )
    assert outcome.tool_result.status == "failed"
    assert "junk" in (outcome.tool_result.error or "")


# ---------------------------------------------------------------------------
# 6) A target that does NOT allow the write pack BLOCKS at resolution.
# ---------------------------------------------------------------------------


async def test_propose_fact_not_allowed_blocks(pool):
    pack = _pack("propose_facts", tools=["propose_fact"])
    scope = TargetScopeView(target_id="t_no_write")
    account = f"acct-{uuid4().hex[:8]}"
    agency = Agency()
    actx = _analyst_ctx()
    ctx = ToolContext(writeback=WritebackContext(pg_pool=pool, analyst_ctx=actx))
    call = ToolCall(
        pack_id="propose_facts", tool_name="propose_fact",
        budget_account=account, requested_by="analyst.x",
        args={"subject": "A", "predicate": "controls", "value": "B",
              "derived_from": [str(uuid4())]},
    )
    async with pool.acquire() as conn:
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("propose_facts")],
            target_allows=[],                       # operator withholds the allow leg
            scope=scope, ctx=ctx,
        )
    assert outcome.admitted is False
    assert outcome.block_cause == "not_allowed"
    assert outcome.tool_result is None
    # No invocation ledger row — the tool never ran.
    async with pool.acquire() as conn:
        invs = await conn.fetchval(
            "SELECT COUNT(*) FROM action_pack_invocations WHERE budget_account = $1",
            account,
        )
    assert invs == 0


# ---------------------------------------------------------------------------
# 7) request_source + open_question land real hypotheses rows.
# ---------------------------------------------------------------------------


async def test_request_source_and_open_question_land_rows(pool):
    pack = _pack("propose_facts",
                 tools=["request_source", "open_question"])
    scope = TargetScopeView(target_id="t_s6")
    agency = Agency()
    actx = _analyst_ctx()
    ref = uuid4()
    ctx = ToolContext(writeback=WritebackContext(pg_pool=pool, analyst_ctx=actx))

    rs_call = ToolCall(
        pack_id="propose_facts", tool_name="request_source",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.x",
        args={"need": "no source covers Zeta energy policy",
              "rationale": "gap blocked the assessment", "derived_from": [str(ref)]},
    )
    oq_call = ToolCall(
        pack_id="propose_facts", tool_name="open_question",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.x",
        args={"question": "Will Zeta nationalize Port Theta?",
              "derived_from": [str(ref)]},
    )
    async with pool.acquire() as conn:
        rs = await agency.run_pack_tool(
            conn, pack=pack, call=rs_call,
            analyst_grants=[_ref("propose_facts")], target_allows=[_ref("propose_facts")],
            scope=scope, ctx=ctx)
        oq = await agency.run_pack_tool(
            conn, pack=pack, call=oq_call,
            analyst_grants=[_ref("propose_facts")], target_allows=[_ref("propose_facts")],
            scope=scope, ctx=ctx)
    assert rs.tool_result.status == "completed"
    assert oq.tool_result.status == "completed"

    async with pool.acquire() as conn:
        rs_row = await conn.fetchrow(
            "SELECT status, analyst_id FROM hypotheses WHERE id = $1",
            rs.tool_result.output["hypothesis_id"])
        oq_row = await conn.fetchrow(
            "SELECT status, thesis FROM hypotheses WHERE id = $1",
            oq.tool_result.output["hypothesis_id"])
    assert rs_row["status"] == "source_request"
    assert rs_row["analyst_id"] == actx.analyst_id
    assert oq_row["status"] == "open_question"
    assert "Zeta" in oq_row["thesis"]


# ---------------------------------------------------------------------------
# 7a) CW-3 — a deictic question is made SELF-CONTAINED before it is stored.
#
# The analyst writing this call is looking at a finding, so it writes "the
# incident" and the answer to "which one" stays in derived_from. K-4 R3
# measured that class at 0.133 across the open-question set.
# ---------------------------------------------------------------------------


async def _open_question(pool, *, question, derived_from):
    pack = _pack("propose_facts", tools=["open_question"])
    agency = Agency()
    ctx = ToolContext(
        writeback=WritebackContext(pg_pool=pool, analyst_ctx=_analyst_ctx())
    )
    call = ToolCall(
        pack_id="propose_facts", tool_name="open_question",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.x",
        args={"question": question,
              "derived_from": [str(r) for r in derived_from]},
    )
    async with pool.acquire() as conn:
        res = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("propose_facts")],
            target_allows=[_ref("propose_facts")],
            scope=TargetScopeView(target_id="t_s6"), ctx=ctx)
    assert res.tool_result.status == "completed"
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT thesis FROM hypotheses WHERE id = $1",
            res.tool_result.output["hypothesis_id"])


async def _origin_finding(pool, title: str):
    fid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, "
            "  data, analyst_id, schema_uri) "
            "VALUES ($1, 'finding', $2, '', 0.8, '{}'::jsonb, 'unit.x', "
            "        'iglu:legba/finding/jsonschema/1-0-0')",
            fid, title,
        )
    return fid


async def test_a_deictic_question_gets_its_referent_inlined_at_write(pool):
    origin = await _origin_finding(
        pool, "Narrative of Ukrainian attack on Iranian vessel, Caspian Sea"
    )
    thesis = await _open_question(
        pool,
        question="Is the framing of the incident state-orchestrated?",
        derived_from=[origin],
    )
    # The analyst's question is intact; the referent is APPENDED to it, so a
    # human reading the row sees both what was asked and what it was about.
    assert thesis.startswith("Is the framing of the incident state-orchestrated?")
    assert "Caspian Sea" in thesis


async def test_a_self_contained_question_is_stored_verbatim(pool):
    origin = await _origin_finding(pool, "Some unrelated finding title")
    thesis = await _open_question(
        pool,
        question="Will Zeta nationalize Port Theta?",
        derived_from=[origin],
    )
    assert thesis == "Will Zeta nationalize Port Theta?"


async def test_an_office_with_no_referent_is_refused_not_recorded(pool):
    """CW-8. Iran abolished the premiership in 1989 and R3 carried two rows
    asking about it. The thesis named two offices and no country — so nothing
    could contradict it, which is both what made it wrong and what made it
    uncheckable. The tool refuses and says what is missing, the way it already
    refuses an empty question."""
    pack = _pack("propose_facts", tools=["open_question"])
    agency = Agency()
    ctx = ToolContext(
        writeback=WritebackContext(pg_pool=pool, analyst_ctx=_analyst_ctx())
    )
    call = ToolCall(
        pack_id="propose_facts", tool_name="open_question",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.x",
        args={
            "question": "How entrenched is the military's loyalty to the "
                        "Supreme Leader versus the Prime Minister?",
            "derived_from": [str(uuid4())],
        },
    )
    async with pool.acquire() as conn:
        res = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("propose_facts")],
            target_allows=[_ref("propose_facts")],
            scope=TargetScopeView(target_id="t_s6"), ctx=ctx)
    assert res.tool_result.status == "failed"
    assert "prime minister" in res.tool_result.error
    assert "WHOSE office" in res.tool_result.error


async def test_the_origin_title_can_ground_an_office_question(pool):
    """The office check runs AFTER the CW-3 inline, deliberately: the origin
    finding routinely supplies the country an office question is missing, and
    refusing one we were a lookup away from grounding would be the guard
    working against the fix."""
    origin = await _origin_finding(pool, "Iran: IRGC command reshuffle")
    thesis = await _open_question(
        pool,
        question="Is the military's loyalty to the Supreme Leader shifting "
                 "after the incident?",
        derived_from=[origin],
    )
    assert "Iran" in thesis


async def test_an_unresolvable_referent_still_records_the_question(pool):
    """A referent that cannot be resolved must leave the thesis exactly as
    written — visible to claim_watch's deictic guard — never fail the write.
    A question recorded deictic is recoverable; one not recorded is not."""
    thesis = await _open_question(
        pool,
        question="Is the framing of the incident state-orchestrated?",
        derived_from=[uuid4()],          # points at nothing
    )
    assert thesis == "Is the framing of the incident state-orchestrated?"


# ---------------------------------------------------------------------------
# 8) A schema-invalid propose payload routes to the dead-letter, not a crash.
# ---------------------------------------------------------------------------


async def test_propose_fact_oversize_value_routes_to_dead_letter(pool):
    pack = _pack("propose_facts", tools=["propose_fact"])
    scope = TargetScopeView(target_id="t_s6")
    agency = Agency()
    actx = _analyst_ctx()
    ctx = ToolContext(writeback=WritebackContext(pg_pool=pool, analyst_ctx=actx))
    # value exceeds FactPayload's max_length=4096 → ValidationError → DLQ.
    call = ToolCall(
        pack_id="propose_facts", tool_name="propose_fact",
        budget_account=f"acct-{uuid4().hex[:8]}", requested_by="analyst.x",
        args={"subject": "Zeta", "predicate": "controls",
              "value": "Z" * 5000, "derived_from": [str(uuid4())]},
    )
    async with pool.acquire() as conn:
        before = await conn.fetchval("SELECT COUNT(*) FROM output_dead_letter")
        outcome = await agency.run_pack_tool(
            conn, pack=pack, call=call,
            analyst_grants=[_ref("propose_facts")], target_allows=[_ref("propose_facts")],
            scope=scope, ctx=ctx)
        after = await conn.fetchval("SELECT COUNT(*) FROM output_dead_letter")
    # The handler reports a clean failure — no crash, no fact row.
    assert outcome.admitted is True
    assert outcome.tool_result.status == "failed"
    assert "dead-letter" in (outcome.tool_result.error or "")
    assert after == before + 1
