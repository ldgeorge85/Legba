# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared contract for the consult front door's ``pinned_context`` field.

The workstation's Consult panel lets the operator PIN substrate records to a
conversation ("keep this report in scope for everything I ask next").  Those
pins reached the model two ways, and only one of them worked:

  1. a ``[Pinned <kind>: "<label>" (id=…)]`` text prefix glued onto the
     question client-side — lossy, but it did arrive; and
  2. a structured ``pinned_context`` field on the POST body — which
     ``ConsultRequest`` never declared, so pydantic v2's default
     ``extra='ignore'`` DROPPED it silently on every request.

This module is the single definition of what (2) is: the caps, the accepted
kinds, and the renderer that turns the rows into the prompt's ``PINNED
CONTEXT`` block.  It is a leaf — stdlib only — so BOTH sides can import it
without dragging the other's dependencies across the image boundary:

  * ``data/registry/consult_api.py`` + ``data/registry/deep_consult_api.py``
    (the registry image) validate the inbound rows against the caps here and
    forward them, unchanged, on the actor's first input row;
  * ``data/analysts/consult_on_demand.py`` (the runtime image) normalizes
    whatever actually arrives and renders the block into the user prompt.

Both halves matter: the registry is the gate (a bad shape is a 422 the client
can see), and the analyst is defensive (the actor input is JSON off a queue,
so it re-clamps rather than trusting the gate).

The text prefix in (1) is deliberately LEFT ALONE.  An older SPA build still
sends it and no server change can update a browser tab that is already open,
so the prefix keeps working and the structured block is additive.  A pin that
arrives both ways is simply named twice — redundant, not wrong.

Shape of one entry (all fields but ``kind``/``id`` optional)::

    {"kind": "finding", "id": "<uuid>", "title": "Border buildup",
     "text": "<record body the caller already had in hand>"}

``label`` is accepted as a synonym for ``title`` — that is the key the SPA's
``Selection`` rows carry, and renaming it server-side would have broken the
one client that was already sending pins.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "MAX_PINNED_RECORDS",
    "MAX_PINNED_TOTAL_CHARS",
    "MAX_PIN_ID_CHARS",
    "MAX_PIN_TEXT_CHARS",
    "MAX_PIN_TITLE_CHARS",
    "PINNED_CONTEXT_HEADER",
    "PINNED_KINDS",
    "normalize_pinned_context",
    "pinned_context_chars",
    "render_pinned_context_block",
]


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
# Sized against the consult input budget (32000 tokens, see the LLM-planes
# note) so a maxed-out pin set spends at most a fifth of the window and still
# leaves the system prompt + tool results room to breathe. The registry
# REJECTS (422) past these; the analyst CLAMPS.

#: Max pinned records on one consult request.
MAX_PINNED_RECORDS = 20
#: Max chars of one pin's human label.
MAX_PIN_TITLE_CHARS = 512
#: Max chars of one pin's hydrated record body.
MAX_PIN_TEXT_CHARS = 8_000
#: Max chars of one pin's opaque id (a UUID today; not required to be one —
#: ``target`` / ``analyst`` pins carry slug ids).
MAX_PIN_ID_CHARS = 200
#: Max chars across the whole block (titles + bodies summed).
MAX_PINNED_TOTAL_CHARS = 24_000

#: Substrate kinds a pin may name. Covers the SPA's ``SelectionKind`` union
#: (target/entity/source/analyst/finding/situation/signal) plus the lineage
#: row kinds and ``report`` — the operator-facing word for the finding a
#: Morning Read / assessment surface renders. Anything else is a 422 at the
#: front door: a kind nothing can resolve is a client bug, and failing it
#: loudly beats pinning a record the model can never look up.
PINNED_KINDS: frozenset[str] = frozenset(
    {
        "report",
        "finding",
        "meta_finding",
        "entity",
        "target",
        "signal",
        "situation",
        "source",
        "analyst",
        "alert",
        "critique",
        "hypothesis",
        "prediction",
    }
)

#: The literal header the prompt block opens with. Tests assert on it, and the
#: model is told (in the block itself) what the rows are for.
PINNED_CONTEXT_HEADER = "PINNED CONTEXT"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_title(entry: Mapping[str, Any]) -> str:
    """The display label for one pin — ``title``, else the SPA's ``label``."""
    for key in ("title", "label"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def pinned_context_chars(pins: Iterable[Mapping[str, Any]]) -> int:
    """Total title + body chars across ``pins``.

    The quantity :data:`MAX_PINNED_TOTAL_CHARS` bounds. Ids and kinds are
    excluded — they are already bounded per-entry and their contribution is
    noise next to a hydrated body.
    """
    total = 0
    for entry in pins:
        if not isinstance(entry, Mapping):
            continue
        total += len(_entry_title(entry))
        total += len(str(entry.get("text") or ""))
    return total


def normalize_pinned_context(raw: Any) -> list[dict[str, str]]:
    """Coerce an untrusted ``pinned_context`` payload into renderable rows.

    Defensive by construction — this runs on the ANALYST side, where the input
    is a JSON blob off the actor queue rather than a validated request body.
    Rows that cannot be read are dropped, everything else is clamped to the
    module caps, and the result is always a list (never ``None``).

    Returns entries with exactly ``kind`` / ``id`` / ``title`` / ``text``,
    ``title`` and ``text`` possibly empty strings.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    budget = MAX_PINNED_TOTAL_CHARS
    for entry in raw[:MAX_PINNED_RECORDS]:
        if not isinstance(entry, Mapping):
            continue
        pin_id = str(entry.get("id") or "").strip()[:MAX_PIN_ID_CHARS]
        if not pin_id:
            continue
        kind = str(entry.get("kind") or "").strip()[:64] or "record"
        title = _entry_title(entry).strip()[:MAX_PIN_TITLE_CHARS]
        text = str(entry.get("text") or "").strip()[:MAX_PIN_TEXT_CHARS]
        # Spend the shared budget in arrival order: the operator pinned the
        # first row first, so a late oversized body loses its text rather
        # than pushing an earlier pin out of the prompt entirely.
        spend = len(title) + len(text)
        if spend > budget:
            title = title[:budget]
            budget -= len(title)
            text = text[:budget]
        budget -= len(title) + len(text)
        out.append({"kind": kind, "id": pin_id, "title": title, "text": text})
        if budget <= 0:
            break
    return out


def render_pinned_context_block(pins: Any) -> str:
    """Render the ``PINNED CONTEXT`` prompt block for ``pins``.

    Takes ``Any`` on purpose: the analyst side hands this whatever came off
    the actor queue. Returns ``""`` for an empty / unusable set, which is what
    keeps the no-pins prompt BYTE-IDENTICAL to the pre-``pinned_context`` one
    (the golden the consult tests hold).
    """
    rows = normalize_pinned_context(pins)
    if not rows:
        return ""
    lines = [
        f"{PINNED_CONTEXT_HEADER} — {len(rows)} record(s) the operator pinned "
        f"to this conversation. Treat them as in scope for the question below "
        f"and cite them by id when you use them.",
    ]
    for i, row in enumerate(rows, start=1):
        label = f' "{row["title"]}"' if row["title"] else ""
        lines.append(f"[{i}] {row['kind']}{label} (id={row['id']})")
        if row["text"]:
            # Indent the body so the model can see where one pin ends.
            body = "\n".join(f"    {ln}" for ln in row["text"].splitlines())
            lines.append(body)
    return "\n".join(lines)
