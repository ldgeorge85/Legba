# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane K-2 — the FEED ROSTER block: a desk's declared feeds, counted in code.

THE GAP THIS CLOSES. A desk that reads a fixed set of feeds (the cyber desk's
agency advisories, trade outlets and provider status pages) owes its reader a
coverage statement — which of its feeds said nothing this window — and owes it
the SOURCE KIND of every row, because what a row can support depends on it: an
agency advisory supports "the agency published advisory Y about Z", a trade
outlet supports "the outlet reported", a provider status page supports "the
provider declared an incident / a recovery". A slice row renders its
canonical URL and nothing else (a register feed's row has no URL at all), so a
model asked to infer either from the rendered slice would be guessing.

So, the spread_block precedent: a NUMBER THE MODEL DID NOT COMPUTE, rendered
into the slice header. Opt-in per descriptor through
``method.options.feed_roster`` — a list of ``"<source_id>=<kind>"`` entries
(the kind is the descriptor's own word; this module attaches no meaning to it).
For each declared feed the block prints how many canonical signals the feed
put into the collection inside the slice's window (one grouped read over
``idx_signals_source_created_at``), and the slice ordinals ``[N]`` of that
feed's rows. A feed with zero collected rows is printed ``SILENT``. Rows
belonging to no declared feed are named as "other outlets".

THE WINDOW. The same window the slice was cut with: the watermark window's
``window_start`` when the slice reader stamped one (``slice_coverage``), else
the descriptor window back from now.

Degrade, never sink: no pool, or a failed count, prints "collected count
unavailable" per feed and keeps the ordinals; the run continues. Absent option
⇒ ``None`` ⇒ the header is byte-identical.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..claim_support import claim_of

logger = logging.getLogger(__name__)

FEED_ROSTER_OPTION = "feed_roster"

#: ``source.cisa.kev=advisory`` — the option catalog pins the same shape.
ENTRY_PATTERN = re.compile(r"^([a-z0-9][a-z0-9_.-]*)=([a-z][a-z0-9_]*)$")

#: Canonical rows each declared feed put into the collection since $2.
_COUNT_SQL = """
SELECT source_id, count(*)::int AS n
  FROM signals
 WHERE source_id = ANY($1::text[])
   AND created_at > $2
   AND fetched_at > $2
   AND canonical_signal_id IS NULL
 GROUP BY source_id
"""


def parse_roster(raw: Any) -> list[tuple[str, str]]:
    """``[(source_id, kind)]`` in declared order; malformed entries dropped."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        m = ENTRY_PATTERN.match(str(item).strip())
        if m is None or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        out.append((m.group(1), m.group(2)))
    return out


def window_start(options: Mapping[str, Any], *, now: datetime | None = None) -> datetime:
    """The slice's own window start (watermark stamp first, then the hours)."""
    cov = options.get("slice_coverage")
    if isinstance(cov, Mapping) and isinstance(cov.get("window_start"), str):
        try:
            start = datetime.fromisoformat(cov["window_start"])
            return start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    hours = options.get("slice_window_hours")
    hours = hours if isinstance(hours, int) and not isinstance(hours, bool) and hours > 0 else 24
    return (now or datetime.now(tz=timezone.utc)) - timedelta(hours=hours)


async def collected_counts(
    pg: Any, source_ids: Iterable[str], since: datetime,
) -> dict[str, int] | None:
    """``source_id -> collected rows`` (absent = 0), or ``None`` when unknown."""
    ids = sorted({str(s) for s in source_ids if s})
    if pg is None or not ids:
        return None
    try:
        rows = await pg.fetch(_COUNT_SQL, ids, since)
    except Exception as exc:  # noqa: BLE001 — degrade, never sink the run
        logger.warning("feed_roster.count_failed err=%s", exc)
        return None
    return {str(r["source_id"]): int(r["n"]) for r in rows}


def build_feed_roster_block(
    sliced: Sequence[Mapping[str, Any]],
    roster: Sequence[tuple[str, str]],
    collected: Mapping[str, int] | None,
    *,
    since: datetime,
) -> str:
    """The rendered block (pure). Ordinals are the slice's own, 1-based."""
    ordinals: dict[str, list[int]] = {}
    for idx, row in enumerate(sliced, start=1):
        ordinals.setdefault(str(row.get("source_id") or ""), []).append(idx)
    declared = {sid for sid, _ in roster}
    lines = [
        "FEED ROSTER — this desk's declared feeds, counted in code since "
        f"{since.isoformat(timespec='minutes')} (the slice window). Orientation, not "
        "evidence: it carries no [N] of its own; the [N] listed are the slice rows "
        "each feed supplied.",
    ]
    for kind in dict.fromkeys(k for _, k in roster):
        lines.append(f"  {kind}:")
        for sid, k in roster:
            if k != kind:
                continue
            refs = "".join(f"[{i}]" for i in ordinals.get(sid, [])) or "none"
            if collected is None:
                count = "collected count unavailable this run"
            else:
                n = int(collected.get(sid, 0))
                count = f"{n} collected" + (" — SILENT" if n == 0 else "")
            lines.append(f"    {sid}: {count}; in this slice: {refs}")
    other = sorted(i for sid, idxs in ordinals.items() if sid not in declared for i in idxs)
    lines.append(
        "  other outlets (general reporting the desk's selector matched): "
        + ("".join(f"[{i}]" for i in other) if other else "none")
    )
    # C-1: CLAIM rows (payload.claim, stamped at ingest from a source of class
    # 'claim') are named apart, by ordinal and claimant. Printed only when the
    # slice holds one, so every other header is byte-identical.
    claims = [(i, claim_of(row.get("data"))) for i, row in enumerate(sliced, start=1)]
    claims = [(i, c) for i, c in claims if c is not None]
    if claims:
        lines.append(
            "  CLAIM rows — each supports only \"<claimant> claimed/posted/listed ...\" "
            "(attributed, dated), never the event, never a corroboration: "
            + "".join(f"[{i}] ({c.get('claimant') or 'unnamed claimant'})" for i, c in claims)
        )
    return "\n".join(lines)


async def feed_roster_from_options(
    options: Mapping[str, Any], sliced: Sequence[Mapping[str, Any]], pg: Any,
    steps: list[dict[str, Any]] | None = None,
) -> str | None:
    """The block for a descriptor that declares ``feed_roster``; else ``None``."""
    roster = parse_roster(options.get(FEED_ROSTER_OPTION))
    if not roster:
        return None
    since = window_start(options)
    collected = await collected_counts(pg, (sid for sid, _ in roster), since)
    if steps is not None:
        steps.append({
            "phase": "plan", "kind": FEED_ROSTER_OPTION, "feeds": len(roster),
            "counted": collected is not None,
            "silent": None if collected is None else sorted(
                sid for sid, _ in roster if not collected.get(sid)),
        })
    return build_feed_roster_block(sliced, roster, collected, since=since)


__all__ = [
    "ENTRY_PATTERN",
    "FEED_ROSTER_OPTION",
    "build_feed_roster_block",
    "collected_counts",
    "feed_roster_from_options",
    "parse_roster",
    "window_start",
]
