# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held family (data-model lane 10, DM-C): what one desk shadow record
would put in the record — the rules, as a pure plan.

The tables are migration ``20261115T1940_held_record`` (``assertions``,
``assertion_versions``, ``assertion_status_log``, ``assertion_status``,
``assertion_support``, ``assertion_shadow_receipts``). This module reads ONE
contract-v2 shadow record (``legba.data.desk_rows_shadow``: its ``outcomes``,
each with the gates' outcome and, for a fact row, the verifier's verdict) and
says what to write. No I/O; :mod:`held_record_store` writes the plan.

THE CLASS RULES (each row ends as exactly one disposition, counted):

* a FACT row the gates passed (``established_eligible``) becomes an assertion
  of the type its kind declares (event / state / level → class
  ``statement``; attributed → class ``attributed``). It is HELD only when the
  clean-context verifier answered ``supports``. ``partly``,
  ``does_not_support``, ``unclear`` or no verdict: written NOT HELD, with the
  verdict as the reason — never held. Its evidence rows are the cited signals
  with their exact quotes (the receipt version is pinned at write), plus one
  ``verdict`` row naming the verdict.
* an ASSESSMENT row the gates passed is HELD only when every premise it rests
  on (``premises_passed``) is a fact row of the same record that is held;
  otherwise NOT HELD (``premise_not_held``). Its premises are ``premise``
  links to those statements' versions. An assessment is never "established":
  held means "the record keeps this judgement, resting on held statements".
* an ABSENCE row the gates passed is HELD (a record about the collection: the
  topic, the sources and the window, which is its period).
* a RESTATEMENT row is a LINK, never a record: the shadow row → the desk's
  prior read it restates (resolved through what the run was handed,
  ``run_handed``), role ``restates``. Unresolvable: counted, not written.
* failed and withdrawn rows are not written (they never passed the gates);
  counted by kind.

THE MATCHING RULE (stated; conservative on purpose). Two records are the same
claim when their CLAIM KEY is equal: sha256 over the type id, the subject key
(normalised) and the type's declared key fields (``assertion_type`` family:
``text`` for statements, attributed and assessments — the claim text under
the house fold, markers stripped, trailing punctuation dropped; ``topic`` +
``collection`` for an absence). The type carries the class, so class is in the
key. On a match:

* a statement / attributed / assessment (key = its text): NO new assertion and
  NO new version (the text is identical by construction) — a ``restates`` link
  from the new shadow row to the held record's current version. If the held
  record is NOT HELD and the new row would be held, a ``held`` status row is
  appended on the current version (``restated_and_verified``) with the new
  row's evidence and verdict attached to that version.
* an absence (key = topic + collection): a NEW VERSION (``later_window``)
  when the window differs — the period moved — with a ``held`` status row; a
  ``restates`` link when the window is the same.

Within one record a second row with the same key is matched to the first the
same way. Contradiction detection is NOT here (SEAMS #held-record).
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .provenance.text_fold import normalize_for_match
from .statement_content import strip_markers

__all__ = [
    "CLASS_OF_ROW_KIND",
    "HELD",
    "NOT_HELD",
    "PlannedLink",
    "PlannedRecord",
    "RULE_VERSION",
    "ShadowPlan",
    "STATUS_REASONS",
    "STATUSES",
    "Subject",
    "VERSION_REASONS",
    "WRITER_HANDLE",
    "claim_key",
    "normalise_claim",
    "normalise_subject_key",
    "plan_shadow_record",
    "type_map",
]

#: Stamped on every receipt: the writer's rules (bump when a rule changes).
RULE_VERSION = "held_record.r1"
#: The principal that writes the rows and decides the status (code: the gates
#: ran, the verifier answered, this rule decided).
WRITER_HANDLE = "rule:held_record_writer"

HELD = "held"
NOT_HELD = "not_held"
CONTESTED = "contested"
SUPERSEDED = "superseded"
WITHDRAWN = "withdrawn"
STATUSES: tuple[str, ...] = (HELD, NOT_HELD, CONTESTED, SUPERSEDED, WITHDRAWN)

#: Why a version exists (closed; ``assertion_versions.reason_code``).
VERSION_REASONS: tuple[str, ...] = ("first", "later_window")

#: Why a status row was written (closed; ``assertion_status_log.reason_code``).
STATUS_REASONS: Mapping[str, str] = {
    "verified_supports": "the gates passed and the clean-context verifier answered 'supports'",
    "verifier_partly": "the gates passed; the verifier answered 'partly' (not held)",
    "verifier_does_not_support": "the gates passed; the verifier answered 'does not support' (not held)",
    "verifier_unclear": "the gates passed; the verifier could not decide (not held)",
    "not_verified": "the gates passed; no verifier verdict was recorded (not held)",
    "premises_held": "an assessment whose every premise is a held statement",
    "premise_not_held": "an assessment resting on a statement that is not held",
    "absence_scoped": "an absence, scoped to the collection's sources and window",
    "restated_and_verified": "a record not held, restated by a later row that is held",
    "later_window": "an absence re-scoped to a later window (a new version)",
}

_VERDICT_REASON = {"supports": "verified_supports", "partly": "verifier_partly",
                   "does_not_support": "verifier_does_not_support", "unclear": "verifier_unclear"}
_ELIGIBLE = "established_eligible"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TRAILING = re.compile(r"[\s.;:!?,]+$")


def type_map() -> dict[str, Any]:
    """``{desk row kind: assertion_type declaration}``, DERIVED from the
    family (two types claiming one row kind is a registry error here)."""
    from .type_registry import types

    out: dict[str, Any] = {}
    for d in types("assertion_type"):
        for kind in d.attr("desk_row_kinds"):
            if kind in out:
                raise ValueError(f"desk row kind {kind!r} is claimed by {out[kind].id!r} and {d.id!r}")
            out[kind] = d
    return out


#: The class each desk row kind maps to, for readers that need no registry.
CLASS_OF_ROW_KIND: Mapping[str, str] = {
    "event": "statement", "state": "statement", "level": "statement",
    "attributed": "attributed", "assessment": "assessment", "absence": "absence",
}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def normalise_claim(text: Any) -> str:
    """The claim text under the house fold, markers stripped, trailing
    punctuation dropped. Idempotent."""
    return _TRAILING.sub("", normalize_for_match(strip_markers(str(text or ""))))


def normalise_subject_key(kind: str, key: Any) -> str:
    k = str(key or "").strip()
    if kind == "entity":
        return k.lower()
    if kind == "place":
        return k.upper()
    if kind == "surface":
        return normalize_for_match(k)
    return k


def claim_key(type_id: str, subject_key: str, fields: Mapping[str, Any], key: Sequence[str]) -> str:
    """sha256 over the type, the normalised subject key and the type's
    declared key fields, each normalised, length-prefixed (so no two field
    lists collide)."""
    parts = [type_id, subject_key] + [normalise_claim(fields.get(k)) for k in key]
    material = "|".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subject:
    kind: str                     # entity / target / place / surface
    key: str                      # normalised
    text: str | None              # as the row named it
    entity_id: str | None = None


@dataclass
class PlannedRecord:
    """One assertion this record proposes (a new one, or a match)."""

    row_id: str
    row_kind: str
    class_: str
    type_id: str
    subject: Subject
    text: str
    claim_key: str
    status: str
    reason_code: str
    verdict: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_from_precision: str | None = None
    valid_to: datetime | None = None
    speaker_text: str | None = None
    #: ``(signal id, quote, quote_found_in)`` per cited signal.
    evidence: list[tuple[str, str, str | None]] = field(default_factory=list)
    #: Row ids of this record's facts it rests on (an assessment's premises).
    premises: list[str] = field(default_factory=list)
    #: Filled by the matcher: the row id of an earlier record in THIS plan
    #: with the same claim key (the store resolves matches against the table).
    same_as_row: str | None = None


@dataclass(frozen=True)
class PlannedLink:
    """A restatement that is a link, not a record: the shadow row → the prior
    read at ordinal ``ref`` of what the run was handed."""

    row_id: str
    ref: int | None
    status: str | None
    backing: tuple[str, ...]


@dataclass
class ShadowPlan:
    shadow_id: int
    run_id: str | None
    analyst_id: str | None
    target_id: str | None
    occurred_at: Any
    contract_version: str | None
    gate_version: str | None
    records: list[PlannedRecord] = field(default_factory=list)
    links: list[PlannedLink] = field(default_factory=list)
    #: ``{"<row_kind>:<outcome>": n}`` for rows not written, and why.
    not_written: Counter = field(default_factory=Counter)
    #: Rows whose subject or type the writer refused (named reasons).
    refused: Counter = field(default_factory=Counter)

    def counts(self) -> dict[str, Any]:
        by_class: Counter = Counter()
        by_status: Counter = Counter()
        reasons: Counter = Counter()
        for r in self.records:
            by_class[r.class_] += 1
            by_status[f"{r.class_}:{r.status}"] += 1
            if r.status != HELD:
                reasons[r.reason_code] += 1
        return {
            "records": len(self.records), "by_class": dict(by_class), "by_status": dict(by_status),
            "not_held_reasons": dict(reasons), "restatement_links": len(self.links),
            "in_record_matches": sum(1 for r in self.records if r.same_as_row),
            "speaker_found": sum(1 for r in self.records if r.speaker_text),
            "attributed": by_class.get("attributed", 0),
            "not_written": dict(self.not_written), "refused": dict(self.refused),
            "evidence_links": sum(len(r.evidence) for r in self.records),
            "premise_links": sum(len(r.premises) for r in self.records),
        }


def _dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _speaker(text: str) -> str | None:
    """The ONE name the text gives the speaker role ("X said", "according to
    X"), by the gates' own reading (:func:`desk_rows_gates.party_roles`);
    ``None`` when there is none or more than one. Never guessed."""
    from .desk_rows_gates import party_roles

    speakers = [n for n, role in party_roles(text).items() if role == "speaker"]
    return speakers[0] if len(speakers) == 1 else None


def _subject(draft: Mapping[str, Any]) -> Subject | None:
    kind = str(draft.get("subject_kind") or "")
    raw = str(draft.get("subject_key") or "").strip()
    if kind not in ("entity", "target", "place", "surface") or not raw:
        return None
    if kind == "entity" and not _UUID.match(raw.lower()):
        return None
    key = normalise_subject_key(kind, raw)
    return Subject(kind, key, raw, key if kind == "entity" else None)


def plan_shadow_record(shadow: Mapping[str, Any], *, types: Mapping[str, Any] | None = None) -> ShadowPlan:
    """The plan for one desk shadow record (a ``desk_rows_shadow`` row as a
    mapping: ``id``, ``run_id``, ``analyst_id``, ``target_id``,
    ``occurred_at``, ``contract_version``, ``gate_version``, ``record``).
    Pure; never raises on a malformed row (it is counted)."""
    types = types if types is not None else type_map()
    record = shadow.get("record") if isinstance(shadow.get("record"), Mapping) else {}
    plan = ShadowPlan(int(shadow["id"]), _s(shadow.get("run_id")), _s(shadow.get("analyst_id")),
                      _s(shadow.get("target_id")), shadow.get("occurred_at"),
                      _s(shadow.get("contract_version")), _s(shadow.get("gate_version")))
    outcomes = [o for o in (record.get("outcomes") or []) if isinstance(o, Mapping)]
    collection = plan.target_id or ""
    by_key: dict[str, PlannedRecord] = {}
    facts: dict[str, PlannedRecord] = {}

    def _add(rec: PlannedRecord) -> None:
        prior = by_key.get(rec.claim_key)
        if prior is not None:
            rec.same_as_row = prior.row_id
        else:
            by_key[rec.claim_key] = rec
        plan.records.append(rec)

    # facts first (assessments rest on them), then absences, then the rest
    order = {"fact": 0, "absence": 1, "assessment": 2, "restatement": 3}
    for o in sorted(outcomes, key=lambda x: order.get(str(x.get("row_kind")), 9)):
        row_kind, outcome, rid = str(o.get("row_kind") or ""), str(o.get("outcome") or ""), str(o.get("id") or "")
        if row_kind == "fact":
            if outcome != _ELIGIBLE:
                plan.not_written[f"fact:{outcome}"] += 1
                continue
            draft = o.get("draft") if isinstance(o.get("draft"), Mapping) else {}
            decl = types.get(str(draft.get("kind") or ""))
            subject = _subject(draft)
            if decl is None:
                plan.refused["fact:type_not_declared"] += 1
                continue
            if subject is None:
                plan.refused["fact:subject_unreadable"] += 1
                continue
            text = strip_markers(str(o.get("text") or "")).strip()
            verdict = _s(o.get("verdict"))
            status = HELD if verdict == "supports" else NOT_HELD
            reason = _VERDICT_REASON.get(verdict or "", "not_verified")
            evidence = [(str(s.get("ref_id")), str(s.get("quote") or ""), _s(s.get("quote_found_in")))
                        for s in (o.get("support") or []) if isinstance(s, Mapping)
                        and _UUID.match(str(s.get("ref_id") or "").lower())]
            cls = decl.attr("assertion_class")
            rec = PlannedRecord(
                rid, row_kind, cls, decl.id, subject, text,
                claim_key(decl.id, subject.key, {"text": text}, decl.attr("key")), status, reason, verdict,
                slots=dict(draft.get("slots") or {}), valid_from=_dt(draft.get("valid_from")),
                valid_from_precision=_s(draft.get("valid_from_precision")),
                speaker_text=_speaker(text) if cls == "attributed" else None, evidence=evidence)
            facts[rid] = rec
            _add(rec)
        elif row_kind == "absence":
            if outcome != "absence":
                plan.not_written[f"absence:{outcome}"] += 1
                continue
            decl = types.get("absence")
            detail = o.get("detail") if isinstance(o.get("detail"), Mapping) else {}
            scope = detail.get("scope") if isinstance(detail.get("scope"), Mapping) else {}
            topic = str(detail.get("topic") or "").strip()
            if decl is None or not topic or not collection:
                plan.refused["absence:unscoped"] += 1
                continue
            subject = Subject("target", collection, collection)
            text = strip_markers(str(o.get("text") or "")).strip()
            rec = PlannedRecord(
                rid, row_kind, decl.attr("assertion_class"), decl.id, subject, text,
                claim_key(decl.id, subject.key, {"topic": topic, "collection": collection}, decl.attr("key")),
                HELD, "absence_scoped",
                slots={"topic": topic, "scope": {k: scope.get(k) for k in
                                                 ("collection", "source_ids", "handed_rows", "missing")}},
                valid_from=_dt(scope.get("window_start")), valid_to=_dt(scope.get("window_end")))
            _add(rec)
        elif row_kind == "assessment":
            if outcome != "assessment":
                plan.not_written[f"assessment:{outcome}"] += 1
                continue
            decl = types.get("assessment")
            detail = o.get("detail") if isinstance(o.get("detail"), Mapping) else {}
            premises = [str(x) for x in (detail.get("premises_passed") or [])]
            if decl is None or not collection:
                plan.refused["assessment:unscoped"] += 1
                continue
            held = bool(premises) and all(p in facts and facts[p].status == HELD for p in premises)
            subject = Subject("target", collection, collection)
            text = strip_markers(str(o.get("text") or "")).strip()
            rec = PlannedRecord(
                rid, row_kind, decl.attr("assertion_class"), decl.id, subject, text,
                claim_key(decl.id, subject.key, {"text": text}, decl.attr("key")),
                HELD if held else NOT_HELD, "premises_held" if held else "premise_not_held",
                slots={"confidence": detail.get("confidence")},
                premises=[p for p in premises if p in facts])
            _add(rec)
        elif row_kind == "restatement":
            if outcome != "restatement":
                plan.not_written[f"restatement:{outcome}"] += 1
                continue
            detail = o.get("detail") if isinstance(o.get("detail"), Mapping) else {}
            cited = [c for c in (o.get("cited") or []) if isinstance(c, int)]
            plan.links.append(PlannedLink(rid, cited[0] if cited else None, _s(detail.get("status")),
                                          tuple(str(x) for x in (detail.get("backing") or []))))
        else:
            plan.not_written[f"{row_kind or 'unknown'}:{outcome}"] += 1
    return plan


def _s(v: Any) -> str | None:
    return None if v is None or v == "" else str(v)


def text_sha(text: str) -> str:
    return _sha(text)


def iter_signal_ids(plans: Iterable[ShadowPlan]) -> list[str]:
    """Every cited signal id across ``plans`` (for one pin read)."""
    return sorted({sid for p in plans for r in p.records for sid, _, _ in r.evidence})
