# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The statement gates' INPUT and OUTPUT shapes — the interface the typing and
verifier seats, the store and the backfill build against.

WHAT THIS IS. The code gates in :mod:`legba.data.statement_gate` decide whether
a proposed statement's support survives the checks code can run without a
model (G1 quote, G2 content, G3 handed rows, G7 independence, G8
support roles, G13 access ceiling). They are pure functions over three inputs:

* a **statement** (:class:`StatementLike`) — text, kind, subject, valid time,
  slots;
* its **support rows** (:class:`SupportLike`) — one per edge, with its role,
  kind, quote, receipt id, source, origin group, access class and source class;
* the **texts** those rows point at (:class:`EvidenceTexts`) — the stored text
  of each cited fetch version, signal and observation, resolved by the caller.

STORAGE-NEUTRAL ON PURPOSE. Whether statements land in a new layer beside
``facts`` or in an extension of ``facts`` is still undecided. Nothing here names a table, a column type or an ORM class. The
inputs are :class:`typing.Protocol` s that any row object satisfies by
attribute — the frozen dataclasses below, a ``SimpleNamespace``, an asyncpg
``Record`` wrapped in one, or a future store's own row type. Field names follow
the column names so a row of either storage shape maps 1:1.

THE CLAIM SOURCE CLASS IS AN INPUT. The source class that marks a
source as a party making claims is built separately and its field name is not
final. The gates never import it: a support row carries
``source_class`` as a :class:`SourceClass` (or its string value), and the
caller maps whatever field lands to it with :func:`coerce_source_class`.

A LEAF: stdlib only — no I/O, no model, no package import beyond the stdlib,
so every module may import it without closing a cycle.
"""
from __future__ import annotations

import enum
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

#: Bumped whenever a gate's rule changes what it admits. Written beside every
#: verdict (``GateReport.as_dict``) so a stored verdict names its rules.
#: v3 (S-5): G1 records which stored text a quote was found in and admits a
#: quote found only in the signal's feed item (title / summary) — recorded as
#: such and marked not checkable; G2 checks a typed counterpart's name.
#: v4 (S-6): G2 reads a figure's qualifier, unit, form and range
#: (``statement_numbers``: "about 300" needs a quote stating 300 with a
#: compatible qualifier; a range never supports a single value; percent is
#: not percentage points; ordinals and "N-day" compounds; statement number
#: words incl. compounds and scales; French number words and month names and
#: space-grouped thousands in quotes); the date rule reads date ranges and
#: two-day lists (a quote's range endpoints are no longer single dates); the
#: coverage gate (``statement_coverage``) is new.
GATE_VERSION = "statement_gate.v4"


class _StrEnum(str, enum.Enum):
    """A str-valued enum whose ``str()`` is its value (JSON-stable)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class StatementKind(_StrEnum):
    """``statements.kind`` — decides how a statement ages."""

    EVENT = "event"
    STATE = "state"
    LEVEL = "level"
    ABSENCE = "absence"
    ATTRIBUTED = "attributed"
    ASSESSMENT = "assessment"
    #: S-6 — a statement about what the COLLECTION holds or how its
    #: reporting framed something ("three outlets reported", "reporting
    #: framed X as Y"): scoped to the handed slice, counted against the rows
    #: it cites, never establishing the reported event (``statement_coverage``).
    COVERAGE = "coverage"


class SubjectKind(_StrEnum):
    """The one subject reference type shared with the
    knowledge design (``subject_kind``, ``subject_key``)."""

    TARGET = "target"
    ENTITY = "entity"
    SERIES = "series"
    PLACE = "place"


class SupportRole(_StrEnum):
    """``statement_support.role``. Only ``evidence`` counts toward
    support; ``contradicts`` is kept and shown."""

    EVIDENCE = "evidence"
    PREMISE = "premise"
    BACKGROUND = "background"
    CONTRADICTS = "contradicts"


class SupportKind(_StrEnum):
    """``statement_support.support_kind`` (``person_assertion`` is
    dropped; ``fact`` is background-only)."""

    SIGNAL = "signal"
    OBSERVATION = "observation"
    STATEMENT = "statement"
    KNOWLEDGE_ITEM = "knowledge_item"
    FACT = "fact"


class SourceClass(_StrEnum):
    """What KIND of speaker a support row's source is, as far as the gates
    care. Only :attr:`CLAIM` changes a verdict (G2's claim rule: a claim-class
    source supports only an ``attributed`` statement, "X claimed Y").

    ``REPORTING`` is every declared non-claim class; ``UNDECLARED`` is a source
    that declares nothing — treated like reporting by the claim rule (the rule
    refuses only what is DECLARED a claim), and reported as such."""

    CLAIM = "claim"
    REPORTING = "reporting"
    UNDECLARED = "undeclared"


class TextOrigin(_StrEnum):
    """Which stored text a quote was checked against — the row records which
    (G1: "the row records which")."""

    FETCH_VERSION = "fetch_version"
    #: B-1: the ITEM as the response carried it (a slice of the fetch version
    #: by the link's locator — ``legba.data.byte_path.item_as_received``).
    FETCH_ITEM = "fetch_item"
    SIGNAL_TEXT = "signal_text"
    OBSERVATION = "observation"
    KNOWLEDGE_ITEM_VERSION = "knowledge_item_version"


class QuoteFoundIn(_StrEnum):
    """WHICH text of a signal a quote was found in (S-5). A signal carries two
    texts: its FEED ITEM (the title and summary the feed published, stored on
    the signal) and its PAGE TEXT (the archived page's extracted text, or the
    signal's stored body when no receipt exists). A quote found only in the
    feed item is a finding about the evidence — the feed's summary and the
    page differ, or the page changed — so it is recorded, never papered over.
    """

    FEED_ITEM = "feed_item"
    PAGE_TEXT = "page_text"
    BOTH = "both"


class ValidFromPrecision(_StrEnum):
    """``valid_from_precision``."""

    DAY = "day"
    MONTH = "month"
    UNKNOWN = "unknown"


#: The four closed slot names (``slots`` — "predicate, value, unit,
#: counterpart"), kind-checked in code.
SLOT_NAMES: frozenset[str] = frozenset({"predicate", "value", "unit", "counterpart"})

#: The value a support row carries when its source declares no access class.
#: Its own value — never silently promoted to ``public``.
UNDECLARED_ACCESS = "undeclared"


def coerce_source_class(
    value: Any, *, claim_values: Iterable[str] = ("claim",),
) -> SourceClass:
    """Map whatever the claim-source-class lane lands on a source to a
    :class:`SourceClass`.

    ``claim_values`` names the raw values that mean "this source is a party
    making claims" — the seam that keeps the gates independent of that lane's
    final field name and vocabulary. ``None`` / empty is ``UNDECLARED``; any
    other value is ``REPORTING``. A :class:`SourceClass` passes through."""
    if isinstance(value, SourceClass):
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        return SourceClass.UNDECLARED
    raw = str(value).strip().lower()
    if raw in {str(v).strip().lower() for v in claim_values}:
        return SourceClass.CLAIM
    if raw == SourceClass.UNDECLARED.value:
        return SourceClass.UNDECLARED
    return SourceClass.REPORTING


# ---------------------------------------------------------------------------
# Input protocols — what a row must expose, by attribute
# ---------------------------------------------------------------------------


@runtime_checkable
class StatementLike(Protocol):
    """A proposed statement version, as the gates read it.

    ``kind`` / ``subject_kind`` may be the enum or its string value.
    ``valid_from`` is timezone-aware or naive-UTC; ``None`` means unknown."""

    @property
    def text(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def subject_kind(self) -> str: ...
    @property
    def subject_key(self) -> str: ...
    @property
    def valid_from(self) -> datetime | None: ...
    @property
    def valid_from_precision(self) -> str | None: ...
    @property
    def slots(self) -> Mapping[str, Any] | None: ...


@runtime_checkable
class SupportLike(Protocol):
    """One support edge, as the gates read it (``statement_support``
    plus the three inputs the store copies at write: ``published_at``,
    ``title``, ``source_class``).

    * ``ref_id`` — the signal / observation / statement / item / fact id.
    * ``fetch_version_id`` — the receipt the quote was read from; ``None`` for
      signals older than receipts (G1 then checks the signal's stored text and
      says so).
    * ``published_at`` — the evidence's publication time (G2's valid-from
      check). A caller with only a fetch time passes that and says so.
    * ``ref_statement_kind`` — for a ``statement`` edge, the cited statement's
      kind (G8: an assessment never supports a factual statement).
    """

    @property
    def role(self) -> str: ...
    @property
    def support_kind(self) -> str: ...
    @property
    def ref_id(self) -> str: ...
    @property
    def quote(self) -> str | None: ...
    @property
    def text_sha256(self) -> str | None: ...
    @property
    def fetch_version_id(self) -> int | None: ...
    @property
    def source_id(self) -> str | None: ...
    @property
    def origin_group_id(self) -> str | None: ...
    @property
    def checkable(self) -> bool | None: ...
    @property
    def access_class(self) -> str | None: ...
    @property
    def source_class(self) -> SourceClass | str | None: ...
    @property
    def published_at(self) -> datetime | None: ...
    @property
    def title(self) -> str | None: ...
    @property
    def ref_statement_kind(self) -> str | None: ...


# ---------------------------------------------------------------------------
# Concrete inputs — dataclasses that satisfy the protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementDraft:
    """A ready-made :class:`StatementLike` (the typing seat's output shape)."""

    text: str
    kind: str
    subject_kind: str
    subject_key: str
    valid_from: datetime | None = None
    valid_from_precision: str | None = None
    slots: Mapping[str, Any] | None = None
    #: When the record LEARNED it (an as-of frame naming the read's own day,
    #: stripped from the valid-time slot — ``statement_dates`` rule (b)).
    #: Not part of :class:`StatementLike`; a store maps it to its known-at.
    known_at: datetime | None = None


@dataclass(frozen=True)
class SupportRow:
    """A ready-made :class:`SupportLike`."""

    role: str
    support_kind: str
    ref_id: str
    quote: str | None = None
    text_sha256: str | None = None
    fetch_version_id: int | None = None
    source_id: str | None = None
    origin_group_id: str | None = None
    checkable: bool | None = None
    access_class: str | None = None
    source_class: SourceClass | str | None = None
    published_at: datetime | None = None
    title: str | None = None
    ref_statement_kind: str | None = None
    #: S-5 — which text of the signal the quote was found in
    #: (:class:`QuoteFoundIn` value); ``None`` when not computed. Not part of
    #: :class:`SupportLike`: the gates read it with ``getattr``.
    quote_found_in: str | None = None
    #: S-5 — the quote extended to the whole sentence of the stored text it
    #: sits in (verbatim, code-found), for the record to show. The gates check
    #: ``quote``; the verifier is shown ``quote`` too (context measured to
    #: loosen it); this is context, never support of its own.
    quote_context: str | None = None


@dataclass(frozen=True)
class ObservationText:
    """The stored form of one cited observation: its rendered line (what a
    quote is checked against) and its numeric value(s), which G2 accepts for a
    number the statement states."""

    text: str
    values: tuple[float, ...] = ()


@dataclass(frozen=True)
class EvidenceTexts:
    """The stored texts the support rows point at, resolved by the caller.

    Keyed by id; any ``Mapping`` works (a lazy DB-backed mapping included).
    A text the caller could not resolve is simply absent — the gate then fails
    the row with ``text_unavailable``, never passes it."""

    by_fetch_version: Mapping[int, str] = field(default_factory=dict)
    #: B-1: ``(fetch_version_id, signal id) -> the item as received`` — one
    #: feed version carries many items, so an item's text is keyed by both.
    by_fetch_item: Mapping[tuple[int, str], str] = field(default_factory=dict)
    by_signal: Mapping[str, str] = field(default_factory=dict)
    by_observation: Mapping[str, ObservationText] = field(default_factory=dict)
    by_knowledge_item_version: Mapping[str, str] = field(default_factory=dict)
    #: S-5 — a signal's FEED ITEM texts (its stored title and summary, each a
    #: separate text), by signal id. G1 looks here when a quote is not in the
    #: page text, and records which (:class:`QuoteFoundIn`).
    feed_item_by_signal: Mapping[str, Sequence[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class HandedSlice:
    """The rows handed to ONE run (G3): ids exactly as the slice carried them.

    ``run_id`` is recorded on the verdict so a refusal names the run. The
    optional scope fields (S-5) say WHAT the slice was — the collection it was
    drawn from, its sources and its time window — so an absence statement can
    be scoped to the collection it is about, in code."""

    ref_ids: frozenset[str]
    run_id: str | None = None
    collection: str | None = None
    source_ids: frozenset[str] = frozenset()
    window_start: datetime | None = None
    window_end: datetime | None = None

    @classmethod
    def of(
        cls, ids: Iterable[Any], run_id: str | None = None, *,
        collection: str | None = None, source_ids: Iterable[Any] = (),
        window_start: datetime | None = None, window_end: datetime | None = None,
    ) -> HandedSlice:
        return cls(frozenset(str(i).lower() for i in ids if i is not None), run_id, collection,
                   frozenset(str(s) for s in source_ids if s), window_start, window_end)

    def __contains__(self, ref: object) -> bool:
        return ref is not None and str(ref).lower() in self.ref_ids


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One code check's verdict.

    ``gate`` is the plan's gate id (``G1`` …); ``check`` names the rule within
    it; ``row`` is the support row's index in the caller's list (``None`` for a
    statement-level check). ``detail`` is JSON-safe and goes, as is, into the
    ``checks`` / ``gate`` jsonb the store writes."""

    gate: str
    check: str
    passed: bool
    reason: str | None = None
    row: int | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"gate": self.gate, "check": self.check, "passed": self.passed}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.row is not None:
            out["row"] = self.row
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass(frozen=True)
class GateReport:
    """Every code check run on one proposal.

    ``passed`` is the conjunction of every check — the code gates' answer
    only. It is NOT establishment: the clean-context support check (G4) and
    the lifecycle decide that, elsewhere."""

    checks: tuple[CheckResult, ...]
    independent_groups: int = 0
    checkable_groups: int = 0
    contradicting_groups: int = 0
    support_word: str = "unclear"
    access_ceiling: str = UNDECLARED_ACCESS
    gate_version: str = GATE_VERSION

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def row_checks(self, row: int) -> list[dict[str, Any]]:
        """The ``statement_support.checks`` jsonb for support row ``row``."""
        return [c.as_dict() for c in self.checks if c.row == row]

    def as_dict(self) -> dict[str, Any]:
        """The ``statement_events.gate`` jsonb."""
        return {
            "gate_version": self.gate_version,
            "passed": self.passed,
            "independent_groups": self.independent_groups,
            "checkable_groups": self.checkable_groups,
            "contradicting_groups": self.contradicting_groups,
            "support_word": self.support_word,
            "access_ceiling": self.access_ceiling,
            "checks": [c.as_dict() for c in self.checks],
        }


def text_sha256(text: str) -> str:
    """The sha256 of a stored text, as ``statement_support.text_sha256``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def enum_value(value: Any) -> str:
    """The plain string of an enum member or string (``None`` → ``""``)."""
    if value is None:
        return ""
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


def values_of(members: Sequence[enum.Enum]) -> frozenset[str]:
    return frozenset(str(m.value) for m in members)


__all__ = [
    "GATE_VERSION",
    "SLOT_NAMES",
    "UNDECLARED_ACCESS",
    "CheckResult",
    "EvidenceTexts",
    "GateReport",
    "HandedSlice",
    "ObservationText",
    "QuoteFoundIn",
    "SourceClass",
    "StatementDraft",
    "StatementKind",
    "StatementLike",
    "SubjectKind",
    "SupportKind",
    "SupportLike",
    "SupportRole",
    "SupportRow",
    "TextOrigin",
    "ValidFromPrecision",
    "coerce_source_class",
    "enum_value",
    "text_sha256",
    "values_of",
]
