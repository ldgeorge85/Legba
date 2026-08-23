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


def _body_shape(sections: str) -> str:
    """The ONE body-structure spec, rendered for one layer's section list.

    The MECHANICS (header placement, no glued headers, no bold pseudo-headers,
    the title cap, title != BLUF) are byte-identical across layers — they are
    what W4 measured breaking: five house styles at the unit layer alone, 21/701
    findings with a header glued to the end of a sentence, 48/701 titles over
    120 chars, 58 titles containing the BLUF verbatim, and one narrative_
    coordination finding whose body landed in the title field. Only the SECTION
    LIST differs, because a unit answers a bounded question and a composition
    argues across units.
    """
    return (
        "BODY SHAPE. Emit exactly these parts, in this order, and NO section "
        f"that is not on this list: {sections} "
        "Every '##' header sits ALONE on its own line with a blank line before "
        "it — NEVER glued to the end of the preceding sentence, NEVER replaced "
        "by a bold pseudo-header ('**Key points**'), and NEVER emitted at a "
        "deeper level ('###'). Do NOT run the whole body together as a single "
        "paragraph with inline labels. "
        "TITLE. 'title' is a headline of AT MOST 90 characters. It must NOT "
        "repeat the BLUF sentence, must NOT begin with 'BLUF', and must NOT be "
        "the body: it names the subject and the verdict in a few words. The "
        "body text belongs in 'body' and nowhere else."
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
COMPOSITION_BODY_SHAPE: str = _body_shape(
    "(1) the as-of line, in italics, alone on the first line; "
    "(2) '**BLUF:**' — ONE sentence naming the single most consequential thing "
    "on this desk and why it matters; "
    "(3) '## The picture' — AT MOST THREE paragraphs of connected argument, "
    "ordered by consequence, saying what these blocks TOGETHER show that none "
    "shows alone. Not a paragraph per block, not a bullet per block, and never "
    "a sub-claim restated verbatim; "
    "(4) '## Tension' — name any two blocks pointing different ways, or state "
    "plainly that they agree; "
    "(5) '## Coverage' — a SINGLE closing line naming the shown blocks whose "
    "read was unremarkable and the ones with no verified read carried this "
    "cycle."
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
    "BANNED_PHRASE_MARKERS",
    "BANNED_TEMPLATE_PHRASES",
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
    "UNIT_READ_CONTRACT",
    "UNIT_READ_CONTRACT_D6",
    "UNIT_VERDICT_RULE",
    "as_of_rule",
    "with_preamble",
    "with_preamble_if_absent",
]
