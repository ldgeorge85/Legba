# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The record one prior consult turn compacts to (C-3, design note
``docs/design/C-3_CONSULT_COMPACTION.md``).

What this is and is not
=======================

This leaf is the SKELETON the C-3 wiring lane builds against: the typed
:class:`TurnRecord`, the pure projection :func:`from_turn` that fills one from a
persisted turn's payload, the deterministic :func:`render` that writes the
structured block the model sees, and :func:`is_open_resolver`, the one rule
that decides whether a dated probability may collapse to a line. Nothing here
is wired into the consult loop — the loop still carries prior turns verbatim
(the seeded transcript in ``consult_on_demand``), and this module has no
caller until the wiring lane lands.

The record's contract, from the design note:

* **keep the ids, drop the bodies.** Every substrate body the turn cited
  reduces to its citation id plus one line; the body itself stays on the
  persisted turn and the run trace, re-hydrated on demand. A claim in the
  record either points at an id or is tagged for what it is — never a claim
  that used to point at an id and no longer does.
* **provenance tags survive.** A line that names none of the turn's cited
  refs is tagged ``model knowledge`` — the same reading the provenance census
  applies to a live answer (``consult_provenance_census``: a sentence that
  names no cited ref is model knowledge), carried onto the compacted form so
  a compacted turn cannot launder an unsourced claim into a cited one.
* **never compact an open resolver.** A dated probability whose resolve-by
  date has not passed renders VERBATIM — it is a claim still under test, and
  a summary of it is a different claim. One whose date has passed renders as
  a line. A probability whose date cannot be read is treated as open: the
  conservative direction, since compacting a resolver we cannot check is the
  unsafe error.
* **the data block is authoritative over prose.** ``from_turn`` reads the
  turn's structured fields (``cited_refs``, ``watch_candidates``,
  ``unanswered_aspects``) where they exist and falls back to parsing the
  answer's own sections only where the turn records no structured field
  (``bottom line``, ``what would change my mind``, the dated probabilities —
  none is persisted as data today; the response bag carries no answer text
  projection).

Pure functions, no I/O, no imports beyond the standard library — the record
is built wherever the wiring lane puts it and never reaches the substrate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

#: The provenance tag a record line carries when it names none of the turn's
#: cited refs — spelled as the census spells it, so an operator (or the judge)
#: reads the same word on a compacted turn as on a live one.
MODEL_KNOWLEDGE_TAG = "model knowledge"

#: Rendered markers. ``[open]`` / ``[resolved YYYY-MM-DD]`` ride each dated
#: probability; ``[model knowledge]`` rides any line that names no cited id.
_OPEN_TAG = "[open]"
_RESOLVED_TAG_PREFIX = "[resolved "

#: An ISO-8601 calendar date as the consult contract writes them (watch
#: candidates are required to end ``by YYYY-MM-DD``; the same grammar is the
#: record's resolver grammar).
_ISO_DATE_RE = re.compile(r"\b(20\d\d-\d{2}-\d{2})\b")

#: A numeric probability token: ``0.7``, ``70%``, ``two in three``-style
#: ``N in M``. The system prompt's CONFIDENCE rule permits a number only
#: beside a resolution test, so a token plus a resolver marker or a date is
#: what makes a sentence a "dated probability".
_PROBABILITY_RE = re.compile(
    r"\b(?:0?\.\d+|\d{1,3}(?:\.\d+)?\s*%|\d+\s+in\s+\d+)\b"
)

#: The resolution-test marker the consult grammar writes: "resolved by the
#: 14 October vote", "resolves on the referendum", "resolution:".
_RESOLVER_RE = re.compile(
    r"\b(?:resolved?\s+(?:by|on|at|after)|resolution\s*[:\-])",
    re.IGNORECASE,
)

#: A bare uuid inside a sentence — a claim pointing at a substrate id. The
#: census's own sentence-level citation signal, reused verbatim.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

#: Markdown section headings, tolerating the numbered forms real answers use
#: (``## 8. What would change my mind — watch these``).
_HEADING_RE = re.compile(r"^#{1,6}\s*(?:\d+\s*[.:)]?\s*)?(?P<title>.+?)\s*$")
_BOTTOM_LINE_TITLE_RE = re.compile(r"^bottom line\b", re.IGNORECASE)
_CHANGE_MIND_TITLE_RE = re.compile(r"^what would change my mind\b", re.IGNORECASE)

#: Bounds, matching the consult contract's own clamps (512 on an
#: unanswered-aspect / watch line, 8 watch candidates, 20 unanswered aspects).
_MAX_LINE_CHARS = 512
_BOTTOM_LINE_CHARS = 2_000
_MAX_CHANGE_MIND_LINES = 8
_MAX_UNANSWERED = 20
#: The squashed claim head a RESOLVED probability keeps — enough to name the
#: claim, never the sentence.
_RESOLVED_CLAIM_CHARS = 120


@dataclass(frozen=True)
class RefPointer:
    """One cited substrate row, reduced to its id + kind + one line.

    The body itself is NOT on the record — it stays on the persisted turn and
    is re-hydrated on demand (the design note's "keep the ids, drop the
    bodies"). ``line`` is the row's recorded ``description`` squashed to one
    line, ``""`` when the turn recorded none; never a re-summary, because a
    line this module invented would be a claim with nothing behind it.
    """

    ref_id: str
    kind: str = ""
    line: str = ""


@dataclass(frozen=True)
class DatedProbability:
    """A dated, resolvable claim lifted VERBATIM from a turn's answer.

    ``claim`` is the sentence as the answer wrote it (the verbatim form an
    OPEN resolver keeps). ``resolve_by`` is the ISO-8601 date the claim names
    as its horizon, ``""`` when the sentence names none — an unreadable
    horizon renders the claim permanently open, the safe direction.
    ``resolver`` is the resolution test as written (the text after "resolved
    by"), ``""`` when only a bare date marks the horizon. ``model_knowledge``
    is the record's provenance tag: the sentence names none of the turn's
    cited refs.
    """

    claim: str
    resolve_by: str = ""
    resolver: str = ""
    model_knowledge: bool = False


@dataclass(frozen=True)
class Challenge:
    """An operator's pushback on the turn's answer, and whether it was taken.

    ``accepted`` is tri-state on purpose: ``True`` / ``False`` when the
    payload says, ``None`` when it does not — "we do not know" is not "the
    operator was refused". The wiring lane populates this from the session
    rows around the turn; this leaf only carries it.
    """

    text: str
    accepted: bool | None = None


@dataclass(frozen=True)
class TurnRecord:
    """The fixed record one prior consult turn compacts to.

    One record per turn PAIR (the operator's question + the run's answer) —
    the shape ``load_turn_for_recovery`` already projects and the shape the
    wiring lane builds for every non-verbatim prior turn. Every field is a
    tuple/immovable value: a record is a fact about a turn that already
    happened, and two renders of it are byte-identical.
    """

    turn_id: str = ""
    question: str = ""
    bottom_line: str = ""
    #: ``True`` when the bottom line names none of the turn's cited refs —
    #: the census's own "model knowledge" reading, kept on the record.
    bottom_line_model_knowledge: bool = False
    probabilities: tuple[DatedProbability, ...] = ()
    #: The answer's own ``## What would change my mind`` lines, verbatim.
    change_my_mind: tuple[str, ...] = ()
    #: The structured watches: ``watch_candidates`` plus any candidate the
    #: mint accepted (``watch_minted``), deduplicated, order preserved.
    watches: tuple[str, ...] = ()
    #: Every citation id the turn used, as pointers — ids preserved, bodies
    #: absent by construction.
    refs: tuple[RefPointer, ...] = ()
    unanswered_aspects: tuple[str, ...] = ()
    challenges: tuple[Challenge, ...] = ()


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _squash(text: str, limit: int = _MAX_LINE_CHARS) -> str:
    """Collapse whitespace to single spaces and cap at ``limit`` chars."""
    squashed = " ".join(str(text or "").split())
    if len(squashed) > limit:
        return squashed[: max(0, limit - 1)].rstrip() + "…"
    return squashed


def _parse_iso_date(raw: Any) -> date | None:
    """``date`` from a ``date``/``datetime``/ISO-8601 string, else ``None``."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        match = _ISO_DATE_RE.search(raw)
        if match:
            try:
                return date.fromisoformat(match.group(1))
            except ValueError:
                return None
    return None


def _names_a_cited_ref(sentence: str, cited_ids: frozenset[str]) -> bool:
    """Whether ``sentence`` carries one of the turn's own cited ids verbatim."""
    if not cited_ids:
        return False
    return any(
        m.group(0).lower() in cited_ids for m in _UUID_RE.finditer(sentence)
    )


def _sentences(text: str) -> list[str]:
    """Candidate claim sentences: the answer split on terminators + newlines.

    Bullet markers and markdown emphasis are stripped off the EDGES only —
    the sentence text itself is what renders verbatim, so interior markup
    survives untouched.
    """
    out: list[str] = []
    for piece in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        piece = piece.strip().lstrip("-*•> ").strip()
        if piece:
            out.append(piece)
    return out


def _section_lines(answer: str, title_re: re.Pattern[str]) -> list[str]:
    """The body lines of the answer section whose heading matches ``title_re``.

    The section runs from its ``##`` heading to the next heading of the same
    or shallower level; a ``---`` rule inside it is a separator, not a line.
    Empty when the answer carries no such section.
    """
    lines = (answer or "").splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and title_re.match(match.group("title")):
            start = i + 1
            break
    if start is None:
        return []
    body: list[str] = []
    for line in lines[start:]:
        match = _HEADING_RE.match(line)
        if match:
            break
        stripped = line.strip()
        if stripped and stripped not in {"---", "***", "___"}:
            body.append(stripped)
    return body


def _first_paragraph(lines: Sequence[str], limit: int) -> str:
    """The first paragraph of ``lines`` — up to the first blank-ish break.

    The section helpers above already dropped blank lines, so a paragraph is
    delimited here by a bullet/numbered item after a prose line or by the
    section end; the pragmatic reading of "the bottom line" is the section's
    first line of prose joined with its continuations, capped.
    """
    kept: list[str] = []
    for line in lines:
        if kept and re.match(r"^[-*•]\s|^\d+\s*[.:)]\s", line):
            break
        kept.append(line.lstrip("-*• ").strip())
        if kept and len(" ".join(kept)) >= limit:
            break
    return _squash(" ".join(kept), limit)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _bottom_line(answer: str) -> str:
    """The answer's ``## Bottom line``, else its first paragraph.

    The consult contract puts the judgment up top; a pre-contract answer with
    no section contributes its opening paragraph instead — the same claim a
    reader would call the bottom line, at the same cap.
    """
    section = _section_lines(answer, _BOTTOM_LINE_TITLE_RE)
    if section:
        return _first_paragraph(section, _BOTTOM_LINE_CHARS)
    for block in re.split(r"\n\s*\n", answer or ""):
        if block.strip():
            return _squash(block, _BOTTOM_LINE_CHARS)
    return ""


def _change_my_mind(answer: str) -> tuple[str, ...]:
    """The ``## What would change my mind`` lines, verbatim, capped at 8."""
    out: list[str] = []
    for line in _section_lines(answer, _CHANGE_MIND_TITLE_RE):
        text = line.lstrip("-*• ").strip()
        text = re.sub(r"^\d+\s*[.:)]\s*", "", text)
        if text:
            out.append(_squash(text))
        if len(out) >= _MAX_CHANGE_MIND_LINES:
            break
    return tuple(out)


def _probabilities(
    answer: str, cited_ids: frozenset[str]
) -> tuple[DatedProbability, ...]:
    """Every dated probability in the answer, verbatim, with its resolver.

    A sentence qualifies when it carries a numeric probability token AND a
    resolver marker or an ISO-8601 date — the CONFIDENCE rule's own shape ("a
    NUMBER only beside a resolution test"). A bare number beside nothing that
    resolves it is not recorded here: it was never a claim under test.
    """
    out: list[DatedProbability] = []
    for sentence in _sentences(answer):
        if not _PROBABILITY_RE.search(sentence):
            continue
        resolver_match = _RESOLVER_RE.search(sentence)
        date_match = _ISO_DATE_RE.search(sentence)
        if resolver_match is None and date_match is None:
            continue
        resolve_by = date_match.group(1) if date_match else ""
        resolver = ""
        if resolver_match is not None:
            resolver = _squash(sentence[resolver_match.end():], 240)
        out.append(
            DatedProbability(
                claim=sentence,
                resolve_by=resolve_by,
                resolver=resolver,
                model_knowledge=not _names_a_cited_ref(sentence, cited_ids),
            )
        )
    return tuple(out)


def _ref_pointers(cited_refs: Any) -> tuple[RefPointer, ...]:
    """The turn's cited refs as pointers — ids + kinds + one line, deduplicated.

    Accepts both persisted shapes: ``{"id", "kind", "description"}`` dicts
    (the ``consult_turns.cited_refs`` jsonb projection) and bare id strings.
    """
    out: list[RefPointer] = []
    seen: set[str] = set()
    for entry in cited_refs or ():
        ref_id = kind = line = ""
        if isinstance(entry, Mapping):
            ref_id = str(entry.get("id") or "").strip()
            kind = str(entry.get("kind") or "").strip()
            line = _squash(entry.get("description") or "", 240)
        elif entry is not None:
            ref_id = str(entry).strip()
        if ref_id and ref_id.lower() not in seen:
            seen.add(ref_id.lower())
            out.append(RefPointer(ref_id=ref_id, kind=kind, line=line))
    return tuple(out)


def _watches(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The turn's standing watches: ``watch_candidates`` then minted ones.

    ``watch_minted`` entries carry the candidate text under ``candidate`` —
    the indicator the mint scored, folded or refused, is still the watch the
    turn declared, so it rides the record beside the un-minted candidates.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        text = _squash(raw)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)

    for item in payload.get("watch_candidates") or ():
        _add(item)
    for entry in payload.get("watch_minted") or ():
        if isinstance(entry, Mapping):
            _add(entry.get("candidate"))
    return tuple(out)


def _challenges(payload: Mapping[str, Any]) -> tuple[Challenge, ...]:
    """The operator's challenges on this turn, as the payload records them.

    ``{"text", "accepted"}`` dicts or bare strings (``accepted=None``) —
    tri-state is preserved, never defaulted.
    """
    out: list[Challenge] = []
    for entry in payload.get("challenges") or ():
        if isinstance(entry, Mapping):
            text = _squash(entry.get("text") or entry.get("question") or "")
            accepted = entry.get("accepted")
            out.append(
                Challenge(
                    text=text,
                    accepted=accepted if isinstance(accepted, bool) else None,
                )
            )
        elif entry is not None:
            out.append(Challenge(text=_squash(entry)))
    return tuple(c for c in out if c.text)


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------


def is_open_resolver(prob: Any, now: Any) -> bool:
    """Whether a dated probability's resolver has NOT yet been reached.

    Open means: stay verbatim. A probability is open when its ``resolve_by``
    date is today or later — the date "has not passed", the resolution test
    may still be running. It is ALSO open when the horizon cannot be read
    (no parseable ``resolve_by``, or an unparseable ``now``): the rule is
    "never compact an open resolver", and a resolver whose date cannot be
    checked cannot be proven closed, so the safe direction is verbatim.

    ``prob`` may be a :class:`DatedProbability` or any mapping carrying a
    ``resolve_by`` key; ``now`` a ``date``, ``datetime`` or ISO-8601 string.
    """
    raw = (
        prob.get("resolve_by")
        if isinstance(prob, Mapping)
        else getattr(prob, "resolve_by", None)
    )
    resolve_by = _parse_iso_date(raw)
    today = _parse_iso_date(now)
    if resolve_by is None or today is None:
        return True
    return resolve_by >= today


def from_turn(turn_payload: Mapping[str, Any]) -> TurnRecord:
    """Project a persisted consult turn pair into its :class:`TurnRecord`.

    ``turn_payload`` is the question+answer dict the wiring lane assembles —
    the same shape ``consult_persistence.load_turn_for_recovery`` already
    returns (``question``, ``answer``, ``cited_refs``, …) plus the optional
    structured fields the response bag carries (``watch_candidates``,
    ``watch_minted``, ``unanswered_aspects``, ``challenges``) and a
    ``turn_id``. ``content`` is accepted as the answer's key too, so a raw
    session-row projection works unchanged.

    The structured fields win over prose wherever both exist: the data block
    is authoritative over prose, and this function parses the answer text
    only for what no field records — the bottom line, the change-my-mind
    lines, and the dated probabilities.
    """
    payload = turn_payload or {}
    answer = str(payload.get("answer") or payload.get("content") or "")
    refs = _ref_pointers(payload.get("cited_refs"))
    cited_ids = frozenset(r.ref_id.lower() for r in refs)
    bottom_line = _bottom_line(answer)
    return TurnRecord(
        turn_id=str(payload.get("turn_id") or payload.get("id") or ""),
        question=_squash(payload.get("question") or "", _BOTTOM_LINE_CHARS),
        bottom_line=bottom_line,
        bottom_line_model_knowledge=not _names_a_cited_ref(
            bottom_line, cited_ids
        ),
        probabilities=_probabilities(answer, cited_ids),
        change_my_mind=_change_my_mind(answer),
        watches=_watches(payload),
        refs=refs,
        unanswered_aspects=tuple(
            _squash(u) for u in (payload.get("unanswered_aspects") or ())[:_MAX_UNANSWERED]
            if _squash(u)
        ),
        challenges=_challenges(payload),
    )


def render(record: TurnRecord, *, now: Any = None) -> str:
    """The structured block the model sees for a compacted turn.

    Deterministic: same record + same ``now`` renders byte-identical — the
    compaction the operator reads and the judge scores must be a function of
    the turn, not of the moment it was rendered (``now`` is therefore an
    explicit input; ``None`` means today, for callers that want the wall
    clock).

    Open resolvers render VERBATIM (the claim is still under test); resolved
    ones render as a line. Every line that names none of the turn's cited ids
    carries the ``model knowledge`` tag the census would give it.
    """
    today = _parse_iso_date(now) or date.today()
    cited_ids = frozenset(r.ref_id.lower() for r in record.refs)
    lines: list[str] = [
        "## Prior consult turn — compacted record",
        (
            "(ids are authoritative; the prose below is a summary of a prior "
            "turn — the full turn is on the audit trail)"
        ),
    ]
    if record.turn_id:
        lines.append(f"turn: {record.turn_id}")
    if record.question:
        lines.append(f"question: {record.question}")
    if record.bottom_line:
        tag = f"  [{MODEL_KNOWLEDGE_TAG}]" if record.bottom_line_model_knowledge else ""
        lines.append(f"bottom line: {record.bottom_line}{tag}")
    if record.probabilities:
        lines.append("dated probabilities:")
        for prob in record.probabilities:
            tag = f" [{MODEL_KNOWLEDGE_TAG}]" if prob.model_knowledge else ""
            if is_open_resolver(prob, today):
                horizon = f" — resolves {prob.resolve_by}" if prob.resolve_by else ""
                lines.append(f"- {prob.claim}{horizon} {_OPEN_TAG}{tag}")
            else:
                lines.append(
                    f"- {_squash(prob.claim, _RESOLVED_CLAIM_CHARS)} "
                    f"{_RESOLVED_TAG_PREFIX}{prob.resolve_by}]{tag}"
                )
    if record.change_my_mind:
        lines.append("what would change my mind:")
        for line in record.change_my_mind:
            tag = (
                f" [{MODEL_KNOWLEDGE_TAG}]"
                if not _names_a_cited_ref(line, cited_ids)
                else ""
            )
            lines.append(f"- {line}{tag}")
    if record.watches:
        lines.append("standing watches:")
        for watch in record.watches:
            lines.append(f"- {watch}")
    if record.refs:
        lines.append(
            "cited substrate ids (bodies stay on the turn; "
            "re-hydrated on demand):"
        )
        for ptr in record.refs:
            kind = f" [{ptr.kind}]" if ptr.kind else ""
            line = f" — {ptr.line}" if ptr.line else ""
            lines.append(f"- {ptr.ref_id}{kind}{line}")
    if record.unanswered_aspects:
        lines.append("unanswered aspects:")
        for aspect in record.unanswered_aspects:
            lines.append(f"- {aspect}")
    if record.challenges:
        lines.append("operator challenges:")
        for challenge in record.challenges:
            verdict = (
                "accepted"
                if challenge.accepted is True
                else "not accepted"
                if challenge.accepted is False
                else "undetermined"
            )
            lines.append(f"- {challenge.text} [{verdict}]")
    return "\n".join(lines)


__all__ = [
    "Challenge",
    "DatedProbability",
    "MODEL_KNOWLEDGE_TAG",
    "RefPointer",
    "TurnRecord",
    "from_turn",
    "is_open_resolver",
    "render",
]
