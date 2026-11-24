# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cited sentences from a persisted desk finding — the statement step's input.

Plan P1 §2.4 / S2: desk findings already persist their resolved citations in
``data['citations']`` beside the prose (``inline_target._extract_citations``:
one entry per distinct ``[N]`` marker that resolved). The statement step reads
those; it does not re-parse prose for ids. This module joins the two: it
splits the finding body into sentences, keeps the sentences that carry
``[N]`` markers, and resolves each marker through the persisted entries.

What a marker resolves to, by the entry's shape:

* ``signal_id`` present — a signal, evidence-eligible;
* ``ref_kind == 'observation'`` with a ``ref_id`` — an observation vintage,
  evidence-eligible;
* any other prebuilt grounding entry (a prior read, a ledger block, …) — not
  evidence: a prior read is the desk's own earlier conclusion (gate G8), so it
  is carried with ``evidence_eligible=False`` and never offered as support;
* no entry — an unresolved marker, listed on the sentence (gate G3: a marker
  that does not resolve to a handed row is refused upstream of the gate).

Segmentation is the house one (``assessment_unsupported.segment_sentences``,
abbreviation-aware via ``assembly_spans.first_sentence_end``), imported at
call time so this module — and the gate leaf beside it — stay cheap to import.
No I/O, no model.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .provenance.citation_markers import normalize_inline_citation_markers
from .statement_content import strip_markers
from .statement_shapes import HandedSlice

#: The same bare-ordinal marker ``inline_target`` resolves (``[3]`` only).
_MARKER_RE = re.compile(r"\[(\d+)\]")
_MARKER_KEY_RE = re.compile(r"^\[(\d+)\]$")

#: ``ref_kind`` values that are evidence-eligible substrate rows.
EVIDENCE_REF_KINDS = frozenset({"signal", "observation"})


@dataclass(frozen=True)
class CitedRef:
    """One resolved ``[N]`` marker."""

    ordinal: int
    ref_kind: str
    ref_id: str | None
    source_id: str | None = None
    title: str | None = None
    evidence_eligible: bool = False


@dataclass(frozen=True)
class CitedSentence:
    """One sentence of a finding that carries citation markers.

    ``text`` is the sentence as written (markers normalised to ASCII);
    ``plain`` the same with markers removed — what a typing seat types and
    what G2 reads numbers from."""

    index: int
    text: str
    plain: str
    refs: tuple[CitedRef, ...]
    unresolved: tuple[int, ...] = ()

    @property
    def evidence_refs(self) -> tuple[CitedRef, ...]:
        return tuple(r for r in self.refs if r.evidence_eligible)


def _ordinal(entry: Mapping[str, Any]) -> int | None:
    raw = entry.get("ordinal")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    m = _MARKER_KEY_RE.match(str(entry.get("marker") or "").strip())
    return int(m.group(1)) if m else None


def _ref(ordinal: int, entry: Mapping[str, Any]) -> CitedRef:
    title = entry.get("title")
    source_id = entry.get("source_id")
    common = {
        "title": str(title) if title else None,
        "source_id": str(source_id) if source_id else None,
    }
    if entry.get("signal_id"):
        return CitedRef(ordinal, "signal", str(entry["signal_id"]), evidence_eligible=True, **common)
    kind = str(entry.get("ref_kind") or entry.get("grounding") or "unknown")
    ref_id = entry.get("ref_id")
    eligible = kind in EVIDENCE_REF_KINDS and bool(ref_id)
    return CitedRef(ordinal, kind, str(ref_id) if ref_id else None,
                    evidence_eligible=eligible, **common)


def citation_index(citations: Iterable[Mapping[str, Any]] | None) -> dict[int, CitedRef]:
    """``{ordinal: CitedRef}`` from a finding's ``data['citations']``; the
    first entry for an ordinal wins (``inline_target`` emits one per marker)."""
    out: dict[int, CitedRef] = {}
    for entry in citations or ():
        if not isinstance(entry, Mapping):
            continue
        n = _ordinal(entry)
        if n is not None and n not in out:
            out[n] = _ref(n, entry)
    return out


def _segments(body: str) -> list[tuple[int, str]]:
    from .analysts.assessment_unsupported import segment_sentences

    return [(s.index, s.text) for s in segment_sentences(body)]


def extract_cited_sentences(
    body: str | None, citations: Iterable[Mapping[str, Any]] | None,
) -> list[CitedSentence]:
    """Every marker-carrying sentence of ``body`` with its resolved refs.

    Headings are skipped; sentences with no ``[N]`` marker are skipped (an
    uncited sentence has nothing for the gate to check)."""
    text = normalize_inline_citation_markers(body or "")
    index = citation_index(citations)
    out: list[CitedSentence] = []
    for i, sentence in _segments(text):
        if sentence.lstrip().startswith("#"):
            continue
        ordinals = list(dict.fromkeys(int(m.group(1)) for m in _MARKER_RE.finditer(sentence)))
        if not ordinals:
            continue
        refs = tuple(index[n] for n in ordinals if n in index)
        unresolved = tuple(n for n in ordinals if n not in index)
        out.append(CitedSentence(i, sentence, strip_markers(sentence), refs, unresolved))
    return out


def extract_from_finding(body: str | None, data: Mapping[str, Any] | None) -> list[CitedSentence]:
    """:func:`extract_cited_sentences` over a finding's body and ``data``."""
    citations = (data or {}).get("citations")
    if not isinstance(citations, Sequence) or isinstance(citations, (str, bytes)):
        citations = ()
    return extract_cited_sentences(body, citations)


def handed_from_citations(
    citations: Iterable[Mapping[str, Any]] | None, run_id: str | None = None,
) -> HandedSlice:
    """The G3 handed set a finding's own citations prove: every evidence-
    eligible id its render-time index resolved (those rows were in the slice
    the run was handed, by construction of ``data['citations']``)."""
    ids = [r.ref_id for r in citation_index(citations).values() if r.evidence_eligible]
    return HandedSlice.of(ids, run_id)


__all__ = [
    "EVIDENCE_REF_KINDS",
    "CitedRef",
    "CitedSentence",
    "citation_index",
    "extract_cited_sentences",
    "extract_from_finding",
    "handed_from_citations",
]
