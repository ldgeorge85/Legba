# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult first-class: usage per turn, judged-or-labelled, the
usage route, and the per-turn ceiling knob (declared, off, refusing loud).

THE DONE TEST is the first one: ten live-shaped consult turns go through the
ASGI app (the real ``build_consult_router``, the real detached-run manager,
the real persistence path) with only the Dapr sidecar stubbed, and every
persisted assistant row — read back on the real driver — carries a
``consult_usage/1`` receipt and a ``consult_verification/1`` stamp: a score,
or ``unassessable``/``not_judged`` with a reason. An unassessable turn never
carries 0.5. The usage route sums the same rows per day.

The judging half runs the REAL verify pass (the deterministic floor + the
judge prompt + the folds) over a stub judge — no model is called.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import legba.data.registry.consult_api as consult_api
from legba.data.analysts import consult_judging
from legba.data.config import PostgresConfig
from legba.data.registry import consult_turn_ceiling, consult_turn_stamps
from legba.data.registry._deps import principal_label
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_sessions_api import build_consult_sessions_router
from legba.data.registry.errors import DescriptorNotFound

REF_A = "11111111-1111-4111-8111-111111111111"
REF_B = "22222222-2222-4222-8222-222222222222"
_TOKEN = "c1-usage-judging-test-token"
_PRINCIPAL = principal_label(_TOKEN)
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# The ASGI app over the real pool; only the sidecar is stubbed.
# ---------------------------------------------------------------------------


class _DescRow:
    version = "v" + "c" * 16


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    async def get(self, descriptor_id, *, family=None, version=None):
        return _DescRow()


class _StackRegistry:
    async def get(self, component_id: str, version: str | None = None):
        if component_id not in consult_api.CONSULT_MODEL_ALLOWLIST.values():
            raise DescriptorNotFound("stack", component_id, version)
        return object()


def _sidecar(envelopes: deque, *, status_code: int = 200):
    """A stand-in for ``httpx.AsyncClient`` that answers each actor invoke
    with the next queued envelope (in POST order)."""

    class _Resp:
        def __init__(self, body: dict[str, Any]) -> None:
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, json=None, headers=None):
            return _Resp(envelopes.popleft())

    return _Client


def _app(pool: Any) -> FastAPI:
    os.environ.pop("LEGBA_REGISTRY_API_TOKEN", None)
    os.environ["LEGBA_DEV_MODE"] = "1"
    deps = RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(pool),  # type: ignore[arg-type]
        stack_registry=_StackRegistry(),  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
    )
    app = FastAPI()
    app.include_router(consult_api.build_consult_router(deps), prefix="/api/v1")
    app.include_router(build_consult_sessions_router(deps), prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM consult_sessions WHERE principal = $1", _PRINCIPAL,
        )
    yield pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM consult_sessions WHERE principal = $1", _PRINCIPAL,
        )
    await pool.close()


def _receipt(*, priced: bool, est: float = 0.0, model: str = "gpt-oss-120b") -> dict:
    """The runtime's usage receipt (``SpendGuard.receipt``)."""
    return {
        "calls": 3, "input_tokens": 12_000, "output_tokens": 900,
        "est_cost_usd": est, "max_input_tokens": 150_000, "max_cost_usd": 3.0,
        "model": model, "subprovider": "vllm", "elapsed_s": 41.2, "priced": priced,
    }


def _judged(score: float) -> dict:
    return {
        "schema": "consult_verification/1", "state": "judged", "score": score,
        "reason": None, "judge_status": "llm", "checkable_claims": 4,
        "supported_claims": 3, "judge_llm_ref": "llm.verify.slm_8b",
        "weak_judge": True,
    }


#: Ten live-shaped turns: (plane, runtime usage, runtime verification).
_TURNS: list[tuple[str, Any, Any]] = [
    ("core", _receipt(priced=False), _judged(0.9)),
    ("core", _receipt(priced=False), _judged(0.75)),
    ("core", _receipt(priced=False), _judged(1.0)),
    ("core", _receipt(priced=False), _judged(0.6)),
    ("core", _receipt(priced=False),
     {"state": "unassessable", "score": None, "reason": "no_cited_claims"}),
    # A runtime that (wrongly) stamped the fleet's 0.5 on an unassessable
    # turn: the front door strips the number.
    ("core", _receipt(priced=False),
     {"state": "unassessable", "score": 0.5, "reason": "no_checkable_claims"}),
    ("core", _receipt(priced=False),
     {"state": "not_judged", "score": None, "reason": "judge_disabled"}),
    # A runtime that predates judging: no stamp at all → labelled here.
    ("core", _receipt(priced=False), None),
    ("opus", _receipt(priced=True, est=0.4212, model="claude-opus-4-8"), _judged(0.8)),
    # A runtime that predates the receipt: no usage at all.
    ("core", None, {"state": "not_judged", "score": None, "reason": "judge_timeout"}),
]


def _envelope(i: int, usage: Any, verification: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"synthesis_status": "complete"}
    if usage is not None:
        data["usage"] = usage
    if verification is not None:
        data["verification"] = verification
    return {
        "outcome": "success",
        "mode": "chat",
        "consult_response": {
            "question": f"q{i}",
            "answer": f"Answer {i}: the bombers left Fairford [{REF_A}].",
            "uncertainty": 0.3,
            "cited_substrate_refs": [REF_A],
            "unanswered_aspects": [],
            "data": data,
        },
        "derived_from": [REF_A],
    }


@pytest.mark.asyncio
async def test_ten_live_shaped_turns_persist_usage_and_a_stamp(pg_pool, monkeypatch):
    queue = deque(_envelope(i, u, v) for i, (_, u, v) in enumerate(_TURNS))
    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _sidecar(queue))
    monkeypatch.delenv(consult_turn_ceiling.TURN_COST_CEILING_ENV, raising=False)

    async with AsyncClient(
        transport=ASGITransport(app=_app(pg_pool)), base_url="http://testserver",
    ) as client:
        bodies = []
        for i, (plane, _u, _v) in enumerate(_TURNS):
            r = await client.post(
                "/api/v1/consult",
                json={"question": f"q{i}", "mode": "chat", "model": plane},
                headers=_HEADERS,
            )
            assert r.status_code == 200, r.text
            bodies.append(r.json())

        # The live body carries the same stamps the row will.
        assert bodies[0]["verification"]["state"] == "judged"
        assert bodies[0]["usage"]["schema"] == "consult_usage/1"
        assert bodies[7]["verification"] == {
            "schema": "consult_verification/1", "state": "not_judged",
            "score": None, "reason": "no_stamp_from_run",
        }

        # --- the persisted rows, read back on the real driver --------------
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.content, t.usage, t.verification, t.request_id,
                       t.synthesis_status, t.session_id
                  FROM consult_turns t
                  JOIN consult_sessions s ON s.id = t.session_id
                 WHERE s.principal = $1 AND t.role = 'assistant'
                 ORDER BY t.created_at
                """,
                _PRINCIPAL,
            )
        assert len(rows) == 10
        by_q = {r["content"].split(":")[0]: r for r in rows}
        for i, (plane, usage_in, ver_in) in enumerate(_TURNS):
            row = by_q[f"Answer {i}"]
            usage = json.loads(row["usage"])
            ver = json.loads(row["verification"])
            assert row["request_id"] == bodies[i]["request_id"]
            assert row["synthesis_status"] == "complete"
            # USAGE — always the receipt, never `{}`.
            assert usage["schema"] == "consult_usage/1"
            assert usage["plane"] == plane
            assert usage["route"] == consult_api.CONSULT_MODEL_ALLOWLIST[plane]
            # VERIFICATION — a state, always; a score only when judged.
            assert ver["state"] in {"judged", "unassessable", "not_judged"}
            if ver["state"] == "judged":
                assert isinstance(ver["score"], float)
            else:
                assert ver["score"] is None and ver["reason"]
            assert ver.get("score") != 0.5 or ver["state"] == "judged"

        core = json.loads(by_q["Answer 0"]["usage"])
        assert core["input_tokens"] == 12_000 and core["output_tokens"] == 900
        assert core["model"] == "gpt-oss-120b" and core["elapsed_s"] == 41.2
        assert core["cost_usd"] is None and core["cost_reason"] == "route_unpriced"
        opus = json.loads(by_q["Answer 8"]["usage"])
        assert opus["cost_usd"] == pytest.approx(0.4212) and opus["cost_reason"] is None
        bare = json.loads(by_q["Answer 9"]["usage"])
        assert bare["input_tokens"] is None and bare["reason"] == "usage_not_reported"
        stripped = json.loads(by_q["Answer 5"]["verification"])
        assert stripped == {
            "state": "unassessable", "score": None, "reason": "no_checkable_claims",
            "schema": "consult_verification/1",
        }

        # --- the session route lifts both onto the reloaded turn -------------
        sid = str(by_q["Answer 8"]["session_id"])
        r = await client.get(f"/api/v1/consult/sessions/{sid}")
        assert r.status_code == 200, r.text
        user, answer = r.json()["turns"]
        assert answer["verification"]["state"] == "judged"
        assert answer["usage"]["cost_usd"] == pytest.approx(0.4212)
        assert user["verification"] is None

        # --- the usage route: totals per day for this principal --------------
        r = await client.get("/api/v1/consult/usage", headers=_HEADERS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"] == _PRINCIPAL
        totals = body["totals"]
        assert totals["turns"] == 10
        assert totals["turns_with_usage"] == 9
        assert totals["input_tokens"] == 9 * 12_000
        assert totals["priced_turns"] == 1
        assert totals["cost_usd"] == pytest.approx(0.4212)
        assert totals["judged"] == 5 and totals["unassessable"] == 2
        assert totals["not_judged"] == 3 and totals["verification_missing"] == 0
        assert len(body["days"]) == 1 and body["days"][0]["turns"] == 10

        # A principal with no turns: absence, never zeros dressed as usage.
        r = await client.get(
            "/api/v1/consult/usage", params={"user": "bearer:nobody"},
            headers=_HEADERS,
        )
        empty = r.json()
        assert empty["days"] == [] and empty["totals"]["turns"] == 0
        assert empty["totals"]["input_tokens"] is None
        assert empty["totals"]["cost_usd"] is None


@pytest.mark.asyncio
async def test_a_failed_run_persists_its_label_and_the_usage_reason(pg_pool, monkeypatch):
    """A run that dies before answering writes the partial turn — and that
    turn, too, carries both stamps: not_judged (synthesis_incomplete) and a
    usage receipt saying nothing was reported (no meter frame reached it)."""
    queue = deque([{"outcome": "error", "error": "boom"}])
    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _sidecar(queue))
    async with AsyncClient(
        transport=ASGITransport(app=_app(pg_pool)), base_url="http://testserver",
    ) as client:
        r = await client.post(
            "/api/v1/consult", json={"question": "q", "mode": "chat"},
            headers=_HEADERS,
        )
    assert r.status_code == 502
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.usage, t.verification, t.synthesis_status, t.request_id
              FROM consult_turns t JOIN consult_sessions s ON s.id = t.session_id
             WHERE s.principal = $1 AND t.role = 'assistant'
            """,
            _PRINCIPAL,
        )
    assert row is not None
    assert json.loads(row["verification"])["reason"] == "synthesis_incomplete"
    usage = json.loads(row["usage"])
    assert usage["plane"] == "core" and usage["reason"] == "usage_not_reported"
    assert row["synthesis_status"] == "none" and row["request_id"]


@pytest.mark.asyncio
async def test_a_deep_answer_turn_carries_both_labels(pg_pool):
    """The deep task's answer turn (written by the status poll) says
    its usage was not carried and that it was not judged; never blank."""
    from legba.data.registry import consult_persistence

    task = f"task-{uuid4()}"
    sid = await consult_persistence.create_session(
        pg_pool, mode="deep", question="deep q", principal=_PRINCIPAL, task_id=task,
    )
    assert sid
    assert await consult_persistence.record_deep_completion(
        pg_pool, task_id=task, answer="deep answer",
    )
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT usage, verification FROM consult_turns "
            "WHERE session_id = $1 AND role = 'assistant'", sid,
        )
    assert json.loads(row["verification"]) == {
        "schema": "consult_verification/1", "state": "not_judged",
        "score": None, "reason": "deep_consult_not_judged",
    }
    usage = json.loads(row["usage"])
    assert usage["reason"] == "deep_consult_usage_not_carried"
    assert usage["input_tokens"] is None and usage["cost_usd"] is None


# ---------------------------------------------------------------------------
# The per-turn ceiling: declared, off, refusing loud.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "-1", "two dollars"])
async def test_the_turn_ceiling_knob_refuses_loud_when_invalid(pg_pool, monkeypatch, value):
    """Closed SEAMS #72: a positive ceiling is enforced by the runner
    (``consult_turn_cost``); a value that cannot be enforced still refuses."""
    queue: deque = deque()
    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _sidecar(queue))
    monkeypatch.setenv(consult_turn_ceiling.TURN_COST_CEILING_ENV, value)
    async with AsyncClient(
        transport=ASGITransport(app=_app(pg_pool)), base_url="http://testserver",
    ) as client:
        r = await client.post(
            "/api/v1/consult", json={"question": "q", "mode": "chat"},
            headers=_HEADERS,
        )
        assert r.status_code == 503, r.text
        assert consult_turn_ceiling.TURN_COST_CEILING_ENV in r.json()["detail"]
        r = await client.post(
            "/api/v1/consult/runs/x/synthesize", json={}, headers=_HEADERS,
        )
        assert r.status_code == 503
        assert consult_turn_ceiling.TURN_COST_CEILING_ENV in r.json()["detail"]
    # Refused BEFORE anything ran or was written.
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM consult_sessions WHERE principal = $1", _PRINCIPAL,
        )
    assert n == 0


def test_the_turn_ceiling_is_off_by_default(monkeypatch):
    monkeypatch.delenv(consult_turn_ceiling.TURN_COST_CEILING_ENV, raising=False)
    assert consult_turn_ceiling.configured_turn_ceiling() is None
    consult_turn_ceiling.check_turn_ceiling()  # no raise
    monkeypatch.setenv(consult_turn_ceiling.TURN_COST_CEILING_ENV, "   ")
    consult_turn_ceiling.check_turn_ceiling()  # empty = off
    monkeypatch.setenv(consult_turn_ceiling.TURN_COST_CEILING_ENV, "5")
    consult_turn_ceiling.check_turn_ceiling()  # a positive ceiling is enforced, not refused


# ---------------------------------------------------------------------------
# The stamps' invariants at the front door.
# ---------------------------------------------------------------------------


def test_a_non_judged_stamp_never_carries_a_score():
    for state in ("unassessable", "not_judged"):
        out = consult_turn_stamps.verification_stamp(
            {"state": state, "score": 0.5}, reason_if_absent="x",
        )
        assert out["score"] is None and out["reason"] == state
    out = consult_turn_stamps.verification_stamp(
        {"state": "judged", "score": None}, reason_if_absent="x",
    )
    assert out["state"] == "not_judged" and out["reason"] == "judged_without_score"
    assert consult_turn_stamps.verification_stamp(
        {"state": "bogus"}, reason_if_absent="why",
    )["reason"] == "why"


def test_usage_receipt_never_invents_a_cost():
    priced = consult_turn_stamps.usage_receipt(
        {"input_tokens": 5, "est_cost_usd": 0.0, "priced": True},
        plane="opus", route="r",
    )
    assert priced["cost_usd"] == 0.0 and priced["cost_reason"] is None
    old_meter = consult_turn_stamps.usage_receipt(
        {"input_tokens": 5, "est_cost_usd": 0.0}, plane="core", route="r",
    )
    assert old_meter["cost_usd"] is None
    assert old_meter["cost_reason"] == "pricing_not_reported"
    assert consult_turn_stamps.usage_from_steps(
        [{"usage": {"calls": 1}}, {"kind": "x"}, {"usage": {"calls": 2}}, {}],
    ) == {"calls": 2}


def test_usage_receipt_carries_the_turn_ceiling_the_run_was_held_to():
    """consult_turn_ceiling.py documents ``turn_ceiling_usd`` on the receipt;
    the console reads ``usage.turn_ceiling_usd``. The real guard's snapshot is
    the source: armed → the figure; the stock guard or no run → ``null``."""
    from legba.data.analysts.consult_spend_guard import SpendGuard
    from legba.data.analysts.consult_turn_cost import TurnCeilingGuard

    armed = TurnCeilingGuard(
        max_input_tokens=0, max_cost_usd=0.0, turn_ceiling_usd=10.0,
        input_per_m=3.0, output_per_m=15.0,
    )
    receipt = consult_turn_stamps.usage_receipt(armed.snapshot(), plane="opus", route="r")
    assert receipt["turn_ceiling_usd"] == 10.0
    stock = consult_turn_stamps.usage_receipt(
        SpendGuard(max_input_tokens=0, max_cost_usd=0.0).snapshot(), plane="core", route="r",
    )
    assert "turn_ceiling_usd" in stock and stock["turn_ceiling_usd"] is None
    assert consult_turn_stamps.usage_receipt(None, plane="core", route="r")["turn_ceiling_usd"] is None


# ---------------------------------------------------------------------------
# Judging — the real verify pass over a stub judge; no model is called.
# ---------------------------------------------------------------------------

_EVIDENCE = {
    REF_A: {"title": "Bombers leave Fairford",
            "body": "The United States withdrew all twelve B-1 bombers from RAF Fairford."},
    REF_B: {"title": "Iran denies plot",
            "body": "Iran's foreign ministry denied any involvement in the Fairford plot."},
}


class _Conn:
    async def fetch(self, sql, keys):
        if "FROM analyst_outputs" in sql:
            return [
                {"id": str(k), "title": _EVIDENCE[str(k)]["title"],
                 "body": _EVIDENCE[str(k)]["body"], "analyst_id": "desk"}
                for k in keys if str(k) in _EVIDENCE
            ]
        return []


class _Pool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


@dataclass
class _Resp:
    content: str
    usage: Any = None


class _StubJudge:
    subprovider = "vllm"

    def __init__(self, verdicts: list[str], *, delay: float = 0.0, fail: bool = False):
        self._verdicts = verdicts
        self._delay = delay
        self._fail = fail
        self.calls = 0

    async def chat_complete(self, messages, **kw):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("upstream 500")
        return _Resp(json.dumps({
            "verdicts": self._verdicts,
            "quotes": [
                "withdrew all twelve B-1 bombers from RAF Fairford",
                "denied any involvement in the Fairford plot",
            ][: len(self._verdicts)],
        }))


_ANSWER = (
    f"The US withdrew twelve B-1 bombers from RAF Fairford [{REF_A}]. "
    f"Iran denied involvement in the plot [{REF_B[:8]}]. "
    "This sentence is model knowledge and carries no citation."
)


@pytest.fixture
def _judge_on(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.delenv(consult_judging.CONSULT_JUDGE_STACK_REF_ENV, raising=False)
    # These tests assert the PLATFORM defaults (the 8B route, labelled weak). A
    # deployment that repoints the chain — the 2026-10-23 cutover sets
    # LEGBA_JUDGE_WEAK_RUNGS= (empty) and a new chain default — leaks those
    # into the container run and the assertions read deployment config.
    monkeypatch.delenv("LEGBA_JUDGE_WEAK_RUNGS", raising=False)
    monkeypatch.delenv("LEGBA_JUDGE_CHAIN_DEFAULT", raising=False)
    monkeypatch.delenv("LEGBA_JUDGE_STACK_REF", raising=False)


async def _run(judge: Any = None, **kw: Any) -> dict[str, Any]:
    async def _resolver(ref: str) -> Any:
        if judge is None:
            raise RuntimeError("route gone")
        return judge

    args = dict(
        answer=_ANSWER, cited_refs=[REF_A, REF_B], synthesis_status="complete",
        judge_resolver=_resolver, pg_pool=_Pool(),
    )
    args.update(kw)
    return await consult_judging.judge_consult_turn(**args)


@pytest.mark.asyncio
async def test_a_cited_answer_is_judged_on_the_8b_route_and_says_weak(_judge_on):
    judge = _StubJudge(["supported", "supported"])
    stamp = await _run(judge)
    assert stamp["state"] == "judged" and stamp["score"] == 1.0
    assert stamp["checkable_claims"] == 2 and stamp["cited_sentences"] == 2
    assert stamp["judge_llm_ref"] == "llm.verify.slm_8b" and stamp["weak_judge"] is True
    assert stamp["judge_pipeline_version"]
    assert judge.calls >= 1


@pytest.mark.asyncio
async def test_an_unsupported_claim_lowers_the_score(_judge_on):
    stamp = await _run(_StubJudge(["supported", "unsupported"]))
    assert stamp["state"] == "judged" and stamp["score"] == 0.5
    assert stamp["unsupported"] and stamp["unsupported"][0]["text"].startswith("Iran")


@pytest.mark.asyncio
async def test_an_uncited_answer_is_unassessable_never_scored(_judge_on):
    stamp = await _run(_StubJudge(["supported"]), answer="Nothing cited here. Or here.")
    assert stamp == {
        "schema": "consult_verification/1", "state": "unassessable",
        "score": None, "reason": "no_cited_claims", "cited_sentences": 0,
        "judge_pipeline_version": stamp["judge_pipeline_version"],
    }


@pytest.mark.asyncio
async def test_unassessable_from_the_verify_pass_is_not_point_five(_judge_on, monkeypatch):
    from legba.data.provenance import verify as verify_mod
    from legba.data.provenance.verify import FaithfulnessReport

    async def _verify(**_kw):
        return FaithfulnessReport(
            faithfulness_score=1.0, checkable_claims=0, supported_claims=0,
            judge_status="llm", score_state="unassessable",
            score_state_reason="no_checkable_claims",
        )

    monkeypatch.setattr(verify_mod, "verify_finding_faithfulness", _verify)
    stamp = await _run(_StubJudge(["supported"]))
    assert stamp["state"] == "unassessable" and stamp["score"] is None
    assert stamp["reason"] == "no_checkable_claims"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"synthesis_status": "partial"}, "synthesis_incomplete"),
        ({"judge_resolver": None}, "no_judge_route"),
        ({"pg_pool": None}, "no_evidence_reader"),
    ],
)
async def test_not_judged_labels(_judge_on, kw, reason):
    stamp = await _run(_StubJudge(["supported", "supported"]), **kw)
    assert stamp["state"] == "not_judged" and stamp["score"] is None
    assert stamp["reason"] == reason


@pytest.mark.asyncio
async def test_not_judged_when_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    stamp = await _run(_StubJudge(["supported", "supported"]))
    assert stamp["state"] == "not_judged" and stamp["reason"] == "judge_disabled"


@pytest.mark.asyncio
async def test_not_judged_when_the_route_cannot_be_built(_judge_on):
    stamp = await _run(None)
    assert stamp["state"] == "not_judged"
    assert stamp["reason"] == "judge_route_unresolved"


@pytest.mark.asyncio
async def test_not_judged_on_timeout(_judge_on):
    stamp = await _run(_StubJudge(["supported", "supported"], delay=2.0), timeout_s=0.05)
    assert stamp["state"] == "not_judged" and stamp["reason"] == "judge_timeout"


@pytest.mark.asyncio
async def test_a_floor_only_pass_is_not_published_as_a_verdict(_judge_on):
    stamp = await _run(_StubJudge(["supported"], fail=True))
    assert stamp["state"] == "not_judged" and stamp["score"] is None
    assert stamp["reason"] == "judge_unavailable"
    assert stamp["judge_status"] not in ("llm", "partial")


def test_ref_markers_are_rewritten_to_ordinals_and_ambiguity_is_no_citation():
    a2 = "11111111-9999-4999-8999-999999999999"  # shares REF_A's 8-hex prefix
    body, ordered, n, cut = consult_judging.build_consult_verify_inputs(
        f"One [{REF_A}]. Two [11111111]. Three [{REF_B[:8]}, {REF_A}]. "
        "Four [see 3]. Five [[ref:" + REF_B + "]].",
        [REF_A, a2, REF_B],
    )
    assert body.split("\n\n") == ["One [1].", "Three [2][1].", "Five [2]."]
    assert ordered == [REF_A, REF_B] and n == 3 and cut is False
    body, ordered, n, cut = consult_judging.build_consult_verify_inputs(
        f"One [{REF_A}]. Two [{REF_B}].", [REF_A, REF_B], max_claims=1,
    )
    assert body == "One [1]." and ordered == [REF_A] and n == 2 and cut is True


# ---------------------------------------------------------------------------
# The run stamps both: the consult loop's receipt + verification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_consult_run_stamps_its_receipt_and_its_verdict(_judge_on):
    from uuid import UUID

    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method

    from .test_analyst_consult_on_demand import _ScriptedLLMHandler, _SubstrateStub

    sig = UUID(REF_A)
    substrate = _SubstrateStub(
        signal_rows=[{"id": REF_A, "title": "Bombers leave Fairford"}],
        signal_refs=[sig],
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "Fairford"}}),
        json.dumps({
            "final": True,
            "answer": f"The US withdrew twelve B-1 bombers from RAF Fairford [{REF_A}].",
            "uncertainty": 0.2,
            "cited_refs": [REF_A],
            "unanswered_aspects": [],
        }),
    ])
    judge = _StubJudge(["supported"])

    async def _resolver(ref: str) -> Any:
        return judge

    deps = ConsultOnDemandDeps(
        llm=llm, substrate=substrate, judge_resolver=_resolver, pg_pool=_Pool(),
    )
    result = await run_method(
        [{"question": "What happened at Fairford?"}],
        {"analyst_id": "consult_default", "run_id": uuid4()}, deps,
    )
    data = result.consult_response.data
    assert data["verification"]["state"] == "judged"
    assert data["verification"]["score"] == 1.0
    usage = data["usage"]
    assert usage["input_tokens"] == 200 and usage["output_tokens"] == 100
    assert usage["model"] == "vllm-test" and usage["priced"] is False
    assert isinstance(usage["elapsed_s"], float)
    frames = [s for s in result.intermediate_steps if s.get("kind") == "judge_turn"]
    assert frames and frames[0]["state"] == "judged"
