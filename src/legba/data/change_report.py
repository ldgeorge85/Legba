# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DOES A VERDICT REPORT A CHANGE? — the one text test, shared by two tiers.

Two callers ask the same question of the same kind of sentence (a desk read's
verdict):

  * the WORLD carry plan's restraint-shift floor
    (``analysts.assembly_world.reports_change``) — a political block that
    reports a change may take one of the reserved world slots;
  * the CARRY RULE (``legba.data.carried_state``, W11-F14) — a desk read whose
    verdict reports a change and cites fresh wire items is its own
    re-evidence, even when none of its indicators fired (W11-F, roll 65).

One definition, so the tiers cannot reach different conclusions about the same
sentence. Lifted here from ``assembly_world`` / ``assembly_carry`` (roll 65)
because ``carried_state`` is imported by the registry's slim image through the
absence route and may not import ``legba.data.analysts``. Stdlib plus the one
text fold (which the absence route already loads).

THE TEST (:func:`text_reports_change`), over the folded text, all required:

  1. the leading EPISTEMIC FRAME is set aside (:data:`EPISTEMIC_PREFIX_RE` —
     *"Assessed - …"*, *"Assessed: …"*, *"Assessed that …"*). Before roll 65 the
     lead clause stopped at the dash after *"Assessed"*, so the stasis test
     read the single word ``assessed`` and passed every *"Assessed - a change
     in Sudan's top leadership is unlikely …"* verdict as a change;
  2. the lead clause does not open on stasis (:data:`STASIS_RE`);
  3. the verdict, up to its provenance tail (:data:`TAIL_RE`), names a change
     event (:data:`CHANGE_RE`) that it does not deny (:func:`asserts_change` —
     *"…reports no unrest, security-force split, or elite defection"* names
     ``defection`` only to say it did not happen; roll 65).

MEASURED (roll 65, the 2,516 moderate-or-higher desk reads whose indicators
evidenced nothing in the 14 days to 2026-11-08): BLUFs passing fell from 29 to
4 on the top-job desk and from 180 to 30 on the elite-and-unrest desk, almost
all of the drop being *"Assessed - … unlikely"* frames and denied lists. On the
2026-11-08 12:21Z world read the restraint floor's eligible set goes from 10
blocks to 9: Mali's *"repression … remains the driver … and no mass protest,
security-force defection … appears"* stops counting as a change.
"""
from __future__ import annotations

import re
from typing import Any

from .provenance.text_fold import normalize_for_match

#: Where a lead span's FIRST ASSERTION ends, over the folded text
#: (:func:`normalize_for_match` turns every dash into ``-``, so a spaced ``-``
#: is an em/en dash and a hyphenated compound is untouched). The desk prompts
#: ask for a verdict, then a provenance tail (``UNIT_BLUF_ABSENCE_RULE``:
#: *"…while no mass unrest … appears in this desk's collection"*); the tail is
#: joined by one of these, so cutting here leaves the verdict and nothing else.
#: ``, and`` / ``, with`` are the coordinated tail (*"…holds at high intensity,
#: with no basing change in this desk's collection"*); an unpunctuated ``with``
#: is not, because it is as often the verdict's own complement.
#: (``assembly_carry.lead_clause`` reads this same object.)
LEAD_CLAUSE_END_RE = re.compile(
    r"\s-{1,2}\s|;|\.\s|,?\s(?:while|whereas|but|although|though|yet)\s"
    r"|,\s(?:and|with)\s"
)

#: Where a verdict's PROVENANCE TAIL starts (over the folded text): the clause
#: the desk prompts append after the verdict (*"…, while no mass unrest …
#: appears in this desk's collection"*). Dashes are NOT here — the clause after
#: a dash is the verdict's own evidence (*"…continues at a moderate pace —
#: President Trump replaced chief of staff Susie Wiles…"*).
TAIL_RE = re.compile(r";|\.\s|,?\s(?:while|whereas|but|although|though)\s")

#: A CHANGE in the political / institutional dimension: an EVENT of office,
#: law or mobilisation — personnel (replaced, resigned, dismissed, fired,
#: firings, appointed, sworn in), legal-institutional acts (arrest, detention,
#: indictment, prosecution, designation, ban, suspension, dissolution,
#: impeachment), and political realignment or rupture (mandate, consolidation,
#: coalition collapse, defection, purge, reshuffle, coup, riot, mutiny,
#: crackdown). Generic DIRECTION words (widening, intensifying) are
#: deliberately NOT here: on e30e67d3 Saudi Arabia's internal_stability verdict
#: was *"External war pressure on Saudi territory keeps widening — Houthi drone
#: and ballistic-missile strikes…"*, a kinetic report filed on a political
#: desk. ``firings`` (roll 65): the noun of ``fired`` — *"post-midterm firings
#: reach Wiles and Miller"* (US top-job read 3824a07b, 2026-11-08).
CHANGE_RE = re.compile(
    r"(?<![\w-])(?:replac\w*|resign\w*|dismiss\w*|fired|firings|ousted|ouster|sacked"
    r"|appoint\w*|install\w*|sworn|step(?:s|ped)?\s+down|impeach\w*|arrest\w*"
    r"|detain\w*|detention|indict\w*|prosecut\w*|convicted|designat\w*"
    r"|banned|suspen\w*|dissol\w*|collaps\w*|defect\w*|purg\w*|reshuffl\w*"
    r"|coup|mandate|consolidat\w*|erupt\w*|riot\w*|mutin\w*|uprising"
    r"|crackdown|churn)(?![\w-])"
)

#: STASIS in the lead clause — the top-job desk's "low likelihood of a change"
#: frame, and a driver stated as holding. A verdict that opens this way is
#: about what did NOT move, whatever its evidence clause goes on to mention.
#: ``no (material) change`` (roll 65): *"Assessed: no change in France's top
#: leadership is likely …"* is the same frame, negated rather than hedged.
STASIS_RE = re.compile(
    r"(?<![\w-])(?:unlikely|likelihood|unchanged|steady|stable|holds?|held"
    r"|remains?\s+(?:low|contained|in\s+place)|stays?\s+low"
    r"|(?:remains?|stays?)\s+(?:high|elevated)"
    r"|no\s+(?:material\s+)?change)(?![\w-])"
)

#: A change word NAMED ONLY TO BE DENIED is not a change report: *"…reports no
#: unrest, security-force split, or elite defection"*, *"…with no street or
#: defection signals"*, *"…nothing it cannot replace"*. A match counts only
#: when no negator precedes it inside its own NEGATION SCOPE — the stretch
#: since the last clause boundary (:data:`NEGATION_SCOPE_END_RE`; commas do
#: not end it, so a denied list stays denied to its last item). ``no longer``
#: is a change report, not a denial (the guard ``assembly_carry`` carries).
NEGATOR_RE = re.compile(
    r"(?<![\w-])(?:no(?!\s+longer(?![\w-]))|not|nothing|without|never|neither"
    r"|nor|none|absent)(?![\w-])"
)
NEGATION_SCOPE_END_RE = re.compile(
    r";|:|\s-{1,2}\s|\.\s"
    r"|\s(?:because|as|after|while|whereas|when|since|but|though|although)\s"
)

#: The leading epistemic frame a desk verdict opens with — *"Assessed - "*,
#: *"Assessed: "*, *"Assessed that "*, *"We assess that "* — set aside before
#: the lead clause is cut, so the clause the stasis test reads is the
#: assertion and not the word ``assessed``. Over folded text (lower-case, every
#: dash ``-``); an optional ``**BLUF:**`` label rides in front.
EPISTEMIC_PREFIX_RE = re.compile(
    r"^\s*(?:\**\s*bluf\s*:?\s*\**\s*)?"
    r"(?:(?:it\s+is\s+|we\s+)?assess(?:ed)?|judged?)(?![\w-])"
    r"\s*(?:that\s+|[:,-]\s*(?:that\s+)?)?"
)


def strip_epistemic_prefix(folded: str) -> str:
    """``folded`` minus a leading *"Assessed …"* frame (see
    :data:`EPISTEMIC_PREFIX_RE`). Unchanged when there is none, or when the
    frame is all there is."""
    out = EPISTEMIC_PREFIX_RE.sub("", folded, count=1)
    return out if out.strip() else folded


def lead_clause_of(folded: str) -> str:
    """Everything before the first clause boundary of already-folded text."""
    m = LEAD_CLAUSE_END_RE.search(folded)
    return folded[: m.start()] if m else folded


def verdict_of(folded: str) -> str:
    """Already-folded text up to its provenance tail (:data:`TAIL_RE`)."""
    m = TAIL_RE.search(folded)
    return folded[: m.start()] if m else folded


def asserts_change(verdict: str) -> bool:
    """Does already-folded verdict text name a change it does not deny?"""
    for m in CHANGE_RE.finditer(verdict):
        start = 0
        for b in NEGATION_SCOPE_END_RE.finditer(verdict, 0, m.start()):
            start = b.end()
        if not NEGATOR_RE.search(verdict, start, m.start()):
            return True
    return False


def opens_on_stasis(text: Any) -> bool:
    """Does the verdict's lead clause (epistemic frame set aside) state stasis?"""
    folded = strip_epistemic_prefix(normalize_for_match(text))
    return bool(STASIS_RE.search(lead_clause_of(folded)))


def text_reports_change(text: Any) -> bool:
    """Does this verdict sentence REPORT A CHANGE, as opposed to stasis?

    Total — ``False`` on anything it cannot read. Absence (a lead span stating
    that nothing happened) is the caller's to rule out first: the world tier
    has a block-level absence test; a desk read's verdict is never an absence
    once it carries a non-quiet status.
    """
    folded = strip_epistemic_prefix(normalize_for_match(text))
    if not folded:
        return False
    if STASIS_RE.search(lead_clause_of(folded)):
        return False
    return asserts_change(verdict_of(folded))


__all__ = [
    "CHANGE_RE",
    "EPISTEMIC_PREFIX_RE",
    "LEAD_CLAUSE_END_RE",
    "STASIS_RE",
    "NEGATION_SCOPE_END_RE",
    "NEGATOR_RE",
    "TAIL_RE",
    "asserts_change",
    "lead_clause_of",
    "opens_on_stasis",
    "strip_epistemic_prefix",
    "text_reports_change",
    "verdict_of",
]
