# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Journal PRE-PUBLISH verdict — verification stands in front of the text (lane N-D).

Before this module the journal's verify pass was a side critique written ~7 s
AFTER the entry, and nothing read it back: journal d4ff1266 (2026-11-11) opened
on "162 drones … [[ref:0c4b6ba8]]", the judge marked that sentence
``judge_unsupported`` at 12:03:02, and the entry published at 12:02:55 stayed
unmarked, its ``honesty_flags`` only ``['forecast_unproven']``.

Now the runtime mints the entry id, runs the SAME single verify pass (no second
judge call — the existing one is moved, not multiplied), then hands the verdict
here BEFORE the row is written. This module is pure: given the payload, the
verdict's flagged spans and the cited items' texts, it returns the payload to
publish.

WHAT IT DOES TO THE ENTRY — mark, never strip (the journal's §10 contract,
"flag-never-strip", and the brief's "choose marking when removal would break
the sentence": a journal sentence is first-person connective prose, and
deleting one leaves the paragraph's next "That …" pointing at nothing):

  * a sentence the judge marked unsupported / contradicted, or whose citation
    did not resolve → `` [[unsupported]]`` at its end;
  * a number no source cited BY ITS CLAUSE states
    (:func:`span_evidence_checks.unsupported_numbers`) → `` [[number_unsupported]]``
    right after the number phrase. A COUNT passes when it equals the distinct
    incidents its citations report, the refs it cites, or their publishers —
    "four tanker kills" over five refs that are three incident groups, one of
    them "two tankers", is four;
  * a cited item restating an incident another cited item in the same
    sentence already reports → `` [[duplicate_citation]]`` after its marker
    (Tehran Times 5ed90ffe = IRNA fc967ac3; Anadolu 0faa410e = IRNA 229572ba);
  * a terminal outcome word no cited text licenses for the same object
    ("kills" over "exploded / caught fire") → `` [[outcome_overstated]]``.

WHAT IT STORES ON THE ROW: ``data.prepublish_verify`` (every flag, with its
sentence and refs, plus the incident groups — the deduplicated cited list) and
an ``honesty_flags`` entry per class present, so a downstream reader sees the
verdict without the critique row.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from ..journal_evidence import marker_refs
from ..provenance.span_evidence_checks import (
    CHECKS_VERSION,
    incident_groups,
    distinct_incident_count,
    overstated_outcomes,
    unsupported_numbers,
)

__all__ = [
    "DUPLICATE_CITATION",
    "INSTRUMENT_NUMBER",
    "INSTRUMENT_NUMBERS_KEY",
    "NUMBER_UNSUPPORTED",
    "OUTCOME_OVERSTATED",
    "PREPUBLISH_VERSION",
    "UNSUPPORTED",
    "VERIFY_MARKS",
    "VERIFY_MARK_RE",
    "apply_prepublish_verdict",
    "strip_verify_marks",
]

PREPUBLISH_VERSION = "journal_prepublish.v1"

UNSUPPORTED = "unsupported"
NUMBER_UNSUPPORTED = "number_unsupported"
DUPLICATE_CITATION = "duplicate_citation"
OUTCOME_OVERSTATED = "outcome_overstated"
VERIFY_MARKS: tuple[str, ...] = (
    UNSUPPORTED, NUMBER_UNSUPPORTED, DUPLICATE_CITATION, OUTCOME_OVERSTATED,
)
#: The honesty flag each mark class raises on the row.
_FLAG_FOR: dict[str, str] = {
    UNSUPPORTED: "unsupported_claims",
    NUMBER_UNSUPPORTED: "number_unsupported",
    DUPLICATE_CITATION: "duplicate_event_citations",
    OUTCOME_OVERSTATED: "outcome_overstated",
}
VERIFY_MARK_RE = re.compile(
    r" ?\[\[(?:" + "|".join(VERIFY_MARKS) + r")\]\]"
)
#: The verdict reasons that mark a sentence ``[[unsupported]]``.
_MARK_REASONS = frozenset({
    "judge_unsupported", "judge_contradicted", "unresolved_citation",
})

_ANY_MARKER_RE = re.compile(r"\[\[[^\]]*\]\]|[\[【]\d{1,3}[\]】]")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“‘(\[*])|\n\s*\n")
_NEEDLE_CHARS = 60


#: ``data.instrument_numbers`` — written by the inquiry kind from the pre-pass's
#: declared ``instrument_provenance`` (``crossroads_detectors``).
INSTRUMENT_NUMBERS_KEY = "instrument_numbers"
#: ``prepublish_verify.instrument_number`` — every number accepted on that
#: provenance, with the row and arithmetic it was checked against.
INSTRUMENT_NUMBER = "instrument_number"
_ORDINAL_CITE_RE = re.compile(r"(?<!\[)[\[【](\d{1,3})[\]】](?!\])")


def _cited_instrument_rows(
    instrument: Sequence[Mapping[str, Any]], sent: str, refs: Sequence[str],
) -> list[Mapping[str, Any]]:
    """The instrument rows ``sent`` cites: by block ordinal, or by a ref."""
    ordinals = {int(m.group(1)) for m in _ORDINAL_CITE_RE.finditer(sent)}
    cited = {r.lower() for r in refs}
    return [
        row for row in instrument
        if (row.get("ordinal") and row.get("ordinal") in ordinals)
        or cited & {str(x).lower() for x in row.get("refs") or ()}
    ]


def _instrument_row_for(
    rows: Sequence[Mapping[str, Any]], surface: str,
) -> Mapping[str, Any] | None:
    """The first cited row that computed the number ``surface``, else None."""
    try:
        value = float(str(surface).replace(",", ""))
    except ValueError:
        return None
    for row in rows:
        if value in {float(x) for x in row.get("numbers") or ()}:
            return row
    return None


def strip_verify_marks(text: str) -> str:
    """``text`` without the marks this module inserts — what a re-judge reads,
    so a re-verify grades the writer's words, not this module's annotations."""
    return VERIFY_MARK_RE.sub("", str(text or ""))


def _sentences(body: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    pos = 0
    for m in _SENT_SPLIT_RE.finditer(body):
        if body[pos:m.start()].strip():
            out.append((pos, m.start()))
        pos = m.end()
    if body[pos:].strip():
        out.append((pos, len(body.rstrip())))
    return out


def _stripped_with_map(text: str) -> tuple[str, list[int]]:
    """``text`` with every marker removed and whitespace collapsed, plus the
    index in ``text`` each kept character came from."""
    keep = [True] * len(text)
    for m in _ANY_MARKER_RE.finditer(text):
        for i in range(m.start(), m.end()):
            keep[i] = False
    chars: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if not keep[i]:
            continue
        if ch.isspace():
            if prev_space:
                continue
            ch, prev_space = " ", True
        else:
            prev_space = False
        chars.append(ch.lower())
        idx.append(i)
    return "".join(chars), idx


def _locate(body: str, claim_text: str) -> int | None:
    """Body offset where ``claim_text`` (a verify-doc segment with ``[N]``
    markers) starts, or ``None`` when it cannot be found unambiguously."""
    needle, _ = _stripped_with_map(claim_text)
    needle = needle.strip()[:_NEEDLE_CHARS].strip()
    if len(needle) < 12:
        return None
    hay, idx = _stripped_with_map(body)
    at = hay.find(needle)
    if at < 0 or hay.find(needle, at + 1) >= 0:
        return None
    return idx[at]


def _sentence_end_insert(body: str, start: int, end: int) -> int:
    """Insert point for a sentence-level mark: before the final terminator."""
    text = body[start:end].rstrip()
    stop = start + len(text)
    if text and text[-1] in ".!?":
        return stop - 1
    return stop


def apply_prepublish_verdict(
    payload: Any,
    *,
    unsupported_spans: Sequence[Mapping[str, Any]],
    ref_texts: Mapping[str, Mapping[str, str]],
    judge_status: str | None,
    reflect: Callable[[str], tuple[list, list, list]],
) -> tuple[Any, dict[str, Any]]:
    """The payload to PUBLISH, and the ``prepublish_verify`` block it carries.

    ``unsupported_spans`` is the verify report's list (``text`` + ``reason``).
    ``ref_texts`` maps a lowercase ref uuid to ``{"title", "text",
    "publisher"}`` for every cited item that resolved to a source. ``reflect``
    is ``journal_reflect._reflect_claims``, re-run over the marked body so the
    claims sidecar carries the same marks (it is deterministic and binds the
    same refs). Pure: no DB, no model.
    """
    body = str(getattr(payload, "body", "") or "")
    inserts: list[tuple[int, str]] = []
    summary: dict[str, Any] = {
        "version": PREPUBLISH_VERSION,
        "checks_version": CHECKS_VERSION,
        "judge_status": judge_status,
        UNSUPPORTED: [],
        NUMBER_UNSUPPORTED: [],
        DUPLICATE_CITATION: [],
        OUTCOME_OVERSTATED: [],
        "event_groups": [],
        "unlocated": 0,
        INSTRUMENT_NUMBER: [],
    }
    sentences = _sentences(body)

    # 1. The judge's verdicts, located back in the body.
    for span in unsupported_spans:
        reason = str(span.get("reason") or "")
        if reason not in _MARK_REASONS:
            continue
        text = str(span.get("text") or "")
        at = _locate(body, text)
        entry = {"reason": reason, "text": text[:600]}
        if at is None:
            summary["unlocated"] += 1
            entry["located"] = False
            summary[UNSUPPORTED].append(entry)
            continue
        for s, e in sentences:
            if s <= at < e:
                inserts.append((_sentence_end_insert(body, s, e), UNSUPPORTED))
                break
        entry["located"] = True
        summary[UNSUPPORTED].append(entry)

    # 2-4. The deterministic checks, per CITED sentence of the body.
    def resolve(ref: str) -> str | None:
        hit = ref_texts.get(ref.lower())
        return hit.get("text") if hit else None

    def titles(refs: Sequence[str]) -> dict[str, str]:
        return {
            r: ref_texts[r]["title"] for r in refs
            if r in ref_texts and ref_texts[r].get("title")
        }

    def counts(refs: list[str]) -> list[float]:
        pubs = {ref_texts[r].get("publisher") for r in refs if r in ref_texts} - {None, ""}
        return [
            float(distinct_incident_count(titles(refs))),
            float(len(refs)),
            float(len(pubs)),
        ]

    # N-T — numbers the deterministic pre-pass COMPUTED carry a declared
    # provenance (``data.instrument_numbers``, one entry per block row). Such a
    # number is checked against the row the sentence cites — by its ordinal
    # ``[N]`` or one of its refs — not against the cited text; it passes only
    # when that row computed exactly that number, and is recorded either way.
    instrument = [
        r for r in (getattr(payload, "data", {}) or {}).get(INSTRUMENT_NUMBERS_KEY) or ()
        if isinstance(r, Mapping)
    ]
    for s, e in sentences:
        sent = body[s:e]
        refs = marker_refs(sent)     # N-U: nested [[ref:a] [ref:b]] groups too
        if not refs:
            continue
        rows = _cited_instrument_rows(instrument, sent, refs)
        for n in unsupported_numbers(sent, resolve, computed_counts=counts):
            # The number's OWN clause's rows first, then the sentence's: a
            # marker may close the clause before it or open the next one (the
            # convention ``unsupported_numbers`` itself follows), so the
            # sentence is the unit, and a row it does not cite lends nothing.
            own = _cited_instrument_rows(rows, "", n.get("refs") or ())
            row = _instrument_row_for(own, n["number"]) or _instrument_row_for(
                rows, n["number"],
            )
            if row is not None:
                summary[INSTRUMENT_NUMBER].append({
                    "number": n["number"], "phrase": n["phrase"],
                    "ordinal": row.get("ordinal"), "kind": row.get("kind"),
                    "arithmetic": str(row.get("arithmetic") or "")[:300],
                    "sentence": sent[:600],
                })
                continue
            inserts.append((s + n["end"], NUMBER_UNSUPPORTED))
            summary[NUMBER_UNSUPPORTED].append({
                "number": n["number"], "phrase": n["phrase"],
                "refs": n["refs"], "sentence": sent[:600],
            })
        groups = incident_groups(titles(refs))
        for g in groups:
            if len(g) < 2:
                continue
            summary["event_groups"].append(list(g))
            for dup in g[1:]:
                m = re.search(rf"\[\[ref:{re.escape(dup)}\]\]", sent, re.IGNORECASE)
                if m:
                    inserts.append((s + m.end(), DUPLICATE_CITATION))
                summary[DUPLICATE_CITATION].append({
                    "ref": dup, "same_incident_as": g[0], "sentence": sent[:600],
                })
        cited = [t for t in (resolve(r) for r in refs) if t]
        for term in overstated_outcomes(sent, cited):
            m = re.search(rf"\b{re.escape(term)}\b", sent, re.IGNORECASE)
            if m:
                inserts.append((s + m.end(), OUTCOME_OVERSTATED))
            summary[OUTCOME_OVERSTATED].append({"term": term, "sentence": sent[:600]})

    marked = body
    for pos, tag in sorted(set(inserts), key=lambda x: (-x[0], x[1])):
        marked = f"{marked[:pos]} [[{tag}]]{marked[pos:]}"
    summary["marked"] = len(set(inserts))

    flags = list(getattr(payload, "honesty_flags", []) or [])
    for mark in VERIFY_MARKS:
        if summary[mark] and _FLAG_FOR[mark] not in flags:
            flags.append(_FLAG_FOR[mark])
    if any(u["reason"] == "judge_contradicted" for u in summary[UNSUPPORTED]):
        if "contradicted_claims" not in flags:
            flags.append("contradicted_claims")
    data = {**(getattr(payload, "data", {}) or {}), "prepublish_verify": summary}
    update: dict[str, Any] = {"honesty_flags": flags, "data": data}
    if marked != body:
        claims, cited_refs, _ = reflect(marked)
        update.update(body=marked, claims=claims, cited_substrate_refs=cited_refs)
    return payload.model_copy(update=update), summary
