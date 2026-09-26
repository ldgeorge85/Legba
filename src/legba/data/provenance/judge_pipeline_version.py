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
#
# 2026-09-03/1 — D-3, THE ASSEMBLY ARMS + THE SHARED FOLD (the composition
#   demotion program, D-1 §3). Two changes ride this one bump, and they are
#   separable in the counters but not in the population.
#
#   (i) FIVE ARMS ENTER, FOUR OF THEM DETERMINISTIC — and the fifth does not
#   ship here. ``assembly_arms`` audits the ``assembly.v1`` payload D-2 emits:
#   QUOTE FIDELITY (every span resolves into its origin head's captured body by
#   sha, byte offsets and containment), SCOPE PRESERVATION (a span may not cut
#   away the collection denominator its source sentence carried, and may not
#   declare one its source sentence lacks), ATTRIBUTION EQUALITY (desk / target
#   / date / head id equal the captured origin record) and SELECTION HONESTY
#   (the coverage ledger accounts for every roster unit exactly once; the drop
#   ledger's arithmetic closes, its why-classes are in the closed enum, and the
#   not-selected list sits strictly below the carried prefix). Sixteen new
#   reason codes, ALL HARD. ARM 5 — TENSION HONESTY, the semi-deterministic one
#   — is NOT in this train: it needs the D-6 Assessment's spine to exist before
#   its two evidence maps can be separated, and shipping half of it would put a
#   soft class in the field with no population to calibrate on.
#
#   (ii) THE COMPOSITION TIER'S FAITHFULNESS NUMBER CHANGES MEANING, because
#   the GRADED BODY CHANGED SHAPE. Under the assembly a composition no longer
#   writes prose about its inputs; it quotes them, and the judge's evidence map
#   becomes byte-identical to the claim. The number that comes out is therefore
#   a different measurement wearing the same name — mechanically near 1.0, and
#   emphatically NOT an improvement. **POOLING ACROSS THIS BOUNDARY IS INVALID
#   FOR ``country_composition``, ``world_assessor`` AND ``escalation_composition``.**
#   Any reader comparing composition faithfulness before and after this stamp is
#   comparing a prose grade to a construction check. Split, or say nothing.
#
#   (iii) A NEW GRADED POPULATION ENTERS WITH NO HISTORY: ``world_assessment``,
#   the D-6 interpretive channel, is graded on FIDELITY TO ITS OWN SPINE as its
#   own population and must never be pooled with the assembly's ~1.0. Its badge
#   shows the R3 lineage number until its own live number exists, and never a
#   bare number without its population label.
#
#   (iv) ``_absence_content_terms`` IS RE-POINTED AT THE SHARED FOLD, and this
#   is the arm of the train that moves the EXISTING fleet. The census at tip
#   found at least SEVEN hand-rolled normalisers in ``data/provenance/`` using
#   THREE different dash tables, none importing another; ``verify.py`` imports
#   ``unicodedata`` and never uses it. ``text_fold.normalize_for_match`` is the
#   ruled union — NFKC + the punctuation table (U+2010–U+2015, U+2212, the five
#   quote forms, the space forms) + U+00AD removal + ``.casefold()`` — and
#   ``_absence_content_terms`` now calls it. **This changes the term set on a
#   measured 4,272 of 7,562 absence claims (56.5%)**: an ASCII-only tokenizer
#   split ``energy‑security`` written with U+2011 into ``{energy, security}``
#   and now yields the compound the desk actually wrote. It also closes a
#   sharper defect — the function returned DIFFERENT TERM SETS DEPENDING ON THE
#   CALLER (V-B and the six ``judge_quote_rules`` sites passed unfolded text,
#   ``denied_enumeration`` and ``composition_integrity`` passed folded), so one
#   screen had two answers decided by who called it. NO OTHER old comparator is
#   re-pointed in this train: re-pointing one is a graded change and must be
#   sized and stamped, never refactored in (F-13).
#
#   THE RE-POINT RE-MEASURED LIVE, because a number quoted from an archive is
#   not a number checked: over a 70-row live corpus (30 compositions + 40 unit
#   desk heads, 1,851 distinct segmented claims) the term set moves on 881
#   claims (47.6%) overall and on **186 of 357 ABSENCE claims (52.1%)** — the
#   population the screen actually serves, and within four points of the
#   archived 56.5%.
#
#   ONE DIRECTION OF THE RE-POINT THAT COULD SURPRISE A READER, said here rather
#   than found later: the fold maps the EM DASH and the HORIZONTAL BAR to ``-``
#   like every other dash, so a clause break written ``Russia—closing the Bonn
#   consulate`` tokenizes as the compound ``russia-closing`` instead of the two
#   terms ``russia`` and ``closing``. This is NOT new behaviour and NOT a
#   decision this train took: ``composition_integrity._PUNCT_FOLD`` has mapped
#   the em dash that way since H2, and every ``composition_integrity`` call site
#   already handed folded text to this screen. What changes is that the OTHER
#   half of the callers now get the same answer. Inventing a second, divergent
#   dash table to special-case it is precisely the disease the census above
#   diagnoses, so it is documented rather than forked; if the compound tokens
#   prove to cost screens on the V-B route, the fix is one table for everyone.
#
#   (iv-b) ``buried_lead_salience`` IS DISABLED FOR ASSEMBLY-REGIME ROWS —
#   **disabled, not re-pointed**, and the choice belongs in this entry because
#   the fold writes a graded soft failure. (The spec named it at
#   ``judge_input_checks.py:113-140``; the live function is
#   ``fold_salience_lead``, and ``_SALIENCE_LEAD_GAP`` is not in that module at
#   all — it is ``meta_findings_synthesizer.py:1105``, feeding the
#   ``data.eval.salience_check`` this fold merely consumes.)
#
#   The check compares the lead's magnitude to the top input's on
#   ``max_salience()`` — a max-pool measured at sd 0.024 across the composition
#   tier, whose world burial guard is 58 pass / 0 fail with a maximum observed
#   gap of 0.050 against a 0.300 threshold. On an assembled row every assumption
#   under it is false: the KEY is repaired (``cited_mass.v1``, sd 1.673 on the
#   live candidate pool against the max-pool's 0.024), the LEAD is PLURAL
#   (``earned_single`` | ``co_leads`` | ``none`` — VOICE §4.5.7: the check
#   "presupposes one correct lead and will fight a plural one"), and the ORDERING
#   IS THE PAYLOAD, so an assembled read cannot bury its own lead and the verdict
#   is already published with its arithmetic in ``assembly.lead.test``.
#
#   RE-POINTING IT WAS REJECTED, deliberately: aiming it at ``cited_mass.v1``
#   means inventing a new gap threshold on a key with ~70x the variance and no
#   calibration behind it — a NEW INSTRUMENT wearing an old reason code, which is
#   the move the stamp discipline exists to prevent. A plural-lead check is its
#   own train with its own bar, once the assembled population has a measured
#   distribution. THE SUPPRESSION IS COUNTED
#   (``salience_lead_suppressed_assembly``), so "how often did the dead key
#   disagree with the repaired one" is a number rather than a silence, and
#   LEGACY-REGIME rows are untouched — the check runs there exactly as today.
#   R2's ``unsurfaced_input_contradiction`` half is NOT withdrawn: D-3 does not
#   ship ARM 5, so pulling it would leave that class with no grader until D-6.
#
#   (v) WHAT WAS ACTUALLY BUILT FOR THE HARD GATE — and the honest answer is
#   THE ARMS, NOT THE GATE. D-1 §3.6 priced "promote to HARD" at six pieces of
#   new machinery, the fourth of which — a withhold / do-not-publish seam —
#   DOES NOT EXIST ANYWHERE IN THE TREE. Ratified F-12: the real enforcement
#   stays at ASSEMBLE time, where a publish decision already lives, and verify's
#   arms are INDEPENDENT AUDITORS that run post-persist like every other check
#   here. So this train ships the arms COUNTED, HARD-LABELLED and LOUD (every
#   fire logs at ERROR naming the constructor as the defect), and it ships NO
#   score cap, NO persisted ``hard_fail_count``, NO fourth ``gate_score()`` cap
#   and NO suppression seam. **The gate is D-3b.** This paragraph is where that
#   is said, per §3.6's own instruction not to ship labels and call it a gate.
#
#   AND WHAT THIS TRAIN DOES NOT BUILD, said out loud because the campaign notes
#   assume otherwise: the D3/#71 CLAIM-CLASS SPLIT. It does not exist — zero
#   occurrences of ``claim_class`` in ``src/``, ``tests/`` or ``planning/`` —
#   and the ruling (C, operator 2026-08-29) is design-first, own stamp,
#   sequenced after #69. D-3 adds ONE new branch (``assembly``, version
#   ``assembly_arms.v1``) for its own deterministic instrument and leaves
#   ``no_citation`` and ``_claim_kind``'s five prose kinds untouched.
#
#   DIRECTION OF THE EXPECTED SHIFT: ALL THREE FAMILIES MOVE, and for two
#   different reasons at once, which is precisely why pooling would lie.
#     * ``faithfulness_score`` — MOVES. On the assembly population it rises
#       mechanically toward 1.0 because the graded body became a quotation;
#       on the ABSENCE population it moves in BOTH directions as the re-pointed
#       term sets change which slice titles screen in. Two arms, opposite signs,
#       one number. Predicted: composition faithfulness → ~1.0 (a construction
#       invariant, NOT a quality gain); ``attribution_ungrounded_quote`` gets
#       its first non-zero population in its life (it has fired 0 times ever,
#       because 82% of composition bodies contain no quotation mark at all);
#       ``absence_scope_laundered`` → 0, superseded by ARM 2;
#       ``metadata_mismatch`` → 0, because the coverage ledger is now PERSISTED
#       and the prose that used to disagree with it no longer exists;
#       ``direction_conflict`` unchanged in RATE and changed in ROLE.
#     * ``severity_split`` — MOVES. Sixteen new HARD classes enter the table,
#       and the absence re-point can move a claim across the V-B hard boundary
#       in either direction. ``buried_lead_salience`` (soft) falls to zero on the
#       assembly population and is unchanged on the legacy one.
#     * ``reason_census`` — MOVES. Sixteen new reasons, thirty-six new counters,
#       and a changed claim SET on the absence route.
#
#   BEFORE/AFTER: on the ASSEMBLY population, quote fidelity is 1.000 over all
#   spans or the constructor is broken — that is an INVARIANT, not a threshold,
#   and 0.98 is not a pass (D-1 §5.3 G2). On the LEGACY population, the arms are
#   INERT BY CONSTRUCTION and this half is proven, not asserted: a 70-row live
#   replay (30 compositions carrying 30 assembly-less payloads, 40 unit desk
#   heads; 477 checkable claims, 206 spans) is **70/70 BYTE-IDENTICAL** to the
#   base tree on the deterministic pass — same score, same claim set, same
#   reason census, zero ``assembly_*`` counters, zero ``branch_scores`` keys.
#   The floor-only replay CANNOT reach the re-pointed screen (the V-B slice
#   route needs a DB connection and the six ``judge_quote_rules`` sites need a
#   judge), which is exactly why the re-point is sized at the FUNCTION level
#   above instead of being reported as "no change observed".
#
#   The absence re-point is the ONLY arm of this train that touches an existing
#   verdict, and it is the one the 52.1% / 56.5% numbers are attached to; read
#   it against the absence-carrying population
#   ONLY, and read the assembly arms against ``regime='assembly'`` rows ONLY —
#   ``data.data.assembly.regime`` is stamped on every composition row from D-2's
#   merge precisely so this stamp can be re-split retroactively if the cutover
#   and the bump ever come apart.
# ---------------------------------------------------------------------------

# 2026-09-05/1 — THE VERIFY-REGIME FIX. One train, three defects, all of them in
#   how this plane GRADES an assembly-regime row, and none of them in the reads.
#   The demotion flipped at 04:16Z on 2026-09-05 and its first live cycle proved
#   the core promise — quote fidelity 1.000 on every audited row, zero real quote
#   or scope-truncation failures — while the instrument reading those rows said
#   0.35. The reads were right and the grader was wrong, in three ways.
#
#   (i) ARM 2's ``scope_widened`` VERIFIES THE DECLARED TOKEN WITH THE PREDICATE
#   THAT OWNS IT, instead of with a substring match. ``spans[].scope_tokens`` is
#   an IDENTIFIER list: D-2 writes the single token ``"collection_denominator"``
#   (``assembly_spans.py:286``, pinned by ``test_composition_assembly_d2.py:189``)
#   when ``has_collection_denominator_scope`` fires on the span's source
#   sentence. D-3's SCOPE_WIDENED tested it with ``fold_contains(sentence, tok)``
#   — a LITERAL substring test, which looks for the word
#   ``collection_denominator`` inside desk prose, where it never appears — so the
#   arm fired on EVERY collection-scoped span in the fleet. Its own docstring
#   named the predicate as the truthmaker "reused VERBATIM" and its sibling
#   SCOPE_TRUNCATED, six lines below, used it correctly; only the widened
#   direction did not. D-3's fixture declared the PHRASE ``["this desk"]``, which
#   a substring test passes, so each side pinned its own assumption and the pair
#   was wrong. MEASURED: 26 fires across the 35 assembly-regime critiques of
#   2026-09-05, every one of them false, on reads whose quote fidelity was 1.000;
#   after the fix, ZERO. A declared token this arm does not recognise is now
#   COUNTED (``assembly_scope_token_unknown``) and never charged — an unrecognised
#   identifier is the arm's ignorance, not the payload's defect, and this class
#   has now false-positived four times.
#
#   (ii) FOR AN ASSEMBLY-REGIME ROW THE ARMS ARE THE GRADER OF THE HEADLINE.
#   ``faithfulness_score`` is ``supported / checkable`` over the claims the LLM
#   judge graded, partitioned into ``absence`` / ``synthesis`` /
#   ``citation_support``. Those branches ask ONE question — does this AUTHORED
#   sentence follow from the evidence it cites? — and an assembly row has no
#   authored sentence to ask it of: every substantive line is a span ARM 1 proved
#   byte-identical to its origin, and every other line is machine-printed from the
#   ledger ARM 3 and ARM 4 audit. The judge was handed a coverage row
#   ("leadership_transition: in basis, 10.5h old"), a generated attribution line
#   and a desk's own quoted BLUF, and asked whether they follow from the
#   sub-claims. It answered 0.18 on rows whose ``branch_scores["assembly"]`` read
#   1.000 in the same payload.
#
#   THAT NUMBER WAS LOAD-BEARING, NOT COSMETIC. ``LEGBA_COMPOSITION_VERIFY_FLOOR``
#   is 0.50 live and every composition tier is verify-floored on its inputs
#   through ``LEAST(confidence, overall_score) >= floor``. On 2026-09-05 that
#   floored **31 of 32 byte-correct country assemblies OUT of the world read**,
#   which published one block of a possible thirty-four. The wager's centrepiece
#   product was degenerate on day one because of an arithmetic artefact.
#
#   So ``assembly_arms.regrade_to_arms`` rewrites the headline of an
#   assembly-regime row to the ARMS' own ratio: one check per quoted span, one
#   per attributed block, one per ledger identity. Nothing is deleted — every
#   judge verdict, every deterministic mark and every branch sub-score stays on
#   the row in ``branch_scores``, ``claim_verdicts`` and ``unsupported_spans``,
#   where a reader can see what each grader said. They are simply no longer the
#   score of a read that authored nothing.
#
#   WHY DEFERRING TO THE ARMS AND NOT SKIPPING THE LEGACY BRANCHES, since the
#   defect statement admits both. Not sending those claims to the judge would
#   leave the DETERMINISTIC FLOOR's denominator behind — it counts every
#   fact-asserting span before any judge runs, and ``_maybe_llm_judge`` floors the
#   published count at it (``max(floor.checkable_claims - …, effective_checkable)``)
#   — so the same denominator would have carried a smaller numerator and the rows
#   would have scored WORSE. The regrade is the change that is true at the one
#   point where the whole tally is known, and it is the only one a legacy row
#   cannot reach.
#
#   THE T7 CEILING SURVIVES, deliberately. ``confidence_ceiling`` is untouched, so
#   ``overall_score = min(arms, ceiling)`` still caps an assembly at its strongest
#   INDEPENDENT cited sub-claim: on the 35 replayed rows the new overalls run
#   0.5714 / 0.8528 / 0.9200 (min / mean / max) and every one of them is the
#   CEILING, not the arms. A read assembled perfectly out of weak inputs is still
#   a weak read. AND IT IS STILL NOT A GATE: D-1 §3.6's forced ``0.0`` on a firing
#   arm remains D-3b and remains unbuilt, so a construction bug reads (say) 0.90
#   here rather than zero — the same labels-only posture the rest of this plane
#   holds, said out loud so nobody reads the regrade as the gate arriving.
#
#   THE GRADER GATE IS NARROWER THAN THE AUDIT GATE, and the difference is D-5.
#   ``is_assembly`` (audit) admits ``regime: "rollup"`` — it is an ``assembly.v1``
#   stamp — and a rollup carries no blocks and no spans, so regrading it would
#   publish 6/6 = 1.000 for a row nothing checked. ``is_grader_of_record``
#   requires the explicit word ``"assembly"``. (Today a rollup never reaches this
#   pass at all: verified live on 2026-09-05, where the one ``region_composition``
#   critique carried ``structural_verify: true`` and no faithfulness block.)
#
#   (iii) THE ASSESSMENT'S EVIDENCE MAP IS THE RECORD, NOT A THIRD OF IT. The
#   first ``world_assessment`` the channel ever published (critique ffaed9fa,
#   2026-09-05 12:17Z) graded **0.00** — seven claims, none supported — and six of
#   the seven were faithful. The channel's fidelity-to-spine IS
#   ``citation_support`` over the spine's own words (D-1 §2.5 / §3.5), and that
#   part was wired correctly and ran: the live row carries ``citation_support``
#   0.0 over 6 checkable with ``branch_versions.citsupp.v5``, minted one citation
#   per cited spine ordinal with ``ref_id`` = the spine row and ``evidence_text`` =
#   that block's quoted span, exactly as specified. The defect is what the map
#   OMITTED. ``build_assessment_prompt`` hands the voice THREE things (D-6 §1.1) —
#   the header, the rendered record, and THE RECORD'S OWN ARITHMETIC, introduced
#   to it as *"facts about the record, handed to you. You may quote them"* — and
#   the map carried only the second. So "the ledger notes six reads sit below the
#   verification floor" and "top-share of 1.000", both quoted accurately off the
#   record's own page, resolved against nothing, and the only verdict available to
#   a judge grading a claim whose truthmaker is absent from its evidence is
#   *unsupported*. The channel was charged for reading the page it was given.
#   ``build_assessment_citations`` now appends the same bytes
#   ``assessment_prompts.record_arithmetic`` renders to every cited ordinal's
#   ``evidence_text``, under a labelled rule so the block's quoted span stays
#   recoverable as the head of the entry. MEASURED on the live row: the truthmaker
#   of 1 of 7 claims was in the old map; 6 of 7 are in the new one. The seventh
#   stays unsupportable and correctly so — "the seven carried sub-reads" is the
#   Assessment reading a count out of a QUOTED COUNTRY BLOCK as if it were the
#   record's own, which is a real fidelity failure and is now the only one.
#
#   THIS IS NOT A WIDENING OF THE FENCE. Every number added is already ON the
#   spine row, in ``payload``, which is still the only argument either the prompt
#   builder or the citation builder has (D-6 §1's assertion 2, unchanged and still
#   AST-pinned). Nothing new is read.
#
#   (iv) A NEW GRADED POPULATION ENTERS WITH NO PRIOR HISTORY. Restated here
#   because it first became TRUE at this stamp rather than the last one:
#   ``world_assessment`` transitioned draft→active on 2026-09-05 and its first row
#   is graded under this stamp. It is graded on FIDELITY TO ITS OWN SPINE as its
#   OWN POPULATION and must never be pooled with the assembly's ~1.0 or with any
#   composition mean — D-6 §6 requires this sentence to ride the D-3 stamp, and
#   the mechanical reason has not changed: there is no per-analyst
#   composition-faithfulness aggregation in the tree, so the pooling bar cannot be
#   enforced by a ``GROUP BY`` that does not exist and has to be stated in words.
#
#   DIRECTION OF THE EXPECTED SHIFT: ALL THREE FAMILIES MOVE, on the ASSEMBLY
#   population only, and the split is by ``data.data.assembly.regime`` rather than
#   by date — which is exactly what that field was stamped on every row for.
#     * ``faithfulness_score`` — MOVES, and this is the largest single-train move
#       in this lineage. On the 35 assembly-regime critiques of 2026-09-05 the
#       published overall goes from 0.1739-0.5926 (mean 0.3524) to 0.5714-0.9200
#       (mean 0.8528), and admission at the 0.50 floor goes from 2/35 to 35/35.
#       On the LEGACY population: NO CHANGE, byte-identical (below).
#     * ``severity_split`` — MOVES. 26 HARD ``scope_widened`` findings leave the
#       assembly population entirely, and the surviving prose verdicts stop
#       contributing to the headline denominator on those rows. No reason code is
#       added, removed or re-classed: ``_FAIL_CLASS_BY_REASON`` is BYTE-IDENTICAL
#       across this bump, and both exhaustive pins are re-asserted unchanged.
#     * ``reason_census`` — MOVES. ``scope_widened`` falls from 26 to 0 on the
#       assembly population. Two new COUNTERS (never reasons):
#       ``assembly_scope_token_unknown`` and
#       ``assembly_headline_regraded_to_arms``, the second being the per-row
#       receipt for whose arithmetic a published number is.
#
#   BEFORE/AFTER: on the ASSEMBLY population, replayed over the 35 live
#   assembly-regime critiques since 04:16Z — arm fires 26 → 0, every
#   ``branch_scores["assembly"]`` 1.000, published overall ≥ 0.50 on 2/35 → 35/35,
#   and the 12:00Z world read's input pool 1/34 admitted → 34/34 (so the world
#   read moves from 1 block to its ``BLOCK_CAP`` of 8). On the LEGACY population
#   the whole train is INERT BY CONSTRUCTION and it is proven, not asserted: 40
#   live pre-04:16Z ``regime: "legacy"`` critiques — country, region, world and
#   thematic — re-graded through the real pass on both trees are **40/40
#   BYTE-IDENTICAL** verification dicts, field for field, under
#   ``json.dumps(..., sort_keys=True)``. On the ASSESSMENT population, one row
#   exists and its truthmaker coverage goes 1/7 → 6/7; the graded number itself is
#   an LLM verdict and is therefore measured on the next live cycle, not claimed
#   here.
#
#   WHAT THIS TRAIN DOES NOT DO: it does not touch a descriptor, the judge route,
#   ``_claim_kind``, the fail-class table, the T7 ceiling, the 0.50 floor, or any
#   prose the voice is shown. It does not build D-3b's gate, and it does not build
#   ARM 5.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

# 2026-09-06/1 — G3, THE WEIGHTED-COMPARISON LICENCE. One graded-behaviour change,
#   on ONE claim kind, on ONE population: the ASSESSMENT family's
#   ``citation_support`` branch. D-6's N=5 clause replay showed the prompt clause
#   working on its target — the INSTRUMENT class fell from 7 of 11 judge failures
#   to 3 of 8 — and the failures redistributing rather than disappearing, with
#   CROSS-BLOCK WEIGHTING rising 1 → 3 to become the leading residual. That class
#   is not fabrication and it is not fixable in the prompt, because the prompt is
#   what asks for it: ``THREADS, WEIGHTED, UNCROWNED`` tells the voice to carry
#   two to four threads and "say how they weigh against each other", and the
#   composition rubric grades every claim against the ONE sub-claim its marker
#   names. No single bounded block states a comparison across blocks, so the
#   sentence the channel mandated was the sentence the judge could only call
#   unsupported. The channel's voice contract and its grader were in direct
#   tension, and D-6 §6.1 named the repair: teach the rubric that the assembly
#   performs one ranking the voice may relay.
#
#   THE LICENCE RESTS ON A PUBLISHED FACT, NOT ON A CONCESSION. The assembly
#   performs exactly one cross-block ranking and prints it twice: the ORDER
#   (``blocks[].ordinal`` follows the ``cited_mass.v1`` ranking by construction,
#   the carried set a strict prefix of the ranked pool — ``judge_input_checks``
#   says "the ordering IS the payload") and the EARNED-LEAD VERDICT
#   (``lead.test``'s ``top_share`` / ``ratio_12`` / ``earned``, rendered verbatim
#   by ``render_lead_test``). P1 put that whole arithmetic block into EVERY cited
#   ordinal's ``evidence_text``, so a weighing sentence that names the blocks it
#   weighs already has its truthmaker in its own evidence map.
#   ``provenance.assessment_weighting`` grants SUPPORTED-BY-CONSTRUCTION to three
#   shapes and to nothing else: ``order`` (a weighing naming TWO OR MORE cited
#   ordinals), ``earned_lead`` (a crowning of the record's own lead ordinals, or
#   of ordinal 1, which the order makes the top-weight block), and ``refusal`` (a
#   negated weighing on a record whose arithmetic says concentration was NOT
#   earned). FOUR gates can withhold it, and each one is a counter: no arithmetic
#   in the map at all (the population fence), an instrument term the evidence
#   does not carry, a NUMBER the arithmetic does not carry, and a crowning the
#   record's own order does not put first. The live 2026-09-06 specimen — "this
#   high-mass (9.10) and well-verified (0.85) read outweighs … (cited mass 5.05,
#   verify 0.60)" — is a weighted comparison AND a real failure, and it is denied
#   twice over (instrument, then number). It still fails, as it must.
#
#   THE SECOND HALF IS A PRODUCER CHANGE AND MOVES NO VERDICT BY ITSELF. The
#   mandated ``## What this reading misses`` section had no vocabulary: the voice
#   was handed counts and guessed names ("no coverage of Central Asia", on a
#   record that never mentions it — 2 of arm B's 8 failures). The record knew the
#   answer and discarded it at the prompt boundary, so
#   ``assessment_prompts.declared_aperture`` now publishes the drop ledger's own
#   desks, targets and head titles plus D-2b's ``coverage_roster``, into the
#   prompt AND — because it is part of ``record_arithmetic`` — into every
#   citation's evidence map, which is what makes a blind-spot sentence gradeable
#   for the first time. ``assessment_unsupported.aperture_unrostered`` is its
#   fence: the SIXTH deterministic class, marking an aperture sentence that names
#   a unit appearing in neither the roster, the drop ledger, a carried block nor
#   a quoted span. ``PROMPT_VERSION`` → ``assessment_prompt.v3`` and
#   ``UNSUPPORTED_VERSION`` → ``unsupported.v3`` carry the attribution.
#
#   DIRECTION OF THE EXPECTED SHIFT: the ASSESSMENT family MOVES on all three;
#   the ASSEMBLY and DESK families cannot move at all, and the reason is
#   mechanical rather than statistical. The licence routes off ONE predicate —
#   does a cited evidence entry carry ``--- THE RECORD THIS BLOCK SITS IN ---`` —
#   and ``assessment_channel.assessment_evidence_text`` is the only writer of that
#   rule in the tree. A composition, a desk head and an assembly row all decline
#   before any lexicon is consulted, with no counter, no span and no log line,
#   which is the ``composition_integrity`` / ``assembly_arms`` inertness contract
#   held for a third time.
#     * ``faithfulness_score`` — MOVES, on Assessment rows only and UPWARD only:
#       the licence can lift a verdict and can never create one. It also moves
#       ``branch_scores["citation_support"]`` — the FIRST override stage in this
#       plane to claim a branch — because that branch IS the Assessment's
#       ``fidelity_to_spine``, the number G3 is about, and leaving it stale would
#       publish a row whose headline moved and whose partition key did not.
#       Nothing else in ``branch_scores`` is touched.
#     * ``severity_split`` — MOVES. ``judge_contradicted`` is HARD and a REFUSAL
#       to crown was one (measured: 1 of arm B's 8 failures), so a licence can
#       retire a hard fail. Counted separately as
#       ``weighted_comparison_licensed_hard`` precisely so the severity move is
#       sizeable on its own rather than inferred from the score.
#     * ``reason_census`` — MOVES. ``judge_unsupported`` and
#       ``judge_contradicted`` fall on the Assessment population and
#       ``override_erased_*`` rises with them. NO REASON CODE IS ADDED, REMOVED OR
#       RE-CLASSED: ``_FAIL_CLASS_BY_REASON`` is BYTE-IDENTICAL across this bump
#       and both exhaustive pins are re-asserted unchanged. Seven new COUNTERS
#       (never reasons): ``weighted_comparison_seen``,
#       ``weighted_comparison_licensed``, ``weighted_comparison_licensed_hard``
#       and the four ``weighted_comparison_denied_*`` classes.
#
#   BEFORE/AFTER, measured: see ``planning/G3_REPAIR_REPORT.md`` for the two-arm
#   N=5 replay through the real channel path and the real verify path against the
#   live core plane and the live judge. On the LEGACY population the train is
#   INERT BY CONSTRUCTION and it is proven the way D-3's was, not asserted: the 40
#   live pre-cutover ``regime: "legacy"`` critiques re-graded through the real
#   pass on both trees are 40/40 BYTE-IDENTICAL verification dicts under
#   ``json.dumps(..., sort_keys=True)``.
#
#   WHAT THIS TRAIN DOES NOT DO: it does not touch a descriptor (the R4 FZ-3
#   freeze is not engaged — ``analyst_world_assessment.yaml`` is byte-unchanged
#   and ``descriptor_prompts.json`` regenerates byte-identically, because this
#   channel's prose doctrine is a CODE constant), the judge route, the judge
#   prompt, ``_claim_kind``, the fail-class table, the T7 ceiling or the 0.50
#   floor. It does not build D-3b's gate and it does not land the ``fact`` arm.
#
# 2026-09-08/1 — PARTITION-PRESERVE. One graded-behaviour change, and it is a
#   TRANSPORT repair that had to be stamped because it moves verdicts.
#
#   WHAT BROKE. The verify judge runs on the $0 core plane through OpenRouter
#   (``nvidia/nemotron-3-super-120b-a12b:free``). From 2026-09-07 04:00Z that
#   route began failing about a third of the time, and it failed in a shape no
#   layer in this tree could see: OpenRouter reports an upstream provider failure
#   as **HTTP 200 carrying an error envelope** — ``{"error": {"message":
#   "Upstream error from Nvidia: Service temporarily overloaded", "code": 502}}``
#   with no ``choices`` and no ``provider``. ``stack/llm/base._call_chat`` retries
#   on ``response.status_code``; the status code was 200, so its retry never
#   fired. ``vllm._parse_response`` mapped the empty ``choices`` to
#   ``content=""``, the judge parse found no ``verdicts`` object, and the row
#   landed as ``judge_empty`` — the reason reserved for "the judge answered and
#   said nothing". The receipt was worse: ``_account_call`` wrote
#   ``status="success"`` 599 times. MEASURED: 599 of 2,019 nemotron calls since
#   09-07 (29.7%); live probe 09-08, 2 of 8 requests (25%); fleet
#   ``Faithfulness verify`` rows 09-05 446 judged / 19 empty → 09-08 117 / 222.
#
#   THE AMPLIFIER, WHICH IS WHAT THIS STAMP IS FOR. ``_run_judge`` splits a
#   finding's claims into an ABSENCE partition and a shared partition and calls
#   the judge once for each. Both call sites read ``if not verdicts: return
#   [], {}`` — EITHER partition coming back empty discarded BOTH. At a ~30%
#   per-request failure rate a finding carrying one absence claim lost its
#   verdicts about half the time, and threw away the verdicts the judge HAD
#   produced along with the ones it had not. Each partition now stands or falls
#   alone: the judged partition's verdicts are KEPT and stay authoritative over
#   the prose they graded, and only the empty partition's claims fall to the
#   floor — where the EXISTING reconciliation already handles them, because
#   "claims the judge did not grade" is a case this code has always had
#   (``residual_floor_spans`` folds their unsupported floor spans into
#   ``effective_checkable``; ``carried_ledger`` carries their floor ledger rows).
#   NO VERDICT IS FABRICATED FOR A CLAIM NOBODY GRADED — that is the invariant
#   the whole repair turns on.
#
#   THE NEW STATE AND ITS CEILING RULE. ``judge_status='partial'``, with the
#   empty partition named in ``judge_unavailable_reason``
#   (``judge_empty_partition:absence``). ``PROVISIONAL_SCORE_CEILING`` (0.85) is
#   the mark of a verdict NO grader adjudicated, so it applies IFF the report
#   carries ZERO judged verdicts: ``deterministic`` and ``unsampled`` keep it
#   exactly as before, ``partial`` does not take it, ``llm`` does not take it.
#   A partial pass is already CHARGED for its gap rather than excused — the
#   floored partition's unsupported spans sit in the denominator as failures, so
#   it can only score at or below a fully-graded pass — and capping it again
#   would charge the finding twice for one provider's overload.
#   ``is_provisional`` is the single decider and it is the only function
#   changed; ``gate_score`` needs no edit. Every OTHER reader in the tree still
#   tests ``judge_status == 'llm'`` and therefore treats a ``partial`` row
#   exactly as it treated the ``deterministic`` row this state REPLACES — the
#   journal no-critique gate (``actor_critic``), the escalation re-grade
#   (``judge_floor_escalation``), the GEPA skip, the adjudicated-share gauge
#   (``production_gauge_integrity``) and the UI's High-confidence rule. Every one
#   of them is conservative under the change and none of them regresses.
#
#   THE TRANSPORT HALF, WHICH MOVES NO VERDICT BY ITSELF and would not have
#   needed a stamp. ``judge_transport.call_judge_with_retry`` wraps ONE judge
#   call in a bounded retry — 3 attempts, 2 s → 8 s exponential backoff with ±25%
#   jitter, under a 45 s added-sleep budget checked BEFORE each sleep, honouring
#   a provider ``Retry-After`` over the schedule. It retries an in-body 5xx/429
#   envelope, a real 5xx/429, a network timeout and an empty body; it re-raises a
#   non-retryable hard failure immediately, so ``judge_error`` keeps its meaning
#   (the 09-06 19:00–23:59Z 429 storm, 141 calls, is correctly ``judge_error``
#   today and nothing here reclassifies it) and ``judge_empty`` keeps its meaning
#   (every attempt came back empty). Two ADDITIVE receipts land on the verify
#   row: ``judge_attempts`` and ``judge_http_statuses``, where ``"200/502"`` reads
#   as *the router answered 200 and named a 502 inside*. Both are SPARSE — absent
#   on every path that made no judge call, so those verification dicts stay
#   byte-identical.
#
#   DIRECTION OF THE EXPECTED SHIFT: all three families MOVE, fleet-wide, and
#   UPWARD in adjudicated volume. The population that moves is every finding
#   carrying at least one absence claim that met a transport failure on exactly
#   one of its two partitions — on 09-08's rates that is a large share of the 222
#   daily ``judge_empty`` rows, and each one converts from a floor-only
#   provisional 0.85-capped verdict to a mixed adjudicated one.
#     * ``faithfulness_score`` — MOVES, in BOTH directions and by design. A
#       partial row's score is the judged partition's ratio with the floored
#       partition's failures folded in, which can land above OR below the floor's
#       own number; and the 0.85 cap lifting is an upward move on rows that
#       clear it. The retry alone also moves it, by turning ``judge_empty`` rows
#       into graded ones.
#     * ``severity_split`` — MOVES. A kept absence verdict can be
#       ``judge_contradicted`` (HARD) where the discarded pass published a soft
#       floor ``no_citation``, and vice versa. Hard-fail share rises on the
#       recovered population because the judge is a stricter grader than the
#       marker floor on exactly these spans.
#     * ``reason_census`` — MOVES. ``no_citation`` and the other floor reasons
#       fall on the recovered population as judge reasons replace them, and the
#       denominator itself changes: a partial row's ``checkable_claims`` is the
#       judged count plus residual floor spans, not the floor's whole claim list.
#       NO REASON CODE IS ADDED, REMOVED OR RE-CLASSED —
#       ``_FAIL_CLASS_BY_REASON`` is BYTE-IDENTICAL across this bump. One new
#       STATUS (``partial``, not a reason), one new reason PREFIX on an existing
#       field (``judge_empty_partition:``) and two new receipts.
#
#   WHAT THIS TRAIN DOES NOT DO: it does not change the judge MODEL, the judge
#   ROUTE, the request's model/temperature/``max_tokens``, any judge PROMPT, the
#   verdict vocabulary, the severity chain, ``_claim_kind``, the fail-class
#   table, the T7 ceiling, the 0.50 floor or any descriptor. It adds NO paid
#   route: OpenRouter serves the ``:free`` slug from exactly ONE endpoint
#   (Nvidia, checked live 09-08), so ``provider.order`` / ``allow_fallbacks`` has
#   nowhere to route and is deliberately not set — the two extra providers on the
#   non-free slug are PAID and that is an operator's route decision, not a
#   transport fix.
#
# 2026-09-20/1 — PARTIAL VERDICTS + THE EVIDENCE ENVELOPE. Two changes, one
#   stamp, and BOTH move verdicts. They ship together because they are the two
#   halves of one measured complaint: the grader was being denied a verdict it
#   had produced, and was being asked for a verdict on evidence it had not been
#   shown.
#
#   A. THE VERDICT-COUNT MISMATCH. ``_judge_claim_partition`` has enforced an
#   honest length contract since #116d: one verdict per graded claim, or fail —
#   because the alternative it replaced was a ``zip`` that truncated to the
#   shorter list and silently passed the ungraded tail. MEASURED on the live
#   route: ~1.5% of judge calls end at ``verify.faithfulness.judge_failed
#   err=judge returned 22 verdicts for 23 claims`` (15 on 09-19, 4 on 09-20; the
#   same shape at 73 for 74). The model drops ONE verdict out of twenty-odd and
#   the entire row falls to the deterministic floor as
#   ``judge-unavailable:judge_error``, capped at the PROVISIONAL 0.85 ceiling.
#   Twenty-two adjudicated claims discarded to punish one missing one — the same
#   amplifier 2026-09-08/1 removed one level up, at the partition.
#
#   WHAT IS SALVAGED, AND THE LINE IS IDENTITY, NOT COUNT. The protocol is
#   POSITIONAL — ``{"verdicts": [...]}``, one token per numbered claim, in order
#   — so a short list of BARE tokens is not a partial answer, it is an
#   unreadable one: every verdict after the drop point may belong to the claim
#   before it, and there is no way to learn which. That case KEEPS today's hard
#   failure, deliberately; guessing by position would fabricate verdicts for
#   named claims, which is the one thing this subsystem may never do. What IS
#   readable is a response whose entries NAME their claim — the judge is shown
#   ``1. <claim>`` … ``N. <claim>`` and may answer ``{"claim": 3, "verdict":
#   "supported"}``, or ship a parallel ``"claim_indices"`` array. Those align by
#   ID, all-or-nothing and distinct-or-nothing, and every claim no entry names is
#   left UNCHECKED — not unsupported, not supported, NOT GRADED. An unchecked
#   claim leaves the judged population exactly the way a floored partition's
#   claims already do, through machinery that predates this train:
#   ``residual_floor_spans`` folds in whatever the DETERMINISTIC floor found
#   about it (so a genuinely uncited claim is still charged) and
#   ``carried_ledger`` carries its floor ledger row. The salvage is bounded at
#   ``max(2, ceil(0.1·N))`` dropped verdicts (``partial_verdict_budget``): a
#   response missing more than a tenth of its answers is not a judge that
#   skipped a line, and it keeps the hard failure too.
#
#   THE STATE AND THE CEILING RULE, unchanged from 2026-09-08/1 and reused
#   rather than re-argued: ``judge_status='partial'``, NOT provisional, no 0.85
#   cap, under its OWN reason prefix ``judge_partial_verdicts:<partition>:<k>``
#   (an empty partition and a short answer are different provider behaviours and
#   the row must say which). Two ADDITIVE sparse receipts ride the verification
#   block: ``judge_partial`` = k and ``judge_partial_claims`` = which claims, in
#   span order. NO ROW CAN MOVE FROM ``llm`` TO ``partial``: this path is
#   reachable only where ``_JudgeVerdictError`` was raised, i.e. only on rows
#   that published the deterministic floor.
#
#   A also repairs a silent defect of its own, and it is a verdict-population
#   change on its own terms: an object-form entry at the RIGHT length used to be
#   stringified whole (``str({'claim': 3, ...})``), miss the four-token
#   vocabulary, and coerce to ``unsupported``. A labelled verdict is now read as
#   the verdict it is.
#
#   B. THE EVIDENCE ENVELOPE. ``_EVIDENCE_TOTAL_CHARS`` 4,000 -> 8,000,
#   ``_EVIDENCE_SOURCE_CHARS`` 3,000 -> 6,000, ``_EVIDENCE_GROUNDING_CHARS``
#   2,400 -> 4,800. The unit evidence string is ``OUTLET + title + SOURCE +
#   Analyst summary``; the producer stores a cited article at
#   ``inline_target._SOURCE_TEXT_CHARS`` (3,200), so the 4,000 total left ~700
#   chars for the analyst summary and cut it mid-sentence. A claim drawn from the
#   tail of what the analyst was SHOWN therefore read as absent from the evidence
#   and graded unsupported. The judge's ``max_tokens`` is 16,384 and the route
#   has no context problem at this size.
#
#   WHAT IS AND IS NOT INERT, said plainly, because it bounds the shift claim:
#     * ``_EVIDENCE_TOTAL_CHARS`` is load-bearing — the analyst-summary line
#       survives whole where it was cut.
#     * ``_EVIDENCE_SOURCE_CHARS`` binds only between 3,000 and the 3,200 store
#       cap, but in that band it also flips the F1 label: an article the judge
#       used to RE-CUT was announced as an "authoritative excerpt", which softens
#       "absent => unsupported" to "contradicted => unsupported". Those sources
#       are complete and are now announced as complete — a STRICTER read on that
#       band, and the one arm of this train that can move a verdict DOWNWARD.
#     * ``_EVIDENCE_GROUNDING_CHARS`` is INERT today: the producer captures a
#       grounding block at ``unit_grounding.EVIDENCE_TEXT_CHARS`` (2,400).
#     * NO PRODUCER CAPTURE MOVES. The composition pin
#       (``test_composition_evidence_window``) relaxes from ``==`` to ``>=``,
#       which is what the P0c defect was actually about — the judge's window may
#       never be NARROWER than the capture — so widening the grader does not drag
#       a prompt-token budget along behind it.
#
#   DIRECTION OF THE EXPECTED SHIFT: all three families MOVE, fleet-wide.
#     * ``faithfulness_score`` — MOVES, both directions. A: ~1.5% of judge calls
#       convert from a floor-only 0.85-capped provisional verdict to a mixed
#       adjudicated one, which can land above or below the floor's number. B:
#       claims that resolve deep in an evidence string stop reading as absent
#       (upward), while the 3,000–3,200 source band loses its excerpt softening
#       (downward).
#     * ``severity_split`` — MOVES. A recovered verdict can be
#       ``judge_contradicted`` (HARD) where the discarded pass published a soft
#       floor reason; and a claim the judge can now read to the end of its
#       evidence can earn a quote it previously could not resolve, which is the
#       difference between a hard fail and a ``contradicted_unquoted`` demotion.
#     * ``reason_census`` — MOVES. The denominator changes on the recovered
#       population (the judged count plus residual floor spans, not the floor's
#       whole claim list), and the floor reasons fall where judge reasons replace
#       them. NO REASON CODE IS ADDED, REMOVED OR RE-CLASSED —
#       ``_FAIL_CLASS_BY_REASON`` is BYTE-IDENTICAL across this bump. One new
#       reason PREFIX on an existing field (``judge_partial_verdicts:``) and two
#       new sparse receipts, neither of which is a reason.
#
#   WHAT THIS TRAIN DOES NOT DO: it does not change the judge MODEL, the judge
#   ROUTE, ``max_tokens``, ``temperature``, any judge PROMPT or rubric, the
#   verdict VOCABULARY (``unchecked`` is a parse slot, never an emitted token
#   and never a ledger row), the severity chain, ``_claim_kind``, the fail-class
#   table, the T7 ceiling, the 0.50 floor, any producer capture or any
#   descriptor. It adds NO retry: the transport's follow-up-call budget is spent
#   on a WHOLE partition, and a second call asking only for the missing claims
#   would need its own prompt — a prompt change, which is judgement and not
#   transport. That is deliberately not done here.
#
# 2026-09-24/1 — H3, the NAMED-CLAIM contract. The judge's reply shape now asks
# every verdict entry to carry ``claim_index`` (the number the claim was listed
# under), on the shared unit/composition lead AND the absence rubric — one
# renderer in ``judge_quote_rules`` so the envelope cannot drift between routes.
# The id arm it feeds is not new: ``judge_verdict_parsing.align_verdicts`` has
# aligned by named claim since 2026-09-20/1 — but no prompt ever ASKED for the
# ids, so the salvage fired only when a model volunteered them. This train asks.
# Positional alignment stays the fallback for entries that name nothing, so a
# judge that ignores the field degrades to the pre-H3 contract rather than
# failing. Both prompted profile versions move with it: ``citsupp.v5 -> v6``
# and ``absence.v4 -> v5``.
#
# DIRECTION OF THE EXPECTED SHIFT — all three families MOVE, and the mechanism
# is the 09-20 one's finally armed:
#   * ``faithfulness_score`` — MOVES, both directions. Every short or reordered
#     reply that used to raise ``_JudgeVerdictError`` and publish the
#     deterministic floor under ``judge_error`` (provisional, 0.85-capped) now
#     lands as a ``partial`` pass scored on the claims actually graded — which
#     can sit above or below the floor's number.
#   * ``severity_split`` — MOVES. A salvaged verdict can be
#     ``judge_contradicted`` (HARD) where the discarded call had published a
#     soft floor reason.
#   * ``reason_census`` — MOVES. ``judge-unavailable:judge_error`` rows fall and
#     ``judge_partial_verdicts:*`` receipts rise. NO REASON CODE IS ADDED,
#     REMOVED OR RE-CLASSED — ``_FAIL_CLASS_BY_REASON`` is byte-identical across
#     this bump.
#
# WHAT THIS TRAIN DOES NOT DO: the rubric, the evidence envelope
# (8000/6000/4800), the verdict vocabulary, the severity chain, the judge
# model/route/max_tokens/temperature and every descriptor are unchanged. The
# ONLY text that moved is the reply-shape sentence; ``quotes`` is still asked
# for as the parallel array.
#
# 2026-09-25/1 — H3-MEASURE, persisting HOW H3 aligned. H3's alignment MODE
# was never persisted — the aligned list wrote to ``claim_verdicts`` exactly
# as a fully positional one would, so H3's own effect was unmeasurable. A
# READOUT wire: every ledger entry carries ``aligned_by`` (``"claim_index"``
# | ``"positional"``, absent — never ``null`` — off the judge path); the block
# gains signed ``miscount_claims`` (per-partition verdicts-minus-claims-sent),
# ``aligned_by_id`` / ``aligned_positionally`` (off the ledger) and
# ``unmatched_claims`` (a named reply's un-named claims, i.e. ``judge_
# partial``); ``judge_stats_api.py`` exposes ``positional_share`` /
# ``miscount_rate`` on the totals and per pipeline-version — one GET.
#
# NOTHING ABOVE TOUCHES A VERDICT: a pure-widening THIRD return value on
# ``align_verdicts``; no reason added/renamed/reclassed; no score, model,
# route, prompt or vocabulary moves. SECOND all-``SHIFT_NONE`` entry (the
# first, 2026-08-30/1, called itself "the ONLY" — true when written).
#
# EXPECTED SHIFT — NONE, on all three: ``aligned_by`` names the branch
# ``align_verdicts`` already took, rather than deciding one — pinned in
# ``test_verify_partial_verdicts.py`` / ``test_judge_verdict_alignment_audit.py``.
# ``positional_share`` reads near 1.0 pre-H3, falling only as the reply
# contract reaches the fleet; ``miscount_rate`` stays FLAT against H3 itself.
# ---------------------------------------------------------------------------

#: Stamped into every faithfulness critique's ``data.verification`` block.
#: Re-exported by ``verify`` (the historical import surface).
JUDGE_PIPELINE_VERSION = "2026-09-25/1"


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
