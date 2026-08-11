# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quote rules — the judge subsystem's FOURTH brick, and the one the
module-size gate named in advance ("the next extraction seam is the JUDGE
subsystem — prompt registry + ``_run_judge`` + **the quote/severity rules**").

Everything here answers ONE question: *has this contradiction EARNED the
platform's highest-severity label?* A ``judge_contradicted`` verdict says the
finding misstates its own cited source. Four panels running, roughly half of
them were wrong, and every fix has taken the same shape — a deterministic,
conservative test that DEMOTES an unearned hard fail to a soft class that names
what actually went wrong, while leaving the claim failed.

The rules, oldest first (each block below carries its own rationale):

* **V-D** — a contradiction must POINT AT its refutation (``_quote_resolves``).
* **W2** — the quote must REFUTE, not merely RESOLVE (``_quote_refutes``).
* **V-G1** — only SOURCE REPORTING, or evidence the claim itself cited, may
  carry a hard fail (``_quote_is_refutable_evidence``).
* **V-G3** — a quote landing on a clause the claim already EXEMPTS refutes
  nothing (``_quote_hits_a_carve_out``).

``verify`` imports these ONE WAY and re-exports every name, so
``verify._quote_refutes`` and friends resolve exactly as before. Nothing here
touches the report/ledger types verify owns — the SEVERITY DECISION (which
verdict label a demotion carries) stays there, next to the fail-class table.
"""
from __future__ import annotations

import re
from typing import Any

from .absence_slice import (
    _absence_carve_outs,
    _absence_content_terms,
    _absence_route_exclusion,
    absence_scope_qualifier,
)


# ---------------------------------------------------------------------------
# V-D (2026-07-31) — HARD/SOFT severity routing: a CONTRADICTION requires a QUOTE.
#
# The readout's structural finding #4, seen in BOTH judges: ``judge_contradicted``
# — the platform's highest-severity verdict, "the finding misstates its own cited
# source" — was stamped on claims the cited evidence plainly CONFIRMS. Half the
# Cerebras hard-fails and three quarters of the same-model hard-fails were false.
# The class is unusable until the severity is EARNED rather than asserted, so the
# readout carries a standing warning: gate nothing on the hard/soft split.
#
# The mechanical rule: a hard fail must be able to POINT AT the refutation. The
# judge is asked for a ``quotes`` array parallel to ``verdicts``, and a
# ``contradicted`` verdict is kept HARD only when its quote RESOLVES — a verbatim
# run of the evidence text the judge was actually shown. Missing, paraphrased, or
# invented quote ⇒ demoted to the soft ``judge_contradicted_unquoted``, counted
# ``hardfail_demoted_no_quote``. The claim still FAILS (the score is unchanged by
# the demotion — only the severity label moves), so a judge that ignores the new
# field costs nothing but hard-fail precision it had not earned.
#
# SCOPE: the judge's contradiction verdict ONLY. The other hard reasons are
# deterministic and carry their proof by construction — ``unresolved_citation``
# IS its own evidence (the marker resolves to nothing), and the M13/M15/E-1
# guards each carry the matched surface in the span text.
# ---------------------------------------------------------------------------

#: The verdict label ``_run_judge`` substitutes for an unquotable contradiction.
_VERDICT_CONTRADICTED_UNQUOTED = "contradicted_unquoted"
#: Its span reason — SOFT (see the fail-class table). Distinct from
#: ``judge_unsupported`` so calibration can tell a demoted contradiction from a
#: plain unsupported verdict.
_JUDGE_CONTRADICTED_UNQUOTED = "judge_contradicted_unquoted"
#: W2 (2026-08-02) — the verdict label for a contradiction whose quote RESOLVES
#: but does not REFUTE (see :func:`_quote_refutes`).
_VERDICT_CONTRADICTED_UNREFUTED = "contradicted_unrefuted"
#: Its span reason — SOFT, and distinct from ``judge_contradicted_unquoted``
#: because the defect is different: this judge DID point at evidence, it just
#: pointed at evidence that resolves the claim's subject rather than opposing it.
_JUDGE_CONTRADICTED_UNREFUTED = "judge_contradicted_unrefuted"
#: V-G1 (2026-08-03) — the verdict label for a contradiction whose quote resolves
#: only inside ANOTHER FINDING (see the SIGNALS-ONLY REFUTATION block below).
_VERDICT_PRIOR_READ_CONFLICT = "prior_read_conflict"
#: Its span reason — SOFT, and its own class: the desk did not misstate its
#: evidence, it DISAGREES with a finding (overwhelmingly its own superseded prior
#: read). That is real information and worth recording; it is not fabrication.
_JUDGE_PRIOR_READ_CONFLICT = "judge_prior_read_conflict"
#: V-H4 (2026-08-04) — the verdict label for a contradiction whose quote names
#: NONE of the things an ENUMERATED denial listed (see ``absence_slice.
#: quote_misses_the_denied_scope``).
_VERDICT_CONTRADICTED_OFF_SCOPE = "contradicted_off_scope"
#: Its span reason — SOFT, and distinct from ``judge_contradicted_unrefuted``
#: because a panel reading the counters has to be able to tell the two demotion
#: mechanisms apart. V-G8's fidelity rule: a pooled counter that answers a
#: different question than the ledger is how the last one hid.
_JUDGE_CONTRADICTED_OFF_SCOPE = "judge_contradicted_off_scope"
#: V-I1 (2026-08-05) — the verdict label for a contradiction whose quote states
#: the CLAIM'S OWN NUMBERS back to it (see the NUMBER-WORDING block below).
_VERDICT_QUOTE_CONFIRMS = "contradicted_confirmed"
#: Its span reason — SOFT, and its own class because it is the only one of the
#: five demotions that says the judge got the DIRECTION wrong rather than the
#: evidence or the scope. A panel has to be able to count it separately to know
#: whether this fix is the one that moved precision.
_JUDGE_QUOTE_CONFIRMS = "judge_quote_confirms_claim"
#: V-I4 (2026-08-05) — the verdict label for a contradiction grounded ONLY in a
#: GDELT/CAMEO machine-coded row (see ``judge_evidence.machine_coded_ordinals``).
_VERDICT_CONTRADICTED_MACHINE_ROW = "contradicted_machine_row"
#: Its span reason — SOFT. The quote is real and it resolves; what it is NOT is
#: testimony. A code label ("STUDENT <-> PAPUA: protest in Jakarta") is a
#: machine's reading of an article, and the V-B route has excluded exactly this
#: class 2,109 times a day since W1(c).
_JUDGE_CONTRADICTED_MACHINE_ROW = "judge_contradicted_machine_row"
#: V-I5 (2026-08-05) — the verdict label for a contradiction on a claim the V-B
#: CONTINUITY ROUTER already routed out of slice checking.
_VERDICT_ROUTE_EXCLUDED = "contradicted_route_excluded"
#: Its span reason — SOFT. One claim cannot have two authorities: if the router
#: decided this claim is a continuity / volume / trajectory read rather than a
#: slice-checkable negative, the judge does not get to hard-fail it anyway.
_JUDGE_CONTRADICTED_ROUTE_EXCLUDED = "judge_contradicted_route_excluded"
#: Shortest quote we will try to resolve. A 1-3 word fragment matches almost any
#: evidence by accident, which would make the requirement decorative.
_JUDGE_QUOTE_MIN_CHARS = 16

# The output-shape addendum, appended to every place the judge's JSON shape is
# stated (the absence rubric + both the unit and composition prompt leads, which
# the M14 survey call also uses). ONE constant so the requirement cannot drift
# between routes, and so a prompt diff is a single greppable change.
_JUDGE_QUOTE_RULE = (
    ' Alongside "verdicts", return "quotes": a list of the SAME length, one entry '
    "per claim, in the same order. For a \"contradicted\" verdict the entry MUST be "
    "a VERBATIM span copied from the evidence above that refutes the claim; for "
    'every other verdict use "". Never paraphrase and never invent a quote — a '
    "contradiction whose quote is not found verbatim in the evidence is recorded "
    "as a lesser, unsupported-class finding."
    # V-G1: the anti-update rule, stated to the judge as well as enforced below.
    " A PRIOR READ block, or any other ANALYST FINDING in the evidence, is the "
    "BASELINE this claim is updating — it is NOT counter-evidence. Never quote a "
    "prior read, a sibling desk's assessment, or any analyst prose to justify "
    '"contradicted": a desk that revises its view on fresh reporting is doing its '
    "job. Refute a claim ONLY with the SOURCE reporting itself."
)


def _normalize_quote_text(text: str) -> str:
    """Case/whitespace-folded text for verbatim quote resolution."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _quote_resolves(quote: Any, corpus: str) -> bool:
    """True when ``quote`` is a long-enough VERBATIM run of the shown evidence.

    ``corpus`` is the already-normalized evidence the judge was handed for this
    partition. Surrounding quotation marks / ellipses are tolerated; everything
    else must match, so a paraphrase or an invented span fails.
    """
    if not isinstance(quote, str):
        return False
    cleaned = _normalize_quote_text(quote).strip("\"'“”‘’ .…")
    if len(cleaned) < _JUDGE_QUOTE_MIN_CHARS:
        return False
    return cleaned in corpus


# ---------------------------------------------------------------------------
# W2 (2026-08-02) — REFUTES vs RESOLVES. D1 is mechanical: it asks only whether
# the quote is verbatim evidence. Three unearned hard fails in the 08-02 panel
# satisfied it anyway, because the judge supplied a quote that RESOLVES the
# claim's subject without opposing it:
#
#   * the South Africa register-metadata claim, verbatim-correct against the
#     register, with the quote lifted from the PRIOR READ block the claim
#     explicitly contrasts against;
#   * the Australia claim the prior read entailed word-for-word (the same judge
#     passed the byte-identical claim shape elsewhere in the same sample);
#   * the Israel elite-fracture claim.
#
# Two deterministic tests, both conservative (a miss leaves the hard fail):
#   R1 RESTATEMENT — the quote is a verbatim run of the CLAIM itself. Evidence
#      that says what the claim says cannot be what refutes it.
#   R2 PRIOR-READ-ONLY — the quote resolves ONLY inside a PRIOR READ block AND
#      the claim explicitly frames itself against the prior read. The block the
#      claim is diffing against is its subject, not its refutation.
# ---------------------------------------------------------------------------

#: Both producers label the continuity block with this exact token
#: (``unit_grounding._render_prior_read`` /
#: ``meta_findings_synthesizer._render_prior_read_lines``), which is what makes
#: R2 a greppable contract rather than a heuristic.
_PRIOR_READ_MARKER = "prior read"

#: A claim that FRAMES itself against the prior read — the R2 precondition.
_CLAIM_CITES_PRIOR_READ_RE = re.compile(
    # V-G2 widened the READ arm from the two fixed bigrams to the qualifier form
    # the producers actually emit — "the prior WORLD read", "the prior VERIFIED
    # read", "the previous COMPOSITION read". The 08-03 `hard_fail#9` specimen
    # ("since the prior world read of 2026-08-03T00:00:15Z") missed R2 on exactly
    # that gap.
    r"\b(?:prior|previous|earlier|last)\s+(?:\w+\s+){0,2}?read\b"
    r"|\bprior\s+assessment\b"
    r"|\bearlier\s+assessment\b"
    r"|\bprior\s+situation\s+register\b|\bpreviously\s+(?:assessed|concluded)\b",
    re.IGNORECASE,
)


def _quote_restates_claim(quote: Any, claim: str) -> bool:
    """R1 — the quote is a verbatim run of the CLAIM, so it confirms, not refutes."""
    if not isinstance(quote, str):
        return False
    cleaned = _normalize_quote_text(quote).strip("\"'“”‘’ .…")
    if len(cleaned) < _JUDGE_QUOTE_MIN_CHARS:
        return False
    return cleaned in _normalize_quote_text(claim)


def _quote_refutes(
    quote: Any, claim: str, corpus: str, prior_read_corpus: str
) -> bool:
    """Does this quote EARN the hard fail — verbatim evidence that OPPOSES the claim?

    ``corpus`` / ``prior_read_corpus`` are the already-normalized evidence this
    partition showed, whole and prior-read-only respectively. Returns False for a
    quote that does not resolve at all (the original D1 rule), that restates the
    claim (R1), or that lives only in the prior read a claim is diffing against
    (R2). Never raises.
    """
    if not _quote_resolves(quote, corpus):
        return False
    if _quote_restates_claim(quote, claim):
        return False
    if (
        prior_read_corpus
        and _quote_resolves(quote, prior_read_corpus)
        and _CLAIM_CITES_PRIOR_READ_RE.search(claim)
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# V-G1 (2026-08-03) — SIGNALS-ONLY REFUTATION. The 08-03 panel's headline
# finding, and the single highest-yield fix on its list: the judge refutes
# findings with FINDINGS.
#
# Tracing all 23 surviving ``judge_contradicted`` quotes back to their origin:
# 7 (30%) came from a real SIGNAL, **13 (57%) from the SAME DESK'S OWN PRIOR
# READ**, 1 from another analyst's output, 1 from nowhere. 14 of 24 hard fails
# therefore rest on a finding quoting a finding. The mechanism penalises exactly
# the behaviour the system exists to produce:
#
#   * ``lane_hormuz`` — the desk shifts holding -> degrading on four fresh attack
#     signals (IRGC drone intercept, a second LNG tanker struck with force
#     majeure on 24 cargoes, explosions off Oman). The judge hard-fails the new
#     BLUF by quoting its OWN prior read verbatim: "No material change in the
#     physical flow through the Strait of Hormuz … the situation is holding
#     steady." The update is the finding; the "refutation" is the thing it
#     supersedes.
#   * ``country_g20_de`` — same shape, and the quote's tail ("…confirming a
#     concrete security action rather than mere rhetoric") is the PRIOR
#     FINDING'S editorial phrasing, not source text at all. The label "verbatim
#     evidence span" was true of the string and false of its status as evidence.
#
# THE RULE. A hard fail must point at SOURCE REPORTING. The refuting quote must
# resolve inside a ``signals``-backed evidence entry (:func:`_signal_backed_ordinals`)
# — OR inside an entry THE CLAIM ITSELF CITES, which is the escape hatch that
# keeps the composition tower gradeable: a composed clause that cites
# ``[[ref:3]]`` can still be hard-failed by what ``[[ref:3]]`` says, because the
# clause chose that evidence and answers for it. What is barred is the judge
# reaching for an UNCITED finding — a prior read, a sibling desk's assessment —
# and calling the disagreement a fabrication.
#
# A quote that resolves ONLY in barred evidence is NOT discarded: the
# disagreement is REAL and worth recording, so the verdict demotes to the soft
# ``judge_prior_read_conflict`` with the quote persisted. The desk changed its
# mind (or diverged from a sibling); that is an UPDATE, not a misstatement of
# evidence, and it must not carry the platform's highest-severity label.
#
# RELATION TO W2/R2, which stays: R2 is the narrower, older test for a claim that
# explicitly FRAMES itself against the prior read it cites. This rule is
# structural and does not care about framing. They compose — R2 still catches the
# case where the claim cites the prior read (so V-G1 admits it) and is then
# "refuted" by the very block it is diffing against.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# V-G3 (2026-08-03) — CARVE-OUTS AND SCALE QUALIFIERS ON THE JUDGE PATH.
#
# F-A W1(d) extracted a claim's carve-out clauses and handed them to the V-B
# stage-2 prompt, and the 08-03 re-run confirms that half worked. The equivalent
# blindness persists on the LLM JUDGE path, which V-D's quote rule does not
# screen for: a quote can satisfy D1 perfectly while refuting only a clause the
# claim ALREADY EXCLUDED. ``hard_fail#4``'s Germany BLUF is refuted by the
# border-control extension the finding explicitly discloses and carves out one
# bullet later — "No reported border incidents… the prior border-control
# extension remains the only noted action [47]."
#
# Two surfaces, because the two failure modes are different:
#
#   * THE PROMPT — every judge route now sees a claim's carve-out clauses and its
#     scope qualifier beneath the claim, the same way stage 2 does. A judge that
#     cannot see a clause as a clause will read the exempted thing as the
#     violation, which is exactly what W1(d) found on the other path. Claims with
#     neither are rendered byte-identically, so this is additive.
#   * THE SEVERITY RULE — deterministic, because a rubric line is a request and
#     this needs to be a guarantee. When a claim carries an explicit carve-out and
#     the refuting quote lands on THAT clause rather than on the claim's
#     assertion, the hard class is not earned: the quote resolves without
#     refuting, which is precisely W2's existing ``contradicted_unrefuted``.
#
# The deterministic arm is CLAIM-LEVEL by design. A carve-out disclosed elsewhere
# in the FINDING (the Germany shape, where the exemption sits in a sibling
# bullet) is stated to the judge in the rubric but NOT mechanically enforced —
# any body-wide overlap test loose enough to catch it also swallows genuine
# catches like the Todd-Blanche entity scramble, whose quote necessarily shares
# its vocabulary with the finding it refutes. Enforcement stays where it can be
# exact; the prompt carries the rest.
# ---------------------------------------------------------------------------

#: Shared content terms required before a quote counts as landing on a carve-out
#: rather than on the claim. Two is enough to be non-accidental and few enough
#: that a real refutation of the assertion is not swept up.
_CARVE_OUT_QUOTE_MIN_SHARED_TERMS = 2

#: The rubric sentences appended to every judge lead, alongside the quote rule.
_JUDGE_QUALIFIER_RULE = (
    " Some claims are followed by an indented QUALIFIERS line. Honour it "
    "strictly. CARVE-OUTS name what the claim already EXEMPTS — evidence of an "
    "exempted thing is evidence the claim ACCOUNTS FOR, never a contradiction. "
    "SCALE names the size the claim denies — evidence of the same kind of event "
    "at a SMALLER scale (dozens against 'mass', one incident against "
    "'widespread') SUPPORTS the claim rather than refuting it. The same holds "
    "for anything the finding itself discloses elsewhere in its own prose: a "
    "fact the analyst states and accounts for is not a fact the analyst hid."
)


def _judge_claim_block(i: int, claim: str) -> str:
    """One numbered claim for a judge prompt, plus its QUALIFIERS line (V-G3).

    Byte-identical to the pre-V-G3 ``f"{i}. {claim}"`` for every claim that
    carries neither a carve-out clause nor a scope qualifier — which is most of
    them, so the prompt diff is confined to the claims that need it.
    """
    block = f"{i}. {claim}"
    parts: list[str] = []
    carve_outs = _absence_carve_outs(claim)
    if carve_outs:
        parts.append("CARVE-OUTS (already exempted): " + "; ".join(
            f"'{c}'" for c in carve_outs
        ))
    qualifier = absence_scope_qualifier(claim)
    if qualifier:
        parts.append(f"SCALE/KIND qualifier: '{qualifier}'")
    if parts:
        block += "\n   QUALIFIERS — " + " | ".join(parts)
    return block


def _quote_hits_a_carve_out(quote: Any, claim: str) -> bool:
    """V-G3 — does this refuting quote land on a clause the CLAIM EXEMPTS?

    Compares the quote's distinctive content terms against each carve-out clause
    and against the claim's own remaining assertion. A quote is a carve-out hit
    only when it shares at least :data:`_CARVE_OUT_QUOTE_MIN_SHARED_TERMS` terms
    with an exemption AND shares no more with the assertion — so a quote that
    genuinely refutes what the claim asserts is never swept up. Conservative in
    both directions; never raises.
    """
    if not isinstance(quote, str) or not quote.strip():
        return False
    carve_outs = _absence_carve_outs(claim)
    if not carve_outs:
        return False
    quote_terms = _absence_content_terms(quote, target_id=None)
    if not quote_terms:
        return False
    assertion = claim
    for clause in carve_outs:
        assertion = assertion.replace(clause, " ")
    assertion_terms = _absence_content_terms(assertion, target_id=None)
    best = 0
    for clause in carve_outs:
        shared = quote_terms & _absence_content_terms(clause, target_id=None)
        best = max(best, len(shared))
    if best < _CARVE_OUT_QUOTE_MIN_SHARED_TERMS:
        return False
    return best > len(quote_terms & assertion_terms)


def _quote_is_refutable_evidence(
    quote: Any, signal_corpus: str, self_cited_corpus: str
) -> bool:
    """V-G1 — may this quote carry a hard fail at all (see the block above)?

    ``signal_corpus`` is the normalized text of every ``signals``-backed evidence
    entry this partition showed; ``self_cited_corpus`` the entries THIS claim's
    own markers name. Either is enough. A quote that resolves in neither lives
    only in analyst prose the claim did not cite, and cannot earn the hard class.
    Never raises.
    """
    if signal_corpus and _quote_resolves(quote, signal_corpus):
        return True
    return bool(self_cited_corpus) and _quote_resolves(quote, self_cited_corpus)


# ---------------------------------------------------------------------------
# V-I1 (2026-08-05) — NUMBER-WORDING BLINDNESS. A quote that CONFIRMS the claim
# cannot be what refutes it.
#
# The round-4 panel's H9 is the type specimen, and it needs no interpretation:
#
#   claim  "Russia's broader assault on Ukraine's Kyiv region claimed SIXTEEN
#           lives and THIRTY-SIX injuries [2]."
#   quote  "In the Kyiv region, 36 people have been wounded ... and 16 people
#           have been killed."
#
# The quote is the claim, in digits. It was stamped ``judge_contradicted`` — the
# platform's highest severity, "this finding misstates its own cited source" —
# on an EXACT numeric match. 5 of the round's 14 hard fails rest on a quote that
# confirms, resolves, or restates the claim; this is the one with a mechanical
# form, and it is the single highest-yield fix on the panel's own list.
#
# W2's R1 restatement test was built for exactly this shape and cannot see it:
# it asks whether the quote is a verbatim RUN of the claim, and these two strings
# share almost no characters, because one spells its numbers as WORDS and the
# other as DIGITS. Every other equivalence in the same family has the same
# property — "25%" vs "25 per cent", "1.2 million" vs "1,200,000", "sixteenth"
# vs "16th". The judge is reading two different surfaces of one number.
#
# THE RULE. Fingerprint both sides as NUMBERS, not as text. When every number
# the claim asserts also appears in the quote, and the two are talking about the
# same thing (>= 2 shared content terms), the quote CONFIRMS and the hard class
# is not earned. Four guards keep it narrow:
#
#   1. citation markers are stripped FIRST. "[20]" is not the number 20, and a
#      claim whose only numeral is its own marker must not be gradeable here.
#   2. the claim must assert at least one magnitude >= 2. "first"/"one"/"no"
#      normalize to 1 and 0, which are determiner-like and far too weak to
#      license a demotion on their own.
#   3. SUBSET, not intersection: EVERY number the claim asserts must be in the
#      quote. This is what keeps the round's H10 hard — "made his first official
#      visit ... on 4 August" against "will make ... from Aug. 6-7" fingerprints
#      {1, 4} against {1, 6, 7}, and 4 is missing, which is precisely the
#      falsehood the judge correctly caught.
#   4. topical binding: 16 dead in Kyiv is not confirmed by 16 dead in Gaza.
#
# Cost of a miss vs a false fire: a demotion moves the SEVERITY LABEL and leaves
# the claim failed and its score unchanged, which is the trade the whole
# demotion train makes. A false hard fail is a fabrication accusation.
#
#   5. ENDPOINT BINDING (2026-08-09, the round-5 tightening). Round 5 scored the
#      rule 0-for-1 on live fires: critique `b14bf715` (journal_assessor) claimed
#      a Special Marine Warning "issued 6 Aug 06:00 EDT, expires 8 Aug 08:00
#      EDT", the judge's quote said "issued August 6 at 7:25AM EDT until August 6
#      at 8:00AM EDT" — a two-day warning invented out of a 35-minute one, the
#      round's one fully-earned third hard fail on that entry, and this rule
#      demoted it. The set-of-magnitudes subset held ({6, 0, 8} is inside
#      {6, 7, 25, 8, 0}) because flattening "06:00" and "8 Aug" to bare digits
#      loses exactly the structure the claim asserts: WHICH endpoint each number
#      pins. So the fingerprint is now endpoint-aware: every clock time and every
#      month-day date the CLAIM pins must appear in the quote AS an endpoint of
#      the same type, or the numbers have diverged and the quote does not
#      confirm. A date-range claim whose endpoint differs from the quote's is
#      refuted by it, not confirmed (the panel's own words). Strictly
#      one-directional: the check can only WITHDRAW a confirmation, never mint
#      one, so H9 (no endpoints anywhere) and every non-firing case are
#      byte-identical — the 61-pair replay under the 2026-08-05/1 stamp flips
#      only b14bf715.
#
#   6. DIRECTION BINDING (2026-08-10, the round-5 §10-5 arm, withdraw-only).
#      Guard 5 closed the numeric half of the divergence class and left the
#      PROSE half open: critique `037f769f` (region_composition) claimed the
#      Burkina Faso assessment "shows no material change since the prior
#      7 August read", and the judge's quote reports Burkinabe troops killing
#      "at least 48 civilians, a concrete casualty figure that was ABSENT from
#      the prior 7 August read". Every numeral the claim asserts is in the
#      quote and its one endpoint (7 Aug) is stated back, so guards 1-5
#      correctly stay quiet — the wrongness is DIRECTIONAL: the claim asserts
#      continuity and the quote asserts novelty, about the same prior read.
#      The rule: when the claim takes exactly one side of a direction axis
#      (no-change/new, rise/fall incl. gained/lost, open/closed, begin/end,
#      above/below, improve/worsen) and the quote takes exactly the OPPOSITE
#      side of the same axis, with the two directional clauses sharing >= 2
#      content terms, the numbers were never a confirmation — the suppression
#      withdraws and the judge's hard fail stands. Lexicon-based and
#      conservative by construction: negated direction words are dropped, a
#      text hitting both sides of an axis abstains, and a missed withdrawal is
#      acceptable while a false one re-creates the wrong-hard-fail class the
#      whole demotion train exists to kill. One-directional like guard 5: it
#      can only WITHDRAW a confirmation, never mint one — the 69-pair replay
#      under the 2026-08-05/1 + 2026-08-09/1 stamps flips only 037f769f.
# ---------------------------------------------------------------------------

#: Word numerals the corpus actually writes out. Bounded on purpose — a general
#: number parser would buy nothing here and would need its own test surface.
_WORD_CARDINALS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
#: The ORDINAL surface of the same values ("the sixteenth strike", "16th").
_WORD_ORDINALS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50,
    "sixtieth": 60, "seventieth": 70, "eightieth": 80, "ninetieth": 90,
}
#: Scale words that MULTIPLY the number in front of them, so "1.2 billion" and
#: "1,200,000,000" fingerprint identically.
_SCALE_WORDS: dict[str, int] = {
    "hundred": 100, "thousand": 1_000, "million": 1_000_000,
    "billion": 1_000_000_000, "trillion": 1_000_000_000_000,
}
#: Citation markers, stripped before ANY numeral is read off a claim or a quote.
#: ``[20]`` is a pointer, not a quantity; reading it as one is how a claim with
#: no numbers at all would become gradeable by this rule.
_MARKER_STRIP_RE = re.compile(r"\[\[ref:\d+(?:\s*,\s*\d+)*\]\]|\[\d+(?:\s*[-–—]\s*\d+)?\]")
#: A digit run with optional thousands separators and decimal tail.
_DIGIT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
#: Hyphenated word compounds ("thirty-six", "twenty-fifth") — joined before the
#: token scan so both halves are read as ONE value.
_WORD_COMPOUND_RE = re.compile(
    r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[-\s]"
    r"(one|two|three|four|five|six|seven|eight|nine|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b"
)
#: How many topical terms the quote and claim must share before a numeric match
#: counts as CONFIRMATION rather than coincidence. Same threshold, and the same
#: reasoning, as V-G3's carve-out screen.
_CONFIRMING_QUOTE_MIN_SHARED_TERMS = 2
#: The claim must assert at least one number this large. 0 and 1 arrive from
#: "no", "a", "one" and "first" and carry no quantitative content.
_CONFIRMING_QUOTE_MIN_MAGNITUDE = 2


def _numeral_fingerprint(text: Any) -> set[float]:
    """Every NUMBER ``text`` asserts, normalized to a bare magnitude.

    Word numerals, hyphenated compounds and ordinals all collapse onto the digit
    form; a scale word immediately after a number multiplies it; ``%`` / "per
    cent" / currency symbols and units fall away because only the magnitude is
    kept. Citation markers are stripped first. Never raises.
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    low = _MARKER_STRIP_RE.sub(" ", text).lower().replace("per cent", "percent")
    low = _WORD_COMPOUND_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}", low)
    out: set[float] = set()
    tokens = re.findall(r"[a-z]+|\d[\d,]*(?:\.\d+)?", low)
    pending: float | None = None
    tens: float | None = None
    for token in tokens:
        scale = _SCALE_WORDS.get(token)
        if scale is not None:
            # "1.2 billion" / "thirty thousand" — multiply what came before;
            # a bare "hundreds of" with nothing in front contributes nothing.
            if pending is not None:
                out.discard(pending)
                out.add(pending * scale)
                pending = None
            tens = None
            continue
        value: float | None = None
        if _DIGIT_RE.fullmatch(token):
            try:
                value = float(token.replace(",", ""))
            except ValueError:
                value = None
        elif token in _WORD_CARDINALS:
            value = float(_WORD_CARDINALS[token])
        elif token in _WORD_ORDINALS:
            value = float(_WORD_ORDINALS[token])
        elif token.rstrip("s") in _WORD_CARDINALS:
            value = float(_WORD_CARDINALS[token.rstrip("s")])
        if value is None:
            tens = None
            pending = None
            continue
        # "thirty six" (the de-hyphenated compound) sums into ONE value.
        if tens is not None and 0 < value < 10:
            out.discard(tens)
            value = tens + value
            tens = None
        elif value in (20, 30, 40, 50, 60, 70, 80, 90):
            tens = value
        else:
            tens = None
        out.add(value)
        pending = value
    return out


#: Month names + the abbreviations the corpus writes, for the DATE endpoint
#: form. Bounded like ``_WORD_CARDINALS`` — "may"/"march" are also verbs, and a
#: false endpoint on either side can only make the rule STRICTER (a confirmation
#: withdrawn), never mint a suppression, so the ambiguity is on the safe side.
_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

#: A clock time — "06:00", "7:25AM", "08:00 EDT", "19:45:30 UTC". Exactly two
#: digits after the colon keep score lines ("won 3:1") out of the endpoint set.
_CLOCK_ENDPOINT_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*(a\.?m\.?|p\.?m\.?)\b)?"
)
#: A month-day date, day first — "6 Aug", "4th of August", "6-7 August".
_DATE_DAY_FIRST_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*[-–—]\s*(\d{{1,2}})(?:st|nd|rd|th)?)?\s+(?:of\s+)?({_MONTH_ALT})\b"
)
#: …and month first — "August 6", "Aug. 6-7", "August 6th".
_DATE_MONTH_FIRST_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*[-–—]\s*(\d{{1,2}})(?:st|nd|rd|th)?)?\b"
)


def _endpoint_fingerprint(text: Any) -> set[tuple[Any, ...]]:
    """Every TYPED endpoint ``text`` pins: clock times and month-day dates.

    Guard 5 (the round-5 tightening). ``_numeral_fingerprint`` flattens "06:00"
    to {6, 0} and "8 Aug" to {8} — bare magnitudes with the structure removed,
    which is how "issued 6 Aug 06:00, expires 8 Aug 08:00" matched a quote
    saying "issued August 6 at 7:25AM until August 6 at 8:00AM". This keeps the
    structure: a clock time becomes ``("time", minutes-since-midnight)`` (12h
    and 24h spellings collapse — "8:00AM" and "08:00" are one endpoint) and a
    month-day date becomes ``("date", month, day)`` in either written order,
    range forms contributing both days. Citation markers are stripped first,
    exactly as in the magnitude fingerprint. Never raises.
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    low = _MARKER_STRIP_RE.sub(" ", text).lower()
    out: set[tuple[Any, ...]] = set()
    for hh, mm, meridiem in _CLOCK_ENDPOINT_RE.findall(low):
        hour, minute = int(hh), int(mm)
        if hour > 23 or minute > 59:
            continue
        if meridiem.startswith("p") and hour != 12:
            hour += 12
        elif meridiem.startswith("a") and hour == 12:
            hour = 0
        out.add(("time", hour * 60 + minute))
    for day, day_to, month in _DATE_DAY_FIRST_RE.findall(low):
        for d in (day, day_to):
            if d and 1 <= int(d) <= 31:
                out.add(("date", _MONTHS[month], int(d)))
    for month, day, day_to in _DATE_MONTH_FIRST_RE.findall(low):
        for d in (day, day_to):
            if d and 1 <= int(d) <= 31:
                out.add(("date", _MONTHS[month], int(d)))
    return out


#: Guard 6's direction axes — ``(name, one side, the other side)``. Bounded on
#: purpose, like ``_WORD_CARDINALS``: every entry is a surface the corpus
#: actually writes, chosen so a match is unambiguous about which way it points.
#: Polysemous surfaces are deliberately ABSENT ("open" — "open situation
#: register entries"; "launches" — the noun in "six drone launches"; "lower" —
#: "lower house"; bare "new" — "New Delhi"), and the risky remainder carries an
#: exclusion in the pattern ("closed doors", "declined to comment"). A missed
#: hit means a missed WITHDRAWAL, which is the cheap direction.
_DIRECTION_AXES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    (
        "steady-vs-new",
        re.compile(
            r"\bno (?:material |significant |substantive |notable |major |new )?changes?\b"
            r"|\bunchanged\b|\bstatus quo\b|\bholding steady\b"
            r"|\b(?:remain(?:s|ed)?|stay(?:s|ed)?|hold(?:s|ing)?|held|keeps?|kept) "
            r"(?:broadly |largely |essentially |relatively |mostly )?"
            r"(?:steady|stable|unchanged|flat|constant|low|high|elevated)\b"
        ),
        re.compile(
            r"\b(?:absent|missing) from the prior\b|\bnot in the prior\b"
            r"|\bnew since the prior\b|\bfor the first time\b"
        ),
    ),
    (
        # gained/lost folds in here: territory gained against territory lost is
        # the same axis as a count that rose against a count that fell.
        "rise-vs-fall",
        re.compile(
            r"\b(?:rise[sn]?|rising|rose|increase[sd]?|increasing|surge[sd]?"
            r"|surging|climb(?:s|ed|ing)?|grew|grow(?:s|ing)?|jump(?:s|ed|ing)?"
            r"|spike[sd]?|escalat(?:e[sd]?|ing)|accelerat(?:e[sd]?|ing)"
            r"|intensif(?:y|ies|ied|ying)|expand(?:s|ed|ing)?|widen(?:s|ed|ing)"
            r"|lift(?:s|ed|ing)?|gain(?:s|ed|ing)?|doubled|tripled|up from)\b"
        ),
        re.compile(
            r"\b(?:fall(?:s|ing|en)?|fell|decline[sd]?"
            r"(?!\s+to\s+(?:comment|say|confirm|discuss|elaborate|provide|answer|be\b))"
            r"|declining|decrease[sd]?|decreasing|drop(?:s|ped|ping)?"
            r"|slump(?:s|ed)?|shrink(?:s|ing)?|shrank|shrunk|plunge[sd]?"
            r"|plummet(?:s|ed)?|subside[sd]?|reced(?:e[sd]|ing)|eas(?:ed|ing)"
            r"|de-?escalat(?:e[sd]?|ing)|narrow(?:s|ed|ing)|halved"
            r"|los(?:es?|t|ing)|down from)\b"
        ),
    ),
    (
        "open-vs-closed",
        re.compile(
            r"\b(?:re-?open(?:s|ed|ing)?|resum(?:e[sd]?|ing|ption)"
            r"|restart(?:s|ed|ing)?|back online)\b"
        ),
        re.compile(
            r"\b(?:closures?|closed(?![ -]doors?)|shut(?:s|ting|tered|down)?"
            r"|suspend(?:s|ed|ing)?|suspensions?|halt(?:s|ed|ing)?"
            r"|taken offline)\b"
        ),
    ),
    (
        "begin-vs-end",
        re.compile(
            r"\b(?:begin(?:s|ning)?|began|begun|start(?:s|ed|ing)?"
            r"|commenc(?:e[sd]?|ing)|underway|(?:takes?|took) effect)\b"
        ),
        re.compile(
            r"\b(?:end(?:s|ed|ing)?|conclude[sd]?|concluding|ceas(?:e[sd]|ing)"
            r"|expir(?:e[sd]?|es|ing|y|ation)|terminat(?:e[sd]?|ing)"
            r"|w(?:i|ou)nd(?:s|ing)? down|called off)\b"
        ),
    ),
    (
        # Both sides demand their object (a digit or a named reference level) so
        # document deixis ("as noted above,") and idiom ("under a new law")
        # never register as a threshold position.
        "above-vs-below",
        re.compile(
            r"\b(?:above|exceed(?:s|ed|ing)?|surpass(?:es|ed|ing)?)\s+"
            r"(?:the |its |a |an )?(?:\d|threshold|limit|cap|ceiling|target"
            r"|average|baseline|forecast|projection)"
        ),
        re.compile(
            r"\b(?:below|under|short of)\s+"
            r"(?:the |its |a |an )?(?:\d|threshold|limit|cap|ceiling|target"
            r"|average|baseline|forecast|projection)"
        ),
    ),
    (
        "improve-vs-worsen",
        re.compile(r"\bimprov(?:e[sd]?|ing|ements?)\b"),
        re.compile(
            r"\b(?:worsen(?:s|ed|ing)?|deteriorat(?:e[sd]?|ing|ion)"
            r"|degrad(?:e[sd]?|ing|ation))\b"
        ),
    ),
)

#: A direction word under negation asserts nothing this guard can use: "did not
#: rise" is not a rise, and treating it as one would fire the guard against a
#: quote that says "fell" — the two AGREE. Negated hits are simply dropped
#: (the CHANGE axis's "no material change" is a whole phrase on its steady
#: side, so it never passes through this filter's window).
_DIRECTION_NEGATOR_BEFORE_RE = re.compile(
    r"\b(?:no|not|never|without|neither|nor|den(?:y|ies|ied))(?:\s+\w+){0,2}\s*$"
)
#: Clause boundaries for the subject-binding step — sentence punctuation plus
#: the semicolon, which is how composition claims chain their per-country arms.
_DIRECTION_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]")


def _direction_hits(low: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """Spans where ``pattern`` asserts a direction — negated matches dropped."""
    return [
        (m.start(), m.end())
        for m in pattern.finditer(low)
        if not _DIRECTION_NEGATOR_BEFORE_RE.search(low[max(0, m.start() - 32) : m.start()])
    ]


def _direction_hit_clauses(low: str, hits: list[tuple[int, int]]) -> list[str]:
    """The clause (sentence / semicolon segment) around each direction hit."""
    bounds = [0] + [m.end() for m in _DIRECTION_CLAUSE_SPLIT_RE.finditer(low)] + [len(low)]
    out: list[str] = []
    for start, _ in hits:
        for lo, hi in zip(bounds, bounds[1:]):
            if lo <= start < hi:
                out.append(low[lo:hi])
                break
    return out


def _prose_direction_diverges(claim: str, quote: Any) -> bool:
    """Guard 6 — does the quote's PROSE point the opposite way from the claim's?

    True only when, on some single axis of :data:`_DIRECTION_AXES`, the claim
    hits exactly one side, the quote hits exactly the other, and a claim clause
    containing the hit shares >= :data:`_CONFIRMING_QUOTE_MIN_SHARED_TERMS`
    content terms with a quote clause containing the opposite hit — the same
    subject, read in two directions. Mixed or absent polarity on either text
    abstains, so the guard is conservative in exactly the direction the class
    demands: it can only WITHDRAW a suppression. Never raises.
    """
    if not isinstance(claim, str) or not isinstance(quote, str):
        return False
    claim_low = _MARKER_STRIP_RE.sub(" ", claim).lower()
    quote_low = _MARKER_STRIP_RE.sub(" ", quote).lower()
    for _name, side_a, side_b in _DIRECTION_AXES:
        ca, cb = _direction_hits(claim_low, side_a), _direction_hits(claim_low, side_b)
        qa, qb = _direction_hits(quote_low, side_a), _direction_hits(quote_low, side_b)
        if ca and not cb and qb and not qa:
            claim_hits, quote_hits = ca, qb
        elif cb and not ca and qa and not qb:
            claim_hits, quote_hits = cb, qa
        else:
            continue
        for clause in _direction_hit_clauses(claim_low, claim_hits):
            terms = _absence_content_terms(clause, target_id=None)
            if not terms:
                continue
            for quote_clause in _direction_hit_clauses(quote_low, quote_hits):
                shared = terms & _absence_content_terms(quote_clause, target_id=None)
                if len(shared) >= _CONFIRMING_QUOTE_MIN_SHARED_TERMS:
                    return True
    return False


def quote_confirms_the_claim(quote: Any, claim: str) -> bool:
    """V-I1 — does this "refuting" quote state the claim's OWN numbers back to it?

    ``True`` only when the claim asserts at least one non-trivial number, EVERY
    number it asserts appears in the quote under numeral/unit/word normalization,
    EVERY clock-time / month-day endpoint it pins appears in the quote as an
    endpoint of the same type (guard 5 — diverging endpoints refute, they do not
    confirm), the two share at least
    :data:`_CONFIRMING_QUOTE_MIN_SHARED_TERMS` topical terms, and the quote's
    prose does not point the OPPOSITE way on a direction the claim asserts
    (guard 6 — a diverging direction refutes, whatever the numbers say).
    Never raises.
    """
    if not isinstance(quote, str) or not quote.strip():
        return False
    claim_nums = _numeral_fingerprint(claim)
    if not any(n >= _CONFIRMING_QUOTE_MIN_MAGNITUDE for n in claim_nums):
        return False
    if not claim_nums <= _numeral_fingerprint(quote):
        return False
    # Guard 5 (2026-08-09): SUBSET again, over typed endpoints. A claim that
    # pins "expires 8 Aug 08:00 EDT" against a quote whose only date is
    # August 6 has DIVERGED from its evidence, however many surface digits the
    # two share — the round-5 over-suppression (critique b14bf715, the banner
    # above). One-directional by construction: an empty claim endpoint set
    # (H9 and every case like it) passes untouched.
    if not _endpoint_fingerprint(claim) <= _endpoint_fingerprint(quote):
        return False
    shared = _absence_content_terms(claim, target_id=None) & _absence_content_terms(
        quote, target_id=None
    )
    if len(shared) < _CONFIRMING_QUOTE_MIN_SHARED_TERMS:
        return False
    # Guard 6 (2026-08-10): the PROSE arm of guard 5's principle. A claim that
    # asserts "no material change since the prior 7 August read" against a
    # quote reporting a casualty figure "absent from the prior 7 August read"
    # shares every numeral and its only endpoint with it — and has still
    # DIVERGED from its evidence (critique 037f769f, the banner above). Runs
    # last so it only ever evaluates pairs that would otherwise confirm;
    # one-directional by construction: a pair with no opposite-direction
    # reading (H9, the marine warning, and every non-firing case) is
    # byte-identical.
    return not _prose_direction_diverges(claim, quote)


# ---------------------------------------------------------------------------
# V-I5 (2026-08-05) — ONE CLAIM CANNOT HAVE TWO AUTHORITIES.
#
# 08-03 recommendation #2 shipped and works: `absence_slice_route_excluded_
# continuity_claim` fired **115 times on 114 rows** in the round-4 population.
# The router's job is to decide what KIND of claim it is looking at — a
# continuity read ("no material change since the prior read"), a volume read, a
# trajectory read — and to take the ones that are not slice-checkable negatives
# OFF the V-B route, because grading them there produced false hard fails.
#
# On H13 it fired on the very claim the judge then hard-failed. Routing a claim
# out of V-B does not exempt it from the judge, so the class the router was built
# to protect is still reachable by the more expensive path, and the panel's
# verdict is exact: **the router fix is bypassable.**
#
# THE RULE. The routing decision is BINDING. A claim the router took off the
# slice route cannot be hard-failed by the judge either. The claim still FAILS —
# only the severity moves, as with every other demotion — and the ledger says
# which authority decided, so nobody has to re-derive it from claim text.
#
# Deliberately claim-level and quote-blind: this is not a statement about the
# evidence, it is a statement about what the platform already decided this claim
# IS. The same precondition the V-B fold uses (a scope qualifier is present)
# gates it, so the two paths cannot disagree about which claims are in scope.
# ---------------------------------------------------------------------------


def claim_is_routed_out(claim: str) -> str | None:
    """The V-B router's exclusion CLASS for this claim, or ``None``.

    Mirrors ``_fold_absence_slice``'s gate exactly — a scope qualifier, then the
    route exclusion — so the judge path and the slice path answer the same
    question the same way. Never raises.
    """
    if not isinstance(claim, str) or absence_scope_qualifier(claim) is None:
        return None
    return _absence_route_exclusion(claim)
