# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/forecast_arms.py``: one packet for every arm, the paid
gate and the ceiling, the tower's honest absence, nothing spent on a dry run.

Fakes only: a fake Messages call (usage + a JSON reply), a fake decide call,
a fake tower read, and the store fake — no network, no money, no DB.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from legba.data import forecast_ledger as fl

from ._forecast_ledger_fakes import FakeLedgerConn

_REPO = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)


def _arms():
    spec = importlib.util.spec_from_file_location("forecast_arms", _REPO / "scripts" / "forecast_arms.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["forecast_arms"] = mod
    spec.loader.exec_module(mod)
    return mod


fa = _arms()


def _prices():
    return fa.common.Prices(model_name="claude-opus-5-5", input=4.0, output=20.0, cache_read=0.2,
                            cache_write=5.0, web_search_per_1k=None, verified_by="test")


def _brief(desk: str) -> dict:
    if desk == "country_broken":
        raise RuntimeError("HTTP 500")
    return {"desk": desk, "export": {"items": [{
        "id": f"finding-{desk}", "title": f"{desk} read",
        "body": f"The {desk} desk says something [1].",
        "citations": [{"marker": "[1]", "signal_id": "00000000-0000-0000-0000-000000000001"}],
    }]}}


class _Calls:
    def __init__(self, p: float = 0.42, words: int = 30):
        self.bodies: list[dict] = []
        self.p = p
        self.words = words

    async def messages(self, body):
        self.bodies.append(body)
        text = json.dumps({"probability": self.p, "rationale": " ".join(["word"] * self.words)})
        return {"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 20000, "output_tokens": 5000}}

    async def decide(self, state, questions):
        self.bodies.append({"state": state, "questions": questions})
        return {"resolves_yes": {"type": "choice", "choice": "no", "confidence": 0.7,
                                 "probabilities": {"yes": 0.3, "no": 0.7}}}


class _Tower:
    def __init__(self, row=None):
        self.row = row

    async def fetchrow(self, sql, *a):
        assert sql == fa.TOWER_SQL
        return self.row


async def _rows(conn: FakeLedgerConn, *, placeholder: bool = False) -> list[dict]:
    for slug, desks, due in (("FX1-a", ["country_aa", "country_broken"], "2026-12-15"),
                             ("FX2-b", ["country_bb"], "2026-12-31")):
        await fl.upsert_question(conn, fl.ForecastQuestion(
            slug=slug, question=f"Fictional question {slug}", resolver_kind="official_result",
            resolver_spec={"authority": "Fictional CEC", "outcome": "X wins"},
            due_at=datetime.fromisoformat(due + "T23:59:59+00:00"),
            subject_keys=tuple(desks),
            condition="<OPERATOR TO WRITE>" if (placeholder and slug == "FX1-a") else None,
        ))
    return await fl.fetch_ledger(conn, now=AS_OF)


def _frontier(calls: _Calls | None, refusal: str | None = None):
    return fa.Frontier(model="claude-opus-5-5", max_tokens=8192, version="v1", prices=_prices(),
                       call=calls.messages if calls else None, refusal=refusal)


def _wiring(conn, rows, calls=None, tower=None, frontier=None):
    return fa.Wiring(rows=rows, brief=_brief, write_conn=conn, read_conn=tower or _Tower(),
                     frontier=frontier, decide=calls.decide if calls else None,
                     decision_model="decision-model", decision_version="v1")


async def test_dry_run_prints_the_packets_calls_nothing_writes_nothing(tmp_path):
    conn = FakeLedgerConn()
    rows = await _rows(conn)
    calls = _Calls()
    printed: list[str] = []
    out = await fa.run_arms(
        _wiring(conn, rows, calls, frontier=_frontier(calls, refusal="dry_run: nothing is called")),
        arms=list(fa.ARMS), as_of=AS_OF, out=tmp_path / "arms", dry_run=True, ceiling=None,
        echo=printed.append)
    assert calls.bodies == []
    assert not (tmp_path / "arms").exists()
    assert conn.numbers == {}
    assert len(printed) == 2 and "country_aa desk says something (signal:" in printed[0]
    assert "no_read: country_broken (RuntimeError)" in printed[0]
    fx1 = out["questions"][0]["arms"]
    assert fx1["frontier_arm"]["status"] == "dry_run" and fx1["frontier_arm"]["worst_case_usd"] > 0
    assert fx1["clef_arm"]["status"] == "dry_run"


async def test_the_paid_arm_refuses_without_a_ceiling_even_when_wired(tmp_path):
    conn = FakeLedgerConn()
    rows = await _rows(conn)
    calls = _Calls()
    out = await fa.run_arms(_wiring(conn, rows, calls, frontier=_frontier(calls)),
                            arms=["frontier_arm"], as_of=AS_OF, out=tmp_path, dry_run=False, ceiling=None)
    assert calls.bodies == []
    assert all(q["arms"]["frontier_arm"]["refusal"].startswith("dry_run: no operator --ceiling-usd")
               for q in out["questions"])
    assert conn.numbers == {}


async def test_execute_states_the_number_records_cost_and_stops_at_the_ceiling(tmp_path):
    conn = FakeLedgerConn()
    rows = await _rows(conn)
    calls = _Calls()
    fr = _frontier(calls)
    out = await fa.run_arms(_wiring(conn, rows, calls, frontier=fr), arms=["frontier_arm"], as_of=AS_OF,
                            out=tmp_path, dry_run=False, ceiling=0.25)
    assert len(calls.bodies) == 1
    body = calls.bodies[0]
    assert body["max_tokens"] == 8192 and "temperature" not in body
    first, second = out["questions"]
    assert first["arms"]["frontier_arm"]["status"] == "answered"
    assert first["arms"]["frontier_arm"]["usd"] == pytest.approx((20000 * 4 + 5000 * 20) / 1e6)
    assert second["arms"]["frontier_arm"]["status"] == "refused"
    assert second["arms"]["frontier_arm"]["refusal"].startswith("run_ceiling")
    assert out["refused_at_ceiling"] == ["FX2-b"]
    (n,) = conn.numbers.values()
    assert n["issuer_kind"] == "frontier_arm" and float(n["p"]) == 0.42
    assert n["rationale_ref"].endswith("FX1-a__frontier_arm.json")
    rec = json.loads((tmp_path / "FX1-a__frontier_arm.json").read_text())
    assert rec["usage"]["input_tokens"] == 20000 and rec["cost"]["usd"] > 0
    assert rec["prompt_sha256"] == fa.PROMPT_SHA256
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["spent_usd"] == pytest.approx(rec["cost"]["usd"])
    # A re-run never re-pays an answered record.
    await fa.run_arms(_wiring(conn, rows, calls, frontier=fr), arms=["frontier_arm"], as_of=AS_OF,
                      out=tmp_path, dry_run=False, ceiling=0.25)
    assert len(calls.bodies) == 1


async def test_every_arm_reads_the_same_packet_and_the_tower_is_an_honest_absence(tmp_path):
    conn = FakeLedgerConn()
    rows = await _rows(conn)
    calls = _Calls(words=200)
    await fa.run_arms(_wiring(conn, rows, calls, frontier=_frontier(calls)), arms=list(fa.ARMS),
                      as_of=AS_OF, out=tmp_path, dry_run=False, ceiling=10.0, slugs=["FX1-a"])
    recs = {arm: json.loads((tmp_path / f"FX1-a__{arm}.json").read_text()) for arm in fa.ARMS}
    shas = {r["packet"]["packet_sha256"] for r in recs.values()}
    assert len(shas) == 1
    # The decision model was handed the packet as its state; the frontier the same packet inside the prompt.
    decide_state = next(b["state"] for b in calls.bodies if "state" in b)
    frontier_prompt = next(b["messages"][0]["content"] for b in calls.bodies if "messages" in b)
    assert decide_state in frontier_prompt
    by_kind = {n["issuer_kind"]: n for n in conn.numbers.values()}
    assert float(by_kind["clef_arm"]["p"]) == 0.3 and by_kind["clef_arm"]["rationale"] is None
    assert by_kind["tower"]["p"] is None
    assert "no scheduled analyst emitted a probability for FX1-a" in by_kind["tower"]["absent_reason"]
    assert len(by_kind["frontier_arm"]["rationale"].split()) == fa.RATIONALE_WORDS
    assert "rationale cut" in recs["frontier_arm"]["problem"]


async def test_a_number_stated_under_a_retired_component_name_is_restated_in_place(tmp_path, monkeypatch):
    from legba.data.schemas import legacy_name_digests as L

    retired = "llm.decide.retired_vendor_choice"
    monkeypatch.setitem(L.LEGACY_COMPONENT_ID_DIGESTS, L.name_digest(retired), fa.DECISION_COMPONENT)
    conn = FakeLedgerConn()
    rows = await _rows(conn)
    qid = next(r["id"] for r in rows if r["slug"] == "FX1-a")
    await fl.state_number(conn, qid, fl.ForecastNumber("clef_arm", retired, 0.6, AS_OF))
    await fa.run_arms(_wiring(conn, rows, _Calls()), arms=["clef_arm"], as_of=AS_OF, out=tmp_path,
                      dry_run=False, ceiling=None, slugs=["FX1-a"])
    (n,) = [n for n in conn.numbers.values() if n["issuer_kind"] == "clef_arm"]
    assert n["issuer_ref"] == retired and float(n["p"]) == 0.3
    # an unrelated ref is never adopted: a fresh question gets the current id
    await fa.run_arms(_wiring(conn, rows, _Calls()), arms=["clef_arm"], as_of=AS_OF,
                      out=tmp_path / "b", dry_run=False, ceiling=None, slugs=["FX2-b"])
    refs = sorted(n["issuer_ref"] for n in conn.numbers.values() if n["issuer_kind"] == "clef_arm")
    assert refs == sorted([retired, fa.DECISION_COMPONENT])


async def test_a_tower_number_is_taken_when_an_analyst_emitted_one(tmp_path):
    conn = FakeLedgerConn()
    rows = await _rows(conn)
    tower = _Tower({"id": "out-1", "analyst_id": "country_assessment", "produced_at": AS_OF, "p": 0.33})
    await fa.run_arms(_wiring(conn, rows, tower=tower), arms=["tower"], as_of=AS_OF, out=tmp_path,
                      dry_run=False, ceiling=None, slugs=["FX2-b"])
    (n,) = conn.numbers.values()
    assert n["issuer_kind"] == "tower" and float(n["p"]) == 0.33


async def test_a_question_whose_wording_is_not_final_is_skipped(tmp_path):
    conn = FakeLedgerConn()
    rows = await _rows(conn, placeholder=True)
    calls = _Calls()
    out = await fa.run_arms(_wiring(conn, rows, calls), arms=["clef_arm"], as_of=AS_OF, out=tmp_path,
                            dry_run=False, ceiling=None)
    assert out["skipped"] == ["FX1-a: wording not final (condition)"]
    assert [q["slug"] for q in out["questions"]] == ["FX2-b"]


async def test_one_run_one_as_of(tmp_path):
    fa.Run.open(tmp_path, AS_OF)
    with pytest.raises(RuntimeError):
        fa.Run.open(tmp_path, AS_OF.replace(hour=21))


def test_the_harness_prices_are_imported_not_copied():
    assert fa.common.__file__.endswith("scripts/bakeoff/common.py")
    assert fa.FRONTIER_COMPONENT == "llm.anthropic.opus_4_7"
    assert "max_tokens" not in fa.FORECAST_PROMPT


def test_reply_parsing():
    assert fa.parse_frontier_reply('{"probability": 0.2, "rationale": "x"}')[:2] == (0.2, "x")
    assert fa.parse_frontier_reply("no json")[0] is None
    assert fa.parse_frontier_reply('{"probability": 1.5}')[0] is None
    assert fa.decision_p({"choice": "yes", "confidence": 0.8}) == 0.8
    assert fa.decision_p({"choice": "no", "confidence": 0.8}) == pytest.approx(0.2)
