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
# 2026-07-31/1 — THE FIRST STAMP. The train (V-F claim-splitter hygiene, V-C
# metadata lookup, V-D earned hard-fail severity, V-B slice-scoped absence, A3's
# counter) is expected to shift mean faithfulness UPWARD. That shift is a
# MEASUREMENT CORRECTION —
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
#
# 2026-08-25/1 — #58, V-B TITLE PARITY. ``load_absence_slice_rows``'s signal
#   leg projected bare ``payload->>'title'``; the renderer
#   (``inline_target._signal_title``, T-1b/M13) has always preferred the
#   stored English translation ``payload->>'title_en'`` first. On a
#   translated (non-Latin) source the two disagreed: the V-B stage-1 screen
#   and the stage-2 slice judge prompt (which is SHOWN this text) read the
#   raw transliterated/native-script title, while the desk the analyst
#   actually worked from — and the claim's own English content terms — read
#   English. The signal leg now reads
#   ``COALESCE(NULLIF(payload->>'title_en', ''), payload->>'title', '')``,
#   the same precedence order and the same COALESCE/NULLIF idiom the
#   2026-08-21/1 body-precedence rider already used one line below it. The
#   composed-row (``analyst_outputs``) leg is unchanged — composed prose is
#   always English and carries no ``title_en`` surface.
#
#   EXPECTED SHIFT — narrow and one-directional. This can only ADD candidate
#   titles to the stage-1 screen on translated sources (an English title_en
#   collides with a claim's English content terms where a native-script title
#   never could), so ``absence_slice_contradicted`` may rise slightly on the
#   translated-source population and nowhere else; every non-translated
#   signal (``title_en`` absent) resolves to the exact same title as before,
#   byte-identical. Splitting on this stamp is what keeps that population
#   boundary visible rather than reading as a fleet-wide precision movement.
#
# 2026-08-27/1 — H1, THE REGISTER SELF-CORROBORATION GUARD. One new counted
#   soft reason, ``register_self_corroboration``: a fact-asserting claim whose
#   resolved citations are ALL ``ref_kind='situation_register'`` and which
#   asserts CURRENCY ("remains", "continues", "ongoing", "active driver") or
#   CORROBORATION ("confirms", "corroborates", "bears out") about the world.
#   This is CORRECTNESS-R2's largest single-mechanism ``inaccurate`` mass
#   (ATTRIBUTION §1): the desks write "no material change" into the situation
#   register, the register reports that back as intensity and recency, and the
#   desks then cite it as confirmation the underlying event is live. At the
#   round's T0 the AR composition's BLUF read "the register confirms the strike
#   remains the active driver with high intensity" about a strike that had ended
#   three weeks earlier. The register is ``[[ref:N]]``-citable, so the standing
#   "no claim may rest on orientation alone" clause never reached it; this
#   closes that exemption from the verify side while the paired render change
#   (``window_ledger.REGISTER_SELF_CORROBORATION_RULE``, printed at the head of
#   both the unit and composition register blocks, plus a per-frame
#   ``last_corroborated_at`` / ``STALE-NO-NEW-EVIDENCE`` field) closes it at the
#   prompt.
#
#   EXPECTED SHIFT — narrow, one-directional, and confined to a population you
#   can name. The guard is INERT on any finding that carries no register
#   citation, which is most of the fleet; on findings that DO cite the register
#   it can only ADD soft spans, so ``faithfulness_score`` falls on exactly the
#   claims that rested on the product's own bookkeeping. Split on this stamp and
#   read ``register_self_corroboration`` against the register-citing population
#   only — pooling it with non-citing findings dilutes the very thing it
#   measures. Expect the count to FALL over subsequent stamps as the paired
#   prompt rule takes; a count that stays flat means the render rule is not
#   reaching the model and the fix is at the prompt, not here.

# 2026-08-27/1 — H2, COMPOSITION-LAYER INTEGRITY. A new judge-subsystem brick
#   (``composition_integrity.py``) grades a COMPOSITION against the desk reads it
#   CITES, on the axis the CORRECTNESS-R2 round measured and no check could see:
#   the composition may not claim what its inputs do not support. Four new span
#   reasons — ``absence_scope_laundered`` (soft; a collection-scoped desk
#   negative republished as a world fact), ``attribution_direction_conflict``
#   (HARD; "the military-posture read CONFIRMS ... INCREASING" over a head whose
#   verdict is "remains UNCHANGED"), ``attribution_asserts_desk_negative`` (soft;
#   a desk cited for the presence of what it records as absent) and
#   ``attribution_ungrounded_quote`` (soft; a coinage quoted as a named desk's
#   own words that appears nowhere in it). PLUS a prompt change: the composition
#   judge lead now carries an additive rubric block naming those three shapes and
#   a fourth — the direction inversion the mechanical arms deliberately decline —
#   with the graded sentences as its negatives.
#
#   TWO REASONS THIS IS A POPULATION BOUNDARY, not a precision movement.
#   (1) THE DENOMINATOR MOVES ON COMPOSITIONS ONLY. Each violation is one more
#   checkable-but-unsupported claim in the ``_fold_guard_spans`` shape, so every
#   affected composition's ``faithfulness_score`` can only FALL, and a
#   composition with no laundered scope and no attributed clause is
#   byte-identical. Unit findings are untouched by construction: without the
#   ``[[ref:N]]`` sub-claim convention the evidence map is empty and all four
#   arms are inert. Pooling across this stamp would read a newly-visible defect
#   class as a fleet-wide quality regression on the composition population.
#   (2) THE COMPOSITION JUDGE PROMPT CHANGED. Every composition claim is now
#   graded against a lead ~2.5k chars longer, which moves verdicts independently
#   of the deterministic arms; a stamp that did not split here could not tell the
#   prompt's effect from the checks'.
#
#   EXPECTED SHIFT — ``attribution_direction_conflict`` is the only HARD class
#   and is expected to be RARE (four conjunctive conditions: an explicit
#   attribution shape, a desk that resolves to a cited head, opposed direction
#   poles with both ambivalence guards clear, and both poles nameable verbatim).
#   ``absence_scope_laundered`` is expected to be the volume class — three of the
#   ten graded lanes carried it. It is DISJOINT from W31 by construction (W31
#   skips cited spans; this arm requires a citation), so
#   ``unscoped_absence_claim`` must NOT move: if it does, the two checks have
#   started double-charging one span and that is the thing to look at first.
#   BEFORE/AFTER: split the composition population on this stamp vs 2026-08-25/1
#   and read the four ``composition_integrity_*`` violation counters beside
#   ``composition_integrity_attributions_seen`` /
#   ``composition_integrity_attributions_clean`` — the pass side is what says
#   whether a rising violation count is detection or over-firing.
#
# 2026-08-28/1 — V-J1, THE DISCLOSED-AND-DOWNWEIGHTED CONFLICT, plus the two
#   DOMAIN-COLLISION few-shots. Off the 08-27 hard-fail step-change check
#   (planning/HARDFAIL_STEPCHANGE_CHECK_2026-08-27.md), which adjudicated 13 of
#   the 24 live ``absence_slice_contradicted`` hard fails on 2026-08-25/1 and
#   tallied 5 true catches, 5 clear over-fires and 3 borderline. That check's
#   own verdict was that the 08-25/1 title-parity attribution is WRONG (the
#   non-absence route rose MORE, and title-parity cannot touch it) — so nothing
#   here reverts it. What the sample DID surface, three times in thirteen plus
#   once more on the generic route, is the defect this stamp fixes.
#
#   V-J1  THE GUARD, deterministic. A composition sentence of the shape "a
#         WEAKLY-SUPPORTED read says no-X, which CONFLICTS WITH the VERIFIED
#         finding of X" was being hard-failed by a row resolving back to the
#         SAME weak side the sentence had already named, cited and rejected.
#         ``_is_absence_claim`` is a substring test, so the embedded quoted
#         negative trips the absence grammar even though it describes the pole
#         the sentence is DISCLOSING, not the one it asserts. The new
#         ``absence_slice.hedged_conflict_disclosure`` decides it on the
#         sentence's own text — three conjunctive lexical conditions, the
#         weakness marker positional against the absence idiom — in the shape
#         ``composition_integrity.direction_conflict``'s AMBIVALENCE GUARD and
#         V-D's earned-detail rule already use: both poles named verbatim or
#         nothing is emitted. It binds on BOTH routes, and deliberately by two
#         different levers: the V-B router gains a ``hedged_conflict`` class
#         (so no ``absence_slice_contradicted`` and no stage-2 tokens), and the
#         judge severity chain gains its own rule ahead of V-I5 — because V-I5
#         is gated on a scope qualifier and the generic-route specimen carries
#         none. New soft reason ``judge_contradicted_hedged_conflict``.
#
#   V-J2  THE RUBRIC, judge-side. The two OVER-FIRE families the same census
#         itemized are not decidable lexically — both are one WORD spanning two
#         subject matters — so they ship as negatives in the V-B stage-2
#         system prompt's new third override rule (DOMAIN COLLISION), worded
#         from the cases: "Indonesia sanctions 6 firms after 1,511 hectares
#         burn in Kalimantan" (a domestic environmental penalty) against "no
#         new SANCTIONS designations ... or secondary sanctions"; "Second EPR
#         site authorised for site preparations" (a CIVILIAN nuclear power
#         station) against "no confirmed capability, deployment, exercise,
#         PROCUREMENT, or doctrine change".
#
#   EXPECTED SHIFT — hard-fail count FALLS on compositions, mean faithfulness
#   is UNCHANGED by the guard and may rise slightly from the rubric. The guard
#   is a DEMOTION, not an acquittal: the claim still fails, only the severity
#   moves, so ``faithfulness_score`` cannot move because of V-J1 at all — what
#   moves is the hard/soft split the panels gate on. Read
#   ``hardfail_demoted_hedged_conflict`` and
#   ``absence_slice_route_excluded_hedged_conflict`` beside each other: the
#   first is the generic route, the second the absence route, and the two are
#   deliberately separate counters because they are separate levers. V-J2 is
#   the only part that can move the SCORE (a stage-2 verdict flipping
#   contradicted -> supported withdraws a failure outright), and it is a prompt
#   change on one route, so it cannot be told from ordinary judge variance
#   except across this boundary. Splitting here is what makes that separable.
#   BEFORE/AFTER: split ``absence_slice_contradicted`` and
#   ``judge_contradicted`` on this stamp vs 2026-08-28/1 and read the two new
#   counters against them; a demotion count that stays at zero while the hard
#   share holds means the template stopped appearing, not that the guard works.
#
# 2026-08-29/1 — LRF, THE LENGTH-RESPONSE FLATTENING. A MEASUREMENT DEFINITION
#   CHANGE, and the doctrine at the top of this file applies to it in full: the
#   population moves because the DENOMINATOR was wrong, not because findings got
#   worse. Nothing here may be reported as a quality regression.
#
#   STAMP COLLISION, recorded so it stays legible: a HELD branch (verify-extract
#   -n1 phase 2, not merging) also claimed ``2026-08-29/1``. This train takes the
#   stamp; if that branch is ever revived it RE-STAMPS. Two populations must
#   never share a key, and the one that shipped owns it.
#
#   THE DEFECT (planning/CAMPAIGN_2026-08-29/PREMISE_GRADING_LOOP.md A-5b and
#   Decision 6B). The gate score peaked at exactly four checkable claims and
#   declined monotonically thereafter (n=1,898 judged rows), so a desk that said
#   MORE checkable things scored WORSE — while the external rounds independently
#   measured ``under_hedged`` 42 against ``over_hedged`` 1, 43/43 band misses
#   BELOW the reference and 21/21 honesty cells under-read. Those are the same
#   fact from two ends: the measurement paid the product to say less, and every
#   prompt and rubric in this system is written by someone reading it.
#
#   WHAT SHIPS. Three rungs of the floor's ``_is_fact_asserting`` ladder granted
#   a WHOLE-SPAN exemption on the strength of a PREFIX, a LABEL or a SUBSTRING
#   and never read what else the span asserted — ``_SYNTHESIS_PREFIXES`` (5,026
#   spans, 14.0% of a 35,951-span live census), ``is_assessment_scaffold`` (the
#   labelled spelling of the same thing, the rung Q-1 corrected for its sibling
#   ``is_labeled_scaffold`` and left alone here), and ``_is_absence_claim``
#   (3,308 spans, 9.2%, a substring test that excused any sentence CARRYING an
#   absence subclause). All three now EARN their exemption from the remainder,
#   which is Q-1's correction applied one layer out: an exemption belongs to the
#   CLAUSE that earns it, never to the span that contains it. The absence rung
#   additionally requires the idiom to GOVERN its clause (positional, via V-B's
#   own ``_first_absence_marker_pos``), so "…continue to highlight government
#   praise of wartime management and the absence of any reported elite purges"
#   stops being a non-claim.
#
#   WHAT DOES NOT SHIP, deliberately. The absence GRAMMAR is untouched, so
#   ``_claim_kind``, V-B and ``composition_integrity`` are byte-identical and the
#   route cannot drift from the grammar. ``no_citation``'s MEANING and the
#   judge's markerless licence are untouched — that standard conflict is review
#   1C/D3 and moving it here would confound two definitions under one stamp. No
#   verdict, reason, counter or prompt block is added, renamed or reworded.
#
#   EXPECTED SHIFT — measured by replay over 6,000 live findings, base vs branch,
#   under one harness:
#     * STRICTLY ADDITIVE. 54,610 segmented spans, 8,866 ADDED to the denominator
#       and ZERO removed. No claim that is counted today can stop being counted,
#       so the direction needs no hedge.
#     * The FLOOR arm (``unsampled`` + ``deterministic`` — 77% of the live
#       population, and the only arm these rungs govern) moves DOWN on
#       claim-dense prose and UP where content had been exempted so hard the pass
#       could not measure at all: 3,085 findings down (mean -0.108), 1,508 up
#       (mean +0.175), 1,407 unchanged; fleet mean published gate 0.571 -> 0.556.
#       ``score_state='unassessable'`` falls 1,011 -> 173 (-83%): 838 findings
#       were unassessable ONLY because their content had been exempted out.
#     * The LENGTH RESPONSE flattens. Published gate spread across the claim-count
#       buckets 0.452 -> 0.304 on the floor arm (-33%), 0.486 -> 0.346 pooled
#       (-29%). The short end rises (+0.138 at one claim), every longer bucket
#       falls, which is the shape of a denominator being repaired.
#     * The JUDGED arm is BYTE-IDENTICAL — 1,394 replayed rows, zero moved. This
#       is expected and is the finding: the judge already grades every span the
#       floor exempts (``_is_judgeable_claim``, the H1 rule), so these rungs never
#       shrank ITS denominator. The peak-at-4 the review measured is the judged
#       arm's, and what remains of it after this train is the ``no_citation``
#       standard conflict (A-4/D3), not an exemption leak. Say so; do not claim
#       this train flattened a curve it did not touch.
#     * ONE ROUTING CHANGE, unscored on purpose: ``_is_null_result_finding``
#       gates the M14 whole-finding survey rubric on the same leaky positive-claim
#       count, so the route flips for 925 of 6,000 findings (1,114 -> 189). The
#       live data show NO score premium for that route conditional on claim count
#       (0.871 vs 0.873 at 3-4 claims), so this is justified as a definition
#       repair and is NOT expected to move the mean. It cannot be replayed — the
#       rubric changes what a live judge would answer — so it is stated as a
#       count, not a number.
#   BEFORE/AFTER: split on this stamp vs 2026-08-28/1 and read
#   ``checkable_claims`` and the ``unassessable_*`` counters FIRST. A fleet mean
#   that falls while ``checkable_claims`` rises ~27% on the floor arm is the
#   denominator being repaired. Pooling across this boundary compares a
#   population whose non-claims were laundered against one whose were not, and it
#   would report an instrument correction as findings getting worse.
#
#   SAME STAMP, SECOND TRAIN — V-J1 ACTIVATION (they deploy together, one bump;
#   the H1+H2 precedent). V-J1 shipped under 2026-08-28/1 and has NEVER FIRED:
#   0 of 573 graded claims on that stamp carry ``hardfail_demoted_hedged_conflict``
#   or ``absence_slice_route_excluded_hedged_conflict``, and R3 traced why.
#
#   THE DEFECT is one character. ``_HEDGED_WEAK_MARKER_RE`` /
#   ``_HEDGED_STRONG_SIDE_RE`` are spelled ``weakly[-\s]+supported`` /
#   ``well[-\s]+supported`` in ASCII, while 58.2% of graded claims (41.3% of all
#   segmented spans) carry U+2011 NON-BREAKING HYPHEN — and
#   ``hedged_conflict_disclosure`` did not apply ``_UNICODE_HYPHENS``, the
#   module's OWN fold, which was sitting 430 lines below it filed as a local
#   helper of the enumeration screen. The whole V-J1 test suite passed because
#   its fixtures are hand-typed with ASCII hyphens and the producers are not: a
#   regression suite written in the author's characters cannot see a defect that
#   lives in the producer's. The fold is now a declared shared primitive at the
#   top of the module, beside ``_CITATION_MARKER_STRIP_RE``, so the next matcher
#   added there finds it before it ships.
#
#   EXPECTED SHIFT — V-J1 STARTS COUNTING, AND THE SCORE CANNOT MOVE FROM IT.
#   R3's archived live-fire census (planning/PROOF_ROUND_2026-08-29/mech/
#   hedged_conflict_livefire.json) replays seven live specimens: 0/7 fire as
#   shipped, 4/7 with the fold, and all seven reproduce exactly in the branch's
#   regression suite. So hedged-conflict HARD fails begin moving to SOFT and the
#   two demotion counters leave zero for the first time. ``faithfulness_score``
#   cannot move from this arm BY CONSTRUCTION — V-J1 is a DEMOTION, not an
#   acquittal (2026-08-28/1's own words: "the claim still fails, only the
#   severity moves"), so what moves is the hard/soft split the panels gate on.
#   The translate is a 1:1 character map, so every offset the guard computes is
#   unchanged and the ASCII population is byte-identical — this ADDS a
#   population rather than moving one.
#   BEFORE/AFTER: read ``hardfail_demoted_hedged_conflict`` (generic route) and
#   ``absence_slice_route_excluded_hedged_conflict`` (absence route) on this
#   stamp against their zeros on 2026-08-28/1, beside the hard share. A hard
#   share that does not fall while the counters rise means the guard is firing on
#   claims that were already soft, which is the thing to look at first.
#
#   AUDIT FINDING, NOT SHIPPED, sized so it can be decided on its own evidence:
#   ``_absence_content_terms`` — the V-B stage-1 screen — is the SECOND site that
#   skips the fold, and it is the site ``_UNICODE_HYPHENS``'s own docstring was
#   written for ("an unfolded U+2011 silently splits a compound into two terms
#   and manufactures overlap that is not there"). Measured over 7,556 live
#   absence claims, folding changes the term set on 4,272 of them (56.5%):
#   "energy‑security" screens as {energy, security} instead of {energy-security},
#   so it matches any slice title mentioning security. The direction of the fix
#   is fewer spurious candidates and therefore fewer ``absence_slice_contradicted``
#   HARD fails — which means it CAN withdraw failures and CAN move
#   ``faithfulness_score``. That is a different expected shift from the one this
#   stamp declares above, on a different arm, and shipping it here would make the
#   two indistinguishable. It needs its own stamp.
#
# 2026-08-30/1 — THE [N+1] PRIOR-READ TRANSPARENCY train (task #62) SHIPPED
#   WITH ITS CONSUMER REPAIR (task #78). The ONLY entry in this lineage whose
#   expected shift is NONE, and the only one that has to begin by retracting
#   the number that commissioned it.
#
#   STAMP REASSIGNMENT, recorded rather than silently corrected. This train was
#   built and proven on branch `verify-extract-n1` claiming `2026-08-29/1`, and
#   was then HELD — the number that commissioned it had been falsified, and the
#   train's own author found two live consumers that misread the convention and
#   that this train does not fix. `2026-08-29/1` was subsequently REASSIGNED to
#   the in-flight length-response train, which landed first. This entry is the
#   same train re-stamped to `2026-08-30/1` and now shipping WITH the consumer
#   repair, which is the disposition its own report recommended: the two keys
#   are exactly what a correct repair keys on, so shipping the discriminator a
#   train ahead of its consumer was the thing worth avoiding. Where this entry
#   says BEFORE/AFTER against `2026-08-28/1` the comparison is unchanged in
#   substance — the length train's stamp sits between the two and moves its own
#   population, so read the prior-read cut against whichever boundary is
#   adjacent in the data.
#
#   WHAT WAS BELIEVED. A unit's DESK GROUNDING blocks take the ordinals just
#   past its signal slice, so a desk self-cites its own PRIOR READ as ``[N+1]``.
#   The 2026-08-27 DQ sweep called that a 53.6% citation RED — 15 of 28 sampled
#   findings carrying a marker that "does not resolve", 15 of the 16 bad markers
#   exactly ``N = n_refs + 1``, every one on a "no change" sentence.
#
#   WHAT IS TRUE. The 2026-08-29 sweep v2 falsified it
#   (planning/CAMPAIGN_2026-08-29/DQ_SWEEP_V2.md §4). The baseline resolved
#   markers against ``analyst_traces.input_row_refs`` — ``uuid[] NOT NULL
#   DEFAULT '{}'``, a flat array of consumed SUBSTRATE row ids that carries no
#   ``ref_kind`` and by construction cannot hold a grounding block. The method
#   therefore flagged every legitimate grounding citation, and that is the whole
#   of the 53.6%. Re-measured against the real ground truth
#   (``analyst_outputs.data->'data'->'citations'``), 48h, full population:
#   0 of 6,556 markers unresolved, 0 of 1,079 marker-carrying findings affected.
#   All 847 markers the old method flags resolve to registered grounding kinds
#   (prior_read 502, window_ledger 219, situation_register 47, finding 41,
#   signal 32, desk_baseline 5, open_questions 1). Both named specimens are
#   correct citations. Genuinely broken: ZERO. ``_grounding_ordinals`` has
#   admitted these as resolved evidence since 2026-07-31, a month before the
#   baseline sweep ran.
#
#   WHAT THIS STAMP THEREFORE COVERS is legibility and nothing else — the prose
#   marker ``[121]`` is spelled identically whether it names a signal or the
#   desk's own last read, and only a consumer that joins to the citation list
#   AND knows ``kinds.GROUNDING_REF_KINDS`` can tell them apart. Three arms,
#   moved together because a spelling only one of them knows is worse than none:
#     * ``citation_markers._PRIOR_READ_REF_RE`` — ``(prior read ref N)`` becomes
#       a recognized marker syntax, rewritten to ``[N]`` before grading. Same
#       "can only ADD a resolution" posture as every other rule in that module.
#       The DEFUSED ``[prior:N]`` form is deliberately NOT matched.
#     * ``inline_target`` applies the SAME compiled rule at write time —
#       imported, not mirrored, so this syntax has one definition from birth.
#     * ``unit_grounding`` stamps ``marker_class='desk_grounding'`` and
#       ``resolves_against='data.citations'`` on every grounding citation, and
#       names the licensed spelling in the prior-read block header and the unit
#       grounding clause.
#
#   AND THE FOURTH ARM, which is why the stamp is spent at all (task #78): the
#   two consumers that misread the convention now key on those two marks.
#   ``export_api._stored_citations`` kept only entries carrying a ``signal_id``
#   — a key that the composition sub-claim ref and all five grounding kinds
#   lack by design — and additionally read ``data->'citations'`` when the
#   column nests at ``data->'data'->'citations'``, so EVERY exported finding
#   shipped an empty ``### Citations`` section and printed "no resolved
#   citations recorded on this row" whatever it cited (measured: old reader 0
#   citations on the real column shape, new reader 4). The v3 UI's
#   ``citationsModel`` special-cased one of the five kinds, dropped the id-less
#   blocks into amber "Unresolved citation" chips over evidence this plane had
#   scored SUPPORTED, and typed ``prior_read`` as a signal with a dead drill.
#   Both now classify on ``marker_class``/``resolves_against`` first, falling
#   back to ``kinds.GROUNDING_REF_KINDS`` for the pre-stamp population — which
#   is the whole population until this stamp lands, so the fallback is the load-
#   bearing path, not a courtesy. NONE of this reaches a verdict: no consumer of
#   these repairs is in the grading path.
#
#   EXPECTED SHIFT — NONE, and that claim is the deliverable rather than a hope.
#   No reason string is added, renamed or reclassified; the fail-class table is
#   untouched. PROVEN on a 50-finding frozen corpus (325 citations, 23 of them
#   ``prior_read``, 201 graded claims) replayed through the real deterministic
#   pass: per-claim verdicts BYTE-IDENTICAL, 201/201, on both arms — the marker
#   rules (inert: 0 matches across 589 corpus texts, since no existing finding
#   contains the new form) and the citation-record change (27 grounding
#   citations re-stamped with the new keys, verdicts unmoved).
#
#   THE ONE THING THE PROOF CANNOT COVER, said plainly: the prior-read block's
#   rendered header gains a line, and that block is ``evidence_text`` for its
#   citation, so the LLM judge's evidence map changes by one line on findings
#   that cite a prior read. The deterministic path reads that field only for
#   PRESENCE, so it cannot move a floor verdict — but a judge is not a
#   deterministic function of its prompt, and this stamp is what keeps that
#   perturbation from being read as a quality movement. BEFORE/AFTER: on
#   prior-read-citing findings only, the fail-class mix on this stamp vs
#   2026-08-28/1 should be FLAT. A visible move there is the render line, not
#   the fleet, and is the signal to revert the render half.
# ---------------------------------------------------------------------------

#: Stamped into every faithfulness critique's ``data.verification`` block.
#: Re-exported by ``verify`` (the historical import surface).
JUDGE_PIPELINE_VERSION = "2026-08-30/1"


# ===========================================================================
# THE STRUCTURED LINEAGE (2026-08-29) — what the prose above says, in a form a
# READER can partition on.
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
}

#: The stamps in lineage order, oldest first. ONE source of truth — derived from
#: the registry rather than repeated, so the two can never drift apart.
STAMP_LINEAGE: tuple[str, ...] = tuple(STAMP_EXPECTED_SHIFTS)


def expected_shift(stamp: str, metric_family: str) -> str:
    """The shift ``stamp`` declares for ``metric_family`` across the boundary it
    opens against its immediate predecessor.

    FAILS SAFE: an unregistered stamp or an unknown family is :data:`SHIFT_MOVES`
    — never poolable. A stamp bumped without a registry entry therefore degrades
    to exactly today's behaviour (partition on the single current stamp) rather
    than silently widening a population, and the drift guard in
    ``tests/data_pkg/test_verify_pipeline_version.py`` fails loudly at the same
    time so it does not stay that way.
    """
    return STAMP_EXPECTED_SHIFTS.get(stamp, {}).get(metric_family, SHIFT_MOVES)


def poolable_stamps(stamp: str, metric_family: str) -> tuple[str, ...]:
    """The stamps that may be POOLED with ``stamp`` for ``metric_family``.

    Walks the lineage outward from ``stamp`` in both directions across
    CONSECUTIVE boundaries, admitting a neighbour only while the boundary
    between them declares :data:`SHIFT_NONE` for this family. The relation is
    transitive by construction (a run of consecutive ``'none'`` boundaries is
    one population for this metric and nothing else joins it), and any
    ``'moves'`` boundary is HARD — the walk stops there and never steps over it.

    Returns a tuple in lineage order, oldest first, ALWAYS containing ``stamp``
    itself. An unregistered stamp returns ``(stamp,)`` — pooling with nothing,
    which is the pre-2026-08-29 behaviour and the safe direction.

    This is deliberately NOT a "distance" or "similarity" rule. Two stamps pool
    only when every boundary between them carries a written declaration that
    this family cannot move; there is no threshold to tune and no way to widen a
    population except by writing one more such declaration in the lineage.
    """
    if stamp not in STAMP_EXPECTED_SHIFTS:
        return (stamp,)
    idx = STAMP_LINEAGE.index(stamp)
    lo = idx
    # Walk BACK: the boundary entering STAMP_LINEAGE[lo] declaring 'none' means
    # lo-1 and lo are one population for this family.
    while lo > 0 and expected_shift(STAMP_LINEAGE[lo], metric_family) == SHIFT_NONE:
        lo -= 1
    hi = idx
    # Walk FORWARD: the boundary entering the NEXT stamp is the one that decides
    # whether it joins.
    while (
        hi + 1 < len(STAMP_LINEAGE)
        and expected_shift(STAMP_LINEAGE[hi + 1], metric_family) == SHIFT_NONE
    ):
        hi += 1
    return STAMP_LINEAGE[lo : hi + 1]


__all__ = [
    "JUDGE_PIPELINE_VERSION",
    "METRIC_FAITHFULNESS_SCORE",
    "METRIC_FAMILIES",
    "METRIC_REASON_CENSUS",
    "METRIC_SEVERITY_SPLIT",
    "SHIFT_MOVES",
    "SHIFT_NONE",
    "STAMP_EXPECTED_SHIFTS",
    "STAMP_LINEAGE",
    "expected_shift",
    "poolable_stamps",
]
