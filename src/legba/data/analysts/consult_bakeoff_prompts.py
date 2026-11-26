# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bake-off's two consult SYSTEM PROMPT variants — text only.

The stock consult prompt cannot serve an honest arm: it orders "the platform's OWN finished
intelligence — list_findings / list_situations — comes FIRST", and its tool
catalogue names twenty-four tools where arm B may use six and arm C four. Each
arm needs "a system-prompt variant whose tool list and survey rule name only
those tools".

HOW A VARIANT IS BUILT. The stock prompt as the arms were measured with
(``consult_bakeoff_stock.FROZEN_STOCK_PROMPT``, a frozen copy: the live consult
prompt has moved on since) is CUT at four markers it carries and re-assembled,
so everything the arms share with that stock text is byte for byte:

  * the analytic-standards preamble and the TASK line (everything before
    ``Available tools:``);
  * the OUTPUT CONTRACT and loop protocol (``OUTPUT CONTRACT —`` up to the
    strategy block);
  * answer quality, the EVIDENCE FLOOR, CONFIDENCE and WHAT WOULD CHANGE MY
    MIND (``Answer quality:`` to the end).

What is the arm's OWN: the tool catalogue (each line is the stock line for
that tool, verbatim, plus — for arm C — one sentence stating the scope the
pack enforces) and the strategy block. The thin-evidence example names tools
the arm has. Nothing orders a finished-intelligence read first.

A marker the stock prompt stops carrying makes this module fail at import, so
a variant can never silently drift into a half-built prompt. The variants are
part of the pre-registered fixed prompts: their sha256 goes into
the run record (``scripts/bakeoff/run_arm.py``).
"""

from __future__ import annotations

import hashlib

from .consult_bakeoff_stock import CONFIDENCE_FROZEN_BAKEOFF
from .consult_bakeoff_stock import FROZEN_STOCK_PROMPT as _STOCK

__all__ = [
    "RAW_SYSTEM_PROMPT",
    "VERIFIED_SYSTEM_PROMPT",
    "prompt_sha256",
    "stock_tool_line",
]

_M_TOOLS = "Available tools:"
_M_CONTRACT = "OUTPUT CONTRACT —"
_M_STRATEGY = "Strategy — SURVEY THEN DRILL:"
_M_QUALITY = "Answer quality:"
_STOCK_THIN_EXAMPLE = "(list_situations, list_findings)"
_STOCK_BATCH_EXAMPLE = "compare_targets after list_targets resolves the ids"


def _cut(text: str, start: str, end: str | None) -> str:
    i = text.index(start)
    return text[i:] if end is None else text[i:text.index(end, i)]


def stock_tool_line(name: str) -> str:
    """The stock catalogue's own line for ``name`` (``  - name(...) — ...``)."""
    tag = f"  - {name}("
    for line in _STOCK.splitlines():
        if line.startswith(tag):
            return line
    raise KeyError(f"the stock consult prompt has no catalogue line for {name!r}")


#: Lines written HERE rather than cut from the stock catalogue, because the
#: stock line names tools the arm does not have (the stock ``list_targets`` line
#: points at query_hypotheses / compare_targets / list_findings; the stock
#: ``read_document`` line at search hits). Arm-specific, so keyed by arm.
_LIST_TARGETS = (
    "  - list_targets() — the monitored targets + their ids (e.g. "
    "country_g20_ir); call this to resolve a place/topic to a valid target_id."
)
_READ_RECEIPT = (
    "  - read_document(doc_id) — fetch ONE signal's full stored body + metadata "
    "by its doc_id (a signal id a returned finding cites) when you need the "
    "WHOLE article text. Returns status ('found' / 'not_found') and the full "
    "indexed document (title, raw_body, facets)."
)

_HEAD = _STOCK[: _STOCK.index(_M_TOOLS)]
_CONTRACT = _cut(_STOCK, _M_CONTRACT, _M_STRATEGY)
# The arms keep the pre-registered CONFIDENCE paragraph, so their hashes do not move.
_TAIL = _cut(_STOCK, _M_QUALITY, None)
if CONFIDENCE_FROZEN_BAKEOFF not in _TAIL:
    raise RuntimeError("the frozen CONFIDENCE paragraph did not land in the bake-off tail")  # pragma: no cover
if _TAIL.count(_STOCK_THIN_EXAMPLE) != 1 or _CONTRACT.count(_STOCK_BATCH_EXAMPLE) != 1:
    raise RuntimeError(  # pragma: no cover — import-time guard
        "the stock consult prompt's worked examples moved; re-cut the variants"
    )


def _assemble(
    catalogue: list[str], strategy: str, *, thin_example: str, batch_example: str,
) -> str:
    """The stock prompt with the catalogue + strategy swapped and the two worked
    examples re-spelled in tools the arm has (each names stock tools otherwise)."""
    contract = _CONTRACT.replace(_STOCK_BATCH_EXAMPLE, batch_example)
    tail = _TAIL.replace(_STOCK_THIN_EXAMPLE, thin_example)
    return (
        _HEAD + _M_TOOLS + "\n" + "\n".join(catalogue) + "\n\n"
        + contract + strategy.rstrip() + "\n" + tail
    )


# ---------------------------------------------------------------------------
# Arm B — the RAW corpus (substrate_read_raw: six readers, nothing derived)
# ---------------------------------------------------------------------------

_RAW_TOOLS = (
    "search_signals", "search_corpus", "read_document", "vector_search",
    "list_targets", "list_sources",
)

_RAW_STRATEGY = """Strategy — SEARCH THEN READ:
  - These six readers are the WHOLE of what this run can read: the ingested source documents themselves. There is no finished analysis, no fact store, no event or situation register and no curated history on this run — what you know about the question is what the source documents you read say.
  - Resolve the place or topic to a target_id with list_targets when the scope names one you need; use list_sources to tell "no coverage on X" apart from "a quiet feed".
  - Survey WIDE first: batch search_corpus (lexical, the full raw text), search_signals (title + summary) and vector_search (semantic) with two or three different phrasings in ONE round. Then read_document the few documents the answer will rest on — a snippet is not the article.
  - Mind the window. The question names an as-of instant and often a window; search for material from that window, prefer each document's own stated date over its fetch time, and do not treat anything dated after the as-of as known.
  - Finish once the documents you read answer the question or you have exhausted the useful searches; say what the documents do not establish."""

RAW_SYSTEM_PROMPT = _assemble(
    [_LIST_TARGETS if t == "list_targets" else stock_tool_line(t) for t in _RAW_TOOLS],
    _RAW_STRATEGY,
    thin_example="(search_corpus, read_document)",
    batch_example="read_document after search_corpus returns the ids",
)


# ---------------------------------------------------------------------------
# Arm C — VERIFIED findings + their receipts (substrate_read_verified)
# ---------------------------------------------------------------------------

_VERIFIED_SCOPE = {
    "list_findings": (
        " On this run every row returned has PASSED the platform's "
        "faithfulness check at the composition admission floor; an unverified "
        "finding is never returned."
    ),
    "belief_as_of": (
        " On this run only findings admissible under the verdict fold you name "
        "are returned; a finding still ungraded on the date is not."
    ),
    "read_document": (
        " On this run it opens ONLY a signal that a verified finding cites (its "
        "receipts); any other id is refused — there is no search on this run."
    ),
    "list_targets": "",
}

_VERIFIED_STRATEGY = """Strategy — VERIFIED FINDINGS, THEN THEIR RECEIPTS:
  - These four readers are the WHOLE of what this run can read: the platform's findings that passed its own faithfulness check, and the source documents those findings cite. There is no free-text search, no fact store and no event or situation register on this run.
  - Resolve the place or topic to a target_id with list_targets when you need one. Then read the desk's verified findings with list_findings(target_id=..., since_hours=...) — its default window is 48 hours, so pass since_hours for anything older (a six-week question needs at least 1008) — and, for what was believed on an earlier date, belief_as_of(as_of=..., target_id=...).
  - A finding is the platform's reading of its sources, not a source. When a claim the answer rests on needs its evidence, open the signals that finding cites with read_document and judge the claim against the document.
  - Mind the window. The question names an as-of instant; do not treat anything produced after it as known.
  - Finish once the verified findings and their receipts answer the question, or say plainly that the verified record does not establish it."""

_VERIFIED_LINES = {"read_document": _READ_RECEIPT, "list_targets": _LIST_TARGETS}

VERIFIED_SYSTEM_PROMPT = _assemble(
    [
        (_VERIFIED_LINES.get(t) or stock_tool_line(t)) + scope
        for t, scope in _VERIFIED_SCOPE.items()
    ],
    _VERIFIED_STRATEGY,
    thin_example="(list_findings, read_document)",
    batch_example="read_document after list_findings returns the finding that cites it",
)


def prompt_sha256(text: str) -> str:
    """The hash a run record pins a prompt variant by."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
