# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run 1b — the diagnostic arms (``arm_open.py``) and their grading (``grade_1b.py``).

Fakes only: no registry, no gateway, no database. Pinned here:

* B-open is arm B's actor request with ONLY the plane override changed (the
  free core plane), and the meter never binds;
* the C-record binding refuses every tool outside the structured-record set and
  dispatches the rest through the production router;
* the record prompt names exactly the record tools and no raw reader;
* the consult FINAL sentinel is stripped from a record, never anything else;
* the tool census counts calls, failures and blocks;
* the 1b tell test draws 6 per letter A–G, never an answer an earlier key opened,
  and its parser accepts only A–G.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "bakeoff"))
sys.path.insert(0, str(ROOT / "src"))

import arm_open  # noqa: E402
import grade_1b  # noqa: E402
import run_arm  # noqa: E402

T = datetime(2026, 11, 5, 7, tzinfo=timezone.utc)
Q = SimpleNamespace(qid="HE01", klass="HEADLINE", desk="country_g20_ir", text="What happened?")


def test_b_open_is_arm_b_with_only_the_plane_swapped():
    b = run_arm.consult_invoke("B", Q, T, hard_stop=5.0, price_per_m={"input": 4.0})
    bo = arm_open.b_open_invoke(Q, T)
    fb, fo = b["inputs"][0], bo["inputs"][0]
    assert fo["llm_component_override"] == "llm.primary.openai_compat"
    assert fb["llm_component_override"] == "llm.anthropic.opus_4_7"
    for k in ("question", "scope_predicate", "max_tool_rounds", "mode", "messages"):
        assert fo[k] == fb[k]
    assert fo["max_tool_rounds"] == 10 and fo["mode"] == "chat"
    assert fo["bakeoff_meter"]["hard_stop_usd"] >= 1000.0


def test_record_binding_refuses_outside_the_record():
    calls = []

    class Port:
        async def query_facts(self, **kw):
            calls.append(kw)
            return {"rows": [], "count": 0}

    b = arm_open.RecordBinding(Port())
    out = asyncio.run(b.run_tool("search_corpus", {"query": "x", "scope_predicate": None}))
    assert out.admitted is False and out.block_cause == "not_in_pack"
    out = asyncio.run(b.run_tool("read_document", {"doc_id": "x"}))
    assert out.admitted is False
    out = asyncio.run(b.run_tool("query_facts", {"subject": "Iran", "scope_predicate": "p"}))
    assert out.admitted is True and out.tool_result.status == "ok"
    assert calls and calls[0]["subject"] == "Iran"
    assert b.pack.identity.id == arm_open.RECORD_PACK


def test_record_prompt_names_only_record_tools():
    p = arm_open.record_system_prompt()
    cat = p[p.index("Available tools:"):p.index("OUTPUT CONTRACT")]
    for t in arm_open.RECORD_TOOLS:
        assert f"  - {t}(" in cat
    for raw in ("search_corpus(", "search_signals(", "read_document(", "vector_search(", "get_timeline("):
        assert raw not in cat
    assert "list_findings first" not in p


def test_final_sentinel_is_stripped_only_at_the_end():
    assert arm_open.FINAL_SENTINEL_RE.sub("", "answer\n\n<<<FINAL>>>\n") == "answer"
    assert arm_open.FINAL_SENTINEL_RE.sub("", "a <<<FINAL>>> b") == "a <<<FINAL>>> b"


def test_tool_census():
    steps = [{"kind": "tool_call", "tool": "a", "ok": True, "admitted": True},
             {"kind": "tool_call", "tool": "a", "ok": False, "admitted": True},
             {"kind": "tool_call", "tool": "b", "admitted": False},
             {"kind": "llm_call"}]
    c = arm_open.tool_census(steps)
    assert (c["calls"], c["failed"], c["blocked"]) == (3, 1, 1)
    assert c["by_tool"]["a"] == {"calls": 2, "failed": 1, "blocked": 0}


def test_tell_parser_takes_seven_letters_only():
    assert grade_1b.parse_tell_1b('{"arm": "g"}') == ("G", [])
    assert grade_1b.parse_tell_1b("F") == ("F", [])
    assert grade_1b.parse_tell_1b('{"arm": "H"}')[0] is None
    assert "seven systems" in grade_1b.TELL_PROMPT_1B and "F or G" in grade_1b.TELL_PROMPT_1B


def test_build_tell_draws_six_per_letter_and_skips_opened(tmp_path, monkeypatch):
    classes = ("HEADLINE", "LONG-MEMORY", "SLOW-CHANGE", "INSUFFICIENT")
    recs1, recs1b = [], []
    for arm in "ABCDE":
        for i in range(12):
            recs1.append({"arm": arm, "class": classes[i % 4], "question_id": f"Q{i:02d}", "status": "answered",
                          "raw_answer": f"text {arm} {i}", "receipts": [], "stated_confidence": 0.5})
    for arm in ("B-open", "C-record"):
        for i in range(12):
            recs1b.append({"arm": arm, "class": classes[i % 4], "question_id": f"Q{i:02d}", "status": "answered",
                           "raw_answer": f"text {arm} {i}", "receipts": [], "stated_confidence": 0.5})
    monkeypatch.setattr(grade_1b, "_records", lambda d: recs1 if d.name == "run1" else recs1b)
    monkeypatch.setattr(grade_1b, "_opened", lambda d: {("Q00", "A"), ("Q01", "A")})
    monkeypatch.setattr(grade_1b.normalise, "normalise", lambda r: {"text": r["raw_answer"]})
    dz = SimpleNamespace(sha256="x", arm_titles={a: a for a in "ABCDE"})
    test, key = grade_1b.build_tell(dz, tmp_path / "run1", tmp_path / "run1b", seed=7)
    assert len(test["items"]) == 42 and len(test["arms"]) == 7
    letters = [v["arm"] for v in key["items"].values()]
    assert all(letters.count(x) == 6 for x in "ABCDEFG")
    assert not any(v["arm"] == "A" and v["question_id"] in ("Q00", "Q01") for v in key["items"].values())


def test_build_tell_draws_f_from_another_run_dir_and_skips_its_opened(tmp_path, monkeypatch):
    """E2: an M1 record-only run has no B-open; letter F comes from run 1b's
    directory, minus the answers run 1b's own tell key opened."""
    classes = ("HEADLINE", "LONG-MEMORY", "SLOW-CHANGE", "INSUFFICIENT")

    def recs(arms):
        return [{"arm": arm, "class": classes[i % 4], "question_id": f"Q{i:02d}", "status": "answered",
                 "raw_answer": f"text {arm} {i}", "receipts": [], "stated_confidence": 0.5}
                for arm in arms for i in range(12)]

    by_dir = {"run1": recs("ABCDE"), "run1b": recs(["B-open", "C-record"]), "m1": recs(["C-record", "C-record-2"])}
    monkeypatch.setattr(grade_1b, "_records", lambda d: by_dir[d.name])
    monkeypatch.setattr(grade_1b, "_opened", lambda d: set())
    monkeypatch.setattr(grade_1b, "_opened_1b", lambda d: {(f"Q{i:02d}", "B-open") for i in range(4)})
    monkeypatch.setattr(grade_1b.normalise, "normalise", lambda r: {"text": r["raw_answer"]})
    dz = SimpleNamespace(sha256="x", arm_titles={a: a for a in "ABCDE"})
    test, key = grade_1b.build_tell(dz, tmp_path / "run1", tmp_path / "m1", seed=7, f_from=tmp_path / "run1b")
    items = list(key["items"].values())
    assert len(items) == 42
    f = [v for v in items if v["arm"] == "F"]
    assert len(f) == 6 and all(v["arm_label"] == "B-open" and v["question_id"] >= "Q04" for v in f)
    assert all(v["arm_label"] == "C-record" for v in items if v["arm"] == "G")
    assert "C-record-2" in grade_1b.NEW_ARMS
