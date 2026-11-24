#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run 1b — the tell test with the ADDED arms, the grading, and the joint report.

Run 1b (``arm_open.py``) answers the same questions with arms B-open (and its
second sample B-open-2) and C-record. Everything grader-facing is run 1's:

  * normaliser v1.2 (``normalise.py``, unchanged), primary grader
    ``lightning-think`` with its pin RE-CHECKED against run 1's pin (any drift
    in served model, alias, component version or ``/v1/models`` entry refuses);
  * gold = run 1's ``gold.json`` byte for byte (sha256 checked);
  * the grade prompt = ``grade_run.GRADE_PROMPT`` (its sha256 must equal run 1's).

``tell-test`` — the added arms change the arm set, so the tell test is re-run on a
NEW seed (written down before sampling, ``--seed``) over 6 answers per arm,
stratified by class as ``grade_sheet.build_tell_test`` does, for the seven arms
A–E (run 1's records, minus every answer either earlier tell key opened) plus
F = B-open and G = C-record. The prompt is run 1's tell prompt with the arm
list widened to seven letters (its own sha256). Pass: accuracy ≤ 35 %. A FAIL
does not stop grading here (run 1b is a diagnostic extension, not a
confirmatory run): the result is marked ``UNBLINDED-RISK`` in run.json and the
report.

``grade`` — ``grade_sheet.py sheet`` builds run 1b's blind sheet (its records
are only the added arms); this grades it with ``grade_run.run_grades`` (same
prompt, same retry-once rule) at concurrency ≤ 4.

``report`` — run 1's graded rows (its sheet + opened key + grades + records, read
only) joined with run 1b's, the same ``report.summarise`` / ``report.compare``
code, plus tool-call, served-model, wall-time, token and post-T tables. Writes
``report_1b.json`` / ``report_1b.md`` where ``--out`` says.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import grade_run  # noqa: E402
import grade_sheet  # noqa: E402
import normalise  # noqa: E402
import report  # noqa: E402

#: Tell-test letters: run 1's five, then the added arms.
TELL_LETTERS = {"A": "A", "B": "B", "C": "C", "D": "D", "E": "E", "F": "B-open", "G": "C-record"}
ADDED_TITLES = {
    "F": "a free self-hosted model's consult over the RAW corpus",
    "G": "a free self-hosted model's consult over the platform's structured record "
         "(findings, facts, events, series)",
}
TELL_PROMPT_1B = grade_run.TELL_PROMPT.replace("five systems", "seven systems").replace(
    '"<A, B, C, D or E>"', '"<A, B, C, D, E, F or G>"')
TELL_PROMPT_1B_SHA256 = grade_run.prompt_sha(TELL_PROMPT_1B, grade_run.RETRY_NOTE)
NEW_ARMS = ("B-open", "B-open-2", "C-record", "C-record-2")


def _load(p: Path) -> Any:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _records(run_dir: Path) -> list[dict[str, Any]]:
    return grade_sheet.load_records(run_dir)


def _opened(run1: Path) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for k in ("tell_key.json", "tell_key_2.json"):
        p = run1 / "sealed" / k
        if p.is_file():
            out |= {(v["question_id"], v["arm"]) for v in _load(p)["items"].values()}
    return out


def _opened_1b(run_dir: Path) -> set[tuple[str, str]]:
    """The (question, arm) answers an earlier run-1b tell key opened."""
    p = run_dir / "sealed" / "tell_key_1b.json"
    if not p.is_file():
        return set()
    return {(v["question_id"], v["arm_label"]) for v in _load(p)["items"].values()}


def build_tell(dz: Any, run1: Path, run1b: Path, seed: int,
               f_from: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """6 answers per letter A–G, stratified 2/2/1/1 by class, seeded.

    ``f_from`` (lane E2): the run directory letter F (B-open) is drawn from,
    when the run being tested has no B-open of its own (an M1 record-only
    baseline). Answers an earlier run-1b tell key in that directory opened are
    not drawn again; the prompt, the letters and the arm descriptions are run
    1b's, so the prompt sha256 is unchanged."""
    rng = random.Random(seed)
    excl = _opened(run1)
    f_dir = f_from or run1b
    if f_dir != run1b:
        excl |= _opened_1b(f_dir)
    recs = [r for r in _records(run1) if r["arm"] in "ABCDE" and len(r["arm"]) == 1] + \
        [r for r in _records(f_dir) if r["arm"] == "B-open"] + \
        [r for r in _records(run1b) if r["arm"] == "C-record"]
    chosen: list[tuple[str, dict[str, Any]]] = []
    for letter, arm in TELL_LETTERS.items():
        pool = {c: sorted((r for r in recs if r["arm"] == arm and r.get("class") == c
                           and r.get("status") in ("answered", "partial")
                           and (r["question_id"], r["arm"]) not in excl),
                          key=lambda r: r["question_id"]) for c in grade_sheet.CLASS_ORDER}
        for c in pool:
            rng.shuffle(pool[c])
        picked: list[dict[str, Any]] = []
        while len(picked) < grade_sheet.TELL_PER_ARM and any(pool.values()):
            for c in grade_sheet.CLASS_ORDER:
                if pool[c] and len(picked) < grade_sheet.TELL_PER_ARM:
                    picked.append(pool[c].pop())
        if len(picked) < grade_sheet.TELL_PER_ARM:
            raise SystemExit(f"arm {arm}: only {len(picked)} answers for the tell test")
        chosen += [(letter, r) for r in picked]
    ids = grade_sheet._ids(rng, len(chosen))  # noqa: SLF001
    order = list(range(len(chosen)))
    rng.shuffle(order)
    items = [{"blind_id": ids[i], "answer": normalise.normalise(chosen[i][1])["text"]} for i in order]
    key = {"schema": grade_sheet.KEY_SCHEMA, "seed": seed, "purpose": "tell_test_1b", "letters": TELL_LETTERS,
           "excluded_opened_keys": ["run1/sealed/tell_key.json", "run1/sealed/tell_key_2.json"],
           "items": {ids[i]: {"arm": chosen[i][0], "arm_label": chosen[i][1]["arm"],
                              "question_id": chosen[i][1]["question_id"], "class": chosen[i][1]["class"]}
                     for i in order}}
    titles = {**{a: dz.arm_titles[a] for a in "ABCDE"}, **ADDED_TITLES}
    test = {"schema": "legba.bakeoff.tell_test/1", "design_sha256": dz.sha256,
            "instruction": "For each answer, name the arm (A, B, C, D, E, F or G) that produced it.",
            "arms": [{"arm": a, "description": titles[a]} for a in TELL_LETTERS],
            "items": items, "pass_if_accuracy_at_most": grade_sheet.TELL_PASS_MAX}
    return test, key


def parse_tell_1b(text: str) -> tuple[str | None, list[str]]:
    obj = common.extract_json_object(text)
    arm = obj.get("arm") if obj else None
    if isinstance(arm, str) and arm.strip().upper() in TELL_LETTERS:
        return arm.strip().upper(), []
    s = common._THINK.sub("", text or "").strip().upper().strip(" .\"'{}")  # noqa: SLF001
    if s in TELL_LETTERS:
        return s, []
    return None, ['expected {"arm": "<A, B, C, D, E, F or G>"}']


def check_pin_against_run1(run1: Path, run1b: Path) -> list[str]:
    a, b = _load(run1 / "run.json")["grader_pin"], _load(run1b / "run.json").get("grader_pin") or {}
    return grade_sheet.pin_drift(a, b)


async def tell_main(a: argparse.Namespace) -> int:
    run1, run1b = Path(a.run1), Path(a.out)
    dz = design_mod.load_design(a.design)
    run = await grade_sheet.recheck_pin(run1b)
    drift = check_pin_against_run1(run1, run1b)
    if drift:
        raise SystemExit("grader pin differs from run 1's: " + "; ".join(drift))
    gdir, sdir = run1b / "grading", run1b / "sealed"
    tpath = gdir / "tell_test.json"
    if not tpath.is_file():
        test, key = build_tell(dz, run1, run1b, a.seed,
                               f_from=Path(a.f_from) if getattr(a, "f_from", None) else None)
        if getattr(a, "f_from", None):
            key["f_from"] = str(a.f_from)
        sha = grade_sheet._seal(key, sdir / "tell_key_1b.json", gdir)  # noqa: SLF001
        common.write_json(tpath, test)
        run = common.load_run(run1b)
        run.setdefault("blind_keys", []).append({"purpose": "tell-test", "seed": a.seed, "sha256": sha})
        run["tell_prompt_sha256"] = TELL_PROMPT_1B_SHA256
        common.save_run(run1b, run)
    test = _load(tpath)
    calls_path = gdir / "tell_calls.json"
    doc = _load(calls_path) if calls_path.is_file() else {"schema": grade_run.TELL_CALLS_SCHEMA,
                                                          "tell_prompt_sha256": TELL_PROMPT_1B_SHA256, "items": {}}
    chat = await grade_run._real_chat()  # noqa: SLF001
    arms_txt = "\n".join(f"{x['arm']}: {x['description']}" for x in test["arms"])
    entries = dict(doc["items"])
    pending = [it for it in test["items"] if not (entries.get(it["blind_id"]) or {}).get("arm")]

    async def work(it: dict[str, Any]) -> None:
        prompt = TELL_PROMPT_1B.format(arms=arms_txt, answer=it["answer"])
        arm, calls, err, last = await grade_run.ask(chat, prompt, parse_tell_1b)
        entries[it["blind_id"]] = {"arm": arm, "error": err, "calls": calls,
                                   "last_reply": None if arm else (last or "")[:4000]}
        doc["items"] = entries
        common.write_json(calls_path, doc)

    await grade_run._fan_out(pending, work, a.concurrency)  # noqa: SLF001
    answers = {bid: e["arm"] for bid, e in entries.items() if e.get("arm")}
    if len(answers) < len(test["items"]):
        print(f"{len(test['items']) - len(answers)} tell answers missing — re-run")
        return 4
    common.write_json(gdir / "tell_answers.json", answers)
    key = _load(sdir / "tell_key_1b.json")
    ok, acc = grade_sheet.tell_test_passes(answers, key)
    per: dict[str, dict[str, int]] = {}
    for bid, v in key["items"].items():
        p = per.setdefault(v["arm"], {"n": 0, "hit": 0, "named": 0})
        p["n"] += 1
        p["hit"] += int(answers.get(bid) == v["arm"])
    for arm in answers.values():
        per.setdefault(arm, {"n": 0, "hit": 0, "named": 0})["named"] += 1
    run = common.load_run(run1b)
    run["tell_usage"] = grade_run.usage_totals(entries)
    run.setdefault("tell_tests", []).append({"accuracy": acc, "passed": ok, "seed": key["seed"], "arms": 7,
                                             "chance": round(1 / 7, 4), "per_arm": per})
    if not ok:
        run["unblinded_risk"] = {"reason": f"tell test 1b failed: accuracy {acc} > {grade_sheet.TELL_PASS_MAX}",
                                 "at": common.utcnow().isoformat()}
    common.save_run(run1b, run)
    print(f"tell test 1b: accuracy {acc:.2%} — {'PASS' if ok else 'FAIL (graded anyway: UNBLINDED-RISK)'}; per arm {per}")
    return 0


def sheet_main(a: argparse.Namespace) -> int:
    """A blind sheet over run 1b's records of ``--arms`` only (``--name`` sheet / blind_key[_name])."""
    run1b = Path(a.out)
    dz = design_mod.load_design(a.design)
    run = common.load_run(run1b)
    refusal = grade_sheet.grading_refusal(run)
    if refusal:
        raise SystemExit(refusal)
    gold = _load(run1b / "gold.json")
    recs = [r for r in _records(run1b) if r["arm"] in a.arms]
    sheet, key = grade_sheet.build_sheet(dz, recs, gold, seed=a.seed)
    sfx = "" if a.name == "sheet" else "_" + a.name.removeprefix("sheet_")
    key_sha = grade_sheet._seal(key, run1b / "sealed" / f"blind_key{sfx}.json", run1b / "grading")  # noqa: SLF001
    common.write_json(run1b / "grading" / f"{a.name}.json", sheet)
    run = common.load_run(run1b)
    run.setdefault("blind_keys", []).append({"purpose": "sheet", "name": a.name, "arms": a.arms, "seed": a.seed,
                                             "sha256": key_sha})
    common.save_run(run1b, run)
    print(f"{a.name}: {len(sheet['items'])} items, arms {a.arms}")
    return 0


async def grade_main(a: argparse.Namespace) -> int:
    run1, run1b = Path(a.run1), Path(a.out)
    gdir = run1b / "grading"
    r1 = _load(run1 / "run.json")
    if r1.get("grade_prompt_sha256") != grade_run.GRADE_PROMPT_SHA256:
        raise SystemExit("the grade prompt differs from run 1's")
    if common.sha256_file(run1 / "gold.json") != common.sha256_file(run1b / "gold.json"):
        raise SystemExit("gold differs from run 1's")
    run = common.load_run(run1b)
    if not (run.get("tell_tests") or []):
        raise SystemExit("no tell test 1b has been scored — run tell-test first")
    run = await grade_sheet.recheck_pin(run1b)
    drift = check_pin_against_run1(run1, run1b)
    if drift:
        raise SystemExit("grader pin differs from run 1's: " + "; ".join(drift))
    grade_run._sealed(run, "sheet")  # noqa: SLF001
    run["grade_prompt_sha256"] = grade_run.GRADE_PROMPT_SHA256
    common.save_run(run1b, run)
    sfx = "" if a.name == "sheet" else "_" + a.name.removeprefix("sheet_")
    sheet_path = gdir / f"{a.name}.json"
    sheet = _load(sheet_path)
    out = gdir / f"grades_primary{sfx}.json"
    doc = _load(out) if out.is_file() else {
        "schema": grade_run.GRADES_SCHEMA, "rater": "primary", "grader": run["grader_pin"]["primary_grader"],
        "grade_prompt_sha256": grade_run.GRADE_PROMPT_SHA256, "sheet_sha256": common.sha256_file(sheet_path),
        "unblinded_risk": bool(run.get("unblinded_risk")), "items": {}}

    def save(bid: str, entry: dict[str, Any]) -> None:
        doc["items"][bid] = entry
        common.write_json(out, doc)

    chat = await grade_run._real_chat()  # noqa: SLF001
    entries = await grade_run.run_grades(sheet, chat, doc["items"], concurrency=a.concurrency,
                                         retry_errors=a.retry_errors, on_entry=save)
    doc["items"] = entries
    common.write_json(out, doc)
    run = common.load_run(run1b)
    run["grade_usage"] = grade_run.usage_totals(entries)
    errors = sum(1 for e in entries.values() if e.get("grade") is None)
    if grade_run.all_closed(sheet, entries):
        run.setdefault("grading_closed", {})[a.name] = {"at": common.utcnow().isoformat(),
                                                        "sha256": common.sha256_file(out)}
    common.save_run(run1b, run)
    print(f"{a.name}: graded {len(entries)} of {len(sheet['items'])}, {errors} grade_error")
    return 0 if a.name in (run.get("grading_closed") or {}) else 4


# ---------------------------------------------------------------------------
# The joint report
# ---------------------------------------------------------------------------


RUN1B_SHEETS = ("sheet", "sheet_bopen2")


def rows_for(run_dir: Path, key_name: str = "blind_key.json") -> list[dict[str, Any]]:
    gdir = run_dir / "grading"
    out = []
    for name in RUN1B_SHEETS:
        sfx = "" if name == "sheet" else "_" + name.removeprefix("sheet_")
        if not (gdir / f"{name}.json").is_file() or not (gdir / f"grades_primary{sfx}.json").is_file():
            continue
        sheet, key = _load(gdir / f"{name}.json"), _load(run_dir / "sealed" / f"blind_key{sfx}.json")
        grades = _load(gdir / f"grades_primary{sfx}.json")["items"]
        out += report.assemble(sheet, key, grades, _records(run_dir))
    return out


def arm_ops(records: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    rs = [r for r in records if r["arm"] == arm and r.get("question_id") in QIDS]
    tools = [r.get("tools") or {} for r in rs]
    usage = [r.get("usage") or {} for r in rs]
    models = sorted({str(m) for r in rs for m in (r.get("call_models") or [])})
    probes = sorted({f"{(p or {}).get('chat_response_model')}/{(p or {}).get('models_entry_served_model')}"
                     for r in rs for p in ((r.get("served_model_probe") or {}).values())})
    post = [r.get("post_t") or {} for r in rs]
    return {
        "n": len(rs), "status": {s: sum(1 for r in rs if r["status"] == s) for s in {r["status"] for r in rs}},
        "tool_calls": sum(int(t.get("calls") or 0) for t in tools) if any(tools) else None,
        "tool_failed": sum(int(t.get("failed") or 0) for t in tools) if any(tools) else None,
        "tool_blocked": sum(int(t.get("blocked") or 0) for t in tools) if any(tools) else None,
        "by_tool": _merge_by_tool(tools),
        "rounds": sum(int(u.get("tool_rounds") or 0) for u in usage),
        "llm_calls": sum(int(u.get("calls") or 0) for u in usage),
        "input_tokens": sum(int(u.get("input_tokens") or 0) for u in usage),
        "output_tokens": sum(int(u.get("output_tokens") or 0) for u in usage),
        "wall_s": round(sum(float(r.get("elapsed_s") or 0) for r in rs), 1),
        "model": sorted({str((r.get("model") or {}).get("model_name")) for r in rs}),
        "envelope_model": sorted({str(r.get("envelope_model")) for r in rs if r.get("envelope_model")}),
        "call_models": models, "probe_models": probes,
        "partial": sum(1 for r in rs if r["status"] == "partial"),
        "forced_final": sum(1 for r in rs if r.get("forced_final")),
        "cited_refs": sum(int(p.get("cited") or 0) for p in post),
        "post_t_refs": sum(int(p.get("post_t") or 0) for p in post),
        "answers_citing_post_t": sum(1 for p in post if (p.get("post_t") or 0) > 0),
        "cost_usd": round(sum(float((r.get("cost") or {}).get("usd") or 0) for r in rs), 4),
    }


def _merge_by_tool(tools: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, dict[str, int]] = {}
    for t in tools:
        for name, v in (t.get("by_tool") or {}).items():
            o = out.setdefault(name, {"calls": 0, "failed": 0, "blocked": 0})
            for k in o:
                o[k] += int(v.get(k) or 0)
    return out


QIDS: set[str] = set()


def joint(run1: Path, run1b: Path) -> dict[str, Any]:
    global QIDS
    r1rows = rows_for(run1)
    r1brows = rows_for(run1b)
    QIDS = {r["question_id"] for r in r1rows}
    rows = r1rows + r1brows
    seed = int(_load(run1b / "grading" / "sheet.json")["seed"])
    arms = [a for a in ("A", "A-r", "B", "C", "D", "E", *NEW_ARMS) if any(r["arm"] == a for r in rows)]
    per_arm = {a: report.summarise([r for r in rows if r["arm"] == a], seed=seed, label=a) for a in arms}
    per_class = {c: {a: report.summarise([r for r in rows if r["arm"] == a and r["class"] == c], seed=seed,
                                         label=f"{a}|{c}") for a in arms}
                 for c in report.CLASSES}
    comps = {}
    for x, y in (("B-open", "A"), ("B", "B-open"), ("C-record", "B-open"), ("B-open-2", "B-open"),
                 ("B-open-2", "A"), ("B", "B-open-2"), ("C-record", "A"), ("C-record", "C"), ("B-open", "A-r"),
                 ("C-record-2", "C-record")):
        if x in arms and y in arms:
            comps[f"{x} - {y}"] = {m: report.compare(rows, x, y, m, seed=seed, permutation=(m == "correct"))
                                   for m in ("correct", "error_rate", "usefulness")}
            comps[f"{x} - {y}"]["by_class"] = {c: report.compare(rows, x, y, "correct", klass=c, seed=seed)
                                               for c in report.CLASSES}
    recs1, recs1b = _records(run1), _records(run1b)
    ops = {a: arm_ops(recs1 if a in ("A", "A-r", "B", "C", "D", "E") else recs1b, a) for a in arms}
    # Sensitivity: the new arms' correctness on answers that cite NO post-T ref.
    post = {(r["question_id"], r["arm"]): ((r.get("post_t") or {}).get("post_t") or 0) for r in recs1b}
    sens = {}
    for a in NEW_ARMS:
        clean = [r for r in rows if r["arm"] == a and post.get((r["question_id"], a), 0) == 0]
        dirty = [r for r in rows if r["arm"] == a and post.get((r["question_id"], a), 0) > 0]
        if clean or dirty:
            sens[a] = {"clean": report.summarise(clean, seed=seed, label=f"clean|{a}"),
                       "cites_post_t": report.summarise(dirty, seed=seed, label=f"dirty|{a}"),
                       "dirty_questions": sorted(r["question_id"] for r in dirty)}
    run = _load(run1b / "run.json")
    return {"schema": "legba.bakeoff.report_1b/1", "seed": seed, "arms": arms, "per_arm": per_arm,
            "per_class": per_class, "comparisons": comps, "ops": ops, "post_t_sensitivity": sens,
            "tell_tests": run.get("tell_tests"), "unblinded_risk": run.get("unblinded_risk"),
            "grader_pin": run.get("grader_pin"), "grader_rechecks": run.get("grader_rechecks"),
            "run1b": {k: run.get(k) for k in ("run_id", "as_of", "design_version", "design_sha256", "gold_sha256",
                                              "grade_usage", "tell_usage", "grading_closed_at", "harness_git_sha")},
            "rows": r1brows}


def _f(x: Any, nd: int = 3) -> str:
    return report._f(x, nd)  # noqa: SLF001


def _ci(ci: Any) -> str:
    return report._ci(ci)  # noqa: SLF001


def render_tables(rep: dict[str, Any]) -> str:
    """The numeric sections of report_1b.md (the prose is written around them)."""
    L: list[str] = []
    arms = rep["arms"]
    L += ["## All arms, all classes", "",
          "| arm | n | graded | correct | 95 % CI | error rate | usefulness | 95 % CI | Brier | stale | parts |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in arms:
        s = rep["per_arm"][a]
        L.append(f"| {a} | {s['n']} | {s['graded']} | {_f(s['correct'])} | {_ci(s.get('correct_ci'))} | "
                 f"{_f(s['error_rate'])} | {_f(s['usefulness'])} | {_ci(s.get('usefulness_ci'))} | {_f(s['brier'])} | "
                 f"{s['stale']} | {_f(s['parts_addressed'])} |")
    for c in report.CLASSES:
        L += ["", f"## {c}", "", "| arm | n | graded | correct | 95 % CI | error rate | usefulness |", "|---|---|---|---|---|---|---|"]
        for a in arms:
            s = rep["per_class"][c][a]
            L.append(f"| {a} | {s['n']} | {s['graded']} | {_f(s['correct'])} | {_ci(s.get('correct_ci'))} | "
                     f"{_f(s['error_rate'])} | {_f(s['usefulness'])} |")
    L += ["", "## Paired differences (per question, x − y; bootstrap 95 % CI; two-sided paired permutation p on correct)", "",
          "| comparison | metric | pairs | x | y | x − y | 95 % CI | p |", "|---|---|---|---|---|---|---|---|"]
    for name, cm in rep["comparisons"].items():
        for m in ("correct", "error_rate", "usefulness"):
            d = cm[m]
            L.append(f"| {name} | {m} | {d['n']} | {_f(d['mean_x'])} | {_f(d['mean_y'])} | {_f(d['mean_diff'])} | "
                     f"{_ci(d['ci'])} | {_f(d.get('p'), 4) if d.get('p') is not None else ''} |")
    L += ["", "### Paired correctness difference by class", "",
          "| comparison | " + " | ".join(report.CLASSES) + " |", "|---|" + "---|" * len(report.CLASSES)]
    for name, cm in rep["comparisons"].items():
        cells = [f"{_f(cm['by_class'][c]['mean_diff'])} {_ci(cm['by_class'][c]['ci'])} (n {cm['by_class'][c]['n']})"
                 for c in report.CLASSES]
        L.append(f"| {name} | " + " | ".join(cells) + " |")
    L += ["", "## Operations per arm (30 graded questions; run-1 arms from their records)", "",
          "| arm | status | tool calls | failed | blocked | rounds | LLM calls | input tok | output tok | wall (s) | "
          "partial | forced final | $ |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in arms:
        o = rep["ops"][a]
        st = ", ".join(f"{k} {v}" for k, v in sorted(o["status"].items()))
        L.append(f"| {a} | {st} | {_f(o['tool_calls'])} | {_f(o['tool_failed'])} | {_f(o['tool_blocked'])} | "
                 f"{o['rounds']} | {o['llm_calls']} | {o['input_tokens']:,} | {o['output_tokens']:,} | {o['wall_s']} | "
                 f"{o['partial']} | {o['forced_final']} | {o['cost_usd']} |")
    L += ["", "### Tool calls by tool (added arms)", "", "| arm | tool | calls | failed | blocked |", "|---|---|---|---|---|"]
    for a in arms:
        for t, v in sorted((rep["ops"][a].get("by_tool") or {}).items()):
            L.append(f"| {a} | {t} | {v['calls']} | {v['failed']} | {v['blocked']} |")
    L += ["", "### Served model and post-T exposure (added arms)", "",
          "| arm | component model | envelope model | per-call models (C-record) | canary chat / models-entry | cited refs | "
          "post-T refs | answers citing post-T |", "|---|---|---|---|---|---|---|---|"]
    for a in arms:
        if a not in NEW_ARMS:
            continue
        o = rep["ops"][a]
        L.append(f"| {a} | {', '.join(o['model'])} | {', '.join(o['envelope_model']) or '—'} | "
                 f"{', '.join(o['call_models']) or '— (not exposed by the actor)'} | {', '.join(o['probe_models'])} | "
                 f"{o['cited_refs']} | {o['post_t_refs']} | {o['answers_citing_post_t']} |")
    if rep.get("post_t_sensitivity"):
        L += ["", "### Sensitivity: correctness split by whether the answer cites any post-T ref", "",
              "| arm | clean n | clean correct | 95 % CI | post-T n | post-T correct | questions citing post-T |",
              "|---|---|---|---|---|---|---|"]
        for a, s in rep["post_t_sensitivity"].items():
            L.append(f"| {a} | {s['clean']['graded']} | {_f(s['clean']['correct'])} | {_ci(s['clean'].get('correct_ci'))} | "
                     f"{s['cites_post_t']['graded']} | {_f(s['cites_post_t']['correct'])} | "
                     f"{', '.join(s['dirty_questions'])} |")
    return "\n".join(L) + "\n"


def report_main(a: argparse.Namespace) -> int:
    run1, run1b = Path(a.run1), Path(a.out)
    run = common.load_run(run1b)
    if "sheet" not in (run.get("grading_closed") or {}):
        raise SystemExit("run 1b grading is not closed")
    if not run.get("blind_key_opened_at"):
        run["blind_key_opened_at"] = common.utcnow().isoformat()
        common.save_run(run1b, run)
    rep = joint(run1, run1b)
    common.write_json(Path(a.json_out), rep)
    Path(a.json_out).with_suffix(".tables.md").write_text(render_tables(rep), encoding="utf-8")
    print(f"wrote {a.json_out} and its .tables.md")
    return 0


# ---------------------------------------------------------------------------
# Secondary analysis: every arm against the gold v2 CANDIDATE
# ---------------------------------------------------------------------------
#
# Gold v2 (run1/gold_v2/gold_v2_candidate.json, deviation D9) re-verified 17
# rows. Handling, as the design handles what cannot be met:
#   * an atom with v2_status VOID / v2_exclude_from_scoring is not gold: it is
#     removed before the grader sees the row;
#   * an INSUFFICIENT row whose absence proof is VOID is a VOID question (§3.5,
#     grade_sheet.void_rules: "no signed absence proof");
#   * a row whose core status-at-T atom is VOID is reported both ways: voided
#     (the primary v2 reading, §3.5 "the gold builder cannot meet its gold
#     source") and kept on its remaining atoms.
#
# WHICH rows and atoms those are is a property of the gold, and evaluation data
# never enters the public repo: ``goldv2-prep`` reads them from a rules file
# passed by path (``--v2-rules``, JSON ``{"core_void_rows": [qid, ...],
# "post_t_only_atoms": ["qid.gN", ...]}``, kept beside the gold under
# planning/), stamps them into ``gold_v2_meta.json``, and every later step reads
# them from that meta file.
# Rows whose RENDERED gold (atom ids + texts, error atoms, prior-state note,
# absence proof) is identical to v1 reuse their v1 grades unchanged; only the
# changed rows are re-graded, for every arm.

def load_v2_rules(path: Path) -> dict[str, list[str]]:
    """The gold-derived v2 rules (see the block comment above), validated.

    ``core_void_rows``: question ids whose core status-at-T atom is VOID.
    ``post_t_only_atoms``: ``"<qid>.<atom id>"`` — true at T, but the only
    spans found were published after T.
    """
    doc = _load(path)
    rows, atoms = doc.get("core_void_rows"), doc.get("post_t_only_atoms")
    if not (isinstance(rows, list) and all(isinstance(x, str) for x in rows)):
        raise SystemExit(f"{path}: core_void_rows must be a list of question ids")
    if not (isinstance(atoms, list) and all(isinstance(x, str) and x.count(".") == 1 for x in atoms)):
        raise SystemExit(f"{path}: post_t_only_atoms must be a list of '<qid>.<atom id>' strings")
    return {"core_void_rows": sorted(rows), "post_t_only_atoms": sorted(atoms)}


def _post_t_only(meta: dict[str, Any]) -> set[tuple[str, str]]:
    return {tuple(x.split(".", 1)) for x in meta["post_t_only_atoms"]}  # type: ignore[misc]


def _is_void_atom(a: Any) -> bool:
    return isinstance(a, dict) and (a.get("v2_status") == "VOID" or bool(a.get("v2_exclude_from_scoring")))


def _rendered(row: dict[str, Any]) -> str:
    g = {k: row.get(k) for k in ("gold_atoms", "error_atoms", "prior_state_note", "absence_proof")}
    return json.dumps([grade_run.atoms(g, "gold_atoms"), grade_run.atoms(g, "error_atoms"),
                       g["prior_state_note"], g["absence_proof"]], sort_keys=True)


def gold_v2_effective(v1: dict[str, Any], cand: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(effective gold, meta)`` — void atoms dropped, void absence proofs void the row."""
    eff = json.loads(json.dumps(cand))
    meta: dict[str, Any] = {"void_atoms": [], "void_rows": {}, "changed_rows": []}
    for qid, row in eff["questions"].items():
        keep = []
        for a in row.get("gold_atoms") or []:
            if _is_void_atom(a):
                meta["void_atoms"].append(f"{qid}.{a.get('id')}")
            else:
                keep.append(a)
        row["gold_atoms"] = keep
        if row.get("v2_absence_proof_status") == "VOID":
            row["absence_proof"] = None  # §3.5 / grade_sheet.void_rules: no absence proof -> void
            meta["void_rows"][qid] = "gold v2: absence proof VOID"
        if _rendered(row) != _rendered(v1["questions"][qid]):
            meta["changed_rows"].append(qid)
    return eff, meta


def goldv2_prep(a: argparse.Namespace) -> int:
    run1, run1b = Path(a.run1), Path(a.out)
    cand_path = run1 / "gold_v2" / "gold_v2_candidate.json"
    eff, meta = gold_v2_effective(_load(run1 / "gold.json"), _load(cand_path))
    meta.update(candidate_sha256=common.sha256_file(cand_path), v1_sha256=common.sha256_file(run1 / "gold.json"),
                **load_v2_rules(Path(a.v2_rules)))
    d = run1b / "goldv2"
    common.write_json(d / "gold_v2_effective.json", eff)
    common.write_json(d / "gold_v2_meta.json", meta)
    print(json.dumps({k: meta[k] for k in ("void_atoms", "void_rows", "changed_rows")}))
    return 0


def goldv2_sheet(a: argparse.Namespace) -> int:
    run1, run1b = Path(a.run1), Path(a.out)
    dz = design_mod.load_design(a.design)
    d = run1b / "goldv2"
    eff, meta = _load(d / "gold_v2_effective.json"), _load(d / "gold_v2_meta.json")
    if meta["candidate_sha256"] != common.sha256_file(run1 / "gold_v2" / "gold_v2_candidate.json"):
        raise SystemExit("gold_v2_candidate.json changed since goldv2-prep")
    rows = set(meta["changed_rows"]) - set(meta["void_rows"])
    recs = [r for r in _records(run1) + _records(run1b)
            if r["arm"] in a.arms and r["question_id"] in rows]
    sheet, key = grade_sheet.build_sheet(dz, recs, eff, seed=a.seed)
    gdir = run1b / "grading"
    key_sha = grade_sheet._seal(key, run1b / "sealed" / f"blind_key_goldv2_{a.tag}.json", gdir)  # noqa: SLF001
    common.write_json(gdir / f"sheet_goldv2_{a.tag}.json", sheet)
    run = common.load_run(run1b)
    run.setdefault("blind_keys", []).append({"purpose": f"sheet-goldv2-{a.tag}", "seed": a.seed, "sha256": key_sha})
    common.save_run(run1b, run)
    print(f"goldv2 sheet {a.tag}: {len(sheet['items'])} items over {len(rows)} rows, arms {a.arms}")
    return 0


async def goldv2_grade(a: argparse.Namespace) -> int:
    run1, run1b = Path(a.run1), Path(a.out)
    gdir = run1b / "grading"
    await grade_sheet.recheck_pin(run1b)
    drift = check_pin_against_run1(run1, run1b)
    if drift:
        raise SystemExit("grader pin differs from run 1's: " + "; ".join(drift))
    sheet_path = gdir / f"sheet_goldv2_{a.tag}.json"
    sheet = _load(sheet_path)
    out = gdir / f"grades_primary_goldv2_{a.tag}.json"
    doc = _load(out) if out.is_file() else {
        "schema": grade_run.GRADES_SCHEMA, "rater": "primary", "gold": "v2 candidate (effective)",
        "grade_prompt_sha256": grade_run.GRADE_PROMPT_SHA256, "sheet_sha256": common.sha256_file(sheet_path),
        "items": {}}

    def save(bid: str, entry: dict[str, Any]) -> None:
        doc["items"][bid] = entry
        common.write_json(out, doc)

    chat = await grade_run._real_chat()  # noqa: SLF001
    entries = await grade_run.run_grades(sheet, chat, doc["items"], concurrency=a.concurrency,
                                         retry_errors=a.retry_errors, on_entry=save)
    doc["items"] = entries
    doc["closed"] = grade_run.all_closed(sheet, entries)
    common.write_json(out, doc)
    errors = sum(1 for e in entries.values() if e.get("grade") is None)
    print(f"goldv2 {a.tag}: graded {len(entries)} of {len(sheet['items'])}, {errors} grade_error")
    return 0


def _metrics_filtered(klass: str, grade: dict[str, Any], drop: set[str]) -> dict[str, Any] | None:
    g = dict(grade)
    g["gold_atoms"] = [x for x in grade["gold_atoms"] if x["id"] not in drop]
    if not g["gold_atoms"]:
        return None
    g["novelty_atoms"] = [x for x in grade["novelty_atoms"] if x not in drop]
    return report.answer_metrics(klass, g)


def goldv2_rows(run1: Path, run1b: Path, *, keep_core_void: bool, strict_post_t: bool) -> list[dict[str, Any]]:
    """Every arm's rows under gold v2: v1 rows for unchanged questions, v2 grades
    for changed ones; void rows removed; post-T-only atoms dropped when strict."""
    meta = _load(run1b / "goldv2" / "gold_v2_meta.json")
    changed = set(meta["changed_rows"])
    void = set(meta["void_rows"]) | (set() if keep_core_void else set(meta["core_void_rows"]))
    post_t_only = _post_t_only(meta)
    v2 = []
    for tag in ("run1arms", "newarms"):
        sp = run1b / "grading" / f"sheet_goldv2_{tag}.json"
        if sp.is_file():
            v2 += report.assemble(_load(sp), _load(run1b / "sealed" / f"blind_key_goldv2_{tag}.json"),
                                  _load(run1b / "grading" / f"grades_primary_goldv2_{tag}.json")["items"],
                                  _records(run1) + _records(run1b))
            v2_grades = _load(run1b / "grading" / f"grades_primary_goldv2_{tag}.json")["items"]
            for r in v2[-len(_load(sp)["items"]):]:
                r["_grade"] = (v2_grades.get(r["blind_id"]) or {}).get("grade")
    base = []
    for rd in (run1, run1b):
        grades = {}
        for name in RUN1B_SHEETS:
            sfx = "" if name == "sheet" else "_" + name.removeprefix("sheet_")
            gp = rd / "grading" / f"grades_primary{sfx}.json"
            if gp.is_file():
                grades.update(_load(gp)["items"])
        rows = rows_for(rd)
        for r in rows:
            r["_grade"] = (grades.get(r["blind_id"]) or {}).get("grade")
        base += rows
    out = [r for r in base if r["question_id"] not in changed] + v2
    out = [r for r in out if r["question_id"] not in void]
    if strict_post_t:
        res = []
        for r in out:
            drop = {g for q, g in post_t_only if q == r["question_id"]}
            if drop and r.get("_grade") is not None:
                m = _metrics_filtered(r["class"], r["_grade"], drop)
                if m is None:
                    continue  # every atom was post-T-only: the row is void under the strict reading
                r = {**r, **m}
            res.append(r)
        out = res
    return out


def goldv2_report(a: argparse.Namespace) -> int:
    run1, run1b = Path(a.run1), Path(a.out)
    v1 = rows_for(run1) + rows_for(run1b)
    seed = int(_load(run1b / "grading" / "sheet.json")["seed"])
    variants = {
        "v1": v1,
        "v2": goldv2_rows(run1, run1b, keep_core_void=False, strict_post_t=False),
        "v2_keep_core_void": goldv2_rows(run1, run1b, keep_core_void=True, strict_post_t=False),
        "v2_strict_post_t": goldv2_rows(run1, run1b, keep_core_void=False, strict_post_t=True),
    }
    arms = [x for x in ("A", "A-r", "B", "C", "D", "E", *NEW_ARMS) if any(r["arm"] == x for r in v1)]
    out: dict[str, Any] = {"seed": seed, "arms": arms, "variants": {}}
    for name, rows in variants.items():
        rows = [{k: v for k, v in r.items() if k != "_grade"} for r in rows]
        per_arm = {x: report.summarise([r for r in rows if r["arm"] == x], seed=seed, label=f"{name}|{x}") for x in arms}
        per_class = {c: {x: report.summarise([r for r in rows if r["arm"] == x and r["class"] == c]) for x in arms}
                     for c in report.CLASSES}
        comps = {}
        for x, y in (("B", "A"), ("B-open", "A"), ("B", "B-open"), ("C-record", "B-open"), ("B-open-2", "B-open"),
                     ("C-record-2", "C-record")):
            if x in arms and y in arms:
                comps[f"{x} - {y}"] = {"ALL": report.compare(rows, x, y, "correct", seed=seed, permutation=True),
                                       **{c: report.compare(rows, x, y, "correct", klass=c, seed=seed)
                                          for c in report.CLASSES}}
        out["variants"][name] = {"questions": len({r["question_id"] for r in rows}), "per_arm": per_arm,
                                 "per_class": per_class, "comparisons": comps}
    common.write_json(Path(a.json_out), out)
    L = []
    names = list(out["variants"])
    L += ["| arm | " + " | ".join(f"{n} correct [95 % CI] (graded)" for n in names) + " |",
          "|---|" + "---|" * len(names)]
    for x in arms:
        L.append(f"| {x} | " + " | ".join(
            f"{_f(out['variants'][n]['per_arm'][x]['correct'])} {_ci(out['variants'][n]['per_arm'][x].get('correct_ci'))} "
            f"({out['variants'][n]['per_arm'][x]['graded']})" for n in names) + " |")
    L += ["", "| arm | " + " | ".join(f"{n} error rate / usefulness" for n in names) + " |", "|---|" + "---|" * len(names)]
    for x in arms:
        L.append(f"| {x} | " + " | ".join(
            f"{_f(out['variants'][n]['per_arm'][x]['error_rate'])} / {_f(out['variants'][n]['per_arm'][x]['usefulness'])}"
            for n in names) + " |")
    L += ["", "| comparison | class | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for cmp_name in out["variants"]["v1"]["comparisons"]:
        for c in ("ALL", *report.CLASSES):
            cells = []
            for n in names:
                d = out["variants"][n]["comparisons"].get(cmp_name, {}).get(c)
                cells.append(f"{_f(d['mean_diff'])} {_ci(d['ci'])} n{d['n']}"
                             + (f" p{_f(d.get('p'), 4)}" if d.get("p") is not None else "") if d else "—")
            L.append(f"| {cmp_name} | {c} | " + " | ".join(cells) + " |")
    for c in report.CLASSES:
        L += ["", f"**{c}** correct by variant", "", "| arm | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for x in arms:
            L.append(f"| {x} | " + " | ".join(
                f"{_f(out['variants'][n]['per_class'][c][x]['correct'])} (n {out['variants'][n]['per_class'][c][x]['graded']})"
                for n in names) + " |")
    Path(a.json_out).with_suffix(".tables.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {a.json_out} and its .tables.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run 1b: tell test with the added arms, grading, joint report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("tell-test", "sheet", "grade", "report", "goldv2-prep", "goldv2-sheet", "goldv2-grade", "goldv2-report"):
        s = sub.add_parser(name)
        s.add_argument("--run1", required=True)
        s.add_argument("--out", required=True, help="the run 1b directory")
        s.add_argument("--design", required=True)
        s.add_argument("--concurrency", type=int, default=grade_run.CONCURRENCY)
        if name in ("tell-test", "goldv2-sheet", "sheet"):
            s.add_argument("--seed", type=int, required=True)
        if name == "tell-test":
            s.add_argument("--f-from", help="run dir letter F (B-open) is drawn from (default: --out)")
        if name in ("sheet", "grade"):
            s.add_argument("--name", default="sheet", choices=RUN1B_SHEETS)
        if name == "sheet":
            s.add_argument("--arms", nargs="+", required=True)
        if name in ("grade", "goldv2-grade"):
            s.add_argument("--retry-errors", action="store_true")
        if name in ("goldv2-sheet", "goldv2-grade"):
            s.add_argument("--tag", required=True, choices=("run1arms", "newarms"))
        if name == "goldv2-sheet":
            s.add_argument("--arms", nargs="+", required=True)
        if name in ("report", "goldv2-report"):
            s.add_argument("--json-out", required=True)
        if name == "goldv2-prep":
            s.add_argument("--v2-rules", required=True,
                           help="JSON of the gold-derived v2 rules (kept under planning/, never in the repo)")
    a = ap.parse_args(argv)
    common.load_env()
    if a.cmd == "sheet":
        return sheet_main(a)
    if a.cmd == "goldv2-prep":
        return goldv2_prep(a)
    if a.cmd == "goldv2-sheet":
        return goldv2_sheet(a)
    if a.cmd == "goldv2-grade":
        return asyncio.run(goldv2_grade(a))
    if a.cmd == "goldv2-report":
        return goldv2_report(a)
    if a.cmd == "tell-test":
        return asyncio.run(tell_main(a))
    if a.cmd == "grade":
        return asyncio.run(grade_main(a))
    return report_main(a)


if __name__ == "__main__":
    sys.exit(main())
