# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE JUDGE PIPELINE VERSION — the population SPLIT key (2026-07-31).

The verify gate is the product's keystone, so every structural change to it
ships behind ONE version stamp on the critique, the MATCHER_VERSION idiom.
Band calibration, the gold-set loop, the correctness scorer and the scorecard
all read faithfulness history; without a split key they would POOL critiques
graded under different pipelines and read the change as a quality movement.

This module carries the stamp and its FULL per-train lineage — what each bump
changed, which way the population is expected to move, and why pooling across
the boundary would lie. Moved out of ``verify.py`` 2026-08-15 (the size-gate
seam: the lineage is a cohesive documentation unit that had grown to ~180
lines inside a module one line under its ceiling). ``verify`` imports the
constant one way and re-exports it; every existing consumer
(``from ...provenance.verify import JUDGE_PIPELINE_VERSION``) is unchanged.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# THE LINEAGE (oldest first). ONE bump per train, ``<train date>/<n>``; a later
# structural change to the verify path bumps it again, in the same commit.
# ---------------------------------------------------------------------------
# The 2026-07-31 train (V-F claim-splitter hygiene, V-C metadata lookup, V-D
# earned hard-fail severity, V-B slice-scoped absence, A3's counter) is expected
# to shift mean faithfulness UPWARD. That shift is a MEASUREMENT CORRECTION —
# the readout established that both judges over-fail, so the prior mean
# UNDERSTATED true faithfulness — and must never be reported as findings getting
# better. Splitting on this stamp is what makes that statement checkable rather
# than asserted.
#
# 2026-08-02/1 — the F-A PRECISION train, off the 08-02 acceptance readout (all
# three pre-declared gates failed at 2026-07-31/1: 70% agreement vs 85%, 60%
# failure precision vs 75%, one pass-side miss vs zero). W1 makes the
# contradicted branch earn its hard fail (target-scope, composition-body,
# machine-row and carve-out filters, a tighter route, slice-size honesty); W2
# makes a hard fail auditable, correctly labelled in the ledger, and actually
# refuting; W3 splits the citationless shapes; W4 lands the four small checkers.
#
# DIRECTION OF THE EXPECTED SHIFT IS NOT ONE-WAY, and pooling would hide that.
# Hard-fail COUNT should fall sharply (20 of the 27 live contradicted verdicts
# are removed by W1's deterministic filters alone). Mean faithfulness may fall
# SLIGHTLY: W1(e) withdraws ~11% of V-B's supported overrides — claims where a
# subordinate negative was certifying a forecast or a two-read comparison it did
# not cover — and those claims go back to carrying the grader's own verdict.
# Fewer false hard fails AND fewer unearned passes is the intended shape; only
# the split key makes it legible as that rather than as a quality movement.
#
# 2026-08-03/1 — the V-G train, off the 08-03 acceptance RE-RUN (all three gates
# failed again, and agreement REGRESSED 70% -> 63%). F-A's filters worked — zero
# cross-target and zero CAMEO failures in the sample, contradicted 27 -> 15 — and
# in clearing them it exposed what they had been hiding: the judge was refuting
# findings with FINDINGS. 14 of 24 hard fails rested on a quote from an analyst
# output, 13 of them the desk's OWN superseded prior read.
#
#   V-G1  a hard fail's quote must resolve to SOURCE reporting, or to evidence
#         the claim itself cites; anything else demotes to the new soft class
#         judge_prior_read_conflict. Retires the whole anti-update class.
#   V-G2  continuity claims ("no material change since the prior read") leave the
#         V-B slice route — a diff between two assessments is not decidable from
#         a row describing the current state.
#   V-G3  the claim's carve-outs and its SCALE word reach the judge prompt, and
#         a quote landing on an exemption no longer earns the hard class.
#   V-G5  a markerless claim resting on an uncited world BASELINE stops passing
#         by default (the pass-side miss, twice running, on the same shape).
#   F-D   composition citations carry the unit judge's whole-evidence window, and
#         the synthesizer packs against the shared input-token budget.
#
# DIRECTION OF THE EXPECTED SHIFT, again not one-way. Hard-fail COUNT should fall
# again and further: V-G1 alone reaches 14 of 24, V-G2 removes 6 of the 15
# surviving absence hard fails (measured read-only on the stamped day). Mean
# faithfulness should move only slightly, and can move DOWN — V-G5 converts 19 of
# 5,338 silent passes into soft fails, and V-G2 hands 81 verified absences back
# to the judge to grade on citation support. Soft-fail count should FALL where
# F-D's wider evidence window lets a composed clause resolve against the body of
# what it cited instead of its first quarter. Three effects, opposite signs, one
# population: pooling this with 2026-08-02/1 would make every one of them
# invisible.
#
# 2026-08-04/1 — the V-H train, the RESIDUALS the 08-03 adjudication itemized and
# V-G did not reach. Smaller than its predecessors by design: V-G took the classes
# that moved volume, and what is left is four narrow defects and one honest
# refusal.
#
#   V-H1  the judge's citation view carries the OUTLET (`signals.source_id`). An
#         attribution claim — "near-identical framing across CBC, NPR and the
#         BBC" — was unverifiable BY CONSTRUCTION; the panel checked all six
#         outlets by hand and the judge still graded it unsupported.
#   V-H2  the UNDECORATED "Indicators to watch:" label is a heading. The producer
#         has always read it as one and mines its bullets as forward-looking;
#         verify required markdown, so it graded them on citation support a watch
#         item can never carry.
#   V-H3  _metadata_dominant opens a SECOND, evidence-bearing arm: the residual
#         passes when the CITED text covers it and agrees on polarity. The
#         anti-laundering arm is untouched.
#   V-H4  a hard fail whose quote names none of an ENUMERATED denial's listed
#         things demotes to the new soft class judge_contradicted_off_scope.
#   V-H5  a scoped negative is not violated by a slice row whose own leading
#         assertion is a negative about the same subject.
#
# DIRECTION OF THE EXPECTED SHIFT — mostly UP, and small, which is itself the
# reason to split. Every one of these five removes a FALSE failure and none adds
# a new failure class, so mean faithfulness should rise slightly and hard-fail
# count should fall slightly. Measured read-only on the 08-02/1 stamp: V-H4 fires
# on 1 of 24 quoted judge hard fails, V-H5 on 1 of 44 absence hard fails, V-H2
# withdraws roughly 4 graded claims from each of 27 findings a day, and V-H1 and
# V-H3 change what the judge can SEE rather than what it decides — so their
# effect is the one that cannot be predicted from here and is exactly what panel
# 3 is for. Two of the five (V-H1, V-H2) alter the population's claim SET, not
# just its verdicts, which is on its own sufficient reason never to pool this
# stamp with 2026-08-03/1.
#
# 2026-08-05/1 — the R train, the PRECISION batch. Unlike V-G and V-H, which
# corrected how claims were GRADED, this one corrects which claims EXIST and what
# a tally is entitled to be called. Two of its four parts change the population's
# claim SET and one changes the published NUMBER, so pooling it with any earlier
# stamp would make all three invisible at once.
#
#   Q-1a  the labeled-scaffold exemption reads PAST the label. It keyed on the
#         bold run and never looked at what followed, so every
#         `- **Heat-wave alerts:** <cited fact>` bullet was floor-exempt and
#         whole bodies segmented to ZERO claims. Measured: 11 critiques in 7 days
#         with no verdicts at all, 10 of them over 1,026-2,091 characters of
#         substantive cited analysis, every one scored 1.0. The LABELED spelling
#         of a derived read joins the synthesis exemption in the same change, so
#         the fix does not trade a false 1.0 for a false no_citation.
#   Q-1b  zero (or near-zero on a substantive body) checkable claims publishes
#         `unassessable` — a NON-score with its own title, tag, body line and
#         counter — instead of borrowing the top of the scale.
#   Q-1c  a judge_status != 'llm' verdict publishes PROVISIONAL under a ceiling.
#   Q-1d  literal JSON syntax is dropped from the claim stream, counted.
#   R2    a detected P/not-P pair in the composition's INPUT set that the body
#         never surfaced is a counted soft failure.
#   R3    a lead buried under a higher-consequence input (the salience check,
#         advisory since it was written) is a counted soft failure.
#
# DIRECTION OF THE EXPECTED SHIFT — mixed, large, and in both directions at once,
# which is the whole reason for the split key.
#
#   * CLAIM COUNT rises sharply on the affected population. Bodies that produced
#     zero claims now produce several; the Italy energy read replayed at 0 -> 3.
#     Every ratio computed over claim counts moves for that reason alone.
#   * MEAN FAITHFULNESS falls. Roughly a third of critiques scored >= 0.999, and
#     some of that was earned on nothing; those become real scores over real
#     denominators, and the two new soft classes add failures that did not exist.
#   * PUBLISHED overall_score falls further and separately, because ~23% of
#     critiques are floor-only and now cap at the provisional ceiling. That is a
#     LABELLING change, not a grading one: the raw tally is unchanged on the row.
#
# The honest summary is that this stamp measures the same fleet more accurately
# and will therefore look worse than its predecessor. Any comparison across the
# boundary is a comparison of two instruments, not of two fleets.
#
# 2026-08-09/1 — the round-5 pair: one regression fix, one honesty fix.
#
#   V-I1 guard 5  the numeral fingerprint is ENDPOINT-AWARE. Round 5 scored
#         V-I1 0-for-1 on live fires — its one absorption (critique b14bf715)
#         demoted a fully-earned hard fail because "issued 6 Aug 06:00, expires
#         8 Aug 08:00" and "issued August 6 at 7:25AM until August 6 at 8:00AM"
#         flatten to the same magnitude set. Every clock-time / month-day
#         endpoint the claim pins must now match the quote AS an endpoint, or
#         the quote does not confirm. One-directional (can only WITHDRAW a
#         confirmation); the 61-pair replay under 2026-08-05/1 flips only
#         b14bf715.
#   rec #8 (2/2)  an unassessable row publishes faithfulness_score = NULL on
#         the critique's verification block and the trace envelope, instead of
#         a raw 1.0 that entered the population mean and read as a perfect
#         pass. ``overall_score`` stays the real capped float (the lateral /
#         gate key); the raw tally on the report object is unchanged.
#
# DIRECTION OF THE EXPECTED SHIFT — small and honest-side. Hard-fail count may
# rise by the b14bf715 class (a suppression withdrawn is a hard fail restored);
# mean faithfulness computed over ``faithfulness_score`` falls slightly because
# unassessable rows leave the numerator instead of contributing 1.0 — which is
# a denominator correction, not a fleet movement. Pooling across this boundary
# would read both as quality changes; the split key is what makes them legible
# as the measurement corrections they are.
#
# 2026-08-10/1 — V-I1 guard 6: the confirmation fingerprint reads PROSE
#   DIRECTION (round-5 §10-5; judge_quote_rules.py's guard-6 banner carries the
#   mechanism). A claim taking one side of a direction axis whose "confirming"
#   quote takes the OPPOSITE side about the same subject was never confirmed —
#   the suppression withdraws. Withdraw-only like guard 5; the 69-pair replay
#   flips only 037f769f. EXPECTED SHIFT: hard-fail count rises by this class.
#
# 2026-08-15/1 — Phase J, the judge-plane rebuild (FORWARD_PLAN_2026-08-15 §1).
#   Three changes land under one stamp, and every one of them moves the
#   POPULATION rather than a verdict rule — which is exactly what the split key
#   exists to keep legible.
#
#   J1  THE JUDGE MODEL + FAMILY CHANGE. The effective judge repoints from
#       Gemma-4-31B on Cerebras (llm.judge.cerebras_gemma4_31b.openai_compat)
#       to NVIDIA Nemotron 3 Super 120B A12B via OpenRouter
#       (llm.judge.openrouter_nemotron120b.openai_compat, free tier).
#       CROSS-FAMILY IS PRESERVED: an NVIDIA judge over an OpenAI-derived
#       (gpt-oss-120b) producer plane — the independence property the judge
#       plane was rebuilt to keep. Same handler subprovider (.openai_compat →
#       vllm wire shape); the verdict rules are byte-identical. Verdicts from
#       a different model family are a different instrument: never pool.
#
#   J2  THE SAMPLING GATE. Verification is now SAMPLED, not exhaustive: the
#       descriptor-driven ``judge_sample_rate`` (deterministic per finding —
#       hash of the finding id vs the rate, replayable, no RNG) decides which
#       findings the LLM judge grades, and ``judge_sample_always`` names the
#       kinds/analysts that are ALWAYS judged (default: compositions + world +
#       journal — meta_findings_synthesizer, cross_analyst_correlator,
#       situation_tracker, journal_assessor). An UNSAMPLED finding keeps
#       today's deterministic floor under the PROVISIONAL ceiling and
#       publishes ``judge_status='unsampled'`` — a new HONEST state (never
#       'error': nothing failed; the row was deliberately not selected).
#       ``overall_score`` stays a real float (the SQL-laterals contract), and
#       no judge tokens are spent anywhere on an unsampled row (the V-B
#       stage-2 absence check included).
#
#   EXPECTED SHIFT — a population REDEFINITION, not a movement. The
#   llm-judged count DROPS to the sample (at the tree default 0.10 on unit
#   findings: ~compositions + ~10% of unit critiques, sized to the free-tier
#   budget), and ``judge_status='unsampled'`` appears as a new, large stratum
#   that did not exist before. Any adjudicated-share, mean-faithfulness or
#   fail-class ratio computed across this boundary compares an exhaustive
#   census against a sample graded by a different model family — two
#   instruments, two frames. Every measurement over this stamp must carry
#   its n AND its sampling frame (§5 of the plan makes that binding).
#
# 2026-08-20/1 — RUST-1, the EVIDENCE-BYTES fix (panel 2026-08-16 §V-1). What
#   the judge SEES is now what the corpus SCORES. The evidence map was rendered
#   with ``json.dumps`` default ``ensure_ascii=True`` — every non-ASCII char
#   shown as a six-char backslash-uXXXX escape, every newline as the two-char
#   backslash-n — while ``quote_corpus`` was built from the UNESCAPED values.
#   A judge that copied a refuting span VERBATIM from what it was shown (the
#   exact behavior the V-D quote rule demands) could never resolve it when the
#   span contained non-ASCII or crossed a line break; for Cyrillic/Arabic/CJK
#   sources every character was an escape. Measured on 2026-08-15/1 over 14
#   days: 36% of contradiction attempts failed to resolve their quote (77
#   ``judge_contradicted_unquoted`` vs 114+21 resolved).
#
#   Two sides, one stamp:
#     * all four evidence render sites (unit + composition leads, both absence
#       evidence lines) pass ``ensure_ascii=False``;
#     * the quote side un-escapes literal JSON string escapes (backslash-uXXXX
#       incl. surrogate pairs, backslash-n/-t/-r and friends) in the judge's
#       RETURNED span before resolution, and the severity chain canonicalizes
#       onto the resolving form (``_unescape_judge_quote`` /
#       ``_canonical_judge_quote``) — which repairs resolution under BOTH the
#       old and new renderings (newlines are still shown escaped; JSON always
#       escapes control characters). Raw form is tried FIRST, so pure-ASCII
#       single-line behavior is byte-identical and the un-escape can only ADD
#       resolutions, never remove one.
#
#   EXPECTED SHIFT — hard-fail count RISES and ``judge_contradicted_unquoted``
#   FALLS, concentrated on non-ASCII-heavy sources: demotions that were
#   punishing compliance become earned hard fails (or flow to a more specific
#   demotion class further down the chain). Mean faithfulness is UNCHANGED by
#   construction (the demotion train never moves the score, only the severity
#   label), but the hard/soft split — which the panels gate on — moves, which
#   is on its own sufficient reason never to pool across this boundary.
#   BEFORE/AFTER: compare the ``judge_contradicted_unquoted`` share of
#   contradiction attempts on this stamp vs 2026-08-15/1 over the first 48h.
#
# 2026-08-21/1 — RUST-2 + RUST-3, the ABSENCE RUBRIC and the FOURTH VERDICT.
#   Both land in ``judge_absence_rubric.py`` (judge-subsystem brick 6) and both
#   change what a verdict MEANS, which is why they share one stamp.
#
#   RUST-2  the ABSENCE route's system prompt is rewritten, ``absence.v3 ->
#           absence.v4``. The 2026-08-16 panel measured the adjudicated error
#           rate per prompt BRANCH and the negative route was the worst surface
#           in the system: 6 of 7 payload-matched ABSENCE items wrong (86%)
#           against 8 of 30 GENERIC (27%) and 1 of 5 NULL-RESULT (20%). n=7 and
#           is stated as n=7 wherever it is used. The old rubric was a verdict
#           definition and nothing else; the new one carries the four doctrine
#           dimensions — identity, both error costs, what the evidence map
#           actually contains, and THIS route's own adjudicated failure record —
#           and is built on the two mechanisms the panel corroborated: 8 of 13
#           ABSENCE payloads omit the ``PRIOR READ`` label the anti-update rule
#           keyed on (so the rewrite judges analyst prose by SHAPE), and every
#           wrong item carrying carve-out or scale language had NO rendered
#           ``QUALIFIERS`` line while all 7 that did were judged correctly (so
#           the rewrite reads the limiting words off the CLAIM). The
#           ``citation_support`` profile does NOT move: its prompt is
#           byte-identical, which is what keeps this attributable.
#
#   RUST-3  the verdict contract grows a FOURTH token, ``not_a_proposition``
#           (the gap recorded as Q5 of the 08-16 judge-prompt draft). Before it,
#           a judge handed a heading, a scaffold row or a fragment of tool
#           output had no way to say the span asserts nothing: the parser
#           coerced every unrecognised token to ``unsupported``, so the honest
#           answer scored as the pipeline's dominant error class. The token is
#           EARNED — a span carrying a checkable particular cannot be nothing,
#           and that verdict is WITHDRAWN to a soft
#           ``judge_nonpropositional_unearned`` — and an earned one leaves the
#           graded population entirely (V-F's treatment of a split-time drop:
#           counted, never a span, never a ledger row).
#
#   DIRECTION OF THE EXPECTED SHIFT — opposite signs on the two arms, which is
#   the whole reason for the split key.
#     * ABSENCE-route failures should FALL, and the fall should be concentrated
#       in ``judge_contradicted`` (its demotion siblings included) and
#       ``judge_unsupported`` on absence-kind spans. Every one of the six
#       adjudicated errors the rewrite targets is a FALSE FAIL, so mean
#       faithfulness on absence-carrying findings should rise.
#
#       MEASURED, and the measurement is the reason to WATCH rather than to
#       celebrate. ``scripts/rust23_absence_replay.py`` replayed the seven
#       adjudicated ABSENCE-rubric items through the real judge path under both
#       rubrics, six paired runs, one variable (the system prompt), model
#       nvidia/nemotron-3-super-120b-a12b:free, served_by=Nvidia throughout.
#       Agreement with the adjudicated truth, per run, n=7 each:
#         absence.v3  5,5,4,2,3,3 of 7      absence.v4  4,5,5,6,4,6 of 7
#       Pooled over item-runs (n=42 per arm): v3 22/42 (52.4%), v4 30/42
#       (71.4%). The gain is concentrated in STABILITY as much as in verdicts:
#       R5-H17 moves 2/6 -> 6/6 and stops oscillating (two distinct verdicts
#       across runs -> one), R5-H10 4/6 -> 5/6, R5-P9 4/6 -> 5/6, R4-S9 0/6 ->
#       2/6. R5-H16 is 0/6 under BOTH rubrics — a prospective-vs-present-state
#       error no prompt in this train reaches. Runs 1-2 predate the continuity
#       fence and are pooled anyway, which if anything understates v4.
#
#       THE SET'S STRUCTURAL LIMIT: all seven of those items are
#       ``gt_fail=False``, so that arm measures FALSE-FAIL SUPPRESSION and
#       nothing else and cannot see over-correction. A CATCH arm was therefore
#       built (``--set catch``): the adjudicated ``gt_fail=True`` rows whose
#       claim routes to this rubric, replayed from the same kind of real
#       preserved payload, so the refuting evidence is IN-FRAME — inside the
#       slice the claim is scoped to, the only frame the judge answers for.
#       Seven qualify; n=7 items x 6 paired runs (42 item-runs per arm):
#         absence.v3  25/42 (59.5%)      absence.v4  22/42 (52.4%)
#       Paired discordance: v3-caught/v4-missed 7, v4-caught/v3-missed 4. Eleven
#       discordant pairs split seven-four is not a significant difference (exact
#       two-sided p ~ 0.55) — but it is not a tie either, and the point estimate
#       favours v3. State it that way and no further: the arms are NOT
#       distinguishable at this n, and the direction that cannot be ruled out is
#       the dangerous one.
#
#       The movement is concentrated, which is what makes it actionable. v4 loses
#       exactly two items and gains one:
#         R2-S9  4/6 -> 0/6. v3's "catch" was the demoted
#                ``contradicted_machine_row`` class (the quote resolved only in a
#                GDELT/CAMEO row, which V-I4 exists to say is not testimony) and
#                v3 returned FOUR DISTINCT VERDICTS in six runs. The real defect
#                per its adjudication note is ``uncited_world_knowledge`` — a
#                different detector, not this rubric.
#         R5-H1  2/6 -> 0/6. A prior-read CONTINUITY claim, lost to the
#                CONTINUITY FENCE this train added. Its own note reads "right
#                catch WRONG VIOLATOR".
#         R4-S8  1/6 -> 4/6, the one item its note calls "strong".
#
#       THE FENCE WAS THEREFORE ABLATED, and the rubric that ships is v4 WITHOUT
#       it. Both gates re-run on the frame-correct instrument, 6 paired runs each:
#
#                       catch (gt_fail=True)     suppression (gt_fail=False)
#         absence.v3        21/42 (50.0%)             23/42 (54.8%)
#         v4 + fence        22/42 (52.4%)             30/42 (71.4%)
#         v4 ABLATED        22/42 (52.4%)             27/42 (64.3%)
#       Gate: catch 0.524 >= 0.500 PASS · suppression 0.643 >= 0.548 PASS.
#
#       READ THE PAIRED NUMBERS, NOT THE RATES, and here is why. On the catch arm
#       v4 scored 22/42 BOTH WITH AND WITHOUT the fence — the ablation did not
#       move it. What moved was the v3 BASELINE, 25/42 in the first session and
#       21/42 in the second, on identical unchanged code. Four item-runs of drift
#       on a seven-item set is the free lane's nondeterminism, and it is larger
#       than any effect being measured. A gate read off absolute rates across
#       sessions would have called the same rubric FAIL then PASS for no reason
#       inside the rubric.
#
#       The PAIRED discordance is computed within-run on the same items and is
#       immune to that drift, so it is the defensible statistic:
#         catch        v3-only 5 · v4-ablated-only 6
#         suppression  v3-only 1 · v4-ablated-only 5
#       Both favour the ablated rubric; the suppression margin is the real one
#       and the catch margin is a coin-flip that at least does not point the
#       wrong way. R5-H1, the fence's measured casualty, recovers 0/6 -> 2/6,
#       matching v3.
#
#       WHAT THE ABLATION COST, stated because it is a real trade: suppression
#       falls 30/42 -> 27/42. R4-S9 gives back its 2/6 and R5-H17 slips 6/6 ->
#       5/6, while R5-P9 gains 5/6 -> 6/6. The doctrine rewrite's gain survives
#       at +9.5pp over v3 instead of +19pp. That is the price of not carrying a
#       fence that could only cost on the catch side, and it is worth paying.
#
#       NOT A VALID GATE, recorded so nobody rebuilds it: the PROOF round's C-D
#       lane. It adjudicates the PRODUCT at a 14-day world frame while the judge
#       grades fidelity to the 72h slice, and four of its six derivable items
#       resolve to a composition COVERAGE sentence that world evidence cannot
#       refute at all. It also ships a contamination trap — every archived page
#       carries a grader header ending in "Used for: … evidence-existed", and
#       feeding it wholesale put the answer key in the judge's evidence map
#       (measured: 6/6 contaminated vs 2/6 clean on the same six items).
#
#       Instrument note: the free lane is NOT deterministic at temperature 0.0.
#       Individual items flip between identical runs on BOTH arms, so a single
#       n=7 read of this route carries roughly +/-14pp and the paired six-run
#       design is the minimum that says anything.
#     * The CLAIM COUNT falls wherever the fourth verdict fires: a span the
#       judge declines and earns is removed from ``checkable``, from
#       ``branch_scores`` and from the ledger. Every ratio computed over claim
#       counts moves for that reason alone, and a finding whose spans are ALL
#       declined lands on the Q-1b ``unassessable`` path rather than borrowing
#       a vacuous 1.0. Both are population changes, not quality changes.
#     * The ``not_a_proposition`` token is accepted on EVERY route but is
#       advertised only by the absence rubric, so any fire outside that route is
#       a model volunteering it — informative in itself, and previously invisible
#       because it was silently scored ``unsupported``.
#
#   Pooling across this boundary would read a prompt rewrite, a denominator
#   change and a contract change as one quality movement. BEFORE/AFTER: split
#   the absence-kind fail classes on this stamp vs 2026-08-20/1, and read
#   ``claims_ungraded_nonpropositional`` beside
#   ``nonprop_withdrawn_carries_particular`` — the second rising without the
#   first is the laundering shape the earn test exists to make visible.
# ---------------------------------------------------------------------------

#: Stamped into every faithfulness critique's ``data.verification`` block.
#: Re-exported by ``verify`` (the historical import surface).
JUDGE_PIPELINE_VERSION = "2026-08-21/1"
