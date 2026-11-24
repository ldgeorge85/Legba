# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Type a Consult answer's cited refs by the table each id is actually in.

The consult kind's ``cited_substrate_refs`` is a flat list of UUIDs, and the
projection used to label every one ``signal`` ("the kind label is not
load-bearing"). It became load-bearing with the Record sheet (U-10): a click on
a cited situation opened ``/lineage/signal/<situation id>`` and got a 404 (roll
84's live walk, tracker W11-F310). The deep transport stores the refs as bare
strings, which the console could not open at all.

:func:`typed_refs` resolves the kinds in one batched lookup per substrate table
(the lineage walk's own catalogue, ``lineage_api._SUBSTRATE_TABLES``, so a kind
named here is always one ``/lineage/{kind}/{id}`` serves). An id found in no
table is labelled :data:`UNRESOLVED` — the console shows it without a link
rather than open a walk that cannot succeed. A failed lookup leaves the refs as
they were (best-effort, like every other read on this path): an answer is never
held back by its citation labels.

Used on the way out (``consult_api._invoke_and_project``, so the live answer and
the persisted turn carry the true kind) and on the way back in
(``consult_sessions_api``, so turns written before this fix read correctly).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from .lineage_api import _SUBSTRATE_TABLES

logger = logging.getLogger(__name__)

__all__ = ["MAX_REFS", "PLACEHOLDER_KIND", "UNRESOLVED", "resolve_kinds", "typed_refs"]

#: The label the old projection gave every ref; only these (and bare ids) are re-typed.
PLACEHOLDER_KIND = "signal"
#: The label for an id no substrate table holds.
UNRESOLVED = "unresolved"
#: Refs typed in one call; the rest keep their label (an answer cites far fewer).
MAX_REFS = 500


def _uuid(v: Any) -> UUID | None:
    try:
        return UUID(str(v))
    except (TypeError, ValueError, AttributeError):
        return None


async def resolve_kinds(pg: Any, ids: list[str]) -> dict[str, str]:
    """``{id: kind}`` for every id found, one ``id = ANY`` query per table."""
    uuids = list(dict.fromkeys(u for u in (_uuid(i) for i in ids[:MAX_REFS]) if u is not None))
    if not uuids:
        return {}
    found: dict[str, str] = {}
    async with pg.acquire() as conn:
        for spec in _SUBSTRATE_TABLES:
            rows = await conn.fetch(
                f"SELECT id, {spec.kind_expr} AS k FROM {spec.table} WHERE id = ANY($1::uuid[])",
                uuids,
            )
            for r in rows:
                found.setdefault(str(r["id"]), str(r["k"]))
    return found


def _as_dict(ref: Any) -> dict[str, Any] | None:
    if isinstance(ref, str):
        return {"kind": None, "id": ref, "description": None} if ref else None
    if isinstance(ref, dict):
        return dict(ref) if ref.get("id") else None
    dump = getattr(ref, "model_dump", None)
    return dump() if callable(dump) else None


async def typed_refs(pg: Any, refs: list[Any]) -> list[Any]:
    """The refs with their true kinds, in the shape they came in.

    Pydantic refs come back as the same model class; dicts and bare strings
    come back as ``{kind, id, description}`` dicts; the list keeps its length
    and order. Only placeholder-labelled
    (``signal``) or unlabelled refs are looked up; any other kind was set by
    whoever wrote it and is kept.
    """
    if not refs:
        return refs
    rows = [_as_dict(r) for r in refs]
    todo = [r for r in rows if r and r.get("kind") in (None, "", PLACEHOLDER_KIND)][:MAX_REFS]
    kinds: dict[str, str] = {}
    if todo:
        try:
            kinds = await resolve_kinds(pg, [str(r["id"]) for r in todo])
        except Exception:  # noqa: BLE001 — labels are best-effort; the answer is not
            logger.warning("consult.cited_ref_kinds.lookup_failed", exc_info=True)
            todo = []
    for r in todo:
        r["kind"] = kinds.get(str(r["id"]), UNRESOLVED)
    for r in rows:  # a bare id the lookup did not reach keeps the old label
        if r and not r.get("kind"):
            r["kind"] = PLACEHOLDER_KIND
    out: list[Any] = []
    for orig, r in zip(refs, rows):
        if r is None:  # not a ref (no id): passed through untouched, the length kept
            out.append(orig)
            continue
        cls = type(orig)
        out.append(cls(**r) if hasattr(orig, "model_dump") else r)
    return out
