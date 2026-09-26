# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The composition-side ``[[event:<uuid>]]`` expansion (V3/P2).

Extracted from :mod:`.composition_citations` when the event-ref additions
pushed that module past its module-size ceiling — the seam is the one the
gate itself names (the whole ``[[event:...]]`` marker grammar + the
expansion pass). :mod:`.composition_citations` imports
:func:`_expand_event_markers` back ONE WAY; nothing else moved.

The provenance leaf :mod:`legba.data.provenance.event_citations` owns the
grammar-free expansion (the SQL, the cap, the truncation stamp); this
module owns the composition-specific parts: the ``[[event:<uuid>]]``
marker spelling, the ``[[ref:K]]`` marker rewrite, and the
``evidence_text`` projection the composition judge's evidence map reads.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from ..provenance.event_citations import (
    EVENT_ID_RE,
    event_expansion_max_signals,
    expand_event_citation,
)

logger = logging.getLogger(__name__)

# V3/P2 — the ``[[event:<uuid>]]`` marker (composition grammar for the
# ``event:<uuid>`` ref kind; the bare / single-bracket spellings are
# tolerated too — marker-drift normalization is the norm on this plane).
_EVENT_MARKER_RE = re.compile(
    r"\[{1,2}\s*event:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*\]{1,2}"
    r"|\bevent:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _event_ref_uuid(m: "re.Match[str]") -> str:
    """The uuid a marker match carries — group 1 is the bracketed
    spelling, group 2 the bare one."""
    return m.group(1) or m.group(2)


def _event_entry_builder(
    *, signal_id: str, title: Any, source: Any, source_id: Any,
    fields: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """``expand_event_citation``'s build_entry adapter for the composition
    path: the REAL ``_citation_entry`` precedence (archived article first,
    ``distilled_body`` never — the faithfulness trust boundary), plus
    ``evidence_text`` mirrored from ``source_text`` because the composition
    judge's ``_ordinal_evidence_map`` reads THAT field. Deferred import —
    inline_target is the heavy module and this path is flag-gated.
    """
    from .inline_target import _citation_entry

    entry = _citation_entry(
        signal_id=signal_id, title=title, source=source,
        fields=fields, source_id=source_id,
    )
    entry["evidence_text"] = entry.get("source_text") or ""
    return entry


async def _expand_event_markers(
    conn: Any,
    finding: Any,
    citations: list[dict[str, Any]],
    *,
    start_ordinal: int,
    stats: dict[str, int] | None = None,
) -> int:
    """V3/P2 — expand ``[[event:<uuid>]]`` markers into per-signal entries.

    ``stats`` (optional) is the caller's receipt tally: ``event_expand_failed``
    counts an event whose expansion RAISED (the degrade path below, which
    before 2026-09-24 reached only a log line — the 09-23 review's item (1)),
    ``event_unresolved`` one that expanded to no member. Both land on the
    bare-entry path; the receipt now says which.

    Each expanded entry takes an ordinal continuing the slice space
    (``start_ordinal + i``), carries ``ref_kind="event"`` + ``event_id`` +
    the signal's real ``signal_id``/``source_text``/``evidence_text``, and
    its ``marker`` is rewritten into the sub-claim ``[[ref:K]]`` grammar —
    the body token is rewritten to the SAME markers so the composition
    floor resolves the clause through the ordinary ordinal path. An event
    whose members don't resolve leaves ONE bare ``[[event:...]]`` entry
    (traceable, no ordinal) and the body token becomes a ``[[ref:K]]``
    with no resolvable ordinal — the honest ``unresolved_citation`` shape,
    never a fabricated resolution. Returns the count of emitted entries.
    """
    body = finding.body or ""
    event_ids: list[str] = []
    for m in _EVENT_MARKER_RE.finditer(body):
        eid = _event_ref_uuid(m)
        if eid.lower() not in {e.lower() for e in event_ids}:
            event_ids.append(eid)
    for item in finding.evidence or []:
        m = EVENT_ID_RE.fullmatch(str(item).strip())
        if m and m.group(1).lower() not in {e.lower() for e in event_ids}:
            event_ids.append(m.group(1))
    if not event_ids:
        return 0

    max_signals = event_expansion_max_signals()
    next_ord = start_ordinal
    rewritten = body
    emitted = 0
    for eid in event_ids:
        try:
            expanded = await expand_event_citation(
                conn, eid, start_ordinal=next_ord,
                max_signals=max_signals,
                build_entry=_event_entry_builder,
            )
        except Exception as exc:  # noqa: BLE001 — a blip must not sink the compose
            expanded = []
            logger.warning(
                "composition.event_citation_expand_failed event=%s err=%s",
                eid, exc,
            )
            if stats is not None:
                stats["event_expand_failed"] = stats.get("event_expand_failed", 0) + 1
        else:
            if not expanded and stats is not None:
                stats["event_unresolved"] = stats.get("event_unresolved", 0) + 1
        if expanded:
            markers = "".join(f"[[ref:{e['ordinal']}]]" for e in expanded)
            for entry in expanded:
                entry["marker"] = f"[[ref:{entry['ordinal']}]]"
                citations.append(entry)
                emitted += 1
            next_ord = expanded[-1]["ordinal"] + 1
        else:
            # Cited, unresolvable: the body keeps ONE ordinal-bearing
            # marker with NO resolvable entry behind it (the floor's
            # unresolved_citation shape), and a bare non-ordinal entry
            # records WHICH event failed to resolve.
            markers = f"[[ref:{next_ord}]]"
            citations.append({
                "marker": f"[[event:{eid}]]",
                "ref_kind": "event",
                "event_id": eid,
            })
            emitted += 1
            next_ord += 1
        rewritten = _EVENT_MARKER_RE.sub(
            lambda m, _eid=eid, _markers=markers: (
                _markers
                if _event_ref_uuid(m).lower() == _eid.lower()
                else m.group(0)
            ),
            rewritten,
        )
    if rewritten != body:
        finding.body = rewritten
    return emitted


