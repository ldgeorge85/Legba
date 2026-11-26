# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk's SOURCE ROSTER state in its window, read ONCE per shadow run.

WHY. A contract-v2 absence ("no row in this desk's collection reports X") is
only as strong as the collection: if a third of the feeds the desk draws on
were failing, the absence says less. The computed block had to state the
silent / failing feeds as unknown (``not_handed``) because the run made no
roster read. This leaf is that read and the pure classification of it.

THE ROSTER (the desk's source POOL). A target descriptor's ``sources`` are
either explicit ``source_id`` refs or SELECTORS (kinds, tags ⊇, geo ∩,
languages ∩, owner tenant) over the active source heads — every country desk
today declares one selector (news RSS) and narrows its SIGNALS by geo, so its
pool is every feed the selector admits. A target with no ``sources`` reads
the tenant-wide pool: every active head (said so in ``pool_basis``).

THE STATE, per pool feed, from ``source_poll_outcomes`` in the run's window
(the provenance every poll writes — the watchdog's evidence):

==========================  ==================================================
``delivered_to_desk``       a row of the handed slice came from it
``delivered_out_of_scope``  it polled productively, but none of what it
                            delivered is in this desk's slice (not about it)
``silent``                  polled cleanly (empty / gated), nothing new
``failing``                 every poll in the window errored
``not_polled``              no poll recorded in the window
==========================  ==================================================

THE READ. ONE statement (:data:`ROSTER_SQL`): the target head by its key, the
active source heads, and per head one windowed descent of
``source_poll_outcomes_source_time_idx`` (source_id, occurred_at DESC). The
caller owns the connection and its transaction; nothing is written.

A LEAF: stdlib only.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

#: The basis a roster state is stamped with (the computed block carries it).
ROSTER_BASIS = "poll_outcomes"
#: A bound on one roster read (seconds; asyncpg ``timeout``).
ROSTER_TIMEOUT_S = 10.0

ROSTER_SQL = """
SELECT d.descriptor_id AS source_id,
       d.body #>> '{identity,kind}' AS kind,
       d.body #> '{scope,tags}' AS tags,
       d.body #> '{scope,geo}' AS geo,
       d.body #> '{scope,languages}' AS languages,
       d.body #>> '{scope,owner_tenant}' AS tenant,
       w.polls, w.errors, w.productive, w.signals_written, w.last_outcome,
       (SELECT t.body -> 'sources' FROM target_descriptors t
         WHERE t.descriptor_id = $1 AND t.is_head LIMIT 1) AS target_sources
  FROM source_descriptors d
  LEFT JOIN LATERAL (
        SELECT count(*) AS polls,
               count(*) FILTER (WHERE p.outcome = 'error') AS errors,
               count(*) FILTER (WHERE p.outcome = 'success') AS productive,
               COALESCE(sum(p.signals_written), 0) AS signals_written,
               (array_agg(p.outcome ORDER BY p.occurred_at DESC))[1] AS last_outcome
          FROM source_poll_outcomes p
         WHERE p.source_id = d.descriptor_id
           AND p.occurred_at >= $2 AND p.occurred_at <= $3
       ) w ON TRUE
 WHERE d.is_head AND d.state = 'active'
"""

STATES = ("delivered_to_desk", "delivered_out_of_scope", "silent", "failing", "not_polled")


async def fetch_roster(conn: Any, target_id: str | None, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The roster rows for ``target_id`` over ``[start, end]`` on ``conn``."""
    rows = await conn.fetch(ROSTER_SQL, target_id or "", start, end, timeout=ROSTER_TIMEOUT_S)
    return [dict(r) for r in rows]


def _list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [raw]
    return [str(x) for x in raw if isinstance(x, (str, int))] if isinstance(raw, list) else []


def _refs(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [r for r in raw if isinstance(r, Mapping)] if isinstance(raw, list) else []


def selector_admits(selector: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """A target's source SELECTOR admits a source head: kind in ``kinds``,
    head tags ⊇ selector tags, geo ∩ and languages ∩ when the selector names
    any, the same owner tenant when it names one."""
    kinds = _list(selector.get("kinds"))
    if kinds and str(row.get("kind") or "") not in kinds:
        return False
    if not set(_list(selector.get("tags"))) <= set(_list(row.get("tags"))):
        return False
    for key in ("geo", "languages"):
        want = set(_list(selector.get(key)))
        if want and not (want & set(_list(row.get(key)))):
            return False
    tenant = selector.get("owner_tenant")
    return not tenant or str(row.get("tenant") or "") == str(tenant)


def roster_pool(rows: Iterable[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], str]:
    """``(pool rows, pool_basis)`` — the heads the target's sources admit."""
    rows = list(rows)
    refs = _refs(rows[0].get("target_sources")) if rows else []
    if not refs:
        return rows, "all_active_sources"
    ids = {str(r["source_id"]) for r in refs if r.get("source_id")}
    sels = [r["source_selector"] for r in refs if isinstance(r.get("source_selector"), Mapping)]
    pool = [r for r in rows if str(r["source_id"]) in ids or any(selector_admits(s, r) for s in sels)]
    basis = "target_selector" if sels and not ids else "target_sources" if ids and not sels else "target_mixed"
    return pool, basis


def classify(row: Mapping[str, Any], delivered: frozenset[str]) -> str:
    sid = str(row.get("source_id"))
    polls = int(row.get("polls") or 0)
    if sid in delivered:
        return "delivered_to_desk"
    if polls == 0:
        return "not_polled"
    if int(row.get("errors") or 0) >= polls:
        return "failing"
    if int(row.get("productive") or 0) > 0:
        return "delivered_out_of_scope"
    return "silent"


def roster_state(rows: Iterable[Mapping[str, Any]], delivered: Iterable[str]) -> dict[str, Any]:
    """The roster state the computed block carries (pure). ``delivered`` are
    the source ids of the handed slice's rows."""
    delivered = frozenset(str(s) for s in delivered if s)
    pool, basis = roster_pool(rows)
    if not pool:
        return {"basis": "roster_empty", "pool_basis": basis, "roster_size": 0}
    by: dict[str, list[str]] = {s: [] for s in STATES}
    for r in pool:
        by[classify(r, delivered)].append(str(r["source_id"]))
    clean = sum(1 for r in pool if int(r.get("polls") or 0) > int(r.get("errors") or 0))
    return {
        "basis": ROSTER_BASIS, "pool_basis": basis, "roster_size": len(pool),
        "delivered_to_desk": len(by["delivered_to_desk"]),
        "delivered_out_of_scope": len(by["delivered_out_of_scope"]),
        "silent": sorted(by["silent"]), "failing": sorted(by["failing"]), "not_polled": sorted(by["not_polled"]),
        "polling_clean": clean,
        "slice_sources_outside_pool": len(delivered - {str(r["source_id"]) for r in pool}),
    }


__all__ = [
    "ROSTER_BASIS",
    "ROSTER_SQL",
    "ROSTER_TIMEOUT_S",
    "STATES",
    "classify",
    "fetch_roster",
    "roster_pool",
    "roster_state",
    "selector_admits",
]
