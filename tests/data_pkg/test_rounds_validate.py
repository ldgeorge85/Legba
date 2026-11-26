# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round-spec format and the validator's refusal rules.

One planted violation per rule, each refused with its named reason and the
path in the spec; the base spec passes; the closed grammar refuses free
expressions; the worst-case calculator is checked by hand on the base spec;
random bounded trees always produce a verdict (accept or named refusals).
Pure — no database, no model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from legba.data.rounds.refs import GrammarError, parse_predicate, parse_ref
from legba.data.rounds.spec import load_yaml, parse_spec
from legba.data.rounds.validate import main as validate_main
from legba.data.rounds.validate import validate

from ._rounds_helpers import SECOND, TreeGen, base_spec, judge, model, mutate, write_cards


def _run(raw: dict[str, Any], tmp_path: Path):
    write_cards(tmp_path)
    spec, refusals = parse_spec(raw)
    if spec is None:
        return refusals, None
    report = validate(spec, card_root=tmp_path)
    return report.refusals, report


def _reasons(raw: dict[str, Any], tmp_path: Path) -> dict[str, list[str]]:
    refusals, _ = _run(raw, tmp_path)
    out: dict[str, list[str]] = {}
    for r in refusals:
        out.setdefault(r.reason, []).append(r.path)
        assert r.path or r.reason == "schema_invalid", r
        assert r.rule in {f"V{i}" for i in range(1, 13)} | {"schema"}
    return out


def test_base_spec_is_accepted_with_a_hand_checked_worst_case(tmp_path):
    refusals, report = _run(base_spec(), tmp_path)
    assert refusals == []
    w = report.worst
    # freeze(0) + blind 3 models + 3 judges + editor = 7 model calls, no retries
    assert w.calls == 7
    # 10 + min(120, 180) + 10 + min(120, 180) + 60
    assert w.seconds == 10 + 120 + 10 + 120 + 60
    assert w.peak == 3
    assert report.spec.sha256 and len(report.spec.sha256) == 64


# --------------------------------------------------------------------------
# One planted violation per rule
# --------------------------------------------------------------------------


def _planted() -> dict[str, tuple[str, Any]]:
    """reason -> (rule, raw spec with exactly that violation planted)."""
    cases: dict[str, tuple[str, Any]] = {}

    s = mutate(base_spec())  # V1: a shape without its bound
    del s["flow"]["seq"][1]["deadline_s"]
    cases["bound_missing"] = ("V1", s)

    s = mutate(base_spec())  # V1: a bound above the caps
    s["flow"]["seq"][4] = {"loop": {"body": {"seat": "editor"}, "max_iterations": 9,
                                    "model_calls": 9}, "id": "again"}
    cases["bound_above_cap"] = ("V1", s)

    s = mutate(base_spec())  # V1: a branch without a default
    s["flow"]["seq"][4] = {"branch": {"cases": [{"when": "tally.judge.outcome == stands",
                                                 "do": {"seat": "editor"}}]}, "id": "fork"}
    cases["branch_no_default"] = ("V1", s)

    s = mutate(base_spec())  # V2: a phase that cannot close by its own rule
    s["flow"]["seq"][1]["min_present"] = 4
    cases["cannot_terminate"] = ("V2", s)

    s = mutate(base_spec())  # V3: a blind seat reads a sibling directly
    s["seats"]["b"] = model(reads=[{"from": "a"}])
    cases["blindness_leak"] = ("V3", s)

    s = mutate(base_spec())  # V3: a withheld field reachable through another seat
    s["seats"]["lead"] = model(output="question_set/1")
    s["seats"]["relay"] = model(reads=[{"from": "lead"}])
    s["seats"]["a"] = model(reads=[{"from": "lead"}, {"from": "relay"}],
                            withheld=["lead.working_reading"])
    s["flow"]["seq"][1:1] = [{"seat": "lead"}, {"seat": "relay"}]
    cases["withheld_reachable"] = ("V3", s)

    s = mutate(base_spec())  # V4: a read of something not produced on every path
    s["seats"]["maybe"] = model()
    s["flow"]["seq"][4:4] = [{"branch": {"cases": [{"when": "tally.judge.outcome == tie",
                                                     "do": {"seat": "maybe"}}],
                                         "default": {"seat": "freeze2"}}, "id": "fork"}]
    s["seats"]["freeze2"] = mutate(s["seats"]["freeze"])
    s["seats"]["editor"]["reads"].append({"from": "maybe"})
    cases["read_not_available"] = ("V4", s)

    s = mutate(base_spec())  # V5: a judge on the family of a seat it judges
    s["seats"]["j1"]["plane"] = "llm.primary.openai_compat"
    s["seats"]["j2"] = judge()
    s["seats"]["j3"] = judge()
    cases["judge_same_family"] = ("V5", s)

    s = mutate(base_spec())  # V5: another family exists and the judge declares no rule
    s["seats"]["j1"] = judge(family_rule="any")
    s["seats"]["j2"] = judge()
    s["seats"]["j3"] = judge()
    cases["judge_family_rule_missing"] = ("V5", s)

    s = mutate(base_spec())  # V6: a judge reading prose
    s["seats"]["j1"]["reads"] = [{"from": "table", "view": "prose"}]
    cases["judge_reads_prose"] = ("V6", s)

    s = mutate(base_spec())  # V7: an even panel
    s["flow"]["seq"][3]["panel"]["judges"] = ["j1", "j2"]
    cases["panel_even"] = ("V7", s)

    s = mutate(base_spec())  # V7: a quorum the panel cannot reach
    s["flow"]["seq"][3]["panel"]["tally"] = {"quorum": {"k": 1, "of": 3, "threshold": 0.6}}
    cases["quorum_unreachable"] = ("V7", s)

    s = mutate(base_spec())  # V8: an unregistered gate
    s["seats"]["a"]["gates"] = ["quotes_ok"]
    cases["unknown_reference"] = ("V8", s)

    s = mutate(base_spec())  # V9: budgets that cannot cover the worst case
    s["budgets"]["model_calls"] = 5
    cases["budget_below_worst_case"] = ("V9", s)

    s = mutate(base_spec())  # V9: a budget counted in tokens
    s["budgets"]["max_tokens"] = 4096
    cases["token_budget_refused"] = ("V9", s)

    s = mutate(base_spec())  # V10: publishes to readers without the operator's allowance
    s["publishes"] = "reader"
    cases["publishes_reader_not_allowed"] = ("V10", s)

    s = mutate(base_spec())  # V11: a shape declared but not built
    s["flow"]["seq"][4] = {"use_round": {"spec": "other_round", "version": 1}, "id": "sub"}
    cases["shape_not_built"] = ("V11", s)

    s = mutate(base_spec())  # V12: a tool that could read this run's rows
    s["seats"]["a"]["tools"] = ["web_access"]
    cases["tool_not_run_cut"] = ("V12", s)
    return cases


@pytest.mark.parametrize("reason", sorted(_planted()))
def test_planted_violation_is_refused_with_its_reason(reason, tmp_path):
    rule, raw = _planted()[reason]
    refusals, _ = _run(raw, tmp_path)
    hits = [r for r in refusals if r.reason == reason]
    assert hits, f"expected {reason}; got {[str(r) for r in refusals]}"
    assert all(r.rule == rule for r in hits)
    assert all(r.path for r in hits)


def test_every_rule_has_a_planted_case():
    rules = {rule for rule, _ in _planted().values()}
    assert rules == {f"V{i}" for i in range(1, 13)}


# --------------------------------------------------------------------------
# More refusals, each with its path
# --------------------------------------------------------------------------


def test_indirect_blindness_leak_through_an_earlier_seat_of_the_same_phase(tmp_path):
    s = mutate(base_spec())
    s["seats"]["p"] = model(reads=[{"from": "c"}])
    s["seats"]["q"] = model(reads=[{"from": "p"}])
    s["flow"]["seq"][1]["blind"] = [{"seq": [{"seat": "p"}, {"seat": "q"}], "id": "pq"},
                                    {"seat": "c"}]
    s["flow"]["seq"][1]["min_present"] = 1
    got = _reasons(s, tmp_path)
    assert "seats.q.reads" in got["blindness_leak"]  # q -> p -> c
    assert "seats.p.reads" in got["blindness_leak"]


def test_reading_the_open_blind_phase_from_inside_is_a_leak(tmp_path):
    s = mutate(base_spec())
    s["seats"]["a"] = model(reads=[{"from": "open.others"}])
    assert "seats.a.reads[0]" in _reasons(s, tmp_path)["blindness_leak"]


def test_judges_never_read_each_other_or_rows(tmp_path):
    s = mutate(base_spec())
    s["seats"]["j2"] = judge(reads=[{"from": "table", "view": "rows"}])
    got = _reasons(s, tmp_path)
    assert "judge_view_not_sentence_table" in got
    s = mutate(base_spec())
    s["seats"]["j0"] = judge()
    s["flow"]["seq"].insert(3, {"seat": "j0"})
    s["seats"]["j2"] = judge(reads=[{"from": "j0", "view": "sentence_table"}])
    assert "judge_reads_judge" in _reasons(s, tmp_path)


def test_judge_output_must_be_a_verdict(tmp_path):
    s = mutate(base_spec())
    s["seats"]["j1"]["output"] = "position/1"
    assert "judge_output_not_verdict" in _reasons(s, tmp_path)


def test_only_one_family_warns_instead_of_refusing(tmp_path):
    s = mutate(base_spec())
    for j in ("j1", "j2", "j3"):
        s["seats"][j] = judge(plane="llm.primary.openai_compat")
    refusals, report = _run(s, tmp_path)
    assert refusals == []
    assert any("V5" in w for w in report.warnings)


def test_declaring_another_family_makes_the_same_family_judge_a_refusal(tmp_path):
    s = mutate(base_spec())
    for j in ("j1", "j2", "j3"):
        s["seats"][j] = judge(plane="llm.primary.openai_compat")
    s["families_available"] = ["google_gemma"]
    assert "judge_same_family" in _reasons(s, tmp_path)


@pytest.mark.parametrize("field,value,reason", [
    ("card", "cards/missing.md", "unknown_reference"),
    ("plane", "llm.nowhere", "unknown_reference"),
    ("output", "nothing/1", "unknown_reference"),
    ("tools", ["no_such_pack"], "unknown_reference"),
])
def test_unknown_names_are_refused(field, value, reason, tmp_path):
    s = mutate(base_spec())
    s["seats"]["a"][field] = value
    assert "seats.a" in "".join(_reasons(s, tmp_path)[reason])


def test_code_seat_needs_a_registered_function_writing_its_output(tmp_path):
    s = mutate(base_spec())
    s["seats"]["table"]["fn"] = "no_such_fn"
    assert "unknown_reference" in _reasons(s, tmp_path)
    s = mutate(base_spec())
    s["seats"]["table"]["output"] = "position/1"
    assert "code_output_mismatch" in _reasons(s, tmp_path)


@pytest.mark.parametrize("plant", [
    lambda s: s["seats"].__setitem__("h", {"kind": "human", "reads": [], "output": "read/1",
                                           "role": "contributor"}),
    lambda s: s["seats"].__setitem__("b", {"seat_ref": "seats/shared_reader/3"}),
    lambda s: s.__setitem__("prior_runs", "sealed_before_as_of"),
])
def test_declared_but_unbuilt_parts_are_refused_loudly(plant, tmp_path):
    s = mutate(base_spec())
    plant(s)
    assert "shape_not_built" in _reasons(s, tmp_path)


def test_seat_ref_cycle_cannot_terminate(tmp_path):
    s = mutate(base_spec())
    s["seats"]["b"] = {"seat_ref": "c"}
    s["seats"]["c"] = {"seat_ref": "b"}
    assert "cannot_terminate" in _reasons(s, tmp_path)


def test_par_quorum_above_children_cannot_terminate(tmp_path):
    s = mutate(base_spec())
    s["flow"]["seq"][1] = {"par": [{"seat": "a"}, {"seat": "b"}], "id": "open",
                           "join": {"quorum": 3}, "deadline_s": 60}
    s["seats"]["c"] = model()
    s["flow"]["seq"].insert(2, {"seat": "c"})
    assert "cannot_terminate" in _reasons(s, tmp_path)


def test_seconds_budget_below_worst_case(tmp_path):
    s = mutate(base_spec())
    s["budgets"]["wall_s"] = 100
    got = _reasons(s, tmp_path)
    assert got["budget_below_worst_case"] == ["budgets.wall_s"]


def test_token_named_key_anywhere_is_refused(tmp_path):
    s = mutate(base_spec())
    s["seats"]["a"]["max_tokens"] = 100
    assert "seats.a.max_tokens" in _reasons(s, tmp_path)["token_budget_refused"]


def test_seat_placed_twice_and_duplicate_phase_ids(tmp_path):
    s = mutate(base_spec())
    s["flow"]["seq"].append({"seat": "editor"})
    assert "seat_placed_twice" in _reasons(s, tmp_path)
    s = mutate(base_spec())
    s["flow"]["seq"][3]["id"] = "open"
    assert "duplicate_id" in _reasons(s, tmp_path)


def test_rebuttal_must_rebut_a_blind_phase_of_seats(tmp_path):
    s = mutate(base_spec())
    s["flow"]["seq"].insert(2, {"rebuttal": {"of": "main", "passes": 1}, "id": "rebut"})
    assert "rebuttal_of_invalid" in _reasons(s, tmp_path)


def test_missing_phase_id_and_unknown_keys_are_schema_refusals(tmp_path):
    s = mutate(base_spec())
    del s["flow"]["seq"][1]["id"]
    assert "phase_id_missing" in _reasons(s, tmp_path)
    s = mutate(base_spec())
    s["flow"]["seq"][1]["timeout"] = 5
    assert "schema_invalid" in _reasons(s, tmp_path)


# --------------------------------------------------------------------------
# The closed grammar
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "count(pass.assessment/1 where diagnostic == true) >= 1",
    "tally.judge.outcome == tie",
    "not gate.adjudicator.passed",
    "gate.adjudicator.handed_rows.passed",
    "unchanged(rebut)",
    "budget.remaining.calls < 4",
    "phase.open.status == invalid",
])
def test_closed_predicates_parse(text):
    assert parse_predicate(text).raw == text


@pytest.mark.parametrize("text", [
    "len(open) > 2",
    "tally.judge.outcome == stands and gate.a.passed",
    "__import__('os')",
    "count(pass.assessment/1) >= 1 + 1",
    "not tally.judge.outcome == tie",
])
def test_free_expressions_are_refused(text):
    with pytest.raises(GrammarError):
        parse_predicate(text)


def test_free_expression_in_a_spec_is_a_named_refusal(tmp_path):
    s = mutate(base_spec())
    s["flow"]["seq"][4] = {"loop": {"body": {"seat": "editor"}, "max_iterations": 2,
                                    "model_calls": 2, "until": "eval('1')"}, "id": "again"}
    assert "predicate_invalid" in _reasons(s, tmp_path)


def test_refs():
    assert parse_ref("open.others").kind == "others"
    assert parse_ref("lead.working_reading").field == "working_reading"
    assert parse_ref("item.hypothesis").kind == "item"
    with pytest.raises(GrammarError):
        parse_ref("a.b.c")


def test_yaml_on_off_mean_on_off_not_booleans():
    raw = load_yaml("thinking: on\npanel: {on: verify}\nflag: true\n")
    assert raw == {"thinking": "on", "panel": {"on": "verify"}, "flag": True}


# --------------------------------------------------------------------------
# Random bounded trees
# --------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(60))
def test_random_bounded_trees_get_a_verdict(seed, tmp_path):
    raw = TreeGen(seed).spec()
    refusals, report = _run(raw, tmp_path)
    assert all(r.reason and r.path is not None for r in refusals)
    if report is not None and report.ok:
        w = report.worst
        assert 0 <= w.peak <= raw["budgets"]["max_parallel"]
        assert w.calls <= 400 and w.seconds <= 7200


def test_cli_prints_refusals_and_exit_code(tmp_path, capsys):
    import yaml
    write_cards(tmp_path)
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump({"round": base_spec()}), encoding="utf-8")
    bad_raw = base_spec()
    bad_raw["seats"]["j1"]["plane"] = "llm.primary.openai_compat"
    bad_raw["seats"]["j2"] = judge()
    bad_raw["seats"]["j3"] = judge()
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(bad_raw), encoding="utf-8")
    assert validate_main([str(good)]) == 0
    assert "worst case: 7 calls" in capsys.readouterr().out
    assert validate_main([str(bad)]) == 1
    assert "judge_same_family" in capsys.readouterr().out
    assert SECOND  # the base spec's judges sit on the second family


def test_catalog_families_agree_with_the_grader_fence_map():
    """One source of truth for model families: every plane the round catalog
    names carries the family the grader fence declares for it."""
    from legba.data.rounds.catalog import default_catalog
    from legba.runtime.external_grader_route import GRADER_FAMILY_BY_COMPONENT

    for plane, family in default_catalog().planes.items():
        assert GRADER_FAMILY_BY_COMPONENT.get(plane) == family, plane


def test_rounds_package_is_not_imported_by_the_registry():
    """The registry runs the slim image; nothing under data/registry may
    import the rounds package until it is proven slim-safe."""
    root = Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "registry"
    offenders = [p.name for p in root.rglob("*.py") if "rounds" in p.read_text(encoding="utf-8")
                 and ("data.rounds" in p.read_text(encoding="utf-8")
                      or "from ..rounds" in p.read_text(encoding="utf-8"))]
    assert offenders == []
