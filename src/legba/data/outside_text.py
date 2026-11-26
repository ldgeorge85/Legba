# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Make frame tokens inside outside text inert before it enters a prompt.

Prompts in this tree are framed by a few tokens that code or a model keys on:

* ``<<<FINAL>>>`` — the Consult reply parser takes the last standalone marker
  line as the start of the answer;
* a triple-backtick fence, and the fence-tagged ``contract`` block the Consult
  parser lifts out of an answer;
* ``ROW n ·`` — the row header of the statement seats' evidence list;
* ``CLAIMS:`` (and ``ABSENCE CLAIMS:`` and the like) — where a judge's claim
  list starts;
* ``SOURCE (authoritative`` — the label of the text a judge grades against.

A tool result, a fetched page or a signal's text can contain any of them. A
page that carries ``<<<FINAL>>>`` on its own line, quoted back by the model,
would end the answer at the quotation; a source that carries ``CLAIMS:`` or
``SOURCE (authoritative):`` reads to a judge as a new section of its own
prompt. :func:`inert_frames` breaks each token with a zero-width joiner
(U+200D) placed inside it: a reader still sees the same characters, and no
parser or frame matches it. It is idempotent.

Pure text, standard library only, so every renderer can import it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = ["INERT_MARK", "inert_frames", "inert_frames_deep"]

#: The character placed inside a frame token to break it.
INERT_MARK = "‍"

_FRAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # The Consult answer marker, in every spelling its parser accepts
    # (two or three angle brackets, spaces, any case): break it between the
    # brackets and the word.
    re.compile(r"(<{2,3})(?=\s*FINAL)", re.IGNORECASE),
    # Any triple-backtick fence, which covers the ``contract`` block.
    re.compile(r"(`)(?=``)"),
    # The statement seats' row header.
    re.compile(r"(\bROW)(?=\s+\d+\s*·)"),
    # A judge's claim-list header.
    re.compile(r"(\bCLAIMS)(?=\s*:)"),
    # The judge's label for the text it grades against.
    re.compile(r"(\bSOURCE)(?=\s*\(authoritative)", re.IGNORECASE),
)


def inert_frames(text: str) -> str:
    """``text`` with every frame token broken by :data:`INERT_MARK`."""
    if not isinstance(text, str) or not text:
        return text
    for pattern in _FRAME_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + INERT_MARK, text)
    return text


def inert_frames_deep(value: Any) -> Any:
    """:func:`inert_frames` applied to every string inside ``value``.

    Mappings and lists are rebuilt; keys are left alone, because they are the
    tool's own field names, not outside text. Anything else is returned as is.
    """
    if isinstance(value, str):
        return inert_frames(value)
    if isinstance(value, Mapping):
        return {k: inert_frames_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [inert_frames_deep(v) for v in value]
    return value
