# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-3 (review G2) — agency wired into real reasoning loops, end-to-end.

The review's G2: the resolve ∩ allow ∩ applicability + governor + ledger
pipeline was mechanism-complete with ZERO production callers. A-3 wires two:

  * the consult kind routes every ReAct tool call through a
    ``substrate_read`` :class:`AgencyToolBinding` (A-3a);
  * the actor run path fires the ``escalate_finding`` pack when a landed
    finding crosses the pack's gate (A-3c) — the D1 example pack, no stub
    anywhere in its path.

Real migrated Postgres (0025 governor tables); the LLM boundary is a
scripted test double (allowed — LLM is the one permitted boundary double);
the substrate port is an in-memory recorder (same pattern the consult kind
documents for tests). The channel emit is the REAL ChannelEmitter over a
recording publish callable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.agency import (
    Agency,
    AgencyToolBinding,
    ChannelEmitter,
    GLOBAL_SCOPE,
    TargetScopeView,
    ToolContext,
    escalation_gate_decision,
)
from legba.data.analysts.agency.binding import EscalationBinding
from legba.data.analysts.agency.substrate_read import SUBSTRATE_READ_PACK_ID
from legba.data.analysts.consult_on_demand import (
    ConsultOnDemandDeps,
    run_method as consult_run_method,
)
from legba.data.provenance.models import FindingPayload
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.runtime.dapr_actors import _maybe_escalate_finding

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


def _load_pack(fname: str) -> ActionPack:
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"]["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


class _RecorderPort:
    """In-memory SubstrateQueryPort recording calls (the documented test
    double for the consult kind — the substrate boundary stays shaped)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def search_signals(self, *, query, limit=20,
                             scope_predicate=None):
        self.calls.append(("search_signals", {
            "query": query, "scope_predicate": scope_predicate,
        }))
        return {"rows": [{"title": "hit one"}],
                "refs": ["6a3b1a82-0000-4000-8000-000000000001"]}

    async def query_facts(self, *, subject=None, predicate=None, value=None,
                          limit=30):
        self.calls.append(("query_facts", {"subject": subject}))
        return {"rows": [], "refs": []}

    async def inspect_entity(self, *, name):
        self.calls.append(("inspect_entity", {"name": name}))
        return {"entity": None, "refs": []}

    async def vector_search(self, *, query, limit=10):
        self.calls.append(("vector_search", {"query": query}))
        return {"rows": [], "refs": []}

    async def search_context(self, *, query, corpus=None, country=None, k=6):
        self.calls.append(("search_context", {
            "query": query, "corpus": corpus, "country": country, "k": k,
        }))
        # W2-T4 ref honesty: the real port returns ctx:-prefixed chunk ids
        # (non-substrate, excluded from lineage) + the parallel context_refs.
        return {
            "rows": [{
                "chunk_id": "ctx:3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                "corpus": corpus or "world_context",
                "doc_id": "brief-1", "title": "Prior", "section": "S1",
                "countries": [country] if country else [],
                "source_url": "https://example.invalid/brief",
                "effective_date": "2026-01-01", "text": "a curated prior",
                "score": 0.88,
            }],
            "refs": ["ctx:3f2504e0-4f89-41d3-9a0c-0305e82c3301"],
            "context_refs": ["ctx:3f2504e0-4f89-41d3-9a0c-0305e82c3301"],
            "count": 1,
        }


def _binding(
    pool, pack: ActionPack, *, port=None, emit=None,
    grants=None, allows=None, scope=None, account=None,
) -> AgencyToolBinding:
    return AgencyToolBinding(
        agency=Agency(),
        pack=pack,
        pg_pool=pool,
        tool_context=ToolContext(queue=None, emit=emit, substrate=port),
        analyst_grants=grants,
        target_allows=allows,
        scope=scope or GLOBAL_SCOPE,
        requested_by=account or f"analyst::test_{uuid4().hex[:8]}",
        budget_account=account or f"acct_{uuid4().hex[:8]}",
    )


async def _invocations(conn, *, requested_by: str):
    return await conn.fetch(
        "SELECT tool_name, outcome FROM action_pack_invocations "
        "WHERE requested_by = $1 ORDER BY occurred_at",
        requested_by,
    )


async def _events(conn, *, requested_by: str):
    return await conn.fetch(
        "SELECT decision, cause, tool_name FROM governor_events "
        "WHERE requested_by = $1 ORDER BY occurred_at",
        requested_by,
    )


# ---------------------------------------------------------------------------
# A-3a — the substrate_read binding
# ---------------------------------------------------------------------------


async def test_search_context_handler_returns_chunks_no_db():
    """S5-T4 handler surface: ``search_context_tool`` reads the port from the
    ToolContext, forwards corpus/country/k, and returns the chunk envelope as a
    completed ToolResult. Pure-unit (no governor / DB) — the handler dispatch,
    not the gate."""
    from legba.data.analysts.agency.substrate_read import search_context_tool
    from legba.data.analysts.agency.tools import ToolCall

    port = _RecorderPort()
    pack = _load_pack("action_pack_substrate_read.yaml")
    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="search_context",
        args={"query": "iran succession", "corpus": "world_context",
              "country": "ir", "k": 4},
    )
    result = await search_context_tool(
        call, pack, ToolContext(substrate=port),
    )
    assert result.status == "completed"
    assert result.output["count"] == 1
    assert result.output["rows"][0]["corpus"] == "world_context"
    assert result.output["refs"] == ["ctx:3f2504e0-4f89-41d3-9a0c-0305e82c3301"]
    assert result.output["context_refs"] == [
        "ctx:3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    ]
    # The port received the coerced args.
    assert port.calls[-1] == (
        "search_context",
        {"query": "iran succession", "corpus": "world_context",
         "country": "ir", "k": 4},
    )


async def test_search_context_handler_no_port_fails_visibly():
    """No SubstrateQueryPort wired → a failed ToolResult naming the gap, never a
    silent empty result."""
    from legba.data.analysts.agency.substrate_read import search_context_tool
    from legba.data.analysts.agency.tools import ToolCall

    pack = _load_pack("action_pack_substrate_read.yaml")
    call = ToolCall(
        pack_id=SUBSTRATE_READ_PACK_ID, tool_name="search_context",
        args={"query": "q"},
    )
    result = await search_context_tool(call, pack, ToolContext(substrate=None))
    assert result.status == "failed"
    assert "search_context" in (result.error or "")


async def test_binding_governed_read_tool_ledgers(pool):
    pack = _load_pack("action_pack_substrate_read.yaml")
    port = _RecorderPort()
    who = f"analyst::bind_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=port,
        grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    outcome = await b.run_tool(
        "search_signals", {"query": "ports", "scope_predicate": None},
    )
    assert outcome.admitted, outcome
    assert outcome.tool_result.status == "completed"
    assert outcome.tool_result.output["refs"]
    assert port.calls and port.calls[0][0] == "search_signals"
    async with pool.acquire() as conn:
        inv = await _invocations(conn, requested_by=who)
        ev = await _events(conn, requested_by=who)
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("search_signals", "completed"),
    ]
    assert ("allow", "ok") in {(r["decision"], r["cause"]) for r in ev}


async def test_binding_denies_ungranted_pack(pool):
    pack = _load_pack("action_pack_substrate_read.yaml")
    who = f"analyst::deny_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=_RecorderPort(),
        grants=[],  # the analyst does NOT grant the pack
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    b.requested_by = who
    outcome = await b.run_tool("search_signals", {"query": "x"})
    assert not outcome.admitted
    assert outcome.block_cause == "not_granted"
    async with pool.acquire() as conn:
        inv = await _invocations(conn, requested_by=who)
        ev = await _events(conn, requested_by=who)
    assert inv == []  # never dispatched, never ledgered as admitted
    assert {(r["decision"], r["cause"]) for r in ev} == {("block", "not_granted")}


async def test_governor_rate_cap_blocks(pool):
    # Synthetic pack with a 1/hour cap — second call must block, visibly.
    pid = f"cap_pack_{uuid4().hex[:8]}"
    pack = ActionPack.model_validate({
        "identity": {
            "id": pid, "name": pid, "schema_uri": "legba/action_pack/1.0.0",
            "version": "1" * 16, "state": "active", "owner": "a3",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": "search_signals"}],
        "governor": {"budget_account": pid, "max_invocations_per_hour": 1},
    }, strict=False)
    who = f"analyst::cap_{uuid4().hex[:8]}"
    b = _binding(
        pool, pack, port=_RecorderPort(),
        grants=[ActionPackRef(pack_id=pid)],
        allows=[ActionPackRef(pack_id=pid)],
        account=who,
    )
    b.requested_by = who
    first = await b.run_tool("search_signals", {"query": "a"})
    second = await b.run_tool("search_signals", {"query": "b"})
    assert first.admitted
    assert not second.admitted
    async with pool.acquire() as conn:
        ev = await _events(conn, requested_by=who)
    assert ("block", second.block_cause) in {
        (r["decision"], r["cause"]) for r in ev
    }


# ---------------------------------------------------------------------------
# A-3a — the consult ReAct loop dispatches THROUGH the binding
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Emits a queued list of replies — the one permitted boundary double."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    async def chat_complete(self, messages, *, max_tokens, temperature, system):
        class _R:
            content = self._replies.pop(0)
            usage = None
        return _R()


async def test_consult_loop_routes_through_agency(pool):
    pack = _load_pack("action_pack_substrate_read.yaml")
    port = _RecorderPort()
    who = f"analyst::consult_{uuid4().hex[:8]}"
    binding = _binding(
        pool, pack, port=port,
        grants=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
        account=who,
    )
    binding.requested_by = who
    llm = _ScriptedLLM([
        json.dumps({"tool": "search_signals", "args": {"query": "port strikes"}}),
        json.dumps({
            "final": True, "answer": "one hit", "uncertainty": 0.4,
            "cited_refs": ["6a3b1a82-0000-4000-8000-000000000001"],
            "unanswered_aspects": [],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=port, agency_binding=binding)
    result = await consult_run_method(
        [{"question": "what about port strikes?", "scope_predicate": None}],
        {"analyst_id": who},
        deps,
    )
    assert result.consult_response.answer == "one hit"
    governed_steps = [
        s for s in result.intermediate_steps
        if s.get("kind") == "tool_call"
    ]
    assert governed_steps and all(s["governed"] for s in governed_steps)
    assert all(s["admitted"] for s in governed_steps)
    assert port.calls[0][0] == "search_signals"
    async with pool.acquire() as conn:
        inv = await _invocations(conn, requested_by=who)
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("search_signals", "completed"),
    ]


async def test_consult_loop_folds_block_back_to_planner(pool):
    # Ungranted pack: the tool call is BLOCKED; the loop folds the denial
    # into the conversation and the planner still finishes.
    pack = _load_pack("action_pack_substrate_read.yaml")
    port = _RecorderPort()
    who = f"analyst::blocked_{uuid4().hex[:8]}"
    binding = _binding(pool, pack, port=port, grants=[], allows=[], account=who)
    binding.requested_by = who
    llm = _ScriptedLLM([
        json.dumps({"tool": "search_signals", "args": {"query": "x"}}),
        json.dumps({
            "final": True, "answer": "insufficient access", "uncertainty": 0.9,
            "cited_refs": [], "unanswered_aspects": ["substrate access denied"],
        }),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=port, agency_binding=binding)
    result = await consult_run_method(
        [{"question": "anything?"}], {"analyst_id": who}, deps,
    )
    assert result.consult_response.answer == "insufficient access"
    assert port.calls == []  # the port was NEVER reached — gate held
    governed_steps = [
        s for s in result.intermediate_steps if s.get("kind") == "tool_call"
    ]
    assert governed_steps and not governed_steps[0]["admitted"]


# ---------------------------------------------------------------------------
# A-3c — the escalate_finding hook (the D1 example pack lifecycle)
# ---------------------------------------------------------------------------


async def _insert_target(conn, *, target_id: str, tags, allows) -> None:
    body = {
        "identity": {"id": target_id, "kind": "geo", "abstraction_level": "L1"},
        "scope": {"tags": tags, "geo": ["BR"], "entity_classes": []},
        "allowed_action_packs": allows,
    }
    await conn.execute(
        """
        INSERT INTO target_descriptors
          (descriptor_id, version, schema_uri, is_head, abstraction_level,
           state, owner, name, body, inherits, created_at)
        VALUES ($1, $2, 'legba/target/3.0.0', TRUE, 'L1',
                'active', 'a3_test', $1, $3::jsonb, '{}', NOW())
        ON CONFLICT (descriptor_id, version) DO NOTHING
        """,
        target_id, uuid4().hex[:16], json.dumps(body),
    )


def _escalation(pool, emitted_sink: list, *, account: str) -> EscalationBinding:
    pack = _load_pack("action_pack_escalate.yaml")

    async def _publish(subject: str, payload: bytes) -> None:
        emitted_sink.append((subject, json.loads(payload)))

    return EscalationBinding(
        binding=AgencyToolBinding(
            agency=Agency(),
            pack=pack,
            pg_pool=pool,
            tool_context=ToolContext(
                queue=None, emit=ChannelEmitter(nats_publish=_publish),
            ),
            analyst_grants=[ActionPackRef(pack_id="escalate_finding")],
            target_allows=None,  # per-run, from the target row
            requested_by=account,
            budget_account=account,
        ),
        severity_gate="high",
        confidence_gate=0.85,
    )


async def test_escalate_full_lifecycle_emits_and_audits(pool):
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::esc_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20", "news"],
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(
            title="Major refinery outage cascading",
            body="multiple corroborating signals",
            confidence=0.97,
        )
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
        )
        inv = await _invocations(conn, requested_by=who)
        ev = await _events(conn, requested_by=who)
    # Dispatch happened: channel emit landed on the recording publish with
    # the escalation body — the REAL side effect, no stub in the path.
    assert emitted, "channel emit must reach the publish callable"
    subject, record = emitted[0]
    assert subject == "channels.escalations"
    assert record["payload"]["severity"] == "high"
    assert record["payload"]["title"].startswith("Major refinery outage")
    # (reaching the publish callable IS delivery — the emitter stamps its
    # in-memory record with delivered=True only after this publish returns)
    # Audit rows: admitted invocation settled completed + allow event.
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("escalate", "completed"),
    ]
    assert ("allow", "ok") in {(r["decision"], r["cause"]) for r in ev}


async def test_escalate_verify_demoted_finding_is_a_noop(pool):
    """S8-T2 — a finding whose RAW confidence clears the gate but whose
    faithfulness verdict floors it below the gate must NOT escalate.

    Same lifecycle setup as ``test_escalate_full_lifecycle_emits_and_audits``
    (0.97 confidence, allowed g20 target) — so WITHOUT the verdict it would fire
    end-to-end — but a ``verification_block`` with ``faithfulness_score=0.2``
    yields ``effective = min(0.97, 0.2) = 0.2`` (< 0.85 gate) → hard no-op."""
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::vdem_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20", "news"],
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(
            title="Major refinery outage cascading",
            body="one weakly-cited claim",
            confidence=0.97,
        )
        # faithfulness_score floors it (unit path — no ceiling).
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
            verification_block={
                "faithfulness_score": 0.2, "confidence_ceiling": None,
            },
        )
        # confidence_ceiling floors it (composition path — perfect faithfulness
        # but a weak strongest independent sub-claim).
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
            verification_block={
                "faithfulness_score": 1.0, "confidence_ceiling": 0.3,
            },
        )
        inv = await _invocations(conn, requested_by=who)
        ev = await _events(conn, requested_by=who)
    assert emitted == [] and inv == [] and ev == []


async def test_escalate_verify_confirmed_finding_still_escalates(pool):
    """S8-T2 control — a faithful verdict (score 1.0, no ceiling) leaves the
    effective confidence == raw, so a gate-clearing finding STILL escalates:
    the fold only ever DEMOTES, never promotes."""
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::vok_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20", "news"],
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(
            title="Major refinery outage cascading",
            body="multiple corroborating signals",
            confidence=0.97,
        )
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
            verification_block={
                "faithfulness_score": 1.0, "confidence_ceiling": None,
            },
        )
        inv = await _invocations(conn, requested_by=who)
    assert emitted, "a faithful, gate-clearing finding must still escalate"
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [
        ("escalate", "completed"),
    ]


async def test_escalate_high_severity_verified_reaches_sink(pool):
    """S3-T4 ACCEPT (positive) — a fixture HIGH-severity, VERIFIED finding
    reaches the sink.

    Severity rides as the `severity:high` TAG the bounded units stamp (NOT a
    payload field), proving the tag→read→gate path. Raw confidence 0.75 is BELOW
    the 0.85 gate on its own, so this finding only escalates because the `high`
    severity weight (1.2) lifts the alert score: 0.75 × 1.2 = 0.90 ≥ 0.85. The
    faithful verdict (faithfulness 1.0) leaves the effective confidence == 0.75,
    so the fold does not demote it."""
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::hsev_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20", "news"],
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(
            title="Border force concentration confirmed",
            body="satellite-confirmed buildup [1]",
            confidence=0.75,
            tags=["escalation", "severity:high", f"target:{target_id}"],
        )
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
            verification_block={"faithfulness_score": 1.0, "confidence_ceiling": None},
        )
        inv = await _invocations(conn, requested_by=who)
    assert emitted, "a high-severity verified finding must reach the sink"
    subject, record = emitted[0]
    assert subject == "channels.escalations"
    assert record["payload"]["severity"] == "high"  # lifted from the tag
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [("escalate", "completed")]


async def test_escalate_high_severity_verify_demoted_is_a_noop(pool):
    """S3-T4 ACCEPT (negative) — a HIGH-severity finding the verify pass FLOORS
    must NOT reach the sink.

    This is the exact raw-confidence gate S3-T4 closes: under the old
    severity-OR-confidence gate an explicit `high` severity fired regardless of
    confidence, so a floored high-severity finding still paged. Now the score is
    ``effective × severity_weight`` = min(0.97, 0.2) × 1.2 = 0.24 < 0.85 → hard
    no-op, even though the RAW confidence (0.97) and the severity would each have
    cleared the old gate."""
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::hdem_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20", "news"],
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(
            title="Alarming but weakly-cited escalation",
            body="one uncited assertion",
            confidence=0.97,
            tags=["escalation", "severity:high", f"target:{target_id}"],
        )
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
            verification_block={"faithfulness_score": 0.2, "confidence_ceiling": None},
        )
        inv = await _invocations(conn, requested_by=who)
        ev = await _events(conn, requested_by=who)
    assert emitted == [] and inv == [] and ev == []


async def test_escalate_indicator_activation_is_a_trigger_class(pool):
    """S3-T4 trigger class (b) — a S3-T2 indicator_tracker FLIP into `triggered`
    escalates on its own, independent of confidence/severity.

    The summary finding carries a LOW confidence (1.0 here is incidental — set
    it low to prove confidence is not what fires) and NO severity, but a
    `data.activation_count > 0` (plus the `indicator_triggered` tag) marks a
    pre-registered warning signpost firing. Delivered on a g20 target so the
    escalate pack's applicability leg passes."""
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::flip_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20", "news"],
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(
            title="Indicator tracker: 1 indicator flip(s), 1 newly triggered",
            body="!! [BR/escalation] border-force-buildup: not_observed -> triggered",
            confidence=0.3,  # well below the 0.85 gate — NOT what fires
            tags=["deterministic", "indicator_tracker", "indicator_triggered"],
            data={"flip_count": 1, "activation_count": 1, "groups_compared": 1},
        )
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
        )
        inv = await _invocations(conn, requested_by=who)
    assert emitted, "an indicator activation must escalate as its own trigger class"
    assert [(r["tool_name"], r["outcome"]) for r in inv] == [("escalate", "completed")]


async def test_escalate_indicator_no_activation_does_not_trigger(pool):
    """Control for trigger (b) — an indicator_tracker summary with ZERO
    activations (a non-activating flip, e.g. triggered→expired) and a sub-gate
    confidence does NOT escalate. Only an activation (→triggered) is the
    trigger, and the confidence/severity score is below the gate."""
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::noflip_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20", "news"],
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(
            title="Indicator tracker: 1 indicator flip(s), 0 newly triggered",
            body="- [BR/escalation] airspace-closure: triggered -> expired",
            confidence=0.3,
            tags=["deterministic", "indicator_tracker"],
            data={"flip_count": 1, "activation_count": 0, "groups_compared": 1},
        )
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
        )
        inv = await _invocations(conn, requested_by=who)
    assert emitted == [] and inv == []


async def test_escalate_below_gate_is_a_noop(pool):
    who = f"analyst::low_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        payload = FindingPayload(title="routine summary", confidence=0.4)
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=None, actor_id="analyst::t::v",
        )
        inv = await _invocations(conn, requested_by=who)
        ev = await _events(conn, requested_by=who)
    assert emitted == [] and inv == [] and ev == []


async def test_escalate_target_not_allowing_blocks_visibly(pool):
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::na_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["g20"],
            allows=[],  # target does NOT allow the pack
        )
        payload = FindingPayload(title="urgent but unallowed", confidence=0.99)
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
        )
        inv = await _invocations(conn, requested_by=who)
        ev = await _events(conn, requested_by=who)
    assert emitted == [] and inv == []
    assert {(r["decision"], r["cause"]) for r in ev} == {("block", "not_allowed")}


async def test_escalate_non_g20_target_fails_applicability(pool):
    target_id = f"target.a3.{uuid4().hex[:8]}"
    who = f"analyst::tag_{uuid4().hex[:8]}"
    emitted: list = []
    esc = _escalation(pool, emitted, account=who)
    async with pool.acquire() as conn:
        await _insert_target(
            conn, target_id=target_id, tags=["maritime"],  # no g20 tag
            allows=[{"pack_id": "escalate_finding"}],
        )
        payload = FindingPayload(title="urgent elsewhere", confidence=0.99)
        await _maybe_escalate_finding(
            conn, escalation=esc, payload=payload,
            output_row_id=uuid4(), target_id=target_id, actor_id="analyst::t::v",
        )
        ev = await _events(conn, requested_by=who)
    assert emitted == []
    assert {(r["decision"], r["cause"]) for r in ev} == {("block", "not_applicable")}


@pytest.mark.parametrize(
    ("severity", "confidence", "expect"),
    [
        # S3-T4 — the gate keys on the SINGLE alert score
        # ``effective_confidence × severity_weight`` crossing 0.85. ``confidence``
        # here is ALREADY the verify-folded effective confidence.
        # A high-severity but DEMOTED finding (low effective confidence) must NOT
        # fire — the raw-confidence gate S3-T4 closes (these two were True under
        # the old severity-OR-confidence gate):
        ("critical", 0.1, False),   # 0.1 × 1.5 = 0.15 < 0.85
        ("high", 0.1, False),       # 0.1 × 1.2 = 0.12 < 0.85
        # High severity boosts a sub-gate confidence over the line (severity now
        # MATTERS, but only atop real post-verify confidence):
        ("critical", 0.9, True),    # 0.9 × 1.5 = 1.35
        ("high", 0.75, True),       # 0.75 × 1.2 = 0.90
        ("high", 0.2, False),       # 0.2 × 1.2 = 0.24 (verify-demoted)
        # Below-`high` severities keep the plain confidence-gate behavior
        # (baseline weight 1.0) — no regression:
        ("medium", 0.1, False),
        ("medium", 0.9, True),
        ("nonsense", 0.1, False),   # unknown severity → baseline weight
        ("nonsense", 0.9, True),
        (None, 0.9, True),          # severity-less → plain confidence gate
        (None, 0.84, False),
        (None, None, False),
    ],
)
async def test_escalation_gate_matrix(severity, confidence, expect):
    # async for the module-level asyncio pytestmark; the helper is pure.
    assert escalation_gate_decision(
        severity=severity, confidence=confidence,
        severity_gate="high", confidence_gate=0.85,
    ) is expect
