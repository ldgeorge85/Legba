# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared analytic-tradecraft preamble for every LLM analyst.

The single source of truth for the "house analytic standard" prepended to each
analyst's system prompt (BLUF, observe-vs-assess, citation, calibrated
confidence, provenance trust, temporal discipline, gap-honesty, output
discipline). Each analyst keeps a thin task-specific block on top of this.

Dependency-free on purpose so it is safe to import from any analyst module and
from ``runtime.analyst_method`` without circular-import or dspy concerns. See
planning/PROMPT_REWRITE_PLAN.md for the rationale per clause.
"""

from __future__ import annotations

ANALYTIC_PREAMBLE = """You are a senior all-source intelligence analyst. Hold to these analytic standards on every output:

1. BLUF. Lead with your single most important judgment in one sentence, before any detail.
2. Separate OBSERVATION from JUDGMENT. Distinguish what the sources state (observed) from your inference (assessed) and any assumption you rely on. Label assessments and assumptions as such; never present inference as established fact.
3. Source every factual claim. Cite the specific evidence you used inline — a signal index like [2], a substrate UUID, or the analyst id you drew from. Do not assert a fact you cannot point to.
4. Calibrate confidence to the evidence, and use matching estimative language:
     >= 0.85  multiple independent vetted sources corroborate    (assess / judge)
     ~  0.6   a single credible source, or a sound inference      (likely / probably)
     <= 0.3   speculative, thin, or a single weak source          (possibly / cannot confirm)
   Never manufacture precision the evidence does not support.
5. Trust by provenance. An AUTHORITATIVE CURRENT CONTEXT block, when present, is operator-vetted ground truth and OVERRIDES anything your training data implies about who holds office, which alliances are in force, or the present state of the world. Treat seed/curated facts as ground truth; treat ingestion/agent facts as LEADS to corroborate, not truth — especially any ingestion fact at confidence 1.0 (a likely extraction error, e.g. 'Iran | capital of | US').
6. Mind time. A source's ingestion/fetch timestamp is NOT when the event occurred. Anchor "what is current" on each source's own stated date and the AUTHORITATIVE CURRENT CONTEXT — never the fetch time. Do not present an older referenced event as if it broke today.
7. Be honest about gaps. If the material is thin, say so plainly rather than padding. Where sources disagree, surface the disagreement rather than averaging it away. State the key uncertainty and what new evidence would change your judgment.
8. Output discipline. Respond with EXACTLY the JSON object your task specifies and nothing else — no prose, no markdown fences, no commentary. The first character must be { and the last must be }."""


def with_preamble(task_block: str) -> str:
    """Compose an analyst system prompt = shared standards + task-specific block."""
    return f"{ANALYTIC_PREAMBLE}\n\n{task_block.strip()}\n"


def with_preamble_if_absent(system_prompt: str | None) -> str | None:
    """Prepend the preamble unless it is already present.

    Used at the inline_target resolution point so a GEPA-promoted candidate
    (which replaces the whole system prompt) still carries the house standard.
    """
    if system_prompt is None:
        return None
    if system_prompt.startswith(ANALYTIC_PREAMBLE[:48]):
        return system_prompt
    return with_preamble(system_prompt)


# ---------------------------------------------------------------------------
# Phase-V VOICE contract — ONE definition, both layers
# ---------------------------------------------------------------------------
#
# VOICE_DIAGNOSTIC_2026-08-04 measured, over 701 unit findings + 117
# compositions in a trailing 36h window, that the tower's prose defects are
# PROMPT defects: the model is doing exactly what it was told. The constants
# below are the replacement instructions, defined ONCE here rather than pasted
# into eight descriptors and four composition prompts, because eight copies of a
# house rule drift the moment one is edited.
#
# HOW THEY REACH EACH LAYER — the two layers bind differently, on purpose:
#
#   * COMPOSITIONS take them by IMPORT: ``meta_findings_synthesizer`` embeds
#     the constants directly in its four system-prompt strings, so a code deploy
#     IS the change.
#   * UNITS take them by PASTE: a bounded unit's system prompt lives in its
#     descriptor (``method.system_prompt``), and the ONE code choke point that
#     could append them (``inline_target._effective_system_prompt``) is shared
#     with THREE non-unit inline_target analysts (corpus_researcher,
#     country_assessor, cross_doc_corroborator) whose body shapes are
#     legitimately different — appending a unit body spec there would corrupt
#     them. So :data:`UNIT_READ_CONTRACT` is pasted VERBATIM into the NINE unit
#     descriptors and ``tests/data_pkg/test_voice_contract.py`` pins
#     descriptor-vs-constant equality (whitespace-normalized). Drift is a red
#     test, not a silent divergence.
#
#     DS-1 (2026-08-06): ``disruption_status`` is the ninth. It was carved out
#     of this paste on a reason that does not survive inspection — it runs a
#     GATHER loop, so it was filed with the retrieval analysts — and went on
#     ordering the exact template sentence D2 replaces (93% of its findings,
#     unchanged across the wave). Gathering is about where evidence comes from;
#     the read contract is about what shape the answer takes. A desk can be
#     both, and this one is.
#
# The one thing these rules REFUSE to ask for, per the approved before/after
# direction: atmosphere. No scene-setting, no inferred motive, no "in a worrying
# sign", no adjective doing the work a cited number should do. The craft target
# is SPECIFICITY — dates, magnitudes, named actors, the named brake on a trend —
# every one of which is already in the rendered evidence and therefore ADDS
# verifiable surface rather than fabrication risk.

#: The three template sentences the diagnostic counted coming back out of the
#: prompt verbatim: "most plausible near-term trajectory" in 292/701 findings
#: (42%), "the dominant … vector is" in 383/701 (55%), and "steady" absorbing
#: everything that is not a visible inflection in 382/701. Written here as the
#: model produces them, quoted into :data:`UNIT_VERDICT_RULE`'s ban sentence.
BANNED_TEMPLATE_PHRASES: tuple[str, ...] = (
    "the most plausible near-term trajectory",
    "the dominant <thing> vector is",
    "steady tension",
)

#: The substrings a prompt-assembly test polices. Each MUST appear in an
#: assembled unit prompt ONLY inside :data:`UNIT_VERDICT_RULE` — i.e. only as
#: part of the prohibition. A count above that means some descriptor is still
#: handing the model the finished sentence, which is the defect D2 exists to
#: remove: banning a phrase while still ordering it produces synonym-swapping,
#: not judgment. Deliberately NOT a ban on the word "vector" — a list of
#: escalation vectors is legitimate taxonomy; "the dominant … vector is" as an
#: output sentence is not.
BANNED_PHRASE_MARKERS: tuple[str, ...] = (
    "near-term trajectory",
    "dominant",
    "steady tension",
)


#: The UNIT-layer TITLE rule — byte-identical to what ``_body_shape`` has
#: rendered since V-1 (``96a6e1e6``, 2026-08-03). Hoisted out of the function
#: body UNCHANGED by TITLE-FRAME-FIX (2026-09-01) so the composition layer can
#: be given a different one without moving a single byte of the nine pasted
#: descriptor copies (``UNIT_READ_CONTRACT``), the MA4 splice anchor below, or
#: the ``test_voice4_flip_kit`` digests that cover them.
#:
#: WHY THE UNIT LAYER KEEPS THE OLD WORDING. ``VOICE_ORGANIC_REVIEW_2026-09-01``
#: §2.a settles it with the desks' own numbers: this contract is IDENTICAL on
#: all nine desks and their frame rates run 0.0% (military_posture,
#: proliferation_watch) to 80.8% (escalation). A constant cannot explain a
#: spread it does not vary with. What DOES vary — and predicts each desk
#: exactly — is the per-desk ``"title"`` schema hint in the descriptor, which is
#: where the unit-layer half of this train lands instead.
_TITLE_RULE_UNIT: str = (
    "TITLE. 'title' is a headline of AT MOST 90 characters. It must NOT "
    "repeat the BLUF sentence, must NOT begin with 'BLUF', and must NOT be "
    "the body: it names the subject and the verdict in a few words. The "
    "body text belongs in 'body' and nowhere else."
)

#: The COMPOSITION-layer TITLE rule — TITLE-FRAME-FIX, 2026-09-01.
#:
#: THE DEFECT IT REPLACES. ``_TITLE_RULE_UNIT``'s "it names the subject and the
#: verdict in a few words" sits one sentence away from
#: :data:`COMPOSITION_BODY_SHAPE` §2's "the single most consequential thing",
#: and in the assembled prompt the two render as one contiguous run. Read as the
#: model reads it: ``title = subject + verdict``, ``subject = the single most
#: consequential thing``, verdict domain = escalation risk. The measured result
#: (review §1.1) is the ``<subject> <driving verb> <risk noun>`` frame on
#: **54/58 (93.1%)** of world reads in the four weeks after V-1 shipped, against
#: **0/60** before it — while the body went the OTHER way (title-subject
#: paragraph coverage 0.662 → 0.458, 17.2 named entities per body). The lock is
#: therefore a HEADLINE lock sitting on a plural body: a misrepresentation
#: defect, not a coverage one.
#:
#: WHY IT IS NOT A BAN. ``_tradecraft.py``'s own D2 note (see
#: :data:`BANNED_PHRASE_MARKERS` above) records what a bare ban buys: "a ban on
#: its own produces 'the principal vector', which is the same defect with a
#: thesaurus". The frame already rotates 24+ verbs and 27 subjects while holding
#: its grammar, so banning the words would move nothing. The replacement SHAPES
#: ship in the same breath as the prohibition, exactly as ``UNIT_VERDICT_RULE``
#: does.
#:
#: WHY IT KEEPS THE ANTI-ROLL-CALL FLOOR. V-1 was itself the cure for a real
#: defect — E0's 60 world reads titled ``World situational assessment —
#: <date>``, naming no entity at all (review §1.1: "no named entity 100.0%").
#: Un-crowning without a floor walks straight back into it, so the rule carries
#: an explicit second failure mode and requires at least one concrete named
#: subject in every headline.
#:
#: THE SHAPE LABELS WERE DELETED, NOT BANNED — and it took two replays to get
#: that right. Replay 1 returned *"Tension: Ukrainian strikes vs Russian Kyiv
#: raids amid multiple regional flare-ups"*: the model printed this rule's own
#: label as a headline prefix. The first fix was a ban — *"never print
#: 'Tension', 'Pattern' … in the headline"* — and replay 2 returned *"Pattern:
#: Haiti gang carnage and Russia mobilization elevate global escalation"*
#: anyway. That is ``_tradecraft.py:112-117``'s own lesson landing on this
#: train: **banning a phrase while still handing it over produces the phrase.**
#: So the four ALL-CAPS tags are gone from the shape list entirely — each shape
#: is now described rather than named — and what remains is a rule about the
#: FORM (no label-and-colon opener) rather than about three words the model
#: would otherwise still be reading.
#:
#: TWO MORE CLAUSES THE REPLAYS EARNED, both against real arm-B output:
#:
#: * AN ABSTRACTION IS NOT A SUBJECT. Replay 1 returned *"Energy-security
#:   pressures and kinetic triggers heighten global escalation risk"* — plural,
#:   un-crowned, and naming nobody: the empty universal reached by a different
#:   road. The floor said "at least ONE concrete subject" and the model read
#:   "concrete" as satisfiable by an abstract noun phrase, so the floor now says
#:   what kind of thing a subject IS and names three abstractions as failures.
#:   ``_title_frame_gauge.entity_tokens`` gained the matching fix in the same
#:   pass, because it had scored that headline as naming somebody.
#: * PREDICATE ON THE WORLD, AND A LONGER SUBJECT IS NOT A PLURAL HEADLINE.
#:   **The most instructive result of the whole train.** Replay 2 — with the
#:   concreteness floor and the brevity clause in place — un-crowned the read
#:   exactly as intended (``crowned_rate`` 0.625 → 0.312) and the strict frame
#:   rate went straight back UP, 0.375 → 0.750, because the model satisfied
#:   every new constraint at once with *"Iran, Israel, Turkey, and Saudi Arabia
#:   drive rising regional escalation risk"*: plural, concrete, inside the cap —
#:   and the identical grammar with a longer subject. The crown was gone and the
#:   formula was not. Two clauses answer it: the compound-subject loophole is
#:   named and banned explicitly, and the underlying cause is addressed — the
#:   frame survives because the VERB is about the reader's alarm rather than
#:   about the world, so the rule now asks for a verb the event owns. This is
#:   also why the gauge grew ``crowned_rate``: without it, replay 2 would have
#:   read as a pure regression rather than as two thirds of a cure.
#:
#: WHY IT CARRIES A BREVITY CLAUSE. Measured, not guessed. The first replay of
#: this contract (16 real world/region runs, ``planning/TITLE_FRAME_FIX_
#: 2026-09-01.md`` §5) did what it was built to do — strict frame 80% → 30% on
#: the region arm, subjects per title 2.2 → 3.1 — and paid for it in characters:
#: mean title length 72.8 → 87.1 and over-90-char titles 2/10 → 4/10. Naming
#: three subjects costs room, and the model bought that room by keeping the
#: connective padding and running past the cap. So PLURAL IS NOT LONGER ships
#: with an ORDER of cuts (padding first, risk noun next, a subject last) rather
#: than as a bare restatement of the cap, which the prompt already carried and
#: which was not what failed. It also names ``amid`` specifically: §1.6 measured
#: it at 22.4% of world titles and 63.5% of country ones, and identifies it as
#: "the grammatical device that lets a ≤90-char single-subject headline gesture
#: at everything it just demoted" — under a plural contract it becomes the
#: cheapest way to blow the cap.
#:
#: WHY IT IS UNDER TRACEABILITY. Review §5.1's stated objection to retargeting
#: the title — "asks for a judgment ('the day's shape') that no evidence block
#: states, so it needs a TRACEABILITY carve-out or it will fabricate". The last
#: clause is that carve-out: the headline may only name what a cited block
#: already carries, so a shape claim is a claim ABOUT the shown blocks and never
#: a new fact about the world.
#:
#: THIS IS THE THIRD OF SIX DRAFTS, AND IT WAS SELECTED ON MEASUREMENT, NOT
#: TASTE. ``planning/TITLE_FRAME_FIX_2026-09-01.md`` §5 replays six versions of
#: this constant over the same 16 real world/region runs against the same
#: reconstructed user prompts. Three further drafts were written and REJECTED,
#: and what they rejected is worth keeping:
#:
#: * v4 added "the headline does not END on a risk word" and v5 replaced it with
#:   "the main verb takes a WORLD object". Both drove the crown lower still
#:   (crowned 0.125 / 0.267 against this draft's 0.250) and both bought it by
#:   busting the character cap — over-90-char titles 5/16 and 6/15 against this
#:   draft's 3/16, which is the LIVE baseline. A headline contract whose titles
#:   overflow the ``<h1>`` a third of the time has traded one product defect for
#:   another.
#: * v6 collapsed the accreted clauses back to near-minimal on the theory that
#:   instruction load was itself the problem. It produced the longest titles of
#:   any arm (91.8 mean chars) and did not recover v1's frame rate.
#:
#: So the accretion stops here: this draft is the one that removes the most
#: crowning while holding cap compliance at the live baseline, and it is the
#: first draft in the series with ZERO shape-label leaks and ZERO roll-call
#: titles. The three later drafts are recorded in the git history of this file
#: (``0c491e65``, ``63ec3806``, ``20e49a53``) so nobody has to re-run them.
_TITLE_RULE_COMPOSITION: str = (
    "TITLE. 'title' is a headline of AT MOST 90 characters. It must NOT "
    "repeat the BLUF sentence, must NOT begin with 'BLUF', and must NOT be "
    "the body: the body text belongs in 'body' and nowhere else. "
    "NAME THE SHAPE OF THE WINDOW, NOT ONE DRIVER. The headline says what "
    "KIND of window these blocks show, and that is usually more than one "
    "thing. Pick the shape the evidence actually fits, and pick it fresh: "
    "(i) two or three subjects named together, with what they share or where "
    "they part; (ii) two named subjects moving OPPOSITE ways, one worsening "
    "while another eases or holds; (iii) the thing several theatres have in "
    "common, with those theatres NAMED — a count with no names is a shrug, "
    "not a finding; (iv) one subject alone, which is right when the evidence "
    "genuinely concentrates there, and then you name the SPECIFIC "
    "development rather than the category it belongs to. "
    "Those four are SHAPES, NOT LABELS. Never open the headline with a label "
    "and a colon — no \"Tension:\", no \"Pattern:\", no \"Update:\". The "
    "headline opens on the world. "
    "CONCENTRATION IS EARNED, NOT ASSUMED: take shape (iv) when one block "
    "plainly outweighs the rest, and where two or more shown blocks carry "
    "comparable stakes, name more than one of them. Never crown a subject "
    "merely to make the sentence shorter. "
    "PLURAL IS NOT LONGER. The 90-character cap does not relax because you "
    "named three things. Name each subject by its shortest identifying word, "
    "cut the connective scaffolding (\"amid\", \"as\", \"while <X> "
    "continues\"), and drop the risk noun before you drop a subject. A "
    "headline that runs past 90 characters has chosen padding over a name. "
    "NO STANDING FORMULA. A subject, a driving verb and a risk noun — "
    "\"<X> drives <risk>\", \"<X> sustains elevated <risk>\" — is BANNED AS A "
    "HABIT. A risk word may appear where it is the most accurate word "
    "available; it may NEVER be the grammar you reach for by default, and "
    "swapping the verb or the risk word does not make it a different "
    "headline. Where a prior read is shown to you, do not rebuild its "
    "headline with today's nouns. "
    "A LONGER SUBJECT IS NOT A PLURAL HEADLINE. \"<X>, <Y> and <Z> drive "
    "<risk>\" is the SAME banned formula with three names in front of one "
    "abstract verb; you have lengthened the subject, not changed the "
    "sentence. "
    "PREDICATE ON THE WORLD, NOT ON THE RISK. The verb belongs to the EVENT: "
    "say what a subject DID or what happened to it — struck, closed, "
    "resigned, halted, widened, held, reopened — never what it does to a "
    "risk level. A reader learns something from a verb about the world and "
    "nothing from a verb about their own alarm. "
    "IT MUST STILL SAY SOMETHING. The opposite failure is a headline that "
    "names nobody and asserts nothing — a masthead with a date, or a "
    "universal that would be true on any day of the year. Every headline "
    "names at least ONE concrete subject A READER COULD POINT AT — a place, "
    "an actor, a government, an institution — in the words the shown blocks "
    "use. An abstraction is not a subject: \"energy-security pressures\", "
    "\"kinetic triggers\", \"regional flashpoints\" name nobody in more "
    "words, and a headline built out of them has un-crowned the driver "
    "without saying anything in its place. "
    "THE HEADLINE IS UNDER TRACEABILITY: every subject, place, figure and "
    "date in it must appear in a block you cite in the body, and it states no "
    "fact the body has not already grounded."
)


def _body_shape(sections: str, *, title_rule: str = _TITLE_RULE_UNIT) -> str:
    """The ONE body-structure spec, rendered for one layer's section list.

    The MECHANICS (header placement, no glued headers, no bold pseudo-headers,
    the title cap, title != BLUF) are byte-identical across layers — they are
    what W4 measured breaking: five house styles at the unit layer alone, 21/701
    findings with a header glued to the end of a sentence, 48/701 titles over
    120 chars, 58 titles containing the BLUF verbatim, and one narrative_
    coordination finding whose body landed in the title field. Only the SECTION
    LIST differs, because a unit answers a bounded question and a composition
    argues across units.

    TITLE-FRAME-FIX (2026-09-01) adds ONE more axis of difference:
    ``title_rule``. The title MECHANICS above are still shared and still
    byte-identical — the 90-char cap, "not the BLUF", "not the body" appear
    verbatim in both rules — but WHAT THE HEADLINE IS FOR now differs by layer,
    because the two layers have measurably different headline defects (review
    §2.a vs §1.7) and one of them is a nine-descriptor paste that this train
    deliberately does not move. Defaulting to :data:`_TITLE_RULE_UNIT` keeps
    every existing caller byte-identical.
    """
    return (
        "BODY SHAPE. Emit exactly these parts, in this order, and NO section "
        f"that is not on this list: {sections} "
        "Every '##' header sits ALONE on its own line with a blank line before "
        "it — NEVER glued to the end of the preceding sentence, NEVER replaced "
        "by a bold pseudo-header ('**Key points**'), and NEVER emitted at a "
        "deeper level ('###'). Do NOT run the whole body together as a single "
        f"paragraph with inline labels. {title_rule}"
    )


#: The unit-layer section list (W4/D4). The section NAMES are a deliberate
#: change from ``Key points / Assessment / Indicators to watch``: the old
#: "Assessment" slot invited a restatement of the severity tag and the
#: trajectory enum (W2), while ``## Why it matters`` asks for the mechanism and
#: ``## What would change this read`` asks for the falsifier the house standard
#: (ANALYTIC_PREAMBLE rule 7) has always required and got in 5/701 findings.
UNIT_BODY_SHAPE: str = _body_shape(
    "(1) the as-of line, in italics, alone on the first line; "
    "(2) '**BLUF:**' followed by ONE sentence; "
    "(3) '## What changed' — what this window shows that the prior read did "
    "not, each point cited [N] and dated; "
    "(4) '## Why it matters' — the MECHANISM, in connected prose: what the "
    "cited facts do to the thing your bounded question is about, and what caps "
    "or reverses them; "
    "(5) '## What would change this read' — the ONE observation that would "
    "most move your verdict, the CLASS of reporting that would carry it "
    "(name a class — 'IAEA reporting', 'port-call data' — never a specific "
    "outlet you cannot see), and whether this desk collects that class; "
    "(6) '## Indicators' — the short bulleted signposts."
)

#: The composition-layer section list (W6/D6). ``## Coverage`` is where the
#: unit roll-call goes: rule (h)'s integrity guarantee (never silently drop a
#: shown unit) is preserved, but as a FOOTER rather than as an equal-airtime
#: obligation that ate the whole body in 94/117 compositions.
#:
#: L3-12 (VOICE-4): section 5 said "the ones with no read at all this cycle",
#: which MANDATED a false self-report — a dimension whose read exists but sat
#: below the verification floor is not a dimension with "no read at all", and
#: the C4 audit's atom 10 is exactly that sentence published as a world fact.
#: "no verified read CARRIED" is true of BOTH withheld classes at once, so the
#: one-line section spec stops forcing a choice the spec cannot see; the
#: per-class vocabulary (below-verification-floor vs unassessed gap) is set
#: downstream by ``_coverage_rule`` against FRAME-1's deterministic COVERAGE
#: LEDGER, which is the only surface that actually knows which class a
#: dimension is in. ``UNIT_BODY_SHAPE`` is deliberately NOT changed: a unit
#: really does have a slice, and its coverage semantics differ.
#:
#: TITLE-FRAME-FIX (2026-09-01) changes TWO things here and nothing else.
#:
#: (a) The TITLE rule becomes :data:`_TITLE_RULE_COMPOSITION` — see that
#: constant for the measurement. This is a COMPOSITION-ONLY swap;
#: :data:`UNIT_BODY_SHAPE` above keeps ``_TITLE_RULE_UNIT`` byte-for-byte.
#:
#: (b) §2's BLUF slot stops MANDATING one thread. The old wording — "ONE
#: sentence naming the single most consequential thing" — is the sentence that
#: sits adjacent to the TITLE rule in the assembled prompt and supplies the
#: singular subject the frame then predicates on (review §2.a). Retargeting the
#: title while leaving this line intact would have the model resolve the
#: contradiction toward the more emphatic of the two, so the pair moves
#: together.
#:
#: WHAT §2 DELIBERATELY DOES NOT DO. It does not become "list the threads".
#: Review §5.1 Option 2's own stated risk is that "an unranked N-thread head is
#: worse than one wrong driver — it re-creates W6's 'coverage instead of
#: judgment' in a new costume", and §3.4 C3 rules that refusing to state a
#: bottom line "abdicates a read". So the slot still demands a BOTTOM LINE and
#: now demands WEIGHTS: threads carry relative weight against each other, the
#: earned single-thread state stays legal, and an unweighted list is named as
#: the failure it is.
COMPOSITION_BODY_SHAPE: str = _body_shape(
    "(1) the as-of line, in italics, alone on the first line; "
    "(2) '**BLUF:**' — the bottom line in ONE or TWO sentences: the two or "
    "three things that most define this window and how they WEIGH against "
    "each other, or ONE thing alone where the evidence genuinely concentrates "
    "there. Weighted, never merely listed — say which carries more and why; an "
    "unranked roll of threads is a roll call, not a bottom line. \"No single "
    "thread dominates this cycle\" is itself a legitimate bottom line when the "
    "shown blocks say so; "
    "(3) '## The picture' — AT MOST THREE paragraphs of connected argument, "
    "ordered by consequence, saying what these blocks TOGETHER show that none "
    "shows alone. Not a paragraph per block, not a bullet per block, and never "
    "a sub-claim restated verbatim; "
    "(4) '## Tension' — name any two blocks pointing different ways, or state "
    "plainly that they agree; "
    "(5) '## Coverage' — a SINGLE closing line naming the shown blocks whose "
    "read was unremarkable and the ones with no verified read carried this "
    "cycle.",
    title_rule=_TITLE_RULE_COMPOSITION,
)

#: D2 — ask for the JUDGMENT, not the sentence. Ships the replacement SHAPE
#: (driver + direction + pace + brake) in the same breath as the ban: a ban on
#: its own produces "the principal vector", which is the same defect with a
#: thesaurus.
UNIT_VERDICT_RULE: str = (
    "STATE THE VERDICT IN YOUR OWN WORDS. Name the driver and the direction as "
    "a claim a reader could disagree with — never by reciting a category "
    "label. Say what is MOVING, in which DIRECTION, at what PACE, and WHAT IS "
    "HOLDING IT BACK: the actor, commitment, constraint, or cost that caps it, "
    "taken from the cited evidence. Where the direction is genuinely flat, say "
    "what is holding it flat and name the ONE development that would break it. "
    "BANNED PHRASES — do NOT write \"the most plausible near-term "
    "trajectory\", \"the dominant <thing> vector is\", or a bare \"steady "
    "tension\". Those are the question's vocabulary, not an answer; a reader "
    "learns nothing from them, and a war, a border-patrol pattern and a "
    "presidential insult match must not be narrated in the same six words. Set "
    "the internal category in 'tags' / 'indicators' and keep the prose human. "
    "PROPORTION. Your language must scale to the stakes actually cited. "
    "Reserve the strongest verbs for cited casualties, cited materiel "
    "movement, or cited irreversible steps; a recalled ambassador and an "
    "active air campaign must not read alike. This is a rule about MATCHING "
    "your language to the evidence — it is NEVER a licence to add stakes, "
    "atmosphere, scene-setting, an inferred motive, or an adjective doing the "
    "work a cited number should do. If the magnitude is not in a cited signal, "
    "it is not in your prose."
)

#: D3 — bind the absence rule to the BLUF slot, and pin confidence to the
#: OBSERVATION. The unit prompts already win this argument in the bullets and
#: lose it in the headline: 336/701 BLUFs assert an absence and only 40 are
#: collection-scoped. The live case this exists to stop is proliferation_watch
#: reporting confidence 0.92 that a nuclear-weapons state shows "no observable
#: progress in nuclear or broader WMD capabilities" — inferred from the topic
#: being absent from a general-news feed.
UNIT_BLUF_ABSENCE_RULE: str = (
    "THE BLUF IS UNDER THE ABSENCE RULE TOO. The BLUF is written last and "
    "compresses everything below it, which is exactly where a scope qualifier "
    "gets dropped. If your verdict is a NEGATIVE, the BLUF ITSELF must carry "
    "the collection scope — \"no <X> appears in this desk's collection through "
    "<the as-of date>\" — never a bare world-fact (\"there is no <X>\", "
    "\"<country> is not subject to <X>\"). "
    "CALIBRATE CONFIDENCE TO THE OBSERVATION, NOT TO THE WORLD. High "
    "confidence that a topic is ABSENT FROM THIS SLICE is not high confidence "
    "that it is absent in reality, and your confidence number must be the "
    "second one. When this desk's collection is general reporting and your "
    "bounded question turns on specialist monitoring — safeguards status, "
    "enrichment, force basing, reserve data, platform-level activity — the "
    "honest confidence in the WORLD-claim is LOW: cap it there, say plainly "
    "which CLASS of reporting would be needed to raise it, and say whether "
    "this desk collects that class at all. A slice full of signals about the "
    "wrong subject corroborates nothing — signal count is not evidence. When "
    "many signals bear on the desk but few bear on your question, that is a "
    "COVERAGE result, not a NEGATIVE result: say so in those words."
)

#: X4 / ESC (W11-F52) — EVERY FACT-STATING SENTENCE CARRIES ITS MARKER, and the
#: BLUF is one of them. Pasted VERBATIM, as its own paragraph ahead of the
#: response-schema paragraph, into the NINE bounded-unit descriptors - first
#: ``escalation`` and ``military_posture`` (X4), then, once the 10-15 after-read
#: on those two held (uncited fact share 0.508 -> 0.094 and 0.571 -> 0.082,
#: judge-path faithfulness inside the -0.03 guard), the other seven (W11-F86,
#: 2026-10-24): internal_stability, economic_coercion, energy_security,
#: leadership_transition, narrative_coordination, proliferation_watch and the
#: draft disruption_status. ``tests/data_pkg/test_esc_cited_sentences.py`` pins
#: descriptor-vs-constant equality on the nine and absence on every other
#: analyst descriptor. Still deliberately NOT folded into
#: :data:`UNIT_READ_CONTRACT`: the descriptors carry the paragraph, so a live
#: head moves only when its own descriptor does. MEASURED (7 d to 2026-10-13, read-only, ``uncited_share_fields`` over
#: the persisted bodies): 50.8 % of escalation's and 57.1 % of military_posture's
#: fact-asserting sentences carried no marker, and the BLUF was uncited in 67.9 %
#: / 82.9 % of reads. The LLM judge grades those sentences as synthesis and
#: forgives most of them; the deterministic floor counts every one as
#: ``no_citation`` (``verify._is_fact_asserting`` treats any prose sentence as a
#: claim unless it is scaffolding, a scoped absence, forward-looking, or a
#: labelled synthesis opener that names no particular) - so a floor-only read
#: (judge down) lands at 0.45-0.56 on the same bodies the judge scores 0.74-0.80.
#: The rule therefore asks for a marker on every sentence of the two sections
#: that carry the read, not only on the ones that name something: a looser
#: statement of the line would leave the model guessing where the floor draws it.
UNIT_CITED_SENTENCE_RULE: str = (
    "EVERY SENTENCE THAT STATES A FACT CARRIES ITS MARKER. The verification "
    "pass reads your body one sentence at a time, and a sentence that states "
    "anything about the world and carries no [N] marker is scored as an "
    "uncited claim — in any section, and in the BLUF above all, because the "
    "BLUF is the sentence a reader takes away. Three consequences. "
    "(1) THE BLUF IS A CITED SENTENCE: it ends with the [N] marker(s) of the "
    "signals that carry the facts it names, like any other claim "
    "('**BLUF:** <the verdict, naming who is doing what> [N][N].'). A BLUF "
    "that names nothing is the bare category label the verdict rule bans, so "
    "there is no uncited form of it worth writing (a BLUF that is itself a "
    "scoped negative follows the exception below). "
    "(2) EVERY SENTENCE IN '## What changed' AND IN '## Why it matters' ends "
    "with the marker(s) of the signals it reports or reasons from — analysis is "
    "not exempt, because the grader cannot tell your inference from a fact you "
    "left unsourced, and a sentence that rests on nothing you can cite is a "
    "sentence to cut. ('## What would change this read' and '## Indicators' "
    "look forward and need none.) "
    "(3) Write each marker in ASCII brackets at the end of the sentence, [N] "
    "or [N][N] — never in full-width brackets and never as a bare number. "
    "A scoped negative, written in the house form (\"no <X> appears in this "
    "desk's collection through <date>\"), has nothing to cite and carries no "
    "marker; keep it scoped and keep the \"no\" at its head. Before you "
    "emit, re-read the body once as a grader would, and for every sentence "
    "without a marker either add the signal it rests on or cut it: a fact you "
    "cannot point to is a fact you may not state."
)

#: D5 — the pipeline's own bookkeeping is being read aloud as intelligence:
#: microsecond ISO timestamps in 56/117 compositions, ``intensity NN.NN`` /
#: event counts in 27/117, and raw ``effective_confidence`` numbers sorted and
#: narrated as if they were findings. Every one of these is a prompt line taken
#: literally, so the fix is to say which side of the line each number lives on.
NO_INSTRUMENT_READINGS: str = (
    "NO INSTRUMENT READINGS IN PROSE. Confidence values, effective_confidence, "
    "salience scores, a situation's intensity score and event_count, indicator "
    "'status' / 'horizon_date' / 'first_seen' fields, id slugs, supersession "
    "and freshness advisories, run ids and internal analyst_ids are the "
    "LEDGER, not the read: USE them to decide, never PRINT them. They already "
    "live in the structured fields that carry them, where the UI and the "
    "calibration tracker read them. In prose, say the same thing in words a "
    "reader outside this system would use — \"the Russia read is the "
    "better-supported of the two\", not \"effective confidence 0.889 vs "
    "0.800\". "
    "RENDER DATES AS A HUMAN WOULD. Every date you cite is a plain calendar "
    "date — \"3 August\", or \"3 August 09:15 UTC\" when the hour matters — "
    "NEVER a raw ISO or microsecond timestamp copied out of a field (never "
    "\"2026-08-03T09:15:53.997707+00:00\")."
)

#: FRAME-3 — SEVERITY AS STATE (``planning/FRAME_PROGRAM_2026-08-20.md`` §0.6,
#: §7 train 3). The C-B driver, and the only one of R1's findings that lives
#: entirely in a single tag's MEANING: a desk tagged the severity of its SLICE
#: DELTA, so a war in its fourth month tagged ``severity:low`` on a week that
#: added nothing to it, the scorecard banded the dimension ``low`` off that tag,
#: and 37/37 non-exact bands sat BELOW the reference. Splitting the tag is the
#: repair — the standing level in ``severity``, the movement in
#: ``severity_delta`` — and it must land on every desk in ONE train, because a
#: scorecard whose seven dimensions mix two meanings of the same word is worse
#: than one that is uniformly wrong.
#:
#: It lives INSIDE the house contract rather than in nine per-unit tag
#: paragraphs for the reason the contract exists: the per-unit ``"tags"``
#: schema lines are already worded nine different ways, and a rule about what a
#: shared field MEANS cannot be one of the things that varies by desk.
#:
#: THE LAST SENTENCE IS NOT DECORATION. Every unit descriptor already carries a
#: paragraph of the form "the tags array MUST contain the topic tag X PLUS
#: EXACTLY ONE severity tag", which reads as an EXHAUSTIVE list, and the JSON
#: schema example above it shows exactly those two. A model handed that plus a
#: new required tag has a genuine conflict to resolve, and the cheapest
#: resolution is to drop the new one — which would make this whole train a
#: no-op the tests could not see. So the rule states its own precedence, once,
#: instead of nine bespoke edits to nine differently-worded schema paragraphs.
SEVERITY_AS_STATE_RULE: str = (
    "SEVERITY IS THE STANDING STATE, NOT THIS SLICE'S MOVEMENT. Your "
    "'severity:<level>' tag records WHERE YOUR DIMENSION STANDS on this desk "
    "right now — the condition a reader would find if they looked today — NOT "
    "how far it moved in the last 72 hours. A war running for months is "
    "'severity:high' in a week that added nothing to it; a calm desk where one "
    "official traded an insult is 'severity:low' even though that insult is "
    "the only thing that changed. Tag the STATE, and tag the MOVEMENT "
    "SEPARATELY: alongside your severity tag emit EXACTLY ONE "
    "'severity_delta:<rose|fell|steady|new>' tag — 'rose' or 'fell' when this "
    "slice's cited evidence moves the standing level up or down, 'steady' when "
    "you checked it against your prior read and the level held (a HIGH level "
    "that held is 'severity:high' + 'severity_delta:steady', never a demotion), "
    "and 'new' when you have no prior read of this dimension to compare "
    "against — never 'steady', which claims a comparison you did not make. The "
    "pair is the point: 'severity:high' + 'severity_delta:steady' is a serious "
    "condition that is still running, while 'severity:low' + "
    "'severity_delta:rose' is a quiet desk that just twitched, and downstream "
    "they must not read alike. THIS TAG IS REQUIRED IN ADDITION to every tag "
    "your output schema below lists: where that schema names the tags your "
    "'tags' array must contain, read it as that list PLUS this one — a 'tags' "
    "array with no 'severity_delta:' entry is incomplete however exhaustive the "
    "schema's own wording sounds. Both tags are STRUCTURED FIELDS: never print "
    "either one, or its level, in the prose — '## What changed' is where the "
    "movement gets said in words."
)

#: The block pasted VERBATIM into all nine bounded-unit descriptors. Order is
#: the order a writer needs it: shape, then verdict, then the tag split the
#: verdict feeds, then the two rules that most often break at the BLUF. The
#: AS-OF rule is NOT here — it is code-appended to every unit via
#: ``unit_grounding.UNIT_GROUNDING_CLAUSE``, so it needs no descriptor copy at
#: all.
UNIT_READ_CONTRACT: str = "\n\n".join(
    (
        "HOUSE READ CONTRACT — identical on every desk. Follow it; do not "
        "restate it in your output.",
        UNIT_BODY_SHAPE,
        UNIT_VERDICT_RULE,
        SEVERITY_AS_STATE_RULE,
        UNIT_BLUF_ABSENCE_RULE,
        NO_INSTRUMENT_READINGS,
    )
)


# ---------------------------------------------------------------------------
# MA4 — the D6 wave's TITLE amendment, unit layer ONLY
# ---------------------------------------------------------------------------
#
# L2-11: units were opening 'title' with the as-of line, so the headline said
# the run date instead of the verdict. The repair is one sentence spliced into
# the TITLE rule.
#
# WHY IT IS SPLICED HERE AND NOT WRITTEN INTO ``_body_shape``. The TITLE
# mechanics are SHARED — ``_body_shape`` renders them for the unit layer AND
# the composition layer — but the two layers bind differently (see the module
# header): compositions take the text by IMPORT, so editing ``_body_shape``
# would rewrite four live composition prompts in the image and make this a code
# deploy. The D6 wave is descriptor-only. So the amendment is applied to the
# UNIT-layer copies alone, and ``COMPOSITION_BODY_SHAPE`` keeps the unamended
# mechanics it ships with today.
#
# NOTHING AT RUNTIME READS THESE. Like :data:`UNIT_READ_CONTRACT` itself, they
# are the canonical paste-source and the anti-drift anchor that
# ``tests/data_pkg/test_voice_contract.py`` pins descriptors against.

#: The MA4 sentence, verbatim as the nine D6 drafts carry it.
TITLE_NOT_THE_AS_OF_LINE: str = (
    "It is NEVER the as-of line and never begins with 'As of'."
)

#: The seam MA4 splices at — the end of the TITLE rule's verdict clause, and
#: the start of the sentence that sends the body back to its own field.
_TITLE_AMENDMENT_ANCHOR: str = "verdict in a few words. "


def _with_title_amendment(text: str) -> str:
    """Splice :data:`TITLE_NOT_THE_AS_OF_LINE` into a unit-layer shape spec.

    Raises rather than returning the text unchanged when the anchor is gone:
    a silent no-op here would let ``_body_shape``'s TITLE wording be reworded
    while the amended constants quietly stopped carrying the amendment, and
    the descriptor pins below would still pass against a contract that no
    longer says the thing L2-11 needs it to say.
    """
    if text.count(_TITLE_AMENDMENT_ANCHOR) != 1:
        raise ValueError(
            f"MA4 anchor {_TITLE_AMENDMENT_ANCHOR!r} occurs "
            f"{text.count(_TITLE_AMENDMENT_ANCHOR)}x, expected exactly 1 — "
            "the TITLE rule in _body_shape() was reworded; re-derive the "
            "splice point before trusting the amended constants"
        )
    return text.replace(
        _TITLE_AMENDMENT_ANCHOR,
        _TITLE_AMENDMENT_ANCHOR + TITLE_NOT_THE_AS_OF_LINE + " ",
        1,
    )


#: :data:`UNIT_BODY_SHAPE` as the D6-flipped descriptors carry it.
UNIT_BODY_SHAPE_D6: str = _with_title_amendment(UNIT_BODY_SHAPE)

#: :data:`UNIT_READ_CONTRACT` as the D6-flipped descriptors carry it. The ONLY
#: difference from the pre-D6 contract is :data:`TITLE_NOT_THE_AS_OF_LINE`;
#: every other byte is identical, which is what makes MA4 auditable as a
#: one-sentence change rather than a contract rewrite.
UNIT_READ_CONTRACT_D6: str = _with_title_amendment(UNIT_READ_CONTRACT)


#: The shared spine of D1 — the dated-claim obligation, identical at both
#: layers. Split out from :func:`as_of_rule` only because the OPENING sentence
#: differs (a unit takes its as-of from the slice header, a composition from the
#: newest ``produced_at`` printed on a shown block) while everything after it is
#: one rule.
#:
#: The mechanical cause D1 targets: the run date IS in the prompt (grounding's
#: "AUTHORITATIVE CURRENT CONTEXT (as of {today} …)" header), the one clause
#: that mentions time FORBIDS the only deixis the model has ("NEVER on 'today',
#: 'now', 'as of this run'"), and nothing ever tells the model to print a date.
#: So it resolves the conflict by dropping temporal reference entirely: 13.8% of
#: findings carry any calendar date and 2.6% of BLUF lines do.
#:
#: FAITHFULNESS-SAFE BY CONSTRUCTION. Every date this rule asks for is COPIED
#: from text already rendered in the slice and therefore already in the verify
#: judge's working text. The rule ADDS verifiable surface; it cannot invent.
_DATED_CLAIM_RULE: str = (
    "Then: EVERY load-bearing claim carries the date of the reporting that "
    "supports it, taken from that source's OWN printed date, written as a "
    "plain calendar date — \"on 2 August\", \"reported 31 July\" — and NEVER "
    "as \"recently\", \"currently\", \"now\", \"in recent days\", \"at "
    "present\", \"ongoing\", or \"in the current window\". When a signal shows "
    "only an ingestion date and no published date, say so ONCE (\"first "
    "collected 2 August; the underlying event is undated in the source\") "
    "rather than implying the event date. A claim you cannot date from the "
    "printed dates is a claim you must either DROP or mark explicitly as "
    "undated. When you state a horizon, give it in DAYS or WEEKS from the "
    "as-of date (\"within about ten days\", \"over the next three weeks\") — "
    "never a bare \"near-term\". "
    "ZERO NEW FACTS: every date you print must ALREADY APPEAR in the rendered "
    "evidence you were shown. Never compute a date, never infer one from an "
    "event you recognize, and never supply one from your own knowledge — a "
    "date you cannot point at is a fabrication exactly like any other."
)


#: V-N2 — the RETRIEVED-CONTEXT half of D1, for the analysts that fetch their
#: own evidence (four ``inline_target`` descriptors run a GATHER loop over
#: ``search_corpus`` / ``vector_search`` / ``read_document`` against a
#: ~106k-document corpus spanning years: cross_doc_corroborator,
#: corpus_researcher, country_assessor, and the UNIT disruption_status — this
#: rule is orthogonal to :data:`UNIT_READ_CONTRACT`, and that desk takes both).
#:
#: WHY THE DATED-CLAIM RULE ALONE IS NOT ENOUGH HERE. ``_DATED_CLAIM_RULE``
#: closes with "every date you print must ALREADY APPEAR in the rendered
#: evidence you were shown" — which a retrieved document satisfies trivially,
#: because it IS rendered evidence and shares one flat ``[N]`` numbering space
#: with the cadence slice. So the rule that guards against inventing a date says
#: nothing about MOVING one: taking a date out of a document retrieved from the
#: archive and attaching it to the event the fresh signals describe passes every
#: check ``_DATED_CLAIM_RULE`` makes.
#:
#: Two distinctions this states explicitly, because the render alone cannot:
#:   1. WHEN THE REPORTING RAN vs WHEN THE EVENT HAPPENED. A story published
#:      today about a strike in 2023 carries both dates; they are not
#:      interchangeable, and a funeral held today for people killed in 2023 is
#:      correctly written with BOTH.
#:   2. THE SLICE vs THE ARCHIVE. A retrieved document is context for judging
#:      the claim, not evidence that its contents are current.
#:
#: FAITHFULNESS-SAFE: it removes a licence rather than granting one, and every
#: date it permits is still copied from rendered text.
RETRIEVED_CONTEXT_RULE: str = (
    "RETRIEVED DOCUMENTS ARE CONTEXT, NOT A CLOCK. Some numbered blocks are "
    "documents YOU fetched (they are marked RETRIEVED and print their own "
    "collection and publication dates). They come from the full archive and "
    "may be ANY age — years old — while the cadence slice is recent. They "
    "share one [N] numbering with the slice, so nothing but those printed "
    "dates tells them apart: READ THEM before you date anything. "
    "SEPARATE THE REPORTING DATE FROM THE EVENT DATE. A document's published "
    "date is when the REPORTING ran; the event it describes may be far older. "
    "Never let one stand in for the other, and never carry a date out of a "
    "retrieved document onto a DIFFERENT event that your fresh signals "
    "describe. When both dates matter, write both and say which is which — "
    "\"a funeral held 4 August for people killed on 23 November 2023 [N]\" — "
    "and when you lean on an older document for background, ATTRIBUTE it in "
    "the prose (\"a November 2023 report describes …\", \"reporting from "
    "March 2024 recorded …\") so a reader can never mistake archive material "
    "for current reporting. "
    "AGE IS EVIDENCE. An old document corroborates what was true WHEN IT WAS "
    "WRITTEN, not what is true now: it can establish that a claim has been "
    "made before, or that a figure was once reported — it cannot confirm a "
    "present state on its own. If the only support for a current claim is a "
    "document older than your slice window, say so plainly and lower your "
    "confidence rather than presenting it as corroboration."
)


#: Q4 (2026-09-28) — THE CARRIED DATED ANCHOR: the THIRD member of the family
#: above, and the one that cost three months of a desk's credibility.
#:
#: THE LIVE CASE (consult review 2026-09-28 §A). ``leadership_transition`` on
#: ``country_g20_fr`` wrote, at confidence 0.9: *"the presidential hand-over
#: from Emmanuel Macron to his successor is virtually certain on 28 September
#: 2026"*. Nothing in its 120-row slice names that date or that event — the
#: slice held Senate elections, a remark about a term-limited departure, and
#: the Pope. The date was nevertheless ALREADY IN THE RENDERED EVIDENCE,
#: because it sat in the PRIOR READ block, and a prior read is a CITABLE ref:
#: the faithfulness judge scored the sentence 1.00 twice (5/5 supported), the
#: WINDOW LEDGER re-fed it every cycle, and 24 findings since 07-01 read "No
#: material change: the scheduled 28 September handover remains" with the
#: countdown ticking and the confidence rising.
#:
#: WHY ``_DATED_CLAIM_RULE`` CANNOT CATCH IT — the same hole
#: :data:`RETRIEVED_CONTEXT_RULE` exists to close, one block-kind over. That
#: rule closes with "every date you print must ALREADY APPEAR in the rendered
#: evidence you were shown", which a prior read that printed the date LAST
#: CYCLE satisfies trivially. A rule against INVENTING a date says nothing
#: about a date that survives by being COPIED FORWARD, and from the inside a
#: fabrication laundered through one's own memory is indistinguishable from a
#: well-grounded carry.
#:
#: SO THE RULE IS ABOUT WHICH BLOCK MAY CARRY A NUMBER, never about whether a
#: number may be present: MEMORY blocks re-state, only SIGNAL blocks
#: re-establish. The block's OWN STAMP is the one exception and it has to be,
#: because the house's licensed continuity idiom is built on it ("no material
#: change since the 3 August morning read [N]") — that date is a fact about
#: THE RECORD, which the block genuinely does establish.
#:
#: FAITHFULNESS-SAFE: like both its siblings it REMOVES a licence rather than
#: granting one, and names the two honest moves so the rule is followable —
#: a bare prohibition here would cost the desk its continuity voice.
CARRIED_ANCHOR_RULE: str = (
    "A CARRIED BLOCK IS MEMORY, NOT EVIDENCE, FOR A DATE OR A FIGURE. The "
    "PRIOR READ and the WINDOW LEDGER tell you WHAT THIS DESK SAID BEFORE. "
    "They are not reporting and they cannot re-establish a fact: everything "
    "in them that is not their own stamp is LAST CYCLE'S CLAIM — every date "
    "and every number inside one included. So a DATE (a deadline, a scheduled "
    "event, a hand-over, a countdown, an anniversary) or a FIGURE (a count, a "
    "total, a share, a casualty number) may be ASSERTED — in the BLUF or "
    "anywhere in the body — ONLY where a SIGNAL block you cite IN THAT SAME "
    "SENTENCE carries it. A prior-read handle or a window-ledger handle is "
    "NOT that support, however exactly the number appears inside it. "
    "THE ONE EXCEPTION IS THE BLOCK'S OWN STAMP. The date a prior read was "
    "PRODUCED, and the dates printed on the ledger's own lines, are facts "
    "about this record: state them and cite them exactly as you do today — "
    "'no material change since the 3 August morning read [N]', 'mass protests "
    "recorded on 7 August [N] have since subsided'. That is the licensed "
    "continuity idiom and this rule does not touch it. "
    "WHEN THIS WINDOW DOES NOT RE-SOURCE A CARRIED DATE OR FIGURE you have "
    "TWO honest moves and no third. Report it AS a carry, in these words — "
    "'carried from the prior read, not re-sourced this window' (or 'carried "
    "from the window ledger, not re-sourced this window') — or DROP it. "
    "NEVER let a date or a number survive a cycle by being copied forward as "
    "though it were established: a scheduled date no signal has restated does "
    "not become truer by being repeated, and your confidence in it must FALL "
    "as it ages, never rise."
)

#: The SAME rule, compressed to one sentence and rendered ON the memory blocks
#: themselves (``unit_grounding._render_prior_read`` and the unit-scoped
#: ``window_ledger.ledger_block_lines`` header), because the clause above is
#: read once at the top of a long system prompt and the temptation arrives
#: 8,000 tokens later, at the moment the model is looking straight at the
#: carried date. One definition, two render sites, no drift.
CARRIED_ANCHOR_BLOCK_NOTE: str = (
    "MEMORY, NOT EVIDENCE: any date or figure here but this block's own stamp "
    "is LAST CYCLE'S CLAIM — re-assert it only from a SIGNAL block cited this "
    "window, or mark it 'not re-sourced'."
)

#: THE LICENSED DISCLOSURE TOKEN, generated once so the prompt that asks for it
#: and the verify-side guard that exempts it cannot drift apart on the wording.
#: Deliberately the SHORT form: the full rule offers "carried from the prior
#: read, not re-sourced this window" and the block note offers "not
#: re-sourced", and a guard keyed on the short token accepts both spellings
#: plus the ledger variant.
#:
#: ``data.provenance`` may not import ``data.analysts`` (the edge runs the
#: other way), so ``provenance.grounding_backstops`` RESTATES this literal and
#: a drift-guard test holds the two equal — the posture
#: ``verify._REGISTER_REF_KIND`` already takes for the register's ref kind.
CARRIED_ANCHOR_DISCLOSURE: str = "not re-sourced"


def as_of_rule(anchor: str) -> str:
    """The AS-OF + dated-claim rule, worded for one layer's date source.

    ``anchor`` completes the as-of line's form and names WHERE its three
    values come from — which is the whole reason this is a function and not a
    constant: a unit reads them off the rendered slice header, a composition
    off the blocks it was shown, and pointing either layer at the other's
    source would be an instruction it cannot follow.
    """
    return (
        "AS-OF AND WINDOW. Open the body with an AS-OF LINE, before the BLUF, "
        f"alone on the first line and in italics: {anchor} {_DATED_CLAIM_RULE}"
    )


#: D7 — the most serious analytic defect the diagnostic found: the top of the
#: tower ranks the world by how well-sourced a finding is rather than by how
#: much it matters. ``effective_confidence`` is the only comparable number the
#: block render prints, "most consequential" appears in no prompt, so the model
#: reaches for the number it has. The live result was Pakistan's SH-15
#: procurement — tagged severity:low by its own desk, described there as
#: "corroboration but no material change" — beating an active war in Sudan, a
#: closed Strait of Hormuz, and a confirmed kinetic strike, as the world's top
#: risk. Applied to every ranking prompt (country, region, world, thematic):
#: the world read inherits its ordering from the layer below it, so fixing only
#: the top would leave the sort key intact one floor down.
CONSEQUENCE_RULE: str = (
    "CONSEQUENCE, NOT CONFIDENCE. When you decide what leads and what follows, "
    "rank on the STAKES the cited blocks describe, in this order: (1) cited "
    "loss of life or ongoing armed conflict; (2) cited disruption to a system "
    "many actors depend on — a closed chokepoint, a severed supply, a "
    "collapsed institution; (3) cited irreversibility — a step that cannot be "
    "walked back; (4) cited proximity — something already happening outranks "
    "something that might. The rubric is MAGNITUDE x PROXIMITY: a large effect "
    "already underway outranks a larger effect that is merely possible. "
    "effective_confidence and salience tell you HOW FIRMLY YOU MAY STATE a "
    "thing, NEVER HOW MUCH IT MATTERS — a 0.62 confirmed strike outranks a "
    "0.89 procurement contract. Carry the SEVERITY the underlying desk "
    "assigned: a block whose own unit called it \"no material change\" or "
    "\"routine\", or tagged it severity:low, may NOT be your lead however "
    "well-sourced it is. \"Holding steady\" is the one exception, and it is a "
    "different claim: it describes the DELTA, never the stakes — a block whose "
    "standing severity is high and whose severity_delta is steady is a "
    "continuing high-stakes condition and stays fully eligible to lead. "
    "Confidence may be DISPLAYED in words where it changes how a reader "
    "should act on a claim, but it is "
    "NEVER the ordering key and never a sorted list. If your lead is not the "
    "highest-stakes item shown, say why in ONE clause. "
    "STANDING PICTURE BEFORE DELTA. Open with what is TRUE AND CONTINUING — "
    "the highest-stakes conditions the blocks describe, whether or not they "
    "changed this cycle — and only THEN say what moved since the prior read. A "
    "quiet cycle over a burning situation is \"the war continues, unchanged "
    "since <date>\", NOT a new headline about the largest-moving small thing. "
    "Novelty is not consequence: do NOT promote an item to the lead because it "
    "is the thing that changed."
)


#: FRAME-3, composition half. The unit rule above changes what ``severity=``
#: MEANS on a rendered block, and the block now prints ``severity_delta=``
#: beside it — so the layer that reads them has to be told, or FRAME-3 lands the
#: fix at the unit layer and loses it one floor up. This is the D7 lesson
#: applied to a second field: a number rendered without a rule is a number the
#: model will order by, and a ``steady`` delta silently read as "nothing here"
#: would re-manufacture the exact class §0.6 exists to kill.
#:
#: Appended to ALL FOUR composition prompts rather than folded into
#: :data:`CONSEQUENCE_RULE` (which the country prompt takes only in reduced
#: form) because the country composition is the layer that consumes UNIT heads
#: and therefore the layer that must not get this wrong.
SEVERITY_STATE_READ_RULE: str = (
    "READING severity AND severity_delta. A block may print two of its "
    "source's structured calls. 'severity=' is the STANDING STATE of that "
    "dimension — where it stands today, not how far it moved — and "
    "'severity_delta=' is what the source's own slice did to it: rose, fell, "
    "steady, or new. Read them as a PAIR. 'severity=high severity_delta=steady' "
    "is a serious condition that is still running, and it outranks "
    "'severity=low severity_delta=rose' however loudly the second one moved: "
    "the delta tells you what belongs in '## What changed', the severity tells "
    "you what matters. A steady delta is NEVER a reason to demote, drop, "
    "soften, or bury a high-severity block. A block showing no "
    "'severity_delta=' carries no movement call at all — infer none, and do "
    "not read its absence as steady. Never print either field or its level; "
    "say the same thing in words."
)


__all__ = [
    "ANALYTIC_PREAMBLE",
    "ASSESSMENT_STANDARDS",
    "BANNED_PHRASE_MARKERS",
    "BANNED_TEMPLATE_PHRASES",
    "CARRIED_ANCHOR_BLOCK_NOTE",
    "CARRIED_ANCHOR_DISCLOSURE",
    "CARRIED_ANCHOR_RULE",
    "COMPOSITION_BODY_SHAPE",
    "CONSEQUENCE_RULE",
    "NO_INSTRUMENT_READINGS",
    "RETRIEVED_CONTEXT_RULE",
    "SEVERITY_AS_STATE_RULE",
    "SEVERITY_STATE_READ_RULE",
    "TITLE_NOT_THE_AS_OF_LINE",
    "UNIT_BLUF_ABSENCE_RULE",
    "UNIT_BODY_SHAPE",
    "UNIT_BODY_SHAPE_D6",
    "UNIT_CITED_SENTENCE_RULE",
    "UNIT_READ_CONTRACT",
    "UNIT_READ_CONTRACT_D6",
    "UNIT_VERDICT_RULE",
    "as_of_rule",
    "with_preamble",
    "with_preamble_if_absent",
]


#: THE ASSESSMENT CHANNEL'S ANALYTIC STANDARDS — one text, every Assessment
#: voice. It lived in ``assessment_prompts`` while there was one file holding
#: every voice; STEP E gave the world's v4 voice its own leaf module
#: (``assessment_prompt_world_v4``), and a standards block that two voice files
#: both interpolate is a fact about the CHANNEL rather than about either voice.
#: So it moves here, to the module that already holds every cross-layer prompt
#: constant, BYTE-IDENTICAL — the three system prompts that interpolate it are
#: unchanged to the character, which a hash test pins.
ASSESSMENT_STANDARDS: str = (
    "ANALYTIC STANDARDS, in this channel's terms. "
    "(1) SEPARATE OBSERVATION FROM JUDGMENT — what the record STATES is "
    "observed; what you make of it is assessed, and the sentence says which. "
    "(2) CALIBRATE — 'likely' where one block carries it, 'possibly' where the "
    "block is thin or below the floor, and never precision the record does not "
    "carry. The blocks show you their own verify scores; a block at 0.36 is not "
    "a block at 1.00 and your LANGUAGE must be able to tell them apart — the "
    "hedge is the place that difference shows, never a sentence about the "
    "score. "
    "(3) MIND TIME — every block carries its own produced-at and age. An older "
    "block is not today's news, and the record's as-of line is not each block's. "
    "(4) BE HONEST ABOUT GAPS — where the record is thin, say so plainly rather "
    "than padding; where two blocks disagree, surface the disagreement rather "
    "than averaging it away."
)
