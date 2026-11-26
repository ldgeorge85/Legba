# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolving a desk's ``[N]`` markers against its render-time index.

EXTRACTED VERBATIM from ``inline_target`` to pay for
the run's typed handed list (``AnalystMethodResult.handed_refs``) under the
module-size gate: :func:`_citation_from_index_entry`, :func:`_extract_citations`
and :data:`_PREBUILT_CITATION_KEY` are imported back ONE WAY and re-exported
there, so every caller and test is unchanged. ``_PREBUILT_CITATION_KEY`` is
the key ``citation_rows.PREBUILT_INDEX_KEY`` reads (a test holds them equal).
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from ..claim_support import with_claim_stamp

#: The bare unit marker (``inline_target._CITATION_MARKER_RE``, same pattern).
_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")


#: QW1-B — index key carrying an ALREADY-BUILT citation for a non-signal block
#: (a DESK GROUNDING block: prior read / situation register / desk baseline /
#: standing questions). Those blocks are not ``signals`` rows, have no
#: ``signal_id``, and each has its own honest ref shape built by
#: ``unit_grounding.citation_for_block`` — so the index carries the finished
#: citation rather than the field bag :func:`_citation_entry` builds for a signal.
_PREBUILT_CITATION_KEY = "_citation"


def _citation_from_index_entry(
    n: int, entry: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """One resolved citation entry for slice position ``n`` — shared by
    :func:`_extract_citations`'s marker-driven resolution and the A2 (2026-07-31)
    unmarked-basis fallback in :func:`run_method`. ``None`` when the entry
    carries no resolvable ``signal_id`` (never a fabricated ref)."""
    if not entry:
        return None
    # QW1-B: a DESK GROUNDING block hands over its finished citation verbatim —
    # ref_kind + the REAL underlying ids + the rendered evidence_text, and
    # deliberately NO ``signal_id`` (it is not a signal) and NO fabricated
    # ``ref_id`` on the synthetic blocks.
    prebuilt = entry.get(_PREBUILT_CITATION_KEY)
    if isinstance(prebuilt, Mapping):
        return dict(prebuilt)
    if not entry.get("signal_id"):
        return None
    citation: dict[str, Any] = {
        "marker": f"[{n}]",
        "signal_id": entry["signal_id"],
    }
    if entry.get("title"):
        citation["title"] = entry["title"]
    if entry.get("source"):
        citation["source"] = entry["source"]
    # V-H1: the OUTLET, so an attribution claim ("near-identical framing across
    # CBC, NPR and the BBC") is checkable at all. Emitted only when the row
    # carried one — never fabricated.
    if entry.get("source_id"):
        citation["source_id"] = entry["source_id"]
    # #116(e): carry the compact evidence snippet (the analyst's WORKING text,
    # distilled-first) so the verify judge sees the cited signal's CONTENT, not
    # just its headline (see _marker_to_evidence).
    if entry.get("snippet"):
        citation["snippet"] = entry["snippet"]
    # FAITHFULNESS TRUST BOUNDARY: carry the RAW authoritative source text so the
    # verify judge grounds a claim against the real article, not our distilled
    # summary — catching a summarizer hallucination (see _marker_to_evidence).
    # ``source_truncated`` rides along ONLY when True (payload-minimal; absent =>
    # complete) so the judge softens "absent => unsupported" on an excerpt.
    if entry.get("source_text"):
        citation["source_text"] = entry["source_text"]
        if entry.get("source_truncated"):
            citation["source_truncated"] = True
    return with_claim_stamp(citation, entry)


def _extract_citations(
    body: str,
    index: Mapping[int, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Resolve the ``[N]`` markers in ``body`` against the render-time index.

    Returns ``(citations, marker_count, resolved_count)``:
      * ``citations`` — one entry per DISTINCT marker that maps to a real
        signal_id, in first-appearance order:
        ``{"marker": "[N]", "signal_id": <id>, "title"?, "source"?}``. A marker
        whose index has no resolvable signal_id is COUNTED but NOT emitted (no
        fabricated id) — same for an out-of-range marker.
      * ``marker_count`` — the number of DISTINCT ``[N]`` markers in the prose.
      * ``resolved_count`` — how many of those resolved to a citation entry.
    """
    citations: list[dict[str, Any]] = []
    seen: set[int] = set()
    marker_count = 0
    for match in _CITATION_MARKER_RE.finditer(body or ""):
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)
        marker_count += 1
        citation = _citation_from_index_entry(n, index.get(n))
        if citation is None:
            # Out-of-range or unresolved index — count it, never fabricate an id.
            continue
        citations.append(citation)
    return citations, marker_count, len(citations)
