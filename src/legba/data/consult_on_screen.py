# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``on_screen`` — what the console shows as a turn is asked — a leaf.

The console (``legba-ui-v3/src/lib/onScreen.ts``) sends, with every turn asked
from the frame, ids and places only — never a view's content::

    {"v": 1, "page": "board", "side": "reader",
     "point": {"kind": "target", "id": "…", "label": "…"} | null,
     "window": {"span": "24h" | null, "from": "…Z", "to": "…Z"},
     "views": [{"id": "v4.map", "at": "p1", "front": true}, …],   # at most 12
     "chat": {"placement": "bottom", "collapsed": false}}

This module declares it (:class:`OnScreen`, validated, at most
:data:`MAX_BYTES` serialised — the console holds it under the same 1,500), turns
it into the consult's compact labelled context block
(:func:`render_on_screen_block`, about 60–110 tokens for a full frame), and
derives the two things the turn contract reads from it: the console's pinned
WINDOW (:func:`window_of`) and the POINT (:func:`point_of`).

CONTEXT, NEVER EVIDENCE. The block says so to the model, and in ``record_only``
the grounding pass cuts a sentence whose only support is what is on screen
(``consult_grounding``).

A leaf: pydantic and stdlib only (the registry's slim image imports it).
"""

from __future__ import annotations

import json
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "MAX_BYTES",
    "MAX_VIEWS",
    "OnScreen",
    "point_of",
    "render_on_screen_block",
    "window_of",
]

MAX_VIEWS = 12
MAX_BYTES = 1500


class _Point(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = Field(min_length=1, max_length=32)
    id: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=80)


class _Window(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    span: str | None = Field(default=None, max_length=8)
    from_: str = Field(default="", alias="from", max_length=40)
    to: str = Field(default="", max_length=40)


class _View(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=120)
    at: str = Field(default="", max_length=16)
    front: bool = False


class _Chat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    placement: str = Field(default="", max_length=16)
    collapsed: bool = False


class OnScreen(BaseModel):
    """The request field. Unknown keys are ignored (a newer console keeps working)."""

    model_config = ConfigDict(extra="ignore")
    v: Literal[1] = 1
    page: str = Field(default="", max_length=64)
    side: Literal["reader", "operator"] = "reader"
    point: _Point | None = None
    window: _Window | None = None
    views: list[_View] = Field(default_factory=list, max_length=MAX_VIEWS)
    chat: _Chat | None = None

    @model_validator(mode="after")
    def _bounded(self) -> "OnScreen":
        size = len(json.dumps(self.wire(), separators=(",", ":")))
        if size > MAX_BYTES:
            raise ValueError(f"on_screen is {size} bytes; the cap is {MAX_BYTES}")
        return self

    def wire(self) -> dict[str, Any]:
        """The canonical dict threaded to the actor (``from`` spelled as on the wire)."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def _get(raw: Any, key: str) -> Any:
    return raw.get(key) if isinstance(raw, Mapping) else None


def window_of(on_screen: Any) -> dict[str, Any] | None:
    """The console's pinned window as the turn contract's ``{start, end, label}``."""
    w = _get(on_screen, "window")
    start, end = _get(w, "from"), _get(w, "to")
    if not start or not end:
        return None
    span = _get(w, "span")
    return {"start": start, "end": end, "label": f"console window {span}" if span else "console window"}


def point_of(on_screen: Any) -> dict[str, str] | None:
    p = _get(on_screen, "point")
    if not _get(p, "kind") or not _get(p, "id"):
        return None
    return {"kind": str(p["kind"]), "id": str(p["id"]), "label": str(_get(p, "label") or "")}


def render_on_screen_block(on_screen: Any) -> str:
    """The compact, labelled context block (``""`` when there is nothing).

    The point's label is written as a JSON string, so a quote mark or a
    newline in it cannot break the block. The open views and the chat
    placement are not printed: no instruction uses them.
    """
    if not isinstance(on_screen, Mapping):
        return ""
    lines = ["ON SCREEN (what the person asking had on screen; context for 'this' and 'here', "
             "never evidence):"]
    head = f"page: {on_screen.get('page') or 'unknown'}"
    point = point_of(on_screen)
    head += (
        f" · point: {point['kind']} {point['id']}"
        + (f" {json.dumps(point['label'], ensure_ascii=False)}" if point["label"] else "")
        if point else " · point: none"
    )
    win = on_screen.get("window") if isinstance(on_screen.get("window"), Mapping) else None
    if win:
        head += f" · window {win.get('span') or 'custom'} ({win.get('from')} → {win.get('to')})"
    lines.append(head)
    return "\n".join(lines)
