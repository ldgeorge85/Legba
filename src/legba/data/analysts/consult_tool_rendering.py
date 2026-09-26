# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rendering ONE tool result into a conversation message, under a budget.

Extracted from ``consult_on_demand`` (2026-09-16, the c8a0105c train) under the
module-size ratchet. The cluster is cohesive and was already the seam: these
four names answer a single question — *how much of a tool result goes into the
prompt, and what is dropped first* — and the only thing that changed about that
question is that the budget is now allocated per ROUND rather than per tool.
That allocation lives in ``stack/llm/tool_round_compaction``; the cut itself
lives here, unchanged.

``consult_on_demand`` re-exports every name, so ``inline_target``,
``journal_assessor`` and the existing tests import them from where they always
did. This module is the definition; that re-export is the compatibility
surface.

The order of the cuts is the whole design, and it is worth restating because
each step exists to avoid the one after it:

1. fits already → the full dump, unmarked;
2. shed the CITATION-ONLY raw bodies — carried for the GATHER [N] grounding
   path, never read by the model, and very often the entire overage;
3. drop WHOLE trailing rows and say so (``truncated``, ``<key>_total``), so
   every surviving row is intact and citable;
4. last resort, an honest envelope carrying a chopped prefix as a JSON string.

What it must never do is the thing it replaced: ``json.dumps(result)[:N]``,
which handed the model invalid mid-JSON that reads as corrupt data and that the
model cannot even detect as incomplete.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _trim_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Compact a tool's args for the lightweight trace (keeps the SSE step
    stream and the persisted ``tool_calls`` small). Caps string/list sizes;
    scalars pass through."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            out[str(k)] = v[:200]
        elif isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            out[str(k)] = v
        elif isinstance(v, list):
            out[str(k)] = [str(x)[:120] for x in v[:10]]
        else:
            out[str(k)] = str(v)[:200]
    return out


# The list-bearing result keys `_bounded_tool_json` may drop whole trailing
# entries from (in probe order). Covers the port readers' shapes: most return
# "rows", get_timeline returns "items", search_corpus returns "results".
_BOUNDED_JSON_LIST_KEYS = ("rows", "items", "results")


#: Keys that exist for the CITATION machinery, not for the conversation. The
#: corpus readers carry the raw article text under ``row["source"]`` / the
#: read_document ``document``'s original field names, because
#: ``inline_target._citation_entry`` grounds a numbered [N] on exactly those
#: (the faithfulness trust boundary). The model never needs them — it reads
#: ``snippet`` / ``body`` — so they are the FIRST thing shed when a tool message
#: will not fit, and a field MISSING from this list is a field that keeps its
#: full 3.2 KB in every message and pushes the result back into the raw_prefix
#: chop. Kept in step with
#: ``runtime.corpus_read_projection.CITATION_SOURCE_FIELDS`` by hand rather than
#: imported: ``data.analysts`` does not import ``runtime``, which imports it.
_CITATION_ONLY_KEYS = (
    "source", "archived_text", "raw_body", "text", "distilled_body",
    "summary", "best_body",
)


def _shed_citation_payload(tool_result: Any) -> Any:
    """A conversation-facing copy of ``tool_result`` with the citation-only raw
    bodies removed, or ``tool_result`` unchanged when it carries none.

    This is what lets ONE tool result serve two consumers honestly: the GATHER
    citation builder keeps the raw source it must ground on, and the consult
    conversation keeps the readable projection instead of losing whole rows to
    the size bound (the 2026-09-16 review's ``search_corpus`` 5-of-8 drop).
    """
    if not isinstance(tool_result, dict):
        return tool_result
    shed = False
    out = dict(tool_result)
    rows = out.get("rows")
    if isinstance(rows, list) and any(
        isinstance(r, dict) and "source" in r for r in rows
    ):
        out["rows"] = [
            {k: v for k, v in r.items() if k != "source"}
            if isinstance(r, dict) else r
            for r in rows
        ]
        shed = True
    doc = out.get("document")
    if isinstance(doc, dict) and any(k in doc for k in _CITATION_ONLY_KEYS):
        out["document"] = {
            k: v for k, v in doc.items() if k not in _CITATION_ONLY_KEYS
        }
        shed = True
    if not shed:
        return tool_result
    out["citation_payload_omitted"] = True
    return out


def _bounded_tool_json(tool_result: Any, limit: int) -> str:
    """Serialize a tool result for a conversation message under a size budget,
    JSON-SAFELY (R2 / W2-T3 — truncation honesty).

    The old ``json.dumps(result)[:N]`` chop silently handed the model INVALID
    mid-JSON — a row cut in half reads as corrupt data and the model cannot
    even tell anything is missing. Instead: when the full dump exceeds
    ``limit``, drop WHOLE trailing rows from the result's list-bearing key
    (``rows`` / ``items`` / ``results``) and mark the payload with an explicit
    ``"truncated": true`` + ``"<key>_total": <n>`` so the model KNOWS the view
    is partial. When no whole-row cut can fit (one giant row, or a non-mapping
    result), fall back to a valid-JSON envelope
    ``{"truncated": true, "raw_prefix": "<chopped dump>"}``.

    Always returns valid JSON of length <= ``limit`` (for any sane limit —
    a degenerate limit smaller than the envelope itself still returns the
    minimal envelope). Non-serializable input degrades to an honest error
    envelope rather than raising.
    """
    try:
        full = json.dumps(tool_result)
    except (TypeError, ValueError):
        # Review fix (B0 batch): json.dumps ESCAPING inflates the repr prefix
        # (up to ~6x on non-ASCII/control chars), so a character-bounded slice
        # alone can overshoot ``limit``. Shrink-until-fits on the FINAL
        # serialized envelope — same monotone loop as the raw_prefix path.
        raw = repr(tool_result)
        cut = max(0, limit - 128)
        while True:
            envelope = json.dumps({
                "truncated": True,
                "error": "unserializable tool result",
                "raw_prefix": raw[:cut],
            })
            if len(envelope) <= limit or cut == 0:
                return envelope
            cut = cut // 2
    if len(full) <= limit:
        return full

    # FIRST cut: shed the citation-only raw bodies. They are carried for the
    # GATHER [N] grounding path and are dead weight in a conversation message,
    # so dropping them costs the model nothing and very often brings the whole
    # result back under budget WITHOUT losing a single row.
    shed = _shed_citation_payload(tool_result)
    if shed is not tool_result:
        try:
            full = json.dumps(shed)
        except (TypeError, ValueError):  # pragma: no cover — shed is a dict copy
            shed = tool_result
        else:
            if len(full) <= limit:
                return full
            tool_result = shed

    # Preferred cut: drop whole trailing rows so every surviving row stays
    # intact + citable, then flag the drop explicitly.
    if isinstance(tool_result, dict):
        list_key = next(
            (
                k for k in _BOUNDED_JSON_LIST_KEYS
                if isinstance(tool_result.get(k), list) and tool_result[k]
            ),
            None,
        )
        if list_key is not None:
            seq = tool_result[list_key]
            trimmed = dict(tool_result)
            trimmed["truncated"] = True
            trimmed[f"{list_key}_total"] = len(seq)
            trimmed[list_key] = []
            # Budget for the rows themselves = limit minus the empty envelope
            # (all other keys + the marker), with slack for separators.
            row_budget = limit - len(json.dumps(trimmed)) - 2
            kept: list[Any] = []
            used = 0
            for row in seq:
                try:
                    row_len = len(json.dumps(row)) + 2  # + ", " separator
                except (TypeError, ValueError):
                    break
                if used + row_len > row_budget:
                    break
                kept.append(row)
                used += row_len
            if kept:
                trimmed[list_key] = kept
                out = json.dumps(trimmed)
                if len(out) <= limit:
                    return out
            # No whole row fits (or the estimate missed) — fall through.

    # Fallback: chop the serialized dump but keep VALID JSON by carrying the
    # chopped text as a string value inside an honest envelope. json escaping
    # can inflate the re-encoded prefix, so shrink until it fits.
    prefix = full
    while True:
        out = json.dumps({"truncated": True, "raw_prefix": prefix})
        if len(out) <= limit or not prefix:
            return out
        overshoot = len(out) - limit
        prefix = prefix[: max(0, len(prefix) - max(overshoot, 64))]


__all__ = [
    "_BOUNDED_JSON_LIST_KEYS",
    "_CITATION_ONLY_KEYS",
    "_bounded_tool_json",
    "_shed_citation_payload",
    "_trim_args",
]
