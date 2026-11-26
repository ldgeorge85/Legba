# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typing a desk sentence PIECE BY PIECE — one seat call per sentence, one
draft and one gate report per piece.

The sentence is split by code first (:func:`statement_split.split_sentence`).
The seat is shown the sentence WITH its ``[N]`` markers (``[N]`` is ROW N),
the pieces code made, the offered subjects, the cited rows and the standing
candidates, and returns one typed entry per piece. When code did not split
the sentence the seat may propose a split itself — piece texts and their
character offsets into the sentence — which code accepts only through
:func:`statement_split.verify_model_split` (verbatim, in order, concatenating
back to the sentence). The model never rewrites a word.

Each piece is then assembled and gated exactly as a whole sentence is
(:func:`statement_typing.assemble`) over ONLY the rows its own citations name:
a quote from a row the piece does not cite is ``row_not_on_sentence``. A piece
with no attached citation is not typed and says so (``uncited``, with the
figures it states).

* ACTORS. Each piece is offered, besides the desk's subjects, the actors the
  entity layer links to the rows THAT PIECE cites and that the piece names
  (:func:`statement_actor_subjects.actor_candidates`). One numbered list is
  shown; each piece says which ids it may take; a subject outside its own
  list is ``subject_not_offered``.
* NARROWING. When a piece says more than its quote supports, the seat may
  narrow it to the part the quote supports — offsets into the desk's own
  sentence, accepted only through :func:`statement_split.verify_narrowing`
  (verbatim, clause-aligned, dropping no negation / attribution / condition
  that scopes over the kept words) — and the narrowed text is what is gated
  and verified. The dropped remainder is recorded, never typed as supported.
* OUTCOMES. Every piece ends as exactly one of :data:`OUTCOMES`:
  ``established_eligible`` (a checkable statement that passed every code gate
  — the only outcome the verifier sees), ``assessment`` (the desk's judgement,
  resting only on other typed pieces as premises), ``absence`` (a statement
  about the handed COLLECTION, scoped in code), ``restatement`` (of a prior
  read or a standing statement — linked, no new piece), or ``failed``. S-6
  adds ``coverage``: what the cited reporting shows AS reporting — how many
  outlets carried it, how it was framed — scoped to the handed slice and
  counted against the rows it cites, in code.

S-6.

* TAILS IN CODE. After the deterministic split,
  :func:`statement_split.split_tails` splits a judgement tail (a contrastive
  appositive, an inference clause) or an absence tail off a statement piece,
  and types text after the last citation that carries no figure, IN CODE:
  an ``assessment`` piece resting on the piece before it as its premise, or
  an ``absence`` piece. The seat is shown these pieces marked code-typed and
  does not type them; a sentence whose pieces all need no seat costs no call.
* THINKING. :func:`type_pieces` takes ``thinking`` and passes it to the
  model call (``None`` sends nothing new: the call is byte-identical).

No I/O beyond the injected ``llm``; no table.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .statement_actor_subjects import actor_candidates
from .statement_seats import TYPING_SEAT_ID, CallBudget, SeatCall, SeatLLM, call_seat, parse_reply_object
from .statement_sentences import CitedSentence
from .statement_shapes import HandedSlice, StatementKind, SupportKind, SupportRole, SupportRow
from .statement_split import Piece, split_sentence, split_tails, verify_model_split, verify_narrowing
from .statement_typing import (
    NON_EVIDENCE_KINDS,
    TypingInput,
    TypingResult,
    assemble,
    evidence_window,
)

#: Piece-level outcomes that are not a typing result.
UNCITED = "uncited"
NO_EVIDENCE_REF = "no_evidence_ref"
PIECE_NOT_TYPED = "piece_not_typed"
#: Cited rows shown for a sentence typed piece by piece (each piece cites its own).
PIECE_MAX_ROWS = 6

#: The five outcomes a piece ends as, counted apart in the tally.
ESTABLISHED_ELIGIBLE = "established_eligible"
ASSESSMENT = "assessment"
ABSENCE = "absence"
COVERAGE = "coverage"
RESTATEMENT = "restatement"
FAILED = "failed"
OUTCOMES = (ESTABLISHED_ELIGIBLE, ASSESSMENT, ABSENCE, COVERAGE, RESTATEMENT, FAILED)

#: Non-evidence citation kinds that are the desk's OWN prior record: a piece
#: citing only these restates it (linked by the cited ref; no new piece).
#: ``situation_register`` and ``open_questions`` are not prior conclusions.
RESTATABLE_REF_KINDS = frozenset({"prior_read", "window_ledger", "desk_baseline"})


@dataclass(frozen=True)
class PieceResult:
    piece: Piece
    result: TypingResult | None
    reason: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    #: The narrowed piece that was typed, when the seat narrowed it.
    narrowed: Piece | None = None

    @property
    def passed(self) -> bool:
        """Every code gate passed for an ESTABLISHED-ELIGIBLE statement (an
        assessment or absence that passed its own gates is not "passed" here:
        it is never established by a quote)."""
        return self.outcome == ESTABLISHED_ELIGIBLE

    @property
    def gates_passed(self) -> bool:
        return self.result is not None and self.result.passed

    @property
    def typed_text(self) -> str:
        return (self.narrowed or self.piece).plain

    @property
    def outcome(self) -> str:
        if self.reason == RESTATEMENT:
            return RESTATEMENT
        r = self.result
        if r is None:
            return FAILED
        if r.relation == "restates" and r.relation_candidate and r.draft is not None:
            return RESTATEMENT
        if not r.passed or r.draft is None:
            return FAILED
        kind = str(r.draft.kind)
        if kind == StatementKind.ASSESSMENT.value:
            return ASSESSMENT
        if kind == StatementKind.ABSENCE.value:
            return ABSENCE
        if kind == StatementKind.COVERAGE.value:
            return COVERAGE
        return ESTABLISHED_ELIGIBLE

    def why(self) -> list[str]:
        """Plain reasons this piece is not a supported statement."""
        if self.reason:
            return [self.reason]
        if self.result is None:
            return [PIECE_NOT_TYPED]
        out = list(self.result.refusals)
        if self.result.gate is not None:
            out += [f"{c.gate}.{c.check}:{c.reason}" for c in self.result.gate.failures]
        return out


@dataclass(frozen=True)
class SentenceTyping:
    sentence: CitedSentence
    pieces: tuple[PieceResult, ...]
    split_by: str
    refusals: tuple[str, ...] = ()
    call: SeatCall | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def any_supported(self) -> bool:
        return any(p.passed for p in self.pieces)


@dataclass(frozen=True)
class SubjectOffer:
    """The subjects shown for one sentence: the desk's own first, then the
    actors any piece may take; ``allowed[i]`` are the 1-based ids piece ``i``
    may take."""

    item: TypingInput
    allowed: Mapping[int, frozenset[int]]
    sentence_allowed: frozenset[int]
    detail: Mapping[str, Any] = field(default_factory=dict)


def _piece_item(item: TypingInput, piece: Piece, typed: Piece | None = None) -> TypingInput:
    wanted = set(piece.ordinals)
    refs = tuple(r for r in item.sentence.refs if r.ordinal in wanted)
    t = typed or piece
    sentence = CitedSentence(item.sentence.index, t.text, t.plain, refs,
                             tuple(n for n in item.sentence.unresolved if n in wanted))
    evidence = tuple(e for e in item.evidence if e.ordinal in wanted)
    return replace(item, sentence=sentence, evidence=evidence)


def offer_subjects(item: TypingInput, pieces: Sequence[Piece]) -> SubjectOffer:
    """Code chooses the subjects: the desk's (``item.subjects``) for every
    piece, plus each piece's actor candidates from the entities linked to
    the rows it cites. Pure."""
    base = list(item.subjects)
    desk_ids = frozenset(range(1, len(base) + 1))
    if not item.linked_entities:
        allowed = {p.index: desk_ids for p in pieces}
        return SubjectOffer(item, allowed, desk_ids, {"actors_offered": 0})
    subjects = list(base)
    index_of = {(s.kind, s.key): i + 1 for i, s in enumerate(subjects)}
    allowed: dict[int, frozenset[int]] = {}
    declined: dict[str, int] = {}
    for p in pieces:
        ev = tuple(e for e in item.evidence if e.ordinal in set(p.ordinals))
        offer = actor_candidates(p.plain, ev, item.linked_entities, offered=base)
        for reason, n in offer.declined_counts().items():
            declined[reason] = declined.get(reason, 0) + n
        ids = set(desk_ids)
        for cand in offer.offered:
            k = (cand.kind, cand.key)
            if k not in index_of:
                subjects.append(cand)
                index_of[k] = len(subjects)
            ids.add(index_of[k])
        allowed[p.index] = frozenset(ids)
    sentence_allowed = frozenset(i for ids in allowed.values() for i in ids)
    detail = {"actors_offered": len(subjects) - len(base), "actors_declined": declined}
    return SubjectOffer(replace(item, subjects=tuple(subjects)), allowed, sentence_allowed, detail)


def _ids(ids: Iterable[int]) -> str:
    return ", ".join(f"S{i}" for i in sorted(ids))


def build_piece_prompt(item: TypingInput, pieces: Sequence[Piece],
                       allowed: Mapping[int, frozenset[int]] | None = None) -> str:
    """The one user message for a sentence typed piece by piece."""
    lines = ["SENTENCE (each [N] marker cites ROW N):", item.sentence.text, ""]
    if len(pieces) > 1:
        lines.append("PIECES (code split the sentence; type EACH piece not marked code-typed; "
                     "do not change them):")
        for p in pieces:
            if p.code_kind:
                lines.append(f"  P{p.index + 1} · code-typed {p.code_kind} (do not type it) · {p.plain}")
                continue
            rows = ", ".join(str(o) for o in p.ordinals) or "none"
            subj = f" · subjects {_ids(allowed[p.index])}" if allowed and p.index in allowed else ""
            lines.append(f"  P{p.index + 1} · rows {rows}{subj} · {p.plain}")
    else:
        lines += ["PIECES: the whole sentence is one piece (P1). If it makes SEVERAL separate",
                  "claims (several figures, or several distinct assertions), you may split it:",
                  "give each piece's text copied EXACTLY from SENTENCE (code finds where it",
                  "sits). Otherwise return one piece."]
        if allowed and pieces and pieces[0].index in allowed and len(allowed[pieces[0].index]) < len(item.subjects):
            lines.append(f"  P1 · subjects {_ids(allowed[pieces[0].index])}")
    lines += ["", "SUBJECTS (answer with one id offered for the piece, or null):"]
    for i, s in enumerate(item.subjects, start=1):
        lines.append(f"  S{i} · {s.label()} · {s.display()}")
    lines += ["", "CITED ROWS (quote ONLY from these texts, copying the words exactly):"]
    for ev in item.evidence[:PIECE_MAX_ROWS]:
        pub = ev.published_at.date().isoformat() if ev.published_at else "unknown"
        lines.append(f"  ROW {ev.ordinal} · {ev.ref_kind} · published {pub}"
                     + (f" · title: {ev.title}" if ev.title else ""))
        lines.append("  TEXT: " + evidence_window(ev.text, item.sentence.plain).replace("\n", " "))
    lines += ["", "STANDING STATEMENTS ON THESE SUBJECTS (may be empty):"]
    if not item.standing:
        lines.append("  (none)")
    for i, c in enumerate(item.standing, start=1):
        lines.append(f"  C{i} · {c.kind} · {c.text}")
    return "\n".join(lines)


def _entries(reply: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = reply.get("pieces")
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, Mapping)]
    return [reply]


def _by_piece(entries: Sequence[Mapping[str, Any]], n: int) -> dict[int, Mapping[str, Any]]:
    out: dict[int, Mapping[str, Any]] = {}
    for pos, e in enumerate(entries):
        raw = str(e.get("piece") or "").strip().upper().lstrip("P")
        idx = int(raw) - 1 if raw.isdigit() else pos
        if 0 <= idx < n and idx not in out:
            out[idx] = e
    return out


def _restates(item: TypingInput, piece: Piece) -> list[dict[str, Any]]:
    """The prior-record refs a piece cites when it cites no evidence row."""
    wanted = set(piece.ordinals)
    return [{"ordinal": r.ordinal, "ref_kind": r.ref_kind, "ref_id": r.ref_id}
            for r in item.sentence.refs
            if r.ordinal in wanted and not r.evidence_eligible and r.ref_kind in RESTATABLE_REF_KINDS]


def premise_ref(sentence_index: int, piece_index: int) -> str:
    """The ref id a premise row carries for another piece of the same
    sentence (storage-agnostic; a store maps it to the piece's statement)."""
    return f"piece:{sentence_index}:{piece_index + 1}"


def _premise_ids(raw: Any) -> list[int] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    out = []
    for x in raw:
        m = re.fullmatch(r"[Pp]?(\d+)", str(x).strip())
        if not m:
            return None
        out.append(int(m.group(1)) - 1)
    return out


def assemble_pieces(
    item: TypingInput,
    reply: Mapping[str, Any] | None,
    *,
    handed: HandedSlice,
    pieces: Sequence[Piece] | None = None,
    allow_publication_date: bool = True,
    offer: SubjectOffer | None = None,
) -> SentenceTyping:
    """Turn one reply into per-piece drafts and gate reports. Pure."""
    pieces = tuple(pieces if pieces is not None else code_pieces(item.sentence.text))
    offer = offer or offer_subjects(item, pieces)
    item = offer.item
    split_by = pieces[0].split_by if len(pieces) > 1 else "none"
    refusals: list[str] = []
    detail: dict[str, Any] = {**offer.detail}
    if reply is None:
        return SentenceTyping(item.sentence, tuple(PieceResult(p, None, PIECE_NOT_TYPED) for p in pieces),
                              split_by, ("reply_unparseable",), detail=detail)
    entries = _entries(reply)
    allowed = dict(offer.allowed)
    if len(pieces) == 1 and len(entries) > 1:
        proposed, reason, counts = verify_model_split(item.sentence.text, entries)
        detail["model_split_offsets_corrected"] = counts["offsets_corrected"]
        if proposed is None:
            refusals.append(f"split_rejected:{reason}")
            return SentenceTyping(item.sentence, (PieceResult(pieces[0], None, PIECE_NOT_TYPED),),
                                  split_by, tuple(refusals), detail=detail)
        pieces, split_by = proposed, "model"
        mapped = dict(enumerate(entries))
        allowed = {p.index: offer.sentence_allowed for p in pieces}
    else:
        mapped = _by_piece(entries, len(pieces))

    out: dict[int, PieceResult] = {}
    later: list[tuple[Piece, Mapping[str, Any], TypingInput, Piece | None, dict[str, Any]]] = []
    for p in pieces:
        if p.code_kind:
            # S-6: a tail code split off and typed; any seat entry is ignored
            entry = {"kind": p.code_kind, "subject": None, "support": [],
                     "premises": [f"P{p.rests_on + 1}"] if p.rests_on is not None else [],
                     "relation": {"action": "new", "candidate": None}}
            cdetail: dict[str, Any] = {"typed_by": p.split_by}
            csub = _piece_item(item, p)
            if p.code_kind == StatementKind.ASSESSMENT.value:
                later.append((p, entry, csub, None, cdetail))
            else:
                res = assemble(csub, entry, handed=handed, allow_publication_date=allow_publication_date,
                               allowed_subjects=allowed.get(p.index))
                out[p.index] = PieceResult(p, res, None, cdetail)
            continue
        if p.uncited:
            out[p.index] = PieceResult(p, None, UNCITED, {"figures": list(p.figures)})
            continue
        sub = _piece_item(item, p)
        if not sub.evidence:
            restates = _restates(item, p)
            if restates:
                out[p.index] = PieceResult(p, None, RESTATEMENT, {"restates": restates})
            else:
                out[p.index] = PieceResult(p, None, NO_EVIDENCE_REF, {"ordinals": list(p.ordinals)})
            continue
        entry = mapped.get(p.index)
        if entry is None:
            out[p.index] = PieceResult(p, None, PIECE_NOT_TYPED)
            continue
        pdetail: dict[str, Any] = {}
        narrowed = None
        if entry.get("narrow") and str(entry.get("kind") or "") not in NON_EVIDENCE_KINDS:
            narrowed, why, ndetail = verify_narrowing(item.sentence.text, p, entry.get("narrow"))
            pdetail["narrow"] = {**ndetail, **({"refused": why} if why else {})}
            if narrowed is not None:
                sub = _piece_item(item, p, narrowed)
        if str(entry.get("kind") or "").strip().lower() == StatementKind.ASSESSMENT.value:
            later.append((p, entry, sub, narrowed, pdetail))
            continue
        if str(entry.get("kind") or "").strip().lower() == StatementKind.COVERAGE.value:
            # S-6: a count spans the SENTENCE ("three reports — A [1][2] and
            # B [3]"): a coverage piece counts every row the sentence cites
            sub = replace(sub, evidence=item.evidence)
            pdetail["counted"] = "sentence_rows"
        res = assemble(sub, entry, handed=handed, allow_publication_date=allow_publication_date,
                       allowed_subjects=allowed.get(p.index))
        out[p.index] = PieceResult(p, res, None, pdetail, narrowed)

    # assessments last: their premises are other pieces typed above
    for p, entry, sub, narrowed, pdetail in later:
        ids = _premise_ids(entry.get("premises"))
        premises: list[SupportRow] = []
        bad = ids is None
        for j in ids or ():
            other = out.get(j)
            if j == p.index or other is None or other.result is None or other.result.draft is None:
                if p.code_kind:
                    # a code tail whose head did not type rests on nothing:
                    # the unsupported judgement it is, and it says so
                    pdetail["premise_unavailable"] = True
                    continue
                bad = True
                continue
            premises.append(SupportRow(SupportRole.PREMISE.value, SupportKind.STATEMENT.value,
                                       premise_ref(item.sentence.index, j),
                                       ref_statement_kind=str(other.result.draft.kind)))
        res = assemble(sub, entry, handed=handed, allow_publication_date=allow_publication_date,
                       allowed_subjects=allowed.get(p.index), premises=premises)
        if bad:
            res = replace(res, refusals=(*res.refusals, "premise_not_a_typed_piece"))
        out[p.index] = PieceResult(p, res, None, pdetail, narrowed)
    return SentenceTyping(item.sentence, tuple(out[p.index] for p in pieces), split_by,
                          tuple(refusals), detail=detail)


def code_pieces(text: str) -> tuple[Piece, ...]:
    """The pieces code makes of one marked sentence: the deterministic split,
    then the judgement / absence tails (S-6)."""
    return split_tails(text, split_sentence(text))


def needs_seat(pieces: Sequence[Piece], item: TypingInput) -> bool:
    """Does any piece need the seat? Not when every piece is code-typed,
    uncited, or cites no evidence row (a restatement): then no call is
    spent."""
    return any(not p.code_kind and not p.uncited and _piece_item(item, p).evidence for p in pieces)


async def type_pieces(
    llm: SeatLLM,
    item: TypingInput,
    *,
    system_prompt: str,
    handed: HandedSlice,
    budget: CallBudget | None = None,
    thinking: bool | None = None,
) -> SentenceTyping:
    """One sentence through the seat piece by piece: at most ONE model call
    (none when code typed every piece — :func:`needs_seat`)."""
    pieces = code_pieces(item.sentence.text)
    offer = offer_subjects(item, pieces)
    if not needs_seat(pieces, item):
        out = assemble_pieces(item, {"pieces": []}, handed=handed, pieces=pieces, offer=offer)
        return SentenceTyping(out.sentence, out.pieces, out.split_by, out.refusals, None,
                              {**out.detail, "seat_called": False})
    content, call = await call_seat(
        llm, seat=TYPING_SEAT_ID, system_prompt=system_prompt,
        user_content=build_piece_prompt(offer.item, pieces, offer.allowed), budget=budget,
        thinking=thinking,
    )
    if content is None:
        return SentenceTyping(item.sentence, tuple(PieceResult(p, None, PIECE_NOT_TYPED) for p in pieces),
                              pieces[0].split_by if len(pieces) > 1 else "none", ("no_call",), call,
                              dict(offer.detail))
    out = assemble_pieces(item, parse_reply_object(content), handed=handed, pieces=pieces, offer=offer)
    return SentenceTyping(out.sentence, out.pieces, out.split_by, out.refusals, call, out.detail)


def _count(d: dict[str, int], key: str, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


def tally_pieces(results: Iterable[SentenceTyping]) -> dict[str, Any]:
    """Counts only: sentences, pieces, how they were split, each piece's
    OUTCOME (established-eligible / assessment / absence / restatement /
    failed — counted apart), the plain reasons the failed ones failed, and
    the actor detail (actor subjects offered / taken / declined, narrowings,
    which text quotes were found in)."""
    results = list(results)
    reasons: dict[str, int] = {}
    outcomes: dict[str, int] = dict.fromkeys(OUTCOMES, 0)
    split: dict[str, int] = {}
    subject_kinds: dict[str, int] = {}
    declined: dict[str, int] = {}
    narrow: dict[str, int] = {}
    code_typed: dict[str, int] = {}
    found_in: dict[str, int] = {}
    pieces = actors_offered = 0
    for r in results:
        _count(split, r.split_by)
        actors_offered += int(r.detail.get("actors_offered", 0) or 0)
        for k, n in (r.detail.get("actors_declined") or {}).items():
            _count(declined, k, n)
        for ref in r.refusals:
            _count(reasons, ref)
        for p in r.pieces:
            pieces += 1
            oc = p.outcome
            _count(outcomes, oc)
            nd = p.detail.get("narrow") if p.detail else None
            if p.piece.code_kind:
                _count(code_typed, f"{p.piece.split_by}:{p.piece.code_kind}:{oc}")
            if nd:
                _count(narrow, "refused:" + nd["refused"] if nd.get("refused") else "accepted")
            if oc == FAILED:
                for why in dict.fromkeys(p.why()):
                    _count(reasons, why)
            if oc == ESTABLISHED_ELIGIBLE and p.result and p.result.draft:
                _count(subject_kinds, str(p.result.draft.subject_kind))
                for s in p.result.support:
                    _count(found_in, getattr(s, "quote_found_in", None) or "not_computed")
    return {
        "sentences": len(results),
        "sentences_with_a_supported_piece": sum(1 for r in results if r.any_supported),
        "pieces": pieces,
        "pieces_passed": outcomes[ESTABLISHED_ELIGIBLE],
        "outcomes": outcomes,
        "split_by": split,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "established_eligible_subject_kinds": subject_kinds,
        "actors": {"offered": actors_offered, "declined": declined},
        "narrowing": narrow,
        "code_typed": code_typed,
        "quote_found_in": found_in,
    }


__all__ = [
    "ABSENCE",
    "ASSESSMENT",
    "COVERAGE",
    "ESTABLISHED_ELIGIBLE",
    "FAILED",
    "NO_EVIDENCE_REF",
    "OUTCOMES",
    "PIECE_NOT_TYPED",
    "RESTATABLE_REF_KINDS",
    "RESTATEMENT",
    "UNCITED",
    "PieceResult",
    "SentenceTyping",
    "SubjectOffer",
    "assemble_pieces",
    "build_piece_prompt",
    "code_pieces",
    "needs_seat",
    "offer_subjects",
    "premise_ref",
    "tally_pieces",
    "type_pieces",
]
