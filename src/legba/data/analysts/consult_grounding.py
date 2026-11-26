# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
""""Record only", ENFORCED — the post-verification pass.

THE DEFECT. A reviewed consult was told "Answer from the record
only. Cite every statement to its row … do not fill from general knowledge".
Its answer still carried sentences resting on no row — general expectations of
"what would normally appear within 72 hours", a parenthetical "[cited in
situation frame; see … context]", judgments — and its judge listed four
unsupported sentences that went out unmarked. The request was a request; the
judge's list changed nothing.

GROUNDING MODE. ``record_only`` when the question asks for it in words
(:func:`grounding_mode_from_words`) or the console sets
``inputs[0]["grounding_mode"] = "record_only"``; else ``default``.

WHAT IS DONE TO EACH SENTENCE (``record_only``), and why per kind. A unit is a
sentence of the MODEL's prose (a table row is one unit; headings, rules, code
and the code-built blocks are never units). Its refs are the reference
COLLECTOR's (``journal_evidence.extract_inline_refs``: every bracket form,
short ids mapped to the turn's own cited ids by unique prefix) and they must
pass the RESOLUTION GATE (``journal_evidence.resolve_ref_existence``).

* ``fact`` (asserts something about the world):
  - a resolving row the verifier did not list → KEPT, untouched;
  - no resolving row → CUT and counted. A statement of fact with no row is
    exactly what the asker forbade, and a label would still publish it;
  - a resolving row the verifier listed unsupported/contradicted → CUT when
    the judge is a strong rung; LABELLED when it is a weak rung (an 8B
    verdict is not strong enough to delete text on).
* ``inference`` (a judgment, an assessment, a probability word): with a
  resolving, unlisted row → kept untouched; otherwise KEPT and LABELLED as the
  consult's inference from the cited rows above. Judgment is what the asker
  asked for in its "Judgment" section; cutting it would cut the analysis.
* ``general`` (general-knowledge filler — "normally", "typically", "one would
  expect"): CUT, unless the question asks for expectations, in which case it is
  MOVED under a labelled "Not from the record" heading at the end of the prose.
* ``record`` (a statement about the record itself — "the record holds no
  claim", "not collected", "none found in the window"): KEPT. It is the answer
  the asker required ("where the record has nothing, say so"); it rests on the
  turn's reads and the coverage block, not on a row.
* ``meta`` (the answer describing its own method) and ``proposal`` (a dated
  indicator, a forecast to seal, a "mover"): KEPT — they assert nothing about
  the world now. Labels (a short bold lead-in, a line ending in ':') are not
  sentences and are never units.

Numbers follow the journal's number-support rule
(``span_evidence_checks.unsupported_numbers``): a cardinal no cited row states
is marked `` [[number_unsupported]]`` after the number; the sentence stays.
Every sentence of the prose is checked (there is no clip on the answer side);
each cited row is read whole up to :data:`NUMBER_CHECK_ROW_CHARS` (Q-2 — it
was the judge bridge's 4,000), and ``number_check`` in the accounting says how
many rows were longer and how many characters of them went unread.

The model's supported sentences are never rewritten: units are cut whole,
moved whole, or get a label appended; separators are kept as written.

In the ``default`` mode NOTHING is cut or labelled; the same classification
runs and its accounting rides on the turn's data (``would_*`` counts).

THE VERIFIER'S LIST (rule D). Every item the judge listed is located back in
the prose and its action recorded (cut / labelled / advisory / in a code-built
block / not located). When the judge did not judge the turn, the accounting
says so and claims no verifier enforcement; when it judged only the first N
cited sentences, the accounting says the rest were checked for a resolving row
only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..journal_evidence import extract_inline_refs, is_prefix_ref, resolve_ref_existence
from ..provenance.span_evidence_checks import unsupported_numbers

logger = logging.getLogger(__name__)

__all__ = [
    "GROUNDING_DEFAULT",
    "GROUNDING_RECORD_ONLY",
    "GROUNDING_SCHEMA",
    "INFERENCE_LABEL",
    "NOT_FROM_RECORD_HEADING",
    "WEAK_VERIFIER_LABEL",
    "Unit",
    "classify",
    "enforce",
    "expectations_requested",
    "grounding_mode_from_words",
    "render_accounting",
    "split_units",
]

GROUNDING_SCHEMA = "consult_grounding/1"
GROUNDING_RECORD_ONLY = "record_only"
GROUNDING_DEFAULT = "default"

INFERENCE_LABEL = " *(the consult's inference from the cited rows above, not a record row)*"
WEAK_VERIFIER_LABEL = " *(a weak verifier found the cited row may not support this)*"
NUMBER_MARK = " [[number_unsupported]]"
NOT_FROM_RECORD_HEADING = "## Not from the record — general expectations (you asked for these)"
#: Q-2 — how much of ONE cited row the number check reads. The row is read
#: whole below this (three times the 65,000-character cap on a finding body);
#: a longer one is read to this length and the accounting says how much was
#: not. A bound on the hot path: the check is pure code over text already
#: fetched, each row tokenised once (``span_evidence_checks._source_index``).
NUMBER_CHECK_ROW_CHARS = 200_000

#: Verdict reasons that ENFORCE (the journal pre-publish pass's own set).
ENFORCING_REASONS = frozenset({"judge_unsupported", "judge_contradicted", "unresolved_citation"})

_RECORD_ONLY_RE = re.compile(
    r"\b(?:answer|respond|work|reply)\s+(?:only\s+)?from\s+the\s+record"
    r"|\brecord[- ]only\b|\bfrom\s+the\s+record\s+only\b"
    r"|\bdo\s+not\s+fill\s+(?:it\s+)?(?:in\s+)?from\s+general\s+knowledge\b"
    r"|\bno\s+general\s+knowledge\b|\bonly\s+what\s+the\s+record\s+(?:holds|says|shows)\b",
    re.IGNORECASE,
)
_EXPECT_RE = re.compile(
    r"\bwhat\s+would\s+(?:normally|typically|usually|ordinarily)\b"
    r"|\b(?:normally|typically|usually)\s+(?:appear|happen|be\s+expected|expect)"
    r"|\bexpectations?\b|\bexpected\s+(?:to\s+)?(?:see|appear)\b",
    re.IGNORECASE,
)
_GENERAL_RE = re.compile(
    r"\b(?:normally|typically|usually|ordinarily|generally|as\s+a\s+rule|in\s+general"
    r"|by\s+convention|standard\s+practice|historically,|is\s+common(?:ly)?|tend(?:s)?\s+to"
    r"|one\s+would\s+(?:expect|see)|would\s+(?:normally|typically|usually)"
    r"|it\s+is\s+(?:usual|typical|common))\b",
    re.IGNORECASE,
)
_RECORD_RE = re.compile(
    r"\b(?:the\s+record\s+(?:holds|has|contains|shows|carries|gives)\s+(?:no|nothing)"
    r"|no\s+(?:row|rows|record|claim|advisory|attribution)s?\s+(?:in|of|for)\s+the\s+(?:record|window)"
    r"|not\s+in\s+the\s+record|nothing\s+in\s+the\s+record|(?:did|do)\s+not\s+collect"
    r"|(?:was|were)\s+not\s+collected|the\s+record\s+is\s+silent|absent\s+from\s+the\s+record"
    r"|we\s+hold\s+no|holds?\s+no\s+row)",
    re.IGNORECASE,
)
_INFERENCE_RE = re.compile(
    r"\b(?:I|we)\s+(?:assess|judge|grade|read|estimate|think|infer|conclude|rate)\b"
    r"|\b(?:likely|unlikely|probabl[ey]|possibl[ey]|plausibl[ey]|suggests?|indicates?"
    r"|appears?|seems?|consistent\s+with|reads\s+as|implies|points\s+to|in\s+my\s+(?:judg(?:e)?ment|reading)"
    r"|my\s+reading|the\s+weakest\s+link|roughly\s+even|almost\s+certain(?:ly)?)\b",
    re.IGNORECASE,
)

#: A sentence leaning on what is ON SCREEN (the ``on_screen`` context) — context,
#: never evidence: with no resolving row it is cut in ``record_only``.
_SCREEN_RE = re.compile(
    r"\b(?:on\s+(?:the\s+|your\s+)?screen|in\s+(?:your\s+)?view\b|you\s+can\s+see"
    r"|(?:the|your)\s+(?:map|panel|view|wall|board|page)\s+(?:shows|displays|has|is\s+showing)"
    r"|as\s+(?:shown|displayed)\s+(?:on|in)|visible\s+(?:on|in)\s+the)",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_LIST_RE = re.compile(r"^(\s*(?:[-*+]|\d{1,3}[.)])\s+)")
_QUOTE_RE = re.compile(r"^(\s*>\s?)")
#: The judge's own sentence boundary (``consult_judging``), with spans kept.
_SENTENCE_SPLIT_RE = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?]\*\*)|(?<=[.!?][*_\"”’)\]]))\s+(?=[\"“(*_\[A-Z0-9])"
)
_BRACKETS_RE = re.compile(r"\[\[[^\]]*\]\]|\[[^\]\n]*\]|【[^】]*】")
_NOISE_RE = re.compile(r"[*_`|>#]+")


@dataclass
class Unit:
    """One sentence (or table row) of the model's prose."""

    line: int
    start: int
    end: int
    text: str
    table_row: bool = False
    kind: str = "fact"
    refs: list[str] = field(default_factory=list)
    resolving: list[str] = field(default_factory=list)
    verifier: dict[str, Any] | None = None
    action: str = "kept_with_rows"
    numbers: list[dict[str, Any]] = field(default_factory=list)


def grounding_mode_from_words(question: str) -> bool:
    return bool(_RECORD_ONLY_RE.search(question or ""))


def expectations_requested(question: str) -> bool:
    return bool(_EXPECT_RE.search(question or ""))


def _norm(text: str) -> str:
    text = _BRACKETS_RE.sub(" ", text or "")
    text = _NOISE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def split_units(body: str) -> tuple[list[str], list[Unit]]:
    """``(lines, units)`` over the model's prose."""
    lines = (body or "").split("\n")
    units: list[Unit] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or _HEADING_RE.match(line) or _RULE_RE.match(line):
            continue
        if _TABLE_SEP_RE.match(line):
            continue
        if line.lstrip().startswith("|"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if _TABLE_SEP_RE.match(nxt):
                continue  # the header row
            units.append(Unit(i, 0, len(line), line, table_row=True))
            continue
        offset = 0
        for rx in (_QUOTE_RE, _LIST_RE):
            m = rx.match(line[offset:])
            if m:
                offset += m.end()
        body_text = line[offset:]
        pos = 0
        for sep in _SENTENCE_SPLIT_RE.finditer(body_text):
            if body_text[pos:sep.start()].strip():
                units.append(Unit(i, offset + pos, offset + sep.start(), body_text[pos:sep.start()]))
            pos = sep.end()
        if body_text[pos:].strip():
            units.append(Unit(i, offset + pos, len(line), body_text[pos:].rstrip()))
    return lines, units


#: A negation near a word naming the record / the collection: "none found in the
#: window", "unresolved in the record", "Iran does not appear in any A or B row".
_ABSENCE_NEAR_RE = re.compile(
    r"\b(?:no|none|not|neither|nothing|zero|absent|unresolved|paused|lacks?|missing)\b"
    r".{0,120}\b(?:record|rows?|window|collected|collection|fleet|feeds?|held)\b"
    r"|\b(?:record|rows?|window|collection|fleet|feeds?)\b.{0,40}\b(?:no|none|not|nothing|zero|paused)\b"
    r"|\bnot\s+held\b|\bdo\s+not\s+have\b|:\W*none\b",
    re.IGNORECASE,
)
#: The answer talking about ITSELF (its method, its window): not a world claim.
_META_RE = re.compile(
    r"^\W*(?:survey\s+window|window\s+used|method|scope)\b"
    r"|\bthis\s+(?:consult|answer|reading)\b|\bI\s+(?:used|searched|read|called|queried|state)\b",
    re.IGNORECASE,
)
#: A proposal or an observable to watch — a forecast, a "mover", a dated
#: indicator: about the future, not an assertion about the world now.
_PROPOSAL_RE = re.compile(
    r"\bby\s+\**\d{4}-\d{2}-\d{2}\b|^\W*(?:mover|observation that would|what would change)\b"
    r"|\bp\s*=\s*\**0?\.\d+",
    re.IGNORECASE,
)


def is_structure(text: str) -> bool:
    """A label, not a sentence: a short all-emphasis lead-in or a line ending in ':'."""
    bare = re.sub(r"[*_`]+", "", text).strip()
    if not bare:
        return True
    if bare.endswith(":"):
        return True
    emphasised = re.fullmatch(r"\s*(\*\*|\*|_)[^*_]+\1[.:]?\s*", text) is not None
    return emphasised and len(bare.split()) <= 14


def classify(text: str) -> str:
    """``record`` | ``meta`` | ``proposal`` | ``general`` | ``inference`` | ``fact``."""
    if _RECORD_RE.search(text) or _ABSENCE_NEAR_RE.search(text):
        return "record"
    if _META_RE.search(text):
        return "meta"
    if _PROPOSAL_RE.search(text):
        return "proposal"
    if _GENERAL_RE.search(text):
        return "general"
    if _INFERENCE_RE.search(text):
        return "inference"
    return "fact"


_DIGIT_SHORT_RE = re.compile(r"(?:\[|,\s*)([0-9]{8})(?=\s*[\],])")


def _unit_refs(text: str, cited: Sequence[str]) -> list[str]:
    """The collector's refs, short ids mapped to the turn's own cited ids.

    The collector skips an all-digit 8-character token (``[20261027]`` is a
    date); here such a token counts when it prefixes EXACTLY ONE of the turn's
    cited ids, the judge's own rule (``consult_judging._resolve_token``).
    """
    out: list[str] = []
    for m in _DIGIT_SHORT_RE.finditer(text):
        hits = {c for c in cited if c.startswith(m.group(1))}
        if len(hits) == 1 and (hit := next(iter(hits))) not in out:
            out.append(hit)
    for ref in extract_inline_refs(text):
        if is_prefix_ref(ref):
            hits = {c for c in cited if c.startswith(ref[:8])}
            ref = next(iter(hits)) if len(hits) == 1 else ref
        if ref not in out:
            out.append(ref)
    return out


def _locate(units: Sequence[Unit], claim: str) -> Unit | None:
    needle = _norm(claim)[:80]
    if len(needle) < 12:
        return None
    for u in units:
        hay = _norm(u.text)
        if hay.startswith(needle) or needle in hay or (len(hay) >= 24 and hay[:80] in _norm(claim)):
            return u
    return None


async def _resolve(pool: Any, refs: Sequence[str]) -> set[str] | None:
    if pool is None:
        return None
    try:
        async with pool.acquire() as conn:
            got = await resolve_ref_existence(conn, list(refs))
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        logger.warning("consult.grounding.resolve_failed err=%s", exc)
        return None
    return None if got is None else {str(r).lower() for r in got}


async def _ref_texts(pool: Any, refs: Sequence[str]) -> tuple[dict[str, str], dict[str, Any]]:
    """``(ref → its row text, coverage)`` for the number rule (the journal's
    citation bridge, read WHOLE — Q-2).

    Until Q-2 the bridge's judge cap (4,000 characters per row) applied here
    too, so a number a long cited row states past its 4,000th character was
    marked unsupported. The row is now read whole up to
    :data:`NUMBER_CHECK_ROW_CHARS`; a row longer than that is checked to that
    length, and ``coverage`` says how many rows and characters were not read.
    """
    full = list(dict.fromkeys(r for r in refs if not is_prefix_ref(r)))
    cover: dict[str, Any] = {"rows": 0, "rows_clipped": 0, "chars_unchecked": 0,
                             "row_chars_cap": NUMBER_CHECK_ROW_CHARS}
    if pool is None or not full:
        return {}, cover
    try:
        from ...runtime.actor_critic import _resolve_journal_citation_bridge

        async with pool.acquire() as conn:
            bridge = await _resolve_journal_citation_bridge(conn, list(full), text_chars=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("consult.grounding.bridge_failed err=%s", exc)
        return {}, {**cover, "state": "unavailable"}
    out: dict[str, str] = {}
    for ref, entry in zip(full, bridge):
        text = " ".join(str(entry.get(k) or "") for k in ("title", "source_text")).strip()
        if not text:
            continue
        cover["rows"] += 1
        if len(text) > NUMBER_CHECK_ROW_CHARS:
            cover["rows_clipped"] += 1
            cover["chars_unchecked"] += len(text) - NUMBER_CHECK_ROW_CHARS
            text = text[:NUMBER_CHECK_ROW_CHARS]
        out[ref.lower()] = text
    return out, cover


def _numbers(unit: Unit, texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """The journal's number-support rule over one unit, refs as ``[[ref:<uuid>]]``."""
    if not unit.resolving or not texts:
        return []
    check = unit.text
    for ref in unit.refs:
        if not is_prefix_ref(ref):
            check = re.sub(r"\[" + re.escape(ref[:8]) + r"[0-9a-f-]*\]", f"[[ref:{ref}]]",
                           check, flags=re.IGNORECASE)
    resolve: Callable[[str], str | None] = lambda r: texts.get(r.lower())  # noqa: E731
    try:
        flagged = list(unsupported_numbers(check, resolve))
    except Exception:  # noqa: BLE001 — the rule is advisory here, never fatal
        return []
    return [n for n in flagged if _material_number(n, unit)]


def _material_number(n: Mapping[str, Any], unit: Unit) -> bool:
    """Whether a flagged number is a QUANTITY worth marking in a consult answer.

    The journal's rule was tuned on first-person journal prose. Replayed over a
    consult answer (the reviewed turn) most of its flags were not quantities: a
    product name ("Microsoft 365"), a clock time ("1 pm"), a count word over
    the answer's own structure ("two major outages"), or a table cell, whose
    clause-to-ref binding the rule cannot see. Those are left unmarked; a digit
    quantity of ten or more in prose is marked.
    """
    if unit.table_row:
        return False
    phrase = str(n.get("phrase") or "")
    m = re.match(r"\s*([\d][\d,.]*)\s*(k|m|bn|million|billion|thousand)?\b", phrase, re.IGNORECASE)
    if not m:
        return False
    if re.match(r"\s*\d{1,2}\s*(?:am|pm)\b", phrase, re.IGNORECASE):
        return False
    num = re.escape(m.group(1))
    if re.search(rf"(?<![\d.,]){num}[-/:]\d|\d[-/:]{num}(?!\d)", unit.text):
        return False  # a date or a clock reading ("11-13", "13 03:17"), not a count
    at = unit.text.find(m.group(1))
    if at > 0 and re.search(r"[A-Z][A-Za-z]+\s$", unit.text[max(0, at - 30):at]):
        return False  # "Microsoft 365", "Copilot 2" — a name, not a count
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return False
    return value >= 10 or bool(m.group(2))


async def enforce(
    body: str,
    *,
    mode: str,
    expectations: bool,
    cited_refs: Sequence[str],
    verification: Mapping[str, Any] | None,
    pool: Any,
    on_screen: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """``(new_body, accounting)`` — the body is unchanged in the default mode."""
    cited = [str(r).lower() for r in cited_refs]
    lines, units = split_units(body)
    units = [u for u in units if not is_structure(u.text)]
    all_refs: list[str] = []
    for u in units:
        u.kind = classify(u.text)
        u.refs = _unit_refs(u.text, cited)
        all_refs += [r for r in u.refs if r not in all_refs]
    resolved = await _resolve(pool, all_refs)
    for u in units:
        u.resolving = [r for r in u.refs if resolved is not None and r in resolved]

    v = verification or {}
    judged = v.get("state") == "judged"
    weak = bool(v.get("weak_judge"))
    listed = v.get("unsupported_claims") or v.get("unsupported") or []
    verifier_items: list[dict[str, Any]] = []
    for item in listed if judged else []:
        reason = str(item.get("reason") or "")
        claim = str(item.get("text") or item.get("claim") or "")
        entry: dict[str, Any] = {"text": claim[:300], "reason": reason}
        unit = _locate(units, claim)
        if reason not in ENFORCING_REASONS:
            entry["action"] = "advisory_no_action"
        elif unit is None:
            entry["action"] = "not_located_in_model_prose"
        else:
            unit.verifier = {"reason": reason}
            entry["unit"] = units.index(unit)
        verifier_items.append(entry)

    texts, number_cover = await _ref_texts(pool, [r for u in units for r in u.resolving])
    for u in units:
        has_row = bool(u.resolving)
        listed_bad = u.verifier is not None
        if not has_row and _SCREEN_RE.search(u.text):
            # Q-1 / U-6: what is on screen is context, never a sentence's support.
            u.action = "cut_screen_as_evidence"
        elif u.kind == "record":
            u.action = "kept_record_statement"
        elif u.kind in ("meta", "proposal"):
            u.action = f"kept_{u.kind}"
        elif u.kind == "general":
            u.action = "moved_not_from_record" if expectations else "cut_general_knowledge"
        elif u.kind == "inference":
            u.action = "kept_with_rows" if has_row and not listed_bad else "labelled_inference"
        elif not has_row:
            u.action = "cut_unresolved_rows" if u.refs else "cut_no_row"
        elif listed_bad:
            u.action = "labelled_weak_verifier" if weak else "cut_verifier_unsupported"
        else:
            u.action = "kept_with_rows"
        if u.action.startswith("kept") or u.action.startswith("labelled"):
            u.numbers = _numbers(u, texts)
    for entry in verifier_items:
        if "unit" in entry:
            entry["action"] = units[entry.pop("unit")].action

    counts: dict[str, int] = {}
    for u in units:
        counts[u.action] = counts.get(u.action, 0) + 1
    numbers = sum(len(u.numbers) for u in units)
    accounting: dict[str, Any] = {
        "schema": GROUNDING_SCHEMA,
        "mode": mode,
        "applied": mode == GROUNDING_RECORD_ONLY,
        "expectations_requested": expectations,
        "on_screen_present": on_screen is not None,
        "units": len(units),
        "counts": counts,
        "numbers_unsupported": numbers,
        # Q-2 — what the number check read: cited rows whole, up to the cap.
        "number_check": number_cover,
        "refs_checked": len(all_refs),
        "refs_resolved": None if resolved is None else len([r for r in all_refs if r in resolved]),
        "resolution": "unavailable" if resolved is None else "checked",
        "verifier": {
            "state": v.get("state"),
            "reason": v.get("reason"),
            "weak": weak,
            "model": v.get("judge_model"),
            "score": v.get("score"),
            "cited_sentences": v.get("cited_sentences"),
            "claims_judged": v.get("claims_judged"),
            "items": verifier_items,
        },
        "cut": [u.text[:300] for u in units if u.action.startswith("cut")],
        "labelled": [u.text[:300] for u in units if u.action.startswith("labelled")],
        "moved": [u.text[:300] for u in units if u.action == "moved_not_from_record"],
        "numbers": [
            {"phrase": n.get("phrase"), "sentence": u.text[:200]}
            for u in units for n in u.numbers
        ][:40],
    }
    if mode != GROUNDING_RECORD_ONLY or resolved is None:
        if resolved is None and mode == GROUNDING_RECORD_ONLY:
            accounting["applied"] = False
            accounting["not_applied_reason"] = "references could not be resolved"
        return body, accounting

    # Apply, line by line, right to left within a line so spans stay valid.
    by_line: dict[int, list[Unit]] = {}
    for u in units:
        by_line.setdefault(u.line, []).append(u)
    moved: list[str] = []
    drop_lines: set[int] = set()
    for idx, line_units in by_line.items():
        line = lines[idx]
        for u in sorted(line_units, key=lambda x: -x.start):
            if u.action.startswith("cut") or u.action == "moved_not_from_record":
                if u.action == "moved_not_from_record":
                    moved.append(u.text.strip())
                nxt = len(line)
                later = [x.start for x in line_units if x.start > u.start]
                if later:
                    nxt = min(later)
                line = line[:u.start] + line[nxt:]
                continue
            seg = line[u.start:u.end]
            for n in sorted(u.numbers, key=lambda n: -len(str(n.get("phrase") or ""))):
                phrase = str(n.get("phrase") or "")
                at = seg.find(phrase) if phrase else -1
                if at >= 0 and NUMBER_MARK not in seg[at + len(phrase):at + len(phrase) + len(NUMBER_MARK)]:
                    seg = seg[: at + len(phrase)] + NUMBER_MARK + seg[at + len(phrase):]
            if u.action == "labelled_inference":
                seg = seg.rstrip() + INFERENCE_LABEL
            elif u.action == "labelled_weak_verifier":
                seg = seg.rstrip() + WEAK_VERIFIER_LABEL
            line = line[:u.start] + seg + line[u.end:]
        prefix = (_QUOTE_RE.match(line) or _LIST_RE.match(line))
        rest = line[prefix.end():] if prefix else line
        if lines[idx].strip() and not rest.strip():
            drop_lines.add(idx)
        else:
            lines[idx] = line.rstrip()
    new_body = "\n".join(l for i, l in enumerate(lines) if i not in drop_lines)
    if moved:
        new_body = new_body.rstrip() + "\n\n" + NOT_FROM_RECORD_HEADING + "\n\n" + "\n".join(
            f"- {m}" for m in moved
        )
    return new_body, accounting


_ACTION_WORDS = {
    "cut_no_row": "cut", "cut_unresolved_rows": "cut", "cut_verifier_unsupported": "cut",
    "cut_general_knowledge": "cut", "cut_screen_as_evidence": "cut", "labelled_inference": "labelled",
    "labelled_weak_verifier": "labelled", "moved_not_from_record": "moved",
    "kept_with_rows": "kept", "kept_record_statement": "kept",
    "kept_meta": "kept", "kept_proposal": "kept",
    "advisory_no_action": "advisory (no action)",
    "not_located_in_model_prose": "not located in the model's prose",
}


def render_accounting(acct: Mapping[str, Any]) -> str:
    """The answer's closing accounting block (code-built)."""
    c = acct.get("counts") or {}
    g = lambda *keys: sum(int(c.get(k, 0)) for k in keys)  # noqa: E731
    v = acct.get("verifier") or {}
    lines = ["---", "**Record accounting** (grounding: record only; computed by the platform after the answer was written).", ""]
    lines.append(f"- Kept with a resolving row: {g('kept_with_rows')} sentence(s).")
    lines.append(f"- Kept as statements about the record's own contents: {g('kept_record_statement')}.")
    lines.append(
        f"- Kept as the answer's own method or as proposals to watch (not claims about the world): "
        f"{g('kept_meta', 'kept_proposal')}."
    )
    lines.append(f"- Labelled as the consult's inference: {g('labelled_inference')}.")
    if g("labelled_weak_verifier"):
        lines.append(f"- Labelled because a weak verifier doubted the cited row: {g('labelled_weak_verifier')}.")
    lines.append(f"- Cut — a statement of fact with no resolving row: {g('cut_no_row', 'cut_unresolved_rows')}.")
    lines.append(f"- Cut — the verifier found the cited row does not support it: {g('cut_verifier_unsupported')}.")
    if g("cut_screen_as_evidence"):
        lines.append(f"- Cut — rested on what is on screen, not on a row: {g('cut_screen_as_evidence')}.")
    if acct.get("expectations_requested"):
        lines.append(f"- Moved under \"Not from the record\" (general expectations, as asked): {g('moved_not_from_record')}.")
    else:
        lines.append(f"- Cut — general knowledge the question did not ask for: {g('cut_general_knowledge')}.")
    lines.append(f"- Numbers no cited row states (marked [[number_unsupported]]): {acct.get('numbers_unsupported', 0)}.")
    nc = acct.get("number_check") or {}
    if nc.get("rows_clipped"):
        lines.append(
            f"  - {nc['rows_clipped']} cited row(s) longer than {int(nc.get('row_chars_cap') or 0):,} "
            f"characters were read only to that length for the number check: "
            f"{int(nc.get('chars_unchecked') or 0):,} characters of them unchecked."
        )
    if v.get("state") == "judged":
        items = v.get("items") or []
        tally: dict[str, int] = {}
        for it in items:
            word = _ACTION_WORDS.get(str(it.get("action")), str(it.get("action")))
            tally[word] = tally.get(word, 0) + 1
        judged = v.get("claims_judged")
        total = v.get("cited_sentences")
        scope = (
            f"judged {judged} of {total} cited sentences; the rest were checked only for a resolving row"
            if judged is not None and total is not None and judged < total else
            f"judged {judged} cited sentences"
        )
        weak = " — a weak judge, so its findings were applied as labels, not cuts" if v.get("weak") else ""
        lines.append(
            f"- Verifier ({v.get('model') or 'judge'}, score {v.get('score')}, {scope}{weak}): "
            f"{len(items)} listed unsupported — "
            + (", ".join(f"{n} {w}" for w, n in sorted(tally.items())) or "none")
            + "."
        )
        for it in items:
            word = _ACTION_WORDS.get(str(it.get("action")), str(it.get("action")))
            lines.append(f"  - \"{str(it.get('text') or '')[:140]}…\" ({it.get('reason')}): {word}.")
    else:
        lines.append(
            f"- The verifier did not judge this turn ({v.get('reason') or v.get('state') or 'no stamp'}); "
            "no verifier-based enforcement was applied — only the row check above ran."
        )
    return "\n".join(lines)
