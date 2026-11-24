# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sentence and position tables, the consistency matrix and the origin count
— the pure CODE seats a round spec may name (plan P4 §4, lane G-3).

Every function takes the seat's handed inputs — a list of :class:`Handed`
outputs, exactly what the store released to it — and returns a payload of
its declared row type. Deterministic: the same inputs give the same rows in
the same order. Origin counting reuses the statement layer's G7
(``statement_independence.count_groups``), imported when it runs.
"""
from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .rows import sentences_of


@dataclass(frozen=True)
class Handed:
    """One output as handed to a reader: which read it answers, whose it
    is, its row type and payload (already cut to the reader's view)."""

    read: str
    ref_id: str  # the producing receipt's id ("input.<name>" for inputs)
    seat: str | None
    row_type: str | None
    payload: Any
    item: str | None = None
    sha256: str | None = None
    absent: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sentence table (the view judges read, and the code seat of that name)
# ---------------------------------------------------------------------------


def table_sentences(payload: Any) -> list[dict[str, Any]]:
    """A payload's sentences as table rows: key, text, stance, the cited rows
    with their quotes, probability. No prose, no summary."""
    out = []
    for s in sentences_of(payload):
        row: dict[str, Any] = {"key": str(s.get("key", "")), "text": str(s.get("text", "")),
                               "stance": s.get("stance", "neutral"),
                               "cited": [{k: v for k, v in r.items() if k in
                                          ("ref_id", "role", "kind", "quote")}
                                         for r in s.get("support") or ()]}
        if s.get("probability") is not None:
            row["probability"] = s["probability"]
        out.append(row)
    return out


def as_table_position(h: Handed) -> dict[str, Any] | None:
    if h.absent or not isinstance(h.payload, Mapping):
        return None
    pos: dict[str, Any] = {"position_id": h.ref_id, "seat": h.seat or "",
                           "sentences": table_sentences(h.payload)}
    if h.item is not None:
        pos["item"] = h.item
    if h.payload.get("probability") is not None:
        pos["probability"] = h.payload["probability"]
    return pos


def sentence_table(inputs: Sequence[Handed]) -> dict[str, Any]:
    """``sentence_table/1`` over every handed output that carries sentences
    (an already-tabled input contributes its positions as they are)."""
    positions: list[dict[str, Any]] = []
    for h in inputs:
        if isinstance(h.payload, Mapping) and h.row_type == "sentence_table/1":
            positions.extend(h.payload.get("positions") or ())
            continue
        pos = as_table_position(h)
        if pos is not None and pos["sentences"]:
            positions.append(pos)
    return {"positions": positions}


def table_view(h: Handed) -> dict[str, Any]:
    """The ``view: sentence_table`` form of one handed output."""
    return sentence_table([h])


# ---------------------------------------------------------------------------
# Panel candidates
# ---------------------------------------------------------------------------


def candidates_of(outputs: Sequence[Handed]) -> list[str]:
    """What a panel rules on: each position of a sentence table, else each
    output. Ids are stable (receipt ids / position ids)."""
    out: list[str] = []
    for h in outputs:
        if h.absent:
            continue
        if isinstance(h.payload, Mapping) and h.row_type == "sentence_table/1":
            out.extend(str(p.get("position_id")) for p in h.payload.get("positions") or ())
        else:
            out.append(h.ref_id)
    return out


def stance_signature(payload: Any) -> tuple[tuple[str, str], ...]:
    """What ``unchanged`` compares between rebuttal passes: each sentence's
    key and stance."""
    return tuple(sorted((str(s.get("key", "")), str(s.get("stance", "neutral")))
                        for s in sentences_of(payload)))


def support_count(h: Handed) -> int:
    """The ``support_count`` score function (``select … by: support_count``)."""
    return sum(len(s.get("support") or ()) for s in sentences_of(h.payload))


SCORE_FNS: dict[str, Callable[[Handed], int]] = {"support_count": support_count}


# ---------------------------------------------------------------------------
# Evidence rows handed to a seat
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]{0,400}>")


def readable_text(text: str | None) -> str:
    """A row's stored text as a reader sees it: markup tags dropped, HTML
    entities decoded (``&nbsp;`` → a space), whitespace collapsed. Rows are
    handed to seats in this form and the round's quote gate checks quotes
    against the same form, so a seat that copies what it reads passes (lane
    G-6: a stored ``to&nbsp;pass`` refused every collector's exact quote).
    The record's own text is not changed; the shadow statement gate still
    checks the stored text under the house fold."""
    if not text:
        return ""
    out = html.unescape(_TAG_RE.sub(" ", str(text)))
    return " ".join(out.split())


def evidence_rows(inputs: Sequence[Handed]) -> dict[str, dict[str, Any]]:
    """Every substrate row (``rows: [...]`` with an id) in the handed
    payloads, by id; the first occurrence wins."""
    out: dict[str, dict[str, Any]] = {}

    def visit(obj: Any) -> None:
        if isinstance(obj, Mapping):
            for r in obj.get("rows") or ():
                if isinstance(r, Mapping) and isinstance(r.get("id"), str):
                    out.setdefault(r["id"], dict(r))
        if isinstance(obj, Mapping):
            for v in obj.values():
                if isinstance(v, (Mapping, list, tuple)):
                    visit(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                visit(v)

    for h in inputs:
        visit(h.payload)
    return out


# ---------------------------------------------------------------------------
# merge_and_count_origins
# ---------------------------------------------------------------------------


def merge_and_count_origins(inputs: Sequence[Handed]) -> dict[str, Any]:
    """``evidence_table/1``: the rows the collectors cited (all handed rows if
    none were cited), with independent origin groups counted by the
    statement layer's G7 (near-duplicates collapsed)."""
    from ..statement_independence import count_groups
    from ..statement_shapes import EvidenceTexts, SupportRow

    rows = evidence_rows(inputs)
    cited: list[str] = []
    for h in inputs:
        for s in sentences_of(h.payload):
            for r in s.get("support") or ():
                ref = str(r.get("ref_id"))
                if ref in rows and ref not in cited:
                    cited.append(ref)
    keep = [rows[i] for i in (cited or list(rows))]
    support = [SupportRow(role="evidence", support_kind=str(r.get("kind", "signal")),
                          ref_id=str(r["id"]), quote=r.get("text"),
                          source_id=r.get("source_id"), origin_group_id=r.get("origin_group_id"),
                          access_class=r.get("access_class")) for r in keep]
    texts = EvidenceTexts(by_signal={str(r["id"]): str(r.get("text") or "") for r in keep})
    count = count_groups(support, texts)
    series: list[Any] = []
    for h in inputs:
        if h.row_type == "series_panel/1" and isinstance(h.payload, Mapping):
            series.extend(h.payload.get("series") or ())
    return {"rows": keep, "origin_groups": count.groups,
            "checkable_groups": count.checkable_groups, "basis": count.basis, "series": series}


# ---------------------------------------------------------------------------
# consistency_matrix
# ---------------------------------------------------------------------------


def consistency_matrix(inputs: Sequence[Handed]) -> dict[str, Any]:
    """``matrix/1`` from ``assessment/1`` outputs: hypotheses × rows with the
    marks the assessors gave (a later pass overrides an earlier one for the
    same cell). Ranked least-inconsistent first (competing-hypotheses
    practice); ``top_two_separated`` is true when the first has strictly
    fewer inconsistent rows than the second. A tie is not separated."""
    cells: dict[str, dict[str, str]] = {}
    rows: list[str] = []
    for h in inputs:
        if h.row_type != "assessment/1" or not isinstance(h.payload, Mapping):
            continue
        hyp = str(h.payload.get("hypothesis"))
        cells.setdefault(hyp, {})
        for m in h.payload.get("marks") or ():
            ref = str(m.get("ref_id"))
            cells[hyp][ref] = str(m.get("mark"))
            if ref not in rows:
                rows.append(ref)
    hyps = sorted(cells)
    inconsistency = {hyp: sum(v == "inconsistent" for v in cells[hyp].values()) for hyp in hyps}
    ranking = sorted(hyps, key=lambda hyp: (inconsistency[hyp], hyp))
    separated = len(ranking) < 2 or inconsistency[ranking[0]] < inconsistency[ranking[1]]
    return {"hypotheses": hyps, "rows": rows, "cells": cells, "inconsistency": inconsistency,
            "ranking": ranking, "top_two_separated": bool(hyps) and separated}


#: The pure code functions, by the name a spec's ``fn`` uses.
PURE_FNS: dict[str, Callable[[Sequence[Handed]], dict[str, Any]]] = {
    "sentence_table": sentence_table,
    "merge_and_count_origins": merge_and_count_origins,
    "consistency_matrix": consistency_matrix,
}
