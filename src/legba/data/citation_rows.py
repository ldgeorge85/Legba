# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows: what a finding's sentences rest on, as plain data.

Pure: no I/O, no model. :func:`plan_citation_rows` turns a finding's body and
its persisted ``data['citations']`` (plus, for a desk run, the index the run
was HANDED) into the rows ``legba.data.citation_rows_store`` inserts beside
the finding:

* a :class:`SentenceRow` for every sentence of the body, in order, with its
  offsets into the stored body;
* a :class:`SupportRow` for every citation a sentence carries: "sentence k
  rests on record r", r named by kind and id (one typed key column per kind);
* an :class:`UnresolvedRow` for every marker or entry that names nothing,
  with the reason (logged, not refused, in this lane);
* :class:`HandedGroup` rows: what the run was handed, typed by kind.

What a citation entry points at is DECLARED, not hand-listed here: each kind
is a ``citation_target`` in the type registry (``table``, ``column``,
``ref_kinds``, ``id_fields``, ``member_fields``, ``restates``). An entry is
resolved by its shape:

* ``signal_id`` present -> a signal (an event-expanded entry also carries
  ``ref_kind='event'``: the signal is the target, ``via_block='event'``);
* ``ref_kind`` in a target's ``ref_kinds`` -> that target, its id from the
  first of ``id_fields`` the entry carries;
* ``ref_kind`` naming a grounding BLOCK in a target's ``member_fields`` -> one
  row per member id, ``via_block`` = the block (the block is N records;
  pointing at one would be a fabricated anchor, ``unit_grounding``);
* anything else -> unresolved, with the reason.

Markers: the unit marker ``[N]`` and the composition marker ``[[ref:N]]``,
read after the house's write-time normaliser
(``provenance.citation_markers.normalize_inline_citation_markers``), so the
spelling a sentence is matched on is the spelling the citation entry carries.
Segmentation is the house one (``assessment_unsupported.segment_sentences``),
imported at call time: the registry image never reaches it.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .provenance.citation_markers import normalize_inline_citation_markers

__all__ = [
    "CitationPlan",
    "HandedGroup",
    "HandedRef",
    "PREBUILT_INDEX_KEY",
    "ROLE_BASIS",
    "ROLE_CITES",
    "ROLE_RESTATES",
    "SentenceRow",
    "SupportRow",
    "Target",
    "TargetSpec",
    "UNRESOLVED_REASONS",
    "UnresolvedRow",
    "canonical_markers",
    "group_handed",
    "handed_from_index",
    "plan_citation_rows",
    "target_specs",
    "targets_of_entry",
]

ROLE_CITES = "cites"
ROLE_RESTATES = "restates"
ROLE_BASIS = "basis"

#: The closed reasons (the migration's CHECK list).
UNRESOLVED_REASONS: tuple[str, ...] = (
    "marker_not_in_citations", "entry_without_target", "unknown_ref_kind",
    "malformed_id", "target_missing",
)

#: The key under which ``inline_target`` carries an already-built citation for
#: a grounding block in its render index (``_PREBUILT_CITATION_KEY``).
PREBUILT_INDEX_KEY = "_citation"

#: ``[[ref:N]]`` (composition) or ``[N]`` (unit) — the two canonical markers.
_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]|\[(\d+)\]")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Targets (from the type registry)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetSpec:
    kind: str
    table: str
    column: str
    key: str
    material: bool
    ref_kinds: tuple[str, ...]
    id_fields: tuple[str, ...]
    member_fields: Mapping[str, str]
    restates: tuple[str, ...]


def target_specs() -> dict[str, TargetSpec]:
    """``{kind: TargetSpec}`` from the ``citation_target`` family."""
    from .type_registry import types

    return {
        d.id: TargetSpec(
            kind=d.id, table=d.attr("table"), column=d.attr("column"), key=d.attr("key"),
            material=bool(d.attr("material")), ref_kinds=tuple(d.attr("ref_kinds")),
            id_fields=tuple(d.attr("id_fields")), member_fields=dict(d.attr("member_fields")),
            restates=tuple(d.attr("restates")),
        )
        for d in types("citation_target")
    }


@dataclass(frozen=True)
class Target:
    """One record a citation points at."""

    kind: str
    column: str
    key: str  # the id (uuid text, lower-case) or the legacy text key
    via_block: str | None = None
    restates: bool = False


@dataclass(frozen=True)
class _Problem:
    reason: str
    ref_kind: str | None
    ref_value: str | None


def _norm_key(spec: TargetSpec, raw: Any) -> str | None:
    """The canonical key, or ``None`` when ``raw`` is not a valid one."""
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    if spec.key == "text":
        return text[:512]
    try:
        return str(UUID(text))
    except (ValueError, AttributeError, TypeError):
        return None


def targets_of_entry(
    entry: Mapping[str, Any], specs: Mapping[str, TargetSpec],
) -> tuple[list[Target], list[_Problem]]:
    """What ONE citation entry points at, and what in it names nothing."""
    by_ref = {rk: s for s in specs.values() for rk in s.ref_kinds}
    by_block = {blk: (s, fld) for s in specs.values() for blk, fld in s.member_fields.items()}
    ref_kind = entry.get("ref_kind")
    ref_kind = str(ref_kind) if ref_kind else None
    signal = specs.get("signal")
    if signal is not None and entry.get("signal_id"):
        key = _norm_key(signal, entry.get("signal_id"))
        if key is None:
            return [], [_Problem("malformed_id", "signal", str(entry.get("signal_id"))[:200])]
        via = "event" if ref_kind == "event" else None
        return [Target("signal", signal.column, key, via)], []
    if ref_kind is None:
        return [], [_Problem("entry_without_target", None, None)]
    if ref_kind in by_block:
        spec, fld = by_block[ref_kind]
        members = entry.get(fld)
        if not isinstance(members, (list, tuple)) or not members:
            return [], [_Problem("entry_without_target", ref_kind, None)]
        out: list[Target] = []
        problems: list[_Problem] = []
        restates = ref_kind in spec.restates
        for raw in dict.fromkeys(str(m) for m in members if m is not None):
            key = _norm_key(spec, raw)
            if key is None:
                problems.append(_Problem("malformed_id", ref_kind, raw[:200]))
            else:
                out.append(Target(spec.kind, spec.column, key, ref_kind, restates))
        return out, problems
    spec = by_ref.get(ref_kind)
    if spec is None:
        return [], [_Problem("unknown_ref_kind", ref_kind, None)]
    raw = next((entry.get(f) for f in spec.id_fields if entry.get(f)), None)
    if raw is None:
        return [], [_Problem("entry_without_target", ref_kind, None)]
    key = _norm_key(spec, raw)
    if key is None:
        return [], [_Problem("malformed_id", ref_kind, str(raw)[:200])]
    return [Target(spec.kind, spec.column, key, None, ref_kind in spec.restates)], []


# ---------------------------------------------------------------------------
# What the run was handed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandedRef:
    """One record the run was shown, at ordinal ``ordinal`` of its index."""

    ordinal: int
    kind: str
    key: str
    via_block: str | None = None


@dataclass(frozen=True)
class HandedGroup:
    kind: str
    column: str
    via_block: str | None
    keys: tuple[str, ...]
    ordinals: tuple[int, ...]


def handed_from_index(
    index: Mapping[int, Mapping[str, Any]] | None,
    specs: Mapping[str, TargetSpec] | None = None,
) -> tuple[HandedRef, ...]:
    """The typed handed list from a desk run's render index (``{N: entry}``):
    a signal row's field bag (``signal_id``), or a grounding block's finished
    citation under :data:`PREBUILT_INDEX_KEY`. An entry that names no record
    contributes nothing (it is not a record; its marker, if cited, is logged
    unresolved at write)."""
    if not index:
        return ()
    specs = specs if specs is not None else target_specs()
    out: list[HandedRef] = []
    for n in sorted(k for k in index if isinstance(k, int)):
        entry = index[n]
        if not isinstance(entry, Mapping):
            continue
        prebuilt = entry.get(PREBUILT_INDEX_KEY)
        citation = prebuilt if isinstance(prebuilt, Mapping) else entry
        targets, _ = targets_of_entry(citation, specs)
        out.extend(HandedRef(n, t.kind, t.key, t.via_block) for t in targets)
    return tuple(out)


def group_handed(
    handed: Iterable[HandedRef], specs: Mapping[str, TargetSpec],
) -> list[HandedGroup]:
    """One group per (kind, via_block), keys and ordinals parallel, in order."""
    groups: dict[tuple[str, str | None], tuple[list[str], list[int]]] = {}
    for ref in handed:
        if ref.kind not in specs:
            continue
        keys, ords = groups.setdefault((ref.kind, ref.via_block), ([], []))
        keys.append(ref.key)
        ords.append(int(ref.ordinal))
    return [
        HandedGroup(kind, specs[kind].column, via, tuple(keys), tuple(ords))
        for (kind, via), (keys, ords) in groups.items()
    ]


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SentenceRow:
    ordinal: int
    char_start: int
    char_end: int
    text: str
    text_sha256: str
    kind: str | None
    marker_count: int


@dataclass(frozen=True)
class SupportRow:
    sentence_ordinal: int | None
    marker: str | None
    role: str
    target: Target
    quote: str | None = None
    shown_field: str | None = None
    shown_sha256: str | None = None
    stance: str | None = None
    speaker: str | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class UnresolvedRow:
    sentence_ordinal: int | None
    marker: str | None
    reason: str
    ref_kind: str | None = None
    ref_value: str | None = None


@dataclass(frozen=True)
class CitationPlan:
    sentences: tuple[SentenceRow, ...]
    supports: tuple[SupportRow, ...]
    unresolved: tuple[UnresolvedRow, ...]
    handed: tuple[HandedGroup, ...] | None
    markers: int
    handed_count: int = 0
    #: Markers of the other convention (a unit [N] inside a composition).
    foreign_markers: int = 0

    @property
    def cited_sentences(self) -> int:
        return sum(1 for s in self.sentences if s.marker_count)


def canonical_markers(text: str) -> list[str]:
    """The distinct canonical markers in ``text``, in order (after the house
    normaliser): ``'[3]'`` or ``'[[ref:3]]'``."""
    out: list[str] = []
    for m in _MARKER_RE.finditer(normalize_inline_citation_markers(text or "")):
        tok = f"[[ref:{m.group(1)}]]" if m.group(1) is not None else f"[{m.group(2)}]"
        if tok not in out:
            out.append(tok)
    return out


def _entry_marker(entry: Mapping[str, Any]) -> str | None:
    raw = entry.get("marker")
    if isinstance(raw, str) and raw.strip():
        found = canonical_markers(raw.strip())
        return found[0] if len(found) == 1 else None
    ordinal = entry.get("ordinal")
    if isinstance(ordinal, int) and not isinstance(ordinal, bool):
        return f"[{ordinal}]"
    return None


def _shown(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for name in ("evidence_text", "source_text", "snippet"):
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            return name, _sha(value)
    return None, None


def _stance(entry: Mapping[str, Any], target: Target) -> tuple[str | None, str | None]:
    if target.kind != "signal":
        return None, None
    claim = entry.get("claim")
    if isinstance(claim, Mapping):
        claimant = claim.get("claimant")
        return "claim", (str(claimant)[:200] if claimant else None)
    return "report", None


def _segments(body: str) -> list[tuple[int, int, int, str]]:
    from .analysts.assessment_unsupported import segment_sentences

    return [(s.index, s.start, s.end, s.text) for s in segment_sentences(body)]


_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_LEADING_MARKERS_RE = re.compile(r"^\s*(?:(?:\[\[ref:\d+\]\]|\[\d+\])\s*)+")


def plan_citation_rows(
    body: str | None,
    citations: Sequence[Mapping[str, Any]] | None,
    *,
    handed: Sequence[HandedRef] | None = None,
    specs: Mapping[str, TargetSpec] | None = None,
    segment: Callable[[str], list[tuple[int, int, int, str]]] | None = None,
) -> CitationPlan:
    """The rows for one finding. ``handed`` is ``None`` when the writer was not
    given the run's index (every non-desk writer today)."""
    specs = specs if specs is not None else target_specs()
    # Segment the NORMALISED body: the normaliser also pulls a marker written
    # after the full stop back inside its sentence, so a marker is attributed
    # to the sentence it closes. Offsets are into the normalised text, which is
    # the stored body for every desk finding (inline_target persists it so).
    text = normalize_inline_citation_markers(body or "")
    entries = [e for e in (citations or ()) if isinstance(e, Mapping)]
    by_marker: dict[str, Mapping[str, Any]] = {}
    unmarked: list[Mapping[str, Any]] = []
    for entry in entries:
        marker = _entry_marker(entry)
        if marker is None:
            unmarked.append(entry)
        elif marker not in by_marker:
            by_marker[marker] = entry
    sentences: list[SentenceRow] = []
    supports: list[SupportRow] = []
    unresolved: list[UnresolvedRow] = []
    used: set[str] = set()
    markers = 0

    def _emit(entry: Mapping[str, Any], ordinal: int | None, marker: str | None, role: str | None) -> None:
        targets, problems = targets_of_entry(entry, specs)
        for p in problems:
            unresolved.append(UnresolvedRow(ordinal, marker, p.reason, p.ref_kind, p.ref_value))
        shown_field, shown_sha = _shown(entry)
        quote = entry.get("quote")
        for t in targets:
            stance, speaker = _stance(entry, t)
            supports.append(SupportRow(
                ordinal, marker,
                role or (ROLE_RESTATES if t.restates else ROLE_CITES), t,
                quote=str(quote) if isinstance(quote, str) and quote.strip() else None,
                shown_field=shown_field, shown_sha256=shown_sha,
                stance=stance, speaker=speaker,
                source_id=str(entry["source_id"]) if t.kind == "signal" and entry.get("source_id") else None,
            ))

    # A finding's OWN markers are one convention (verify._uses_subclaim_convention):
    # a composition cites [[ref:N]]; a bare [N] in its prose is a unit marker
    # carried from a sub-read, which this finding does not resolve (and the
    # reverse for a unit read). Those are counted apart, never logged unresolved.
    composition = any(e.get("ref_kind") == "finding" or str(e.get("marker") or "").startswith("[[ref:")
                      for e in entries)
    foreign = 0
    segs = (segment or _segments)(text)
    # A marker written after the full stop opens the NEXT segment ("... stopped.
    # [1] Then ..."): it closes the sentence before it, which rests on it.
    owned: dict[int, list[str]] = {}
    prev: int | None = None
    for index, _s, _e, sentence in segs:
        found_all = canonical_markers(sentence)
        own = [m for m in found_all if m.startswith("[[ref:") == composition]
        foreign += len(found_all) - len(own)
        lead = _LEADING_MARKERS_RE.match(normalize_inline_citation_markers(sentence))
        leading = [m for m in canonical_markers(lead.group(0)) if m in own] if lead and prev is not None else []
        if leading:
            owned.setdefault(prev, []).extend(m for m in leading if m not in owned.get(prev, []))
        owned.setdefault(index, []).extend(m for m in own if m not in leading and m not in owned.get(index, []))
        if not _HEADING_RE.match(sentence):
            prev = index
    for index, start, end, sentence in segs:
        kind = "heading" if _HEADING_RE.match(sentence) else None
        found = owned.get(index, [])
        sentences.append(SentenceRow(index, start, end, sentence, _sha(sentence), kind, len(found)))
        for marker in found:
            markers += 1
            entry = by_marker.get(marker)
            if entry is None:
                unresolved.append(UnresolvedRow(index, marker, "marker_not_in_citations"))
                continue
            used.add(marker)
            _emit(entry, index, marker, None)
    # Entries no sentence carries: the unmarked-basis fallback ("the run read
    # these"), a handler's marker-less reference list, a marker lost in prose.
    for marker, entry in by_marker.items():
        if marker not in used:
            _emit(entry, None, marker, ROLE_BASIS if entry.get("resolution") == "fallback_basis" else None)
    for entry in unmarked:
        _emit(entry, None, None, ROLE_BASIS if entry.get("resolution") == "fallback_basis" else None)

    groups = tuple(group_handed(handed, specs)) if handed is not None else None
    return CitationPlan(
        sentences=tuple(sentences), supports=tuple(supports), unresolved=tuple(unresolved),
        handed=groups, markers=markers,
        handed_count=sum(len(g.keys) for g in groups) if groups is not None else 0,
        foreign_markers=foreign,
    )
