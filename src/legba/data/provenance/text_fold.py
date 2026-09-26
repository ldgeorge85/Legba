# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE SHARED NORMALISATION SITE — one fold, called by every comparator that
compares model prose to producer prose (D-1 §3.1, ruled).

THE CENSUS THAT ORDERED THIS MODULE. The demotion design asked for "one shared
normalisation site"; the measurement at tip found the opposite, and worse than
the design assumed. Inside ``data/provenance/`` alone there are **at least
seven** hand-rolled normalisers using **three different dash tables**, none
importing another:

  * ``absence_slice._UNICODE_HYPHENS``      — 4 dashes (U+2010–2013 only)
  * ``composition_integrity._PUNCT_FOLD``   — 18 entries (6 dashes, 5 quote
                                              forms, 4 space forms)
  * ``judge_assessability._DASH_TRANS``     — 7 dashes
  * ``judge_quote_rules._normalize_quote_text`` — **no dash fold at all**
  * ``citation_markers._canonical_annotation``
  * ``world_knowledge_guards``              — the plane's only ``NFKD``
  * ``verify._canon_ref``

and ``verify.py:38`` imports ``unicodedata`` and never uses it — the fossil of a
plane-wide normaliser that was contemplated and never landed.

THE SPECIMEN, because the class is not hypothetical. MECH-6:
``_HEDGED_WEAK_MARKER_RE`` matches ``weakly[-\\s]+supported`` while the tier
writes U+2011 NON-BREAKING HYPHEN; **58.2% of claims graded under
``2026-08-28/1`` carry U+2011**, and one world lineage measured 298 of 299 rows
carrying it. A comparator that folds on one side and not the other does not
merely miss — it decides the wrong way, silently, at fleet scale.

WHAT THIS FOLD IS, exactly (the ruled union, D-1 §3.1):

    NFKC  +  the punctuation table  +  U+00AD removal  +  casefold
             + whitespace collapse

* **NFKC first.** It is the only step that reaches the full-width forms
  (``ＵＳ`` → ``US``), the narrow/no-break spaces the wire writes inside
  numbers (U+202F, U+00A0 → ordinary space) and the ligature/compat forms. It
  does **not** touch the dashes, which is why the table below still exists.
* **The punctuation table** covers what NFKC leaves: U+2010–U+2015 (the four
  the old ``_UNICODE_HYPHENS`` had, PLUS the em dash U+2014 and the horizontal
  bar U+2015 it did not) and U+2212 MINUS, the five quote forms, and the space
  forms NFKC misses.
* **U+00AD SOFT HYPHEN is DELETED, not folded to ``-``.** It is an invisible
  line-break hint; rendering it as a hyphen would split a word that no reader
  ever saw split.
* **``.casefold()``, not ``.lower()``.** ``.lower()`` leaves ``ß`` alone and
  mishandles the Turkish dotted/dotless I pair; a comparator over wire prose
  about Türkiye is exactly where that costs something.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not re-point the seven
normalisers above. Re-pointing an OLD comparator is a GRADED-BEHAVIOUR change
and must ride a stamp with a replay, never a refactor (D-1 §7 F-13). Exactly
one old comparator is re-pointed in the D-3 train — ``_absence_content_terms``,
whose fold impact was measured at **4,272 of 7,562 claims = 56.5%** — and it is
named in the lineage entry with that number attached. The rest stay where they
are until someone sizes them the same way.

This module imports nothing from the package, so every other module may import
it without closing a cycle.

A MERGE ACTION, STATED HERE SO IT CANNOT BE MISSED. D-2 and D-3 built in
parallel against the same ruling and each landed the D-0 site under its own
name: this module and ``provenance/normalize_match.py``. They are the SAME fold
and the same public name — ``normalize_for_match``, ``fold_equal``,
``PUNCT_FOLD``, ``FOLD_VERSION`` — which is what makes the merge a deletion
rather than a reconciliation. **``normalize_match`` is the one the spec names as
the D-0 site and it should win**; the merge deletes this file and re-points its
two importers (``assembly_arms``, ``absence_slice``) with an import swap, since
the call sites are already spelled identically. ONE known difference to check on
the way through: this module collapses ALL whitespace including newlines
(``\\s+``) where ``normalize_match`` collapses horizontal runs only
(``[ \\t\\r\\f\\v]+``). It is behaviour-neutral for the arms — a quotation check
compares the same bytes on both sides, so a newline is present or absent
symmetrically — but it is not neutral in general, and whichever survives should
say which it does and why.
"""
from __future__ import annotations

import re
import unicodedata

#: Bumped when the fold changes what it folds. A comparator's behaviour is a
#: graded behaviour, so a bump here is a stamp-bearing event, not a refactor.
FOLD_VERSION = "fold.v1"

#: U+00AD SOFT HYPHEN — an invisible line-break hint. DELETED (see the banner):
#: folding it to ``-`` would split a word no reader ever saw split.
_SOFT_HYPHEN = "­"

#: What NFKC leaves behind. Dashes first (NFKC decomposes none of them), then
#: the quote forms, then the space forms NFKC does not already reach.
PUNCT_FOLD: dict[str, str] = {
    # -- core-plane citation brackets (merge-union with D-2's fold, 2026-09-04:
    # its normalize_match folded these and its span matching was proven with
    # them; folding both sides of a match can only unify what IS the same
    # citation, so the shared table keeps the pair) ----------------------
    "【": "[",  # LEFT BLACK LENTICULAR BRACKET — core-plane 【N】 markers
    "】": "]",  # RIGHT BLACK LENTICULAR BRACKET
    # -- dashes: U+2010..U+2015 and U+2212 -------------------------------
    "‐": "-",  # HYPHEN
    "‑": "-",  # NON-BREAKING HYPHEN — the MECH-6 specimen
    "‒": "-",  # FIGURE DASH
    "–": "-",  # EN DASH
    "—": "-",  # EM DASH        (absent from _UNICODE_HYPHENS)
    "―": "-",  # HORIZONTAL BAR (absent from _UNICODE_HYPHENS)
    "−": "-",  # MINUS SIGN     (absent from _UNICODE_HYPHENS)
    # -- quotes ----------------------------------------------------------
    "“": '"', "”": '"', "„": '"',
    "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'",
    # -- spaces NFKC does not normalise to U+0020 -------------------------
    " ": " ",  # LINE SEPARATOR
    " ": " ",  # PARAGRAPH SEPARATOR
    "﻿": " ",  # ZERO WIDTH NO-BREAK SPACE / BOM
}

_PUNCT_FOLD_RE = re.compile("|".join(re.escape(k) for k in PUNCT_FOLD))
_WS_RUN_RE = re.compile(r"\s+")


def normalize_for_match(text: object) -> str:
    """The one fold. Total: never raises, never returns ``None``.

    Non-string / empty input returns ``""`` — a comparator that cannot see its
    input decides nothing, which is the honest degrade every arm in this plane
    already takes.

    IDEMPOTENT: ``normalize_for_match(normalize_for_match(x)) ==
    normalize_for_match(x)`` for every input, which is what makes it safe to
    call on text some caller already folded (the ``composition_integrity``
    arms hand ``_plain``-ed prose straight to ``_absence_content_terms``).
    """
    if not isinstance(text, str) or not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = out.replace(_SOFT_HYPHEN, "")
    out = _PUNCT_FOLD_RE.sub(lambda m: PUNCT_FOLD[m.group(0)], out)
    out = out.casefold()
    return _WS_RUN_RE.sub(" ", out).strip()


def fold_equal(a: object, b: object) -> bool:
    """Are two texts equal UNDER THE FOLD? The comparator both sides run
    through — never one side folded and the other not, which is the whole
    MECH-6 class."""
    return normalize_for_match(a) == normalize_for_match(b)


def fold_contains(haystack: object, needle: object) -> bool:
    """Is ``needle`` a substring of ``haystack`` UNDER THE FOLD?

    An empty needle is NOT contained: a comparator that answers "yes" for
    nothing at all would pass every empty span, and an empty quote is a
    construction defect, not a satisfied one.
    """
    n = normalize_for_match(needle)
    if not n:
        return False
    return n in normalize_for_match(haystack)


__all__ = [
    "FOLD_VERSION",
    "PUNCT_FOLD",
    "fold_contains",
    "fold_equal",
    "normalize_for_match",
]
