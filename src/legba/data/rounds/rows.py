# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seat output row types.

A small registry of versioned payload schemas, keyed ``<name>/<version>``.
Every seat declares one as its ``output``; the interpreter parses each reply
against it, and a reply that does not parse is a seat failure (the raw text
is kept on the receipt), never a silent empty row.

Vocabulary: a seat's unit of content is a SENTENCE with support
rows; what a round proposes for the record is a STATEMENT. Support roles and
kinds reuse the statement layer's values.

Adding a row type is one model here and one line in :data:`ROW_TYPES`. Pure;
no tables, no I/O.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Unquoted text: notes and challenges
# ---------------------------------------------------------------------------

#: What an unquoted field may not carry: a digit (a figure, a date, an id), a
#: bracketed citation (``[3]``, ``[[ref:..]]``, the full-width form) or a link.
#: Such a field has no quote behind it, so a figure or a citation in it would
#: reach a reader without any check.
_UNQUOTED_REFUSED = re.compile(r"[0-9\[\]\u3010\u3011]|https?://|www\.", re.IGNORECASE)


#: A seat's own item label (Q2, H3, S1, O2): allowed in unquoted text, since
#: it names an item of the round, not a row or a figure.
_ITEM_LABEL = re.compile(r"\b[A-Z]{1,2}[0-9]{1,2}\b")


def plain_text(value: str) -> str:
    """Refuse a citation, a link or a figure in a field that carries no quote."""
    hit = _UNQUOTED_REFUSED.search(_ITEM_LABEL.sub("", value))
    if hit is not None:
        raise ValueError(
            f"carries {hit.group(0)!r}: this field has no quote behind it, so it takes no "
            "citation, link, date or other figure")
    return value


#: A field written without quotes (``notes``, a challenge's text).
PlainText = Annotated[str, AfterValidator(plain_text)]

#: The fields a seat writes WITHOUT a quote, by row type. They are not
#: sentences about the record, so the quote, number and date gates do not read
#: them (``sentences_of`` and ``support_of`` never return them). ``notes`` and
#: ``challenges`` are also plain text (:func:`plain_text`). An assumption cites
#: rows only when it is marked ``supported``.
UNQUOTED_FIELDS: dict[str, tuple[str, ...]] = {
    "question_set/1": ("questions", "working_reading"),
    "hypothesis_set/1": ("hypotheses",),
    "assumption_set/1": ("assumptions (unless supported)", "notes"),
    "position/1": ("notes",),
    "challenge/1": ("challenges", "notes"),
    "context_pack/1": ("notes",),
    "disposition_set/1": ("dispositions[].notes",),
    "evidence_rows/1": ("notes",),
    "assessment/1": ("notes",),
    "ruling/1": ("notes",),
    "synthesis/1": ("notes",),
    "read/1": ("notes",),
    "turn/1": ("notes",),
}


# ---------------------------------------------------------------------------
# Shared parts
# ---------------------------------------------------------------------------

SupportRoleName = Literal["evidence", "contradicts", "context"]
SupportKindName = Literal["signal", "observation", "statement", "fact", "knowledge_item", "row"]
Stance = Literal["supports", "opposes", "neutral", "unsure"]
VerdictOutcome = Literal["stands", "falls", "unsure"]

#: The closed list a verdict's reason is drawn from.
VERDICT_REASONS = (
    "supported", "unsupported", "contradicted", "misquoted", "off_question", "illogical",
    "not_diagnostic", "not_decision_useful", "persona_drift", "weakest_link", "other",
)
VerdictReason = Literal[
    "supported", "unsupported", "contradicted", "misquoted", "off_question", "illogical",
    "not_diagnostic", "not_decision_useful", "persona_drift", "weakest_link", "other",
]


class EvidenceRow(_Row):
    """A substrate row as handed to a seat (signal, observation, …)."""

    id: str
    kind: SupportKindName = "signal"
    text: str | None = None
    title: str | None = None
    source_id: str | None = None
    origin_group_id: str | None = None
    access_class: str | None = None
    published_at: str | None = None
    fetch_version_id: int | None = None
    values: tuple[float, ...] = ()
    required: bool = False


class Support(_Row):
    ref_id: str
    role: SupportRoleName = "evidence"
    kind: SupportKindName = "signal"
    quote: str | None = None


class Sentence(_Row):
    key: str
    text: str
    stance: Stance = "neutral"
    support: tuple[Support, ...] = ()
    probability: float | None = Field(default=None, ge=0.0, le=1.0)


class Question(_Row):
    key: str
    text: str


# ---------------------------------------------------------------------------
# Row types
# ---------------------------------------------------------------------------


class FrozenScope(_Row):
    subject: str
    as_of: str
    rows: tuple[EvidenceRow, ...] = ()


class SeatSlices(_Row):
    core: tuple[str, ...] = ()
    slices: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    rows: tuple[EvidenceRow, ...] = ()


class ContextPack(_Row):
    """The rows a context builder keeps. The seat writes ``row_ids``; code
    fills ``rows`` from what the seat was handed or its tools returned
    (:func:`fill_rows_from_ids`), so no row text is ever re-typed."""

    row_ids: tuple[str, ...] = ()
    rows: tuple[EvidenceRow, ...] = ()
    notes: PlainText | None = None


class QuestionSet(_Row):
    questions: tuple[Question, ...]
    working_reading: str | None = None


class Disposition(_Row):
    question: str
    disposition: Literal["supported", "contradicted", "unresolved"]
    sentences: tuple[Sentence, ...] = ()
    notes: PlainText | None = None


class DispositionSet(_Row):
    dispositions: tuple[Disposition, ...] = ()


class EvidenceRows(_Row):
    rows: tuple[EvidenceRow, ...] = ()
    sentences: tuple[Sentence, ...] = ()
    notes: PlainText | None = None


class Series(_Row):
    key: str
    label: str
    values: tuple[float, ...] = ()
    rows: tuple[str, ...] = ()


class SeriesPanel(_Row):
    series: tuple[Series, ...] = ()


class EvidenceTable(_Row):
    rows: tuple[EvidenceRow, ...] = ()
    origin_groups: int = 0
    checkable_groups: int = 0
    basis: str = ""
    series: tuple[Series, ...] = ()


class Challenge(_Row):
    """A challenge to reasoning: a step that does not follow, or something a
    position leaves out. It is not a statement about the record, so it takes
    no quote — and therefore no citation and no figure either."""

    key: str
    text: PlainText
    target: str | None = None


class Position(_Row):
    sentences: tuple[Sentence, ...]
    summary: str | None = None
    probability: float | None = Field(default=None, ge=0.0, le=1.0)
    prose: str | None = None
    notes: PlainText | None = None


class Critique(_Row):
    """``challenge/1``: a challenge to other seats' positions. Statements
    about the record are sentences (quoted, gated); challenges to their
    reasoning are ``challenges`` (no quote)."""

    sentences: tuple[Sentence, ...] = ()
    challenges: tuple[Challenge, ...] = ()
    notes: PlainText | None = None


class TableSentence(_Row):
    key: str
    text: str
    stance: Stance
    cited: tuple[Support, ...] = ()
    probability: float | None = None


class TablePosition(_Row):
    position_id: str
    seat: str
    item: str | None = None
    sentences: tuple[TableSentence, ...] = ()
    probability: float | None = None
    challenges: tuple[Challenge, ...] = ()


class SentenceTable(_Row):
    positions: tuple[TablePosition, ...] = ()


class VerdictEntry(_Row):
    candidate: str
    outcome: VerdictOutcome
    reason: VerdictReason = "other"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Verdict(_Row):
    verdicts: tuple[VerdictEntry, ...] = ()
    ranking: tuple[str, ...] = ()
    settled: bool | None = None


#: What a judge may name as the problem with one sentence (``verdict/2``).
SENTENCE_PROBLEMS = (
    "none", "number_changed", "date_changed", "subject_changed", "adds_cause_or_intent",
    "adds_forecast", "claim_as_fact", "certainty_raised", "selective_quote",
    "contradicts_quote", "quote_does_not_say_it", "no_quote", "off_question",
    "does_not_follow", "other",
)
SentenceProblem = Literal[
    "none", "number_changed", "date_changed", "subject_changed", "adds_cause_or_intent",
    "adds_forecast", "claim_as_fact", "certainty_raised", "selective_quote",
    "contradicts_quote", "quote_does_not_say_it", "no_quote", "off_question",
    "does_not_follow", "other",
]


class SentenceCheck(_Row):
    """One judge's check of one sentence against the quotes it cites."""

    sentence: str
    holds: bool
    problem: SentenceProblem = "none"


class VerdictEntry2(_Row):
    """``verdict/2``: a ruling built from a check of EVERY sentence. The
    tally counts it as ``falls`` when any check does not hold, whatever the
    ``outcome`` says (:func:`legba.data.rounds.tally.votes_of`)."""

    candidate: str
    checks: tuple[SentenceCheck, ...] = ()
    weakest: str | None = None
    outcome: VerdictOutcome = "unsure"
    reason: VerdictReason = "other"


class Verdict2(_Row):
    verdicts: tuple[VerdictEntry2, ...] = ()
    ranking: tuple[str, ...] = ()


class WeakestLink(_Row):
    candidate: str | None = None
    sentence_key: str | None = None
    reason: VerdictReason = "weakest_link"
    note: str | None = None


class Absence(_Row):
    kind: str
    what: str
    detail: str | None = None


class BlindSpots(_Row):
    absences: tuple[Absence, ...] = ()


class Assumption(_Row):
    """An assumption a position rests on. Only one marked ``supported`` (a row
    states it) cites rows; an unsupported or unstated one is the seat's own
    reading of the positions and needs no quote."""

    key: str
    text: str
    status: Literal["supported", "unsupported", "unstated"]
    support: tuple[Support, ...] = ()
    if_wrong: str | None = None

    @model_validator(mode="after")
    def _supported_cites(self) -> "Assumption":
        if self.status == "supported" and not self.support:
            raise ValueError("an assumption marked supported cites the row that states it")
        return self


class AssumptionSet(_Row):
    assumptions: tuple[Assumption, ...] = ()
    notes: PlainText | None = None


class Hypothesis(_Row):
    id: str
    text: str


class HypothesisSet(_Row):
    hypotheses: tuple[Hypothesis, ...]


class Mark(_Row):
    ref_id: str
    mark: Literal["consistent", "inconsistent", "neutral"]


class Assessment(_Row):
    hypothesis: str
    diagnostic: bool
    marks: tuple[Mark, ...] = ()
    sentences: tuple[Sentence, ...] = ()
    notes: PlainText | None = None


class Matrix(_Row):
    hypotheses: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()
    cells: dict[str, dict[str, str]] = Field(default_factory=dict)
    inconsistency: dict[str, int] = Field(default_factory=dict)
    ranking: tuple[str, ...] = ()
    top_two_separated: bool = False


class Ruling(_Row):
    sentences: tuple[Sentence, ...]
    dissent: tuple[Sentence, ...] = ()
    unsure: bool = False
    notes: PlainText | None = None


class Proposal(_Row):
    text: str
    support: tuple[Support, ...] = ()
    gate: dict[str, Any] = Field(default_factory=dict)


class StatementProposals(_Row):
    proposals: tuple[Proposal, ...] = ()


class Synthesis(_Row):
    sentences: tuple[Sentence, ...]
    dissent: tuple[Sentence, ...] = ()
    would_prove_dissent: tuple[Sentence, ...] = ()
    notes: PlainText | None = None


class Read(_Row):
    sentences: tuple[Sentence, ...]
    dissent: tuple[Sentence, ...] = ()
    notes: PlainText | None = None


class Turn(_Row):
    sentences: tuple[Sentence, ...] = ()
    concede: bool = False
    notes: PlainText | None = None


class Concede(_Row):
    to: str | None = None
    note: str | None = None


class Selection(_Row):
    kept: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    by: str = ""
    tied_at_cut: bool = False


class TallyRow(_Row):
    """The stored form of a :class:`~.tally.TallyResult` (see :mod:`.tally`).
    ``advisory`` lists what judges WITHOUT a passing calibration said (their
    removals not applied, their votes not counted); ``calibration`` is each
    judge's record as the host read it."""

    rule: str
    outcome: str
    candidates: tuple[dict[str, Any], ...] = ()
    present: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    dissent: tuple[dict[str, Any], ...] = ()
    advisory: tuple[dict[str, Any], ...] = ()
    calibration: dict[str, Any] = Field(default_factory=dict)


class Absent(_Row):
    reason: str


#: The registry: ``<name>/<version>`` -> payload model.
ROW_TYPES: dict[str, type[_Row]] = {
    "frozen_scope/1": FrozenScope,
    "seat_slices/1": SeatSlices,
    "context_pack/1": ContextPack,
    "question_set/1": QuestionSet,
    "disposition_set/1": DispositionSet,
    "evidence_rows/1": EvidenceRows,
    "series_panel/1": SeriesPanel,
    "evidence_table/1": EvidenceTable,
    "position/1": Position,
    "challenge/1": Critique,
    "sentence_table/1": SentenceTable,
    "verdict/1": Verdict,
    "verdict/2": Verdict2,
    "weakest_link/1": WeakestLink,
    "blind_spots/1": BlindSpots,
    "assumption_set/1": AssumptionSet,
    "hypothesis_set/1": HypothesisSet,
    "assessment/1": Assessment,
    "matrix/1": Matrix,
    "ruling/1": Ruling,
    "statement_proposals/1": StatementProposals,
    "synthesis/1": Synthesis,
    "read/1": Read,
    "turn/1": Turn,
    "concede/1": Concede,
    "selection/1": Selection,
    "tally/1": TallyRow,
    "absent/1": Absent,
}

#: Row types a judge seat may write (its output must be a verdict/* type).
JUDGE_ROW_TYPES = frozenset({"verdict/1", "verdict/2", "weakest_link/1"})


class RowTypeError(ValueError):
    """A payload that does not parse as its declared row type."""


#: The most validation errors one :class:`RowTypeError` message lists.
MAX_ROW_ERRORS_LISTED = 8


def row_fields(row_type: str) -> frozenset[str]:
    model = ROW_TYPES.get(row_type)
    return frozenset(model.model_fields) if model is not None else frozenset()


def parse_row(row_type: str, payload: Any) -> dict[str, Any]:
    """Validate ``payload`` against ``row_type``; return its JSON-safe dict."""
    model = ROW_TYPES.get(row_type)
    if model is None:
        raise RowTypeError(f"row type {row_type!r} is not registered")
    try:
        return model.model_validate(payload).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        # Every error, not only the first: a retry that is told one broken
        # field fixes that one and can break another, so it runs out of
        # attempts one field at a time. Bounded so the message stays short.
        errors = exc.errors()
        parts = [
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg')}"
            for err in errors[:MAX_ROW_ERRORS_LISTED]
        ]
        if len(errors) > MAX_ROW_ERRORS_LISTED:
            parts.append(f"{len(errors) - MAX_ROW_ERRORS_LISTED} more")
        raise RowTypeError(f"{row_type}: {'; '.join(parts)}") from None


#: Row types whose rows a seat names by id; code fills in the rows.
ID_FILLED_ROW_TYPES: frozenset[str] = frozenset({"context_pack/1"})


def fill_rows_from_ids(row_type: str, payload: Any,
                       handed_rows: Mapping[str, Mapping[str, Any]]) -> Any:
    """Fill ``rows`` from ``row_ids`` with the rows the seat was handed.

    The seat names each row it keeps by id; the row itself is copied from
    ``handed_rows`` (the handed inputs plus the rows its own tools returned),
    so its text is the record's, never the model's retyping. A reply that
    wrote whole rows instead is read for their ids only. An id that was not
    handed stays in ``row_ids`` with no row, so the ``handed_rows`` gate
    refuses it by name. Other row types pass through untouched.
    """
    if row_type not in ID_FILLED_ROW_TYPES or not isinstance(payload, Mapping):
        return payload
    out = dict(payload)
    raw = out.get("row_ids")
    if not raw:
        raw = [r.get("id") for r in out.get("rows") or () if isinstance(r, Mapping)]
    ids: list[str] = []
    for i in raw or ():
        if isinstance(i, str) and i.strip() and i.strip() not in ids:
            ids.append(i.strip())
    by_lower = {str(k).lower(): v for k, v in handed_rows.items()}
    fields = set(EvidenceRow.model_fields)
    rows = []
    for i in ids:
        row = handed_rows.get(i) or by_lower.get(i.lower())
        if row is not None:
            rows.append({k: v for k, v in row.items() if k in fields})
    out["row_ids"] = ids
    out["rows"] = rows
    return out


def sentences_of(payload: Any) -> list[dict[str, Any]]:
    """Every sentence-shaped entry a payload carries (positions, rulings,
    reads, syntheses, dispositions, assessments; dissent included)."""
    out: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return out
    for key in ("sentences", "dissent", "would_prove_dissent"):
        for s in payload.get(key) or ():
            if isinstance(s, dict):
                out.append(s)
    for d in payload.get("dispositions") or ():
        if isinstance(d, dict):
            out.extend(s for s in d.get("sentences") or () if isinstance(s, dict))
    return out


def support_of(payload: Any) -> list[dict[str, Any]]:
    """Every support row a payload's sentences, assumptions and proposals cite."""
    out: list[dict[str, Any]] = []
    for s in sentences_of(payload):
        out.extend(r for r in s.get("support") or () if isinstance(r, dict))
    if isinstance(payload, dict):
        for key in ("assumptions", "proposals"):
            for a in payload.get(key) or ():
                if isinstance(a, dict):
                    out.extend(r for r in a.get("support") or () if isinstance(r, dict))
    return out
