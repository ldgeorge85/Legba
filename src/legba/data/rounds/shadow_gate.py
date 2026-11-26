# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The round's SHADOW statement gate (code seat ``statement_gate_shadow``),
on the S-5 honesty gates (``statement_gate.v3``).

For each sentence of the ruling the round would propose:

1. **Split** by code (:func:`legba.data.statement_split.split_sentence`): a
   sentence carrying two claims is two pieces, each gated on its own.
2. **Subject**: the pieces are offered, in order, the desk's own subject
   (:func:`resolve_subject_names` over the profile), the countries a thematic
   desk spans (:func:`related_subjects`), and the ACTORS the entity layer
   links to the rows the sentence cites
   (:func:`statement_actor_subjects.actor_candidates` — publishers, places
   named in passing, unnamed entities declined with their reasons). A piece
   takes the first offered subject it NAMES; none named → the desk's.
3. **Outcome** — exactly one per piece, as S-5 defines them:
   ``established_eligible`` (a checkable piece that passed every code gate),
   ``absence`` (no evidence quote, states an absence, scoped to the handed
   collection: G8 ``absence_scope``), ``assessment`` (no evidence quote: the
   seat's judgement; passes as an unsupported judgement and says so), or
   ``failed``. ``restatement`` needs the standing statements, which a harness
   run does not read: it is never assigned here (``restatement_checked:
   false`` on every proposal).

Shadow only: nothing here writes, and nothing is established. Pure (the
caller fetches rows and links read-only).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from ..statement_actor_subjects import LinkedEntity, actor_candidates
from ..statement_gate import (
    absence_stated,
    evaluate_absence_gates,
    evaluate_assessment_gates,
    evaluate_code_gates,
)
from ..statement_shapes import EvidenceTexts, HandedSlice, StatementDraft, SupportRow
from ..statement_split import split_sentence
from ..statement_subject_names import name_in_text, related_subjects, resolve_subject_names
from ..statement_typing import EvidenceRef, SubjectCandidate

_QUOTED = ("evidence", "contradicts")


def _dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def offered_subjects(subject: str, **kw: Any) -> list[SubjectCandidate]:
    """The desk's own subject, then the countries it spans."""
    out = [SubjectCandidate("target", subject, resolve_subject_names("target", subject, **kw))]
    for kind, key in related_subjects(subject, **{k: v for k, v in kw.items()
                                                 if k in ("subjects", "gazetteer")}):
        out.append(SubjectCandidate(kind, key, resolve_subject_names(kind, key, **kw)))
    return out


def _named(cand: SubjectCandidate, text: str) -> bool:
    names = cand.names
    excluded = getattr(names, "excluded", ())
    return any(name_in_text(n, text, excluded=excluded) for n in names)


def gate_sentence(
    sentence: Mapping[str, Any], *, subject: str, offered: Sequence[SubjectCandidate],
    rows: Mapping[str, Mapping[str, Any]], linked: Iterable[LinkedEntity], handed: HandedSlice,
    verifier: str | None = None, actor_kw: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The shadow proposals for one ruling sentence (one per piece)."""
    text = str(sentence.get("text", ""))
    support_raw = [r for r in sentence.get("support") or () if isinstance(r, Mapping)]
    role_map = {"context": "background"}
    support = [SupportRow(role=role_map.get(str(r.get("role", "evidence")), str(r.get("role", "evidence"))),
                          support_kind=str(r.get("kind", "signal")), ref_id=str(r.get("ref_id")),
                          quote=r.get("quote"),
                          source_id=(rows.get(str(r.get("ref_id"))) or {}).get("source_id"),
                          access_class=(rows.get(str(r.get("ref_id"))) or {}).get("access_class"),
                          title=(rows.get(str(r.get("ref_id"))) or {}).get("title"),
                          published_at=_dt((rows.get(str(r.get("ref_id"))) or {}).get("published_at")))
               for r in support_raw]
    evidence = [s for s in support if s.role in _QUOTED]
    texts = EvidenceTexts(by_signal={i: " ".join(str(t.get("text") or "").split())
                                     for i, t in rows.items()})
    refs = [EvidenceRef(i, "signal", s.ref_id, texts.by_signal.get(s.ref_id, ""),
                        published_at=s.published_at, title=s.title, source_id=s.source_id,
                        source_name=(rows.get(s.ref_id) or {}).get("source_name"))
            for i, s in enumerate(evidence) if s.ref_id in rows]
    out: list[dict[str, Any]] = []
    linked = list(linked)
    for k, piece in enumerate(split_sentence(text)):
        ptext = (piece.plain or piece.text).strip()
        actors = actor_candidates(ptext, refs, linked, offered=offered, **dict(actor_kw or {}))
        cands = list(offered) + list(actors.offered)
        quoted = " ".join(f"{s.quote or ''} {s.title or ''}" for s in evidence)
        named = [c for c in cands if _named(c, ptext)]
        # the subject the piece names AND its sources name; else the first it names
        chosen = next((c for c in named if _named(c, quoted)), named[0] if named else offered[0])
        gate: dict[str, Any] = {"shadow": True, "verifier": verifier, "restatement_checked": False,
                                "subject": {"kind": chosen.kind, "key": chosen.key,
                                            "named": _named(chosen, ptext)},
                                "actors_offered": [c.key for c in actors.offered],
                                "actors_declined": [list(d) for d in actors.declined][:12]}
        if evidence:
            report = evaluate_code_gates(
                StatementDraft(text=ptext, kind="event", subject_kind=chosen.kind,
                               subject_key=chosen.key),
                support, texts=texts, subject_names=chosen.names, handed=handed)
            gate.update(outcome="established_eligible" if report.passed else "failed",
                        passed=report.passed, support_word=report.support_word,
                        independent_groups=report.independent_groups)
        elif absence_stated(ptext):
            report = evaluate_absence_gates(
                StatementDraft(text=ptext, kind="absence", subject_kind=chosen.kind,
                               subject_key=chosen.key),
                handed=handed, cited=[s.ref_id for s in support])
            gate.update(outcome="absence" if report.passed else "failed", passed=report.passed)
        else:
            report = evaluate_assessment_gates(
                StatementDraft(text=ptext, kind="assessment", subject_kind=chosen.kind,
                               subject_key=chosen.key), ())
            gate.update(outcome="assessment" if report.passed else "failed", passed=report.passed,
                        premises=0)
        gate["failed"] = [c.as_dict() for c in report.failures][:12]
        out.append({"text": ptext, "support": [dict(r) for r in support_raw],
                    "gate": {**gate, "sentence": sentence.get("key"), "piece": k,
                             "split_by": getattr(piece, "split_by", None)}})
    return out


def handed_slice(ids: Iterable[str], run_id: str, *, subject: str, as_of: datetime,
                 window_hours: int, source_ids: Iterable[str] = ()) -> HandedSlice:
    """The slice the round was handed, with its scope (an absence needs it)."""
    return HandedSlice.of(ids, run_id, collection=f"desk_slice:{subject}", source_ids=source_ids,
                          window_start=as_of - timedelta(hours=window_hours), window_end=as_of)


__all__ = ["gate_sentence", "handed_slice", "offered_subjects"]
