# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The likelihood words, stated once.

Seven bands plus "cannot say". The Consult's answer format, the shared
analytic standards, the desk read contract, the assessment standards and the
renderer of competing explanations all read this one table, so a word in a
prompt and a word in a rendered answer cannot differ. The console's badge
uses the same cuts.

Two renderings of the same table:

* :data:`LIKELIHOOD_WORDS` — each word with its range, for prompts that may
  also write a number;
* :data:`LIKELIHOOD_WORD_LIST` — the bare words, for prompts that must never
  print a percentage.

Pure strings, no imports: the slim registry image and any prompt builder can
import it without pulling in the analyst package.
"""

from __future__ import annotations

__all__ = [
    "CANNOT_SAY",
    "LIKELIHOOD_BANDS",
    "LIKELIHOOD_WORDS",
    "LIKELIHOOD_WORD_LIST",
]

#: ``(upper bound, exclusive; word)`` in rising order — the console's
#: ``LIKELIHOOD_CUTS``. The last bound is above 1 so p = 1.0 has a word.
LIKELIHOOD_BANDS: tuple[tuple[float, str], ...] = (
    (0.05, "almost no chance"), (0.2, "very unlikely"), (0.45, "unlikely"),
    (0.55, "roughly even chance"), (0.8, "likely"), (0.95, "very likely"),
    (1.01, "almost certain"),
)

#: The word for "the evidence supports no band".
CANNOT_SAY = "cannot say"


def _render_bands() -> str:
    parts: list[str] = []
    low = 0
    for cut, word in LIKELIHOOD_BANDS:
        high = round(cut * 100)
        if low == 0:
            parts.append(f"{word} (under {high}%)")
        elif cut > 1:
            parts.append(f"{word} (over {low}%)")
        else:
            parts.append(f"{word} ({low}-{high}%)")
        low = high
    return ", ".join(parts)


#: The likelihood words with their ranges.
LIKELIHOOD_WORDS: str = (
    f"{_render_bands()}; write \"{CANNOT_SAY}\" when the evidence supports no band"
)

#: The same words with no ranges, for prompts that must not print a number.
LIKELIHOOD_WORD_LIST: str = (
    ", ".join(word for _cut, word in LIKELIHOOD_BANDS) + f", or {CANNOT_SAY}"
)
