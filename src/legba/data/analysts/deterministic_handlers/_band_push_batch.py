# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 ALERTS — the band moves of one trigger tick are ONE push.

WHY. The banding tick moves bands on many desks at once, and each move was its
own alert row AND its own push: review C watched NINE ``Band deterioration /
improvement`` pushes land in one second (one per desk), on the same channel as
the world's escalations. A desk-by-desk drumroll is noise however true each
line is; the operator needs "9 band moves: 7 worse, 2 better", once.

WHAT CHANGES, AND WHAT DOES NOT.

* Every band move still writes its OWN ``kind='alert'`` row — the morning
  read's per-desk fold (``/v3/since`` → ``morningReadBands``) and the lineage
  walk both read those rows, and a batch is not a reason to lose one.
* Every band row of one tick carries ``data.push_batch = band_crossing:<run>``.
  The daily page budget treats the batch as ONE unit (one slot, one decision
  for every member: :func:`._daily_page_budget.apply_daily_page_budget`) and
  counts it as ONE page (the paged-today SQL counts distinct batches).
* The fan-out sends ONE payload for the batch (:func:`group_pushes`): the
  summary counts the moves by direction, the body lists them worst-first, at
  most :data:`BAND_BATCH_MAX_LINES_ENV` lines (default
  :data:`DEFAULT_BAND_BATCH_MAX_LINES`) and then ``… and N more``. The receipt
  link anchors on the worst move's row; the ledger row lists every member id.
* A tick with ONE band move pushes exactly the payload it always did.

Per-desk rollups (the per-desk cap's fold) are untouched: a rollup is its own
push, as before.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Iterable, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

TRIGGER_BAND = "band_crossing"  # literal mirror of alert_trigger_scan.TRIGGER_BAND

#: THE TUNABLE (docs/TUNABLES.md §5) — the per-tick cap on listed moves.
BAND_BATCH_MAX_LINES_ENV = "LEGBA_ALERT_BAND_BATCH_MAX_LINES"
DEFAULT_BAND_BATCH_MAX_LINES = 8

#: Direction → the word the summary counts it under (in this order).
_DIRECTION_WORD: dict[str, str] = {
    "deterioration": "worse",
    "improvement": "better",
    "evidence-gained": "evidence gained",
    "evidence-lost": "evidence lost",
    "semantics-migration": "re-derived",
    "indeterminate": "unclassified",
}
_DIRECTION_ORDER = {d: i for i, d in enumerate(_DIRECTION_WORD)}
_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def band_batch_max_lines() -> int:
    raw = (os.environ.get(BAND_BATCH_MAX_LINES_ENV) or "").strip()
    if not raw:
        return DEFAULT_BAND_BATCH_MAX_LINES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "alert_trigger_scan.bad_env %s=%r — using %d",
            BAND_BATCH_MAX_LINES_ENV, raw, DEFAULT_BAND_BATCH_MAX_LINES,
        )
        return DEFAULT_BAND_BATCH_MAX_LINES
    return value if value > 0 else DEFAULT_BAND_BATCH_MAX_LINES


def push_batch_key(run_uuid: Any) -> str:
    return f"{TRIGGER_BAND}:{run_uuid}"


def mark_band_batch(candidates: Iterable[Any], *, run_uuid: Any) -> int:
    """Stamp this tick's batch key on every band candidate; returns how many.

    Runs BEFORE the daily page budget so the budget sees the unit, and before
    the row write so the row records which push carried it.
    """
    key = push_batch_key(run_uuid)
    n = 0
    for cand in candidates:
        if cand.trigger_class == TRIGGER_BAND:
            cand.data["push_batch"] = key
            n += 1
    return n


def _direction(cand: Any) -> str:
    return str(cand.data.get("direction") or "indeterminate")


def _order_key(cand: Any) -> tuple[int, int, str]:
    return (
        -_SEVERITY_RANK.get(str(cand.severity), 0),
        _DIRECTION_ORDER.get(_direction(cand), len(_DIRECTION_ORDER)),
        str(cand.title),
    )


def band_move_line(cand: Any) -> str:
    """``country_g20_de military_posture low→watch (worse)``."""
    d = cand.data
    desk = d.get("desk") or cand.target_id or "-"
    direction = _direction(cand)
    if direction == "semantics-migration":
        n = len(d.get("transitions") or [])
        return f"{desk}: {n} dimension(s) re-derived under new semantics (not a world change)"
    word = _DIRECTION_WORD.get(direction, direction)
    return (
        f"{desk} {d.get('dimension') or '-'} "
        f"{d.get('from_band') or '-'}→{d.get('to_band') or '-'} ({word})"
    )


def summary_line(cands: list[Any]) -> str:
    """``9 band moves: 7 worse, 2 better`` (only the directions present)."""
    counts: dict[str, int] = {}
    for c in cands:
        word = _DIRECTION_WORD.get(_direction(c), _direction(c))
        counts[word] = counts.get(word, 0) + 1
    ordered = [w for w in _DIRECTION_WORD.values() if w in counts]
    ordered += sorted(w for w in counts if w not in ordered)
    parts = ", ".join(f"{counts[w]} {w}" for w in ordered)
    return f"{len(cands)} band moves: {parts}"


def batch_payload(
    members: list[tuple[UUID, Any]],
    *,
    sink_payload: Callable[[UUID, Any], Any],
    max_lines: Optional[int] = None,
) -> Any:
    """ONE payload for the tick's band moves (``members`` = (row id, cand)).

    One member pushes exactly what it always did. Several: the worst move
    anchors the receipt; summary, body, severity and the batch fields
    describe all of them.
    """
    if len(members) == 1:
        row_id, cand = members[0]
        return sink_payload(row_id, cand)
    cap = max_lines if max_lines is not None else band_batch_max_lines()
    ordered = sorted(members, key=lambda m: _order_key(m[1]))
    anchor_id, anchor = ordered[0]
    cands = [c for _, c in ordered]
    lines = [band_move_line(c) for c in cands[:cap]]
    if len(cands) > cap:
        lines.append(
            f"… and {len(cands) - cap} more (per-tick cap {cap}; every move "
            "has its own alert row — the console's alert list)"
        )
    base = sink_payload(anchor_id, anchor)
    return base.model_copy(
        update={
            "summary": summary_line(cands)[:512],
            "detail": "\n".join(lines)[:4000],
            "severity": anchor.severity,
            "target_id": None,
            "kind": TRIGGER_BAND,
            "batch_count": len(cands),
            "batch_alert_ids": tuple(str(r) for r, _ in ordered),
        }
    )


def group_pushes(
    to_fan_out: list[tuple[UUID, Any]],
    *,
    sink_payload: Callable[[UUID, Any], Any],
    max_lines: Optional[int] = None,
) -> tuple[list[Any], int]:
    """The tick's outward payloads, band moves folded into ONE.

    Order is preserved; the batch takes the position of its first member.
    Returns ``(payloads, band_members)``.
    """
    band = [(r, c) for r, c in to_fan_out if c.trigger_class == TRIGGER_BAND]
    payloads: list[Any] = []
    placed = False
    for row_id, cand in to_fan_out:
        if cand.trigger_class != TRIGGER_BAND:
            payloads.append(sink_payload(row_id, cand))
        elif not placed:
            payloads.append(
                batch_payload(band, sink_payload=sink_payload, max_lines=max_lines)
            )
            placed = True
    return payloads, len(band)


__all__ = [
    "BAND_BATCH_MAX_LINES_ENV",
    "DEFAULT_BAND_BATCH_MAX_LINES",
    "band_batch_max_lines",
    "band_move_line",
    "batch_payload",
    "group_pushes",
    "mark_band_batch",
    "push_batch_key",
    "summary_line",
]
