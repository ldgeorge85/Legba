# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract-v2 rows through the statement gates, BEFORE anything is written.

Every row ends as exactly one OUTCOME, with its named reasons when it fails —
a failed row is kept, never silently dropped:

* a FACT row is first held to the contract (one sentence, at most one figure,
  a fact kind, cited, every cited ``[N]`` a handed SIGNAL row, a subject from
  the offered list — an actor only when a cited row links it), then assembled
  and gated EXACTLY as a typed desk sentence is
  (:func:`statement_typing.assemble`: G1 quote in the cited text, G2 numbers /
  dates / subject named / valid-from, G3 handed rows, claim rule, control
  text, access ceiling, G7 independence — ``statement_gate.v4``). Passing it
  all is ``established_eligible``: the only outcome the verifier sees.
* an ABSENCE row names a topic; code writes the sentence, scoped to the handed
  collection and window (:func:`statement_gate.evaluate_absence_gates`).
* an ASSESSMENT row rests on fact rows that passed (``premise`` rows,
  :func:`statement_gate.evaluate_assessment_gates`); a confidence word from
  the house bands; no figure its premises do not state.
* a RESTATEMENT names a PRIOR-READ block by its ``[N]``, a status, and the
  passed rows that bear on it.

THE REPAIR ROUND (:func:`build_repair_prompt`, :func:`apply_repairs`): the
model is shown ONLY the failed rows, their reasons in plain words and the
texts of the rows they cite (with, for a subject or counterpart the quotes do
not name, where the cited text names it), and may fix a quote, narrow a claim,
SPLIT a fact row into one row per claim, or withdraw it. THE RULE (D-2,
:func:`repair_refusal`): a repaired text may add only words that occur in its
OWN cited quotes — quotes that are, word for word, in the cited row's text —
or drop words; it may not add a figure or a date its quotes do not state; it
may not flip a negation, nor drop an attribution, modal or condition the
original carried, nor add one its quotes do not carry (the S-5 narrowing
guards' words); it may not turn two named parties round into an order its
quotes do not give ("X struck Y" -> "Y struck X", all words copied); and it
may not cite a row the run was not handed. A split
holds each part to the same rule against the original and the part's own
quotes, every part with its own cite. D-1's rule (at most two new content
words, quotes ignored) refused 125 fixes of which the measured majority only
aligned the claim to its quote. A repair naming a row that did not fail is
refused. The repaired reply is gated again from the top.

SURFACE SUBJECTS and GENERIC ROLES: :mod:`desk_rows_surface`. ACTORS: an
offered actor may be a fact row's subject when a cited row LINKS it or NAMES
it (``found_by`` per row, :func:`desk_rows_computed.offered_subjects`).

No I/O; no table.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .desk_rows_contract import (
    FACT_KINDS,
    MAX_SPLIT_ROWS,
    AbsenceRow,
    AssessmentRow,
    FactRow,
    ParsedRepairs,
    Restatement,
    RowsReply,
    fact_as_dict,
    parse_rows_reply,
)
from .desk_rows_surface import SURFACE, is_generic_role, surface_refusals
from .provenance.text_fold import normalize_for_match
from .statement_content import find_dates, strip_markers
from .statement_gate import evaluate_absence_gates, evaluate_assessment_gates
from .statement_numbers import read_figures
from .statement_sentences import CitedRef, CitedSentence
from .statement_shapes import (
    CheckResult,
    HandedSlice,
    StatementDraft,
    SupportKind,
    SupportRole,
    SupportRow,
)
from .statement_split import _LEAD_SCOPE_RE  # the S-5 narrowing guards' scope words, reused as is
from .statement_typing import EvidenceRef, SubjectCandidate, TypingInput, TypingResult, assemble, evidence_window

#: This module's rule set, stamped on every shadow record beside the contract
#: and the statement gate versions. ``d2``: surface subjects, generic-role
#: refusals, actors named in a cited row, the quote-words repair rule, splits.
DESK_GATES_VERSION = "desk_rows_gates.d2"

ESTABLISHED_ELIGIBLE = "established_eligible"
ASSESSMENT = "assessment"
ABSENCE = "absence"
RESTATEMENT = "restatement"
FAILED = "failed"
WITHDRAWN = "withdrawn"
OUTCOMES = (ESTABLISHED_ELIGIBLE, ASSESSMENT, ABSENCE, RESTATEMENT, FAILED, WITHDRAWN)

#: Grounding blocks a restatement may name (the desk's OWN prior record —
#: ``statement_piece_typing.RESTATABLE_REF_KINDS``).
RESTATABLE_REF_KINDS = frozenset({"prior_read", "window_ledger", "desk_baseline"})
#: Parse defects that make a row fail (the rest are notes).
_FAILING_DEFECTS = frozenset({"cites_not_a_list", "cite_not_an_object"})
#: Negations: a repair may neither add nor drop one (polarity).
_NEGATION_RE = re.compile(r"\b(?:not|no|never|none|nor|neither|without|den(?:y|ied|ies|ial)|reject\w*|"
                          r"refut\w*|dismiss\w*)\b|n['’]t\b", re.IGNORECASE)
_STOP = frozenset(
    "the and for that this with from have has had was were are been its their his her into over "
    "after before about than then also which who whom what when where while said says say not "
    "but any all some more most other such only very can could would should will may might its "
    "per via under upon against between during through".split())

_PLAIN = {
    "G1.quote:quote_absent": "the quote is not, word for word, in the cited row's text",
    "G1.quote:quote_too_short": "the quote is shorter than three words",
    "G1.quote:quote_missing": "no quote was given for a cited row",
    "G2.numbers:number_not_in_evidence": "a figure in the text is not in the quotes",
    "G2.dates:date_not_in_evidence": "a date in the text is not in the quotes",
    "G2.subject_named:subject_not_named": "the subject is not named in the quotes",
    "G2.valid_from_stated:valid_from_not_stated": "the valid_from date is not stated in the quotes",
    "G2.claim_source:claim_source_supports_only_attributed":
        "the cited row is a party's own claim: the row must be kind 'attributed' (X said Y)",
    "subject_not_offered": "the subject is not an id from the SUBJECTS list",
    "subject_not_linked_to_cited_rows": "that actor is neither linked to nor named in any row this fact cites",
    "surface_not_in_quote": "a surface subject must be copied word for word from one of the row's quotes",
    "surface_not_proper_name": "a surface subject must be a proper name (a named body, group or person)",
    "subject_generic_role": ("a generic role ('officials', 'a spokesperson') is never a subject: take the body "
                             "they speak for when the quote names it, or write the row as kind 'attributed' "
                             "about what the claim concerns"),
    "counterpart_generic_role": "a generic role ('officials', 'residents') is never a counterpart",
    "surface_names_an_offered_subject": "that name is on the SUBJECTS list: use its S-id, not a surface subject",
    "surface_missing": "subject_kind is 'surface' but no name was given",
    "G2.counterpart_named:counterpart_not_named": "the counterpart is not named in the quotes",
    "not_one_sentence": "the text is more than one sentence",
    "more_than_one_figure": "the text states more than one figure",
    "uncited": "the row cites nothing",
    "cite_row_not_handed": "a cited [N] is not a row of this slice",
    "cites_no_evidence_row": "the row cites only a grounding block, not a signal (carry a prior point as a restatement)",
    "collection_fact_in_rows": "collection counts are computed by code, not written as rows",
    "kind_not_fact": "a judgement or an absence is not a fact row",
    "confidence_word_unknown": "the confidence word is not one of the house bands",
    "rests_on_no_eligible_fact": "none of the fact rows it rests on passed its checks",
    "premise_unknown": "rests_on names a row id that does not exist",
    "assessment_new_figure": "the judgement states a figure none of its fact rows state",
    "ref_not_prior_read": "ref is not a PRIOR READ block",
    "restatement_unbacked": "no passed row bears on it",
}


def _plain(reason: str) -> str:
    return _PLAIN.get(reason) or _PLAIN.get(reason.split(":")[0]) or reason


@dataclass(frozen=True)
class GateContext:
    """What the gates need about the run: the handed rows by ordinal, the
    non-signal blocks' kinds by ordinal, the offered subjects (S1 = the
    desk), which handed signal ids link each offered actor, and the run's
    scope."""

    evidence: Mapping[int, EvidenceRef]
    block_kinds: Mapping[int, str]
    subjects: Sequence[SubjectCandidate]
    actor_links: Mapping[str, frozenset[str]]
    desk_subject_count: int
    handed: HandedSlice
    target_id: str | None
    read_at: datetime | None = None
    computed: Mapping[str, Any] = field(default_factory=dict)
    #: Per offered actor and handed signal id: ``linked`` / ``named_in_text``
    #: (:class:`desk_rows_computed.OfferedSubjects`). Recorded on the row.
    found_by: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RowOutcome:
    row_id: str
    row_kind: str          # fact / absence / assessment / restatement
    outcome: str
    reasons: tuple[str, ...] = ()
    text: str = ""
    cited: tuple[int, ...] = ()
    result: TypingResult | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    repaired: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.row_id, "row_kind": self.row_kind, "outcome": self.outcome,
                               "text": self.text, "cited": list(self.cited)}
        if self.reasons:
            out["reasons"] = list(self.reasons)
        if self.repaired:
            out["repaired"] = True
        if self.result is not None and self.result.draft is not None:
            d = self.result.draft
            out["draft"] = {"kind": str(d.kind), "subject_kind": d.subject_kind, "subject_key": d.subject_key,
                            "valid_from": d.valid_from.isoformat() if d.valid_from else None,
                            "valid_from_precision": d.valid_from_precision,
                            "slots": dict(d.slots) if d.slots else None}
            out["support"] = [{"ref_id": s.ref_id, "quote": s.quote, "quote_found_in": s.quote_found_in}
                              for s in self.result.support]
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", strip_markers(text).lower()) if len(w) >= 3 and w not in _STOP}


def figure_values(text: str) -> set[float]:
    return {round(f.value, 6) for f in read_figures(strip_markers(text))}


def _sentences(text: str) -> int:
    from .analysts.assessment_unsupported import segment_sentences

    return sum(1 for s in segment_sentences(strip_markers(text)) if s.text.strip() and not s.text.lstrip().startswith("#"))


def _offered_index(raw: str, n: int) -> int | None:
    m = re.fullmatch(r"[Ss]?(\d+)", (raw or "").strip())
    if not m:
        return None
    i = int(m.group(1))
    return i if 1 <= i <= n else None


def absence_text(topic: str, computed: Mapping[str, Any]) -> str:
    """The sentence code writes for an absence (the model names the topic)."""
    topic = strip_markers(topic).strip().rstrip(".")
    span = ""
    if computed.get("window_start") and computed.get("window_end"):
        span = f", {str(computed['window_start'])[:10]} to {str(computed['window_end'])[:10]}"
    return (f"No row in this desk's collection ({computed.get('rows_handed', 0)} signals from "
            f"{computed.get('distinct_sources', 0)} sources{span}) reports {topic}.")


# ---------------------------------------------------------------------------
# per row
# ---------------------------------------------------------------------------


def gate_fact(f: FactRow, ctx: GateContext, index: int) -> RowOutcome:
    contract: list[str] = [d for d in f.defects if d in _FAILING_DEFECTS]
    text = strip_markers(f.text).strip()
    if not text:
        contract.append("text_missing")
    elif _sentences(text) > 1:
        contract.append("not_one_sentence")
    if text and len(read_figures(text)) > 1:
        contract.append("more_than_one_figure")
    if f.kind == "coverage":
        contract.append("collection_fact_in_rows")
    elif f.kind in {"assessment", "absence"}:
        contract.append("kind_not_fact")
    elif f.kind not in FACT_KINDS:
        contract.append("kind_unknown")
    if not f.cites:
        contract.append("uncited")
    signal_cites: list[tuple[EvidenceRef, str]] = []
    for c in f.cites:
        if c.row is not None and c.row in ctx.evidence:
            signal_cites.append((ctx.evidence[c.row], c.quote))
        elif c.row is not None and c.row in ctx.block_kinds:
            continue
        elif "cite_row_not_handed" not in contract:
            contract.append("cite_row_not_handed")
    if f.cites and not signal_cites and "cite_row_not_handed" not in contract:
        contract.append("cites_no_evidence_row")
    cited_ids = {ev.ref_id for ev, _ in signal_cites}
    detail: dict[str, Any] = {}
    subjects = tuple(ctx.subjects)
    subject_ref = f.subject
    if f.subject_kind == SURFACE:
        # an actor no offered subject matches: its name, verbatim from a quote
        surface = re.sub(r"\s+", " ", f.subject.strip())
        contract += surface_refusals(surface, [c.quote for c in f.cites], subjects)
        subjects = subjects + (SubjectCandidate(SURFACE, surface, (surface,), canonical_name=surface),)
        subject_ref = f"S{len(subjects)}"
        detail["subject_kind"] = SURFACE
    else:
        si = _offered_index(f.subject, len(ctx.subjects))
        if si is not None and si > ctx.desk_subject_count:
            key = ctx.subjects[si - 1].key
            links = ctx.actor_links.get(key, frozenset())
            if not (links & cited_ids):
                contract.append("subject_not_linked_to_cited_rows")
            else:
                how = {ctx.found_by.get(key, {}).get(r, "linked") for r in links & cited_ids}
                detail["subject_found_by"] = "linked" if "linked" in how else "named_in_text"
    cp = f.counterpart.strip()
    if cp and not re.fullmatch(r"[Ss]\d+", cp) and is_generic_role(cp):
        contract.append("counterpart_generic_role")
    evs = tuple(dict.fromkeys(ev for ev, _ in signal_cites))
    refs = tuple(CitedRef(ev.ordinal, "signal", ev.ref_id, ev.source_id, ev.title, True) for ev in evs)
    sentence = CitedSentence(index, text, text, refs)
    item = TypingInput(sentence, evs, subjects, read_at=ctx.read_at)
    slots: dict[str, Any] = {"counterpart": f.counterpart} if f.counterpart else {}
    figures = read_figures(text) if text else []
    if f.kind == "level" and len(figures) == 1:
        # A level row states ONE figure (the contract's rule): that figure IS
        # its value, filled by code — G2 then checks it against the quotes.
        slots["value"] = figures[0].value
        if figures[0].unit:
            slots["unit"] = figures[0].unit
    reply = {
        "kind": f.kind, "subject": subject_ref, "valid_from": f.valid_from or None,
        "valid_from_precision": f.valid_from_precision or ("unknown" if not f.valid_from else ""),
        "slots": slots,
        "support": [{"row": ev.ordinal, "quote": q} for ev, q in signal_cites],
        "relation": {"action": "new"},
    }
    result = assemble(item, reply, handed=ctx.handed)
    if f.subject_kind == SURFACE and result.gate is not None:
        result = replace(result, gate=_surface_gate(result.gate, detail, contract))
    reasons = list(dict.fromkeys(contract + list(result.refusals)
                                 + ([f"{c.gate}.{c.check}:{c.reason}" for c in result.gate.failures]
                                    if result.gate else [])))
    ok = not contract and result.passed
    notes = [d for d in f.defects if d not in _FAILING_DEFECTS]
    if notes:
        detail["notes"] = notes
    if f.split_from:
        detail["split_from"] = f.split_from
    return RowOutcome(f.id, "fact", ESTABLISHED_ELIGIBLE if ok else FAILED, tuple(reasons), text,
                      tuple(c.row for c in f.cites if c.row is not None), result, detail)


def _surface_gate(gate: Any, detail: Mapping[str, Any], contract: Sequence[str]) -> Any:
    """A surface row's gate report: the statement layer's G8 subject check
    refuses a ``surface`` subject kind (it is not a key — module doc of
    :mod:`desk_rows_surface`); here that one check is REPLACED by the surface
    rule's own (``D2.surface_subject``, passed only when no surface reason was
    named). Every other check stands as run."""
    own = [r for r in contract if r.startswith(("surface_", "subject_generic_role"))]
    checks = tuple(c for c in gate.checks if not (c.gate == "G8" and c.check == "subject"))
    checks += (CheckResult("D2", "surface_subject", not own, own[0] if own else None, None,
                           {"subject_kind": SURFACE, **dict(detail)}),)
    return replace(gate, checks=checks)


def is_surface(o: RowOutcome) -> bool:
    """A fact row whose subject is a surface form (not an entity key)."""
    return o.row_kind == "fact" and o.detail.get("subject_kind") == SURFACE


def gate_absence(a: AbsenceRow, ctx: GateContext) -> RowOutcome:
    reasons: list[str] = []
    topic = a.topic.strip()
    if not topic:
        reasons.append("topic_missing")
    if re.search(r"\[\s*\d+\s*\]", topic):
        reasons.append("absence_cites_rows")
    if topic and read_figures(strip_markers(topic)):
        reasons.append("absence_topic_has_figure")
    text = absence_text(topic, ctx.computed)
    draft = StatementDraft(text, "absence", "target", ctx.target_id or "")
    gate = evaluate_absence_gates(draft, handed=ctx.handed)
    reasons += [f"{c.gate}.{c.check}:{c.reason}" for c in gate.failures]
    scope = next((dict(c.detail) for c in gate.checks if c.check == "absence_scope"), {})
    return RowOutcome(a.id, "absence", ABSENCE if not reasons else FAILED, tuple(reasons), text, (),
                      None, {"topic": topic, "scope": scope})


def gate_assessment(j: AssessmentRow, ctx: GateContext, facts: Mapping[str, RowOutcome]) -> RowOutcome:
    reasons: list[str] = []
    text = strip_markers(j.text).strip()
    if not text:
        reasons.append("text_missing")
    if j.confidence is None:
        reasons.append("confidence_word_unknown")
    unknown = [x for x in j.rests_on if x not in facts]
    if unknown:
        reasons.append("premise_unknown")
    passed = [x for x in j.rests_on if x in facts and facts[x].outcome == ESTABLISHED_ELIGIBLE]
    if not passed:
        reasons.append("rests_on_no_eligible_fact")
    premise_figs: set[float] = set()
    for x in passed:
        premise_figs |= figure_values(facts[x].text)
    if text and figure_values(text) - premise_figs:
        reasons.append("assessment_new_figure")
    premises = [SupportRow(SupportRole.PREMISE.value, SupportKind.STATEMENT.value, x,
                           ref_statement_kind=str(facts[x].result.draft.kind)
                           if facts[x].result and facts[x].result.draft else None) for x in passed]
    draft = StatementDraft(text or "-", "assessment", "target", ctx.target_id or "")
    gate = evaluate_assessment_gates(draft, premises)
    reasons += [f"{c.gate}.{c.check}:{c.reason}" for c in gate.failures]
    return RowOutcome(j.id, "assessment", ASSESSMENT if not reasons else FAILED, tuple(dict.fromkeys(reasons)),
                      text, (), None, {"confidence": j.confidence or j.confidence_raw, "rests_on": list(j.rests_on),
                                       "premises_passed": passed})


def gate_restatement(r: Restatement, ctx: GateContext, passed: Mapping[str, RowOutcome]) -> RowOutcome:
    reasons: list[str] = []
    if r.ref is None or (r.ref not in ctx.block_kinds and r.ref not in ctx.evidence):
        reasons.append("ref_unknown")
    elif ctx.block_kinds.get(r.ref) not in RESTATABLE_REF_KINDS:
        reasons.append("ref_not_prior_read")
    if r.status is None:
        reasons.append("status_unknown")
    if not strip_markers(r.text).strip():
        reasons.append("text_missing")
    if any(x not in passed and not x.startswith(("F", "A")) for x in r.bearing):
        reasons.append("bearing_unknown")
    backing = [x for x in r.bearing if x in passed and passed[x].outcome in (ESTABLISHED_ELIGIBLE, ABSENCE)]
    if r.status == "confirmed":
        backing = [x for x in backing if passed[x].outcome == ESTABLISHED_ELIGIBLE]
    if not backing:
        reasons.append("restatement_unbacked")
    return RowOutcome(r.id, "restatement", RESTATEMENT if not reasons else FAILED, tuple(reasons),
                      strip_markers(r.text).strip(), (r.ref,) if r.ref else (), None,
                      {"status": r.status, "bearing": list(r.bearing), "backing": backing})


def gate_reply(reply: RowsReply, ctx: GateContext, *, repaired: frozenset[str] = frozenset()) -> list[RowOutcome]:
    """Every row of ``reply`` through its gates, in dependency order (facts,
    absences, then the judgements and restatements that rest on them)."""
    out: list[RowOutcome] = []
    facts = {f.id: gate_fact(f, ctx, i) for i, f in enumerate(reply.fact_rows)}
    absences = {a.id: gate_absence(a, ctx) for a in reply.absence_rows}
    out += list(facts.values()) + list(absences.values())
    out += [gate_assessment(j, ctx, facts) for j in reply.assessment_rows]
    passed = {**facts, **absences}
    out += [gate_restatement(r, ctx, passed) for r in reply.restatements]
    return [replace(o, repaired=True) if o.row_id in repaired else o for o in out]


# ---------------------------------------------------------------------------
# The repair round
# ---------------------------------------------------------------------------


REPAIR_INSTRUCTIONS = "\n".join((
    "REPAIR ROUND. The items below, from the read you just wrote, failed the platform's checks; "
    "each is shown with its reasons. For each item, do one of three things:",
    "- FIX it: copy the quote word for word from the cited row's text (a quote that names the "
    "subject when the subject is not named), narrow the text to what the quote states, correct "
    "its kind, subject, counterpart or dates, or cite the evidence row that actually says it.",
    f"- SPLIT a fact item that states more than one figure or claim into 2 to {MAX_SPLIT_ROWS} "
    "items, one figure or claim each, each with its own quote.",
    "- WITHDRAW it.",
    "The rule for a fixed text:",
    "- it uses only words of the original or words of its own quotes;",
    "- it adds no figure or date its quotes do not state;",
    "- it keeps every negation, attribution ('said', 'according to'), hedge ('reportedly', 'may') "
    "and condition the original had;",
    "- it cites only numbered evidence rows of this run.",
    "You may NOT add an item except by splitting a failed one, and you may NOT add a claim. When a "
    "fact's actor is named in your quote but is not on the SUBJECTS list, set \"subject_kind\": "
    "\"surface\" and \"subject\" to the name copied exactly from the quote; a generic role "
    "('officials', 'a spokesperson') is never a subject.",
    "Reply with ONE JSON object and nothing else: "
    '{"repairs": [{"id": "<item id>", "action": "fix", "row": {<the whole fixed item, same fields '
    'as before>}}, {"id": "<item id>", "action": "split", "rows": [{<a whole item>}, {<a whole '
    'item>}]}, {"id": "<item id>", "action": "withdraw"}]}',
))


def failed_ids(outcomes: Sequence[RowOutcome]) -> frozenset[str]:
    return frozenset(o.row_id for o in outcomes if o.outcome == FAILED)


def build_repair_prompt(reply: RowsReply, outcomes: Sequence[RowOutcome], ctx: GateContext) -> str | None:
    """The repair round's user message: the failed rows only, their reasons in
    plain words, and for a fact row the texts of the rows it cites. ``None``
    when nothing failed (no call)."""
    failed = [o for o in outcomes if o.outcome == FAILED]
    if not failed:
        return None
    from .desk_rows_contract import reply_as_dict

    by_id = {r["id"]: r for rows in reply_as_dict(reply).values() for r in rows}
    lines = [REPAIR_INSTRUCTIONS, "", "FAILED ITEMS:"]
    for o in failed:
        lines.append(f"{o.row_id} — failed because: " + "; ".join(_plain(r) for r in o.reasons))
        lines.append("  row: " + json.dumps(by_id.get(o.row_id, {}), ensure_ascii=False))
        if o.row_kind == "fact":
            for n in dict.fromkeys(o.cited):
                ev = ctx.evidence.get(n)
                if ev is None:
                    continue
                body = evidence_window(ev.text or " ".join(ev.feed_text), o.text).replace("\n", " ")
                lines.append(f"  ROW [{n}] · title: {ev.title or '(untitled)'}")
                lines.append(f"    TEXT: {body}")
            lines += _naming_hints(o, ctx)
    if any(o.row_kind == "fact" for o in failed):
        from .desk_rows_computed import subjects_block

        lines += ["", subjects_block(ctx.subjects)]
    if any(o.row_kind in ("assessment", "restatement") for o in failed):
        ok = [o for o in outcomes if o.outcome in (ESTABLISHED_ELIGIBLE, ABSENCE)]
        lines += ["", "ITEMS THAT PASSED (a judgement may rest on, a restatement may be borne by, these ids):"]
        lines += [f"  {o.row_id}: {o.text}" for o in ok] or ["  (none)"]
    return "\n".join(lines)


def _naming_hints(o: RowOutcome, ctx: GateContext) -> list[str]:
    """For a fact row whose quotes do not name its subject (or counterpart):
    where the CITED rows' text names it — the sentence, verbatim — so the
    repair can quote the span that names it. Nothing is added to the row."""
    wanted = {"G2.subject_named:subject_not_named": "subject", "G2.counterpart_named:counterpart_not_named":
              "counterpart"}
    roles = [wanted[r] for r in o.reasons if r in wanted]
    d = o.result.draft if o.result else None
    if not roles or d is None:
        return []
    by_key = {s.key: s for s in ctx.subjects}
    out: list[str] = []
    for role in roles:
        if role == "subject":
            cand = by_key.get(d.subject_key)
            names = [str(getattr(n, "name", n)) for n in (cand.names if cand else ())]
        else:
            cdet = (o.result.detail or {}).get("counterpart") or {}
            cand = by_key.get(cdet.get("key")) if cdet.get("key") else None
            names = [str(getattr(n, "name", n)) for n in cand.names] if cand else \
                [str((d.slots or {}).get("counterpart") or "")]
        for n in dict.fromkeys(o.cited):
            ev = ctx.evidence.get(n)
            hit = _sentence_naming(ev, names) if ev is not None else None
            if hit:
                out.append(f"    ROW [{n}] NAMES THE {role.upper()} HERE: \"{hit}\"")
                break
    return out


def _sentence_naming(ev: EvidenceRef, names: Sequence[str]) -> str | None:
    for text in (ev.text, *ev.feed_text):
        if not text:
            continue
        for name in sorted((x for x in names if x and len(x) >= 2), key=len, reverse=True):
            m = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, flags=0 if name.isupper() else re.IGNORECASE)
            if not m:
                continue
            start = max(text.rfind(". ", 0, m.start()) + 2, text.rfind("\n", 0, m.start()) + 1, 0)
            ends = [i for i in (text.find(". ", m.end()), text.find("\n", m.end())) if i >= 0]
            end = min(ends) + 1 if ends else len(text)
            return re.sub(r"\s+", " ", text[start:end]).strip()[:400]
    return None


# --- the repair rule (D-2) ---------------------------------------------------

_SUFFIX_RE = re.compile(r"(?:ing|ed|es|s|ly)$")


def _stem(w: str) -> str:
    return _SUFFIX_RE.sub("", w) if len(w) > 4 else w


def _claim_words(text: str) -> set[str]:
    """Content words, figures left to the figure rule."""
    return {w for w in content_words(text) if not w.isdigit()}


#: The narrowing guards' scope words, by what they do to a claim: an
#: ATTRIBUTION ("X said", "according to"), a MODAL or hedge ("would",
#: "likely", "plans to"), a CONDITION ("if", "unless"). A repair keeps each
#: class the original carried (a synonym inside the class — "said" for
#: "stated" — is not a change of attribution; the speaker's name is held by
#: the word rule and the subject gate).
_ATTRIBUTION_RE = re.compile(
    r"^(?:sa(?:y|ys|id)|claim\w*|alleg\w*|accord\w*|report\w*|cit(?:e|es|ed|ing)|told|tells?|stat(?:e|es|ed)|"
    r"announc\w*|quot\w*)$")
_CONDITION_RE = re.compile(r"^(?:if|unless|whether)$")
_EXTRA_ATTRIBUTION = re.compile(r"\b(?:stress\w*|add(?:s|ed)|not(?:es|ed)|wrote|confirm\w*|insist\w*|"
                                r"assert\w*|acknowledg\w*|accus\w*|deni\w*|deny)\b", re.IGNORECASE)


def _scope_words(text: str) -> set[str]:
    """The scope CLASSES (``attribution`` / ``modal`` / ``condition``) the
    text carries, from the narrowing guards' words; negations are polarity.
    A capitalised match inside the sentence is a name, not a scope word
    ("United States", "State Department")."""
    plain = strip_markers(text)
    out: set[str] = set()
    for m in _LEAD_SCOPE_RE.finditer(plain):
        w = m.group(0)
        if _NEGATION_RE.fullmatch(w) or (w[:1].isupper() and m.start() > 0):
            continue
        lw = w.lower()
        out.add("attribution" if _ATTRIBUTION_RE.match(lw) else "condition" if _CONDITION_RE.match(lw)
                else "modal")
    if _EXTRA_ATTRIBUTION.search(plain):
        out.add("attribution")
    return out


def _negation_class(w: str) -> str:
    w = w.lower().replace("’", "'")
    if w == "n't":
        return "not"
    for stem in ("den", "reject", "refut", "dismiss"):
        if w.startswith(stem):
            return stem
    return w


def _negations(text: str) -> Counter[str]:
    """The negations a text carries, by class ("n't" = "not"; "rejected" =
    "rejects")."""
    return Counter(_negation_class(m.group(0)) for m in _NEGATION_RE.finditer(strip_markers(text)))


def _day_keys(text: str) -> set[tuple[Any, Any, Any]]:
    return {(d.month, d.day, d.year) for d in find_dates(strip_markers(text))}


def text_refusal(old: str, new: str, sources: Sequence[str], *, part: bool = False) -> str | None:
    """Why a repaired text ``new`` may not replace ``old``, given the texts
    its new words may come from (``sources``: the fixed row's own located
    quotes, or for a judgement the texts of the rows it rests on), else
    ``None``. Dropping words is always allowed. ``part``: ``new`` is one part
    of a SPLIT — a negation or scope word of the original may sit in another
    part (the split checks the parts together, :func:`_split`)."""
    src = " ".join(s for s in sources if s)
    # the most specific reason first: meaning changes, then figures and
    # dates, then words from nowhere
    if not part and _negations(new) != _negations(old):
        return "repair_changes_polarity"
    if part and _negations(new) - _negations(old):
        return "repair_changes_polarity"
    old_s, new_s = _scope_words(old), _scope_words(new)
    if not part and old_s - new_s:
        return "repair_drops_scope"
    if new_s - old_s - _scope_words(src):
        return "repair_adds_scope_not_in_quote"
    if figure_values(new) - figure_values(old) - figure_values(src):
        return "repair_new_figure_not_in_quote"
    if _day_keys(new) - _day_keys(old) - _day_keys(src):
        return "repair_new_date_not_in_quote"
    old_w, src_w = _claim_words(old), _claim_words(src)
    known = old_w | src_w | {_stem(w) for w in old_w | src_w}
    if any(w not in known and _stem(w) not in known for w in _claim_words(new)):
        return "repair_adds_words_not_in_quotes"
    if _reorders_parties(old, new, src):
        return "repair_reorders_parties"
    return None


_NAME_RE = re.compile(r"(?<![\w-])[A-Z][\w-]+(?:\s+(?:of\s+(?:the\s+)?)?[A-Z][\w-]+)*")
#: Capitalised words that start a sentence or a phrase, never a name.
_FUNCTION_CAPS_RE = re.compile(
    r"(?<![\w-])(?:The|A|An|In|On|At|After|Before|During|Since|By|As|It|Its|This|That|These|Those|"
    r"According|He|She|They|We|His|Her|Their|But|And|While|When|If)(?![\w-])")
#: A name right after one of these is a counterpart or a place, not the actor.
_ROLE_PREPS = frozenset("in at near from to on against with into over for toward towards across inside off".split())
_BE_RE = re.compile(r"(?<![\w-])(?:was|were|is|are|been|being)(?![\w-])")
_SAY_RE = (r"(?:sa(?:y|ys|id)|told|tells?|stat(?:e|es|ed)|add(?:s|ed)|announc(?:e|es|ed)|claim(?:s|ed)?|"
           r"warn(?:s|ed)?|report(?:s|ed)?|wrote|stress(?:es|ed)|insist(?:s|ed)|confirm(?:s|ed)|den(?:y|ies|ied))")
_SPEAKER_AFTER = re.compile(rf"^(?:\s+[a-z][\w-]*){{0,3}}\s+{_SAY_RE}(?![\w-])")
_ACCORDING_BEFORE = re.compile(r"according to\s*$")
#: "..., said X." — a say-verb right before a name that ends the text.
_SAY_BEFORE = re.compile(rf"(?<![\w-]){_SAY_RE}\s*$")
_ENDS = re.compile(r"^\s*[.;!]?\s*$")
_COORDINATED = re.compile(r"^\s*(?:,\s*(?:and\s+)?|and\s+)$")


def party_roles(text: str) -> dict[str, str]:
    """Who is WHO in a one-claim text, by name: ``speaker`` (the source of an
    attribution: "X said", "according to X", "..., said X"), ``agent`` (the
    actor: the first other name of an active clause and any name coordinated
    with it, or the "by X" of a passive one), ``patient`` (the target: a later
    name of an active clause, or the subject of a passive one), or
    ``obl:<prep>`` (a counterpart or a place: "to X", "with X", "in X").
    Only names the text carries ONCE are given (a name used twice has no one
    role). Code, no model: a role it cannot read is simply absent."""
    t = re.sub(r"['’]s\b", "", strip_markers(text).replace("’", "'"))
    t = _FUNCTION_CAPS_RE.sub(lambda m: m.group(0).lower(), t)
    found = list(_NAME_RE.finditer(t))
    counts = Counter(m.group(0) for m in found)
    found = [m for m in found if counts[m.group(0)] == 1]
    roles: dict[str, str] = {}
    open_names: list[re.Match[str]] = []   # names not yet given a role, in order
    for m in found:
        name, before, after = m.group(0), t[:m.start()], t[m.end():]
        prev = re.findall(r"[a-z]+", before.lower()[-20:])
        prev_word = prev[-1] if prev else ""
        if (_SPEAKER_AFTER.match(after) or _ACCORDING_BEFORE.search(before)
                or (_SAY_BEFORE.search(before) and _ENDS.match(after))):
            roles[name] = "speaker"
            continue
        if prev_word == "by":
            # a passive clause: "<patient> ... was <done> by <agent>"
            last = open_names[-1] if open_names else None
            if last is not None and _BE_RE.search(t[last.end():m.start()]):
                roles[name] = "agent"
                roles[last.group(0)] = "patient"
                open_names.pop()
                continue
        if prev_word in _ROLE_PREPS:
            roles[name] = f"obl:{prev_word}"
            continue
        open_names.append(m)
    actor_seen = False
    previous: re.Match[str] | None = None
    for m in open_names:
        if m.group(0) in roles:
            continue
        if previous is not None and _COORDINATED.match(t[previous.end():m.start()]):
            roles[m.group(0)] = roles[previous.group(0)]
        elif not actor_seen and "agent" not in roles.values():
            roles[m.group(0)] = "agent"
        else:
            roles[m.group(0)] = "patient"
        actor_seen = True
        previous = m
    return roles


def _reorders_parties(old: str, new: str, src: str) -> bool:
    """Words copied from the quote can still swap WHO did WHAT to WHOM ("X
    struck Y" -> "Y struck X", "Y was struck by X" -> "X was struck by Y",
    "X struck Y" -> "X was struck by Y", the speaker traded with a party):
    a name both texts carry may change its ROLE (:func:`party_roles`) only
    into the role the quotes give it. Word ORDER alone is not a swap: the
    faithful passive, a moved attribution, reordered co-actors and a moved
    place phrase all keep every role. (Judging by order instead caught 21 of
    29 planted swaps and refused all 24 planted good repairs — the role rule
    catches 29 and refuses none.)"""
    before, after, quoted = party_roles(old), party_roles(new), party_roles(src)
    return any(role != before[name] and quoted.get(name) != role
               for name, role in after.items() if name in before)


def located_quotes(row: FactRow, ctx: GateContext) -> list[str]:
    """The row's quotes that are, word for word (folded), in the text of the
    handed row they cite — the only quotes a repair may take words from."""
    out: list[str] = []
    for c in row.cites:
        ev = ctx.evidence.get(c.row) if c.row is not None else None
        q = normalize_for_match(strip_markers(c.quote or ""))
        if ev is not None and q and any(q in normalize_for_match(t) for t in (ev.text, *ev.feed_text) if t):
            out.append(c.quote)
    return out


def repair_refusal(old: FactRow, new: FactRow, ctx: GateContext, *, part: bool = False) -> str | None:
    """THE REPAIR RULE for a fact row (module doc), else ``None``."""
    if any(c.row is None or (c.row not in ctx.evidence and c.row not in ctx.block_kinds) for c in new.cites):
        return "repair_cites_unhanded_row"
    return text_refusal(old.text, new.text, located_quotes(new, ctx), part=part)


def _fact_from(base: FactRow, patch: Mapping[str, Any]) -> FactRow | None:
    fixed = parse_rows_reply({"fact_rows": [{**fact_as_dict(base), **dict(patch), "id": base.id}]})
    return fixed.fact_rows[0] if fixed and fixed.fact_rows else None


def _split(f: FactRow, parts: Sequence[Mapping[str, Any]], ctx: GateContext,
           taken: set[str]) -> tuple[list[FactRow], str | None]:
    """A failed fact row split into one row per claim: every part holds to
    the repair rule against the original and its OWN quotes, every part cites
    at least one quote, and no two parts rest on the same quotes."""
    rows: list[FactRow] = []
    seen_quotes: set[tuple[str, ...]] = set()
    for k, p in enumerate(parts):
        row = _fact_from(f, {k2: v for k2, v in p.items() if k2 != "id"})
        if row is None:
            return [], "repair_unparseable"
        if not any((c.quote or "").strip() for c in row.cites):
            return [], "split_part_uncited"
        qkey = tuple(sorted(normalize_for_match(c.quote or "") for c in row.cites))
        if qkey in seen_quotes:
            return [], "split_parts_share_quotes"
        seen_quotes.add(qkey)
        why = repair_refusal(f, row, ctx, part=True)
        if why:
            return [], why
        rows.append(row)
    # together, the parts keep every negation and every attribution / modal /
    # condition the original carried (each in the part it governs)
    negs: Counter[str] = Counter()
    scope: set[str] = set()
    for row in rows:
        negs += _negations(row.text)
        scope |= _scope_words(row.text)
    if _negations(f.text) - negs:
        return [], "repair_changes_polarity"
    if _scope_words(f.text) - scope:
        return [], "repair_drops_scope"
    out: list[FactRow] = []
    n = 0
    for k, row in enumerate(rows):
        rid = f.id
        if k:
            n += 1
            while f"F{n}" in taken:
                n += 1
            rid = f"F{n}"
            taken.add(rid)
        out.append(replace(row, id=rid, model_id=f.model_id if not k else None, split_from=f.id))
    return out, None


def apply_repairs(reply: RowsReply, parsed: ParsedRepairs, ctx: GateContext) -> tuple[RowsReply, dict[str, Any]]:
    """``(repaired_reply, detail)``. Withdrawn rows leave the reply (their
    failure is kept by the caller); a fix or split the repair rule refuses
    leaves the original row as it was (module doc). ``detail["proposals"]``
    lists every fix / split proposed with its verdict (CONTENT — the harness
    and the shadow record keep it; counts never carry it)."""
    refused: list[tuple[str, str]] = list(parsed.refused)
    accepted: list[str] = []
    proposals: list[dict[str, Any]] = []
    facts: list[FactRow] = []
    taken = {f.id for f in reply.fact_rows}
    for f in reply.fact_rows:
        if f.id in parsed.withdraw:
            continue
        if f.id in parsed.split:
            parts, why = _split(f, parsed.split[f.id], ctx, taken)
            proposals.append({"id": f.id, "action": "split", "old": f.text, "refused": why,
                              "new": [dict(p) for p in parsed.split[f.id]]})
            if why:
                refused.append((f.id, why))
                facts.append(f)
            else:
                facts += parts
                accepted += [p.id for p in parts]
            continue
        new = parsed.fixed.get(f.id)
        if new is None:
            facts.append(f)
            continue
        row = _fact_from(f, new)
        why = "repair_unparseable" if row is None else repair_refusal(f, row, ctx)
        proposals.append({"id": f.id, "action": "fix", "old": f.text, "refused": why, "new": dict(new),
                          "old_quotes": [c.quote for c in f.cites]})
        if why:
            refused.append((f.id, why))
            facts.append(f)
            continue
        facts.append(replace(row, id=f.id, model_id=f.model_id))
        accepted.append(f.id)
    fact_text = {f.id: f.text for f in facts}
    absences: list[AbsenceRow] = []
    for a in reply.absence_rows:
        if a.id in parsed.withdraw:
            continue
        new = parsed.fixed.get(a.id)
        topic = str((new or {}).get("topic") or "").strip() if new else ""
        why = None if new is None else ("repair_unparseable" if not topic else text_refusal(a.topic, topic, ()))
        if new is None or why:
            if why:
                refused.append((a.id, why))
            absences.append(a)
            continue
        absences.append(replace(a, topic=topic))
        accepted.append(a.id)
    judgements: list[AssessmentRow] = []
    for j in reply.assessment_rows:
        if j.id in parsed.withdraw:
            continue
        new = parsed.fixed.get(j.id)
        if new is None:
            judgements.append(j)
            continue
        fixed = parse_rows_reply({"assessment_rows": [{"id": j.id, "text": j.text, "confidence": j.confidence_raw,
                                                      "rests_on": list(j.rests_on), **dict(new)}]})
        row = fixed.assessment_rows[0] if fixed and fixed.assessment_rows else None
        # a judgement's words may come from the fact rows it (now) rests on
        why = "repair_unparseable" if row is None else \
            text_refusal(j.text, row.text, [fact_text[x] for x in row.rests_on if x in fact_text])
        if why:
            refused.append((j.id, why))
            judgements.append(j)
            continue
        judgements.append(replace(row, id=j.id, model_id=j.model_id))
        accepted.append(j.id)
    restated: list[Restatement] = []
    for r in reply.restatements:
        if r.id in parsed.withdraw:
            continue
        new = parsed.fixed.get(r.id)
        if new is None:
            restated.append(r)
            continue
        fixed = parse_rows_reply({"restatements": [{"id": r.id, "ref": r.ref, "status": r.status, "text": r.text,
                                                   "bearing": list(r.bearing), **dict(new)}]})
        row = fixed.restatements[0] if fixed and fixed.restatements else None
        why = "repair_unparseable" if row is None else \
            text_refusal(r.text, row.text, [fact_text[x] for x in row.bearing if x in fact_text])
        if why:
            refused.append((r.id, why))
            restated.append(r)
            continue
        restated.append(replace(row, id=r.id, model_id=r.model_id))
        accepted.append(r.id)
    out = reply.with_rows(fact_rows=facts, absence_rows=absences, assessment_rows=judgements,
                          restatements=restated)
    return out, {"accepted": accepted, "withdrawn": sorted(parsed.withdraw),
                 "refused": dict(Counter(reason for _, reason in refused)),
                 "split": sum(1 for p in proposals if p["action"] == "split" and not p["refused"]),
                 "proposals": proposals}


def tally(outcomes: Sequence[RowOutcome]) -> dict[str, Any]:
    """Counts only: rows by outcome, by row kind, and failure reasons."""
    reasons: Counter[str] = Counter()
    for o in outcomes:
        if o.outcome in (FAILED, WITHDRAWN):
            for r in dict.fromkeys(o.reasons):
                reasons[r] += 1
    return {
        "rows": len(outcomes),
        "by_outcome": {k: sum(1 for o in outcomes if o.outcome == k) for k in OUTCOMES},
        "by_kind": dict(Counter(f"{o.row_kind}:{o.outcome}" for o in outcomes)),
        # counted APART: a surface subject is the entity layer's gap, not a key
        "surface_subject": dict(Counter(o.outcome for o in outcomes if is_surface(o))),
        "subject_found_by": dict(Counter(o.detail["subject_found_by"] for o in outcomes
                                         if o.outcome == ESTABLISHED_ELIGIBLE and o.detail.get("subject_found_by"))),
        "split_rows": sum(1 for o in outcomes if o.detail.get("split_from")),
        "failure_reasons": dict(reasons.most_common()),
        "repaired": sum(1 for o in outcomes if o.repaired),
    }


__all__ = [
    "ABSENCE",
    "ASSESSMENT",
    "DESK_GATES_VERSION",
    "ESTABLISHED_ELIGIBLE",
    "FAILED",
    "OUTCOMES",
    "REPAIR_INSTRUCTIONS",
    "RESTATABLE_REF_KINDS",
    "RESTATEMENT",
    "WITHDRAWN",
    "GateContext",
    "RowOutcome",
    "absence_text",
    "apply_repairs",
    "build_repair_prompt",
    "content_words",
    "failed_ids",
    "figure_values",
    "gate_absence",
    "gate_assessment",
    "gate_fact",
    "gate_reply",
    "gate_restatement",
    "is_surface",
    "located_quotes",
    "repair_refusal",
    "tally",
    "text_refusal",
]
