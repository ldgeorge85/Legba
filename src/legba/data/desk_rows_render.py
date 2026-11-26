# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PROSE LAST — the read's text rendered from its rows.

A contract-v2 read is never written as prose by the model. Its text is
RENDERED, deterministically, from the rows that passed their gates (and, for a
fact row the verifier saw, that it did not reject), in the house read
contract's sections:

* the as-of line — the COMPUTED collection facts (rows, sources, origin
  groups, window), in italics, alone on the first line;
* ``**BLUF:**`` — the model's optional LEAD, kept only sentence by sentence as
  it maps onto rows (below); with no kept lead, the first judgement row, else
  the first fact row;
* ``## What changed`` — the fact rows, grouped by subject in the order the
  subjects were offered, each ending in its ``[N]`` markers; then the prior
  read's points carried forward (confirmed / revised / no longer supported,
  with the markers of the rows that bear on them); then what was looked for
  and not found, scoped as code wrote it;
* ``## Why it matters`` — each judgement with its confidence word and the
  markers of the fact rows it rests on;
* ``## What would change this read`` — the observation, its reporting class,
  whether this desk collects it;
* ``## Indicators`` — the signposts.

THE LEAD CHECK (:func:`check_lead`). Each sentence of the model's lead must
MAP to rows: at least :data:`LEAD_MIN_CONTAINMENT` of its content words must
be in the text of at most :data:`LEAD_MAX_ROWS` rendered rows, and every figure
and date it states must be in those rows' texts. A sentence that maps is kept
and given the markers of the rows it maps to; one that does not is DROPPED and
counted — the lead may only restate rows.

THE LEAD IS BUILT BY CODE (:func:`code_lead`). An earlier model-written lead kept 13 of 65
model-written lead sentences: the rest framed or judged beyond the rows (16),
restated rows that had failed (13), synthesised across more than two rows (8),
paraphrased (6) or carried a figure or date no shown row states (8). So the
BLUF is now built deterministically from the TOP-RANKED rows — the first
judgement that rests on a shown fact, then the top fact row (verifier-
supported first, then the desk's own subject, then the most rows cited) —
each with its markers. A model lead is used only when the caller opts in
(``model_lead=True``) AND every sentence of it passes the check; the check is
run and counted either way.

No I/O; no table.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .desk_rows_contract import RowsReply
from .desk_rows_gates import (
    ABSENCE,
    ASSESSMENT,
    ESTABLISHED_ELIGIBLE,
    RESTATEMENT,
    RowOutcome,
    content_words,
    figure_values,
)
from .statement_content import find_dates, strip_markers

LEAD_MIN_CONTAINMENT = 0.6
LEAD_MAX_ROWS = 2
#: Verifier verdicts that keep a fact row out of the rendered read.
REJECTED_VERDICTS = frozenset({"does_not_support", "partly"})


@dataclass(frozen=True)
class LeadCheck:
    kept: tuple[tuple[str, tuple[str, ...]], ...]      # (sentence, row ids)
    dropped: tuple[str, ...]
    #: Which lead the read shows: ``code`` (built from the top rows),
    #: ``model`` (the model's, every sentence passed and the caller opted in)
    #: or ``none`` (nothing passed).
    source: str = "code"

    def as_counts(self) -> dict[str, Any]:
        return {"lead_sentences": len(self.kept) + len(self.dropped), "kept": len(self.kept),
                "dropped": len(self.dropped), "lead_source": self.source}


def _markers(ordinals: Sequence[int]) -> str:
    return "".join(f"[{n}]" for n in dict.fromkeys(o for o in ordinals if o))


def _day(iso: Any) -> str:
    return str(iso)[:10] if iso else "?"


def rendered_rows(outcomes: Sequence[RowOutcome], verdicts: Mapping[str, str] | None = None) -> list[RowOutcome]:
    """The rows the read shows: passed rows, minus fact rows the verifier
    rejected, minus judgements left resting on no shown fact."""
    verdicts = verdicts or {}
    shown = [o for o in outcomes if o.outcome in (ESTABLISHED_ELIGIBLE, ABSENCE, RESTATEMENT, ASSESSMENT)
             and not (o.row_kind == "fact" and verdicts.get(o.row_id) in REJECTED_VERDICTS)]
    fact_ids = {o.row_id for o in shown if o.row_kind == "fact"}
    return [o for o in shown if o.row_kind != "assessment"
            or any(p in fact_ids for p in (o.detail.get("premises_passed") or ()))]


def check_lead(lead: str, rows: Sequence[RowOutcome]) -> LeadCheck:
    """Keep each lead sentence that restates at most two shown rows; drop the
    rest (see the module doc)."""
    from .analysts.assessment_unsupported import segment_sentences

    kept: list[tuple[str, tuple[str, ...]]] = []
    dropped: list[str] = []
    texts = {o.row_id: o.text for o in rows if o.text}
    for s in segment_sentences(strip_markers(lead or "")):
        sentence = s.text.strip()
        if not sentence or sentence.startswith("#"):
            continue
        words = content_words(sentence)
        if not words:
            dropped.append(sentence)
            continue
        ranked = sorted(texts, key=lambda rid: -len(words & content_words(texts[rid])))
        best: tuple[str, ...] = ()
        for k in range(1, LEAD_MAX_ROWS + 1):
            pick = tuple(ranked[:k])
            union: set[str] = set().union(*(content_words(texts[r]) for r in pick)) if pick else set()
            if pick and len(words & union) / len(words) >= LEAD_MIN_CONTAINMENT:
                best = pick
                break
        if not best:
            dropped.append(sentence)
            continue
        joined = " ".join(texts[r] for r in best)
        figs_ok = not (figure_values(sentence) - figure_values(joined))
        row_dates = {(d.month, d.day) for d in find_dates(joined)}
        dates_ok = all((d.month, d.day) in row_dates for d in find_dates(sentence))
        if figs_ok and dates_ok:
            kept.append((sentence, best))
        else:
            dropped.append(sentence)
    return LeadCheck(tuple(kept), tuple(dropped))


def _rank_facts(facts: Sequence[RowOutcome], order: Mapping[str, int],
                verdicts: Mapping[str, str]) -> list[RowOutcome]:
    def key(o: RowOutcome) -> tuple[Any, ...]:
        d = o.result.draft if o.result else None
        num = int(o.row_id[1:]) if o.row_id[1:].isdigit() else 999
        return (verdicts.get(o.row_id) != "supports", order.get(d.subject_key if d else "", len(order)),
                -len(set(o.cited)), num)
    return sorted(facts, key=key)


def code_lead(rows: Sequence[RowOutcome], *, subject_order: Sequence[str] = (),
              verdicts: Mapping[str, str] | None = None) -> str:
    """The BLUF built from the top-ranked shown rows (module doc), with
    markers; a fixed sentence when nothing passed."""
    by_id = {o.row_id: o for o in rows}
    order = {k: i for i, k in enumerate(subject_order)}
    facts = _rank_facts([o for o in rows if o.row_kind == "fact"], order, verdicts or {})
    judgement = next((o for o in rows if o.row_kind == "assessment"), None)
    parts: list[str] = []
    if judgement is not None:
        parts.append(f"{judgement.text.rstrip()} ({judgement.detail.get('confidence')}) "
                     f"{_markers(_premise_markers(judgement, by_id))}".strip())
    if facts:
        parts.append(f"{facts[0].text.rstrip()} {_markers(facts[0].cited)}".strip())
    return " ".join(parts) or "Nothing in this window passed the desk's checks."


def render_read(
    reply: RowsReply,
    outcomes: Sequence[RowOutcome],
    computed: Mapping[str, Any],
    *,
    subject_order: Sequence[str] = (),
    subject_names: Mapping[str, str] | None = None,
    verdicts: Mapping[str, str] | None = None,
    model_lead: bool = False,
) -> tuple[str, LeadCheck]:
    """``(body, lead_check)`` — the read's markdown, rendered from its rows."""
    rows = rendered_rows(outcomes, verdicts)
    by_id = {o.row_id: o for o in rows}
    facts = [o for o in rows if o.row_kind == "fact"]
    lead = check_lead(reply.lead, rows)
    as_of = (f"*As of {_day(computed.get('window_end'))} — this desk's collection: "
             f"{computed.get('rows_handed', 0)} signals from {computed.get('distinct_sources', 0)} sources "
             f"({computed.get('distinct_origin_groups', 0)} origin groups), "
             f"{_day(computed.get('window_start'))} to {_day(computed.get('window_end'))}"
             + (f"; {computed['not_read']} in-window signals not read" if computed.get("not_read") else "")
             + _roster_clause(computed) + ".*")
    out = [as_of, ""]
    if model_lead and lead.kept and not lead.dropped:
        bluf = " ".join(s + " " + _markers([n for r in ids for n in by_id[r].cited]) for s, ids in lead.kept)
        lead = LeadCheck(lead.kept, lead.dropped, "model")
    else:
        bluf = code_lead(rows, subject_order=subject_order, verdicts=verdicts)
        lead = LeadCheck(lead.kept, lead.dropped, "code" if rows else "none")
    out += [f"**BLUF:** {bluf.strip()}", "", "## What changed", ""]
    order = {k: i for i, k in enumerate(subject_order)}
    subject_names = subject_names or {}

    def _subj(o: RowOutcome) -> str:
        d = o.result.draft if o.result else None
        return d.subject_key if d else ""

    facts.sort(key=lambda o: (order.get(_subj(o), len(order)), o.row_id))
    last = None
    for o in facts:
        s = _subj(o)
        if s != last and len(order) > 1:
            out.append(f"**{subject_names.get(s, s)}**")
            last = s
        out.append(f"- {o.text} {_markers(o.cited)}")
    for o in (x for x in rows if x.row_kind == "restatement"):
        status = str(o.detail.get("status") or "").replace("_", " ")
        bear = [n for b in o.detail.get("backing") or () if b in by_id for n in by_id[b].cited]
        out.append(f"- From the prior read {_markers(o.cited)}, {status}: {o.text} {_markers(bear)}".rstrip())
    for o in (x for x in rows if x.row_kind == "absence"):
        out.append(f"- {o.text}")
    if not facts and not any(x.row_kind in ("restatement", "absence") for x in rows):
        out.append("- Nothing in this window passed the desk's checks.")
    out += ["", "## Why it matters", ""]
    judgements = [o for o in rows if o.row_kind == "assessment"]
    for o in judgements:
        out.append(f"{o.text} ({o.detail.get('confidence')}) {_markers(_premise_markers(o, by_id))}".strip())
        out.append("")
    if not judgements:
        out += ["No judgement in this window rests on a row that passed its checks.", ""]
    wc = reply.would_change
    out += ["## What would change this read", ""]
    if wc and wc.observation:
        collects = {"yes": "this desk collects that class", "no": "this desk does not collect that class",
                    "unknown": "whether this desk collects that class is not known"}[wc.desk_collects]
        out += [f"{wc.observation} — {wc.reporting_class or 'reporting class not named'}; {collects}.", ""]
    else:
        out += ["Not stated.", ""]
    out += ["## Indicators", ""]
    out += [f"- {i}" for i in reply.indicators] or ["- None stated."]
    return "\n".join(out).rstrip() + "\n", lead


def _roster_clause(computed: Mapping[str, Any]) -> str:
    """The roster state in the as-of line, when the run read it (code-written;
    counts only — the ids stay in the computed block)."""
    size = computed.get("roster_size")
    if not size or computed.get("sources_failing") is None:
        return ""
    failing, silent = len(computed.get("sources_failing") or ()), len(computed.get("sources_silent") or ())
    return (f"; of the {size} feeds in this desk's source pool, {failing} failing and {silent} polled "
            f"with nothing new in the window")


def _premise_markers(o: RowOutcome, by_id: Mapping[str, RowOutcome]) -> list[int]:
    if o.row_kind != "assessment":
        return list(o.cited)
    return [n for p in o.detail.get("premises_passed") or () if p in by_id for n in by_id[p].cited]


__all__ = [
    "LEAD_MAX_ROWS",
    "LEAD_MIN_CONTAINMENT",
    "REJECTED_VERDICTS",
    "LeadCheck",
    "check_lead",
    "code_lead",
    "render_read",
    "rendered_rows",
]
