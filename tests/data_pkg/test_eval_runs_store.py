# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E-3 — the published evaluation record's store (``legba.data.eval_runs``).

Over the in-memory fake (the production functions answer it): validation,
the seal, publish → version 1, a correction → version 2 superseding it (both
kept), the identical republish that writes nothing, a withdrawal once, and
the route's read model (status in words, superseded_by derived). Invented
rows only. The DB half (the 0260 guard) is ``test_migration_0260_eval_runs.py``.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from legba.data import eval_runs as er

from ._eval_runs_fakes import FakeEvalConn, arm, run

ACTOR = "user:0b4c2e0e-1111-4222-8333-944455556666"


def test_the_worktree_import_path() -> None:
    here = Path(__file__).resolve().parents[2]
    assert Path(er.__file__).resolve().is_relative_to(here / "src")


def test_no_field_could_carry_a_question_or_an_answer() -> None:
    run_fields = {f.name for f in dataclasses.fields(er.EvalRunRecord)}
    arm_fields = {f.name for f in dataclasses.fields(er.EvalArmRecord)}
    for name in run_fields | arm_fields:
        assert not any(w in name for w in ("answer", "gold_text", "question_text", "prompt", "rows")), name
    assert "questions" not in run_fields and "answers" not in run_fields


def test_validation_names_every_problem() -> None:
    assert er.run_problems(run()) == []
    bad = run(run_key="no spaces", title="", design_sha256="x", temperature=3.0, gold_versions=(),
              arms=(arm("A", 0.4, description=""), arm("A", 0.5)))
    p = "\n".join(er.run_problems(bad))
    for frag in ("run_key", "title", "design_sha256", "temperature", "gold_versions",
                 "plain-words description", "each arm_key once"):
        assert frag in p, frag
    assert any("sample_of" in x for x in er.run_problems(run(arms=(arm("A", 0.4, sample_of="Z", sample_index=1),))))
    assert any("interval" in x for x in er.run_problems(run(arms=(arm("A", 0.4, ci_low=0.5, ci_high=0.1),))))
    assert any("purpose" in x for x in er.run_problems(run(purpose="x" * 301)))


def test_the_seal_is_stable_and_covers_every_arm() -> None:
    a, b = run(), run()
    assert er.content_sha256(a) == er.content_sha256(b)
    assert er.content_sha256(run(arms=(arm("A", 0.4), arm("B", 0.61)))) != er.content_sha256(a)


async def test_publish_correct_and_withdraw() -> None:
    conn = FakeEvalConn()
    first = await er.publish(conn, run(), actor=ACTOR)
    assert first["written"] and first["version"] == 1 and first["supersedes_id"] is None
    assert [x["arm_key"] for x in conn.arms] == ["A", "B"]

    # the identical content again writes nothing
    again = await er.publish(conn, run(), actor=ACTOR)
    assert again == {"written": False, "id": first["id"], "version": 1, "content_sha256": first["content_sha256"]}

    # a change is a correction: refused without a reason, a new version with one
    changed = run(arms=(arm("A", 0.4), arm("B", 0.55)))
    with pytest.raises(er.EvalRunRefusal, match="correction"):
        await er.publish(conn, changed, actor=ACTOR)
    second = await er.publish(conn, changed, actor=ACTOR, correction_reason="arm B's score was transcribed wrong")
    assert second["version"] == 2 and second["supersedes_id"] == first["id"]
    assert len(conn.runs) == 2  # both versions kept

    with pytest.raises(er.EvalRunRefusal, match="nothing|no published version"):
        await er.publish(conn, run("inv-new"), actor=ACTOR, correction_reason="nothing to correct")
    with pytest.raises(er.EvalRunRefusal, match="actor"):
        await er.publish(conn, run("inv-002"), actor="operator")

    w = await er.withdraw(conn, "inv-001", actor=ACTOR, reason="the gold set was found to be wrong")
    assert w["version"] == 2
    with pytest.raises(er.EvalRunRefusal, match="already withdrawn"):
        await er.withdraw(conn, "inv-001", actor=ACTOR, reason="again")
    with pytest.raises(er.EvalRunRefusal, match="reason"):
        await er.withdraw(conn, "inv-001", actor=ACTOR, reason=" ", version=1)
    with pytest.raises(er.EvalRunRefusal, match="no published run"):
        await er.withdraw(conn, "nope", actor=ACTOR, reason="r")


async def test_the_route_model_marks_superseded_and_withdrawn_in_words() -> None:
    conn = FakeEvalConn()
    v1 = await er.publish(conn, run(), actor=ACTOR)
    v2 = await er.publish(conn, run(arms=(arm("A", 0.4), arm("B", 0.5))), actor=ACTOR, correction_reason="fixed")
    await er.publish(conn, run("inv-002", title="Withdrawn invented run"), actor=ACTOR)
    await er.withdraw(conn, "inv-002", actor=ACTOR, reason="published in error")
    rows = await er.fetch_runs(conn)
    by = {(r["run_key"], r["version"]): r for r in rows}
    assert by[("inv-001", 2)]["status"] == "current" and by[("inv-001", 2)]["supersedes"] == v1["id"]
    assert by[("inv-001", 1)]["status"] == "superseded" and by[("inv-001", 1)]["superseded_by"] == v2["id"]
    gone = by[("inv-002", 1)]
    assert gone["status"] == "withdrawn" and gone["withdrawn_reason"] == "published in error"
    cur = by[("inv-001", 2)]
    assert [a["id"] for a in cur["arms"]] == ["A", "B"]
    res = cur["results"][1]
    assert res["score"] == 0.5 and res["n"] == 12 and res["ci_level"] == 0.95
    assert res["by_class"] == [{"class": "ALPHA", "score": 0.5, "n": 6}, {"class": "BETA", "score": 0.5, "n": 6}]
    assert cur["question_classes"] == [{"class": "ALPHA", "n": 6}, {"class": "BETA", "n": 6}]
    assert cur["caveats"] == ["An invented caveat."] and cur["deviations"] == ["D1: an invented departure"]
    assert await er.fetch_runs(FakeEvalConn()) == []
