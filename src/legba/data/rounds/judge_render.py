# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a ``verdict/2`` judge is shown, and how its reply is read back (lane
G-6).

The first real runs (lane G-5) had every judge on every panel rule every
position ``stands`` with confidence 1.0 and the same template fields filled.
They were shown 20 k characters of JSON, a card written for seats that WRITE
sentences, and an output contract whose optional fields they copied. This
module shows a judge something it can check instead:

* **A table, not JSON.** Each candidate gets a letter (A, B, …), each of its
  sentences a label (A1, A2, …); under each sentence, the exact quotes it
  cites and the row ids. No prose, no summary, nothing a seat wrote outside
  its sentences. Candidates a panel stage removed are not shown.
* **A forced check per sentence.** The contract asks for a check of EVERY
  sentence (``holds`` true/false and a problem from a closed list), the
  sentence the candidate stands or falls on (``weakest``), the outcome and a
  ranking of all candidates. The tally counts a candidate with a failing
  check as ``falls`` (:func:`legba.data.rounds.tally.entry_outcome`).
* **Read back by code.** Letters and labels map back to the candidate ids and
  sentence keys; an unknown letter is dropped (and listed), never guessed.

:data:`RENDER_VERSION` is part of a judge's calibration key: a judge measured
under one rendering is not calibrated under another. Pure.
"""
from __future__ import annotations

import json
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .rows import SENTENCE_PROBLEMS, VERDICT_REASONS

#: Bump when what a judge is shown or asked changes (calibration key).
RENDER_VERSION = "judge_render.v1"
JUDGE_ROW_TYPE = "verdict/2"
#: The rendering every other judge row type gets (``model_seats.render_prompt``:
#: the card, the handed JSON, the generated output contract as of lane G-6).
JSON_RENDER_VERSION = "json_contract.g6"


def render_version(row_type: str | None) -> str:
    """The rendering a judge writing ``row_type`` is shown (calibration key)."""
    return RENDER_VERSION if row_type == JUDGE_ROW_TYPE else JSON_RENDER_VERSION


@dataclass(frozen=True)
class JudgeView:
    """The candidates as shown: label → id, sentence label → (candidate id,
    sentence key), and the rendered text."""

    labels: Mapping[str, str]
    sentences: Mapping[str, tuple[str, str]]
    text: str
    shown: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def candidate_of(self, label: Any) -> str | None:
        raw = str(label or "").strip()
        if raw in self.labels:
            return self.labels[raw]
        if raw.upper() in self.labels:
            return self.labels[raw.upper()]
        return raw if raw in self.labels.values() else None

    def sentence_of(self, label: Any) -> str | None:
        raw = str(label or "").strip()
        hit = self.sentences.get(raw) or self.sentences.get(raw.upper())
        if hit:
            return hit[1]
        keys = {k for _, k in self.sentences.values()}
        return raw if raw in keys else None


def _letter(i: int) -> str:
    letters = string.ascii_uppercase
    return letters[i] if i < len(letters) else f"{letters[i // 26 - 1]}{letters[i % 26]}"


def positions_of(inputs: Sequence[Any]) -> list[Mapping[str, Any]]:
    """Every position of every handed sentence table, in handed order."""
    out: list[Mapping[str, Any]] = []
    for h in inputs:
        payload = getattr(h, "payload", None)
        if getattr(h, "row_type", None) == "sentence_table/1" and isinstance(payload, Mapping):
            out.extend(p for p in payload.get("positions") or () if isinstance(p, Mapping))
    return out


def build_view(inputs: Sequence[Any], candidates: Sequence[str] = ()) -> JudgeView:
    """The table a judge is shown. ``candidates`` (the panel stage's live
    candidates) restrict and order it; empty shows every handed position."""
    positions = positions_of(inputs)
    by_id = {str(p.get("position_id")): p for p in positions}
    order = [c for c in candidates if c in by_id] if candidates else list(by_id)
    labels: dict[str, str] = {}
    sentences: dict[str, tuple[str, str]] = {}
    lines: list[str] = []
    shown = []
    for i, cid in enumerate(order):
        p = by_id[cid]
        lab = _letter(i)
        labels[lab] = cid
        shown.append(p)
        head = f"### Candidate {lab}"
        if p.get("probability") is not None:
            head += f" (its probability: {p['probability']})"
        lines += ["", head]
        sents = [s for s in p.get("sentences") or () if isinstance(s, Mapping)]
        if not sents:
            lines.append("(no sentences)")
        for j, s in enumerate(sents, 1):
            sl = f"{lab}{j}"
            sentences[sl] = (cid, str(s.get("key", "")))
            lines.append(f"{sl}. {str(s.get('text', '')).strip()}")
            cited = [c for c in s.get("cited") or () if isinstance(c, Mapping)]
            quoted = [c for c in cited if c.get("quote")]
            if not quoted:
                lines.append("    cites: NO QUOTE" + (f" (rows {', '.join(str(c.get('ref_id')) for c in cited)})"
                                                      if cited else ""))
            for c in quoted:
                role = str(c.get("role") or "evidence")
                lines.append(f"    quote ({role}, row {c.get('ref_id')}): \"{str(c.get('quote')).strip()}\"")
    return JudgeView(labels, sentences, "\n".join(lines).strip(), tuple(shown))


def judge_contract(view: JudgeView) -> str:
    """The ``verdict/2`` reply shape, with the view's own labels."""
    first = next(iter(view.labels), "A")
    return "\n".join([
        f"## Output: `{JUDGE_ROW_TYPE}`",
        "Reply with ONE JSON object and nothing else, of this shape (angle brackets are what you "
        "fill in):",
        "```",
        '{"verdicts": [{"candidate": "<letter>", "checks": [{"sentence": "<label>", '
        '"holds": <true|false>, "problem": "<problem>"}], "weakest": "<label>", '
        '"outcome": "<stands|falls|unsure>", "reason": "<reason>"}], '
        '"ranking": ["<letter>", "..."]}',
        "```",
        f"- One entry in `verdicts` per candidate ({', '.join(view.labels) or first}), and in its "
        f"`checks` one check for EVERY sentence of that candidate, by its label ({first}1, "
        f"{first}2, …).",
        "- `holds` is true only when the quotes the sentence cites state everything the sentence "
        "asserts: each figure, date, name, cause, intent, forecast and degree of certainty. "
        "Otherwise it is false. A year the quote leaves out may be the year its row was "
        "published: a missing year alone does not make a sentence fail.",
        f"- `problem` is one of: {', '.join(SENTENCE_PROBLEMS)} (`none` when it holds).",
        "- `weakest` is the label of the sentence the candidate depends on most that is least "
        "supported.",
        "- `outcome` is `falls` when any check does not hold (and when your card's test fails); "
        "`stands` only when every check holds and your card's test passes.",
        f"- `reason` is one of: {', '.join(VERDICT_REASONS)}.",
        "- `ranking` lists every candidate letter, best supported first.",
    ])


def render(card_text: str, inputs: Sequence[Any], candidates: Sequence[str] = ()) -> tuple[str, str, JudgeView]:
    """``(system, user, view)`` for one ``verdict/2`` judge call."""
    view = build_view(inputs, candidates)
    system = "\n".join([card_text.strip(), "", judge_contract(view)])
    user = "\n".join(["# The candidates", "",
                      view.text or "(no candidates were handed)"])
    return system, user, view


def read_back(payload: Mapping[str, Any], view: JudgeView) -> tuple[dict[str, Any], list[str]]:
    """The reply with letters and labels mapped back to candidate ids and
    sentence keys; returns ``(payload, unmapped)``. Entries for an unknown
    candidate are dropped and listed."""
    unmapped: list[str] = []
    verdicts = []
    for v in payload.get("verdicts") or ():
        if not isinstance(v, Mapping):
            continue
        cid = view.candidate_of(v.get("candidate"))
        if cid is None:
            unmapped.append(f"candidate:{v.get('candidate')}")
            continue
        entry = dict(v)
        entry["candidate"] = cid
        checks = []
        for c in v.get("checks") or ():
            if not isinstance(c, Mapping):
                continue
            key = view.sentence_of(c.get("sentence"))
            if key is None:
                unmapped.append(f"sentence:{c.get('sentence')}")
                continue
            checks.append({**c, "sentence": key})
        entry["checks"] = checks
        if v.get("weakest") is not None:
            wk = view.sentence_of(v.get("weakest"))
            if wk is None:
                unmapped.append(f"weakest:{v.get('weakest')}")
                entry.pop("weakest", None)
            else:
                entry["weakest"] = wk
        verdicts.append(entry)
    ranking = []
    for r in payload.get("ranking") or ():
        cid = view.candidate_of(r)
        if cid is None:
            unmapped.append(f"ranking:{r}")
        elif cid not in ranking:
            ranking.append(cid)
    return {"verdicts": verdicts, "ranking": ranking}, unmapped


def decide_questions(card_text: str, view: JudgeView) -> tuple[str, dict[str, dict[str, Any]]]:
    """The same checks as typed questions for a DECISION plane (``/decide``):
    one ``choice`` question per sentence, ``holds`` / ``fails``."""
    by_label: dict[str, Mapping[str, Any]] = {}
    for lab, cid in view.labels.items():
        p = next(p for p in view.shown if str(p.get("position_id")) == cid)
        for j, s in enumerate([s for s in p.get("sentences") or () if isinstance(s, Mapping)], 1):
            by_label[f"{lab}{j}"] = s
    questions: dict[str, dict[str, Any]] = {}
    for sl, s in by_label.items():
        quotes = [str(c.get("quote")).strip() for c in s.get("cited") or ()
                  if isinstance(c, Mapping) and c.get("quote")]
        quoted = " | ".join(f'"{q}"' for q in quotes) if quotes else "(no quote)"
        questions[sl] = {
            "type": "choice",
            "instructions": (f"Sentence: {str(s.get('text', '')).strip()}\nQuotes it cites: {quoted}\n"
                             "Do the quotes state everything the sentence asserts?"),
            "criteria": {
                "holds": "the quotes state every figure, date, name, cause, intent, forecast and "
                         "degree of certainty the sentence asserts",
                "fails": "the sentence says something the quotes do not: a changed figure, date or "
                         "name; an added cause, intent or forecast; a party's claim stated as fact; "
                         "a contradiction; or no quote at all",
            },
        }
    return card_text.strip(), questions


def verdict_from_answers(view: JudgeView, answers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A ``verdict/2`` payload built in code from a decision plane's answers:
    a sentence holds when its choice is ``holds``; a candidate falls when any
    sentence fails; ``weakest`` is the sentence with the lowest P(holds); the
    ranking orders candidates by their weakest sentence's P(holds)."""
    verdicts = []
    floor: dict[str, float] = {}
    for lab, cid in view.labels.items():
        checks = []
        worst: tuple[float, str | None] = (2.0, None)
        for sl, (c, key) in view.sentences.items():
            if c != cid or sl not in answers:
                continue
            a = answers[sl]
            holds = a.get("choice") == "holds"
            p = (a.get("probabilities") or {}).get("holds")
            p = float(p) if isinstance(p, (int, float)) else (1.0 if holds else 0.0)
            if p < worst[0]:
                worst = (p, key)
            checks.append({"sentence": key, "holds": holds,
                           "problem": "none" if holds else "quote_does_not_say_it"})
        fails = any(not c["holds"] for c in checks)
        entry: dict[str, Any] = {"candidate": cid, "checks": checks,
                                 "outcome": "falls" if fails else ("stands" if checks else "unsure"),
                                 "reason": "unsupported" if fails else "supported"}
        if worst[1] is not None:
            entry["weakest"] = worst[1]
        floor[cid] = worst[0] if worst[1] is not None else 0.0
        verdicts.append(entry)
    ranking = sorted(floor, key=lambda c: (-floor[c], list(view.labels.values()).index(c)))
    return {"verdicts": verdicts, "ranking": ranking}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


__all__ = [
    "JUDGE_ROW_TYPE",
    "RENDER_VERSION",
    "JudgeView",
    "build_view",
    "decide_questions",
    "judge_contract",
    "positions_of",
    "read_back",
    "render",
    "render_version",
    "verdict_from_answers",
]
