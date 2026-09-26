"""THE CITED-SIGNAL JOIN — the one DB read `cited_mass.v1` needs.

``_run`` is DB-FREE by design (the runtime materializes the slice before calling
it), and the repaired salience key is computed over ``signals.salience`` — a
column no composition row carries. So the join happens once, at slice-read time,
and the result is DENORMALISED onto each row under
``assembly_payload.CITED_SALIENCE_ROW_KEY``, the same way ``_evidence_tier``,
``_admissibility_horizon_h`` and ``_region_coverage`` already ride the slice.

COST, AND WHY THE FLAG GATES IT. One extra query per composition slice read:
``SELECT id, salience FROM signals WHERE id = ANY($1)`` over the citation ids of
at most ``MAX_WORLD_INPUT_FINDINGS`` heads — on live data 892 citation rows
across 227 heads for a whole cycle. With ``LEGBA_COMPOSITION_ASSEMBLY`` unset the
query is NOT ISSUED and no key is stamped, so the legacy path is byte-identical
and costs nothing — which is the flag-off half of ``test_assembly_flag_off_byte_
identical``.

The flag is read at its COARSEST grain here (is any analyst assembling?) rather
than per-analyst, because the gather does not know which composer it is feeding.
Over-attaching is harmless: an unread annotation on a slice row changes no
output, and the per-analyst decision is still made where the payload is built.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping, Sequence

from .assembly_payload import ASSEMBLY_ENV, attach_cited_salience

logger = logging.getLogger(__name__)

__all__ = [
    "SIGNAL_SALIENCE_SQL",
    "assembly_any_enabled",
    "attach_cited_salience_from_db",
    "collect_signal_ids",
    "resolve_child_signal_ids",
    "signal_magnitudes",
]

SIGNAL_SALIENCE_SQL: str = """
    SELECT id, salience
      FROM signals
     WHERE id = ANY($1::uuid[])
       AND salience IS NOT NULL
"""

#: One hop down, for the POOLED grain. A composition candidate (which is what
#: the world tier reads) cites FINDINGS, not wire items — its citations carry
#: ``ref_id`` and no ``signal_id`` — so scoring it directly returns 0.0 for
#: every candidate and the world pool is flat by construction. This resolves its
#: children's own citations so the key can be pooled over their distinct signal
#: ids, which is what ``pool_salience`` describes.
CHILD_CITATIONS_SQL: str = """
    SELECT id, data->'data'->'citations' AS citations
      FROM analyst_outputs
     WHERE id = ANY($1::uuid[]) AND kind = 'finding'
"""


def assembly_any_enabled() -> bool:
    """Is the assembly path on for ANY analyst? The gate on the extra query."""
    raw = (os.environ.get(ASSEMBLY_ENV) or "").strip()
    return bool(raw) and raw != "0"


def _inner(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    env = row.get("data")
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except (TypeError, ValueError):
            return None
    if not isinstance(env, Mapping):
        return None
    inner = env.get("data")
    return inner if isinstance(inner, Mapping) else None


def collect_signal_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every distinct ``signal_id`` the given heads CITED, in first-seen order.

    The cited grain is the whole point (D-1 §1.5.2): computed over the pack a
    desk was SHOWN, all eight of a country's desks score identically, because
    the number then measures how loud that country's wire was rather than what
    any desk found in it.
    """
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        inner = _inner(row)
        if inner is None:
            continue
        cits = inner.get("citations")
        if not isinstance(cits, list):
            continue
        for c in cits:
            if not isinstance(c, Mapping):
                continue
            sid = c.get("signal_id")
            if not sid:
                continue
            key = str(sid)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def signal_magnitudes(records: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """``signal_id -> magnitude`` from ``signals`` rows. Unscored ids are ABSENT.

    Absent, not zero. An unscored signal is unmeasured; recording it as 0.0 would
    read as "the desk cited something inconsequential", which is a different and
    false claim — and 1.8% of live signals are unscored, enough to move a rank.
    """
    out: dict[str, float] = {}
    for r in records:
        sal = r.get("salience")
        if isinstance(sal, str):
            try:
                sal = json.loads(sal)
            except (TypeError, ValueError):
                continue
        if not isinstance(sal, Mapping):
            continue
        try:
            out[str(r.get("id"))] = float(sal.get("magnitude"))
        except (TypeError, ValueError):
            continue
    return out


async def resolve_child_signal_ids(
    conn, rows: Sequence[Mapping[str, Any]]
) -> dict[str, list[str]]:
    """``row_id -> [signal_id]`` pooled one hop down, for composition candidates.

    Only rows that cite NO wire item themselves are walked, so a desk-head slice
    (every country read's slice) issues no query at all. Distinct ids per parent
    — two of a country's desks resting on one shared signal are one piece of
    evidence, the same de-duplication the correlation guard already performs.
    """
    parents = [
        r for r in rows
        if not any(c.get("signal_id") for c in _citations(r))
        and r.get("derived_from")
    ]
    if not parents:
        return {}
    child_ids: list[str] = []
    for r in parents:
        child_ids.extend(str(u) for u in (r.get("derived_from") or ()))
    if not child_ids:
        return {}
    try:
        records = await conn.fetch(CHILD_CITATIONS_SQL, sorted(set(child_ids)))
    except Exception as exc:  # pragma: no cover — degrade-not-break
        logger.warning("assembly.child_signal_ids failed: %s", exc)
        return {}
    by_child: dict[str, list[str]] = {}
    for rec in records:
        cits = rec["citations"]
        if isinstance(cits, str):
            try:
                cits = json.loads(cits)
            except (TypeError, ValueError):
                cits = None
        if not isinstance(cits, list):
            continue
        by_child[str(rec["id"])] = [
            str(c["signal_id"])
            for c in cits
            if isinstance(c, Mapping) and c.get("signal_id")
        ]
    out: dict[str, list[str]] = {}
    for r in parents:
        seen: set[str] = set()
        pooled: list[str] = []
        for u in r.get("derived_from") or ():
            for sid in by_child.get(str(u), ()):
                if sid not in seen:
                    seen.add(sid)
                    pooled.append(sid)
        out[str(r.get("id") or "")] = pooled
    return out


def _citations(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inner = _inner(row)
    cits = inner.get("citations") if inner else None
    return [c for c in cits if isinstance(c, Mapping)] if isinstance(cits, list) else []


async def attach_cited_salience_from_db(conn, rows: Sequence[Mapping[str, Any]]) -> None:
    """Stamp `cited_mass.v1` onto every row in ``rows``, in place.

    Best-effort by design: a failed join leaves the rows unannotated, and an
    unannotated row sorts LAST inside its severity band (never above a scored
    peer) rather than taking the run down. The assembly itself still fails loud
    on the things that must fail loud — a span that will not quote — but a
    salience join is a ranking input, not a truth claim.
    """
    children = await resolve_child_signal_ids(conn, rows)
    ids = collect_signal_ids(rows)
    for pooled in children.values():
        ids.extend(s for s in pooled if s not in set(ids))
    ids = list(dict.fromkeys(ids))
    if not ids:
        return
    try:
        records = await conn.fetch(SIGNAL_SALIENCE_SQL, ids)
    except Exception as exc:  # pragma: no cover — degrade-not-break
        logger.warning(
            "assembly.cited_salience join failed over %d ids: %s", len(ids), exc
        )
        return
    mags = signal_magnitudes([dict(r) for r in records])
    attach_cited_salience(rows, mags, children)
    logger.debug(
        "assembly.cited_salience heads=%d signals=%d scored=%d pooled_parents=%d",
        len(rows), len(ids), len(mags), len(children),
    )
