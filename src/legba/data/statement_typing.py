# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TYPING seat — one cited sentence in, one typed statement draft out
(plan P1 §4 lane S3; design B1 §5 "what needs a model").

WHAT THE MODEL DOES. Reads one cited sentence of a desk finding, the subjects
code offers, the cited rows' stored texts (a window code chose) and a short
list of same-subject standing statements code found, and proposes:

* ``kind`` (event / state / level / absence / attributed / assessment);
* which OFFERED subject the sentence is about (by number — never a free key);
* ``valid_from`` and its precision (``day`` / ``month`` / ``unknown``);
* ``slots`` (predicate, value, unit, counterpart — where the kind needs them);
* for each cited row it relies on, an EXACT quote copied from that row's text;
* whether the sentence restates, revises, or is new against the candidates.

WHAT CODE DOES, and why the model cannot get past it.

* The statement TEXT is the sentence as the desk wrote it, markers removed —
  the model never rewrites it, so it cannot add a number, a date or a name
  (the hindsight mitigation in plan §5.6: fields come from the sentence and its
  quote only).
* The subject is one of the offered candidates or the draft is refused
  (``subject_not_offered``). A support row names one of the sentence's OWN
  cited rows or it is dropped and the refusal counted (``row_not_on_sentence``,
  gate G3's rule at the seat). Every other field is parsed against a closed
  vocabulary; a value outside it is a named refusal, never a guess.
* The assembled draft then goes through every code gate
  (:func:`statement_gate.evaluate_code_gates`: quote in the cited text, numbers
  and dates in the quotes, subject named, valid-from not after publication,
  claim-class rule, handed rows, roles, control text, access ceiling,
  independence). The result is a draft and a report — never a status.

The standing candidates come from :func:`same_subject_candidates` — code, not
the model, chooses the short list (plan S3).

S-5 ADDITIONS.

* Subjects may be ACTORS (``kind="entity"``, keyed by profile id) that code
  found linked to the cited rows (:mod:`statement_actor_subjects`); the seat
  picks from what is offered or declines.
* A RELATION between two actors is typed as subject + ``counterpart`` slot.
  A counterpart given as an offered id (``S3``) is resolved to that subject
  and G2 checks its names in the quotes like the subject's
  (``G2.counterpart_named``); a counterpart given as text must be words of
  the sentence itself (else ``counterpart_not_in_sentence``) and is checked
  the same way. A counterpart equal to the subject is refused
  (``counterpart_is_subject``).
* An ASSESSMENT never takes a quote: quotes the seat gave it are dropped and
  counted; it rests only on premises (other typed statements), gated by
  :func:`statement_gate.evaluate_assessment_gates`. An ABSENCE takes no quote
  either: it is scoped to the run's handed slice and gated by
  :func:`statement_gate.evaluate_absence_gates`. Neither is ever sent to the
  verifier or "established" by a quote. Either may default its subject to the
  desk's own (S1) when the seat names none — recorded.
* Each support row records which text of its signal the quote was found in
  (``quote_found_in``); a quote found only in the feed item is marked not
  checkable. Each row also records ``quote_context``: the quote extended, in
  code and verbatim, to the whole sentence of the stored text it sits in —
  for the record to show; the gates check the quote itself, and the verifier
  is shown the quote, not this (measured: ``statement_verifier``).

No I/O beyond the injected ``llm``; no table is read or written.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .provenance.text_fold import fold_contains, normalize_for_match
from .statement_content import find_dates
from .statement_coverage import evaluate_coverage_gates
from .statement_dates import find_as_of_frames
from .statement_gate import (
    evaluate_absence_gates,
    evaluate_assessment_gates,
    evaluate_code_gates,
    locate_quote,
    resolve_text,
)
from .statement_seats import (
    TYPING_SEAT_ID,
    CallBudget,
    SeatCall,
    SeatLLM,
    as_text,
    call_seat,
    first_of,
    parse_reply_object,
)
from .statement_sentences import CitedSentence
from .statement_shapes import (
    SLOT_NAMES,
    EvidenceTexts,
    GateReport,
    HandedSlice,
    ObservationText,
    SourceClass,
    StatementDraft,
    QuoteFoundIn,
    StatementKind,
    SubjectKind,
    SupportKind,
    SupportRole,
    SupportRow,
    ValidFromPrecision,
    values_of,
)

#: Characters of a cited row's stored text the seat is shown (a window code
#: picks around the sentence's words). Quotes are still checked against the
#: WHOLE stored text by G1.
WINDOW_CHARS = 2400
#: Cited rows per sentence the seat is shown (the first N evidence refs).
MAX_ROWS = 3
#: Standing candidates offered per sentence (plan: "a short list").
MAX_CANDIDATES = 5

KINDS = values_of(list(StatementKind))
PRECISIONS = values_of(list(ValidFromPrecision))
RELATIONS = frozenset({"new", "restates", "revises"})

#: The seat's closed refusal reasons (each a counted, named outcome).
REFUSALS = (
    "no_call", "reply_unparseable", "kind_unknown", "subject_not_offered",
    "valid_from_unparseable", "row_not_on_sentence", "no_support",
    "relation_unknown", "candidate_not_offered", "slot_not_scalar",
    "counterpart_not_offered", "counterpart_not_in_sentence", "counterpart_is_subject",
)

#: Kinds that are not checkable reports of the world: never quoted, never
#: sent to the verifier, never established by a quote (S-5).
NON_EVIDENCE_KINDS = frozenset({StatementKind.ASSESSMENT.value, StatementKind.ABSENCE.value,
                                StatementKind.COVERAGE.value})

#: The longest context a quote is extended to for the verifier.
CONTEXT_MAX_CHARS = 700


@dataclass(frozen=True)
class SubjectCandidate:
    """A subject code offers the seat: its kind, its key as the existing tables
    spell it, and the names G2 accepts for it — plain strings, or the
    resolver's :class:`~legba.data.statement_subject_names.SubjectNames`
    (usable names with provenance, and the declared homonym exclusions)."""

    kind: str
    key: str
    names: Any
    #: S-5 — an ACTOR subject (``kind == "entity"``): its profile id,
    #: canonical name and entity class, as the entity layer records them.
    entity_id: str | None = None
    canonical_name: str | None = None
    entity_type: str | None = None

    def display(self, limit: int = 6) -> str:
        shown = [str(getattr(n, "name", n)) for n in self.names][:limit]
        return " / ".join(shown) or self.canonical_name or self.key

    def label(self) -> str:
        """``kind`` as the seat is shown it (``entity:person``)."""
        return f"{self.kind}:{self.entity_type}" if self.kind == SubjectKind.ENTITY.value and self.entity_type \
            else self.kind


@dataclass(frozen=True)
class EvidenceRef:
    """One cited row, resolved by the caller (the store or a read-only script).

    ``text`` is the stored text G1 checks quotes against: the cited fetch
    version's extracted text when ``fetch_version_id`` is set, else the
    signal's stored text (or an observation's rendered line)."""

    ordinal: int
    ref_kind: str
    ref_id: str
    text: str
    fetch_version_id: int | None = None
    published_at: datetime | None = None
    title: str | None = None
    source_id: str | None = None
    origin_group_id: str | None = None
    checkable: bool | None = None
    access_class: str | None = None
    source_class: SourceClass | str | None = None
    observation_values: tuple[float, ...] = ()
    #: S-5 — the signal's FEED ITEM texts (stored title, summary), each on its
    #: own; G1 looks here when a quote is not in ``text`` and records which.
    feed_text: tuple[str, ...] = ()
    #: S-5 — the row's publisher as the citation names it (the outlet): an
    #: actor of that name is the publisher, not the subject.
    source_name: str | None = None
    #: B-1: ``text`` is the ITEM as the response carried it (a slice of the
    #: fetch version — ``legba.data.byte_path.signal_item_text``), not the
    #: version's whole extracted text.
    item_text: bool = False


@dataclass(frozen=True)
class StandingCandidate:
    """A statement already in the record about the same subject."""

    statement_id: str
    kind: str
    text: str
    subject_kind: str
    subject_key: str
    valid_from: datetime | None = None


@dataclass(frozen=True)
class TypingInput:
    sentence: CitedSentence
    evidence: tuple[EvidenceRef, ...]
    subjects: tuple[SubjectCandidate, ...]
    standing: tuple[StandingCandidate, ...] = ()
    #: When the desk wrote the sentence (the finding's production time): the
    #: date rule resolves the sentence's own relative words and its as-of
    #: frame against it (``statement_dates``).
    read_at: datetime | None = None
    #: S-5 — the entities the entity layer links to the cited rows
    #: (``statement_actor_subjects.LinkedEntity``); piece typing offers the
    #: ones each piece names as actor subjects.
    linked_entities: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TypingResult:
    """What the seat returns for one sentence. ``passed`` is the code gates'
    answer plus the seat's own refusals — it is not establishment (the
    verifier and the lifecycle decide that, elsewhere)."""

    draft: StatementDraft | None
    support: tuple[SupportRow, ...]
    refusals: tuple[str, ...]
    gate: GateReport | None
    relation: str | None = None
    relation_candidate: str | None = None
    call: SeatCall | None = None
    seat: str = TYPING_SEAT_ID
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.draft is not None and not self.refusals and bool(self.gate and self.gate.passed)


# ---------------------------------------------------------------------------
# Code chooses: candidates and windows
# ---------------------------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w for w in normalize_for_match(text).split() if len(w) > 2}


def same_subject_candidates(
    subject_kind: str,
    subject_key: str,
    sentence: str,
    standing: Iterable[StandingCandidate],
    *,
    limit: int = MAX_CANDIDATES,
) -> tuple[StandingCandidate, ...]:
    """The short list the seat compares against: standing statements on the
    SAME subject, ranked by word overlap with the sentence (ties by id), at most
    ``limit``. Code-chosen; the model never searches the record."""
    words = _words(sentence)
    same = [s for s in standing if s.subject_kind == subject_kind and s.subject_key == subject_key]

    def score(s: StandingCandidate) -> tuple[float, str]:
        other = _words(s.text)
        union = words | other
        return (-(len(words & other) / len(union)) if union else 0.0, s.statement_id)

    return tuple(sorted(same, key=score)[:limit])


def evidence_window(text: str, sentence: str, *, chars: int = WINDOW_CHARS) -> str:
    """The ``chars``-long stretch of ``text`` sharing the most words with the
    sentence (paragraph-aligned where possible); the whole text when short."""
    if len(text) <= chars:
        return text
    words = _words(sentence)
    step = max(200, chars // 4)
    best, best_at = -1, 0
    for start in range(0, max(1, len(text) - chars + step), step):
        hits = len(words & _words(text[start:start + chars]))
        if hits > best:
            best, best_at = hits, start
    return text[best_at:best_at + chars]


# ---------------------------------------------------------------------------
# The prompt (user message; the system prompt is the descriptor's)
# ---------------------------------------------------------------------------


def _day(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.date().isoformat()


def build_typing_prompt(item: TypingInput) -> str:
    """The one user message: the sentence, the offered subjects, the cited
    rows' text windows, and the code-chosen candidates."""
    lines = ["SENTENCE:", item.sentence.plain, "", "SUBJECTS (answer with one id, or null):"]
    for i, s in enumerate(item.subjects, start=1):
        lines.append(f"  S{i} · {s.label()} · {s.display()}")
    lines += ["", "CITED ROWS (quote ONLY from these texts, copying the words exactly):"]
    for ev in item.evidence[:MAX_ROWS]:
        lines.append(f"  ROW {ev.ordinal} · {ev.ref_kind} · published {_day(ev.published_at)}"
                     + (f" · title: {ev.title}" if ev.title else ""))
        lines.append("  TEXT: " + evidence_window(ev.text, item.sentence.plain).replace("\n", " "))
    lines += ["", "STANDING STATEMENTS ON THESE SUBJECTS (may be empty):"]
    if not item.standing:
        lines.append("  (none)")
    for i, c in enumerate(item.standing, start=1):
        lines.append(f"  C{i} · {c.kind} · {c.text} · valid from {_day(c.valid_from)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Code decides: the reply → a draft
# ---------------------------------------------------------------------------

_YMD_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")


def parse_valid_from(raw: Any, precision_raw: Any) -> tuple[datetime | None, str, bool]:
    """``(valid_from, precision, ok)``. ``null`` / ``unknown`` → unknown, ok.
    A value that is neither ``YYYY-MM-DD`` nor ``YYYY-MM`` → unknown, NOT ok."""
    text = as_text(raw)
    precision = as_text(precision_raw).lower()
    if not text or text.lower() in {"null", "none", "unknown"} or precision == "unknown":
        return None, ValidFromPrecision.UNKNOWN.value, True
    try:
        m = _YMD_RE.match(text)
        if m:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return d, ValidFromPrecision.DAY.value, True
        m = _YM_RE.match(text)
        if m:
            d = datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=timezone.utc)
            return d, ValidFromPrecision.MONTH.value, True
    except ValueError:
        pass
    return None, ValidFromPrecision.UNKNOWN.value, False


def _offered_id(raw: Any, prefix: str, n: int) -> int | None:
    m = re.fullmatch(rf"(?:{re.escape(prefix)})?(\d+)", as_text(raw), flags=re.IGNORECASE)
    if not m:
        return None
    i = int(m.group(1))
    return i if 1 <= i <= n else None


def _slots(raw: Any, refusals: list[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if v is None or v == "":
            continue
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out[str(k)] = as_text(v) if isinstance(v, str) else v
            if str(k) == "value" and isinstance(v, str):
                # A numeric value given as a string is read as the number, so
                # G2 checks it against the evidence like any stated figure.
                try:
                    out["value"] = float(as_text(v).replace(",", ""))
                except ValueError:
                    pass
        elif "slot_not_scalar" not in refusals:
            refusals.append("slot_not_scalar")
    return out


def _support_row(ev: EvidenceRef, quote: str) -> SupportRow:
    kind = (SupportKind.OBSERVATION.value if ev.ref_kind == "observation"
            else SupportKind.SIGNAL.value)
    return SupportRow(
        role=SupportRole.EVIDENCE.value, support_kind=kind, ref_id=ev.ref_id, quote=quote,
        fetch_version_id=ev.fetch_version_id, source_id=ev.source_id,
        origin_group_id=ev.origin_group_id, checkable=ev.checkable,
        access_class=ev.access_class, source_class=ev.source_class,
        published_at=ev.published_at, title=ev.title,
    )


def evidence_texts(evidence: Iterable[EvidenceRef]) -> EvidenceTexts:
    """The :class:`EvidenceTexts` the gates read, from the resolved rows."""
    by_fv: dict[int, str] = {}
    by_item: dict[tuple[int, str], str] = {}
    by_signal: dict[str, str] = {}
    by_obs: dict[str, ObservationText] = {}
    feed: dict[str, tuple[str, ...]] = {}
    for ev in evidence:
        if ev.ref_kind == "observation":
            by_obs[ev.ref_id] = ObservationText(ev.text, tuple(ev.observation_values))
            continue
        if ev.fetch_version_id is not None and ev.item_text:
            by_item[(ev.fetch_version_id, ev.ref_id)] = ev.text
        elif ev.fetch_version_id is not None:
            by_fv[ev.fetch_version_id] = ev.text
        elif ev.text:
            by_signal[ev.ref_id] = ev.text
        parts = tuple(t for t in ev.feed_text if t and t.strip())
        if parts:
            feed[ev.ref_id] = parts
    return EvidenceTexts(by_fetch_version=by_fv, by_fetch_item=by_item, by_signal=by_signal,
                         by_observation=by_obs,
                         feed_item_by_signal=feed)


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])[\"'’”)\]]*\s+|\n+")


def _raw_span(text: str, quote: str) -> tuple[int, int] | None:
    """Where ``quote`` sits in ``text``: exact, else case-insensitive with
    any whitespace run matching any whitespace run. ``None`` when the fold
    G1 uses found it but this simple match cannot place it."""
    at = text.find(quote)
    if at >= 0:
        return at, at + len(quote)
    words = quote.split()
    if not words:
        return None
    m = re.search(r"\s+".join(re.escape(w) for w in words), text, flags=re.IGNORECASE)
    return m.span() if m else None


def quote_in_context(text: str | None, quote: str | None, *, max_chars: int = CONTEXT_MAX_CHARS) -> str | None:
    """``quote`` extended to the whole sentence(s) of ``text`` it sits in —
    verbatim, by code. ``None`` when the quote cannot be placed or the
    sentence is longer than ``max_chars`` (then the quote stands alone)."""
    if not text or not quote or not quote.strip():
        return None
    span = _raw_span(text, quote.strip())
    if span is None:
        return None
    a, b = span
    starts = [m.end() for m in _SENTENCE_END_RE.finditer(text, 0, a)]
    start = starts[-1] if starts else 0
    m = _SENTENCE_END_RE.search(text, b)
    end = m.start() if m else len(text)
    ctx = text[start:end].strip()
    if not ctx or len(ctx) > max_chars:
        return None
    return ctx if len(ctx) > len(quote.strip()) else None


def stamp_support(rows: Sequence[SupportRow], texts: EvidenceTexts) -> tuple[SupportRow, ...]:
    """Each row with ``quote_found_in`` (which text of the signal holds the
    quote), ``checkable=False`` when that is the feed item only, and
    ``quote_context`` (the quote's whole sentence in the text it was found
    in). Pure; the quote itself is never changed."""
    out: list[SupportRow] = []
    for r in rows:
        found = locate_quote(r, texts)
        changes: dict[str, Any] = {}
        if found is not None and (texts.feed_item_by_signal or {}).get(str(r.ref_id)):
            changes["quote_found_in"] = found
        if found == QuoteFoundIn.FEED_ITEM.value:
            changes["checkable"] = False
            source = next((t for t in texts.feed_item_by_signal.get(str(r.ref_id), ())
                           if fold_contains(t, r.quote or "")), None)
        else:
            source, _ = resolve_text(r, texts)
        ctx = quote_in_context(source, r.quote)
        if ctx:
            changes["quote_context"] = ctx
        out.append(replace(r, **changes) if changes else r)
    return tuple(out)


def strip_as_of(
    plain: str, valid_from: datetime | None, precision: str, read_at: datetime | None,
) -> tuple[datetime | None, str, datetime | None, bool]:
    """``statement_dates`` rule (b) at the seat: an as-of frame naming the
    read's own day is when the record LEARNED the statement. Returns
    ``(valid_from, precision, known_at, stripped)``: a valid-from the seat set
    to that day is cleared (precision ``unknown``) unless the sentence states
    the same day outside the frame."""
    if read_at is None:
        return valid_from, precision, None, False
    read_day = (read_at.astimezone(timezone.utc) if read_at.tzinfo else read_at).date()
    frames = [d for d in find_as_of_frames(plain)
              if d.month == read_day.month and d.day in (None, read_day.day)
              and d.year in (None, read_day.year)]
    if not frames:
        return valid_from, precision, None, False
    known_at = datetime(read_day.year, read_day.month, read_day.day, tzinfo=timezone.utc)
    if valid_from is None:
        return valid_from, precision, known_at, False
    vf_day = (valid_from.astimezone(timezone.utc) if valid_from.tzinfo else valid_from).date()
    same = vf_day == read_day or (precision == ValidFromPrecision.MONTH.value
                                  and (vf_day.year, vf_day.month) == (read_day.year, read_day.month))
    framed = {d.span for d in frames}
    elsewhere = any(d.span not in framed and d.month == vf_day.month and d.day in (None, vf_day.day)
                    for d in find_dates(plain))
    if same and not elsewhere:
        return None, ValidFromPrecision.UNKNOWN.value, known_at, True
    return valid_from, precision, known_at, False


def _counterpart(
    raw: Any, item: TypingInput, subject: SubjectCandidate | None, allowed: frozenset[int] | None,
    refusals: list[str],
) -> tuple[str | None, Any, dict[str, Any] | None]:
    """``(slot_text, names, detail)`` for a counterpart slot value."""
    text = as_text(raw)
    if not text:
        return None, None, None
    if re.fullmatch(r"[Ss]\d+", text):
        ci = _offered_id(text, "S", len(item.subjects))
        if ci is None or (allowed is not None and ci not in allowed):
            refusals.append("counterpart_not_offered")
            return None, None, None
        cand = item.subjects[ci - 1]
        if subject is not None and (cand.kind, cand.key) == (subject.kind, subject.key):
            refusals.append("counterpart_is_subject")
            return None, None, None
        name = cand.canonical_name or (cand.display(1) if cand.names else cand.key)
        return name, cand.names, {"kind": cand.kind, "key": cand.key, "name": name, "by": "offered"}
    if not normalize_for_match(text) or normalize_for_match(text) not in normalize_for_match(item.sentence.plain):
        refusals.append("counterpart_not_in_sentence")
        return None, None, None
    if subject is not None and normalize_for_match(text) in {
            normalize_for_match(str(getattr(n, "name", n))) for n in subject.names}:
        refusals.append("counterpart_is_subject")
        return None, None, None
    return text, [text], {"kind": None, "key": None, "name": text, "by": "sentence_words"}


def assemble(
    item: TypingInput,
    reply: Mapping[str, Any] | None,
    *,
    handed: HandedSlice,
    allow_publication_date: bool = True,
    allowed_subjects: frozenset[int] | None = None,
    premises: Sequence[SupportRow] = (),
) -> TypingResult:
    """Turn a parsed reply into a draft + support rows, then run the code
    gates. Pure; every defect is a named refusal.

    ``allowed_subjects`` (1-based ids into ``item.subjects``) narrows which
    offered subjects THIS piece may take (S-5: actors are offered per
    piece). ``premises`` are the premise rows an assessment rests on (other
    typed pieces), built by the caller."""
    if reply is None:
        return TypingResult(None, (), ("reply_unparseable",), None)
    refusals: list[str] = []
    detail: dict[str, Any] = {}
    kind = first_of([as_text(reply.get("kind"))], KINDS)
    if kind is None:
        refusals.append("kind_unknown")
    si = _offered_id(reply.get("subject"), "S", len(item.subjects))
    if si is not None and allowed_subjects is not None and si not in allowed_subjects:
        si = None
    if si is None and kind in NON_EVIDENCE_KINDS and item.subjects and as_text(reply.get("subject")).lower() in {
            "", "null", "none"}:
        si = 1   # the desk's own subject: a judgement / an absence in its collection
        detail["subject_defaulted"] = "desk"
    subject = item.subjects[si - 1] if si else None
    if subject is None:
        refusals.append("subject_not_offered")
    valid_from, precision, vf_ok = parse_valid_from(
        reply.get("valid_from"), reply.get("valid_from_precision"))
    if not vf_ok:
        refusals.append("valid_from_unparseable")
    slots = _slots(reply.get("slots"), refusals)
    counterpart_names = None
    if "counterpart" in slots:
        name, counterpart_names, cdetail = _counterpart(slots["counterpart"], item, subject,
                                                         allowed_subjects, refusals)
        if name is None:
            slots.pop("counterpart")
        else:
            slots["counterpart"] = name
            detail["counterpart"] = cdetail

    by_ordinal = {ev.ordinal: ev for ev in item.evidence}
    support: list[SupportRow] = []
    raw_support = reply.get("support")
    for entry in raw_support if isinstance(raw_support, list) else []:
        if not isinstance(entry, Mapping):
            continue
        n = _offered_id(entry.get("row"), "", 10**6)
        ev = by_ordinal.get(n) if n is not None else None
        if ev is None:
            if "row_not_on_sentence" not in refusals:
                refusals.append("row_not_on_sentence")
            continue
        support.append(_support_row(ev, as_text(entry.get("quote"))))
    if kind in NON_EVIDENCE_KINDS:
        # an assessment / absence never rests on a quote: dropped, counted
        if support:
            detail["quotes_dropped"] = len(support)
        support = list(premises) if kind == StatementKind.ASSESSMENT.value else []
        refusals = [r for r in refusals if r != "row_not_on_sentence"]
    elif not support:
        refusals.append("no_support")

    relation_raw = reply.get("relation") if isinstance(reply.get("relation"), Mapping) else {}
    relation = first_of([as_text(relation_raw.get("action"))], RELATIONS)
    if relation is None:
        refusals.append("relation_unknown")
    cand_id = None
    if relation in {"restates", "revises"}:
        ci = _offered_id(relation_raw.get("candidate"), "C", len(item.standing))
        if ci is None:
            refusals.append("candidate_not_offered")
        else:
            cand_id = item.standing[ci - 1].statement_id

    if kind is None or subject is None:
        return TypingResult(None, tuple(support), tuple(refusals), None, relation, cand_id, detail=detail)
    valid_from, precision, known_at, stripped = strip_as_of(
        item.sentence.plain, valid_from, precision, item.read_at)
    draft = StatementDraft(
        text=item.sentence.plain, kind=kind, subject_kind=subject.kind,
        subject_key=subject.key, valid_from=valid_from,
        valid_from_precision=precision, slots={k: v for k, v in slots.items()} or None,
        known_at=known_at,
    )
    if kind == StatementKind.ASSESSMENT.value:
        gate = evaluate_assessment_gates(draft, support)
        detail["premises"] = len(support)
    elif kind == StatementKind.ABSENCE.value:
        gate = evaluate_absence_gates(draft, handed=handed, cited=[ev.ref_id for ev in item.evidence])
    elif kind == StatementKind.COVERAGE.value:
        # S-6: counted against the rows the piece cites (code, never a quote)
        counted = [_support_row(ev, "") for ev in item.evidence]
        gate = evaluate_coverage_gates(draft, handed=handed, rows=counted,
                                       texts=evidence_texts(item.evidence))
        detail["counted_rows"] = len({ev.ref_id for ev in item.evidence})
    else:
        texts = evidence_texts(item.evidence)
        support = list(stamp_support(support, texts))
        gate = evaluate_code_gates(
            draft, support, texts=texts, subject_names=subject.names,
            handed=handed, allow_publication_date=allow_publication_date, read_at=item.read_at,
            counterpart_names=counterpart_names,
        )
    if subject.kind == SubjectKind.ENTITY.value:
        detail["subject"] = {"kind": "entity", "entity_id": subject.entity_id,
                             "canonical_name": subject.canonical_name, "entity_type": subject.entity_type}
    unknown_slots = sorted(set(slots) - SLOT_NAMES)
    if unknown_slots:
        detail["unknown_slots"] = unknown_slots
    if stripped:
        detail["valid_from_stripped"] = "as_of_frame"
    return TypingResult(draft, tuple(support), tuple(refusals), gate, relation, cand_id,
                        detail=detail)


async def type_sentence(
    llm: SeatLLM,
    item: TypingInput,
    *,
    system_prompt: str,
    handed: HandedSlice,
    budget: CallBudget | None = None,
    allow_publication_date: bool = True,
) -> TypingResult:
    """One sentence through the seat: one model call, then code."""
    content, call = await call_seat(
        llm, seat=TYPING_SEAT_ID, system_prompt=system_prompt,
        user_content=build_typing_prompt(item), budget=budget,
    )
    if content is None:
        return TypingResult(None, (), ("no_call",), None, call=call)
    result = assemble(item, parse_reply_object(content), handed=handed,
                      allow_publication_date=allow_publication_date)
    return TypingResult(result.draft, result.support, result.refusals, result.gate,
                        result.relation, result.relation_candidate, call, detail=result.detail)


def tally_refusals(results: Sequence[TypingResult]) -> dict[str, int]:
    """Counts of each seat refusal over many results (counts only)."""
    out: dict[str, int] = {}
    for r in results:
        for reason in r.refusals:
            out[reason] = out.get(reason, 0) + 1
    return out


__all__ = [
    "CONTEXT_MAX_CHARS",
    "MAX_CANDIDATES",
    "MAX_ROWS",
    "NON_EVIDENCE_KINDS",
    "REFUSALS",
    "WINDOW_CHARS",
    "EvidenceRef",
    "StandingCandidate",
    "SubjectCandidate",
    "TypingInput",
    "TypingResult",
    "assemble",
    "build_typing_prompt",
    "evidence_texts",
    "evidence_window",
    "parse_valid_from",
    "quote_in_context",
    "same_subject_candidates",
    "stamp_support",
    "strip_as_of",
    "tally_refusals",
    "type_sentence",
]
