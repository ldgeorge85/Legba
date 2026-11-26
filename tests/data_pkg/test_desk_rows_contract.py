# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desk output contract v2: parse, gates, the repair round, the
renderer and its lead check, the slice scope and the computed block, and one
whole shadow run through a scripted model. No I/O; no table."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from legba.data import desk_rows_computed as comp
from legba.data.desk_rows_contract import (
    CONTRACT_VERSION,
    normalize_confidence,
    parse_repairs,
    parse_rows_reply,
)
from legba.data.desk_rows_gates import (
    ABSENCE,
    ASSESSMENT,
    ESTABLISHED_ELIGIBLE,
    FAILED,
    RESTATEMENT,
    GateContext,
    apply_repairs,
    build_repair_prompt,
    gate_reply,
    tally,
)
from legba.data.desk_rows_render import check_lead, render_read
from legba.data.desk_rows_shadow import ShadowInput, ShadowPrompts, run_contract_v2
from legba.data.statement_typing import SubjectCandidate

SIG1 = "00000000-0000-4000-8000-0000000000d1"
SIG2 = "00000000-0000-4000-8000-0000000000d2"
SIG3 = "00000000-0000-4000-8000-0000000000d3"
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)

TEXT1 = ("The Druzhba pipeline was halted on 3 November after a drone strike near Unecha, "
         "the operator Transneft said on Monday. Flows of 250,000 barrels per day stopped.")
TEXT2 = "Hungary's foreign minister Peter Szijjarto said supplies to Hungary would resume within days."
TEXT3 = "Oil prices rose 3% on Tuesday as traders weighed supply risks in Ruritania and beyond."


def _row(sid: str, source: str, title: str, text: str) -> dict[str, Any]:
    return {"id": sid, "source_id": source, "title": title, "fetched_at": T0,
            "data": {"title": title, "archived_text": text, "published_at": "2026-11-03T10:00:00+00:00"}}


SLICE = [
    _row(SIG1, "source.wire.a", "Druzhba pipeline halted after drone strike", TEXT1),
    _row(SIG2, "source.wire.b", "Szijjarto says supplies will resume", TEXT2),
    _row(SIG3, "source.wire.a", "Oil prices rise on supply risk", TEXT3),
]
PRIOR_ORDINAL = 4
CITATION_INDEX = {1: {"signal_id": SIG1}, 2: {"signal_id": SIG2}, 3: {"signal_id": SIG3},
                  PRIOR_ORDINAL: {"_citation": {"ref_kind": "prior_read", "ref_id": "p1"}}}
SCOPE = comp.slice_scope(SLICE, [SIG1, SIG2, SIG3], collection="desk_ru",
                         coverage={"window_start": "2026-11-02T00:00:00+00:00",
                                   "window_end": "2026-11-03T12:00:00+00:00"}, window_hours=None)


def _ctx(**over: Any) -> GateContext:
    evidence = comp.slice_evidence(SLICE)
    subjects = [
        SubjectCandidate("target", "desk_ru", ["Druzhba", "Russia"]),
        SubjectCandidate("entity", "e-hu", ["Hungary", "Peter Szijjarto"], entity_id="e-hu",
                         canonical_name="Peter Szijjarto", entity_type="person"),
    ]
    base = dict(evidence=evidence, block_kinds={PRIOR_ORDINAL: "prior_read"}, subjects=subjects,
                actor_links={"e-hu": frozenset({SIG2})}, desk_subject_count=1,
                handed=comp.handed_slice(SCOPE, "run-1"), target_id="desk_ru", read_at=T0,
                computed=comp.computed_block(SLICE, SCOPE, evidence=evidence))
    base.update(over)
    return GateContext(**base)


GOOD_FACT = {"id": "F1", "text": "The Druzhba pipeline was halted on 3 November after a drone strike.",
             "kind": "event", "subject": "S1", "valid_from": "2026-11-03", "valid_from_precision": "day",
             "cites": [{"row": 1, "quote": "The Druzhba pipeline was halted on 3 November after a drone strike"}]}


def _reply(**rows: Any) -> Any:
    return parse_rows_reply({"title": "t", "severity": "severity:high", "confidence": 0.6, **rows})


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_reads_rows_and_names_defects():
    r = parse_rows_reply({
        "fact_rows": [{"id": "x", "text": "a"}, {"id": "F1", "text": "b", "cites": "not a list"}],
        "assessment_rows": [{"id": "J1", "text": "j", "confidence": "Highly likely", "rests_on": ["x"]}],
        "severity": "severity:elevated", "severity_delta": "rose", "confidence": 2,
    })
    assert [f.id for f in r.fact_rows] == ["F2", "F1"]          # reassigned past the model's taken id
    assert r.fact_rows[0].defects == ("id_reassigned",) and r.fact_rows[0].model_id == "X"
    assert "cites_not_a_list" in r.fact_rows[1].defects
    assert r.assessment_rows[0].rests_on == ("F2",)              # resolved through the model's own id
    assert r.assessment_rows[0].confidence == "very likely"
    assert r.severity == "elevated" and r.severity_delta == "rose"
    assert r.confidence is None and "confidence_out_of_range" in r.defects
    assert parse_rows_reply(None) is None and parse_rows_reply([]) is None  # type: ignore[arg-type]
    assert normalize_confidence("roughly even") == "roughly even chance"
    assert normalize_confidence("certain-ish") is None
    assert CONTRACT_VERSION == "desk_rows.v2.1"


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def _outcomes(**rows: Any) -> dict[str, Any]:
    return {o.row_id: o for o in gate_reply(_reply(**rows), _ctx())}


def test_a_clean_fact_row_is_established_eligible():
    out = _outcomes(fact_rows=[GOOD_FACT])
    assert out["F1"].outcome == ESTABLISHED_ELIGIBLE, out["F1"].reasons
    d = out["F1"].result.draft
    assert (d.subject_key, str(d.kind)) == ("desk_ru", "event")


@pytest.mark.parametrize("change,reason", [
    ({"text": "Transneft halted 250,000 barrels per day on 3 November after 2 strikes."}, "more_than_one_figure"),
    ({"text": "The pipeline stopped. Flows ended."}, "not_one_sentence"),
    ({"kind": "coverage"}, "collection_fact_in_rows"),
    ({"kind": "assessment"}, "kind_not_fact"),
    ({"cites": []}, "uncited"),
    ({"cites": [{"row": 99, "quote": "anything at all here"}]}, "cite_row_not_handed"),
    ({"cites": [{"row": PRIOR_ORDINAL, "quote": "the prior read said so"}]}, "cites_no_evidence_row"),
    ({"cites": [{"row": 1, "quote": "the pipeline was bombed by unknown forces"}]}, "G1.quote:quote_absent"),
    ({"subject": "S9"}, "subject_not_offered"),
    ({"subject": "S2"}, "subject_not_linked_to_cited_rows"),
])
def test_a_fact_row_fails_with_its_named_reason(change, reason):
    out = _outcomes(fact_rows=[{**GOOD_FACT, **change}])
    assert out["F1"].outcome == FAILED
    assert reason in out["F1"].reasons, out["F1"].reasons


def test_a_level_rows_one_figure_is_its_value_and_is_checked():
    level = {"id": "F1", "text": "Druzhba flows of 250,000 barrels per day stopped.", "kind": "level",
             "subject": "S1", "valid_from": None, "valid_from_precision": "unknown",
             "cites": [{"row": 1, "quote": "Flows of 250,000 barrels per day stopped"}]}
    out = _outcomes(fact_rows=[level])["F1"]
    assert out.outcome == ESTABLISHED_ELIGIBLE, out.reasons
    assert out.result.draft.slots["value"] == 250000
    wrong = _outcomes(fact_rows=[{**level, "text": "Druzhba flows of 260,000 barrels per day stopped."}])["F1"]
    assert "G2.numbers:number_not_in_evidence" in wrong.reasons


def test_an_actor_subject_linked_to_the_cited_row_passes():
    fact = {"id": "F1", "text": "Peter Szijjarto said supplies to Hungary would resume within days.",
            "kind": "attributed", "subject": "S2", "valid_from": None, "valid_from_precision": "unknown",
            "cites": [{"row": 2, "quote": "Peter Szijjarto said supplies to Hungary would resume within days"}]}
    out = _outcomes(fact_rows=[fact])
    assert out["F1"].outcome == ESTABLISHED_ELIGIBLE, out["F1"].reasons
    assert out["F1"].result.draft.subject_kind == "entity"


def test_an_absence_is_written_and_scoped_by_code():
    out = _outcomes(absence_rows=[{"id": "A1", "topic": "a declared energy emergency"}])
    a = out["A1"]
    assert a.outcome == ABSENCE, a.reasons
    assert a.text.startswith("No row in this desk's collection (3 signals from 2 sources")
    assert a.detail["scope"]["window_start"].startswith("2026-11-02") and a.detail["scope"]["collection"] == "desk_ru"
    bad = _outcomes(absence_rows=[{"id": "A1", "topic": "more than 3 tankers [2]"}])["A1"]
    assert bad.outcome == FAILED and {"absence_cites_rows", "absence_topic_has_figure"} <= set(bad.reasons)
    unscoped = gate_reply(_reply(absence_rows=[{"id": "A1", "topic": "a strike"}]),
                          _ctx(handed=comp.handed_slice({"row_ids": [SIG1]}, "r")))
    assert "G8.absence_scope:absence_scope_unknown" in unscoped[0].reasons


def test_an_assessment_rests_only_on_passed_fact_rows():
    bad_fact = {**GOOD_FACT, "id": "F2", "cites": [{"row": 1, "quote": "not in the text at all, no"}]}
    out = _outcomes(fact_rows=[GOOD_FACT, bad_fact], assessment_rows=[
        {"id": "J1", "text": "Supply to central Europe is at risk.", "confidence": "likely", "rests_on": ["F1"]},
        {"id": "J2", "text": "Supply is at risk.", "confidence": "likely", "rests_on": ["F2"]},
        {"id": "J3", "text": "Supply will fall by 40 percent.", "confidence": "likely", "rests_on": ["F1"]},
        {"id": "J4", "text": "Supply is at risk.", "confidence": "fairly sure", "rests_on": ["F1", "F7"]},
    ])
    assert out["J1"].outcome == ASSESSMENT, out["J1"].reasons
    assert "rests_on_no_eligible_fact" in out["J2"].reasons
    assert "assessment_new_figure" in out["J3"].reasons
    assert {"confidence_word_unknown", "premise_unknown"} <= set(out["J4"].reasons)


def test_a_restatement_names_a_prior_read_and_passed_rows():
    out = _outcomes(fact_rows=[GOOD_FACT], restatements=[
        {"id": "R1", "ref": PRIOR_ORDINAL, "status": "confirmed", "text": "Druzhba flows were disrupted.",
         "bearing": ["F1"]},
        {"id": "R2", "ref": 1, "status": "revised", "text": "x", "bearing": ["F1"]},
        {"id": "R3", "ref": PRIOR_ORDINAL, "status": "revised", "text": "x", "bearing": []},
    ])
    assert out["R1"].outcome == RESTATEMENT, out["R1"].reasons
    assert "ref_not_prior_read" in out["R2"].reasons
    assert "restatement_unbacked" in out["R3"].reasons


# ---------------------------------------------------------------------------
# the repair round
# ---------------------------------------------------------------------------


def test_the_repair_prompt_shows_only_failed_rows_with_plain_reasons():
    reply = _reply(fact_rows=[GOOD_FACT, {**GOOD_FACT, "id": "F2", "cites": [{"row": 1, "quote": "a drone hit it hard"}]}])
    outcomes = gate_reply(reply, _ctx())
    prompt = build_repair_prompt(reply, outcomes, _ctx())
    assert '"id": "F2"' in prompt and '"id": "F1"' not in prompt
    assert "word for word" in prompt and "ROW [1]" in prompt
    assert build_repair_prompt(_reply(fact_rows=[GOOD_FACT]), gate_reply(_reply(fact_rows=[GOOD_FACT]), _ctx()),
                               _ctx()) is None


def test_a_repair_may_fix_narrow_or_withdraw_but_never_add():
    broken = {**GOOD_FACT, "id": "F2", "cites": [{"row": 1, "quote": "a drone hit it hard"}]}
    reply = _reply(fact_rows=[broken, {**GOOD_FACT, "id": "F3", "kind": "coverage"}],
                   assessment_rows=[{"id": "J1", "text": "Risk is up.", "confidence": "likely", "rests_on": ["F2"]}])
    failed = frozenset({"F2", "F3", "J1"})
    parsed = parse_repairs({"repairs": [
        {"id": "F2", "action": "fix", "row": {"cites": GOOD_FACT["cites"]}},
        {"id": "F3", "action": "withdraw"},
        {"id": "J1", "action": "fix", "row": {"text": "Risk is up by 40 percent."}},
        {"id": "F1", "action": "fix", "row": {"text": "anything"}},
    ]}, failed)
    assert ("F1", "not_a_failed_row") in parsed.refused
    fixed, detail = apply_repairs(reply, parsed, _ctx())
    assert detail["accepted"] == ["F2"] and detail["withdrawn"] == ["F3"]
    assert detail["refused"] == {"not_a_failed_row": 1, "repair_new_figure_not_in_quote": 1}
    assert [f.id for f in fixed.fact_rows] == ["F2"]
    out = {o.row_id: o for o in gate_reply(fixed, _ctx(), repaired=frozenset(detail["accepted"]))}
    assert out["F2"].outcome == ESTABLISHED_ELIGIBLE and out["F2"].repaired
    assert out["J1"].outcome == ASSESSMENT          # its premise passes now
    assert parse_repairs("nope", failed).refused == (("*", "reply_unparseable"),)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# prose last
# ---------------------------------------------------------------------------


def test_the_read_is_rendered_from_passed_rows_in_the_house_sections():
    reply = _reply(
        fact_rows=[GOOD_FACT, {**GOOD_FACT, "id": "F2", "kind": "coverage"}],
        absence_rows=[{"id": "A1", "topic": "a declared energy emergency"}],
        assessment_rows=[{"id": "J1", "text": "Supply to central Europe is at risk.", "confidence": "likely",
                          "rests_on": ["F1"]}],
        restatements=[{"id": "R1", "ref": PRIOR_ORDINAL, "status": "confirmed",
                       "text": "Druzhba flows were disrupted.", "bearing": ["F1"]}],
        would_change={"observation": "a restart notice", "reporting_class": "operator notices", "desk_collects": "no"},
        indicators=["Transneft restart notice"],
        lead="The Druzhba pipeline was halted on 3 November after a drone strike. Flows will not resume this year.",
    )
    ctx = _ctx()
    outcomes = gate_reply(reply, ctx)
    body, lead = render_read(reply, outcomes, ctx.computed, verdicts={"F1": "supports"})
    heads = [ln for ln in body.splitlines() if ln.startswith("## ")]
    assert heads == ["## What changed", "## Why it matters", "## What would change this read", "## Indicators"]
    assert body.splitlines()[0].startswith("*As of 2026-11-03 — this desk's collection: 3 signals from 2 sources")
    # The BLUF is BUILT by code — the top judgement, then the top fact row
    assert ("**BLUF:** Supply to central Europe is at risk. (likely) [1] The Druzhba pipeline was halted on "
            "3 November after a drone strike. [1]") in body
    assert "Flows will not resume" not in body                      # the lead sentence that maps to no row
    assert lead.as_counts() == {"lead_sentences": 2, "kept": 1, "dropped": 1, "lead_source": "code"}
    # a model lead is used only on opt-in AND when every sentence passes
    half, half_lead = render_read(reply, outcomes, ctx.computed, verdicts={"F1": "supports"}, model_lead=True)
    assert half_lead.source == "code"
    clean = _reply(fact_rows=[GOOD_FACT], lead="The Druzhba pipeline was halted on 3 November after a drone strike.")
    mbody, mlead = render_read(clean, gate_reply(clean, ctx), ctx.computed, verdicts={"F1": "supports"},
                               model_lead=True)
    assert mlead.source == "model" and "**BLUF:** The Druzhba pipeline was halted on 3 November" in mbody
    assert "From the prior read [4], confirmed" in body and "No row in this desk's collection" in body
    assert "(likely) [1]" in body
    rejected, _ = render_read(reply, outcomes, ctx.computed, verdicts={"F1": "does_not_support"})
    assert "halted on 3 November after a drone strike. [1]" not in rejected.split("## Why it matters")[0].split(
        "## What changed")[1]
    assert "No judgement in this window rests on a row" in rejected


def test_the_lead_check_drops_a_new_figure():
    ctx = _ctx()
    outcomes = gate_reply(_reply(fact_rows=[GOOD_FACT]), ctx)
    kept = check_lead("The Druzhba pipeline was halted on 3 November after a drone strike.", outcomes)
    dropped = check_lead("The Druzhba pipeline was halted on 3 November after 4 drone strikes.", outcomes)
    assert len(kept.kept) == 1 and len(dropped.dropped) == 1


# ---------------------------------------------------------------------------
# scope and the computed block
# ---------------------------------------------------------------------------


def test_the_slice_scope_says_where_its_window_came_from():
    wm = comp.slice_scope(SLICE, [SIG1], collection="d", coverage={"window_start": "2026-11-02T00:00:00+00:00",
                                                                     "window_end": "2026-11-03T00:00:00+00:00"},
                          window_hours=72)
    assert wm["window_basis"] == "watermark" and wm["row_ids"] == [SIG1] and wm["rows_handed"] == 3
    dw = comp.slice_scope(SLICE, [], collection="d", coverage=None, window_hours=72, run_at=T0)
    assert dw["window_basis"] == "descriptor_window" and dw["window_start"] == "2026-10-31T12:00:00+00:00"
    obs = comp.slice_scope(SLICE, [], collection="d", coverage=None, window_hours=None)
    assert obs["window_basis"] == "observed_span" and obs["source_ids"] == ["source.wire.a", "source.wire.b"]


def test_the_computed_block_counts_the_slice_in_code():
    c = comp.computed_block(SLICE, SCOPE)
    assert c["rows_handed"] == 3 and c["distinct_sources"] == 2
    assert c["per_source"] == {"source.wire.a": 2, "source.wire.b": 1}
    assert c["distinct_origin_groups"] == 2 and c["sources_silent"] is None
    ev = comp.slice_evidence([{**SLICE[0], "data": {**SLICE[0]["data"], "distilled_body": "OUR BRIEF"}}])
    assert "OUR BRIEF" not in ev[1].text           # our own derivative is never quotable


# ---------------------------------------------------------------------------
# one whole shadow run through a scripted model
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class ScriptedLLM:
    def __init__(self, synthesis: str, repair: str | None = None) -> None:
        self.synthesis, self.repair = synthesis, repair
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        user = messages[0]["content"]
        self.calls.append({"thinking": kwargs.get("thinking"), "kind": (
            "repair" if user.startswith("REPAIR ROUND") else "verify" if user.startswith("STATEMENT:") else "synthesis")})
        if self.calls[-1]["kind"] == "repair":
            return _Resp(self.repair or "{}")
        if self.calls[-1]["kind"] == "verify":
            return _Resp('{"verdict": "supports", "reason_code": "fully_stated"}')
        return _Resp(self.synthesis)


def _input() -> ShadowInput:
    return ShadowInput("energy_security", "desk_ru", "run-1", "SLICE PROMPT", SLICE, CITATION_INDEX,
                       [SIG1, SIG2, SIG3], coverage={"window_start": "2026-11-02T00:00:00+00:00",
                                                     "window_end": "2026-11-03T12:00:00+00:00"}, run_at=T0)


def test_a_shadow_run_gates_repairs_once_verifies_and_renders(monkeypatch):
    monkeypatch.setattr(comp, "desk_subjects", lambda t: [SubjectCandidate("target", t, ["Druzhba", "Russia"])])
    synthesis = json.dumps({"title": "Druzhba halted", "fact_rows": [
        GOOD_FACT, {**GOOD_FACT, "id": "F2", "cites": [{"row": 1, "quote": "nothing like this at all"}]}],
        "assessment_rows": [{"id": "J1", "text": "Supply is at risk.", "confidence": "likely", "rests_on": ["F1"]}]})
    llm = ScriptedLLM(synthesis, json.dumps({"repairs": [{"id": "F2", "action": "withdraw"}]}))
    rec = asyncio.run(run_contract_v2(llm, _input(), ShadowPrompts("ROWS", "VERIFY")))
    assert rec.status == "written"
    assert [c["kind"] for c in llm.calls] == ["synthesis", "repair", "verify"]
    assert [c["thinking"] for c in llm.calls] == [False, False, False]   # a double that cannot think
    assert rec.first_pass["by_outcome"][FAILED] == 1
    assert rec.final["by_outcome"]["withdrawn"] == 1 and rec.repair["withdrawn"] == 1
    assert rec.verify == {"eligible": 1, "verified": 1, "verdicts": {"supports": 1}, "established": 1}
    assert rec.body.count("## ") == 4 and rec.scope["window_basis"] == "watermark"
    counts = rec.counts()
    assert counts["calls"] == {"synthesis": 1, "repair": 1, "verify": 1}
    json.dumps(rec.as_dict(), default=str)          # the ledger row serialises


def test_a_shadow_run_never_raises_on_an_unreadable_reply(monkeypatch):
    monkeypatch.setattr(comp, "desk_subjects", lambda t: [])
    rec = asyncio.run(run_contract_v2(ScriptedLLM("this is prose, not rows"), _input(), ShadowPrompts("R", "V")))
    assert rec.status == "reply_unparseable" and rec.final == {}
    tally([])  # empty tallies are fine
