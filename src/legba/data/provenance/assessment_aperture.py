# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""o4 — THE APERTURE SECTION IS NOT A SLICE-CHECKABLE NEGATIVE.

THE DEFECT, and it is a ROUTING defect rather than a fabrication. The world
voice's 2026-09-25 12:35Z read graded 7 of 8 claims supported and lost the
eighth to a ``hard_fail``:

    "The absence of these reads leaves gaps in coverage of possible
     proliferation developments, additional economic-coercion or escalation
     dynamics, and broader G20-level political or security shifts that could
     materially alter the cross-country picture."

V-B took it as a scoped negative (the qualifier is *"additional"*), screened it
against the run's RETAINED INPUT SLICE, found escalation rows there, and stage 2
returned ``absence_slice_contradicted`` — HARD, and the demotion floor put the
read at 0.50. Two of the fourteen ``world_assessment`` reads in seven days.

**The slice found escalation rows because the record CARRIED an escalation
read.** That is not a refutation of the sentence; it is the premise of it. The
sentence is about the twenty-six roster units the record did NOT carry, and the
input slice is what the surface WAS SHOWN. A row in the slice evidences that
this surface SAW something — which can never be evidence against a claim about
what it did not carry. One authority is grading the wrong proposition.

SO THE WHOLE SECTION LEAVES THE ROUTE, not the one sentence. ``ASSESSMENT_BODY_
SHAPE`` item 4 mandates a ``## What this reading misses`` section and defines it
as *the aperture* — the units this record could have carried and did not. Every
negative in it has the same truthmaker (the record's own drop ledger and roster)
and none of them has the slice. This is W1(e)'s shape exactly: *"a read that
carries the absence SHAPE without being a slice-checkable negative — it keeps
today's route"*, and it takes W1(e)'s mechanism unchanged — a route-exclusion
class, its own receipt counter, and V-I5's binding rule, which says that a claim
the router took off the slice route cannot be hard-failed by the judge either.

WHAT THIS DOES NOT DO — and the division of labour is the point. It does not
decide that the sentence is GOOD. The sentence IS a guess: it generalised into
topic classes on a record that handed over twenty-six units by name, and
``assessment_unsupported.aperture_guess`` marks it as one, inline and in public,
with UTF-16-exact offsets. That mark is a SOFT, visible finding on the row. What
leaves is only the unearned HARD class, which the slice was never entitled to
award. The two halves are deliberately independent: the mark says *what* is
wrong with the sentence, and the routing says *who is not allowed to decide it*.

WHY THE HEADING IS RESTATED rather than imported. ``data.provenance`` must not
import ``data.analysts`` — the edge runs the other way, which is why
``assembly_arms`` restates ``assembly_spans``' scope tokens and
``assessment_weighting`` restates three whole lexicons, each held against its
owner by a test instead of by an import. :data:`APERTURE_SECTION_HEADING` is
pinned against ``assessment_unsupported._APERTURE_HEADING`` the same way, so a
rename on either side fails loudly rather than silently unrouting the section.

WHY IT LIVES IN ITS OWN MODULE. ``verify.py`` sits at its size-gate ceiling
(5900/5900) and the gate is honoured by splitting, never by raising a number, so
the fold's aperture arm costs ``verify`` no lines at all: the branch is a keyword
on a call it already makes, and everything it decides is here.
"""

from __future__ import annotations

import re
from typing import Any

from .text_fold import normalize_for_match

__all__ = [
    "APERTURE_SECTION_HEADING",
    "ROUTE_EXCLUSION_APERTURE",
    "aperture_section",
    "claim_in_aperture_section",
]

#: ``ASSESSMENT_BODY_SHAPE`` item 4's heading, folded. RESTATED from
#: ``assessment_unsupported._APERTURE_HEADING`` (see the banner's last
#: paragraph) and pinned against the owner by a test.
APERTURE_SECTION_HEADING: str = "what this reading misses"

#: The route-exclusion CLASS this module contributes to V-B's router. It rides
#: the existing receipts — ``absence_slice_route_excluded`` and the per-class
#: ``absence_slice_route_excluded_aperture`` — so a panel reads this split
#: beside volume / trajectory / continuity without a new counter shape.
ROUTE_EXCLUSION_APERTURE: str = "aperture"

#: A markdown heading, wherever it sits. NOT anchored to a line start, because
#: the assessors emit the inline form (``…picture.## What this reading misses``)
#: often enough that ``verify._segment_claims_raw`` carries its own repair for
#: it (W4) — a section boundary this pass could not see would silently route
#: the whole aperture back onto the slice.
_HEADING_RE = re.compile(r"#{1,6}[^\S\n]*([^\n]*)")

#: Citation markers in both spellings. Stripped from BOTH sides of the
#: containment test: ``_segment_claims_raw`` pulls a trailing marker back inside
#: the sentence it supports (P7-F1), so a claim's marker placement need not match
#: the body's byte for byte even though the words do.
_MARKER_RE = re.compile(r"\[\[ref:\d+\]\]|\[\d+\]")

#: Below this many characters a claim span is a fragment, not a sentence, and is
#: not allowed to decide which section it came from.
_MIN_CLAIM_CHARS: int = 24


def _core(text: Any) -> str:
    """``text`` as the form both sides of the containment test are compared in.

    Markers out, emphasis out, whitespace collapsed, then the shared fold. Never
    used to take an OFFSET — the fold is not length-preserving — only to answer
    a yes/no about which section a claim came from.
    """
    raw = _MARKER_RE.sub(" ", str(text or ""))
    raw = re.sub(r"[*_`>#]+", " ", raw)
    return re.sub(r"\s+", " ", normalize_for_match(raw)).strip()


def aperture_section(body: str) -> str:
    """The prose under ``## What this reading misses``, or ``""``.

    The section runs from the end of its heading to the next heading of any
    level, or to the end of the body. A body with no such heading — every unit
    finding, every composition, every desk head, i.e. essentially the whole
    fleet — returns ``""`` and every caller is inert on it.
    """
    text = str(body or "")
    if not text:
        return ""
    hits = list(_HEADING_RE.finditer(text))
    for i, hit in enumerate(hits):
        if APERTURE_SECTION_HEADING not in _core(hit.group(1)):
            continue
        start = hit.end()
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        return text[start:end]
    return ""


def claim_in_aperture_section(claim: str, body: str) -> bool:
    """Is ``claim`` a sentence of this body's aperture section?

    Containment over :func:`_core` rather than an offset, because the claim a
    caller holds has already been through ``_segment_claims``' normalizations
    (marker drift, the trailing-marker pull, the inline-heading break) and is no
    longer a byte run of the body. The words survive all three; the punctuation
    and the marker placement do not.

    A short fragment cannot decide a section — ``_segment_claims`` emits a bare
    ``vs.``-split fragment often enough that a two-word span would match
    somewhere in almost any prose — so anything under
    :data:`_MIN_CLAIM_CHARS` answers ``False`` and keeps today's route. That is
    the conservative direction: this predicate only ever takes a claim OFF the
    slice route, so a miss costs the status quo and a false hit costs a verdict.
    """
    core = _core(claim)
    if len(core) < _MIN_CLAIM_CHARS:
        return False
    section = _core(aperture_section(body))
    return bool(section) and core in section

