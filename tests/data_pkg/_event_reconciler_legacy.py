# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The all-pairs ``event_reconciler`` pair walk as it ran until N-V (2026-11-13).

Kept VERBATIM (only renamed) as the reference the indexed implementation is
proven equal to. It is quadratic: on the live 5,000-row scan it spent ~115 s
of the event loop per pass. Never import it from production code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from legba.data.analysts.deterministic_handlers.event_reconciler import (
    DEFAULT_EVOLVES_MAX_GAP_HOURS,
    DEFAULT_EVOLVES_MIN_ACTORS,
    _event_time_bounds,
    _uuid,
)


def legacy_shared_actors(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    """Count shared resolved actors between two event rows."""
    a = {_uuid(v) for v in (left.get("actor_ids") or ())}
    b = {_uuid(v) for v in (right.get("actor_ids") or ())}
    return len({x for x in a if x is not None} & {x for x in b if x is not None})


def legacy_evolves_pairs(
    rows: Iterable[Mapping[str, Any]],
    *,
    min_actors: int = DEFAULT_EVOLVES_MIN_ACTORS,
    max_gap_hours: int = DEFAULT_EVOLVES_MAX_GAP_HOURS,
    should_stop: Callable[[], bool] | None = None,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], int]]:
    """Latest temporally adjacent predecessor sharing ``min_actors`` actors."""
    items = [dict(r) for r in rows if _uuid(r.get("id")) is not None]
    out: list[tuple[Mapping[str, Any], Mapping[str, Any], int]] = []
    for src in reversed(items):
        if should_stop is not None and should_stop():
            break
        src_start, _src_end = _event_time_bounds(src)
        if src_start is None:
            continue
        best: tuple[datetime, int, str, Mapping[str, Any]] | None = None
        for dst in items:
            if dst["id"] == src["id"]:
                continue
            _dst_start, dst_end = _event_time_bounds(dst)
            if dst_end is None or dst_end > src_start:
                continue
            gap_hours = (src_start - dst_end).total_seconds() / 3600.0
            if gap_hours > max_gap_hours:
                continue
            shared = legacy_shared_actors(src, dst)
            if shared < min_actors:
                continue
            key = (dst_end, shared, str(dst.get("id")), dst)
            if best is None or key[:3] > best[:3]:
                best = key
        if best is not None:
            out.append((src, best[3], best[1]))
    return out
