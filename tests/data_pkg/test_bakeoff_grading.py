# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-G1 — the bake-off grading steps: grader calls, gold drafting, the §6 report.

Fakes only: no registry, no gateway, no database. The grader / drafter is an
``async chat(messages)`` fake and the route probe an ``async probe(run_dir)``
fake returning a pin built by ``grade_sheet.grader_pin``.

Pinned here:

* ``grade_run.py tell-test`` — the fixed prompt, one call per answer, the
  ``{"<blind_id>": "<arm>"}`` file ``grade_sheet.py tell-score`` reads as it
  stands, never written short;
* ``grade_run.py grade`` — the grade reply shape, one retry on a malformed
  reply then ``grade_error``, usage per call, ``grading_closed_at``; the pin
  refusals (unpinned, drifted → void);
* the operator sheet — 20 % stratified (ceil per class, seeded) + every
  contradicted answer, blank, never overwritten;
* ``gold_draft.py`` rows are unsigned and the sheet voids them;
  ``gold_sign.py`` signs one row in one shape;
* ``report.py`` — hand-computed numbers on a synthetic 6 questions × 6 arms
  run, the exact permutation p, κ, Brier bins, the decision rules, and the
  refusal before ``grading_closed_at``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import shutil
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "bakeoff"))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import gold_draft  # noqa: E402
import gold_sign  # noqa: E402
import grade_run  # noqa: E402
import grade_sheet  # noqa: E402
import normalise  # noqa: E402
import report  # noqa: E402

ARMS6 = ("A", "A-r", "B", "C", "D", "E")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _pin(root="nvidia/nemotron-lightning-x", probed_at="T1"):
    comps = {
        grade_sheet.GRADER_COMPONENT: {"model_name": "lightning-think", "version": "a" * 16,
                                       "state": "active", "api_endpoint": "https://gw/v1"},
        grade_sheet.GOLD_DRAFTER_COMPONENT: {"model_name": "gemma-think", "version": "b" * 16,
                                             "state": "active", "api_endpoint": "https://gw/v1"},
    }
    listings = {
        grade_sheet.GRADER_COMPONENT: {"data": [{"id": "lightning-think", "root": root}]},
        grade_sheet.GOLD_DRAFTER_COMPONENT: {"data": [{"id": "gemma-think"}]},
    }
    return grade_sheet.grader_pin(comps, listings, probed_at=probed_at)


def _probe(pin=None):
    async def probe(run_dir):
        return pin or _pin(probed_at="T-recheck")
    return probe


def _usage(i=100, o=20):
    return {"input_tokens": i, "output_tokens": o, "reasoning_tokens": None, "total_tokens": i + o}


class FakeChat:
    """Replies by a rule over the prompt; counts and keeps every call."""

    def __init__(self, rule):
        self.rule = rule
        self.calls: list[list[dict]] = []

    async def __call__(self, messages):
        self.calls.append(messages)
        text = self.rule(messages)
        if isinstance(text, Exception):
            raise text
        return {"text": text, "usage": _usage(), "response_model": "lightning-think", "elapsed_s": 0.1}


def _run_dir(tmp_path, **extra):
    d = tmp_path / "run"
    (d / "records").mkdir(parents=True)
    (d / "grading").mkdir()
    run = {"run_id": "r1", "as_of": "2026-11-05T12:00:00+00:00", "design_sha256": "d" * 64,
           "design_version": "1.1", "spent_usd": 0.0}
    grade_sheet.apply_pin(run, _pin())
    run.update(extra)
    (d / "run.json").write_text(json.dumps(run))
    return d


def _run(d):
    return json.loads((d / "run.json").read_text())


# ---------------------------------------------------------------------------
# The gateway call shape
# ---------------------------------------------------------------------------


def test_chat_body_is_temperature_one_with_no_max_tokens():
    body = common.chat_body("lightning-think", [{"role": "user", "content": "x"}])
    assert body == {"model": "lightning-think", "temperature": 1.0, "messages": [{"role": "user", "content": "x"}]}
    assert common.chat_url("https://api.innoai.net/v1") == "https://api.innoai.net/v1/chat/completions"
    assert common.chat_url("http://ai1:8000/") == "http://ai1:8000/v1/chat/completions"
    assert common.extract_json_object('<think>{"no": 1}</think>\n```json\n{"arm": "B"}\n```') == {"arm": "B"}
    assert common.extract_json_object("I think {\"a\": 1} then {\"arm\": \"C\"}") == {"arm": "C"}
    assert common.extract_json_object("no json") is None


def test_the_fixed_prompts_are_hashed_and_stable():
    assert grade_run.TELL_PROMPT_SHA256 == grade_run.prompt_sha(grade_run.TELL_PROMPT, grade_run.RETRY_NOTE)
    assert len({grade_run.TELL_PROMPT_SHA256, grade_run.GRADE_PROMPT_SHA256, gold_draft.GOLD_PROMPT_SHA256}) == 3
    run = {"grade_prompt_sha256": "0" * 64}
    with pytest.raises(grade_run.GradingRefused, match="changed mid-run"):
        grade_run._check_prompt(run, "grade_prompt_sha256", grade_run.GRADE_PROMPT_SHA256)


# ---------------------------------------------------------------------------
# The tell test
# ---------------------------------------------------------------------------


def _tell_inputs(tmp_path):
    dz = SimpleNamespace(sha256="d" * 64, arm_titles={a: f"title of arm {a}" for a in "ABCDE"})
    recs = []
    for arm in "ABCDE":
        for klass, pre in (("HEADLINE", "HE"), ("LONG-MEMORY", "LM"), ("SLOW-CHANGE", "SC"), ("INSUFFICIENT", "IE")):
            for i in range(1, 3):
                recs.append({"question_id": f"{pre}{i:02d}", "class": klass, "arm": arm, "label": arm,
                             "status": "answered", "raw_answer": f"Answer TXT-{arm}-{pre}{i} on the matter.",
                             "stated_confidence": 0.5, "receipts": [], "question": "q"})
    test, key = grade_sheet.build_tell_test(dz, recs, seed=3)
    d = _run_dir(tmp_path, blind_keys=[{"purpose": "tell-test", "sha256": "k", "seed": 3}])
    (d / "grading" / "tell_test.json").write_text(normalise.dumps(test))
    (d / "sealed").mkdir()
    (d / "sealed" / "tell_key.json").write_text(normalise.dumps(key))
    return d, test, key


def test_tell_test_round_trip_feeds_tell_score_as_it_stands(tmp_path):
    d, test, key = _tell_inputs(tmp_path)
    first = test["items"][0]["blind_id"]
    seen: dict[str, int] = {}

    def rule(messages):
        prompt = messages[0]["content"]
        bid = next(it["blind_id"] for it in test["items"] if it["answer"] in prompt)
        seen[bid] = seen.get(bid, 0) + 1
        if bid == first and seen[bid] == 1:
            return "I believe it is probably the consult one."  # malformed → one retry
        return '{"arm": "A"}'

    chat = FakeChat(rule)
    rc = asyncio.run(grade_run.main_async(grade_run.parse_args(["tell-test", "--run-dir", str(d)]),
                                          chat=chat, probe=_probe()))
    assert rc == 0
    assert len(chat.calls) == 31  # 30 answers + 1 retry
    prompt = chat.calls[0][0]["content"]
    assert "title of arm C" in prompt and '{"arm": "<A, B, C, D or E>"}' in prompt
    retry = next(c for c in chat.calls if len(c) == 3)
    assert retry[1]["role"] == "assistant" and "did not match" in retry[2]["content"]
    answers = json.loads((d / "grading" / "tell_answers.json").read_text())
    assert set(answers) == {it["blind_id"] for it in test["items"]} and set(answers.values()) == {"A"}
    calls = json.loads((d / "grading" / "tell_calls.json").read_text())
    assert len(calls["items"][first]["calls"]) == 2 and calls["items"][first]["calls"][0]["usage"]["input_tokens"] == 100
    run = _run(d)
    assert run["tell_prompt_sha256"] == grade_run.TELL_PROMPT_SHA256
    assert run["tell_usage"]["calls"] == 31 and run["grader_rechecks"][0]["drift"] == []
    # grade_sheet.py tell-score reads the file exactly as written: 6 of 30 are A → 20 % → pass.
    assert grade_sheet.main(["tell-score", "--key", str(d / "sealed" / "tell_key.json"),
                             "--predictions", str(d / "grading" / "tell_answers.json"),
                             "--run-dir", str(d)]) == 0
    assert _run(d)["tell_tests"][-1] == {"accuracy": 0.2, "passed": True, "seed": 3}


def test_tell_answers_are_never_written_short(tmp_path):
    d, test, _ = _tell_inputs(tmp_path)
    stuck = test["items"][5]["answer"]
    chat = FakeChat(lambda m: "no idea" if stuck in m[0]["content"] else '{"arm": "B"}')
    args = grade_run.parse_args(["tell-test", "--run-dir", str(d)])
    assert asyncio.run(grade_run.main_async(args, chat=chat, probe=_probe())) == 4
    assert not (d / "grading" / "tell_answers.json").exists()
    # A re-run asks only the missing one.
    chat2 = FakeChat(lambda m: '{"arm": "B"}')
    assert asyncio.run(grade_run.main_async(args, chat=chat2, probe=_probe())) == 0
    assert len(chat2.calls) == 1


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


GOLD2 = {"gold_atoms": [{"id": "g1", "text": "fact one", "source": "s"}, {"id": "g2", "text": "fact two", "source": "s"}],
         "error_atoms": [{"id": "e1", "text": "wrong"}], "prior_state_note": "before", "absence_proof": None}
GOLD_IE = {"gold_atoms": [{"id": "g1", "text": "declines the specific", "source": "s"}],
           "error_atoms": [{"id": "e1", "text": "the specific as fact"}], "prior_state_note": "p",
           "absence_proof": "searched"}


def _grade(sc=2, con=0, asserted=False, n_gold=2, usefulness=3, novelty=None, trace=None, temporal=None,
           stale=False, conf=None, answered=True, parts=1.0):
    statuses = ["stated_correct"] * sc + ["contradicted"] * con
    statuses += ["absent"] * (n_gold - len(statuses))
    return {"gold_atoms": [{"id": f"g{i}", "status": s} for i, s in enumerate(statuses, 1)],
            "error_atoms": [{"id": "e1", "asserted": asserted}], "usefulness": usefulness,
            "novelty_atoms": novelty or [], "traceability": trace or [], "temporal": temporal or [],
            "stale_state": stale, "stated_confidence": conf,
            "coverage": {"answered": answered, "parts_addressed": parts}}


def test_a_grade_reply_is_parsed_and_a_bad_one_named():
    item = {"gold": GOLD2}
    good = _grade(sc=1, con=1, novelty=["g1"], trace=[{"sentence": "s [R1]", "verdict": "partial"}],
                  temporal=[{"claim": "on 2026-11-01", "right": True}], conf=0.7, parts=0.5)
    grade, problems = grade_run.parse_grade("```json\n" + json.dumps({**good, "extra": 1}) + "\n```", item)
    assert problems == [] and grade == good  # extra keys dropped, the shape kept
    bad = {**good, "gold_atoms": [{"id": "g1", "status": "right"}], "usefulness": 5, "novelty_atoms": ["g2"],
           "traceability": [{"sentence": "s", "verdict": "supports"}] * 6}
    _, problems = grade_run.parse_grade(json.dumps(bad), item)
    text = " | ".join(problems)
    assert "each exactly once" in text and "not one of" in text and "usefulness 5" in text
    assert "novelty_atoms" in text and "traceability" in text
    assert grade_run.parse_grade("nothing here", item)[1] == ["no JSON object in the reply"]


def _sheet(n_items=3):
    items = []
    for i in range(n_items):
        items.append({"position": i + 1, "blind_id": f"bid{i:03d}", "question_id": f"HE{i + 1:02d}",
                      "question": f"Question {i}?", "class": "HEADLINE", "as_of": "2026-11-05T12:00:00+00:00",
                      "gold": GOLD2, "answer": f"ANS-{i} text.\n\nStated confidence: 0.60",
                      "receipts_axis_4": [{"r": "R1", "documents": [{"masthead": "DW", "date": "published 2026-11-01",
                                                                      "title": "t", "url": "u", "body": "b"}]}]})
    return {"schema": grade_sheet.SHEET_SCHEMA, "design_sha256": "d" * 64, "seed": 9, "items": items,
            "void_questions": [], "void_for_arm": [], "descriptive_only_classes": [], "excluded": []}


def _grading_run(tmp_path, sheet, **extra):
    extra = {"blind_keys": [{"purpose": "sheet", "sha256": "k", "seed": 9}],
             "tell_tests": [{"accuracy": 0.2, "passed": True, "seed": 3}], **extra}
    d = _run_dir(tmp_path, **extra)
    (d / "grading" / "sheet.json").write_text(normalise.dumps(sheet))
    return d


def test_grade_retries_a_malformed_reply_once_then_records_grade_error(tmp_path):
    sheet = _sheet(3)
    d = _grading_run(tmp_path, sheet)
    tries: dict[str, int] = {}

    def rule(messages):
        prompt = messages[0]["content"]
        i = int(re.search(r"ANS-(\d+)", prompt).group(1))
        tries[i] = tries.get(i, 0) + 1
        if i == 1 and tries[i] == 1:
            return '{"gold_atoms": []}'          # malformed → retried, then good
        if i == 2:
            return "the answer is fine"           # malformed twice → grade_error
        return json.dumps(_grade(sc=1, conf=0.6))

    chat = FakeChat(rule)
    rc = asyncio.run(grade_run.main_async(grade_run.parse_args(["grade", "--run-dir", str(d)]),
                                          chat=chat, probe=_probe()))
    assert rc == 0 and len(chat.calls) == 5
    p = chat.calls[0][0]["content"]
    assert "g1: fact one" in p and "e1: wrong" in p and "[R1] DW — published 2026-11-01" in p
    assert "Prior-state note (what was already established at the window start): before" in p
    doc = json.loads((d / "grading" / "grades_primary.json").read_text())
    items = doc["items"]
    assert set(items) == {"bid000", "bid001", "bid002"}
    assert items["bid000"]["grade"] == _grade(sc=1, conf=0.6) and items["bid000"]["grade_error"] is None
    assert len(items["bid001"]["calls"]) == 2 and items["bid001"]["grade"] is not None
    assert items["bid002"]["grade"] is None and "no JSON object" in items["bid002"]["grade_error"]
    assert items["bid002"]["last_reply"] == "the answer is fine" and len(items["bid002"]["calls"]) == 2
    assert all(c["usage"]["input_tokens"] == 100 for e in items.values() for c in e["calls"])
    assert doc["sheet_sha256"] == common.sha256_file(d / "grading" / "sheet.json")
    run = _run(d)
    assert run["grading_closed_at"] and run["grade_prompt_sha256"] == grade_run.GRADE_PROMPT_SHA256
    assert run["grade_usage"] == {"calls": 5, "failed_calls": 0, "input_tokens": 500, "output_tokens": 100}
    # Closed: a second grade run is refused.
    with pytest.raises(grade_run.GradingRefused, match="closed"):
        asyncio.run(grade_run.main_async(grade_run.parse_args(["grade", "--run-dir", str(d)]),
                                         chat=chat, probe=_probe()))


def test_grade_concurrency_never_exceeds_four():
    sheet = _sheet(12)
    live = {"now": 0, "max": 0}

    async def chat(messages):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        return {"text": json.dumps(_grade()), "usage": _usage()}

    entries = asyncio.run(grade_run.run_grades(sheet, chat, {}, concurrency=10))
    assert len(entries) == 12 and live["max"] == 4


def test_grading_refuses_unpinned_drifted_or_untold(tmp_path):
    sheet = _sheet(1)
    args = lambda d: grade_run.parse_args(["grade", "--run-dir", str(d)])  # noqa: E731
    chat = FakeChat(lambda m: json.dumps(_grade()))
    d = _grading_run(tmp_path / "a", sheet)
    run = _run(d)
    run.pop("grader_pin")
    (d / "run.json").write_text(json.dumps(run))
    with pytest.raises(grade_sheet.SheetRefused, match="pin-grader"):
        asyncio.run(grade_run.main_async(args(d), chat=chat, probe=_probe()))
    d = _grading_run(tmp_path / "b", sheet)
    with pytest.raises(grade_sheet.SheetRefused, match="void"):
        asyncio.run(grade_run.main_async(args(d), chat=chat, probe=_probe(_pin(root="nvidia/other"))))
    assert "served_model" in _run(d)["grader_route_changed"]["drift"][0]
    assert chat.calls == []
    d = _grading_run(tmp_path / "c", sheet, tell_tests=[{"accuracy": 0.5, "passed": False, "seed": 1}])
    with pytest.raises(grade_run.GradingRefused, match="tell test"):
        asyncio.run(grade_run.main_async(args(d), chat=chat, probe=_probe()))
    d = _run_dir(tmp_path / "e", tell_tests=[{"passed": False}])
    with pytest.raises(grade_sheet.SheetRefused, match="pin-grader"):  # the tell test refuses unpinned too
        r = _run(d)
        r.pop("grader_pin")
        (d / "run.json").write_text(json.dumps(r))
        asyncio.run(grade_run.main_async(grade_run.parse_args(["tell-test", "--run-dir", str(d)]),
                                         chat=chat, probe=_probe()))


# ---------------------------------------------------------------------------
# The operator's sheet
# ---------------------------------------------------------------------------


def _class_sheet():
    items = []
    for c, pre in (("HEADLINE", "HE"), ("LONG-MEMORY", "LM"), ("SLOW-CHANGE", "SC"), ("INSUFFICIENT", "IE")):
        for i in range(10):
            items.append({"blind_id": f"{pre.lower()}{i:04d}", "question_id": f"{pre}{i:02d}", "class": c,
                          "question": "q", "as_of": "T", "gold": GOLD2, "answer": "a", "receipts_axis_4": []})
    return {"seed": 5, "design_sha256": "d", "items": items}


def test_operator_sample_is_twenty_percent_stratified_plus_every_contradicted():
    sheet = _class_sheet()
    grades = {it["blind_id"]: {"grade": _grade(sc=2)} for it in sheet["items"]}
    for bid in ("he0001", "sc0007", "ie0003"):
        grades[bid] = {"grade": _grade(sc=1, con=1)}
    grades["lm0002"] = {"grade": None, "grade_error": "x"}
    doc, members = grade_run.operator_sample(sheet, grades, seed=5)
    sampled = {b for b, w in members.items() if "sample" in w}
    flagged = {b for b, w in members.items() if "contradicted" in w}
    assert flagged == {"he0001", "sc0007", "ie0003"}
    for pre in ("he", "lm", "sc", "ie"):
        assert sum(1 for b in sampled if b.startswith(pre)) == 2  # ceil(0.2 × 10)
    assert set(doc["items"]) == sampled | flagged
    again, members2 = grade_run.operator_sample(sheet, grades, seed=5)
    assert members2 == members and again == doc  # seeded-stable
    other, _ = grade_run.operator_sample(sheet, grades, seed=6)
    assert set(other["items"]) != set(doc["items"])
    row = next(iter(doc["items"].values()))
    assert "reasons" not in row and "primary" not in json.dumps(row)  # the operator never sees the flag
    assert row["grade"]["gold_atoms"] == [{"id": "g1", "status": None}, {"id": "g2", "status": None}]
    assert row["grade"]["coverage"] == {"answered": None, "parts_addressed": None}
    assert grade_run.validate_grade(row["grade"], ["g1", "g2"], ["e1"])[0] is None  # blank ≠ filled


def test_operator_sample_cli_needs_closed_grading_and_never_overwrites(tmp_path):
    sheet = _class_sheet()
    d = _grading_run(tmp_path, sheet)
    (d / "grading" / "grades_primary.json").write_text(json.dumps(
        {"items": {it["blind_id"]: {"grade": _grade()} for it in sheet["items"]}}))
    args = grade_run.parse_args(["grade", "--run-dir", str(d), "--rater", "operator-sample"])
    with pytest.raises(grade_run.GradingRefused, match="not closed"):
        asyncio.run(grade_run.main_async(args))
    run = _run(d)
    run["grading_closed_at"] = "2026-11-08T00:00:00+00:00"
    (d / "run.json").write_text(json.dumps(run))
    assert asyncio.run(grade_run.main_async(args)) == 0
    op = _run(d)["operator_sample"]
    assert op["seed"] == 5 and op["sampled"] == 8 and op["contradicted"] == 0 and op["rows"] == 8
    with pytest.raises(grade_run.GradingRefused, match="never overwritten"):
        asyncio.run(grade_run.main_async(args))


# ---------------------------------------------------------------------------
# Gold: drafted unsigned, voided by the sheet, signed by the operator
# ---------------------------------------------------------------------------


QS = [design_mod.Question("HE01", "HEADLINE", "country_g20_cn", "T-7d..T", "G-WIRE", "What did China announce?"),
      design_mod.Question("IE01", "INSUFFICIENT", "country_g20_cn", "as of T", "G-ABSENCE", "Exactly how many?")]


def _drafter(messages):
    p = messages[0]["content"]
    body = {"gold_atoms": [{"id": "g1", "text": "China listed gallium", "source": "Reuters"},
                           {"id": "g2", "text": "on 2026-11-01", "source": "MOFCOM notice"}],
            "error_atoms": [{"id": "e1", "text": "a ban on all exports"}],
            "prior_state_note": "controls existed since 2023", "absence_proof": None}
    if "Class: INSUFFICIENT" in p:
        body["absence_proof"] = "no official count published"
    return json.dumps(body)


def test_gold_draft_rows_are_unsigned_and_the_sheet_voids_them(tmp_path):
    d = _run_dir(tmp_path)
    chat = FakeChat(_drafter)
    rc = asyncio.run(gold_draft.main_async(gold_draft.parse_args(["--run-dir", str(d)]),
                                           chat=chat, probe=_probe(), questions=QS))
    assert rc == 0 and len(chat.calls) == 2
    prompt = chat.calls[0][0]["content"]
    assert "Question id: HE01" in prompt and "Gold-source rule: G-WIRE" in prompt
    assert "Window: 2026-10-29 to 2026-11-05T12:00:00+00:00" in prompt and "As of (T): 2026-11-05" in prompt
    doc = json.loads((d / "gold_draft.json").read_text())
    assert doc["web_access"] is False and doc["gold_prompt_sha256"] == gold_draft.GOLD_PROMPT_SHA256
    for qid, row in doc["questions"].items():
        assert row["signed"] is False and row["signed_by"] is None, qid
        assert all(a["source"] == gold_draft.UNVERIFIED for a in row["gold_atoms"])
    assert doc["questions"]["HE01"]["gold_atoms"][1]["source_hint"] == "MOFCOM notice"
    assert doc["questions"]["IE01"]["absence_proof"] == "no official count published"
    assert doc["questions"]["HE01"]["absence_proof"] is None
    assert _run(d)["gold_prompt_sha256"] == gold_draft.GOLD_PROMPT_SHA256
    void, _, _ = grade_sheet.void_rules(QS, doc, [])
    assert void == {"HE01": "gold_missing: no operator-signed gold row",
                    "IE01": "gold_missing: no operator-signed absence proof"}
    recs = [{"question_id": q.qid, "class": q.klass, "arm": "B", "label": "B", "status": "answered",
             "raw_answer": "x", "receipts": []} for q in QS]
    sheet, _ = grade_sheet.build_sheet(SimpleNamespace(sha256="d", questions=QS), recs, doc, seed=1, questions=QS)
    assert sheet["items"] == [] and len(sheet["void_questions"]) == 2


def test_gold_draft_retries_then_records_a_draft_error():
    chat = FakeChat(lambda m: '{"gold_atoms": [{"text": "only one"}], "error_atoms": [], "prior_state_note": "p"}')
    rows = asyncio.run(gold_draft.draft_gold(QS[:1], common.parse_as_of("2026-11-05T12:00:00Z"), chat, {}))
    row = rows["HE01"]
    assert len(chat.calls) == 2 and "2 to 6" in row["draft_error"]
    assert row["gold_atoms"] == [] and row["signed_by"] is None
    _, problems = gold_draft.validate_draft({"gold_atoms": [{"text": "a"}, {"text": "b"}], "error_atoms": [],
                                             "prior_state_note": "p", "absence_proof": None}, "INSUFFICIENT")
    assert problems == ["absence_proof is required on an INSUFFICIENT question"]


def test_gold_sign_signs_one_row_in_one_shape(tmp_path):
    d = _run_dir(tmp_path)
    asyncio.run(gold_draft.main_async(gold_draft.parse_args(["--run-dir", str(d)]),
                                      chat=FakeChat(_drafter), probe=_probe(), questions=QS))
    with pytest.raises(gold_sign.SignRefused, match="copy gold_draft.json"):
        gold_sign.main(["--run-dir", str(d), "--qid", "HE01", "--by", "Lewis"])
    shutil.copy(d / "gold_draft.json", d / "gold.json")
    with pytest.raises(gold_sign.SignRefused, match="no verified source"):
        gold_sign.main(["--run-dir", str(d), "--qid", "HE01", "--by", "Lewis"])
    doc = json.loads((d / "gold.json").read_text())
    for a in doc["questions"]["HE01"]["gold_atoms"]:
        a["source"] = "https://example.org/verified"
    (d / "gold.json").write_text(json.dumps(doc))
    assert gold_sign.main(["--run-dir", str(d), "--qid", "HE01", "--by", "Lewis"]) == 0
    doc = json.loads((d / "gold.json").read_text())
    row = doc["questions"]["HE01"]
    assert row["signed"] is True and row["signed_by"] == "Lewis" and row["met"] is True
    assert re.match(r"\d{4}-\d{2}-\d{2}T", row["signed_at"])
    assert row["signed_row_sha256"] == gold_sign.row_sha256(row)
    assert doc["questions"]["IE01"]["signed_by"] is None  # one row only
    void, _, _ = grade_sheet.void_rules(QS, doc, [])
    assert "HE01" not in void and "IE01" in void
    with pytest.raises(gold_sign.SignRefused, match="already signed"):
        gold_sign.main(["--run-dir", str(d), "--qid", "HE01", "--by", "Lewis"])
    # A void INSUFFICIENT row is signed on the operator's word alone.
    assert gold_sign.main(["--run-dir", str(d), "--qid", "IE01", "--by", "Lewis",
                           "--authoritative-answer-exists"]) == 0
    doc = json.loads((d / "gold.json").read_text())
    assert doc["questions"]["IE01"]["authoritative_answer_exists"] is True
    void, _, _ = grade_sheet.void_rules(QS, doc, [])
    assert void["IE01"].startswith("an authoritative answer exists")


# ---------------------------------------------------------------------------
# The report — statistics on known inputs
# ---------------------------------------------------------------------------


def test_exact_paired_permutation_on_known_pairs():
    rng = report.rng_for(1, "x")
    # [0.5, 1, 0.5, 0.5, 0, 1]: only the two all-same-sign patterns of the five non-zero
    # differences reach |Σ| = 3.5, × 2 for the zero's sign → 4 of 64.
    assert report.paired_permutation([0.5, 1.0, 0.5, 0.5, 0.0, 1.0], rng) == (0.0625, "exact")
    # [0.5, −0.5, 0.5, 0.5]: |Σ| ≥ 1 needs k ∈ {0, 1, 3, 4} plus-signs → 1 + 4 + 4 + 1 = 10 of 16.
    assert report.paired_permutation([0.5, -0.5, 0.5, 0.5], rng) == (0.625, "exact")
    assert report.paired_permutation([], rng) == (None, None)
    # 20 equal differences: Monte Carlo, only the identity-like patterns reach → p = 1/10001.
    p, method = report.paired_permutation([1.0] * 20, report.rng_for(1, "y"))
    assert method == "monte_carlo" and p == pytest.approx(1 / 10_001)


def test_cohen_kappa_on_a_known_table():
    pairs = [(1, 1)] * 4 + [(1, 0)] + [(0, 1)] + [(0, 0)] * 4
    k = report.cohen_kappa(pairs)  # po 0.8, pe 0.5·0.5 + 0.5·0.5 = 0.5 → κ 0.6
    assert k["observed_agreement"] == 0.8 and k["expected_agreement"] == 0.5 and k["kappa"] == pytest.approx(0.6)
    assert report.cohen_kappa([(1, 1), (1, 1)])["kappa"] is None  # pe = 1: undefined
    assert report.cohen_kappa([])["n"] == 0


def test_brier_and_its_three_bins():
    pairs = [(0.9, 1), (0.9, 0), (0.2, 1), (0.5, 1), (1.0, 1), (1 / 3, 0)]
    # (0.01 + 0.81 + 0.64 + 0.25 + 0 + 1/9) / 6
    assert report.brier(pairs) == pytest.approx((1.71 + 1 / 9) / 6)
    bins = report.reliability(pairs)
    assert [b["n"] for b in bins] == [1, 2, 3]  # 1/3 opens the middle bin; 1.0 closes the top one
    assert bins[0]["mean_confidence"] == 0.2 and bins[0]["observed_correct"] == 1.0
    assert bins[1]["mean_confidence"] == pytest.approx((0.5 + 1 / 3) / 2, abs=1e-6) and bins[1]["observed_correct"] == 0.5
    assert bins[2]["mean_confidence"] == pytest.approx(2.8 / 3, abs=1e-6)
    assert bins[2]["observed_correct"] == pytest.approx(2 / 3, abs=1e-6)
    assert report.brier([]) is None


def test_bootstrap_ci_is_seeded_and_degenerate_on_a_constant():
    assert report.bootstrap_ci([0.25] * 7, report.mean, report.rng_for(3, "c")) == [0.25, 0.25]
    a = report.bootstrap_ci([0.0, 0.5, 1.0, 1.0], report.mean, report.rng_for(3, "c"))
    b = report.bootstrap_ci([0.0, 0.5, 1.0, 1.0], report.mean, report.rng_for(3, "c"))
    assert a == b and 0.0 <= a[0] <= 0.625 <= a[1] <= 1.0
    assert report.bootstrap_ci([], report.mean, report.rng_for(3, "c")) is None


# The synthetic run: 6 questions × 6 arms. Per answer (stated-correct gold atoms,
# contradicted gold atoms, error atom asserted). HE/LM/SC questions carry two gold
# atoms, IE questions one (the decline).
QIDS = (("HE01", "HEADLINE"), ("HE02", "HEADLINE"), ("LM01", "LONG-MEMORY"), ("SC01", "SLOW-CHANGE"),
        ("IE01", "INSUFFICIENT"), ("IE02", "INSUFFICIENT"))
SPEC = {
    "A":   [(1, 0, 0), (0, 0, 0), (0, 0, 0), (1, 0, 0), (1, 0, 0), (0, 0, 1)],
    "A-r": [(2, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0)],
    "B":   [(2, 0, 0), (2, 0, 0), (1, 0, 0), (2, 0, 0), (1, 0, 0), (1, 0, 0)],
    "C":   [(1, 1, 0), (2, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0)],
    "D":   [(2, 0, 0), (1, 0, 0), (2, 0, 0), (1, 0, 0), (0, 0, 1), (1, 0, 0)],
    "E":   [(1, 0, 0), (1, 0, 0), (0, 0, 0), (2, 0, 0), (1, 0, 0), (1, 0, 0)],
}
COST = {"A": 0.0, "A-r": 0.0, "B": 2.0, "C": 1.0, "D": 0.5, "E": 0.25}
CONF_A = {"HE01": 0.9, "HE02": 0.9, "LM01": None, "SC01": 0.2, "IE01": 0.5, "IE02": None}


def _synthetic_report_run(tmp_path, *, closed=True):
    d = tmp_path / "run"
    (d / "records").mkdir(parents=True)
    (d / "grading").mkdir()
    (d / "sealed").mkdir()
    items, key_items, grades = [], {}, {}
    n = 0
    for arm in ARMS6:
        for (qid, klass), (sc, con, asserted) in zip(QIDS, SPEC[arm]):
            n += 1
            bid = f"x{n:05d}"
            ie = klass == "INSUFFICIENT"
            conf = CONF_A[qid] if arm == "A" else 0.8 if arm == "B" else None
            items.append({"position": n, "blind_id": bid, "question_id": qid, "question": f"{qid}?", "class": klass,
                          "as_of": "T", "gold": GOLD_IE if ie else GOLD2, "receipts_axis_4": [],
                          "answer": f"Answer {bid}.\n\nStated confidence: " + ("none" if conf is None else f"{conf:.2f}")})
            key_items[bid] = {"arm": arm, "label": arm, "question_id": qid,
                              "cut": {"dropped": [3] if arm == "D" else []}}
            g = _grade(sc=sc, con=con, asserted=bool(asserted), n_gold=1 if ie else 2,
                       usefulness=1 if arm == "A" else (4 if (arm, qid) == ("C", "HE01") else 3), conf=conf)
            if arm == "B":
                g["novelty_atoms"] = ["g1"] if not ie else []
                g["traceability"] = [{"sentence": "a", "verdict": "supports"}, {"sentence": "b", "verdict": "partial"}]
                g["temporal"] = [{"claim": "c", "right": True}, {"claim": "d", "right": False}]
            if arm == "A":
                g["traceability"] = [{"sentence": "a", "verdict": "unresolvable"}]
                if qid == "HE02":
                    g["stale_state"] = True
                    g["coverage"]["parts_addressed"] = 0.0
            grades[bid] = {"grade": g, "grade_error": None, "calls": []}
            (d / "records" / f"{qid}__{arm}.json").write_text(json.dumps(
                {"question_id": qid, "arm": arm, "cost": {"usd": COST[arm]}}))
    sheet = {"schema": grade_sheet.SHEET_SCHEMA, "seed": 77, "items": items, "design_sha256": "d" * 64,
             "void_questions": [{"question_id": "HE09", "reason": "gold_missing: x"}], "void_for_arm": [],
             "descriptive_only_classes": [], "excluded": []}
    (d / "grading" / "sheet.json").write_text(normalise.dumps(sheet))
    key = {"schema": grade_sheet.KEY_SCHEMA, "seed": 77, "items": key_items}
    (d / "sealed" / "blind_key.json").write_text(normalise.dumps(key))
    (d / "grading" / "grades_primary.json").write_text(json.dumps(
        {"sheet_sha256": common.sha256_file(d / "grading" / "sheet.json"), "items": grades}))
    by = {(it["question_id"], key_items[it["blind_id"]]["arm"]): it for it in items}
    op_items = {}
    for (qid, arm), sc in ((("HE01", "A"), 2), (("HE02", "A"), 0), (("HE01", "B"), 0), (("LM01", "A"), 0),
                           (("SC01", "C"), None)):
        it = by[(qid, arm)]
        op_items[it["blind_id"]] = {**{k: it[k] for k in ("blind_id", "question_id", "class", "gold", "answer")},
                                    "grade": grade_run.blank_grade(it) if sc is None else _grade(sc=sc)}
    (d / "grading" / "operator_sample.json").write_text(json.dumps({"items": op_items}))
    (d / "grading" / "packet_ceiling.json").write_text(json.dumps(
        {"questions": {"HE01": {"share_present": 0.9}, "HE02": {"share_present": 0.8}, "LM01": {"share_present": 0.0}}}))
    run = {"run_id": "r1", "as_of": "2026-11-05T12:00:00+00:00", "design_sha256": "d" * 64, "design_version": "1.1",
           "grader_model_served": "nvidia/nemotron-lightning-x",
           "blind_keys": [{"purpose": "sheet", "sha256": common.sha256_file(d / "sealed" / "blind_key.json"),
                           "seed": 77}]}
    grade_sheet.apply_pin(run, _pin())
    if closed:
        run["grading_closed_at"] = "2026-11-09T00:00:00+00:00"
    (d / "run.json").write_text(json.dumps(run))
    return d


def test_report_refuses_before_grading_is_closed(tmp_path):
    d = _synthetic_report_run(tmp_path, closed=False)
    with pytest.raises(report.ReportRefused, match="not closed"):
        report.main(["--run-dir", str(d)])
    assert not (d / "report.json").exists() and "blind_key_opened_at" not in _run(d)


def test_report_refuses_a_key_that_is_not_the_sealed_one(tmp_path):
    d = _synthetic_report_run(tmp_path)
    (d / "sealed" / "blind_key.json").write_text("{}")
    with pytest.raises(report.ReportRefused, match="not the key sealed"):
        report.build(d)


def test_report_reproduces_the_hand_computed_numbers(tmp_path):
    d = _synthetic_report_run(tmp_path)
    assert report.main(["--run-dir", str(d)]) == 0
    rep = json.loads((d / "report.json").read_text())
    an = rep["analysis"]
    pa = an["per_arm"]
    approx = lambda x: pytest.approx(x, abs=1e-6)  # noqa: E731
    assert rep["seed"] == 77 and rep["counts"] == {"answers": 36, "graded": 36, "grade_errors": 0}
    assert _run(d)["blind_key_opened_at"]
    # Axis 1. A: .5 0 0 .5 1 0; A-r: 1 .5 .5 .5 1 1; B: 1 1 .5 1 1 1; C: .5 1 .5 .5 1 1; D: 1 .5 1 .5 0 1; E: .5 .5 0 1 1 1.
    assert {a: pa[a]["correct"] for a in ARMS6} == {
        "A": approx(2 / 6), "A-r": approx(4.5 / 6), "B": approx(5.5 / 6), "C": approx(4.5 / 6),
        "D": approx(4 / 6), "E": approx(4 / 6)}
    # error_rate: A IE02 asserted (1/1), C HE01 contradicted (1/2), D IE01 asserted (1/1).
    assert pa["A"]["error_rate"] == approx(1 / 6) and pa["C"]["error_rate"] == approx(0.5 / 6)
    assert pa["D"]["error_rate"] == approx(1 / 6) and pa["B"]["error_rate"] == 0.0
    # Axis 2: C's 4 on a contradicted answer is capped at 2.
    assert pa["A"]["usefulness"] == 1.0 and pa["B"]["usefulness"] == 3.0
    assert pa["C"]["usefulness"] == approx(17 / 6) and pa["C"]["usefulness_capped"] == 1
    # Axis 3: B novel 4 atoms of its 9 stated-correct.
    assert pa["B"]["novelty_mean"] == approx(4 / 6) and pa["B"]["novelty_share"] == approx(4 / 9)
    assert pa["A"]["novelty_share"] == 0.0
    # Axis 4: B (1 + 0.5) per 2 sentences; A unresolvable; C no sentences.
    assert pa["B"]["traceability"] == 0.75 and pa["A"]["traceability"] == 0.0 and pa["C"]["traceability"] is None
    # Axis 5.
    assert pa["B"]["temporal"] == 0.5 and pa["B"]["temporal_claims"] == 12 and pa["A"]["stale"] == 1
    # Axis 6: A (0.9,1) (0.9,0) (0.2,1) (0.5,1) → 1.71 / 4; B 0.8 against six correct → 0.04.
    assert pa["A"]["brier"] == approx(0.4275) and pa["A"]["confidence_coverage"] == approx(4 / 6)
    assert [b["n"] for b in pa["A"]["reliability"]] == [1, 1, 2]
    assert pa["A"]["reliability"][2]["mean_confidence"] == 0.9 and pa["A"]["reliability"][2]["observed_correct"] == 0.5
    assert pa["B"]["brier"] == approx(0.04) and pa["B"]["brier_ci"] == [approx(0.04), approx(0.04)]
    assert pa["C"]["brier"] is None and pa["C"]["confidence_coverage"] == 0.0
    # Axis 7 + axis 8.
    assert pa["A"]["parts_addressed"] == approx(5 / 6) and pa["A"]["abstentions"] == 1 and pa["A"]["answered"] == 1.0
    assert {a: pa[a]["cost_per_answer_usd"] for a in ARMS6} == COST
    assert pa["D"]["cut_share"] == 1.0 and pa["B"]["cut_share"] == 0.0
    # Per class.
    assert an["per_class"]["HEADLINE"]["A"]["correct"] == 0.25 and an["per_class"]["HEADLINE"]["B"]["correct"] == 1.0
    assert an["per_class"]["INSUFFICIENT"]["D"]["correct"] == 0.5
    # Primary: B − A = .5 1 .5 .5 0 1 → 3.5 / 6; exact p = 4/64.
    p = an["primary"]
    assert p["n"] == 6 and p["mean_diff"] == approx(3.5 / 6) and p["mean_x"] == approx(5.5 / 6)
    assert p["p"] == 0.0625 and p["test"] == "exact"
    assert 0 < p["ci"][0] <= p["mean_diff"] <= p["ci"][1] <= 1.0
    sec = an["secondary"]
    assert sec["S1"]["correct"]["mean_diff"] == approx(-1 / 6)
    assert sec["S1"]["error_rate"]["mean_x"] == approx(0.5 / 6) and sec["S1"]["error_rate"]["mean_y"] == 0.0
    assert (sec["S2"]["A_vs_D"]["mean_x"], sec["S2"]["A_vs_D"]["mean_y"]) == (0.0, 1.0)
    assert (sec["S2"]["B_vs_D"]["mean_x"], sec["S2"]["B_vs_D"]["mean_y"]) == (0.5, 1.0)
    assert sec["S3"]["correct"]["mean_diff"] == 0.0
    assert sec["S3"]["cost_adjusted"]["E"]["correct_per_usd"] == 4.0
    assert sec["S3"]["cost_adjusted"]["B"]["correct_per_usd"] == 0.5
    assert sec["S4"]["A"]["correct"] == 0.5 and sec["S4"]["B"]["correct"] == 1.0
    assert sec["S6"]["mean_diff"] == approx(2.5 / 6)
    # κ: (1,1) (0,0) (1,0) (0,0) → po .75, pe .5 → .5; the blank row is not counted.
    k = an["agreement"]
    assert (k["operator_rows"], k["operator_filled"], k["n"]) == (5, 4, 4)
    assert k["kappa"] == 0.5 and k["table"] == {"both_1": 1, "primary_1_operator_0": 1,
                                                "primary_0_operator_1": 0, "both_0": 2}
    # §6.3.
    pc = {c["question_id"]: c["reading"] for c in an["packet_ceiling"]}
    assert pc["HE02"].startswith("extraction (N6) hid the answer") and pc["LM01"].startswith("the tower lacks")
    assert pc["HE01"] == "A scored high"
    # §6.4.
    met = {r["rule"]: r["met"] for r in an["decisions"]}
    assert met == {"R1": True, "R2": False, "R3": False, "R4": True, "R5": True, "R6": True}
    md = (d / "report.md").read_text()
    assert "## Primary endpoint — B vs A on correctness" in md and "| 6 | 0.333 | 0.917 | 0.583 |" in md
    assert "Cohen's κ 0.500" in md and "HE09 (gold_missing: x)" in md
    assert "On-demand reasoning over the corpus is the product's better reader." in md
    # Seeded: the same inputs give the same report.
    again = report.build(d)
    assert again["analysis"] == rep["analysis"]


def test_report_runs_on_the_standard_library_alone():
    for name in ("report.py", "grade_run.py", "gold_draft.py", "gold_sign.py"):
        src = (ROOT / "scripts" / "bakeoff" / name).read_text()
        top = set(re.findall(r"^(?:import|from) ([a-z_]+)", src, re.M))
        assert not top & {"numpy", "scipy", "pandas", "httpx", "statsmodels"}, name
    src = (ROOT / "scripts" / "bakeoff" / "report.py").read_text()
    assert set(re.findall(r"^(?:import|from) ([a-z_]+)", src, re.M)) <= {
        "__future__", "argparse", "json", "math", "random", "re", "statistics", "sys", "pathlib", "typing",
        "common", "grade_run", "grade_sheet"}


def test_report_counts_a_grade_error_and_drops_it_from_the_pairs(tmp_path):
    d = _synthetic_report_run(tmp_path)
    g = json.loads((d / "grading" / "grades_primary.json").read_text())
    key = json.loads((d / "sealed" / "blind_key.json").read_text())["items"]
    bid = next(b for b, k in key.items() if (k["arm"], k["question_id"]) == ("B", "IE02"))
    g["items"][bid] = {"grade": None, "grade_error": "no JSON object in the reply", "calls": []}
    (d / "grading" / "grades_primary.json").write_text(json.dumps(g))
    rep = report.build(d)
    assert rep["counts"] == {"answers": 36, "graded": 35, "grade_errors": 1}
    pb = rep["analysis"]["per_arm"]["B"]
    assert pb["grade_errors"] == 1 and pb["correct"] == pytest.approx(4.5 / 5)
    assert rep["analysis"]["primary"]["n"] == 5 and "IE02" not in rep["analysis"]["primary"]["questions"]
