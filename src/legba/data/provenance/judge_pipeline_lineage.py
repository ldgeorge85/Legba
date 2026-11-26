# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE STRUCTURED JUDGE PIPELINE LINEAGE — the split-key registry, in a form a
reader can pool on.

``judge_pipeline_version.py`` carries the stamp and its FULL prose lineage —
what each bump changed, which way the population is expected to move, and why
pooling across the boundary would lie. This module carries the STRUCTURED
half: the metric-family vocabulary and the per-stamp ``STAMP_EXPECTED_SHIFTS``
registry the prose licenses, so a reader can pool exactly as far as the
lineage says and no further (:func:`judge_pipeline_version.poolable_stamps`).

Moved out of ``judge_pipeline_version.py`` 2026-09-28 (the size-gate seam: the
prose lineage's per-train entries — required to stay in that module so
``test_verify_pipeline_version.py``'s ``inspect.getsource``-based stamp-header
scan keeps finding them — had grown to where the registry table alongside them
pushed the module to its ceiling). ``judge_pipeline_version`` imports this
module's names ONE WAY and re-exports them; every existing consumer
(``from ...provenance.judge_pipeline_version import STAMP_EXPECTED_SHIFTS``,
etc.) is unchanged.
"""
from __future__ import annotations

# ===========================================================================
# THE STRUCTURED LINEAGE (2026-08-29) — what the prose lineage in
# ``judge_pipeline_version.py`` says, in a form a READER can partition on.
# ===========================================================================
#
# THE DEFECT this exists to fix (CAMPAIGN_2026-08-29/PREMISE_GRADING_LOOP.md
# A-7 / Decision 4). The split key rotates faster than the horizons the metrics
# built on it need: 12 distinct stamps across the 26 days
# ``band_calibration_claims`` covers, mean stamp lifetime ~2.3 days, against a
# SHORTEST horizon of 14 days. A claim can therefore never be both
# current-stamped AND resolved, so ``band_calibration_tracker`` has reported
# ``n_scored = 0`` on EVERY run since 2026-08-04 — 1,802 claims, all excluded,
# daily, for 25 days. The companion is the same shape: 573 of 26,949
# faithfulness critiques carry the current stamp.
#
# That is a real failure of THIS module, not of its readers. A split key exists
# to stop DISHONEST pooling, and every entry above is careful to say which way
# the population is expected to move and why pooling would lie. But some of
# those entries say, in terms, that a particular metric CANNOT move across
# their boundary — and a reader that pools nothing has thrown that information
# away along with everything else. Refusing to pool where the lineage itself
# declares no shift is not purity; it is silence wearing purity's clothes.
#
# SO: the prose keeps saying WHY, and this table says WHAT, per metric family,
# so a reader can pool exactly as far as the lineage licenses and no further.
#
# THE CONTRACT, and it is deliberately narrow:
#
#   * An entry describes the shift a stamp introduces relative to its IMMEDIATE
#     PREDECESSOR in this table — i.e. it labels the BOUNDARY the stamp opens,
#     which is exactly what each prose entry's "DIRECTION OF THE EXPECTED
#     SHIFT" paragraph is about.
#   * ``'none'`` means the lineage AFFIRMATIVELY declares that family cannot
#     move across the boundary. It is not "we did not measure one" and not "we
#     expect it to be small" — those are ``'moves'``.
#   * ``'moves'`` is the FAIL-SAFE. Anything ambiguous, unstated, or
#     multi-armed where ANY arm can move the family is ``'moves'``; an unknown
#     or unregistered stamp pools with nothing at all (:func:`poolable_stamps`).
#     Pooling can only ever be WIDENED by an explicit, written declaration.
#   * A stamp carrying TWO trains (the H1+H2 and LRF+V-J1 precedent) takes the
#     MOVING arm for every family either arm moves. One stamp, one verdict.
#
# THIS TABLE IS NOT A LICENCE TO BUMP LOOSELY. It reads the lineage; it does not
# relax it. The measured yield today is that the head stamp pools with nothing
# (see the module's own test suite) — the cadence, not the reader, is what keeps
# calibration dark, and that finding survives this change rather than being
# papered over by it.

#: The SCORE family: ``faithfulness_score``, the mean over it, the published
#: gate/``overall_score``, and everything banded off them. This is the family
#: ``band_calibration_tracker`` and ``unit_correctness_scorer`` calibrate on —
#: a band is a verdict about a faithfulness-gated finding.
METRIC_FAITHFULNESS_SCORE = "faithfulness_score"

#: The SEVERITY family: the hard/soft split the panels gate on — hard-fail
#: count and share, and which class a failure lands in. Moves independently of
#: the score: ``judge_score = supported / checkable`` (verify.py), so a
#: hard->soft DEMOTION changes the label and nothing else.
METRIC_SEVERITY_SPLIT = "severity_split"

#: The CENSUS family: what is IN the denominator and under which counted
#: reason — ``checkable_claims``, the per-reason counters, the claim SET.
METRIC_REASON_CENSUS = "reason_census"

METRIC_FAMILIES: tuple[str, ...] = (
    METRIC_FAITHFULNESS_SCORE,
    METRIC_SEVERITY_SPLIT,
    METRIC_REASON_CENSUS,
)

#: A boundary the lineage declares this family CANNOT cross-move.
SHIFT_NONE = "none"
#: A boundary that moves the family, is expected to, or does not say. FAIL-SAFE.
SHIFT_MOVES = "moves"

#: stamp -> {metric family -> expected shift ACROSS THE BOUNDARY IT OPENS}.
#: Oldest first — the ORDER is load-bearing (:data:`STAMP_LINEAGE` and the
#: adjacency :func:`poolable_stamps` walks are both taken from it). Every entry
#: is derived from the prose lineage above it, quoted in its own comment.
STAMP_EXPECTED_SHIFTS: dict[str, dict[str, str]] = {
    # "expected to shift mean faithfulness UPWARD"; V-D lands EARNED hard-fail
    # severity and A3 adds a counter. Also the first stamp: the boundary it
    # opens is against the unstamped era, which is not a population this table
    # can characterise at all.
    "2026-07-31/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # F-A: "Hard-fail COUNT should fall sharply ... Mean faithfulness may fall
    # SLIGHTLY" (W1(e) withdraws ~11% of V-B's supported overrides); W3 splits
    # the citationless shapes.
    "2026-08-02/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # V-G: "Mean faithfulness should move only slightly, and can move DOWN";
    # hard-fail count falls again; V-G1 mints the ``judge_prior_read_conflict``
    # class and V-G5 converts 19 silent passes into soft fails.
    "2026-08-03/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # V-H: "mean faithfulness should rise slightly and hard-fail count should
    # fall slightly", and V-H1/V-H2 "alter the population's claim SET, not just
    # its verdicts".
    "2026-08-04/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # R/Q-1: "MEAN FAITHFULNESS falls", "CLAIM COUNT rises sharply", published
    # overall_score falls further and separately. Every family, loudly.
    "2026-08-05/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # V-I1 guard 5 ALONE would be score-neutral (a withdrawn suppression is a
    # hard fail restored — a severity move). But rec #8 ships under the same
    # stamp and publishes ``faithfulness_score = NULL`` on unassessable rows:
    # "mean faithfulness ... falls slightly because unassessable rows leave the
    # numerator". A denominator correction is still a MOVE for the score family,
    # and the moving arm decides the stamp.
    "2026-08-09/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # V-I1 guard 6, and the ONLY declared shift is "hard-fail count rises by
    # this class". Withdraw-only, one-directional, and the thing it withdraws is
    # a hard->soft DEMOTION (``_VERDICT_QUOTE_CONFIRMS`` ->
    # ``hardfail_demoted_quote_confirms``): both the demoted and the restored
    # form are unsupported spans, and ``judge_score = supported / checkable``,
    # so the score cannot move. This is 2026-08-20/1's own rule — "the demotion
    # train never moves the score, only the severity label" — applied to its
    # sibling guard, which the 08-10 entry states as a mechanism rather than as
    # a score claim. The 69-pair replay flipping exactly one critique is the
    # measurement that bounds it.
    "2026-08-10/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_NONE,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # Phase J — THE HARDEST BOUNDARY IN THE LINEAGE. The judge MODEL and FAMILY
    # change AND verification becomes sampled: "compares an exhaustive census
    # against a sample graded by a different model family — two instruments, two
    # frames". Nothing pools across a different instrument.
    "2026-08-15/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # RUST-1, and the declaration is explicit: "Mean faithfulness is UNCHANGED
    # by construction (the demotion train never moves the score, only the
    # severity label), but the hard/soft split — which the panels gate on —
    # moves". ``judge_contradicted_unquoted`` falls, so the census moves too.
    "2026-08-20/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_NONE,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # RUST-2 + RUST-3: "mean faithfulness on absence-carrying findings should
    # rise", and "The CLAIM COUNT falls wherever the fourth verdict fires".
    "2026-08-21/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # #58 title parity: "``absence_slice_contradicted`` may rise slightly on the
    # translated-source population". That class is an ADDED hard failure, not a
    # demotion — a claim that passed now fails — so the score falls with it.
    "2026-08-25/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # H1 + H2, one stamp, two trains, and both move the score in terms: H1 —
    # "``faithfulness_score`` falls on exactly the claims that rested on the
    # product's own bookkeeping"; H2 — "every affected composition's
    # ``faithfulness_score`` can only FALL".
    "2026-08-27/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # V-J. V-J1 alone is score-neutral by its own words ("the claim still fails,
    # only the severity moves, so ``faithfulness_score`` cannot move because of
    # V-J1 at all") — but V-J2 ships under the same stamp and "is the only part
    # that can move the SCORE (a stage-2 verdict flipping contradicted ->
    # supported withdraws a failure outright)". The moving arm decides.
    "2026-08-28/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # LRF + V-J1 ACTIVATION. The V-J1 arm cannot move the score, but LRF is a
    # MEASUREMENT DEFINITION change with the number attached: 8,866 spans ADDED
    # to the denominator and "fleet mean published gate 0.571 -> 0.556".
    "2026-08-29/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # "The ONLY entry in this lineage whose expected shift is NONE" — the
    # [N+1] transparency train's grading-equivalence proof was 201/201
    # byte-identical on both arms and its replay reason census unchanged.
    "2026-08-30/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_NONE,
        METRIC_SEVERITY_SPLIT: SHIFT_NONE,
        METRIC_REASON_CENSUS: SHIFT_NONE,
    },
    # D-3, the ASSEMBLY ARMS + the shared fold. Every family moves, and the
    # entry above says why in terms: "the composition tier's faithfulness number
    # changes MEANING, because the GRADED BODY CHANGED SHAPE ... POOLING ACROSS
    # THIS BOUNDARY IS INVALID FOR ``country_composition``, ``world_assessor``
    # AND ``escalation_composition``"; sixteen new HARD reason codes enter the
    # severity table; and ``_absence_content_terms`` re-points at the shared fold,
    # "a measured 4,272 of 7,562 absence claims (56.5%)". Two arms with opposite
    # signs inside one number is the strongest possible case for SHIFT_MOVES —
    # a fail-safe here is not caution, it is the only honest reading.
    "2026-09-03/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # THE VERIFY-REGIME FIX. All three families move, and the entry above says
    # where: on the ASSEMBLY population ONLY. The registry's key is a METRIC
    # FAMILY, not a population — there is no "assembly family" row to write — so
    # the population split is stated in the prose and re-derivable from
    # ``data.data.assembly.regime``, which is stamped on every composition row
    # for exactly this purpose.
    #
    #   * ``faithfulness_score``: "the published overall goes from 0.1739-0.5926
    #     (mean 0.3524) to 0.5714-0.9200 (mean 0.8528), and admission at the 0.50
    #     floor goes from 2/35 to 35/35".
    #   * ``severity_split``: "26 HARD ``scope_widened`` findings leave the
    #     assembly population entirely". ``_FAIL_CLASS_BY_REASON`` itself is
    #     BYTE-IDENTICAL across this bump — no reason is added, removed or
    #     re-classed — and both exhaustive pins are re-asserted unchanged.
    #   * ``reason_census``: "``scope_widened`` falls from 26 to 0 on the assembly
    #     population", plus two new COUNTERS that are not reasons.
    #
    # SHIFT_NONE was available for none of the three and would have been wrong for
    # all of them: ``poolable_stamps()`` treats MOVES as a hard stop, and a reader
    # pooling a 0.35 mean with an 0.85 mean across this boundary would be pooling
    # a mis-grade with a measurement. The LEGACY population is 40/40 byte-identical
    # and pools across this boundary freely — which is a statement about the ROWS,
    # and this registry keys on the stamp.
    "2026-09-05/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # G3 — the weighted-comparison licence. The prose entry's own "DIRECTION OF
    # THE EXPECTED SHIFT" paragraph, transcribed: "the ASSESSMENT family MOVES on
    # all three; the ASSEMBLY and DESK families cannot move at all".
    #
    #   * ``faithfulness_score``: "MOVES, on Assessment rows only and UPWARD
    #     only", and it moves ``branch_scores["citation_support"]`` with it.
    #   * ``severity_split``: "MOVES … a licence can retire a hard fail"
    #     (``judge_contradicted`` over a relayed refusal to crown).
    #   * ``reason_census``: "MOVES … ``judge_unsupported`` and
    #     ``judge_contradicted`` fall on the Assessment population", plus seven
    #     new COUNTERS that are not reasons.
    #
    # SHIFT_NONE was available for none of the three ON THIS TABLE'S TERMS, and
    # the reason is worth stating because the temptation here is real: the
    # ASSEMBLY and LEGACY populations are provably inert (40/40 byte-identical),
    # so a reader might expect a 'none'. But this registry keys on the STAMP, and
    # a stamp is one boundary for the whole fleet — the Assessment rows carry it
    # too. Declaring 'none' would license pooling an Assessment row graded under
    # the licence with one graded without it, which is exactly the comparison the
    # split key exists to refuse. Inertness on a SUBSET is a statement about the
    # rows and belongs in the prose entry, where it is; it is not a licence to
    # pool the stamp.
    "2026-09-06/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # PARTITION-PRESERVE. The prose entry's own "DIRECTION OF THE EXPECTED SHIFT"
    # paragraph, transcribed: "all three families MOVE, fleet-wide, and UPWARD in
    # adjudicated volume".
    #
    #   * ``faithfulness_score``: "MOVES, in BOTH directions and by design" — a
    #     mixed partial score can land above or below the floor's number, and the
    #     0.85 cap lifting moves rows that clear it upward.
    #   * ``severity_split``: "MOVES … a kept absence verdict can be
    #     ``judge_contradicted`` (HARD) where the discarded pass published a soft
    #     floor ``no_citation``".
    #   * ``reason_census``: "MOVES … the denominator itself changes: a partial
    #     row's ``checkable_claims`` is the judged count plus residual floor
    #     spans". No reason code added, removed or re-classed.
    #
    # This is the loudest 'moves' in the lineage and the reason is worth stating:
    # the boundary is not a rubric edit on one population, it is the fleet's
    # judged/floored MIX changing. Pooling a row graded while a third of its
    # judge calls were being silently discarded with one graded after would
    # compare two different instruments wearing one name — which is the exact
    # comparison the split key exists to refuse.
    "2026-09-08/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # PARTIAL VERDICTS + THE EVIDENCE ENVELOPE. Two arms, and the prose entry's
    # own "DIRECTION OF THE EXPECTED SHIFT" paragraph declares all three
    # families moving on BOTH of them.
    #
    #   * ``faithfulness_score``: "MOVES, both directions" — ~1.5% of judge
    #     calls convert from a floor-only 0.85-capped provisional verdict to a
    #     mixed adjudicated one, and the wider envelope lets a claim resolve
    #     deep in its evidence (up) while the 3,000–3,200 source band loses its
    #     excerpt softening (down).
    #   * ``severity_split``: "MOVES … a recovered verdict can be
    #     ``judge_contradicted`` (HARD) where the discarded pass published a
    #     soft floor reason", and a judge that can read to the end of an
    #     evidence string can earn a quote it could not resolve before — the
    #     difference between a hard fail and a ``contradicted_unquoted``
    #     demotion.
    #   * ``reason_census``: "MOVES … the denominator changes on the recovered
    #     population". ``_FAIL_CLASS_BY_REASON`` is BYTE-IDENTICAL across this
    #     bump; one new reason PREFIX and two sparse receipts that are not
    #     reasons.
    #
    # The two-train rule applies and takes the moving arm for every family
    # either arm moves — but here it changes nothing, because each arm moves all
    # three on its own. SHIFT_NONE was available for none of them: the envelope
    # arm alone changes what the grader can SEE on every unit finding in the
    # fleet, which is the one thing whose effect cannot be predicted from here.
    "2026-09-20/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # H3 — the NAMED-CLAIM contract. The prose entry's own paragraph declares
    # all three families moving: named-claim salvage changes the judged/floored
    # MIX exactly the way 2026-09-20/1's arm A did (score either direction, a
    # kept ``judge_contradicted`` where a soft floor reason stood, census
    # denominators off the recovered population) — and the reply-shape text is
    # a prompt change besides, the one effect nobody can predict from here.
    # SHIFT_NONE was available for none of them.
    "2026-09-24/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # H3-MEASURE. ``aligned_by`` is read off the SAME branch align_verdicts
    # already took — it names the branch, never decides one. No scoring/
    # severity consumer reads it or the four new keys. Earned, not assumed.
    "2026-09-25/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_NONE,
        METRIC_SEVERITY_SPLIT: SHIFT_NONE,
        METRIC_REASON_CENSUS: SHIFT_NONE,
    },
    # carried_unsupported. The prose entry's own words: "narrow and
    # one-directional ... inert on any finding carrying no memory citation; on
    # findings that do, it can only ADD soft spans." A dated or numeric claim
    # whose resolved citations are ALL memory blocks (prior read / window
    # ledger) now soft-fails under this reason where it previously scored
    # supported — SEVERITY and CENSUS move on the memory-citing population by
    # construction, and the score follows because
    # ``judge_score = supported / checkable``. SHIFT_NONE was available for
    # none of them: the shift is narrow and one-directional, but it is a real
    # move, not an absent one — split on this stamp and read the new reason
    # against the memory-citing population only.
    "2026-09-28/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # carried_ungraded (SW-9). The prose entry: a memory-only unit finding
    # publishes a NON-SCORE state — the SCORE family moves (rows leave the
    # mean; the gate number caps at 0.5). The state is stamped after every fold
    # and touches no span, verdict or reason, so SEVERITY and CENSUS are
    # declared ``none`` — earned by the byte-identical-ledger pin, not assumed.
    "2026-10-03/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_NONE,
        METRIC_REASON_CENSUS: SHIFT_NONE,
    },
    # The world read's subject-group floor (W4 F-3). The prose entry: one new
    # check unit on every world assembly (SCORE moves, world only), one new
    # reason code in the table (CENSUS moves, expected count 0). No existing
    # reason is re-classed and the new one is hard by construction, so the
    # hard/soft split is declared ``none``. Inert on country, thematic and
    # region rows (replay-proven).
    "2026-10-03/2": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_NONE,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # The sampling regime (2026-11-13): temperature 0.0/0.1 -> 1.0 on the judge,
    # critic and graders. The prose entry: direction not predicted, so ALL
    # families are declared ``moves`` — the boundary is marked and nothing
    # pools across it.
    "2026-11-13/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
    # The judges stop leaning toward a pass; two verdict words added; unknown
    # verdict words fail loudly. Every family moves.
    "2026-11-25/1": {
        METRIC_FAITHFULNESS_SCORE: SHIFT_MOVES,
        METRIC_SEVERITY_SPLIT: SHIFT_MOVES,
        METRIC_REASON_CENSUS: SHIFT_MOVES,
    },
}

#: The stamps in lineage order, oldest first. ONE source of truth — derived from
#: the registry rather than repeated, so the two can never drift apart.
STAMP_LINEAGE: tuple[str, ...] = tuple(STAMP_EXPECTED_SHIFTS)

__all__ = [
    "METRIC_FAITHFULNESS_SCORE",
    "METRIC_FAMILIES",
    "METRIC_REASON_CENSUS",
    "METRIC_SEVERITY_SPLIT",
    "SHIFT_MOVES",
    "SHIFT_NONE",
    "STAMP_EXPECTED_SHIFTS",
    "STAMP_LINEAGE",
]
