# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE ROW STAMPS — what an ``assembly.v1`` payload stamps on its own finding
row: the severity (max over carried blocks), the confidence (the weakest
carried block), the tags that carry severity to its read column, and the one
``as_of`` spelling.

Split out of :mod:`assembly_payload` (W1, 2026-11-07) to keep that module under
the size gate's 1,500-line entry threshold (``tests/test_module_size_gate.py``).
PURE MOVE. Imported ONE WAY and re-exported by :mod:`assembly_payload`, the
``assembly_cap`` / ``assembly_carry`` idiom: this leaf imports nothing from it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "assembly_confidence",
    "assembly_severity",
    "assembly_tags",
    "now_iso",
]

#: Severity ranks, high to low. The assembly is the FIRST composition row ever to
#: carry a severity: it is NULL on 100% of composition rows today (0 of 1,108 in
#: 14 days) while desk heads carry it on ~99%. Setting it deterministically to
#: the max across carried blocks is what makes the dead severity dot in the
#: history list render for the first time, at zero extra cost.
_SEVERITY_ORDER: dict[str, int] = {
    "critical": 5, "high": 4, "elevated": 3,
    "moderate": 2, "medium": 2, "low": 1, "info": 0,
}


def _severity_rank(level: Any) -> int:
    return _SEVERITY_ORDER.get(str(level or "").strip().lower(), -1)


def assembly_severity(blocks: Sequence[Mapping[str, Any]]) -> str | None:
    """The read's severity = the MAX over carried blocks. ``None`` when no
    carried block declares one — an unstated severity is stated as unknown."""
    best, best_rank = None, -1
    for b in blocks:
        r = _severity_rank(b.get("severity"))
        if r > best_rank:
            best_rank, best = r, str(b.get("severity") or "").strip().lower()
    return best or None


def assembly_confidence(payload: Mapping[str, Any]) -> float:
    """The row's ``confidence`` — the WEAKEST carried block's effective score.

    Deterministic and conservative, and it is the house's own idiom: every
    admission predicate in the tree folds to ``LEAST(confidence,
    faithfulness_score)``. A composed read is no stronger than the weakest thing
    it quotes, and under the assembly it quotes everything it carries — so the
    minimum is not a heuristic, it is the arithmetic of what the page contains.
    Blocks with no score do not drag it down (unscored is not weak); a payload
    with no scored block at all returns 0.5, the model default, because that is
    the honest "no basis to move it".
    """
    scores = []
    for b in payload.get("blocks") or ():
        v = (b.get("verify") or {}).get("effective_confidence")
        try:
            scores.append(float(v))
        except (TypeError, ValueError):
            continue
    return round(min(scores), 4) if scores else 0.5


def assembly_tags(payload: Mapping[str, Any]) -> list[str]:
    """The row's ``tags``, which is how ``severity`` reaches its READ COLUMN.

    Severity travels as a ``severity:<level>`` TAG that the write path lifts to
    ``analyst_outputs.severity`` (``models.severity_from_tags``). It is NULL on
    100% of composition rows today — 0 of 1,108 in 14 days — while desk heads
    carry it on ~99%, which is why the severity dot in the world history list has
    never rendered. The assembly sets it to the max across carried blocks, and
    the dot works for the first time at zero extra cost.
    """
    tags = ["assembly", f"regime:{payload.get('regime')}"]
    sev = assembly_severity(payload.get("blocks") or ())
    if sev:
        tags.append(f"severity:{sev}")
    lead = (payload.get("lead") or {}).get("kind")
    if lead:
        tags.append(f"lead:{lead}")
    return tags


def now_iso() -> str:
    """UTC now, ISO-8601 with a ``Z``-equivalent offset. One spelling."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
