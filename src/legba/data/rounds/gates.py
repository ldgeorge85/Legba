# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gates at handoffs: code checks on a seat's output before any reader may
have it (plan P4 §4.3, lane G-3).

The first five are the statement layer's honesty gates, CALLED (not copied)
on the sentences a seat wrote:

==========================  ===========================================  =====
Gate                        Rule                                         Uses
==========================  ===========================================  =====
``handed_rows``             every cited signal/observation was handed    G3 ``check_handed_row``
                            to this seat (a cited round row too)
``quote_in_source``         every evidence quote is in the cited row's   G1 ``check_quote``
                            stored text
``numbers_in_evidence``     every number a sentence states is in a       G2 ``check_numbers``
                            quote or a cited observation
``dates_in_evidence``       every date a sentence states is in a quote   G2 ``check_dates``
``control_text``            no model/harness control text                G11 ``check_control_text``
==========================  ===========================================  =====

Round-specific (design §4.3): ``must_include_set`` (rows code marked
``required`` are not dropped), ``every_sentence_maps_to_rows`` (each sentence
cites at least one handed row, and only handed rows),
``probabilities_copied_not_averaged`` (every probability written is one a
handed input carries — copied, never computed).

A gate never raises on a malformed payload: it fails with a reason. The
statement gate library is imported when a gate first runs.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .rows import sentences_of, support_of
from .tables import Handed, evidence_rows

_QUOTED = ("evidence", "contradicts")


@dataclass(frozen=True)
class GateContext:
    """What a gate sees: the seat's parsed output and what it was handed."""

    seat: str
    row_type: str
    payload: Mapping[str, Any]
    handed: Sequence[Handed]
    run_id: str = ""

    def handed_ids(self) -> set[str]:
        ids = {h.ref_id for h in self.handed}
        ids |= set(evidence_rows(self.handed))
        for h in self.handed:
            if isinstance(h.payload, Mapping) and h.row_type == "sentence_table/1":
                for p in h.payload.get("positions") or ():
                    ids.add(str(p.get("position_id")))
                    for s in p.get("sentences") or ():
                        ids |= {str(c.get("ref_id")) for c in s.get("cited") or ()}
            # a row an upstream sentence cites (with its quote) was handed too
            ids |= {str(r.get("ref_id")) for r in support_of(h.payload)}
        return {i.lower() for i in ids}


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    reason: str | None = None
    checks: tuple[Mapping[str, Any], ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"gate": self.gate, "passed": self.passed}
        if self.reason:
            out["reason"] = self.reason
        if self.detail:
            out["detail"] = dict(self.detail)
        failed = [c for c in self.checks if not c.get("passed", True)]
        if failed:
            out["failed_checks"] = failed
        return out


def _published(value: Any) -> datetime | None:
    """A handed row's publication time (ISO text) as an aware datetime, for
    the date rule's publication-day half; anything unreadable is ``None``."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _support_row(r: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]]) -> Any:
    from ..statement_shapes import SupportRow

    src = rows.get(str(r.get("ref_id")), {})
    return SupportRow(
        role=str(r.get("role", "evidence")), support_kind=str(r.get("kind", "signal")),
        ref_id=str(r.get("ref_id")), quote=r.get("quote"),
        fetch_version_id=src.get("fetch_version_id"), source_id=src.get("source_id"),
        origin_group_id=src.get("origin_group_id"), access_class=src.get("access_class"),
        title=src.get("title"), published_at=_published(src.get("published_at")),
    )


@dataclass(frozen=True)
class _Tagged:
    """A check result tagged with the sentence it was made on, so a failed
    gate names the sentence (the ``repair: reasons`` retry reads it)."""

    inner: Any
    sentence: str | None
    index: int | None
    extra: Mapping[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return bool(self.inner.passed)

    @property
    def reason(self) -> str | None:
        return self.inner.reason

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.inner.as_dict())
        if self.sentence is not None:
            out["sentence"] = self.sentence
        if self.index is not None:
            out["sentence_index"] = self.index
        if self.extra:
            out["detail"] = {**dict(out.get("detail") or {}), **dict(self.extra)}
        return out


def _supports_by_sentence(payload: Any) -> list[tuple[str | None, int | None, dict[str, Any]]]:
    """Every support row with the key and index of the sentence (or
    assumption / proposal) it hangs on — the order ``support_of`` uses."""
    out: list[tuple[str | None, int | None, dict[str, Any]]] = []
    for i, s in enumerate(sentences_of(payload)):
        for r in s.get("support") or ():
            if isinstance(r, dict):
                out.append((str(s.get("key", "")) or None, i, r))
    if isinstance(payload, dict):
        for key in ("assumptions", "proposals"):
            for a in payload.get(key) or ():
                if isinstance(a, dict):
                    for r in a.get("support") or ():
                        if isinstance(r, dict):
                            out.append((str(a.get("key", "")) or None, None, r))
    return out


def _texts(rows: Mapping[str, Mapping[str, Any]]) -> Any:
    from ..statement_shapes import EvidenceTexts, ObservationText

    signals = {i: str(r["text"]) for i, r in rows.items()
               if r.get("text") is not None and r.get("kind", "signal") == "signal"}
    fetched = {int(r["fetch_version_id"]): str(r["text"]) for r in rows.values()
               if r.get("fetch_version_id") is not None and r.get("text") is not None}
    obs = {i: ObservationText(str(r.get("text") or ""), tuple(r.get("values") or ()))
           for i, r in rows.items() if r.get("kind") == "observation"}
    return EvidenceTexts(by_fetch_version=fetched, by_signal=signals, by_observation=obs)


def _statement(text: str) -> Any:
    from ..statement_shapes import StatementDraft

    return StatementDraft(text=text, kind="assessment", subject_kind="other", subject_key="")


def _from_checks(gate: str, checks: list[Any]) -> GateResult:
    failed = [c for c in checks if not c.passed]
    return GateResult(gate, not failed, failed[0].reason if failed else None,
                      tuple(c.as_dict() for c in checks))


def handed_rows(ctx: GateContext) -> GateResult:
    from ..statement_gate import check_handed_row
    from ..statement_shapes import HandedSlice

    handed = ctx.handed_ids()
    rows = evidence_rows(ctx.handed)
    slice_ = HandedSlice.of(handed, ctx.run_id)
    checks: list[Any] = []
    for i, (key, si, r) in enumerate(_supports_by_sentence(ctx.payload)):
        if r.get("kind") == "row" and r.get("role", "evidence") in _QUOTED:
            ok = str(r.get("ref_id")).lower() in handed
            checks.append(_Tagged(_Check("G3", "handed", ok, None if ok else "not_in_run_slice", i),
                                  key, si))
            continue
        checks.append(_Tagged(check_handed_row(_support_row(r, rows), slice_, i), key, si))
    return _from_checks("handed_rows", checks)


def quote_in_source(ctx: GateContext) -> GateResult:
    from ..statement_gate import check_quote

    rows = evidence_rows(ctx.handed)
    texts = _texts(rows)
    checks = []
    for i, (key, si, r) in enumerate(_supports_by_sentence(ctx.payload)):
        res = check_quote(_support_row(r, rows), texts, row_index=i)
        hint = None
        if not res.passed and res.reason == "quote_absent":
            hint = closest_passage(str(r.get("quote") or ""),
                                   str((rows.get(str(r.get("ref_id"))) or {}).get("text") or ""))
        checks.append(_Tagged(res, key, si, {"closest_passage": hint} if hint else None))
    return _from_checks("quote_in_source", checks)


def closest_passage(quote: str, text: str, *, max_words: int = 30) -> str | None:
    """The run of the row's own words that best matches a quote that is not
    in it (code-found, verbatim from the row) — what a ``repair: reasons``
    retry is shown so it can copy the source's words instead of its own."""
    import difflib

    qw, tw = quote.split(), text.split()
    if not qw or not tw:
        return None
    n = min(len(qw) + 4, max_words, len(tw))
    best, best_score = None, 0.0
    ql = [w.lower().strip(".,;:\"'()") for w in qw]
    for start in range(0, max(1, len(tw) - n + 1)):
        window = tw[start:start + n]
        score = difflib.SequenceMatcher(None, ql, [w.lower().strip(".,;:\"'()") for w in window],
                                        autojunk=False).ratio()
        if score > best_score:
            best, best_score = window, score
    return " ".join(best) if best is not None and best_score >= 0.4 else None


def numbers_in_evidence(ctx: GateContext) -> GateResult:
    from ..statement_gate import check_numbers

    rows = evidence_rows(ctx.handed)
    texts = _texts(rows)
    checks = [_Tagged(check_numbers(_statement(str(s.get("text", ""))),
                                    [_support_row(r, rows) for r in s.get("support") or ()], texts),
                      str(s.get("key", "")) or None, i)
              for i, s in enumerate(sentences_of(ctx.payload))]
    return _from_checks("numbers_in_evidence", checks)


def dates_in_evidence(ctx: GateContext) -> GateResult:
    from ..statement_gate import check_dates

    rows = evidence_rows(ctx.handed)
    checks = [_Tagged(check_dates(_statement(str(s.get("text", ""))),
                                  [_support_row(r, rows) for r in s.get("support") or ()]),
                      str(s.get("key", "")) or None, i)
              for i, s in enumerate(sentences_of(ctx.payload))]
    return _from_checks("dates_in_evidence", checks)


def control_text(ctx: GateContext) -> GateResult:
    from ..statement_gate import check_control_text

    rows = evidence_rows(ctx.handed)
    checks = [_Tagged(check_control_text(_statement(str(s.get("text", ""))),
                                         [_support_row(r, rows) for r in s.get("support") or ()]),
                      str(s.get("key", "")) or None, i)
              for i, s in enumerate(sentences_of(ctx.payload))]
    return _from_checks("control_text", checks)


@dataclass(frozen=True)
class _Check:
    """A round gate's check, shaped like the statement layer's CheckResult."""

    gate: str
    check: str
    passed: bool
    reason: str | None = None
    row: int | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"gate": self.gate, "check": self.check, "passed": self.passed}
        if self.reason:
            out["reason"] = self.reason
        if self.row is not None:
            out["row"] = self.row
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


def must_include_set(ctx: GateContext) -> GateResult:
    required = sorted(i for i, r in evidence_rows(ctx.handed).items() if r.get("required"))
    kept = {str(r.get("id")) for r in ctx.payload.get("rows") or () if isinstance(r, Mapping)}
    kept |= {str(r.get("ref_id")) for r in support_of(ctx.payload)}
    missing = [i for i in required if i not in kept]
    return GateResult("must_include_set", not missing, "required_row_dropped" if missing else None,
                      detail={"required": len(required), "missing": missing})


def every_sentence_maps_to_rows(ctx: GateContext) -> GateResult:
    handed = ctx.handed_ids()
    checks = []
    for i, s in enumerate(sentences_of(ctx.payload)):
        refs = [str(r.get("ref_id")).lower() for r in s.get("support") or ()]
        key = str(s.get("key", "")) or None
        if not refs:
            checks.append(_Tagged(_Check("R1", "maps_to_rows", False, "sentence_without_rows", i),
                                  key, i))
        elif any(r not in handed for r in refs):
            checks.append(_Tagged(_Check("R1", "maps_to_rows", False, "row_not_handed", i,
                                         {"not_handed": sorted(r for r in refs if r not in handed)}),
                                  key, i))
        else:
            checks.append(_Tagged(_Check("R1", "maps_to_rows", True, None, i), key, i))
    if not checks:
        checks.append(_Check("R1", "maps_to_rows", False, "no_sentences"))
    return _from_checks("every_sentence_maps_to_rows", checks)


def _probabilities(obj: Any, out: list[float]) -> None:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k == "probability" and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
            else:
                _probabilities(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _probabilities(v, out)


def probabilities_copied_not_averaged(ctx: GateContext) -> GateResult:
    written: list[float] = []
    _probabilities(ctx.payload, written)
    given: list[float] = []
    for h in ctx.handed:
        _probabilities(h.payload, given)
    invented = [p for p in written if not any(abs(p - g) < 1e-9 for g in given)]
    return GateResult("probabilities_copied_not_averaged", not invented,
                      "probability_not_copied" if invented else None,
                      detail={"written": written, "invented": invented})


#: The gates, by the name a spec's ``gates`` list uses (pinned to catalog.yaml).
GATES: dict[str, Callable[[GateContext], GateResult]] = {
    "handed_rows": handed_rows,
    "quote_in_source": quote_in_source,
    "numbers_in_evidence": numbers_in_evidence,
    "dates_in_evidence": dates_in_evidence,
    "control_text": control_text,
    "must_include_set": must_include_set,
    "every_sentence_maps_to_rows": every_sentence_maps_to_rows,
    "probabilities_copied_not_averaged": probabilities_copied_not_averaged,
}


def run_gates(names: Sequence[str], ctx: GateContext) -> list[GateResult]:
    out = []
    for name in names:
        fn = GATES.get(name)
        if fn is None:
            out.append(GateResult(name, False, "gate_not_registered"))
            continue
        try:
            out.append(fn(ctx))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            out.append(GateResult(name, False, "gate_input_malformed", detail={"error": str(exc)}))
    return out
