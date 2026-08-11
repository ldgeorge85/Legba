# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citation markers — the judge subsystem's FIFTH brick, and the one that keeps
telling the same story: the model said what it cited, and the parser could not
hear it.

Everything here is marker PARSING and marker DRIFT. The canonical forms are
``[N]`` (unit), ``[[ref:N]]`` (composition), ``[lo-hi]`` (a survey citing the
whole enumerated corpus) and ``[no citation]`` (a clause the assessor flags as
deliberately un-citable). Everything else in this module exists because the core
plane emits one of those in some OTHER punctuation, and each variant cost a
panel round to find:

* ``【3】`` / ``［3］`` — full-width brackets (2026-06-30, live).
* ``【ref:2】``, ``【1-120】``, ``【none】`` / ``【assessed】`` — the R-tail set.
* ``(57, 87)`` — a parenthesized comma-list of unit markers (C1).
* ``(ref:2)``, ``[[ref:2,6]]``, ``【2†L1-L2】`` — V-I3, the round-4 residue.

A cited claim graded as uncited is a free precision loss, which is why the list
grows rather than being closed off: normalizing a variant can only ever ADD a
resolvable citation, and the allowlists (annotations, the required dagger)
guarantee CJK prose using the same glyphs survives untouched.

``verify`` imports these ONE WAY and re-exports every name, so
``verify._CLAIM_MARKER_RE``, ``verify._normalize_verify_markers`` and friends
resolve exactly as before. Extracted 2026-08-05 when the V-I train pushed
verify.py past its DO-NOT-RAISE ceiling.

DELIBERATE DUPLICATION: ``data.analysts.inline_target`` carries the same rules at
WRITE time and persists the rewrite; this module grades what was stored. They are
mirrored rather than shared because verify.py stays stdlib-only and
slim-image-safe — importing the analysts package from here would close an import
cycle through ``runtime.analyst_method``. A third copy lives in
``data.registry.export_api`` for the export view and is recorded as owed.
"""
from __future__ import annotations

import re


# Marker for a bare ``[3]`` citation (mirrors inline_target._CITATION_MARKER_RE).
_CLAIM_MARKER_RE = re.compile(r"\[(\d+)\]")

# M14 (2026-07-06) — a RANGE citation marker ``[1-92]`` / ``[1–92]`` cites the
# WHOLE enumerated corpus (the shape a survey / NULL-RESULT finding uses: "51 of
# the 92 signals concern floods/sports/trade [1-92]"). The bare-marker regex
# ``\[(\d+)\]`` NEVER matches a range (its first digit is followed by '-', not
# ']'), so a range-cited clause was read as ``no_citation`` and an honest survey
# floored to ~0. This expands a range to its integer members (hyphen / en- / em-
# dash) so the floor resolves the clause against the citation bridge like any
# multi-marker clause. Capped so a pathological ``[1-999999]`` can't fan out.
_CLAIM_RANGE_RE = re.compile(r"\[(\d+)\s*[-–—]\s*(\d+)\]")
_MAX_RANGE_WIDTH = 500

# M14 — an explicit ``[no citation]`` annotation is the assessor flagging a clause
# as DELIBERATELY un-citable (a synthesis / framing / corpus-survey line), NOT a
# fabricated fact. The floor treats it as floor-EXEMPT (see _is_fact_asserting);
# the JUDGE still grades it (a fabricated absence must not hide behind the marker).
_NO_CITATION_MARKER = "[no citation]"


def _range_markers(claim: str) -> set[int]:
    """Integer marker indices contributed by RANGE citations ``[lo-hi]`` (M14)."""
    out: set[int] = set()
    for m in _CLAIM_RANGE_RE.finditer(claim):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi and (hi - lo) <= _MAX_RANGE_WIDTH:
            out.update(range(lo, hi + 1))
    return out

# Marker for a composition ``[[ref:N]]`` citation — a 1-BASED ORDINAL (small int)
# naming the Nth cited sub-claim in the rendered bundle. This is a LOCAL copy of
# meta_findings_synthesizer._REF_MARKER_RE (NOT imported — verify.py stays
# stdlib-only + slim-image-safe: it must not import the analysts package). The
# two marker regexes are PROVABLY DISJOINT — ``\[(\d+)\]`` requires a digit right
# after ``[`` and never matches ``[[ref:5]]`` (there the digit is preceded by
# ``:``); ``\[\[ref:`` never matches ``[N]`` — so the unit ([N]) path stays inert
# whenever the composition path is selected and vice-versa.
_REF_MARKER_RE = re.compile(r"\[\[ref:(\d+)\]\]")

# C1 (2026-07-03) — citation-marker drift normalization, applied to the body at
# the TOP of _segment_claims so BOTH the section-segmentation and the [N] matching
# that consumes the spans see ASCII markers. (a) full-width / CJK lenticular
# brackets the core plane (gpt-oss / Qwen) non-deterministically emits — mirrors
# inline_target._VARIANT_CITATION_RE; (b) a parenthesized comma-list of TWO OR
# MORE numbers, ``(57, 87)`` -> ``[57][87]``, while a single-number paren
# ``(2023)`` is LEFT ALONE (year false-positive). verify.py stays stdlib-only — no
# inline_target import.
_VARIANT_CITATION_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")
_PAREN_CITATION_LIST_RE = re.compile(r"\((\s*\d+(?:\s*,\s*\d+)+\s*)\)")

# R-tail (2026-08-04) — the same bracket drift on the NON-numeric markers, and
# the same drift on the canonical no-citation marker's own spelling. Mirrors
# ``inline_target`` (see the long rationale there); duplicated rather than
# imported because verify.py stays stdlib-only + slim-image-safe. Measured over
# the live corpus: 218 findings carry a non-digit lenticular bracket.
#
#   * ``【ref:2】`` -> ``[[ref:N]]`` — the COMPOSITION marker in variant
#     brackets, invisible to ``_REF_MARKER_RE`` today, so a correctly-cited
#     composed claim grades as uncited.
#   * ``【none】`` / ``【assessed】`` / ``【not_observed】`` / … and the ASCII
#     misspellings ``[no_citation]`` / ``[no-citation]`` -> the canonical
#     ``_NO_CITATION_MARKER``. All are the assessor flagging a clause as
#     deliberately un-citable; without the rewrite the floor counts them as
#     uncited fact assertions and demotes an honestly-annotated finding. The
#     JUDGE still grades the clause either way, so this widens no hiding place.
_VARIANT_REF_CITATION_RE = re.compile(
    r"[【［〔〖]\s*\[?\s*ref:\s*(\d+)\s*\]?\s*[】］〕〗]", re.IGNORECASE
)
#: ``【1-120】`` — a RANGE citation in variant brackets (93 live occurrences).
#: The bare-integer rule above skips it, so ``_CLAIM_RANGE_RE`` never sees it
#: and an honest survey clause citing the whole enumerated corpus floors as
#: UNCITED. Wide dash class in (ASCII / en dash / non-breaking hyphen all occur
#: live), ASCII hyphen out, so the range parser matches whichever arrived.
_VARIANT_RANGE_CITATION_RE = re.compile(
    r"[【［〔〖]\s*(\d+)\s*[-–—‑]\s*(\d+)\s*[】］〕〗]"
)
_VARIANT_ANNOTATION_RE = re.compile(r"[【［〔〖]([^】］〕〗]{1,40})[】］〕〗]")
_ASCII_NO_CITATION_VARIANT_RE = re.compile(
    r"\[\s*no[_\-\s]?citation\s*\]", re.IGNORECASE
)
# V-I3 (2026-08-05) — THE THREE REMAINING MARKER SYNTAXES. Recommendation #5 of
# the round-4 panel: "a cited claim graded as uncited is a free precision loss",
# and each of these has a named live specimen:
#
#   * ``(ref:2)`` — S2, this round. `country_composition`/cn writes "Unremarkable
#     reads: economic_coercion (ref:2), energy_security (ref:6),
#     leadership_transition (ref:7)". All three references check out against
#     their titles; ``markers=[]``, so a fully cited claim was graded
#     `judge_unsupported`. Bare parentheses instead of double brackets.
#   * ``[[ref:2,6]]`` — the 08-04 panel's §6.2 compound form, named there and
#     still unparsed. One marker naming two sub-claims resolves to neither.
#   * ``【2†L1-L2】`` — §6.8, the Brazil `military_posture` body, which carries
#     ``【2†L1-L2】【5†L1-L2】【8†L1-L3】`` where ``[2][5][8]`` belongs. The
#     bare-integer rule above skips it (the bracket holds a dagger and a line
#     range) and the sentence graded markerless.
#
# All three expand to the canonical ASCII form the parsers already understand,
# so nothing downstream changes. Comma lists fan out to one marker each, which
# is what the composition path means by them.
_PAREN_REF_CITATION_RE = re.compile(
    r"\(\s*\[{0,2}\s*ref:\s*(\d+(?:\s*,\s*\d+)*)\s*\]{0,2}\s*\)", re.IGNORECASE
)
_COMPOUND_REF_CITATION_RE = re.compile(
    r"\[\[\s*ref:\s*(\d+(?:\s*,\s*\d+)+)\s*\]\]", re.IGNORECASE
)
#: ``【2†L1-L2】`` — a full-width bracket carrying a citation ordinal, a DAGGER
#: and a line-range annotation. The dagger is required: it is what tells this
#: apart from CJK prose that merely uses the same glyphs.
_VARIANT_DAGGER_CITATION_RE = re.compile(r"[【［〔〖]\s*(\d+)\s*†[^】］〕〗]*[】］〕〗]")


def _expand_ref_list(digits: str) -> str:
    """``"2, 6, 7"`` -> ``"[[ref:2]][[ref:6]][[ref:7]]"`` (one marker each)."""
    return "".join(f"[[ref:{n.strip()}]]" for n in digits.split(","))
#: An explicit ALLOWLIST, not "any non-digit content" — the guarantee that CJK
#: prose using these glyphs survives untouched is worth keeping.
_UNCITABLE_ANNOTATIONS: frozenset[str] = frozenset(
    {
        "none",
        "no citation",
        "not observed",
        "assessed",
        "assessment",
        "assessed situation",
        "assessed situations",
        "assessed structure",
        "system assessed",
        "derived structure",
        "authoritative current context",
    }
)


def _canonical_annotation(token: str) -> str | None:
    """``_NO_CITATION_MARKER`` when ``token`` is a known un-citable annotation
    (separator- and case-insensitive: the corpus spells these with ``_``, ``-``
    and spaces interchangeably), else ``None`` — leave the text alone."""
    flat = re.sub(r"[_\-\s]+", " ", token).strip().casefold()
    return _NO_CITATION_MARKER if flat in _UNCITABLE_ANNOTATIONS else None


def _normalize_verify_markers(text: str) -> str:
    """Rewrite citation-marker drift variants to ASCII ``[N]`` before parsing."""
    if not text:
        return text
    text = _VARIANT_CITATION_RE.sub(lambda m: f"[{m.group(1).strip()}]", text)
    text = _PAREN_CITATION_LIST_RE.sub(
        lambda m: "".join(f"[{n.strip()}]" for n in m.group(1).split(",")),
        text,
    )
    text = _VARIANT_RANGE_CITATION_RE.sub(
        lambda m: f"[{m.group(1)}-{m.group(2)}]", text
    )
    # V-I3: the dagger form runs BEFORE the plain annotation rewrite, which would
    # otherwise see "2†L1-L2" as un-citable prose and leave it alone.
    text = _VARIANT_DAGGER_CITATION_RE.sub(lambda m: f"[{m.group(1)}]", text)
    text = _VARIANT_REF_CITATION_RE.sub(lambda m: f"[[ref:{m.group(1)}]]", text)
    text = _COMPOUND_REF_CITATION_RE.sub(lambda m: _expand_ref_list(m.group(1)), text)
    text = _PAREN_REF_CITATION_RE.sub(lambda m: _expand_ref_list(m.group(1)), text)
    text = _VARIANT_ANNOTATION_RE.sub(
        lambda m: _canonical_annotation(m.group(1)) or m.group(0), text
    )
    return _ASCII_NO_CITATION_VARIANT_RE.sub(_NO_CITATION_MARKER, text)
