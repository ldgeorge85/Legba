# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X3/G0 — the bake-off's read scopes, on the REAL driver.

Arm C of planning/reviews/BAKEOFF_DESIGN.md reads ONLY the findings a
composition would admit, and opens only the signals those findings cite. These
tests seed ``analyst_outputs`` + ``signals`` in the ephemeral migrated database
and read them back through the real ``PostgresQdrantSubstrateQueryPort`` and
through a real ``AgencyToolBinding`` over the DRAFT packs:

* ``verified_only`` excludes a CRITIQUE-LESS finding (the done test), a
  below-floor one, a coerce-fallback one and one graded only by a generic
  (non-faithfulness) critique; the default read still returns them all;
* the page limit applies AFTER the floor (a page of unverified rows never
  starves the verified answer);
* ``belief_as_of(verified_only=True)`` gates on the fold it was asked for;
* the receipt check admits a signal an admitted finding cites and refuses
  one only an unverified finding cites;
* the verified pack's governed binding forces the scope the planner cannot
  set or clear, and the raw pack's binding refuses every derived reader
  (``unknown_tool``) while serving the raw ones — the consult loop over a
  raw binding offers and reads only the arm's surface.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts.agency import Agency, AgencyToolBinding, GLOBAL_SCOPE, ToolContext
from legba.data.analysts.consult_bakeoff_prompts import RAW_SYSTEM_PROMPT
from legba.data.schemas.action_pack import ActionPack, ActionPackRef
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

pytestmark = [pytest.mark.asyncio]

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def port(pg_pool):
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_pool, qdrant_client=None, signals_collection="legba_test_g0__signals",
    )


async def _scrub_g0(pool) -> None:
    """Delete every row this file seeds into the SESSION-shared database.

    These tests mint findings (target ``g0_target_*``), their faithfulness
    critiques, ``source.test.g0`` signals and governed-binding invocations, and
    used to leave all of it behind. ~30 fresh, unsuperseded findings is enough to
    fill the default 20-row page of an open ``list_findings`` read, so
    ``tests/runtime/test_as_of_readers.py::test_findings_believed_as_of_folds_
    the_verdict_at_d`` (whose 2026-08-01 finding is older than every one of them)
    could not find its own row when the shuffle ran it after this file (nightly
    10-09 .. 10-12). Critiques first: they point at the findings by id.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE kind = 'critique' "
            "AND data->>'analyzed_output_id' IN ("
            "  SELECT id::text FROM analyst_outputs "
            "  WHERE kind = 'finding' AND target_id LIKE 'g0\\_target\\_%')"
        )
        await conn.execute(
            "DELETE FROM analyst_outputs "
            "WHERE kind = 'finding' AND target_id LIKE 'g0\\_target\\_%'"
        )
        await conn.execute("DELETE FROM signals WHERE source_id = 'source.test.g0'")
        await conn.execute(
            "DELETE FROM action_pack_invocations WHERE requested_by LIKE 'analyst::g0\\_%'"
        )


@pytest_asyncio.fixture(autouse=True)
async def _own_what_you_seed(pg_pool):
    await _scrub_g0(pg_pool)
    try:
        yield
    finally:
        await _scrub_g0(pg_pool)


def _pack(fname: str) -> ActionPack:
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"]["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


async def _signal(pool) -> str:
    sid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO signals (id, source_id, modality, payload) "
            "VALUES ($1, 'source.test.g0', 'text', $2::jsonb)",
            sid, json.dumps({"title": f"g0 receipt {sid.hex[:6]}", "raw_body": "body"}),
        )
    return str(sid)


async def _finding(pool, *, target: str, confidence: float = 0.8, tags=None,
                   derived=(), produced_at: datetime | None = None) -> str:
    oid = uuid4()
    data = {"tags": list(tags)} if tags else {}
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (id, kind, title, body, confidence, severity, data,
                                         target_id, produced_at, derived_from, schema_uri)
            VALUES ($1, 'finding', $2, 'body', $3, 'medium', $4::jsonb, $5, $6, $7::uuid[],
                    'iglu:legba/finding/jsonschema/1-0-0')
            """,
            oid, f"g0_{oid.hex[:6]}", confidence, json.dumps(data), target,
            produced_at or datetime.now(timezone.utc), list(derived),
        )
    return str(oid)


async def _critique(pool, *, analyzed: str, score: float, title: str = "Faithfulness verify",
                    produced_at: datetime | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (id, kind, title, body, confidence, severity, data,
                                         produced_at, derived_from, schema_uri)
            VALUES ($1, 'critique', $2, '', $3, NULL, $4::jsonb, $5, '{}'::uuid[],
                    'iglu:legba/critique/jsonschema/1-0-0')
            """,
            uuid4(), title, score,
            json.dumps({"analyzed_output_id": analyzed, "overall_score": score}),
            produced_at or datetime.now(timezone.utc),
        )


async def _seed_four(pool):
    t = f"g0_target_{uuid4().hex[:6]}"
    admitted = await _finding(pool, target=t)
    await _critique(pool, analyzed=admitted, score=0.9)
    critique_less = await _finding(pool, target=t)
    below = await _finding(pool, target=t)
    await _critique(pool, analyzed=below, score=0.3)
    coerced = await _finding(pool, target=t, tags=["coerce_failed"])
    await _critique(pool, analyzed=coerced, score=0.9)
    generic = await _finding(pool, target=t)
    await _critique(pool, analyzed=generic, score=0.9, title="Critic review")
    return t, admitted, critique_less, below, coerced, generic


# ---------------------------------------------------------------------------
# list_findings / belief_as_of
# ---------------------------------------------------------------------------


async def test_verified_only_excludes_a_critique_less_finding(pg_pool, port):
    t, admitted, critique_less, below, coerced, generic = await _seed_four(pg_pool)
    out = await port.list_findings(target_id=t, verified_only=True)
    assert [r["id"] for r in out["rows"]] == [admitted]
    assert critique_less not in out["refs"]
    assert out["verified_only"] == {"floor": 0.5}
    row = out["rows"][0]
    assert row["critic_score"] == pytest.approx(0.9)          # the faithfulness verdict
    assert row["effective_confidence"] == pytest.approx(0.8)  # min(0.8, 0.9)
    # The default read is unchanged: every one of them comes back.
    plain = await port.list_findings(target_id=t)
    assert {admitted, critique_less, below, coerced, generic} <= set(plain["refs"])
    assert "verified_only" not in plain


async def test_verified_only_limits_after_the_floor(pg_pool, port):
    t = f"g0_target_{uuid4().hex[:6]}"
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    verified = await _finding(pg_pool, target=t, produced_at=old)
    await _critique(pg_pool, analyzed=verified, score=0.95)
    for _ in range(3):  # three NEWER rows nobody verified
        await _finding(pg_pool, target=t)
    out = await port.list_findings(target_id=t, verified_only=True, limit=1)
    assert out["refs"] == [verified]


async def test_verified_only_honours_the_env_floor(pg_pool, port, monkeypatch):
    t = f"g0_target_{uuid4().hex[:6]}"
    f = await _finding(pg_pool, target=t, confidence=0.9)
    await _critique(pg_pool, analyzed=f, score=0.6)
    monkeypatch.setenv("LEGBA_COMPOSITION_VERIFY_FLOOR", "0.7")
    assert (await port.list_findings(target_id=t, verified_only=True))["refs"] == []
    monkeypatch.setenv("LEGBA_COMPOSITION_VERIFY_FLOOR", "0.5")
    assert (await port.list_findings(target_id=t, verified_only=True))["refs"] == [f]


async def test_belief_as_of_verified_only_gates_on_the_chosen_fold(pg_pool, port):
    t = f"g0_target_{uuid4().hex[:6]}"
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    late = await _finding(pg_pool, target=t, confidence=0.9, produced_at=t0)
    await _critique(pg_pool, analyzed=late, score=0.8,
                    produced_at=datetime(2026, 8, 10, tzinfo=timezone.utc))  # after D
    never = await _finding(pg_pool, target=t, confidence=0.9, produced_at=t0)
    held = await port.belief_as_of(as_of="2026-08-05", target_id=t, verified_only=True)
    assert held["refs"] == []                       # ungraded ON D: not admissible then
    latest = await port.belief_as_of(as_of="2026-08-05", target_id=t,
                                     fold_verdicts="latest", verified_only=True)
    assert latest["refs"] == [late]                 # today's verdict admits it
    assert never not in latest["refs"]              # never graded: never admitted
    assert latest["verified_only"] == {"floor": 0.5}
    plain = await port.belief_as_of(as_of="2026-08-05", target_id=t)
    assert {late, never} <= set(plain["refs"])


async def test_verified_receipt_scope(pg_pool, port):
    t = f"g0_target_{uuid4().hex[:6]}"
    cited_by_verified = await _signal(pg_pool)
    cited_by_unverified = await _signal(pg_pool)
    ok = await _finding(pg_pool, target=t, derived=[cited_by_verified])
    await _critique(pg_pool, analyzed=ok, score=0.9)
    await _finding(pg_pool, target=t, derived=[cited_by_unverified])
    assert await port.verified_receipt(doc_id=cited_by_verified) is True
    assert await port.verified_receipt(doc_id=cited_by_unverified) is False
    assert await port.verified_receipt(doc_id="not-a-uuid") is False


# ---------------------------------------------------------------------------
# The governed bindings over the DRAFT packs (the real gate, the real port)
# ---------------------------------------------------------------------------


def _binding(pool, pack: ActionPack, port) -> AgencyToolBinding:
    who = f"analyst::g0_{uuid4().hex[:8]}"
    ref = ActionPackRef(pack_id=pack.identity.id)
    return AgencyToolBinding(
        agency=Agency(), pack=pack, pg_pool=pool,
        tool_context=ToolContext(queue=None, emit=None, substrate=port),
        analyst_grants=[ref.model_dump()], target_allows=[ref],
        scope=GLOBAL_SCOPE, requested_by=who, budget_account=who,
    )


async def test_verified_pack_binding_forces_the_scope(pg_pool, port):
    t, admitted, critique_less, *_ = await _seed_four(pg_pool)
    b = _binding(pg_pool, _pack("action_pack_substrate_read_verified.yaml"), port)
    # The pack config sets the scope; the planner passes nothing and gets it.
    got = await b.run_tool("list_findings", {"target_id": t})
    assert got.admitted, got
    assert got.tool_result.output["refs"] == [admitted]
    assert got.tool_result.output["verified_only"] == {"floor": 0.5}
    # The planner cannot switch it off: the agency's arg grammar refuses the key.
    off = await b.run_tool("list_findings", {"target_id": t, "verified_only": False})
    assert not off.admitted and off.block_cause == "invalid_arg"
    # Receipt scope: an id no verified finding cites is refused by name.
    stray = await _signal(pg_pool)
    refused = await b.run_tool("read_document", {"doc_id": stray})
    assert refused.admitted
    assert refused.tool_result.status == "failed"
    assert "receipt_scope" in (refused.tool_result.error or "")
    # A derived reader the pack does not list is refused at the gate.
    blocked = await b.run_tool("list_situations", {})
    assert not blocked.admitted and blocked.block_cause == "unknown_tool"


async def test_stock_pack_binding_cannot_take_the_verified_flag(pg_pool, port):
    t, admitted, critique_less, *_ = await _seed_four(pg_pool)
    b = _binding(pg_pool, _pack("action_pack_substrate_read.yaml"), port)
    # Stock consult cannot opt into the arm's scope either (arg grammar) ...
    got = await b.run_tool("list_findings", {"target_id": t, "verified_only": True})
    assert not got.admitted and got.block_cause == "invalid_arg"
    # ... and its plain read is unchanged.
    plain = await b.run_tool("list_findings", {"target_id": t})
    assert plain.admitted and critique_less in plain.tool_result.output["refs"]
    assert "verified_only" not in plain.tool_result.output


async def test_raw_pack_binding_refuses_every_derived_reader(pg_pool, port):
    b = _binding(pg_pool, _pack("action_pack_substrate_read_raw.yaml"), port)
    for tool in ("list_findings", "belief_as_of", "list_situations", "query_events",
                 "query_facts", "inspect_entity", "search_context", "series_history",
                 "get_timeline", "query_hypotheses"):
        out = await b.run_tool(tool, {})
        assert not out.admitted and out.block_cause == "unknown_tool", tool
    ok = await b.run_tool("list_targets", {})
    assert ok.admitted and ok.tool_result.status == "completed"


async def test_consult_loop_over_the_raw_binding_reads_the_arm_surface(pg_pool, port):
    """run_method through the REAL binding: the arm's prompt is the one sent,
    a derived reader the planner asks for is refused at the gate, a raw one
    is served — the arm is what the binding's pack says it is."""
    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 20
        reasoning_tokens = 0

    class _Resp:
        def __init__(self, content):
            self.content = content
            self.usage = _Usage()

    class _LLM:
        subprovider = "vllm-test"

        def __init__(self):
            self.systems: list[str] = []
            self.replies = [
                json.dumps({"tool": "list_findings", "args": {}}),
                json.dumps({"tool": "list_targets", "args": {}}),
                "<<<FINAL>>>\nuncertainty: 0.4\n\n## Bottom line\nAnswer.",
            ]

        async def chat_complete(self, messages, *, system=None, **kw):
            self.systems.append(system or "")
            return _Resp(self.replies.pop(0))

    llm = _LLM()
    binding = _binding(pg_pool, _pack("action_pack_substrate_read_raw.yaml"), port)
    deps = ConsultOnDemandDeps(llm=llm, substrate=port, agency_binding=binding)
    result = await run_method([{"question": "What changed?", "mode": "chat"}], {}, deps)
    assert llm.systems and llm.systems[0].startswith(RAW_SYSTEM_PROMPT)
    assert "list_situations" not in llm.systems[0]
    steps = result.intermediate_steps or []
    blocked = [s for s in steps if "tool_blocked" in json.dumps(s, default=str)]
    assert blocked, "list_findings must be refused at the gate for arm B"
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tool_name, outcome FROM action_pack_invocations WHERE requested_by = $1",
            binding.requested_by,
        )
    assert [(r["tool_name"], r["outcome"]) for r in rows] == [("list_targets", "completed")]


async def test_the_scrub_removes_every_row_the_file_seeds(pg_pool, port):
    """X4 NIGHTLY: the findings, critiques and signals this file seeds must not
    outlive it -- ~30 of them fill a default 20-row open page of the session DB."""
    t, *ids = await _seed_four(pg_pool)
    sig = await _signal(pg_pool)
    await _scrub_g0(pg_pool)
    async with pg_pool.acquire() as conn:
        outputs = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE id::text = ANY($1::text[]) "
            "OR data->>'analyzed_output_id' = ANY($1::text[])", ids)
        sigs = await conn.fetchval("SELECT count(*) FROM signals WHERE id::text = $1", sig)
    assert (outputs, sigs) == (0, 0)
    assert (await port.list_findings(target_id=t, include_superseded=True))["rows"] == []
