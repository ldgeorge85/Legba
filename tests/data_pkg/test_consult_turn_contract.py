# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult TURN CONTRACT, on the real ``run_method`` path.

Stubbed model (scripted replies), stubbed substrate port, and a read-only
fake pool that answers the contract's own SQL (coverage by day, the cited
rows' dates, the reference-resolution gate, the citation bridge). Every
fixture here is SYNTHETIC — no row, sentence or id of the reviewed turn.

Rules covered: A (window in force + its source, narrowing detected from the
citation span against the coverage, the code-built coverage block), B (typed
explanations: render, sum/test checks, one repair, non-conforming), C/D
(record-only cuts and labels, the verifier list, the accounting block, the
chained verification digest), E (the per-turn cost ceiling stops drilling and
says so), and the U-6 ``on_screen`` context (window + point source, context
never evidence).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from legba.data import consult_on_screen as OS
from legba.data.analysts import consult_grounding as G
from legba.data.analysts import consult_hypotheses as H
from legba.data.analysts import consult_window as W
from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, FINAL_SENTINEL, run_method
from legba.data.analysts.consult_turn_cost import TurnCeilingGuard, arm_turn_ceiling
from legba.data.analysts.consult_spend_guard import SpendGuard
from legba.data.registry import consult_turn_stamps as ST

from .test_analyst_consult_on_demand import _ScriptedLLMHandler, _SubstrateStub

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# A read-only fake pool answering the contract's SQL
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, pool: "_Pool") -> None:
        self.pool = pool

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        p = self.pool
        p.sql.append(sql)
        if "count(DISTINCT source_id)" in sql:
            return [{"d": d, "n": n, "s": 5} for d, n in p.signal_days.items()]
        if "FROM analyst_outputs WHERE kind = 'finding'" in sql:
            return [{"d": d, "n": 3} for d in p.signal_days]
        if "FROM events WHERE time_start" in sql:
            return [{"d": d, "n": 1} for d in p.signal_days]
        if "state = 'paused'" in sql:
            return [{"descriptor_id": "src.paused_one", "name": "Paused one"}]
        if "outcome = 'error'" in sql:
            return [{"source_id": "src.flaky", "n": 4, "last": NOW}]
        if sql.startswith("SELECT id::text AS id, fetched_at AS at"):
            return [{"id": r, "at": at, "pub": None} for r, at in p.dates.items()
                    if r in {str(a) for a in args[0]}]
        if sql.startswith("SELECT id FROM ") and "WHERE id = ANY" in sql:
            # Any table the resolution gate probes (robust to its table order).
            return [{"id": u} for u in args[0] if str(u) in p.rows]
        if sql.startswith("SELECT id, payload, canonical_url FROM signals"):
            return [{"id": u, "payload": {"title": p.rows[str(u)]}, "canonical_url": "x"}
                    for u in args[0] if str(u) in p.rows]
        return []

    async def fetchval(self, *_a: Any) -> Any:
        return None


class _Pool:
    def __init__(self, rows: dict[str, str], dates: dict[str, datetime], signal_days: dict[Any, int]):
        self.rows, self.dates, self.signal_days, self.sql = rows, dates, signal_days, []

    def acquire(self) -> "_Pool":
        return self

    async def __aenter__(self) -> _Conn:
        return _Conn(self)

    async def __aexit__(self, *_a: Any) -> None:
        return None


def _final(answer: str, refs: list[str], block: dict[str, Any] | None) -> str:
    body = answer
    if block is not None:
        body += "\n\n```contract\n" + json.dumps(block) + "\n```\n"
    return f"{FINAL_SENTINEL}\nuncertainty: 0.3\ncited_refs: {', '.join(refs)}\n\n{body}"


_TOOL = json.dumps({"tool": "search_signals", "args": {"query": "q"}})


def _sub(*refs: str) -> _SubstrateStub:
    from uuid import UUID

    return _SubstrateStub(signal_rows=[{"id": r} for r in refs], signal_refs=[UUID(r) for r in refs])


def _days(n: int) -> dict[Any, int]:
    return {(NOW - timedelta(days=i)).date(): 400 for i in range(n)}


# ---------------------------------------------------------------------------
# A — the window in force
# ---------------------------------------------------------------------------


def test_window_comes_from_words_then_console_then_default():
    w = W.resolve_window("What happened in the last 48 hours?", {"hours": 24}, now=NOW)
    assert w.source == W.SOURCE_USER_WORDS and round(w.hours) == 48
    w = W.resolve_window("between 2026-11-01 and 2026-11-03, what failed?", None, now=NOW)
    assert (w.start.date().isoformat(), w.end.date().isoformat()) == ("2026-11-01", "2026-11-04")
    w = W.resolve_window("what happened for this window?", {"hours": 24}, now=NOW)
    assert w.source == W.SOURCE_CONSOLE and round(w.hours) == 24
    w = W.resolve_window("what happened for this window?", None, now=NOW)
    assert w.source == W.SOURCE_DEFAULT and w.deictic and round(w.hours) == 168
    assert "no console window reached this turn" in W.render_window_lines(w)
    # A comparison baseline and a forecast date are not windows.
    w = W.resolve_window("Compare with the prior six months; resolves by 2026-12-31.", None, now=NOW)
    assert w.source == W.SOURCE_DEFAULT


#: A Saturday, so the weekday phrasings have a fixed answer.
_SAT = datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc)
_CONSOLE_24H = {"start": "2026-11-13T04:00Z", "end": "2026-11-14T04:00Z"}


@pytest.mark.parametrize("words,start,end", [
    ("What failed from 3 Nov to 10 Nov?", "2026-11-03T00:00", "2026-11-11T00:00"),
    ("What has changed since Tuesday?", "2026-11-10T00:00", "2026-11-14T04:00"),
    ("Summarise the past fortnight.", "2026-10-31T04:00", "2026-11-14T04:00"),
    ("Anything in the last 36 hours?", "2026-11-12T16:00", "2026-11-14T04:00"),
    ("between 1 and 5 November", "2026-11-01T00:00", "2026-11-06T00:00"),
    ("November 3 to 10", "2026-11-03T00:00", "2026-11-11T00:00"),
    ("from 3-10 Nov", "2026-11-03T00:00", "2026-11-11T00:00"),
    ("since the 3rd", "2026-11-03T00:00", "2026-11-14T04:00"),
    ("the last couple of days", "2026-11-12T04:00", "2026-11-14T04:00"),
    ("over the weekend", "2026-11-07T00:00", "2026-11-09T00:00"),
    ("in October", "2026-10-01T00:00", "2026-11-01T00:00"),
    ("on 12 November", "2026-11-12T00:00", "2026-11-13T00:00"),
    ("previous 48 hours", "2026-11-12T04:00", "2026-11-14T04:00"),
])
def test_the_askers_words_always_win_over_the_console_and_the_default(words, start, end):
    for console in (_CONSOLE_24H, None):
        w = W.resolve_window(words, console, now=_SAT)
        assert w.source == W.SOURCE_USER_WORDS, (words, console)
        assert (w.start.isoformat()[:16], w.end.isoformat()[:16]) == (start, end), words


@pytest.mark.parametrize("words", [
    "in may cases the vendor says nothing",      # not the month
    "resolves by 30 November",                    # a forecast date, not a window
    "compare with the prior six months",          # a baseline, not the window
])
def test_words_that_are_not_windows_fall_back(words):
    assert W.resolve_window(words, _CONSOLE_24H, now=_SAT).source == W.SOURCE_CONSOLE


def test_bakeoff_arms_keep_their_frozen_hashes_and_the_live_prompt_one_vocabulary():
    from legba.data.analysts import consult_bakeoff_prompts as bp
    from legba.data.analysts import consult_system_prompt as sp

    # The pre-registered arm prompts are byte-identical to the bake-off's record.
    assert bp.prompt_sha256(bp.RAW_SYSTEM_PROMPT) == (
        "e9511378b841866ff971abccbb1540ab9907851e73c2c350afffbed2037aa745")
    assert bp.prompt_sha256(bp.VERIFIED_SYSTEM_PROMPT) == (
        "3ead8c38765f3bcd170a139dadce067fed99215167a7c3447977817b99879e54")
    for arm in (bp.RAW_SYSTEM_PROMPT, bp.VERIFIED_SYSTEM_PROMPT):
        assert sp.CONFIDENCE_FROZEN_BAKEOFF in arm and sp.CONFIDENCE_LIVE not in arm
    live = sp._SYSTEM_PROMPT
    assert sp.CONFIDENCE_LIVE in live and sp.CONFIDENCE_FROZEN_BAKEOFF not in live
    assert "likely, probable" not in live and "ICD-203" not in live
    # Every band the contract block renders is named once in the live prompt
    # (in the shared standards the numbers rule points back to).
    for _cut, band in H._BANDS:
        assert f"{band} (" in live, band
    assert live.count("roughly even chance (") == 1


def test_narrowing_is_detected_from_the_citation_span_and_the_coverage():
    w = W.resolve_window("in the last 7 days", None, now=NOW)
    held = {d.isoformat(): 10 for d in w.days(now=NOW)}
    reach = W.assess_reach(w, [NOW - timedelta(hours=3)], rows_by_day=held, now=NOW)
    assert reach.narrowed == "undeclared" and len(reach.uncited_edge_days) >= 5
    # The same span over a record that holds nothing earlier is sparse data, not a narrowing.
    sparse = {d: (10 if d == NOW.date().isoformat() else 0) for d in held}
    assert W.assess_reach(w, [NOW - timedelta(hours=3)], rows_by_day=sparse, now=NOW).narrowed is None
    declared = {"window_used": {"start": (NOW - timedelta(days=1)).date().isoformat(),
                                "end": NOW.date().isoformat(), "reason": "only the outage day matters"}}
    reach = W.assess_reach(w, [NOW - timedelta(hours=3)], rows_by_day=held, declared=declared, now=NOW)
    assert reach.narrowed == "declared" and "outage day" in W.render_window_lines(w, reach)
    reach = W.assess_reach(w, [NOW - timedelta(days=40)], rows_by_day=held, now=NOW)
    assert reach.wider_reach == "undeclared" and reach.before_window == 1


# ---------------------------------------------------------------------------
# The whole contract on the real run_method path
# ---------------------------------------------------------------------------


def _hyp_block(r1: str, r2: str) -> dict[str, Any]:
    by = (NOW + timedelta(days=30)).date().isoformat()
    return {
        "window_used": None, "wider_reach": None,
        "hypotheses": [
            {"explanation": "a configuration change at the operator caused the outage", "p": 0.6,
             "for": [r1[:8]], "against": [], "test": {"observable": "the operator's review names the change",
                                                     "by": by, "p_observable": 0.5}},
            {"explanation": "a hostile intrusion caused the outage", "p": 0.25,
             "for": [], "against": [r2[:8]], "test": {"observable": "a national CERT attributes it", "by": by}},
        ],
        "something_else_p": 0.15,
    }


@pytest.mark.asyncio
async def test_record_only_turn_end_to_end():
    r1, r2 = str(uuid4()), str(uuid4())
    pool = _Pool(
        rows={r1: "Grid operator reports 3 substations offline after update", r2: "Agency sees no sign of intrusion"},
        dates={r1: NOW - timedelta(hours=5), r2: NOW - timedelta(hours=4)},
        signal_days=_days(8),
    )
    substrate = _SubstrateStub(signal_rows=[{"id": r1}, {"id": r2}], signal_refs=[uuid4().__class__(r1), uuid4().__class__(r2)])
    answer = (
        "## Bottom line\n\n"
        f"The grid operator reported 3 substations offline after an update [{r1[:8]}]. "
        "The outage began at a regional control centre. "
        f"I assess the update as the likely trigger [{r1[:8]}]. "
        "This reads as an accident rather than an attack. "
        "Normally a hostile operation would be claimed within 72 hours. "
        "The map shows the affected region in red. "
        "The record holds no claim of responsibility.\n\n"
        f"| Event | Note |\n|---|---|\n| Outage | agency sees no intrusion [{r2[:8]}] |\n| Rumour | unconfirmed reports of sabotage |\n"
    )
    llm = _ScriptedLLMHandler([
        json.dumps({"tool": "search_signals", "args": {"query": "grid outage"}}),
        _final(answer, [r1, r2], _hyp_block(r1, r2)),
    ])
    deps = ConsultOnDemandDeps(llm=llm, substrate=substrate, turn_contract=True, contract_pool=pool)
    q = ("Answer from the record only; do not fill from general knowledge. "
         "What disrupted the grid in the last 7 days?")
    result = await run_method([{"question": q, "request_id": "req-1"}], {}, deps)
    out = result.consult_response
    text = out.answer
    data = out.data["contract"]

    # The model's first turn carried the contract block with the coverage.
    first_user = llm.calls[0]["messages"][-1]["content"]
    assert first_user.startswith("TURN CONTRACT") and "coverage by day" in first_user

    # A — window line first, sourced from the words; narrowing detected; coverage block.
    assert text.startswith("> **Window in force:**") and 'taken from your words: "last 7 days"' in text
    assert data["window"]["source"] == "user_words"
    assert data["reach"]["narrowed"] == "undeclared"
    assert "**Narrowed (not declared):**" in text
    assert "## Coverage by day" in text and "Known collection gaps: 1 source(s) paused" in text
    assert data["coverage"]["state"] == "measured"

    # B — explanations rendered from the typed block, no raw JSON left.
    assert "```contract" not in text
    assert "P = 0.60 (likely)" in text and "Something else: P = 0.15" in text
    assert "a different quantity from P above" in text
    assert data["hypotheses"]["conforming"] is True
    assert data["hypotheses"]["forecast_proposals"][0]["consult_number"]["p"] == 0.5

    # C — record only: cut / labelled / kept, never rewritten.
    assert "The outage began at a regional control centre." not in text            # fact, no row
    assert "Normally a hostile operation" not in text.split("## Not from the record")[0]
    assert "## Not from the record — general expectations" not in text            # not asked for
    assert "The map shows the affected region" not in text                         # screen ≠ evidence
    assert "| Rumour |" not in text                                                # table row, no row
    assert f"I assess the update as the likely trigger [{r1[:8]}]." in text        # supported inference
    assert "This reads as an accident rather than an attack." + G.INFERENCE_LABEL in text
    assert "The record holds no claim of responsibility." in text
    assert f"3 substations offline after an update [{r1[:8]}]" in text
    acct = data["grounding"]
    assert acct["applied"] and acct["counts"]["cut_no_row"] == 2
    assert acct["counts"]["cut_general_knowledge"] == 1
    assert acct["counts"]["cut_screen_as_evidence"] == 1
    assert acct["counts"]["labelled_inference"] == 1
    assert text.rstrip().splitlines()[-1].startswith("- The verifier did not judge this turn")
    assert "**Record accounting**" in text

    # D — the judge was not wired: said so, no enforcement claimed; digest chained.
    v = out.data["verification"]
    assert v["state"] == "not_judged" and v["grounding"]["applied"] is True
    assert v["grounding"]["output_sha256"] == ST.settle_digest(text, out.cited_substrate_refs)


@pytest.mark.asyncio
async def test_default_mode_cuts_nothing_but_accounts():
    r1 = str(uuid4())
    pool = _Pool(rows={r1: "x"}, dates={r1: NOW}, signal_days=_days(2))
    answer = f"A fact with a row [{r1[:8]}]. The plant closed in March."
    llm = _ScriptedLLMHandler([_TOOL, _final(answer, [r1], None)])
    deps = ConsultOnDemandDeps(llm=llm, substrate=_sub(r1), turn_contract=True, contract_pool=pool)
    result = await run_method([{"question": "what happened today?"}], {}, deps)
    text = result.consult_response.answer
    assert "The plant closed in March." in text and "**Record accounting**" not in text
    acct = result.consult_response.data["contract"]["grounding"]
    assert acct["mode"] == "default" and not acct["applied"] and acct["counts"]["cut_no_row"] == 1


@pytest.mark.asyncio
async def test_unarmed_deps_are_byte_identical():
    llm = _ScriptedLLMHandler([_final("Plain answer.", [], None)])
    result = await run_method([{"question": "q"}], {}, ConsultOnDemandDeps(llm=llm, substrate=_SubstrateStub()))
    assert result.consult_response.answer == "Plain answer."
    assert "contract" not in result.consult_response.data


# ---------------------------------------------------------------------------
# B — the hypothesis contract and its one repair
# ---------------------------------------------------------------------------


def test_hypothesis_checks_catch_proxy_probabilities_and_bad_sums():
    held = ["aaaaaaaa-0000-4000-8000-000000000000"]
    by = (NOW + timedelta(days=10)).date().isoformat()
    bad = {"hypotheses": [{"explanation": "no agency states a malicious cause by 2026-12-31",
                           "p": 0.75, "for": ["aaaaaaaa"], "against": [],
                           "test": {"observable": "x", "by": by}}], "something_else_p": 0.5}
    problems = H.check_hypotheses(bad, held_refs=held, now=NOW)
    assert any("dated observable" in p for p in problems)
    assert any("sum to 1.250" in p for p in problems)
    assert any("no 'would be confirmed" in p for p in H.check_hypotheses(
        {"hypotheses": [{"explanation": "e", "p": 1.0, "for": ["aaaaaaaa"]}], "something_else_p": 0},
        held_refs=held, now=NOW))
    assert H.likelihood_band(0.5) == "roughly even chance" and H.likelihood_band(0.97) == "almost certain"


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_fixes", [True, False])
async def test_one_bounded_repair_then_non_conforming(repair_fixes):
    r1, r2 = str(uuid4()), str(uuid4())
    pool = _Pool(rows={r1: "a", r2: "b"}, dates={r1: NOW, r2: NOW}, signal_days=_days(1))
    good = _hyp_block(r1, r2)
    bad = json.loads(json.dumps(good))
    bad["something_else_p"] = 0.6  # sums to 1.45
    repaired = "```contract\n" + json.dumps(good if repair_fixes else bad) + "\n```"
    llm = _ScriptedLLMHandler([_TOOL, _final(f"Answer [{r1[:8]}].", [r1, r2], bad), repaired, "unused"])
    deps = ConsultOnDemandDeps(llm=llm, substrate=_sub(r1, r2), turn_contract=True, contract_pool=pool)
    result = await run_method([{"question": "why today?"}], {}, deps)
    hyp = result.consult_response.data["contract"]["hypotheses"]
    assert len(llm.calls) == 3  # a tool round, the answer, exactly one repair
    assert hyp["repair"] == ("repaired" if repair_fixes else "still_failing")
    assert hyp["conforming"] is repair_fixes
    assert ("**Non-conforming:**" in result.consult_response.answer) is (not repair_fixes)


@pytest.mark.asyncio
async def test_repair_of_an_unparseable_block_shows_the_block_and_today():
    r1, r2 = str(uuid4()), str(uuid4())
    pool = _Pool(rows={r1: "a", r2: "b"}, dates={r1: NOW, r2: NOW}, signal_days=_days(1))
    # A trailing comma makes the block invalid JSON; its words must still
    # reach the repair call.
    broken = json.dumps(_hyp_block(r1, r2))[:-1] + ",}"
    answer = (f"{FINAL_SENTINEL}\nuncertainty: 0.3\ncited_refs: {r1}, {r2}\n\n"
              f"Answer [{r1[:8]}].\n\n```contract\n{broken}\n```\n")
    repaired = "```contract\n" + json.dumps(_hyp_block(r1, r2)) + "\n```"
    llm = _ScriptedLLMHandler([_TOOL, answer, repaired, "unused"])
    deps = ConsultOnDemandDeps(llm=llm, substrate=_sub(r1, r2), turn_contract=True, contract_pool=pool)
    await run_method([{"question": "why today?"}], {}, deps)
    assert len(llm.calls) == 3
    sent = llm.calls[2]["messages"][-1]["content"]
    assert "(unparseable)" not in sent
    assert "a configuration change at the operator caused the outage" in sent
    assert f"Today (UTC): {datetime.now(timezone.utc).date().isoformat()}" in sent


def test_repair_messages_show_the_raw_block_only_when_it_did_not_parse():
    _, msgs = H.repair_messages(None, "contract block is not JSON", ["x"], raw_block="{'p': 1,}")
    assert "{'p': 1,}" in msgs[0]["content"]
    _, msgs = H.repair_messages({"p": 1}, None, ["x"], raw_block="ignored")
    assert "ignored" not in msgs[0]["content"] and '"p": 1' in msgs[0]["content"]
    assert H.contract_block_text("a\n```contract\n{x}\n```\n") == "{x}"


# ---------------------------------------------------------------------------
# C / D — the verifier's list, strong and weak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("weak", [False, True])
async def test_verifier_list_cuts_on_a_strong_judge_and_labels_on_a_weak_one(weak):
    r1 = str(uuid4())
    pool = _Pool(rows={r1: "the plant restarted"}, dates={r1: NOW}, signal_days=_days(1))
    body = f"The plant restarted on Tuesday [{r1[:8]}]. The plant is owned by a foreign firm [{r1[:8]}]."
    stamp = {"state": "judged", "score": 0.5, "weak_judge": weak, "judge_model": "j",
             "cited_sentences": 2, "claims_judged": 2,
             "unsupported_claims": [{"text": f"The plant is owned by a foreign firm [{r1[:8]}].",
                                     "reason": "judge_unsupported"},
                                    {"text": "something the prose never said at all", "reason": "judge_unsupported"}]}
    new, acct = await G.enforce(body, mode=G.GROUNDING_RECORD_ONLY, expectations=False,
                                cited_refs=[r1], verification=stamp, pool=pool)
    assert "The plant restarted on Tuesday" in new
    if weak:
        assert "foreign firm" in new and G.WEAK_VERIFIER_LABEL in new
        assert acct["verifier"]["items"][0]["action"] == "labelled_weak_verifier"
    else:
        assert "foreign firm" not in new
        assert acct["verifier"]["items"][0]["action"] == "cut_verifier_unsupported"
    assert acct["verifier"]["items"][1]["action"] == "not_located_in_model_prose"
    block = G.render_accounting(acct)
    assert "2 listed unsupported" in block


def test_expectations_asked_for_are_moved_not_cut():
    assert G.expectations_requested("what would normally appear within 72 hours?")
    lines, units = G.split_units("- Normally a vendor posts an advisory first.\n- Plain.")
    assert [G.classify(u.text) for u in units] == ["general", "fact"]


def test_the_verdict_survives_only_a_chained_grounding_edit():
    judged, final = "Judged text [abc12345].", "Judged text [abc12345]."
    refs = ["abc12345-0000-4000-8000-000000000000"]
    stamp = {"state": "judged", "score": 0.9, "settled_sha256": ST.settle_digest("Judged text plus a cut sentence.", refs)}
    assert ST.verification_stamp(dict(stamp), reason_if_absent="x", answer=final, cited_refs=refs)["reason"] == "judged_text_changed"
    stamp["grounding"] = {"input_sha256": stamp["settled_sha256"], "output_sha256": ST.settle_digest(final, refs)}
    kept = ST.verification_stamp(dict(stamp), reason_if_absent="x", answer=final, cited_refs=refs)
    assert kept["state"] == "judged" and kept["score"] == 0.9
    del judged


# ---------------------------------------------------------------------------
# E — the per-turn cost ceiling
# ---------------------------------------------------------------------------


class _Price:
    input_per_m, output_per_m = 10.0, 50.0


class _PricedLLM(_ScriptedLLMHandler):
    subprovider = "anthropic"
    PRICE_TABLE = {"model-x": _Price()}

    class _cfg:  # noqa: N801 — the handler's config shape
        class model_name:  # noqa: N801
            raw = "model-x"


def test_ceiling_arms_only_on_a_priced_route_when_set(monkeypatch):
    monkeypatch.delenv("LEGBA_CONSULT_TURN_COST_CEILING_USD", raising=False)
    assert type(arm_turn_ceiling(SpendGuard(), _PricedLLM([]), max_output_tokens=1000)) is SpendGuard
    monkeypatch.setenv("LEGBA_CONSULT_TURN_COST_CEILING_USD", "5")
    assert isinstance(arm_turn_ceiling(SpendGuard(), _PricedLLM([]), max_output_tokens=1000), TurnCeilingGuard)
    assert type(arm_turn_ceiling(SpendGuard(), _ScriptedLLMHandler([]), max_output_tokens=1000)) is SpendGuard


@pytest.mark.asyncio
async def test_ceiling_is_soft_stops_drilling_synthesises_and_reports_the_total(monkeypatch):
    """$10, the recommended value, on a transcript shaped like turn e749e301:
    each round replays a prompt ~15k tokens bigger, the synthesis replays the
    whole of it and writes a long answer. Nothing is refused; the answer
    arrives, opens "Stopped for cost", and reports the total INCLUDING the
    synthesis, which stays under the ceiling because it was reserved for."""
    monkeypatch.setenv("LEGBA_CONSULT_TURN_COST_CEILING_USD", "10")

    class _Usage:
        def __init__(self, p: int, out: int) -> None:
            self.prompt_tokens, self.completion_tokens, self.reasoning_tokens = p, out, 0
            self.cost_estimate_usd = (p * 10 + out * 50) / 1e6

    class _Resp:
        def __init__(self, content: str, p: int, out: int) -> None:
            self.content, self.usage = content, _Usage(p, out)

    class _Growing(_PricedLLM):
        n = 0

        async def chat_complete(self, messages, **kw):  # noqa: ANN001
            self.calls.append({"messages": messages, **kw})
            if "The tool budget is used up" in str(messages[-1].get("content", "")):
                return _Resp(f"{FINAL_SENTINEL}\nuncertainty: 0.4\n\nSynthesised answer.",
                             1_000 + 15_000 * _Growing.n + 20_000, 20_000)
            _Growing.n += 1
            return _Resp(json.dumps({"tool": "search_signals", "args": {"query": "q"}}),
                         1_000 + 15_000 * (_Growing.n - 1), 600)

    llm = _Growing([])
    # The per-RUN ceilings as the live .env pins them (lifted), so only the
    # per-turn ceiling can stop this turn.
    deps = ConsultOnDemandDeps(llm=llm, substrate=_SubstrateStub(), max_tokens=32768, max_rounds=30,
                               max_input_tokens_per_run=100_000_000, max_cost_usd_per_run=1_000_000.0)
    result = await run_method([{"question": "q", "max_tool_rounds": 30}], {}, deps)
    out = result.consult_response
    stops = [s for s in result.intermediate_steps if s.get("kind") == "spend_ceiling_reached"]
    assert stops and stops[0]["reason"].startswith("the turn's cost ceiling ($10.00)")
    assert "projected synthesis" in stops[0]["reason"]
    assert stops[0]["round"] < 30
    # Soft: an answer, complete, synthesised by the forced final.
    assert out.data["synthesis_status"] == "complete"
    assert any(s.get("kind") == "forced_final" for s in result.intermediate_steps)
    assert "Synthesised answer." in out.answer
    usage = out.data["usage"]
    assert out.answer.startswith("> **Stopped for cost:**")
    assert f"Spent when drilling stopped: ${stops[0]['est_cost_usd']:.2f}" in out.answer
    assert f"the turn's total including the final synthesis: ${usage['est_cost_usd']:.2f}" in out.answer
    # ``round`` on the stop frame counts the rounds that ran; +1 is the synthesis.
    assert usage["calls"] == stops[0]["round"] + 1
    assert usage["est_cost_usd"] > stops[0]["est_cost_usd"] and usage["est_cost_usd"] < 10.0
    assert usage["turn_ceiling_usd"] == 10.0


def test_the_reviewed_turn_at_ten_dollars_stops_after_round_seven():
    """The arithmetic on turn e749e301's own per-round prompts (synthetic copy of
    the numbers only): at $10 the rule stops drilling after round 7."""
    rounds = [  # (prompt tokens of that round, est spent after it)
        (1031, 0.21), (20058, 0.44), (37089, 0.83), (52688, 1.40), (67389, 2.12),
        (82990, 2.99), (96676, 4.00), (109650, 5.14),
    ]
    stopped_after = None
    for i, (prompt, spent) in enumerate(rounds, start=1):
        g = TurnCeilingGuard(turn_ceiling_usd=10.0, input_per_m=10.0, output_per_m=50.0,
                             max_output_tokens=32768, last_prompt_tokens=prompt, cost_usd=spent)
        if g.exhausted_reason() is not None:
            stopped_after = i
            break
    assert stopped_after == 7
    g = TurnCeilingGuard(turn_ceiling_usd=10.0, input_per_m=10.0, output_per_m=50.0,
                         max_output_tokens=32768, last_prompt_tokens=96676, cost_usd=4.00)
    assert round(g.next_call_usd(), 2) == 3.09 and round(g.synthesis_usd(), 2) == 3.81


# ---------------------------------------------------------------------------
# on_screen: declared, capped, context not evidence
# ---------------------------------------------------------------------------

_SCREEN = {
    "v": 1, "page": "board", "side": "reader",
    "point": {"kind": "target", "id": "subject_a", "label": "Subject A"},
    "window": {"span": "24h", "from": (NOW - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%MZ"),
               "to": NOW.strftime("%Y-%m-%dT%H:%MZ")},
    "views": [{"id": "v4.morning_read", "at": "stage", "front": True},
              {"id": "v4.map", "at": "p1", "front": True},
              {"id": "subject.page:subject_a", "at": "p2", "front": False}],
    "chat": {"placement": "bottom", "collapsed": False},
}


def test_on_screen_model_caps_and_block_is_small():
    model = OS.OnScreen.model_validate(_SCREEN)
    assert model.wire()["window"]["from"] == _SCREEN["window"]["from"]
    big = dict(_SCREEN, views=[{"id": "v" * 120, "at": "p1", "front": True}] * 12)
    with pytest.raises(ValueError):
        OS.OnScreen.model_validate(big)
    with pytest.raises(ValueError):
        OS.OnScreen.model_validate(dict(_SCREEN, views=_SCREEN["views"] * 5))
    block = OS.render_on_screen_block(model.wire())
    assert "never evidence" in block and "point: target subject_a" in block
    assert len(block) // 4 < 130  # ~tokens for a three-view frame


@pytest.mark.asyncio
async def test_on_screen_sets_the_console_window_the_point_and_rides_the_turn():
    r1 = str(uuid4())
    pool = _Pool(rows={r1: "x"}, dates={r1: NOW - timedelta(hours=2)}, signal_days=_days(2))
    llm = _ScriptedLLMHandler([_TOOL, _final(f"Subject A had one event [{r1[:8]}].", [r1], None)])
    deps = ConsultOnDemandDeps(llm=llm, substrate=_sub(r1), turn_contract=True, contract_pool=pool)
    result = await run_method([{"question": "what happened here in this window?", "on_screen": _SCREEN}], {}, deps)
    text = result.consult_response.answer
    data = result.consult_response.data["contract"]
    assert data["window"]["source"] == "console" and round(data["window"]["hours"]) == 24
    assert '**Point (from the console):** "Subject A" (target subject_a).' in text
    assert data["on_screen"]["page"] == "board"
    assert "ON SCREEN (" in llm.calls[0]["messages"][-1]["content"]
    # The point's target narrows the coverage's findings/events.
    assert data["coverage"]["subject_filter"] == {"target_ids": ["subject_a"]}
