# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Q-2 — the gaps Q-1 left in the consult turn contract, closed.

1. WINDOWS the parser missed: numeric day/month ranges (one stated rule for
   the ambiguous reading, printed), "from the 3rd to the 10th", early / mid /
   late <month> on fixed day bands, and EVENT ANCHORS — detected, never
   dated by guess: declared unresolved unless the anchor's own words carry a
   date. Precedence (words > console > default) unchanged.
2. The NUMBER check reads a cited row whole (it read the first 4,000
   characters, so a number a long row states later was flagged unsupported);
   a row past the bound is read to it and the accounting says how much was not.
3. ``synthesize_from`` (recovery) settles its OWN contract over the original
   turn's window / grounding / coverage, by the same functions; the front door
   answers with it, else with the original's stored contract (roll 81).
4. DEEP consult: the contract reads the asker's question (not the evidence
   brief appended to it), the settle-point judge runs (a stub judge here — no
   model), and the contract and verdict travel to the finding and the turn.

Fakes only: no model, no database, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import consult_contract as C
from legba.data.analysts import consult_grounding as G
from legba.data.analysts import consult_window as W
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method
from legba.data.registry import consult_persistence
from legba.data.registry import consult_turn_stamps as stamps
from legba.data.registry.consult_synthesis_api import build_recovery_input, recovered_contract

from .test_analyst_consult_on_demand import _ScriptedLLMHandler, _SubstrateStub
from .test_consult_c1_usage_judging import _judge_on, _StubJudge  # noqa: F401 — fixture

#: A Saturday, mid-month: fixed answers for every relative phrasing.
NOW = datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc)
_CONSOLE_24H = {"start": "2026-11-13T04:00Z", "end": "2026-11-14T04:00Z"}


# ---------------------------------------------------------------------------
# 1. Windows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("words,start,end,reading", [
    # Numeric: both readings valid -> day/month, and the other is named.
    ("What failed 3/11 to 10/11?", "2026-11-03T00:00", "2026-11-11T00:00",
     "read as day/month: 3 Nov 2026 to 10 Nov 2026 (month/day (11 Mar 2026 to 11 Oct 2026) was also valid"),
    ("between 3/11 and 10/11", "2026-11-03T00:00", "2026-11-11T00:00", "read as day/month"),
    ("11/3 to 11/10", "2026-03-11T00:00", "2026-10-12T00:00",
     "month/day (3 Nov 2026 to 10 Nov 2026) was also valid"),
    # Only one reading valid -> that one, said.
    ("13/11 to 14/11", "2026-11-13T00:00", "2026-11-14T04:00",
     "the month/day reading is not a valid past range"),
    ("since 11/13", "2026-11-13T00:00", "2026-11-14T04:00", "read as month/day: 13 Nov 2026"),
    ("from 03/11/2026 - 10/11/2026", "2026-11-03T00:00", "2026-11-11T00:00", "read as day/month"),
    ("3/11/26 to 10/11/26", "2026-11-03T00:00", "2026-11-11T00:00", "read as day/month"),
    ("28/12 to 3/1", "2025-12-28T00:00", "2026-01-04T00:00", "28 Dec 2025 to 3 Jan 2026"),
    ("3/11 to 20/11", "2026-11-03T00:00", "2026-11-14T04:00", "still under way, so the window ends now"),
    # Bare ordinals: this month (the 3rd has come).
    ("from the 3rd to the 10th", "2026-11-03T00:00", "2026-11-11T00:00", "read as this month"),
    # early / mid / late on the fixed bands.
    ("early November", "2026-11-01T00:00", "2026-11-11T00:00", "days 1-10"),
    ("mid-November", "2026-11-11T00:00", "2026-11-14T04:00", "days 11-20"),
    ("late October", "2026-10-21T00:00", "2026-11-01T00:00", "days 21-31"),
    ("late Oct 2025", "2025-10-21T00:00", "2025-11-01T00:00", "late October 2025"),
    ("early this month", "2026-11-01T00:00", "2026-11-11T00:00", "early November 2026"),
    ("late last month", "2026-10-21T00:00", "2026-11-01T00:00", "late October 2026"),
    ("early December", "2025-12-01T00:00", "2025-12-11T00:00", "early December 2025"),
    # An anchor whose own words carry a date is anchored on it.
    ("since the strike on 3 Nov", "2026-11-03T00:00", "2026-11-14T04:00",
     "anchored on the date your words give for the event, 2026-11-03"),
    ("after the outage on 3/11", "2026-11-03T00:00", "2026-11-14T04:00", "read as day/month"),
    ("before the election on 2026-11-03", "2026-10-27T00:00", "2026-11-03T00:00",
     "keeps the 168h default span before it"),
])
def test_q2_phrasings_resolve_with_a_stated_reading(words, start, end, reading):
    for console in (_CONSOLE_24H, None):  # the asker's words still beat the console
        w = W.resolve_window(words, console, now=NOW)
        assert w.source == W.SOURCE_USER_WORDS, (words, console)
        assert (w.start.isoformat()[:16], w.end.isoformat()[:16]) == (start, end), words
        assert w.reading and reading in w.reading, (words, w.reading)
        assert f"**Read as:** {w.reading}" in W.render_window_lines(w)
        assert w.as_dict()["reading"] == w.reading


@pytest.mark.parametrize("words,in_words", [
    ("3-10 November", "3-10 November"),
    ("Nov 3–10", "Nov 3–10"),
])
def test_q1_word_ranges_still_read_without_a_reading(words, in_words):
    w = W.resolve_window(words, None, now=NOW)
    assert w.source == W.SOURCE_USER_WORDS and w.basis == in_words and w.reading is None
    assert (w.start.isoformat()[:10], w.end.isoformat()[:10]) == ("2026-11-03", "2026-11-11")


@pytest.mark.parametrize("words", [
    "between 1/2 and 3/4 of sources",   # fractions, not dates
    "a 24/7 desk",                       # no range, no "since"
    "late this month",                   # a band still to come is not on the record
])
def test_numbers_and_future_bands_that_are_not_windows(words):
    assert W.resolve_window(words, _CONSOLE_24H, now=NOW).source == W.SOURCE_CONSOLE


@pytest.mark.parametrize("question,phrase,source", [
    ("What changed since the ceasefire?", "ceasefire", W.SOURCE_CONSOLE),
    ("after the summit, what moved?", "summit", W.SOURCE_CONSOLE),
    ("since the strike on the port at Odesa what changed", "strike on the port at Odesa", W.SOURCE_CONSOLE),
    ("following the announcement of new tariffs, what happened?", "announcement of new tariffs",
     W.SOURCE_CONSOLE),
    # A window stated in the same question stands; the anchor is still declared.
    ("since the ceasefire in the last 48 hours", "ceasefire", W.SOURCE_USER_WORDS),
    ("after the summit last week", "summit", W.SOURCE_USER_WORDS),
])
def test_an_event_anchor_is_declared_unresolved_and_never_dated(question, phrase, source):
    w = W.resolve_window(question, _CONSOLE_24H, now=NOW)
    assert w.source == source
    a = w.anchor
    assert a and a["phrase"] == phrase and a["resolved"] is False and "date" not in a
    lines = W.render_window_lines(w)
    assert "**Event-anchored window not resolved:**" in lines and a["words"] in lines
    if source == W.SOURCE_CONSOLE:
        assert (w.start.isoformat()[:16], w.end.isoformat()[:16]) == ("2026-11-13T04:00", "2026-11-14T04:00")
        assert "the console's window above is in force" in lines
    # The default applies when no console window came, with the same declaration.
    if source == W.SOURCE_CONSOLE:
        d = W.resolve_window(question, None, now=NOW)
        assert d.source == W.SOURCE_DEFAULT and d.anchor["resolved"] is False
        assert "the declared default above is in force" in W.render_window_lines(d)


def test_a_dated_anchor_still_to_come_is_not_used_and_its_date_never_becomes_a_window():
    w = W.resolve_window("What moved before the vote on 20 Nov?", None, now=NOW)
    assert w.source == W.SOURCE_DEFAULT  # not "on 20 Nov" as a one-day window last year
    assert w.anchor["resolved"] is False and "still to come" in w.anchor["reason"]


@pytest.mark.parametrize("words", [
    "since the start of the month", "after the weekend", "before the end of the window",
    "since the 3rd", "since Tuesday",
])
def test_time_phrases_are_not_event_anchors(words):
    assert W.resolve_window(words, None, now=NOW).anchor is None


def test_the_prompt_block_tells_the_model_the_reading_and_the_unresolved_anchor():
    for q, needle in (
        ("3/11 to 10/11", "were read as: read as day/month"),
        ("since the ceasefire", "did NOT resolve it to a date and you must not date the event yourself"),
    ):
        w = W.resolve_window(q, None, now=NOW)
        c = C.TurnContract(window=w, grounding_mode="default", grounding_source="default",
                           expectations=False, coverage={"state": "unavailable", "reason": "x"},
                           target_ids=[])
        assert needle in C._prompt_block(c)


def test_a_stored_window_round_trips_with_its_reading_and_anchor():
    w = W.resolve_window("since the ceasefire, 3/11 to 10/11", None, now=NOW)
    back = W.TurnWindow.from_dict(json.loads(json.dumps(w.as_dict())))
    assert back == w
    assert W.TurnWindow.from_dict({"start": "x", "end": "y", "source": "default"}) is None


# ---------------------------------------------------------------------------
# 2. The number check reads the whole cited row
# ---------------------------------------------------------------------------

REF = "33333333-3333-4333-8333-333333333333"


class _RowConn:
    def __init__(self, body: str) -> None:
        self.body = body

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        keys = [str(k) for k in (args[0] if args else [])]
        if sql.startswith("SELECT id, title, body, analyst_id FROM analyst_outputs"):
            return [{"id": REF, "title": "Grid output", "body": self.body, "analyst_id": "desk"}
                    for k in keys if k == REF]
        if sql.startswith("SELECT id FROM ") and "WHERE id = ANY" in sql:
            return [{"id": UUID(k)} for k in keys if k == REF]
        return []


class _RowPool:
    def __init__(self, body: str) -> None:
        self.body = body

    def acquire(self) -> "_RowPool":
        return self

    async def __aenter__(self) -> _RowConn:
        return _RowConn(self.body)

    async def __aexit__(self, *_a: Any) -> None:
        return None


_SENTENCE = f"The plant's output reached 1,234 megawatts on Tuesday [{REF}]."


async def _numbers(body: str) -> dict[str, Any]:
    _new, acct = await G.enforce(
        _SENTENCE, mode=G.GROUNDING_RECORD_ONLY, expectations=False,
        cited_refs=[REF], verification=None, pool=_RowPool(body),
    )
    return acct


@pytest.mark.asyncio
async def test_a_number_stated_past_4000_characters_of_a_row_is_supported():
    body = ("Filler about the grid. " * 400) + "Output reached 1,234 megawatts."
    assert len(body) > 4000
    acct = await _numbers(body)
    assert acct["numbers_unsupported"] == 0
    assert acct["number_check"] == {"rows": 1, "rows_clipped": 0, "chars_unchecked": 0,
                                    "row_chars_cap": G.NUMBER_CHECK_ROW_CHARS}
    # The same row WITHOUT the number: the check still flags it (not a no-op).
    assert (await _numbers("Filler about the grid. " * 400))["numbers_unsupported"] == 1


@pytest.mark.asyncio
async def test_a_row_past_the_bound_is_read_to_it_and_the_unread_share_is_said():
    filler = "x" * (G.NUMBER_CHECK_ROW_CHARS + 500)
    acct = await _numbers(filler + " Output reached 1,234 megawatts.")
    nc = acct["number_check"]
    assert nc["rows_clipped"] == 1 and nc["chars_unchecked"] > 500
    assert acct["numbers_unsupported"] == 1
    text = G.render_accounting(acct)
    assert "were read only to that length for the number check" in text
    assert f"{nc['chars_unchecked']:,} characters of them unchecked" in text


def test_the_judge_bridge_keeps_its_own_cap():
    import inspect

    from legba.runtime import actor_critic

    sig = inspect.signature(actor_critic._resolve_journal_citation_bridge)
    assert sig.parameters["text_chars"].default == actor_critic._JOURNAL_EVIDENCE_TEXT_CHARS == 4000


# ---------------------------------------------------------------------------
# 3. Recovery (synthesize_from) settles its own contract
# ---------------------------------------------------------------------------


from .test_consult_turn_contract import _Pool as _ContractPool  # noqa: E402

REF_R = "44444444-4444-4444-8444-444444444444"


def _original_contract() -> dict[str, Any]:
    w = W.resolve_window("since 3/11", None, now=NOW)
    return {
        "schema": "consult_contract/1", "armed": True, "window": w.as_dict(),
        "grounding_mode": "record_only", "grounding_source": "console",
        "coverage": {"state": "measured", "days": [], "zero_days": [], "sparse_days": [],
                     "gaps": {"paused_sources": [], "error_sources": []}},
        "cost_stop": "> **Stopped for cost:** …",
    }


async def _recover(original: dict[str, Any] | None, *, armed: bool = True) -> Any:
    from legba.data.analysts import consult_transcript

    answer = f"Output fell on the 5th [{REF_R}]. Typically outages recover within days."
    llm = _ScriptedLLMHandler([json.dumps({
        "final": True, "answer": answer, "uncertainty": 0.3,
        "cited_refs": [REF_R], "unanswered_aspects": [],
    })])
    pool = _ContractPool(rows={REF_R: "Output fell on the 5th."},
                         dates={REF_R: datetime(2026, 11, 5, tzinfo=timezone.utc)},
                         signal_days={})
    deps = ConsultOnDemandDeps(
        llm=llm, substrate=_SubstrateStub(signal_refs=[UUID(REF_R)]),
        turn_contract=armed, contract_pool=pool,
    )
    evidence = {
        "question": "What changed since 3/11?", "steps": [], "cited_refs": [REF_R],
        "turn_id": "t-0", "session_id": "s-0", "contract": original,
        "asked_at": NOW.isoformat(),
        "replay_transcript": consult_transcript.build_transcript(
            system="s", messages=[{"role": "user", "content": "q"}]),
    }
    first = build_recovery_input(evidence, llm_component_override=None, request_id="r-1")
    assert first["synthesize_from"]["contract"] == original
    result = await run_method([first], {"analyst_id": "consult_default"}, deps)
    return result.consult_response, llm


@pytest.mark.asyncio
async def test_a_recovered_answer_carries_its_own_contract_under_the_original_window():
    original = _original_contract()
    consult, _llm = await _recover(original)
    ct = consult.data["contract"]
    assert ct["schema"] == C.CONTRACT_SCHEMA and ct["armed"] is True
    assert ct["window"] == original["window"]                 # not re-resolved at recovery
    assert ct["grounding_mode"] == "record_only"
    assert ct["coverage"] == original["coverage"]
    assert ct["recovery"] == {"window_from": "original_turn", "coverage_from": "original_turn",
                              "original_turn_id": "t-0", "original_cost_stop": original["cost_stop"]}
    assert ct["reach"]["cited_dated"] == 1                   # the NEW answer's citations
    assert ct["grounding"]["applied"] is True                 # record-only over the NEW prose
    assert "Typically outages recover" not in consult.answer  # general filler cut
    assert consult.answer.startswith("> **Window in force:**")
    assert "> **Recovered answer:**" in consult.answer
    assert "stopped drilling for cost" in consult.answer
    # The front door answers with it (it carries ``recovery``), not the original's.
    assert recovered_contract(ct, {"contract": original}) == ct


@pytest.mark.asyncio
async def test_a_turn_that_stored_no_contract_re_reads_its_question_at_its_own_time():
    consult, _ = await _recover(None)
    ct = consult.data["contract"]
    assert ct["recovery"]["window_from"] == "question_at_original_turn"
    assert ct["recovery"]["coverage_from"] == "measured_at_recovery"
    assert ct["window"]["start"].startswith("2026-11-03") and "read as day/month" in ct["window"]["reading"]


@pytest.mark.asyncio
async def test_an_unarmed_original_or_unarmed_deps_recover_with_no_contract_of_their_own():
    consult, _ = await _recover({**_original_contract(), "armed": False})
    assert "contract" not in consult.data or not consult.data["contract"].get("armed")
    original = _original_contract()
    consult, _ = await _recover(original, armed=False)
    # Nothing produced: the front door falls back to the original's (roll 81).
    assert recovered_contract(consult.data.get("contract"), {"contract": original}) == original


@pytest.mark.asyncio
async def test_the_recovery_loader_carries_the_original_turns_instant():
    created = NOW - timedelta(days=2)

    class _Conn:
        async def fetchrow(self, *_a: Any) -> dict[str, Any]:
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "session_id": "00000000-0000-0000-0000-0000000000aa",
                "role": "assistant", "content": "cut", "steps": "[]", "tool_calls": "[]",
                "cited_refs": "[]", "finding_id": None, "request_id": "req-0",
                "synthesis_status": "partial", "replay_transcript": None,
                "usage": None, "response": None, "created_at": created,
            }

        async def fetchval(self, *_a: Any) -> str:
            return "q"

    class _P:
        def acquire(self) -> "_P":
            return self

        async def __aenter__(self) -> _Conn:
            return _Conn()

        async def __aexit__(self, *_a: Any) -> None:
            return None

    ev = await consult_persistence.load_turn_for_recovery(_P(), request_id="req-0")
    assert ev["asked_at"] == created.isoformat()
    sent = build_recovery_input(ev, llm_component_override=None, request_id="r")["synthesize_from"]
    assert sent["asked_at"] == created.isoformat() and sent["contract"] is None


# ---------------------------------------------------------------------------
# 4. Deep consult: the asker's question, the judge, the carry
# ---------------------------------------------------------------------------


class _DeepConn:
    """Answers the contract's reads and the judge's bridge, read-only."""

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        keys = [str(k) for k in (args[0] if args and isinstance(args[0], list) else [])]
        if sql.startswith("SELECT id, title, body, analyst_id FROM analyst_outputs"):
            return [{"id": REF_R, "title": "Grid", "body": "The grid lost two lines on the 5th.",
                     "analyst_id": "desk"} for k in keys if k == REF_R]
        if sql.startswith("SELECT id FROM ") and "WHERE id = ANY" in sql:
            return [{"id": UUID(k)} for k in keys if k == REF_R]
        return []

    async def fetchval(self, *_a: Any) -> Any:
        return None


class _DeepPool:
    def acquire(self) -> "_DeepPool":
        return self

    async def __aenter__(self) -> _DeepConn:
        return _DeepConn()

    async def __aexit__(self, *_a: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_deep_analyze_reads_the_askers_question_is_judged_and_carries_the_contract(
    _judge_on, monkeypatch,
):
    from legba.runtime.dapr_workflow import deep_consult as D

    from ..runtime.test_deep_consult_workflow import (
        _candidates_json, _input, _plan_json, _ScriptedLLM, _SubstrateStub as _DeepSub,
    )

    # The evidence brief carries a date and record-only words that are NOT the asker's.
    monkeypatch.setattr(D, "_evidence_brief", lambda *a, **k: (
        "row published since 2026-10-01; answer from the record only"))
    answer = f"The grid lost two lines on the 5th [{REF_R}]."
    llm = _ScriptedLLM([_plan_json(), json.dumps({
        "final": True, "answer": answer, "uncertainty": 0.3,
        "cited_refs": [REF_R], "unanswered_aspects": [],
    }), _candidates_json()])
    judge = _StubJudge(["supported"])

    async def _resolver(ref: str) -> Any:
        return judge

    deps = D.DeepConsultStageDeps(llm=llm, substrate=_DeepSub(signal_refs=[UUID(REF_R)]),
                                  pg_pool=_DeepPool(), judge_resolver=_resolver)
    plan = await D._run_plan(_input(question="What failed since the ceasefire?"), deps)
    analyzed = await D._run_analyze(await D._run_acquire(plan, deps), deps)

    ct = analyzed["contract"]
    # The window is the asker's (an unresolved anchor -> the default), never
    # the brief's "since 2026-10-01"; the mode is not the brief's "record only".
    assert ct["window"]["source"] == W.SOURCE_DEFAULT
    assert ct["window"]["anchor"]["phrase"] == "ceasefire" and ct["window"]["anchor"]["resolved"] is False
    assert ct["grounding_mode"] == G.GROUNDING_DEFAULT
    # Judged at the settle point (it was ``not_judged: no_judge_route``).
    ver = analyzed["verification"]
    assert ver["state"] == "judged" and judge.calls >= 1
    # The loop's own cited refs (the run collected none itself here: the
    # acquire stage did) — the set the verdict's digest was taken over.
    assert isinstance(analyzed["judged_refs"], list)
    # The verdict is about the answer the finding persists.
    checked = stamps.verification_stamp(ver, reason_if_absent="x", answer=analyzed["answer"],
                                        cited_refs=analyzed["judged_refs"])
    assert checked["state"] == "judged"
    json.dumps(analyzed)  # the workflow serialises the stage output


@pytest.mark.asyncio
async def test_deep_analyze_without_a_judge_route_still_says_so():
    from legba.runtime.dapr_workflow import deep_consult as D

    from ..runtime.test_deep_consult_workflow import (
        _candidates_json, _final_json, _input, _plan_json, _ScriptedLLM, _SubstrateStub as _DeepSub,
    )

    llm = _ScriptedLLM([_plan_json(), _final_json(), _candidates_json()])
    deps = D.DeepConsultStageDeps(llm=llm, substrate=_DeepSub())
    analyzed = await D._run_analyze(await D._run_acquire(await D._run_plan(_input(), deps), deps), deps)
    assert analyzed["verification"]["state"] in ("not_judged", "unassessable")


@pytest.mark.asyncio
async def test_the_deep_turn_stores_the_contract_and_the_checked_verdict():
    inserted: list[tuple[Any, ...]] = []

    class _Conn:
        async def fetchrow(self, sql: str, *args: Any) -> Any:
            if "FROM consult_sessions" in sql:
                return {"id": "00000000-0000-0000-0000-0000000000aa"}
            if "INSERT INTO consult_turns" in sql:
                inserted.append(args)
                return {"id": "t-1"}
            return None

        async def fetchval(self, *_a: Any) -> Any:
            return None

        async def execute(self, *_a: Any) -> str:
            return "UPDATE 1"

        def transaction(self) -> "_Conn":
            return self

        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _P:
        def acquire(self) -> _Conn:
            return _Conn()

    answer = "An answer."
    ver = {"schema": "consult_verification/1", "state": "judged", "score": 1.0,
           "settled_sha256": stamps.settle_digest(answer, [REF_R])}
    contract = {"schema": "consult_contract/1", "armed": True}
    await consult_persistence.record_deep_completion(
        _P(), task_id="deep.x.abcdef12", answer=answer, cited_refs=[REF_R, str(uuid4())],
        finding_id="f", contract=contract, verification=ver, judged_refs=[REF_R],
    )
    args = inserted[0]
    assert json.loads(args[12])["contract"] == contract
    assert json.loads(args[13])["state"] == "judged"
    # A verdict over other text is withdrawn, never stored as a verdict.
    inserted.clear()
    await consult_persistence.record_deep_completion(
        _P(), task_id="deep.x.abcdef12", answer="moved text", cited_refs=[REF_R],
        finding_id="f", contract=None, verification=ver, judged_refs=[REF_R],
    )
    assert json.loads(inserted[0][13])["state"] != "judged"
    assert inserted[0][12] is None
