# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/DATA_SOURCES.md states how many source kinds are registered; the
number is the registry's, counted (the page once said eighteen while
twenty-two were registered)."""
from __future__ import annotations

import pathlib
import re

_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_TENS = {"twenty": 20, "thirty": 30}


def _number(word: str) -> int:
    word = word.lower()
    if "-" in word:
        tens, unit = word.split("-", 1)
        return _TENS[tens] + _WORDS[unit]
    return _WORDS[word] if word in _WORDS else _TENS[word]


def test_the_stated_kind_count_is_the_registrys() -> None:
    from legba.runtime import source_factory

    doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "DATA_SOURCES.md").read_text()
    m = re.search(r"\*\*([A-Za-z-]+)\*\* first-party kinds are\s+registered today", doc)
    assert m, "the kind-count sentence moved; keep it counted"
    assert _number(m.group(1)) == len(source_factory.discover_source_kinds())
