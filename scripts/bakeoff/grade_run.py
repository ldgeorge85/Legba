#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The grader-call runner (design v1.1 §4.2–§4.4, §5.4) — the tell test and the grading.

Driven by the run directory (``<D>/run.json``) and its grading directory
(``<D>/grading``, where ``grade_sheet.py`` wrote ``tell_test.json`` and
``sheet.json``). Every call goes to the PRIMARY GRADER only —
``grade_sheet.GRADER_COMPONENT`` (the think judge on the gateway) — at
temperature 1.0 with no max_tokens, at most :data:`CONCURRENCY` at once.

Before any call, both model subcommands refuse a run with no ``grader_pin``,
a recorded route change, or a route that re-probes differently from the pin
(``grade_sheet.recheck_pin``: the drift is written to ``run.json`` and the run
is void). The fixed prompts are in this file; their sha256 goes into
``run.json`` (``tell_prompt_sha256`` / ``grade_prompt_sha256``) at first use,
and a later invocation with a different prompt is refused.

``tell-test`` — one call per answer of ``tell_test.json``: name the arm.
Writes ``tell_calls.json`` (every call, its usage, the reply's arm) and, once
every answer has an arm, ``tell_answers.json`` — exactly the
``{"<blind_id>": "<arm>"}`` map ``grade_sheet.py tell-score --predictions``
reads. A missing answer would count as a miss and flatter the criterion, so
the map is never written short (exit 4; re-run asks only the missing ones).

``grade`` — one call per row of ``sheet.json``: ONE normalised answer against
its gold, axes 1–7 (axis 8 is never shown). The reply must be the JSON shape
:func:`validate_grade` checks; a malformed reply is retried once (the problems
named), then recorded as ``grade_error``. Writes ``grades_primary.json`` keyed
by blind id after every answer (a re-run resumes). Refused unless the sheet's
key is sealed in ``run.json`` and the last tell test PASSED (§5.4: grading never
starts on a failed tell test; ``--practice`` lifts that for a practice run).
When every row has a grade or a grade_error, ``grading_closed_at`` is set —
``report.py`` opens the blind key only after it.

``grade --rater operator-sample`` — no model call. Writes
``operator_sample.json``: the stratified 20 % (seeded, ceil per class) plus
every answer the primary marked contradicted, as EMPTY grading sheets in the
same shape for the operator to fill by hand (§4.4 second rater). Which rows
were sampled and which were contradicted is kept in ``run.json``, not shown to
the operator. An existing operator file is never overwritten.

USAGE
    python3 scripts/bakeoff/grade_run.py tell-test --run-dir <D>
    python3 scripts/bakeoff/grade_run.py grade --run-dir <D>
    python3 scripts/bakeoff/grade_run.py grade --run-dir <D> --rater operator-sample [--seed S]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import re
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import grade_sheet  # noqa: E402

GRADES_SCHEMA = "legba.bakeoff.grades/1"
TELL_CALLS_SCHEMA = "legba.bakeoff.tell_calls/1"
OPERATOR_SCHEMA = "legba.bakeoff.operator_sample/1"
CONCURRENCY = 4
OPERATOR_SHARE = 0.20
STATUSES = ("stated_correct", "contradicted", "absent")
VERDICTS = ("supports", "partial", "does_not_support", "unresolvable")
TRACE_MAX = 5
GRADE_KEYS = ("gold_atoms", "error_atoms", "usefulness", "novelty_atoms", "traceability",
              "temporal", "stale_state", "stated_confidence", "coverage")

# ---------------------------------------------------------------------------
# The fixed prompts (hashed into run.json at first use)
# ---------------------------------------------------------------------------

TELL_PROMPT = """You are shown one answer to an intelligence question. It was written by one of five systems ("arms"), described below. Every answer was passed through the same deterministic normaliser before you see it, so formatting is uniform.

The arms:
{arms}

Which arm most likely produced this answer? Reply with ONE JSON object and nothing else, exactly: {{"arm": "<A, B, C, D or E>"}}

The answer:
<<<
{answer}
>>>"""

GRADE_PROMPT = """You are grading ONE answer to one question against a gold standard that was written before any answer was seen. You see no other answer and you are not told which system wrote this one. Grade only what the answer says, as of the question's as-of time {as_of}.

Question class: {klass}
Question: {question}

GOLD
Gold atoms (id: statement):
{gold_atoms}
Error atoms — tempting but wrong (id: statement):
{error_atoms}
Prior-state note (what was already established at the window start): {prior_state_note}
Absence proof (INSUFFICIENT questions only): {absence_proof}

THE ANSWER (its receipt markers [R1]..[Rk] resolve below)
<<<
{answer}
>>>

RECEIPTS (open these only for item 4)
{receipts}

Grade these items and nothing else:
1. gold_atoms: for EVERY gold atom id above exactly once, a status: "stated_correct" (the answer states it, correctly), "contradicted" (the answer states something incompatible with it), or "absent".
   error_atoms: for EVERY error atom id above exactly once, asserted true if the answer asserts it as fact, else false.
   On an INSUFFICIENT question the gold atoms describe the correct decline (not asserting the requested specific as fact; saying what is and is not established). Reporting a claimed figure AS A CLAIM, with its claimant, is not asserting it.
2. usefulness, an integer 0-4: 0 no usable content (refuses without reason, or off-question); 1 on topic but generic, a reader could not act on it; 2 answers with at least one specific (actor, date, magnitude) but omits or buries what a decision would turn on; 3 leads with the answer, gives the specifics a decision turns on, and states the main uncertainty; 4 as 3, and says what would change the assessment or what to watch next, with specifics, and nothing in it is wrong. An answer with any contradicted gold atom scores at most 2.
3. novelty_atoms: the ids of the gold atoms you marked stated_correct that are NOT already in the prior-state note.
4. traceability: up to 5 factual sentences of the answer that carry a receipt marker, each quoted, with a verdict after opening its receipts: "supports", "partial", "does_not_support", or "unresolvable" (no document behind the marker). An empty list when the answer cites nothing.
5. temporal: every dated or sequenced claim in the answer, each quoted, with right true or false (an event date within one day is right). stale_state: true if the answer describes as current a status that had changed before {as_of}.
6. stated_confidence: the number on the answer's last line "Stated confidence: 0.xx", or null when it reads "none".
7. coverage: answered is false only when the answer is empty or says nothing on the question; parts_addressed is the share, 0 to 1, of the question's parts the answer addresses.

Reply with ONE JSON object and nothing else, exactly this shape:
{{"gold_atoms": [{{"id": "g1", "status": "stated_correct"}}], "error_atoms": [{{"id": "e1", "asserted": false}}], "usefulness": 3, "novelty_atoms": ["g1"], "traceability": [{{"sentence": "...", "verdict": "supports"}}], "temporal": [{{"claim": "...", "right": true}}], "stale_state": false, "stated_confidence": 0.7, "coverage": {{"answered": true, "parts_addressed": 1.0}}}}"""

RETRY_NOTE = """Your reply did not match the required shape: {problems}. Reply again with ONE JSON object of exactly the shape asked for, and nothing else."""


def prompt_sha(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


TELL_PROMPT_SHA256 = prompt_sha(TELL_PROMPT, RETRY_NOTE)
GRADE_PROMPT_SHA256 = prompt_sha(GRADE_PROMPT, RETRY_NOTE)


class GradingRefused(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Gold atoms as the grader sees them
# ---------------------------------------------------------------------------


def atoms(gold: dict[str, Any] | None, key: str) -> list[dict[str, str]]:
    """``[{id, text}]`` for ``gold_atoms`` (ids ``g1..``) or ``error_atoms``
    (``e1..``); a bare-string atom gets its positional id."""
    prefix = "g" if key == "gold_atoms" else "e"
    out = []
    for i, a in enumerate((gold or {}).get(key) or [], 1):
        if isinstance(a, dict):
            out.append({"id": str(a.get("id") or f"{prefix}{i}"), "text": str(a.get("text") or "")})
        else:
            out.append({"id": f"{prefix}{i}", "text": str(a)})
    return out


def _render_atoms(rows: list[dict[str, str]]) -> str:
    return "\n".join(f"- {a['id']}: {a['text']}" for a in rows) or "(none)"


def _render_receipts(receipts: list[dict[str, Any]] | None) -> str:
    out = []
    for r in receipts or []:
        docs = r.get("documents") or []
        if not docs:
            out.append(f"[{r.get('r')}] (no document)")
        for d in docs:
            head = " — ".join(str(x) for x in (d.get("masthead"), d.get("date"), d.get("title"), d.get("url")) if x)
            out.append(f"[{r.get('r')}] {head}\n{d.get('body') or ''}")
    return "\n\n".join(out) or "(none)"


def grade_prompt(item: dict[str, Any]) -> str:
    gold = item.get("gold") or {}
    return GRADE_PROMPT.format(
        as_of=item.get("as_of") or "T", klass=item.get("class"), question=item.get("question"),
        gold_atoms=_render_atoms(atoms(gold, "gold_atoms")),
        error_atoms=_render_atoms(atoms(gold, "error_atoms")),
        prior_state_note=gold.get("prior_state_note") or "(none)",
        absence_proof=gold.get("absence_proof") or "(none)",
        answer=item.get("answer") or "", receipts=_render_receipts(item.get("receipts_axis_4")),
    )


def tell_prompt(test: dict[str, Any], item: dict[str, Any]) -> str:
    arms = "\n".join(f"{a['arm']}: {a['description']}" for a in test["arms"])
    return TELL_PROMPT.format(arms=arms, answer=item["answer"])


# ---------------------------------------------------------------------------
# Reply shapes
# ---------------------------------------------------------------------------


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_grade(obj: Any, gold_ids: list[str], error_ids: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    """``(grade, problems)``: the reply checked against the one grade shape —
    every gold / error atom id exactly once, the enums, the ranges. A clean
    reply comes back with only the shape's keys (usefulness an int)."""
    if not isinstance(obj, dict):
        return None, ["the reply is not a JSON object"]
    p: list[str] = [f"missing key {k!r}" for k in GRADE_KEYS if k not in obj]
    if p:
        return None, p
    ga, ea = obj["gold_atoms"], obj["error_atoms"]
    if not isinstance(ga, list) or not all(isinstance(a, dict) for a in ga):
        p.append("gold_atoms must be a list of {id, status}")
        ga = []
    ids = [str(a.get("id")) for a in ga]
    if sorted(ids) != sorted(gold_ids):
        p.append(f"gold_atoms ids {sorted(ids)} != the gold's {sorted(gold_ids)} (each exactly once)")
    p += [f"gold atom {a.get('id')}: status {a.get('status')!r} not one of {STATUSES}"
          for a in ga if a.get("status") not in STATUSES]
    if not isinstance(ea, list) or not all(isinstance(a, dict) for a in ea):
        p.append("error_atoms must be a list of {id, asserted}")
        ea = []
    eids = [str(a.get("id")) for a in ea]
    if sorted(eids) != sorted(error_ids):
        p.append(f"error_atoms ids {sorted(eids)} != the gold's {sorted(error_ids)} (each exactly once)")
    p += [f"error atom {a.get('id')}: asserted must be true or false" for a in ea
          if not isinstance(a.get("asserted"), bool)]
    u = obj["usefulness"]
    if not (_num(u) and float(u).is_integer() and 0 <= u <= 4):
        p.append(f"usefulness {u!r} is not an integer 0-4")
    correct = {str(a.get("id")) for a in ga if a.get("status") == "stated_correct"}
    nov = obj["novelty_atoms"]
    if not isinstance(nov, list) or not all(isinstance(x, str) for x in nov):
        p.append("novelty_atoms must be a list of atom ids")
    elif not set(nov) <= correct:
        p.append(f"novelty_atoms {sorted(set(nov) - correct)} are not gold atoms marked stated_correct")
    tr = obj["traceability"]
    if not isinstance(tr, list) or len(tr) > TRACE_MAX or not all(
            isinstance(t, dict) and isinstance(t.get("sentence"), str) and t.get("verdict") in VERDICTS for t in tr):
        p.append(f"traceability must be up to {TRACE_MAX} {{sentence, verdict in {VERDICTS}}}")
    tm = obj["temporal"]
    if not isinstance(tm, list) or not all(
            isinstance(t, dict) and isinstance(t.get("claim"), str) and isinstance(t.get("right"), bool) for t in tm):
        p.append("temporal must be a list of {claim, right: true|false}")
    if not isinstance(obj["stale_state"], bool):
        p.append("stale_state must be true or false")
    c = obj["stated_confidence"]
    if c is not None and not (_num(c) and 0 <= c <= 1):
        p.append(f"stated_confidence {c!r} is not null or a number 0-1")
    cov = obj["coverage"]
    if not (isinstance(cov, dict) and isinstance(cov.get("answered"), bool)
            and _num(cov.get("parts_addressed")) and 0 <= cov["parts_addressed"] <= 1):
        p.append("coverage must be {answered: true|false, parts_addressed: 0-1}")
    if p:
        return None, p
    return {
        "gold_atoms": [{"id": str(a["id"]), "status": a["status"]} for a in ga],
        "error_atoms": [{"id": str(a["id"]), "asserted": a["asserted"]} for a in ea],
        "usefulness": int(u),
        "novelty_atoms": list(dict.fromkeys(nov)),
        "traceability": [{"sentence": t["sentence"], "verdict": t["verdict"]} for t in tr],
        "temporal": [{"claim": t["claim"], "right": t["right"]} for t in tm],
        "stale_state": obj["stale_state"],
        "stated_confidence": None if c is None else float(c),
        "coverage": {"answered": cov["answered"], "parts_addressed": float(cov["parts_addressed"])},
    }, []


def parse_grade(text: str, item: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    obj = common.extract_json_object(text)
    if obj is None:
        return None, ["no JSON object in the reply"]
    gold = item.get("gold") or {}
    return validate_grade(obj, [a["id"] for a in atoms(gold, "gold_atoms")],
                          [a["id"] for a in atoms(gold, "error_atoms")])


_LONE_ARM = re.compile(r"^\W*([A-E])\W*$")


def parse_tell(text: str) -> tuple[str | None, list[str]]:
    obj = common.extract_json_object(text)
    arm = obj.get("arm") if obj else None
    if isinstance(arm, str) and arm.strip().upper() in grade_sheet.TELL_ARMS:
        return arm.strip().upper(), []
    m = _LONE_ARM.match(common._THINK.sub("", text or "").strip().upper())
    if m:
        return m.group(1), []
    return None, ['expected {"arm": "<A, B, C, D or E>"}']


def blank_grade(item: dict[str, Any]) -> dict[str, Any]:
    """The grade shape with nothing filled — the operator's sheet."""
    gold = item.get("gold") or {}
    return {
        "gold_atoms": [{"id": a["id"], "status": None} for a in atoms(gold, "gold_atoms")],
        "error_atoms": [{"id": a["id"], "asserted": None} for a in atoms(gold, "error_atoms")],
        "usefulness": None, "novelty_atoms": [], "traceability": [], "temporal": [],
        "stale_state": None, "stated_confidence": None,
        "coverage": {"answered": None, "parts_addressed": None},
    }


# ---------------------------------------------------------------------------
# One question to the grader: a call, one retry on a malformed reply
# ---------------------------------------------------------------------------


async def ask(
    chat: common.Chat, prompt: str, parse: Callable[[str], tuple[Any, list[str]]],
) -> tuple[Any, list[dict[str, Any]], str | None, str | None]:
    """``(value, calls, error, last_reply)``. Two attempts at most; the second
    carries the first reply and the problems found in it. Every call's usage
    is in ``calls``."""
    messages = [{"role": "user", "content": prompt}]
    calls: list[dict[str, Any]] = []
    problems: list[str] = []
    last: str | None = None
    for attempt in (1, 2):
        try:
            reply = await chat(messages)
        except Exception as exc:  # noqa: BLE001 — a failed call is a failed attempt, recorded
            calls.append({"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"[:300]})
            problems = [f"the call failed ({type(exc).__name__})"]
            continue
        last = reply.get("text") or ""
        calls.append({"attempt": attempt, "usage": reply.get("usage"),
                      "response_model": reply.get("response_model"), "elapsed_s": reply.get("elapsed_s")})
        value, problems = parse(last)
        if not problems:
            return value, calls, None, last
        messages = [*messages, {"role": "assistant", "content": last},
                    {"role": "user", "content": RETRY_NOTE.format(problems="; ".join(problems[:8]))}]
    return None, calls, "; ".join(problems) or "no reply", last


async def _fan_out(pending: list[Any], work: Callable[[Any], Any], concurrency: int) -> None:
    sem = asyncio.Semaphore(max(1, min(concurrency, CONCURRENCY)))

    async def one(x: Any) -> None:
        async with sem:
            await work(x)

    await asyncio.gather(*(one(x) for x in pending))


async def run_tell(
    test: dict[str, Any], chat: common.Chat, done: dict[str, Any], *,
    concurrency: int = CONCURRENCY, on_entry: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Ask the arm of every tell-test answer not yet answered in ``done``."""
    entries = dict(done)
    pending = [it for it in test["items"] if not (entries.get(it["blind_id"]) or {}).get("arm")]

    async def work(it: dict[str, Any]) -> None:
        arm, calls, err, last = await ask(chat, tell_prompt(test, it), parse_tell)
        prev = entries.get(it["blind_id"]) or {}
        entries[it["blind_id"]] = {"arm": arm, "error": err, "calls": [*(prev.get("calls") or []), *calls],
                                   "last_reply": None if arm else (last or "")[:4000],
                                   "answered_at": common.utcnow().isoformat()}
        if on_entry:
            on_entry(it["blind_id"], entries[it["blind_id"]])

    await _fan_out(pending, work, concurrency)
    return entries


async def run_grades(
    sheet: dict[str, Any], chat: common.Chat, done: dict[str, Any], *,
    concurrency: int = CONCURRENCY, retry_errors: bool = False,
    on_entry: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Grade every sheet row not yet graded in ``done`` (and, with
    ``retry_errors``, the rows recorded as ``grade_error``)."""
    entries = dict(done)

    def needed(bid: str) -> bool:
        e = entries.get(bid)
        return e is None or (retry_errors and e.get("grade") is None)

    pending = [it for it in sheet["items"] if needed(it["blind_id"])]

    async def work(it: dict[str, Any]) -> None:
        grade, calls, err, last = await ask(chat, grade_prompt(it), lambda t: parse_grade(t, it))
        prev = entries.get(it["blind_id"]) or {}
        entries[it["blind_id"]] = {"grade": grade, "grade_error": err,
                                   "calls": [*(prev.get("calls") or []), *calls],
                                   "last_reply": None if grade else (last or "")[:4000],
                                   "graded_at": common.utcnow().isoformat()}
        if on_entry:
            on_entry(it["blind_id"], entries[it["blind_id"]])

    await _fan_out(pending, work, concurrency)
    return entries


def all_closed(sheet: dict[str, Any], entries: dict[str, Any]) -> bool:
    """Every row has a grade or a grade_error."""
    return all((entries.get(it["blind_id"]) or {}).get("grade") is not None
               or (entries.get(it["blind_id"]) or {}).get("grade_error") for it in sheet["items"])


def usage_totals(entries: dict[str, Any]) -> dict[str, int]:
    tot = {"calls": 0, "failed_calls": 0, "input_tokens": 0, "output_tokens": 0}
    for e in entries.values():
        for c in e.get("calls") or []:
            tot["calls"] += 1
            if c.get("error"):
                tot["failed_calls"] += 1
            u = c.get("usage") or {}
            tot["input_tokens"] += int(u.get("input_tokens") or 0)
            tot["output_tokens"] += int(u.get("output_tokens") or 0)
    return tot


# ---------------------------------------------------------------------------
# The operator's second-rater sheet
# ---------------------------------------------------------------------------


def contradicted_ids(grades: dict[str, Any]) -> set[str]:
    return {bid for bid, e in grades.items() if e.get("grade")
            and any(a["status"] == "contradicted" for a in e["grade"]["gold_atoms"])}


def operator_sample(
    sheet: dict[str, Any], grades: dict[str, Any], *, seed: int, share: float = OPERATOR_SHARE,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """``(operator file, membership)``: ceil(share × n) answers per class drawn
    with ``random.Random(seed)`` from the class's sorted blind ids, plus every
    answer the primary marked contradicted. ``membership`` (blind id → why) is
    for run.json — the operator's file never says which the primary flagged."""
    by_class: dict[str, list[str]] = {}
    for it in sheet["items"]:
        by_class.setdefault(it["class"], []).append(it["blind_id"])
    rng = random.Random(seed)
    sampled: set[str] = set()
    for klass in sorted(by_class):
        ids = sorted(by_class[klass])
        sampled |= set(rng.sample(ids, math.ceil(share * len(ids))))
    flagged = contradicted_ids(grades)
    membership: dict[str, list[str]] = {}
    items: dict[str, Any] = {}
    for it in sheet["items"]:
        bid = it["blind_id"]
        why = [w for w, hit in (("sample", bid in sampled), ("contradicted", bid in flagged)) if hit]
        if not why:
            continue
        membership[bid] = why
        items[bid] = {k: it.get(k) for k in ("blind_id", "question_id", "question", "class", "as_of",
                                             "gold", "answer", "receipts_axis_4")}
        items[bid]["grade"] = blank_grade(it)
    doc = {
        "schema": OPERATOR_SCHEMA, "rater": "operator", "rater_name": None,
        "instructions": ("Grade each answer on its own, axes 1-7, exactly as the grade shape asks "
                         "fill every status / asserted / usefulness / stale_state / "
                         "stated_confidence / coverage field. A row counts once every field validates."),
        "design_sha256": sheet.get("design_sha256"), "seed": seed, "share": share, "items": items,
    }
    return doc, membership


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _check_prompt(run: dict[str, Any], field: str, sha: str) -> None:
    have = run.get(field)
    if have and have != sha:
        raise GradingRefused(f"run.json {field} {have[:12]} != this script's {sha[:12]}: the fixed prompt "
                             "changed mid-run")
    run[field] = sha


def _sealed(run: dict[str, Any], purpose: str) -> None:
    if not any(k.get("purpose") == purpose for k in run.get("blind_keys") or []):
        raise GradingRefused(f"run.json carries no sealed {purpose!r} key — build it with grade_sheet.py first")


async def _real_chat() -> common.Chat:  # pragma: no cover — G1 only
    common.load_env()
    route = await common.open_chat_route(common.Registry.from_env(), grade_sheet.GRADER_COMPONENT)
    return route.complete


async def tell_main(a: argparse.Namespace, *, chat: common.Chat | None = None, probe: Any = None) -> int:
    run_dir, gdir = Path(a.run_dir), Path(a.grading_dir or Path(a.run_dir) / "grading")
    run = await grade_sheet.recheck_pin(run_dir, probe=probe)
    _sealed(run, "tell-test")
    _check_prompt(run, "tell_prompt_sha256", TELL_PROMPT_SHA256)
    common.save_run(run_dir, run)
    test = _load(gdir / "tell_test.json")
    calls_path = gdir / "tell_calls.json"
    doc = _load(calls_path) if calls_path.is_file() else {
        "schema": TELL_CALLS_SCHEMA, "grader": run["grader_pin"]["primary_grader"],
        "tell_prompt_sha256": TELL_PROMPT_SHA256, "items": {}}

    def save(bid: str, entry: dict[str, Any]) -> None:
        doc["items"][bid] = entry
        common.write_json(calls_path, doc)

    entries = await run_tell(test, chat or await _real_chat(), doc["items"], concurrency=a.concurrency,
                             on_entry=save)
    doc["items"] = entries
    common.write_json(calls_path, doc)
    run = common.load_run(run_dir)
    run["tell_usage"] = usage_totals(entries)
    common.save_run(run_dir, run)
    answers = {it["blind_id"]: entries[it["blind_id"]]["arm"] for it in test["items"]
               if (entries.get(it["blind_id"]) or {}).get("arm")}
    missing = len(test["items"]) - len(answers)
    if missing:
        print(f"tell test: {missing} of {len(test['items'])} answers have no arm (see {calls_path}); "
              "re-run to ask them again — tell_answers.json is never written short")
        return 4
    common.write_json(gdir / "tell_answers.json", answers)
    print(f"wrote {gdir / 'tell_answers.json'} ({len(answers)} answers) — score it with grade_sheet.py tell-score")
    return 0


def _tell_passed(run: dict[str, Any]) -> bool:
    tests = run.get("tell_tests") or []
    return bool(tests) and tests[-1].get("passed") is True


async def grade_main(a: argparse.Namespace, *, chat: common.Chat | None = None, probe: Any = None) -> int:
    run_dir, gdir = Path(a.run_dir), Path(a.grading_dir or Path(a.run_dir) / "grading")
    run = common.load_run(run_dir)
    if a.rater == "operator-sample":
        return operator_main(a, run, run_dir, gdir)
    if run.get("grading_closed_at"):
        if not a.retry_errors:
            raise GradingRefused(f"grading closed at {run['grading_closed_at']}; the grades stand")
        if run.get("blind_key_opened_at"):
            raise GradingRefused("the blind key has been opened (report.py): no grading after that")
    if not a.practice and not _tell_passed(run):
        raise GradingRefused("the last tell test did not pass (or none was scored): grading "
                             "never starts on a failed tell test")
    run = await grade_sheet.recheck_pin(run_dir, probe=probe)
    _sealed(run, "sheet")
    _check_prompt(run, "grade_prompt_sha256", GRADE_PROMPT_SHA256)
    run.pop("grading_closed_at", None)
    common.save_run(run_dir, run)
    sheet_path = gdir / "sheet.json"
    sheet = _load(sheet_path)
    sheet_sha = common.sha256_file(sheet_path)
    out = gdir / "grades_primary.json"
    doc = _load(out) if out.is_file() else {
        "schema": GRADES_SCHEMA, "rater": "primary", "grader": run["grader_pin"]["primary_grader"],
        "grade_prompt_sha256": GRADE_PROMPT_SHA256, "sheet_sha256": sheet_sha, "practice": bool(a.practice),
        "items": {}}
    if doc.get("sheet_sha256") != sheet_sha:
        raise GradingRefused(f"{out} grades a different sheet.json (sha {str(doc.get('sheet_sha256'))[:12]})")

    def save(bid: str, entry: dict[str, Any]) -> None:
        doc["items"][bid] = entry
        common.write_json(out, doc)

    entries = await run_grades(sheet, chat or await _real_chat(), doc["items"], concurrency=a.concurrency,
                               retry_errors=a.retry_errors, on_entry=save)
    doc["items"] = entries
    common.write_json(out, doc)
    run = common.load_run(run_dir)
    run["grade_usage"] = usage_totals(entries)
    errors = sum(1 for e in entries.values() if e.get("grade") is None)
    if all_closed(sheet, entries):
        run["grading_closed_at"] = common.utcnow().isoformat()
        run["grades_primary_sha256"] = common.sha256_file(out)
    common.save_run(run_dir, run)
    print(f"wrote {out}: {len(entries)} of {len(sheet['items'])} rows, {errors} grade_error"
          + (f"; grading closed at {run['grading_closed_at']}" if run.get("grading_closed_at") else ""))
    return 0 if run.get("grading_closed_at") else 4


def operator_main(a: argparse.Namespace, run: dict[str, Any], run_dir: Path, gdir: Path) -> int:
    refusal = grade_sheet.grading_refusal(run)
    if refusal:
        raise GradingRefused(refusal)
    if not run.get("grading_closed_at"):
        raise GradingRefused("the primary grading is not closed — every row needs a grade or a grade_error "
                             "before the contradicted set is known")
    out = gdir / "operator_sample.json"
    if out.exists():
        raise GradingRefused(f"{out} exists — the operator's sheet is never overwritten")
    sheet = _load(gdir / "sheet.json")
    grades = _load(gdir / "grades_primary.json")["items"]
    seed = a.seed if a.seed is not None else int(sheet["seed"])
    doc, membership = operator_sample(sheet, grades, seed=seed)
    common.write_json(out, doc)
    run["operator_sample"] = {
        "seed": seed, "share": OPERATOR_SHARE, "rows": len(membership),
        "sampled": sum(1 for w in membership.values() if "sample" in w),
        "contradicted": sum(1 for w in membership.values() if "contradicted" in w),
        "membership": membership, "built_at": common.utcnow().isoformat(),
    }
    common.save_run(run_dir, run)
    print(f"wrote {out}: {len(membership)} rows for the operator "
          f"({run['operator_sample']['sampled']} sampled, {run['operator_sample']['contradicted']} contradicted)")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="bake-off grader calls: the tell test and the grading")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("tell-test", "grade"):
        s = sub.add_parser(name)
        s.add_argument("--run-dir", required=True)
        s.add_argument("--grading-dir", help="default <run-dir>/grading")
        s.add_argument("--concurrency", type=int, default=CONCURRENCY, help=f"at most {CONCURRENCY}")
        if name == "grade":
            s.add_argument("--rater", choices=("primary", "operator-sample"), default="primary")
            s.add_argument("--seed", type=int, help="operator-sample: default the sheet's seed")
            s.add_argument("--retry-errors", action="store_true",
                           help="re-ask the grade_error rows (refused once the blind key is opened)")
            s.add_argument("--practice", action="store_true", help="a practice run: no tell test required")
    return ap.parse_args(argv)


async def main_async(a: argparse.Namespace, *, chat: common.Chat | None = None, probe: Any = None) -> int:
    if a.cmd == "tell-test":
        return await tell_main(a, chat=chat, probe=probe)
    return await grade_main(a, chat=chat, probe=probe)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
