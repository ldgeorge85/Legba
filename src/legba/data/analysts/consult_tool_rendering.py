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
from collections.abc import Callable, Mapping
from typing import Any

from ..display_policy import OPERATOR, READER, display_payload, principal_class


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
            # The TRUE row count: a count the tool itself stated (or an
            # earlier trim recorded) is never replaced by this copy's length.
            stated = tool_result.get(f"{list_key}_total")
            trimmed[f"{list_key}_total"] = (
                stated
                if isinstance(stated, int)
                and not isinstance(stated, bool)
                and stated >= len(seq)
                else len(seq)
            )
            trimmed[list_key] = []
            # Reserve the ``_shown`` key in the envelope the budget measures
            # (the final value is at most ``len(seq)``, so same digit width).
            trimmed[f"{list_key}_shown"] = len(seq)
            # W11-F150 paging dialect (list_findings): a tool that STATED its
            # ``rows_total`` pages by ``offset``. Its ``next_offset`` counted
            # the full page, so after a trim it would skip the rows cut here;
            # it is re-pointed at the first row not shown. ``offset`` rides
            # through the dict copy unchanged. Reserve the key's widest value.
            page_base: int | None = None
            if (
                list_key == "rows"
                and isinstance(stated, int)
                and not isinstance(stated, bool)
            ):
                raw_offset = tool_result.get("offset", 0)
                page_base = (
                    raw_offset
                    if isinstance(raw_offset, int)
                    and not isinstance(raw_offset, bool)
                    else 0
                )
                trimmed["next_offset"] = page_base + len(seq)
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
                # How many rows the model actually has in front of it, beside
                # the true total, so it can tell the two differ.
                trimmed[f"{list_key}_shown"] = len(kept)
                if page_base is not None:
                    resume = page_base + len(kept)
                    if (len(kept) < len(seq)
                            and resume < trimmed[f"{list_key}_total"]):
                        trimmed["next_offset"] = resume
                    elif "next_offset" in tool_result:
                        trimmed["next_offset"] = tool_result["next_offset"]
                    else:
                        trimmed.pop("next_offset", None)
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


#: The consult request key (``inputs[0]``) the front door stamps with the
#: asker's display class (``operator`` / ``reader``) — X2-B4.
DISPLAY_CLASS_KEY = "display_class"


def tool_context_renderer(
    request: Mapping[str, Any],
) -> Callable[[Any, int], str]:
    """The CONSULT CONTEXT BUILDER's renderer for this request (X2-B4).

    What a tool result becomes in the model's context is decided here, once
    per run, from the asker's display class on the request:

      * a READER's consult renders every tool result through the reader display
        policy FIRST (``display_policy.display_payload``): ``archived_text``,
        ``raw_body``, ``source_text`` and the other publisher-text fields never
        enter the context, and every remaining prose field of a publisher
        document (a corpus hit, its citation block, a read_document body) is
        bounded to one span. The model cannot repeat what it was never shown.
      * an OPERATOR's consult renders exactly as before (:func:`_bounded_tool_json`
        — the same function object, so the context is byte-identical).

    UNSTAMPED = OPERATOR, deliberately: the runtime is not the trust boundary
    — the consult front door is, and every caller it admits today is an
    operator-class credential. The front door stamps ``display_class`` from the
    asker's principal; a stamp that is present but not ``operator`` fails
    CLOSED to the reader renderer. Only the conversation copy is projected —
    the raw results still feed refs, gates and the GATHER citation path.
    """
    stamp = request.get(DISPLAY_CLASS_KEY) if isinstance(request, Mapping) else None
    if stamp is None or principal_class(stamp) == OPERATOR:
        return _bounded_tool_json

    def _reader_tool_json(tool_result: Any, limit: int) -> str:
        return _bounded_tool_json(display_payload(tool_result, READER), limit)

    return _reader_tool_json


__all__ = [
    "DISPLAY_CLASS_KEY",
    "_BOUNDED_JSON_LIST_KEYS",
    "_CITATION_ONLY_KEYS",
    "_bounded_tool_json",
    "_shed_citation_payload",
    "_trim_args",
    "tool_context_renderer",
]
