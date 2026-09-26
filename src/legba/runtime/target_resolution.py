# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve a target_id that carries no rows of its own to its constituent desks.

Defect 3 of the 2026-09-16 consult review: ``list_findings`` and
``query_hypotheses`` on ``situation_iran_war`` both returned zero while the
US-Iran war was the single most active thing in the corpus. Confirmed
read-only against the live DB:

  * ``situation_iran_war`` IS a real, ACTIVE head target descriptor (L1,
    ``analyst.use = inline_target``, owner ``phase5c``) — the planner did not
    hallucinate it; ``list_targets`` handed it over.
  * 0 ``analyst_outputs`` and 0 ``hypotheses`` rows carry that ``target_id``,
    at any time. No producer has ever written for it.
  * It has no members under the house membership idiom either: no active head
    target carries ``situation_iran_war`` in ``scope.tags`` (the convention
    ``composition_slice._REGION_MEMBERS_SQL`` uses, where a MENA desk is
    tagged ``region_mena``). Its own ``scope.geo`` is empty.

So the frame is an ORPHAN: real, active, offered to the planner, and empty.
The producer-side repair (tagging the member desks, or wiring an analyst onto
the frame) is a DESCRIPTOR change and out of this lane. The reader-side repair
is the one that stops a live orphan reading as "nothing is happening", and it
is the correct half regardless: a tool that answers "zero rows" to a question
about an active frame has told the analyst something FALSE.

The ladder, most authoritative first:

  1. **tag membership** — active head targets carrying the frame id as a
     ``scope.tags`` entry. The house idiom; resolves every ``region_*`` frame.
  2. **shared geo** — active head targets whose ``scope.geo`` intersects the
     frame's. Resolves a frame scoped to places rather than to a tag.
  3. **gazetteer** — the frame's ``scope.themes`` / ``scope.tags`` tokens
     matched against ``iso_countries`` names, then back to the desks covering
     those ISO codes. This is the rung that resolves ``situation_iran_war``:
     theme ``iran`` → ``IR`` → ``country_watch_ir`` + ``lane_hormuz``.

An unresolvable target is reported as such, with its scope predicate attached
so the planner can still read the frame's signal slice through
``search_signals(scope_predicate=...)``. Nothing here fabricates a row.
"""

from __future__ import annotations

import json
from typing import Any

#: Tokens that describe a frame's KIND rather than its subject. Matching these
#: against the gazetteer would drag in unrelated desks, so they never become
#: gazetteer probes.
_GENERIC_TOKENS = frozenset({
    "thematic", "situation", "geopolitical", "news", "region", "frame",
    "watch", "g20", "supply_chain", "conflict", "war", "military", "crisis",
    "security", "economic", "energy", "political", "global", "world",
    "country", "lane", "flow", "maritime", "chokepoint", "shipping",
})

#: A gazetteer probe shorter than this matches too much ("us" is a substring
#: of hundreds of country names, "ir" of several).
_MIN_TOKEN_CHARS = 4

#: Cap on resolved desks. A frame that resolves to the whole roster has not
#: been resolved — it has been diluted — so the ladder stops at a set small
#: enough to actually read.
MAX_MEMBERS = 8

_HEAD_TARGET_SQL = """
    SELECT descriptor_id, body
      FROM target_descriptors
     WHERE descriptor_id = $1
       AND is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
     LIMIT 1
"""

#: The house membership idiom (``composition_slice._REGION_MEMBERS_SQL``): a
#: frame and its members share ONE tag, and that tag IS the frame's target id.
_TAG_MEMBERS_SQL = """
    SELECT descriptor_id
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND descriptor_id <> $1
       AND (body -> 'scope' -> 'tags') ? $1
     ORDER BY descriptor_id
"""

_GEO_MEMBERS_SQL = """
    SELECT descriptor_id
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
       AND descriptor_id <> $1
       AND (body -> 'scope' -> 'geo') ?| $2::text[]
     ORDER BY descriptor_id
"""

_GAZETTEER_SQL = """
    SELECT DISTINCT iso2
      FROM iso_countries
     WHERE lower(name) LIKE ANY($1::text[])
        OR lower(official) LIKE ANY($1::text[])
"""

_ACTIVE_TARGET_IDS_SQL = """
    SELECT descriptor_id
      FROM target_descriptors
     WHERE is_head = TRUE
       AND state = 'active'
     ORDER BY descriptor_id
"""


def _scope(body: Any) -> dict[str, Any]:
    """The ``scope`` block off a descriptor body (asyncpg jsonb may be str)."""
    parsed = json.loads(body) if isinstance(body, str) else (body or {})
    scope = parsed.get("scope") if isinstance(parsed, dict) else None
    return scope if isinstance(scope, dict) else {}


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _gazetteer_probes(scope: dict[str, Any]) -> list[str]:
    """SQL ``LIKE`` probes for the frame's subject tokens, generic ones removed."""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in (*_str_list(scope.get("themes")), *_str_list(scope.get("tags"))):
        token = raw.lower().strip()
        if len(token) < _MIN_TOKEN_CHARS or token in _GENERIC_TOKENS:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(f"%{token}%")
    return tokens


async def resolve_member_targets(conn: Any, target_id: str) -> dict[str, Any]:
    """Resolve ``target_id`` to the desks that hold its constituent evidence.

    Returns ``{"exists", "members", "via", "scope_predicate", ...}``.
    ``exists`` False means the id is not an active head target at all — the
    caller must say ``unknown_target`` rather than "no rows", because those
    are completely different answers to an analyst.
    """
    record = await conn.fetchrow(_HEAD_TARGET_SQL, str(target_id))
    if record is None:
        known = [str(r["descriptor_id"]) for r in await conn.fetch(_ACTIVE_TARGET_IDS_SQL)]
        return {"exists": False, "members": [], "via": None, "known_target_ids": known}

    scope = _scope(record["body"])
    out: dict[str, Any] = {
        "exists": True,
        "members": [],
        "via": None,
        "scope_predicate": scope.get("predicate"),
        "scope_geo": _str_list(scope.get("geo")),
        "scope_themes": _str_list(scope.get("themes")),
    }

    rows = await conn.fetch(_TAG_MEMBERS_SQL, str(target_id))
    if rows:
        out["members"] = [str(r["descriptor_id"]) for r in rows][:MAX_MEMBERS]
        out["via"] = "tag_membership"
        return out

    geo = out["scope_geo"]
    if geo:
        rows = await conn.fetch(_GEO_MEMBERS_SQL, str(target_id), geo)
        if rows:
            out["members"] = [str(r["descriptor_id"]) for r in rows][:MAX_MEMBERS]
            out["via"] = "shared_geo"
            return out

    probes = _gazetteer_probes(scope)
    if probes:
        iso_rows = await conn.fetch(_GAZETTEER_SQL, probes)
        codes = [str(r["iso2"]) for r in iso_rows]
        if codes:
            rows = await conn.fetch(_GEO_MEMBERS_SQL, str(target_id), codes)
            if rows:
                out["members"] = [str(r["descriptor_id"]) for r in rows][:MAX_MEMBERS]
                out["via"] = "gazetteer_geo"
                out["resolved_iso"] = codes
                return out

    return out


def unresolved_note(resolution: dict[str, Any], target_id: str) -> str:
    """One honest sentence for a target that produced nothing and resolved to
    nothing — so the model reports an EMPTY FRAME, not an empty world."""
    if not resolution.get("exists"):
        return (
            f"{target_id!r} is not an active target. Call list_targets to get "
            f"the valid ids before filtering on one."
        )
    if resolution.get("scope_predicate"):
        return (
            f"{target_id!r} is an active target with no findings or hypotheses "
            f"of its own and no member desks. Its evidence is the raw signal "
            f"slice its scope predicate selects — read it with search_signals "
            f"or search_corpus, and do NOT read this empty result as quiet."
        )
    return (
        f"{target_id!r} is an active target that no producer writes for. Read "
        f"the country desks directly; this empty result is a coverage gap, "
        f"not an absence of events."
    )


async def widen_to_member_targets(
    conn: Any,
    sql: str,
    params: list[Any],
    target_param: int,
    target_id: str,
) -> tuple[list[Any], dict[str, Any]]:
    """Second pass for a ``target_id`` filter that matched nothing.

    Resolves the target to its constituent desks
    (:func:`resolve_member_targets`) and re-runs the SAME query with the member
    ids substituted into the ``target_id = ANY($n::text[])`` slot — which is
    why the readers pass an ARRAY parameter even for one id: widening is then a
    parameter swap, not a second query that has to be kept in sync.

    Returns ``(records, resolution)``. An unresolvable target yields no records
    and a resolution the caller stamps as an honest note; nothing here invents
    a row.
    """
    resolution = await resolve_member_targets(conn, target_id)
    members = resolution.get("members") or []
    if not members:
        return [], resolution
    widened = list(params)
    widened[target_param - 1] = members
    return list(await conn.fetch(sql, *widened)), resolution


def stamp_target_resolution(
    out: dict[str, Any], target_id: str, resolution: dict[str, Any] | None,
) -> None:
    """Record HOW an empty target filter was answered, in-band.

    A reader that widens its own filter must say so, or the model reads desk
    findings as the frame's own. ``status`` is the field a planner can branch
    on; ``note`` is the sentence it will actually read.
    """
    if resolution is None:
        return
    if not resolution.get("exists"):
        out["status"] = "unknown_target"
        out["note"] = unresolved_note(resolution, target_id)
        out["known_target_ids"] = resolution.get("known_target_ids") or []
        return
    members = resolution.get("members") or []
    if members and out.get("rows"):
        via = resolution.get("via")
        # Rung 1 resolves true MEMBERSHIP (a frame and its desks share one
        # tag); rungs 2-3 resolve scope OVERLAP. Say which — "its member
        # desks" and "the desks covering its scope" are different claims, and
        # an analyst weighs them differently.
        relation = (
            "its member desks" if via == "tag_membership"
            else "the desks covering its scope"
        )
        out["status"] = "resolved_to_member_targets"
        out["resolved_from"] = target_id
        out["resolved_targets"] = members
        out["resolved_via"] = via
        out["note"] = (
            f"{target_id!r} carries no rows of its own; these rows come from "
            f"{relation} ({', '.join(members)}), resolved via {via}. Each "
            f"row's own target_id says which desk it came from — cite it as "
            f"that desk, not as {target_id!r}."
        )
        return
    out["status"] = "no_rows_for_target"
    out["note"] = unresolved_note(resolution, target_id)
    if resolution.get("scope_predicate"):
        out["scope_predicate"] = resolution["scope_predicate"]


__all__ = [
    "MAX_MEMBERS",
    "resolve_member_targets",
    "stamp_target_resolution",
    "unresolved_note",
    "widen_to_member_targets",
]
