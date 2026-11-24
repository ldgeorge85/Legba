# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E-3 — ``scripts/publish_eval_run.py``, the owner's loader for one evaluation run.

Small INVENTED fixtures, written here by hand, in the three report shapes the
loader reads (run 1's ``legba.bakeoff.report/1``, run 1b's
``legba.bakeoff.report_1b/1``, and the variants report). Proves: the dry run
writes nothing and prints counts, field names and arm-level numbers only —
never a question, a gold answer or a model answer, even though the fixture
reports carry them in their per-answer rows; every arm must be described in
plain words; fields the files lack come from arguments; ``--apply`` writes
through the store with a resolved, active admin/owner actor; withdraw.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from ._eval_runs_fakes import FakeEvalConn

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("_e3_publish_eval_run", REPO / "scripts" / "publish_eval_run.py")
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)

SECRET_Q = "INVENTED QUESTION TEXT: will the fictional river flood?"
SECRET_GOLD = "INVENTED GOLD ANSWER: it flooded on day 3"
SECRET_ANS = "INVENTED MODEL ANSWER: no flood expected"
USER_ID = "0b4c2e0e-1111-4222-8333-944455556666"


def _agg(score: float, n: int = 4, graded: int | None = None) -> dict[str, Any]:
    return {"n": n, "graded": n if graded is None else graded, "correct": score,
            "correct_ci": [round(score - 0.1, 3), round(score + 0.1, 3)], "brier": 0.3}


def _per_class(arms: dict[str, float]) -> dict[str, Any]:
    return {c: {k: _agg(v, n=2) for k, v in arms.items()} for c in ("ALPHA", "BETA")}


def _rows() -> list[dict[str, Any]]:
    return [{"question_id": "ZZ01", "question": SECRET_Q, "gold": SECRET_GOLD, "answer": SECRET_ANS, "arm": "A"}]


def write_run1(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    arms = {"A": 0.25, "B": 0.75}
    (d / "report.json").write_text(json.dumps({
        "schema": "legba.bakeoff.report/1", "seed": 1,
        "analysis": {"per_arm": {k: _agg(v) for k, v in arms.items()}, "per_class": _per_class(arms),
                     "primary": {"metric": "correct", "x": "B", "y": "A"}},
        "answers": _rows(),
        "run": {"run_id": "inv0000run01", "as_of": "2026-03-01T07:00:00+00:00", "design_sha256": "c" * 64,
                "design_version": "1.1", "grader_model_served": "invented-grader"},
        "void_questions": [{"question_id": "ZZ09", "reason": "invented"}],
    }))
    (d / "run.json").write_text(json.dumps({"schema": "legba.bakeoff.run/1", "run_id": "inv0000run01"}))
    return d / "report.json"


def write_run1b(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    arms = {"A": 0.25, "B-open": 0.5, "B-open-2": 0.45}
    (d / "report_1b.json").write_text(json.dumps({
        "schema": "legba.bakeoff.report_1b/1", "arms": list(arms),
        "per_arm": {k: _agg(v) for k, v in arms.items()}, "per_class": _per_class(arms), "rows": _rows(),
        "run1b": {"run_id": "1b-inv000", "as_of": "2026-03-01T07:00:00+00:00", "design_sha256": "d" * 64,
                  "design_version": "1.2", "gold_sha256": "e" * 64},
        "grader_pin": {"primary_grader": {"served_model": "invented-grader-pin"}},
    }))
    (d / "run.json").write_text(json.dumps({"run_id": "1b-inv000", "as_of": "2026-03-01T07:00:00+00:00",
                                            "design_sha256": "d" * 64, "design_version": "1.2",
                                            "grader_model_served": "invented-grader", "gold_sha256": "e" * 64}))
    (d / "report_variants.json").write_text(json.dumps({
        "arms": list(arms), "seed": 2,
        "variants": {"v2": {"per_arm": {k: _agg(v - 0.05) for k, v in arms.items()},
                            "per_class": _per_class(arms), "questions": 4, "comparisons": {}}},
    }))
    return d / "report_1b.json"


def arms_file(tmp: Path, keys: list[str], **extra: dict[str, Any]) -> Path:
    doc = {k: {"label": f"Invented arm {k}", "description": f"An invented way of answering called {k}."} for k in keys}
    for k, v in extra.items():
        doc.setdefault(k, {}).update(v)
    p = tmp / f"arms_{'_'.join(keys)}.json"
    p.write_text(json.dumps(doc))
    return p


def ns(report: Path, arms: Path, **kw: Any) -> argparse.Namespace:
    argv = ["publish", "--report", str(report), "--title", "Invented run", "--temperature", "1.0",
            "--gold-version", "gold v1", "--metric-label", "Share correct", "--ci-level", "0.95",
            "--arms", str(arms)]
    for k, v in kw.items():
        flag = "--" + k.replace("_", "-")
        if v is True:
            argv.append(flag)
        elif isinstance(v, list):
            for x in v:
                argv += [flag, x]
        else:
            argv += [flag, str(v)]
    return pe.parser().parse_args(argv)


async def _never() -> Any:
    raise AssertionError("the dry run opened a database connection")


async def test_run1_dry_run_maps_aggregates_and_prints_no_question_text(tmp_path: Path) -> None:
    report = write_run1(tmp_path / "run1")
    code, out = await pe.run(_never, ns(report, arms_file(tmp_path, ["A", "B"]), caveat=["An invented caveat."]))
    assert code == 0, out
    text = json.dumps(out)
    for secret in (SECRET_Q, SECRET_GOLD, SECRET_ANS, "ZZ01"):
        assert secret not in text
    w = out["would_publish"]
    assert w["run_key"] == "inv0000run01" and w["grader"] == "invented-grader"
    assert w["question_count"] == 4 and w["question_classes"] == {"ALPHA": 2, "BETA": 2}
    assert [(a["arm"], a["score"], a["interval"], a["n"]) for a in w["arms"]] == [
        ("A", "0.250", "0.150..0.350", 4), ("B", "0.750", "0.650..0.850", 4)]
    assert w["caveats"] == 1 and w["deviations"] == 0 and out["problems"] == []
    assert out["noted_in_files_not_published"]["void_questions"] == 1
    assert "arms[].score" in out["fields_stored"]


async def test_run1b_shape_samples_and_grader_and_variants(tmp_path: Path) -> None:
    report = write_run1b(tmp_path / "run1b")
    arms = arms_file(tmp_path, ["A", "B-open", "B-open-2"],
                     **{"B-open": {"sample_of": "B-open", "sample_index": 1},
                        "B-open-2": {"sample_of": "B-open", "sample_index": 2}})
    code, out = await pe.run(_never, ns(report, arms))
    assert code == 0, out
    w = out["would_publish"]
    assert w["run_key"] == "1b-inv000" and w["design"]["version"] == "1.2" and w["grader"] == "invented-grader"
    assert [a["sample"] for a in w["arms"]] == [None, "1 of B-open", "2 of B-open"]
    assert out["noted_in_files_not_published"]["gold_sha256"] == "e" * 64
    assert SECRET_ANS not in json.dumps(out)

    variants = report.parent / "report_variants.json"
    code, out = await pe.run(_never, ns(variants, arms))
    assert code == 1 and "--variant" in out["refused"]
    code, out = await pe.run(_never, ns(variants, arms, variant="v2"))
    assert code == 1 and "--run-key" in out["refused"]
    code, out = await pe.run(_never, ns(variants, arms, variant="v2", run_key="1b-inv000.goldv2"))
    assert code == 0, out
    assert out["would_publish"]["source"]["schema"] == "legba.bakeoff.report_1b.variants#v2"
    assert out["would_publish"]["arms"][0]["score"] == "0.200"


async def test_every_arm_needs_plain_words_and_no_extra_arm(tmp_path: Path) -> None:
    report = write_run1(tmp_path / "run1")
    code, out = await pe.run(_never, ns(report, arms_file(tmp_path, ["A"])))
    assert code == 1 and "B" in out["refused"] and "losses beside wins" in out["refused"]
    code, out = await pe.run(_never, ns(report, arms_file(tmp_path, ["A", "B", "Z"])))
    assert code == 1 and "Z" in out["refused"]
    code, out = await pe.run(_never, ns(report, arms_file(tmp_path, ["A", "B"], A={"description": ""})))
    assert code == 1 and any("plain-words description" in p for p in out["problems"])


async def test_deviation_headings_and_notes_files(tmp_path: Path) -> None:
    md = tmp_path / "DEVIATIONS.md"
    md.write_text("# Invented\n\n## D1: an invented first departure\nbody\n## D2: second\n## D3: third\n")
    notes = tmp_path / "caveats.txt"
    notes.write_text("# comment\nFirst invented caveat.\n\nSecond invented caveat.\n")
    report = write_run1(tmp_path / "run1")
    a = ns(report, arms_file(tmp_path, ["A", "B"]), deviations_md=md, deviation_ids="D1-D2,D3",
           caveats_file=notes, deviation=["An extra invented departure."])
    rec = pe.build_record(pe.read_report(report), a)
    assert rec.deviations == ("D1: an invented first departure", "D2: second", "D3: third",
                              "An extra invented departure.")
    assert rec.caveats == ("First invented caveat.", "Second invented caveat.")
    with pytest.raises(pe.LoadRefusal, match="D9"):
        pe.read_deviation_headings(md, ["D9"])
    with pytest.raises(pe.LoadRefusal):
        pe.expand_ids("D3-D1")
    code, out = await pe.run(_never, ns(report, arms_file(tmp_path, ["A", "B"]), deviation_ids="D1"))
    assert code == 1 and "--deviations-md" in out["refused"]


class _Users(FakeEvalConn):
    def __init__(self, role: str = "operator", status: str = "active") -> None:
        super().__init__()
        self.user = {"id": USER_ID, "role": role, "status": status}

    async def fetchrow(self, sql: str, *a: Any):
        if sql == pe._USER_BY_EMAIL:
            return self.user if a[0] == "owner@example.org" else None
        if sql == pe._USER_BY_ID:
            return self.user if a[0] == USER_ID else None
        return await super().fetchrow(sql, *a)


async def test_apply_publishes_with_a_resolved_admin_and_withdraw(tmp_path: Path) -> None:
    report = write_run1(tmp_path / "run1")
    arms = arms_file(tmp_path, ["A", "B"])
    conn = _Users()

    async def factory():
        return conn

    code, out = await pe.run(factory, ns(report, arms, apply=True))
    assert code == 1 and "--actor" in out["refused"]
    code, out = await pe.run(factory, ns(report, arms, apply=True, actor="Owner@Example.org"))
    assert code == 0, out
    assert out["actor"] == f"user:{USER_ID}" and out["published"]["version"] == 1
    stored = next(iter(conn.runs.values()))
    assert stored["published_by"] == f"user:{USER_ID}"
    assert all(SECRET_Q not in json.dumps(v, default=str) for v in stored.values())

    # a second publish is a correction
    code, out = await pe.run(factory, ns(report, arms, apply=True, actor=f"user:{USER_ID}",
                                         caveat=["A caveat added later."]))
    assert code == 1 and "correction" in out["refused"]
    code, out = await pe.run(factory, ns(report, arms, apply=True, actor=f"user:{USER_ID}",
                                         caveat=["A caveat added later."], correction_reason="a caveat was missing"))
    assert code == 0 and out["published"]["version"] == 2

    w = pe.parser().parse_args(["withdraw", "--run-key", "inv0000run01", "--reason", "invented reason"])
    code, out = await pe.run(_never, w)
    assert code == 0 and out["would_withdraw"]["version"] == "the newest"
    w = pe.parser().parse_args(["withdraw", "--run-key", "inv0000run01", "--reason", "invented reason",
                                "--actor", f"user:{USER_ID}", "--apply"])
    code, out = await pe.run(factory, w)
    assert code == 0 and out["withdrawn"]["version"] == 2


@pytest.mark.parametrize("role,status", [("read", "active"), ("operator", "disabled")])
async def test_apply_refuses_an_actor_who_is_not_an_active_admin(tmp_path: Path, role: str, status: str) -> None:
    conn = _Users(role=role, status=status)

    async def factory():
        return conn

    code, out = await pe.run(factory, ns(write_run1(tmp_path / "r"), arms_file(tmp_path, ["A", "B"]),
                                         apply=True, actor="owner@example.org"))
    assert code == 1 and "not an active admin/owner" in out["refused"]
    assert conn.runs == {}
