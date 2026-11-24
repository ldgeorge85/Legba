#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draft the gold (design v1.1 §4.1, §4.4) — one question at a time, never signed.

The gold drafter is ``grade_sheet.GOLD_DRAFTER_COMPONENT`` (``gemma-think`` on
the gateway; it never grades, the grader never builds gold). For each question
— by class, in the design's class order — it is asked, with ONE fixed prompt
(:data:`GOLD_PROMPT`, sha256 into ``run.json`` as ``gold_prompt_sha256``), for
the §4.1 row: 2–6 gold atoms, 0–3 error atoms, the prior-state note, and for an
INSUFFICIENT question the absence proof. It is given the question text, class,
desk, gold-source rule, window and the run's as-of T — never an arm's output.

It does NOT search the web: the governed web pack is the consult plane's, and
this script calls the drafter bare. So every atom's ``source`` is written
:data:`UNVERIFIED` (the drafter's suggestion, if any, is kept as
``source_hint``) and every row is written ``signed: false, signed_by: null``.
``grade_sheet.py sheet`` voids a row with no ``signed_by`` (§3.5), so nothing
drafted here can be graded against until the operator verifies the row in
``gold.json`` and signs it with ``gold_sign.py``.

A malformed reply is retried once, then the row is written with
``draft_error`` and no atoms. Existing rows are kept (``--redraft`` re-asks).
Refused without the run's ``grader_pin`` or on a re-probed route change.

USAGE
    python3 scripts/bakeoff/gold_draft.py --design <BAKEOFF_DESIGN.md> --run-dir <D> [--qid HE01 ...]
    python3 scripts/bakeoff/gold_draft.py --practice scripts/bakeoff/practice_questions.json --run-dir <D>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import grade_run  # noqa: E402
import grade_sheet  # noqa: E402

GOLD_SCHEMA = "legba.bakeoff.gold/1"
UNVERIFIED = "unverified — operator to supply"
GOLD_ATOMS = (2, 6)
ERROR_ATOMS = (0, 3)
#: Fields the operator's signature covers (``gold_sign.py``); the rest is bookkeeping.
ROW_CONTENT = ("question_id", "class", "desk", "question", "window", "gold_source", "gold_atoms",
               "error_atoms", "prior_state_note", "absence_proof", "met", "authoritative_answer_exists")

GOLD_PROMPT = """You are drafting the GOLD STANDARD for one evaluation question, before any answer to it has been written. An operator verifies and signs every row. You have no web access in this step: do not invent documents, URLs or quotations. For each gold atom, name in "source" the KIND of source that would establish it (an outlet type, an official body, a named statistical series); the operator supplies the actual document.

Question id: {qid}
Class: {klass}
Desk: {desk}
Gold-source rule: {gold}
Window: {window_start} to {window_end}{prior}
As of (T): {as_of}
Question: {question}

Class meanings: HEADLINE — what happened in the 7-day window; LONG-MEMORY — an observation weeks old and its status at T; SLOW-CHANGE — the direction of change over the 42-day window against the 42 days before it; INSUFFICIENT — the right answer is that the evidence does not establish the requested specific.

Write:
- gold_atoms: 2 to 6 atomic statements a correct answer as of T should contain, each with an id ("g1".."g6"), its text, and its source.
- error_atoms: 0 to 3 statements that are tempting and wrong (a false premise, a stale status, a widely repeated wrong figure), each with an id ("e1".."e3") and its text.
- prior_state_note: what was already established at the window start, used to judge novelty.
- absence_proof: on an INSUFFICIENT question, the searches that would show that no authoritative answer exists as of T, and what they would have to find; null on every other class.
On an INSUFFICIENT question, g1 is the decline itself ("Does not assert <the requested specific> as established fact"), the other gold atoms say what IS established, and the error atoms are the specific stated as fact.

Reply with ONE JSON object and nothing else, exactly this shape:
{{"gold_atoms": [{{"id": "g1", "text": "...", "source": "..."}}], "error_atoms": [{{"id": "e1", "text": "..."}}], "prior_state_note": "...", "absence_proof": null}}"""

GOLD_PROMPT_SHA256 = grade_run.prompt_sha(GOLD_PROMPT, grade_run.RETRY_NOTE)


def draft_prompt(q: Any, as_of: datetime) -> str:
    start, end = q.window_bounds(as_of)
    prior = q.prior_window(as_of)
    return GOLD_PROMPT.format(
        qid=q.qid, klass=q.klass, desk=q.desk, gold=q.gold,
        window_start=start.date().isoformat(), window_end=end.isoformat(),
        prior=(f" (compared with {prior[0].date().isoformat()} to {prior[1].date().isoformat()})" if prior else ""),
        as_of=as_of.isoformat(), question=q.text,
    )


def _atom_ok(a: Any) -> bool:
    """An atom is a dict with non-empty text; ids are re-numbered g1.. / e1.. here."""
    return isinstance(a, dict) and isinstance(a.get("text"), str) and a["text"].strip() != ""


def validate_draft(obj: Any, klass: str) -> tuple[dict[str, Any] | None, list[str]]:
    """``(row content, problems)`` for one drafter reply, in the §4.1 shape."""
    if not isinstance(obj, dict):
        return None, ["the reply is not a JSON object"]
    p: list[str] = []
    ga, ea = obj.get("gold_atoms"), obj.get("error_atoms")
    if not isinstance(ga, list) or not GOLD_ATOMS[0] <= len(ga) <= GOLD_ATOMS[1] or not all(_atom_ok(a) for a in ga):
        p.append("gold_atoms must be 2 to 6 {id, text, source}")
    if not isinstance(ea, list) or not ERROR_ATOMS[0] <= len(ea) <= ERROR_ATOMS[1] or not all(_atom_ok(a) for a in ea):
        p.append("error_atoms must be 0 to 3 {id, text}")
    if not isinstance(obj.get("prior_state_note"), str) or not obj["prior_state_note"].strip():
        p.append("prior_state_note must be a non-empty string")
    proof = obj.get("absence_proof")
    if klass == "INSUFFICIENT" and not (isinstance(proof, str) and proof.strip()):
        p.append("absence_proof is required on an INSUFFICIENT question")
    if p:
        return None, p
    gold = [{"id": f"g{i}", "text": a["text"].strip(), "source": UNVERIFIED,
             "source_hint": (str(a.get("source")).strip() or None) if a.get("source") else None}
            for i, a in enumerate(ga, 1)]
    errs = [{"id": f"e{i}", "text": a["text"].strip()} for i, a in enumerate(ea, 1)]
    return {
        "gold_atoms": gold, "error_atoms": errs,
        "prior_state_note": obj["prior_state_note"].strip(),
        "absence_proof": proof.strip() if klass == "INSUFFICIENT" else None,
    }, []


def empty_row(q: Any, as_of: datetime) -> dict[str, Any]:
    start, end = q.window_bounds(as_of)
    prior = q.prior_window(as_of)
    return {
        "question_id": q.qid, "class": q.klass, "desk": q.desk, "question": q.text, "gold_source": q.gold,
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "prior": [prior[0].isoformat(), prior[1].isoformat()] if prior else None},
        "gold_atoms": [], "error_atoms": [], "prior_state_note": None, "absence_proof": None,
        "met": None, "authoritative_answer_exists": None,
        "signed": False, "signed_by": None, "signed_at": None,
        "draft_error": None, "drafter_calls": [], "drafted_at": None,
    }


def _order(questions: list[Any]) -> list[Any]:
    rank = {c: i for i, c in enumerate(grade_sheet.CLASS_ORDER)}
    return sorted(questions, key=lambda q: (rank.get(q.klass, 9), q.qid))


async def draft_gold(
    questions: list[Any], as_of: datetime, chat: common.Chat, done: dict[str, Any], *,
    redraft: bool = False, on_row: Any = None,
) -> dict[str, Any]:
    """Draft each question not in ``done`` (all with ``redraft``), ONE AT A TIME,
    by class. Every row comes back unsigned."""
    rows = dict(done)
    for q in _order(questions):
        if q.qid in rows and not redraft:
            continue
        row = empty_row(q, as_of)
        content, calls, err, last = await grade_run.ask(
            chat, draft_prompt(q, as_of),
            lambda text, k=q.klass: validate_draft(common.extract_json_object(text), k))
        if content:
            row.update(content)
        row.update(draft_error=err, drafter_calls=calls, drafted_at=common.utcnow().isoformat(),
                   last_reply=None if content else (last or "")[:4000])
        rows[q.qid] = row
        if on_row:
            on_row(q.qid, row)
    return rows


async def _real_chat() -> common.Chat:  # pragma: no cover — G1 only
    common.load_env()
    route = await common.open_chat_route(common.Registry.from_env(), grade_sheet.GOLD_DRAFTER_COMPONENT)
    return route.complete


def load_questions(a: argparse.Namespace) -> list[Any]:
    if a.practice:
        import run_arm

        qs = run_arm.load_practice(Path(a.practice))
    else:
        if not a.design:
            raise SystemExit("--design (or --practice) is required")
        qs = list(design_mod.load_design(a.design).questions)
    if a.qid:
        want = set(a.qid)
        qs = [q for q in qs if q.qid in want]
        missing = want - {q.qid for q in qs}
        if missing:
            raise SystemExit(f"unknown question ids: {sorted(missing)}")
    return qs


async def main_async(a: argparse.Namespace, *, chat: common.Chat | None = None, probe: Any = None,
                     questions: list[Any] | None = None) -> int:
    run_dir = Path(a.run_dir)
    run = await grade_sheet.recheck_pin(run_dir, probe=probe)
    grade_run._check_prompt(run, "gold_prompt_sha256", GOLD_PROMPT_SHA256)
    common.save_run(run_dir, run)
    as_of = common.parse_as_of(run["as_of"])
    qs = questions if questions is not None else load_questions(a)
    out = run_dir / "gold_draft.json"
    doc = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {
        "schema": GOLD_SCHEMA, "as_of": run["as_of"], "design_sha256": run.get("design_sha256"),
        "drafter": run["grader_pin"]["gold_drafter"], "gold_prompt_sha256": GOLD_PROMPT_SHA256,
        "web_access": False, "questions": {}}

    def save(qid: str, row: dict[str, Any]) -> None:
        doc["questions"][qid] = row
        common.write_json(out, doc)
        print(f"{qid} {'drafted' if not row['draft_error'] else 'draft_error: ' + row['draft_error'][:100]}")

    rows = await draft_gold(qs, as_of, chat or await _real_chat(), doc["questions"],
                            redraft=a.redraft, on_row=save)
    doc["questions"] = rows
    common.write_json(out, doc)
    errors = sorted(q for q, r in rows.items() if r.get("draft_error"))
    print(f"wrote {out}: {len(rows)} rows, all unsigned; {len(errors)} draft_error {errors}")
    return 0 if not errors else 4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="draft the bake-off gold with the gold drafter (unsigned)")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--design")
    ap.add_argument("--practice", help="a practice-question file (P01..)")
    ap.add_argument("--qid", nargs="*", help="only these questions (default: all)")
    ap.add_argument("--redraft", action="store_true", help="re-ask questions already drafted")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
