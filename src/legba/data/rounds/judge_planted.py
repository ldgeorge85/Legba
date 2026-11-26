# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planted POSITIONS for calibrating panel judges.

A judge on a round panel rules on positions shown as a sentence table. This
module builds, deterministically and by code only, positions whose right
answer is known: known-good CONTROLS and positions carrying one PLANTED
error. Every place, body and figure is the statement canary's
(:mod:`legba.data.statement_seat_canary`: invented on purpose — the
calibration measures the instrument, not any instance's data).

A base position is three canary sentences about three different invented
subjects, each citing its own row by a quote that states every figure and
date in it. The first sentence is the KEY sentence; a plant changes it (or
adds one) and nothing else:

==============================  =============================================
sentence-level (reused from     ``statement_planted.plant`` on the key item:
``statement_planted``)          wrong_number, wrong_date, wrong_subject,
                                other_version_quote, unsupported_inference,
                                claim_as_fact
``key_sentence_unsupported``    the key sentence cites a real, handed row that
                                is about something else (its quote is exact,
                                and says nothing the sentence asserts)
``contradicts_own_rows``        the key sentence's own row says it did not
                                happen; the sentence cites that denial
``restates_without_evidence``   a working-reading conclusion is put first,
                                citing nothing
==============================  =============================================

S-3's lesson holds: a planted error must leave NO OTHER supporting quote —
the other sentences are about other subjects, and :func:`supporting_elsewhere`
checks that no other handed row states the planted content (a test runs it
over the whole set).

Batches: the set is cut into judge calls of :data:`BATCH` candidates, ordered
by a hash so position in the batch is not the truth. Pure.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..statement_content import find_numbers, strip_markers
from ..statement_planted import PLANTED_KINDS, TRUE, from_canary, plant
from ..statement_seat_canary import CANARY, CanaryItem

JUDGE_PLANTED_VERSION = "judge_planted.v1"
POSITION_KINDS: tuple[str, ...] = ("key_sentence_unsupported", "contradicts_own_rows",
                                   "restates_without_evidence")
ALL_KINDS: tuple[str, ...] = PLANTED_KINDS + POSITION_KINDS
#: Candidates per judge call (a dialogues panel rules on four positions).
BATCH = 4
#: The conclusion a ``restates_without_evidence`` plant puts first.
READINGS = (
    "Taken together, the situation is clearly deteriorating and the trend will continue.",
    "Overall, the authorities have lost control of events and further disruption is certain.",
    "In sum, the pressure is building toward a crisis within weeks.",
)


@dataclass(frozen=True)
class CalItem:
    """One candidate a judge is measured on. ``truth`` is ``"true"`` or the
    planted kind; ``position`` is a ``sentence_table/1`` position; ``rows``
    are the evidence rows (id, text) the position cites."""

    item_id: str
    truth: str
    position: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...]

    @property
    def planted(self) -> bool:
        return self.truth != TRUE


def _row(item: CanaryItem, text: str | None = None) -> dict[str, Any]:
    sup = item.support[0]
    body = text if text is not None else next(iter(item.texts.by_signal.values()))
    return {"id": sup.ref_id, "kind": "signal", "text": body, "title": sup.title,
            "published_at": sup.published_at.isoformat() if sup.published_at else None}


def _sentence(key: str, text: str, ref: str | None, quote: str | None) -> dict[str, Any]:
    cited = [{"ref_id": ref, "role": "evidence", "kind": "signal", "quote": quote}] if ref else []
    return {"key": key, "text": strip_markers(text), "stance": "supports", "cited": cited}


def _bases(n: int) -> list[tuple[CanaryItem, CanaryItem, CanaryItem]]:
    m = len(CANARY)
    return [(CANARY[i % m], CANARY[(i + 7) % m], CANARY[(i + 13) % m]) for i in range(n)]


def _position(pid: str, key_item: CanaryItem, others: Sequence[CanaryItem], *,
              key_text: str | None = None, key_ref: str | None = None,
              key_quote: str | None = None, key_row_text: str | None = None,
              extra_rows: Sequence[Mapping[str, Any]] = (), lead: str | None = None,
              cite_key: bool = True) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    sup = key_item.support[0]
    sents = []
    if lead is not None:
        sents.append(_sentence("k0", lead, None, None))
    sents.append(_sentence("k1", key_text if key_text is not None else key_item.statement.text,
                           (key_ref or sup.ref_id) if cite_key else None,
                           key_quote if key_quote is not None else sup.quote))
    rows = [_row(key_item, key_row_text)]
    for i, o in enumerate(others, 2):
        sents.append(_sentence(f"k{i}", o.statement.text, o.support[0].ref_id, o.support[0].quote))
        rows.append(_row(o))
    rows.extend(dict(r) for r in extra_rows)
    return ({"position_id": pid, "seat": "calibration", "sentences": sents}, tuple(rows))


def _plant_sentence(pid: str, kind: str, base: tuple[CanaryItem, ...], index: int) -> CalItem | None:
    key, *others = base
    made = plant(from_canary(key), kind, index=index)
    if made is None:
        return None
    sup = made.support[0]
    row_text = made.texts.by_signal.get(sup.ref_id)
    pos, rows = _position(pid, key, others, key_text=made.statement.text, key_quote=sup.quote,
                          key_row_text=row_text)
    return CalItem(pid, kind, pos, rows)


def _plant_position(pid: str, kind: str, base: tuple[CanaryItem, ...], index: int) -> CalItem | None:
    key, *others = base
    if kind == "key_sentence_unsupported":
        used = {b.item_id for b in base}
        foreign = next(c for c in CANARY[(index * 5 + 3) % len(CANARY):] + CANARY
                       if c.item_id not in used)
        fsup = foreign.support[0]
        pos, rows = _position(pid, key, others, key_ref=fsup.ref_id, key_quote=fsup.quote,
                              extra_rows=[_row(foreign)])
        rows = tuple(r for r in rows if r["id"] != key.support[0].ref_id)
        return CalItem(pid, kind, pos, rows)
    if kind == "contradicts_own_rows":
        sup = key.support[0]
        quote = f"officials denied that {sup.quote}"
        body = (f"Officials denied the reports at a briefing: officials denied that {sup.quote}. "
                "Further figures are expected later in the week, the statement added.")
        pos, rows = _position(pid, key, others, key_quote=quote, key_row_text=body)
        return CalItem(pid, kind, pos, rows)
    if kind == "restates_without_evidence":
        pos, rows = _position(pid, key, others, lead=READINGS[index % len(READINGS)])
        return CalItem(pid, kind, pos, rows)
    raise ValueError(f"unknown position kind {kind!r}")


def calibration_set(*, per_kind: int = 3, controls: int = 12) -> list[CalItem]:
    """``per_kind`` planted candidates of every kind plus ``controls`` true
    ones, each on its own base position (rotated so no base carries a kind)."""
    bases = _bases(len(CANARY))
    out: list[CalItem] = []
    n = len(bases)
    for k, kind in enumerate(ALL_KINDS):
        taken = 0
        for step in range(n):
            if taken >= per_kind:
                break
            base = bases[(k * per_kind + step * 3) % n]
            pid = f"cal/{kind}/{taken}"
            made = (_plant_sentence(pid, kind, base, taken) if kind in PLANTED_KINDS
                    else _plant_position(pid, kind, base, taken))
            if made is not None and not supporting_elsewhere(made):
                out.append(made)
                taken += 1
    for i in range(controls):
        key, *others = bases[(i * 5 + 1) % n]
        pid = f"cal/true/{i}"
        pos, rows = _position(pid, key, others)
        out.append(CalItem(pid, TRUE, pos, rows))
    return out


def supporting_elsewhere(item: CalItem) -> bool:
    """Would another handed row support the planted key sentence? True when a
    figure the key sentence states that its own quote does not appears in
    another row's text — the plant would then not be an error."""
    sents = item.position["sentences"]
    key = next(s for s in sents if s["key"] == "k1")
    own = " ".join(c.get("quote") or "" for c in key["cited"])
    own_ref = {c.get("ref_id") for c in key["cited"]}
    stated = {n.raw for n in find_numbers(key["text"])}
    in_quote = {n.raw for n in find_numbers(own)}
    novel = stated - in_quote
    others = [r["text"] for r in item.rows if r["id"] not in own_ref]
    return any(raw in t for raw in novel for t in others)


def _h(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def batches(items: Sequence[CalItem], size: int = BATCH) -> list[list[CalItem]]:
    """Judge calls: candidates in a hash order (position is not the truth),
    cut into ``size``; each batch's ids are unique."""
    ordered = sorted(items, key=lambda it: (_h(JUDGE_PLANTED_VERSION, it.item_id), it.item_id))
    return [list(ordered[i:i + size]) for i in range(0, len(ordered), size)]


def table_of(batch: Sequence[CalItem]) -> dict[str, Any]:
    """The ``sentence_table/1`` payload a judge is handed for one batch."""
    return {"positions": [dict(it.position) for it in batch]}


def rows_of(batch: Sequence[CalItem]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for it in batch:
        for r in it.rows:
            seen.setdefault(str(r["id"]), dict(r))
    return list(seen.values())


def score(results: Iterable[tuple[CalItem, str, Sequence[str]]]) -> dict[str, Any]:
    """Catch / false reject (counted outcome ``falls``), unsure counts and
    pairwise ranking accuracy, from ``(item, counted outcome, batch ranking)``.

    * catch = planted candidates the judge counted ``falls``;
    * false reject = controls it counted ``falls``;
    * ranking = over every (control, planted) pair in one batch both ranked,
      the share with the control ranked above the planted candidate."""
    from ..statement_planted import Rate

    rows = list(results)
    planted = [(it, o) for it, o, _ in rows if it.planted]
    true = [(it, o) for it, o, _ in rows if not it.planted]
    per_kind: dict[str, list[int]] = {}
    for it, o in planted:
        r = per_kind.setdefault(it.truth, [0, 0])
        r[0] += int(o == "falls")
        r[1] += 1
    pairs = right = 0
    by_rank: dict[tuple[str, ...], list[CalItem]] = {}
    for it, _, ranking in rows:
        by_rank.setdefault(tuple(ranking), []).append(it)
    for ranking, its in by_rank.items():
        pos = {c: i for i, c in enumerate(ranking)}
        for a in its:
            for b in its:
                if a.planted or not b.planted:
                    continue
                if a.item_id in pos and b.item_id in pos:
                    pairs += 1
                    right += int(pos[a.item_id] < pos[b.item_id])
    return {
        "catch": Rate(sum(o == "falls" for _, o in planted), len(planted)).as_dict(),
        "false_reject": Rate(sum(o == "falls" for _, o in true), len(true)).as_dict(),
        "unsure": {"planted": sum(o not in ("stands", "falls") for _, o in planted),
                   "true": sum(o not in ("stands", "falls") for _, o in true)},
        "ranking_pairs": Rate(right, pairs).as_dict(),
        "per_kind": {k: Rate(v[0], v[1]).as_dict() for k, v in sorted(per_kind.items())},
        "planted_set": JUDGE_PLANTED_VERSION,
    }


__all__ = ["ALL_KINDS", "BATCH", "JUDGE_PLANTED_VERSION", "POSITION_KINDS", "CalItem", "batches",
           "calibration_set", "rows_of", "score", "supporting_elsewhere", "table_of"]
