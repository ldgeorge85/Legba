# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desk OUTPUT CONTRACT v2 — the factual part of a read as typed ROWS.

WHY. A desk (an ``inline_target`` unit) writes one prose finding with ``[N]``
markers. Lanes S-3..S-6 measured how much of that prose can be turned into
checkable statements after the fact: on fresh desk sentences 16-29 % can be
established, 20-35 % are legitimately not statements, and about half of the
~41-44 % that fail fail because of what the desk is ASKED to write — collection
counts in prose, a claim and an absence fused in one sentence, a sentence with
no row behind it, a finding with no slice scope. Contract v2 asks the desk for
the rows directly, so the checks run on what the desk meant instead of on a
reconstruction of it.

THE SHAPE (what the model writes; :func:`parse_rows_reply` reads it):

* ``fact_rows[]`` — ONE claim each: ``text`` (one sentence, at most one
  figure), ``kind`` (event / state / level / attributed), ``subject`` (an id
  from the SUBJECTS list code offered: the desk's subject, related subjects,
  actors linked to or named in the slice's rows — or, v2.1, with
  ``subject_kind: "surface"``, an actor's name copied verbatim from the
  row's quote when no offered subject matches it,
  :mod:`desk_rows_surface`), optional ``counterpart`` (another offered
  id, or words of the row's own text), ``valid_from`` + precision, and
  ``cites`` — for each cited slice row its ``[N]`` and the EXACT words quoted
  from it.
* ``absence_rows[]`` — what was looked for and not found: the model names the
  TOPIC only; code writes the sentence and attaches the collection, sources
  and window it is scoped to (:mod:`desk_rows_computed`).
* ``assessment_rows[]`` — a judgement, its confidence WORD (the ICD-203 bands
  the house uses), and ``rests_on``: the ids of the fact rows it rests on. An
  assessment never quotes.
* ``restatements[]`` — a prior-read entry carried forward BY REF (the ``[N]``
  of the PRIOR READ block), ``confirmed`` / ``revised`` /
  ``no_longer_supported``, and the fact rows that bear on it.
* the read's furniture: ``title``, ``severity`` + ``severity_delta`` tags,
  ``confidence``, ``would_change`` (the one observation that would move the
  verdict, its reporting class, whether this desk collects it),
  ``indicators`` (short signpost strings), and an optional ``lead`` (one or
  two sentences that may only restate rows — checked sentence by sentence in
  code, :mod:`desk_rows_render`).

The COMPUTED block — rows handed, sources, origin groups, time span, the
coverage gap — is NOT written by the model; code builds it from the slice
(:mod:`desk_rows_computed`). No collection count is ever asked of the model.

This module only READS a reply: every defect is a named, counted reason; a
row is never silently dropped. Gating is :mod:`desk_rows_gates`.

A LEAF: stdlib + the seat reply helpers.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .likelihood_words import LIKELIHOOD_BANDS
from .statement_seats import as_text

#: The contract's name, stamped on every shadow record. ``v2.1``:
#: a fact row may take a SURFACE subject (``subject_kind: surface``), actors
#: are offered when a cited row names them (not only when it links them), and
#: the repair round's rule changed (words from the row's own quotes; a split).
CONTRACT_VERSION = "desk_rows.v2.1"

#: How a fact row names its subject: an id from the SUBJECTS list code
#: offered (``offered``), or — when the quote names an actor no offered
#: subject matches — the actor's name copied verbatim from the quote
#: (``surface``; :mod:`desk_rows_gates` holds the rule).
SUBJECT_KINDS = frozenset({"offered", "surface"})
#: Rows one split may produce in the repair round.
MAX_SPLIT_ROWS = 4

#: The statement kinds a FACT row may take (the evidence-bearing kinds; an
#: assessment / absence / coverage written as a fact row is a defect).
FACT_KINDS = frozenset({"event", "state", "level", "attributed"})

RESTATEMENT_STATUSES = frozenset({"confirmed", "revised", "no_longer_supported"})

#: The seven likelihood bands, from the one shared table. "cannot say" is not
#: among them: the parser does not accept it as a row's confidence.
CONFIDENCE_WORDS: tuple[str, ...] = tuple(word for _cut, word in LIKELIHOOD_BANDS)
_CONFIDENCE_ALIASES = {
    "remote": "almost no chance", "highly unlikely": "very unlikely",
    "roughly even": "roughly even chance", "even chance": "roughly even chance",
    "probable": "likely", "probably": "likely", "highly likely": "very likely",
    "almost certainly": "almost certain", "virtually certain": "almost certain",
}

#: The word a read gives when nothing in its window bears on the question
#: (a level and a change both accept it).
NO_LEVEL = "none"
SEVERITY_DELTAS = frozenset({"rose", "fell", "steady", "new", NO_LEVEL})


def severity_levels() -> tuple[str, ...]:
    """The profile's level words, lowest first (``constants.levels``). A
    profile that declares none cannot run the desk-rows seat: refused by
    name, never another profile's words."""
    from .profile_constants import current, levels

    words = levels()
    if not words:
        raise ValueError(f"desk_rows.levels_undeclared: profile {current().profile!r} declares no "
                         "constants.levels; the desk-rows seat needs its level words")
    return words
DESK_COLLECTS = frozenset({"yes", "no", "unknown"})

#: Rows of one kind read from one reply. Rows past it are counted, not read —
#: a bound on what one shadow record holds, never on what the model may write.
MAX_ROWS_PER_KIND = 40

_ID_RE = {"fact": re.compile(r"^F\d{1,3}$"), "absence": re.compile(r"^A\d{1,3}$"),
          "assessment": re.compile(r"^J\d{1,3}$"), "restatement": re.compile(r"^R\d{1,3}$")}
_PREFIX = {"fact": "F", "absence": "A", "assessment": "J", "restatement": "R"}
_MARKER_RE = re.compile(r"\[\s*(\d+)\s*\]")


@dataclass(frozen=True)
class Cite:
    """One cited slice row: its ``[N]`` and the words quoted from it."""

    row: int | None
    quote: str


@dataclass(frozen=True)
class FactRow:
    id: str
    text: str
    kind: str
    subject: str
    counterpart: str
    valid_from: str
    valid_from_precision: str
    cites: tuple[Cite, ...]
    #: Parse-level defects (named reasons), e.g. ``id_reassigned``.
    defects: tuple[str, ...] = ()
    #: The model's own id when code reassigned it (so ``rests_on`` resolves).
    model_id: str | None = None
    #: ``offered`` (``subject`` is an S-id) or ``surface`` (``subject`` is a
    #: name copied from the quote). An unknown value is read as ``offered``
    #: and noted (``subject_kind_unknown``).
    subject_kind: str = "offered"
    #: Set on a row the repair round SPLIT out of another: that row's id.
    split_from: str | None = None


@dataclass(frozen=True)
class AbsenceRow:
    id: str
    topic: str
    defects: tuple[str, ...] = ()
    model_id: str | None = None


@dataclass(frozen=True)
class AssessmentRow:
    id: str
    text: str
    confidence: str | None
    confidence_raw: str
    rests_on: tuple[str, ...]
    defects: tuple[str, ...] = ()
    model_id: str | None = None


@dataclass(frozen=True)
class Restatement:
    id: str
    ref: int | None
    status: str | None
    text: str
    bearing: tuple[str, ...]
    defects: tuple[str, ...] = ()
    model_id: str | None = None


@dataclass(frozen=True)
class WouldChange:
    observation: str
    reporting_class: str
    desk_collects: str


@dataclass(frozen=True)
class RowsReply:
    """One parsed contract-v2 reply."""

    fact_rows: tuple[FactRow, ...]
    absence_rows: tuple[AbsenceRow, ...]
    assessment_rows: tuple[AssessmentRow, ...]
    restatements: tuple[Restatement, ...]
    title: str = ""
    severity: str | None = None
    severity_delta: str | None = None
    confidence: float | None = None
    lead: str = ""
    would_change: WouldChange | None = None
    indicators: tuple[str, ...] = ()
    #: Reply-level defects (``fact_rows_not_a_list``, ``rows_over_cap:fact`` …).
    defects: tuple[str, ...] = ()

    def all_rows(self) -> tuple[Any, ...]:
        return (*self.fact_rows, *self.absence_rows, *self.assessment_rows, *self.restatements)

    def row(self, row_id: str) -> Any | None:
        return next((r for r in self.all_rows() if r.id == row_id), None)

    def with_rows(self, **rows: Sequence[Any]) -> RowsReply:
        return replace(self, **{k: tuple(v) for k, v in rows.items()})


# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------


def normalize_confidence(raw: Any) -> str | None:
    """The house band a confidence word names, or ``None``."""
    text = re.sub(r"\s+", " ", as_text(raw).lower().replace("_", " ")).strip(" .")
    if text in CONFIDENCE_WORDS:
        return text
    return _CONFIDENCE_ALIASES.get(text)


def _row_number(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    m = _MARKER_RE.fullmatch(as_text(raw)) or re.fullmatch(r"(\d+)", as_text(raw))
    return int(m.group(1)) if m else None


def _ids(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(as_text(x).upper() for x in raw if as_text(x)))


def _cites(raw: Any, defects: list[str]) -> tuple[Cite, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        defects.append("cites_not_a_list")
        return ()
    out: list[Cite] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            if "cite_not_an_object" not in defects:
                defects.append("cite_not_an_object")
            continue
        out.append(Cite(_row_number(entry.get("row")), as_text(entry.get("quote"))))
    return tuple(out)


def _list(obj: Mapping[str, Any], key: str, defects: list[str]) -> list[Any]:
    raw = obj.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        defects.append(f"{key}_not_a_list")
        return []
    if len(raw) > MAX_ROWS_PER_KIND:
        defects.append(f"rows_over_cap:{key}:{len(raw) - MAX_ROWS_PER_KIND}")
    return raw[:MAX_ROWS_PER_KIND]


def _assign_ids(kind: str, entries: list[Mapping[str, Any]]) -> list[tuple[str, str | None, list[str]]]:
    """``[(id, model_id_if_reassigned, defects)]`` — the model's id when it
    is well formed and unique, else the next free one (counted)."""
    seen: set[str] = set()
    out: list[tuple[str, str | None, list[str]]] = []
    wanted = [as_text(e.get("id")).upper() for e in entries]
    taken = {w for w in wanted if _ID_RE[kind].match(w)}
    n = 0
    for w in wanted:
        if _ID_RE[kind].match(w) and w not in seen:
            seen.add(w)
            out.append((w, None, []))
            continue
        n += 1
        while f"{_PREFIX[kind]}{n}" in taken or f"{_PREFIX[kind]}{n}" in seen:
            n += 1
        new = f"{_PREFIX[kind]}{n}"
        seen.add(new)
        out.append((new, w or None, ["id_reassigned"]))
    return out


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def parse_rows_reply(obj: Mapping[str, Any] | None) -> RowsReply | None:
    """A parsed contract-v2 reply, or ``None`` when ``obj`` is not an object.

    Pure. Reads what is there and names what is wrong; it does not judge a
    row (that is :mod:`desk_rows_gates`)."""
    if not isinstance(obj, Mapping):
        return None
    defects: list[str] = []
    facts: list[FactRow] = []
    entries = [e for e in _list(obj, "fact_rows", defects) if isinstance(e, Mapping)]
    for e, (rid, model_id, d) in zip(entries, _assign_ids("fact", entries)):
        d = list(d)
        cites = _cites(e.get("cites"), d)
        sk = as_text(e.get("subject_kind")).lower() or "offered"
        if sk not in SUBJECT_KINDS:
            d.append("subject_kind_unknown")
            sk = "offered"
        facts.append(FactRow(
            rid, as_text(e.get("text")), as_text(e.get("kind")).lower(), as_text(e.get("subject")),
            as_text(e.get("counterpart")), as_text(e.get("valid_from")),
            as_text(e.get("valid_from_precision")).lower(), cites, tuple(d), model_id, sk))
    absences: list[AbsenceRow] = []
    entries = [e for e in _list(obj, "absence_rows", defects) if isinstance(e, Mapping)]
    for e, (rid, model_id, d) in zip(entries, _assign_ids("absence", entries)):
        absences.append(AbsenceRow(rid, as_text(e.get("topic") or e.get("text")), tuple(d), model_id))
    judgements: list[AssessmentRow] = []
    entries = [e for e in _list(obj, "assessment_rows", defects) if isinstance(e, Mapping)]
    for e, (rid, model_id, d) in zip(entries, _assign_ids("assessment", entries)):
        raw_conf = as_text(e.get("confidence"))
        judgements.append(AssessmentRow(rid, as_text(e.get("text")), normalize_confidence(raw_conf), raw_conf,
                                        _ids(e.get("rests_on")), tuple(d), model_id))
    restated: list[Restatement] = []
    entries = [e for e in _list(obj, "restatements", defects) if isinstance(e, Mapping)]
    for e, (rid, model_id, d) in zip(entries, _assign_ids("restatement", entries)):
        status = as_text(e.get("status")).lower().replace(" ", "_").replace("-", "_")
        restated.append(Restatement(rid, _row_number(e.get("ref")),
                                    status if status in RESTATEMENT_STATUSES else None,
                                    as_text(e.get("text")), _ids(e.get("bearing")), tuple(d), model_id))
    # a reassigned id is resolved for rests_on / bearing through the model's id
    remap = {r.model_id: r.id for r in (*facts, *absences) if r.model_id}
    judgements = [replace(j, rests_on=tuple(remap.get(x, x) for x in j.rests_on)) for j in judgements]
    restated = [replace(r, bearing=tuple(remap.get(x, x) for x in r.bearing)) for r in restated]

    wc = obj.get("would_change")
    would = None
    if isinstance(wc, Mapping):
        collects = as_text(wc.get("desk_collects")).lower()
        would = WouldChange(as_text(wc.get("observation")), as_text(wc.get("reporting_class")),
                            collects if collects in DESK_COLLECTS else "unknown")
    severity = as_text(obj.get("severity")).lower().removeprefix("severity:") or None
    delta = as_text(obj.get("severity_delta")).lower().removeprefix("severity_delta:") or None
    conf = obj.get("confidence")
    try:
        confidence = float(conf) if conf is not None and not isinstance(conf, bool) else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        defects.append("confidence_out_of_range")
        confidence = None
    indicators = obj.get("indicators")
    return RowsReply(
        tuple(facts), tuple(absences), tuple(judgements), tuple(restated),
        title=as_text(obj.get("title")),
        severity=severity if severity in (*severity_levels(), NO_LEVEL) else None,
        severity_delta=delta if delta in SEVERITY_DELTAS else None,
        confidence=confidence, lead=as_text(obj.get("lead")), would_change=would,
        indicators=tuple(as_text(i.get("statement") if isinstance(i, Mapping) else i)
                         for i in (indicators if isinstance(indicators, list) else []) if i)[:12],
        defects=tuple(defects),
    )


def reply_as_dict(reply: RowsReply) -> dict[str, Any]:
    """The reply as plain JSON (the shadow record's ``rows`` and the repair
    prompt's rows)."""
    return {
        "fact_rows": [fact_as_dict(f) for f in reply.fact_rows],
        "absence_rows": [{"id": a.id, "topic": a.topic} for a in reply.absence_rows],
        "assessment_rows": [{"id": j.id, "text": j.text, "confidence": j.confidence or j.confidence_raw,
                             "rests_on": list(j.rests_on)} for j in reply.assessment_rows],
        "restatements": [{"id": r.id, "ref": r.ref, "status": r.status, "text": r.text,
                          "bearing": list(r.bearing)} for r in reply.restatements],
    }


def fact_as_dict(f: FactRow) -> dict[str, Any]:
    out = {"id": f.id, "text": f.text, "kind": f.kind, "subject": f.subject,
           "counterpart": f.counterpart or None, "valid_from": f.valid_from or None,
           "valid_from_precision": f.valid_from_precision or None,
           "cites": [{"row": c.row, "quote": c.quote} for c in f.cites]}
    if f.subject_kind != "offered":
        out["subject_kind"] = f.subject_kind
    if f.split_from:
        out["split_from"] = f.split_from
    return out


@dataclass(frozen=True)
class ParsedRepairs:
    """The repair round's reply: per row id, ``withdraw``, a fixed row, or
    (a fact row only) a SPLIT into several rows."""

    withdraw: frozenset[str] = frozenset()
    fixed: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Entries naming a row that was not failed / not shown, or malformed.
    refused: tuple[tuple[str, str], ...] = ()
    split: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)


def parse_repairs(obj: Mapping[str, Any] | None, shown: frozenset[str]) -> ParsedRepairs:
    """``{"repairs": [{"id": "F3", "action": "fix"|"withdraw"|"split", "row":
    {...}, "rows": [{...}, ...]}]}``. Only rows that were SHOWN (failed rows)
    may be repaired; anything else is refused and named
    (``not_a_failed_row``) — the round cannot add a row, except that a failed
    FACT row may be SPLIT into 2..:data:`MAX_SPLIT_ROWS` rows (one claim each,
    each with its own quotes; the rule is in :mod:`desk_rows_gates`)."""
    if not isinstance(obj, Mapping) or not isinstance(obj.get("repairs"), list):
        return ParsedRepairs(refused=(("*", "reply_unparseable"),))
    withdraw: set[str] = set()
    fixed: dict[str, Mapping[str, Any]] = {}
    split: dict[str, tuple[Mapping[str, Any], ...]] = {}
    refused: list[tuple[str, str]] = []
    for e in obj["repairs"]:
        if not isinstance(e, Mapping):
            refused.append(("?", "repair_not_an_object"))
            continue
        rid = as_text(e.get("id")).upper()
        action = as_text(e.get("action")).lower()
        if rid not in shown:
            refused.append((rid or "?", "not_a_failed_row"))
        elif action == "withdraw":
            withdraw.add(rid)
        elif action == "fix" and isinstance(e.get("row"), Mapping):
            fixed[rid] = e["row"]
        elif action == "split" and rid.startswith("F") and isinstance(e.get("rows"), list):
            parts = tuple(r for r in e["rows"] if isinstance(r, Mapping))
            if 2 <= len(parts) <= MAX_SPLIT_ROWS and len(parts) == len(e["rows"]):
                split[rid] = parts
            else:
                refused.append((rid, "split_shape"))
        else:
            refused.append((rid, "action_unknown"))
    return ParsedRepairs(frozenset(withdraw), fixed, tuple(refused), split)


__all__ = [
    "CONFIDENCE_WORDS",
    "CONTRACT_VERSION",
    "DESK_COLLECTS",
    "FACT_KINDS",
    "MAX_ROWS_PER_KIND",
    "MAX_SPLIT_ROWS",
    "NO_LEVEL",
    "RESTATEMENT_STATUSES",
    "SEVERITY_DELTAS",
    "SUBJECT_KINDS",
    "AbsenceRow",
    "AssessmentRow",
    "Cite",
    "FactRow",
    "ParsedRepairs",
    "Restatement",
    "RowsReply",
    "WouldChange",
    "fact_as_dict",
    "normalize_confidence",
    "severity_levels",
    "parse_repairs",
    "parse_rows_reply",
    "reply_as_dict",
]
