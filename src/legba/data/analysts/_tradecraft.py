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
    "read was unremarkable and the ones with no read at all this cycle."
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

#: The block pasted VERBATIM into all nine bounded-unit descriptors. Order is
#: the order a writer needs it: shape, then verdict, then the two rules that
#: most often break at the BLUF. The AS-OF rule is NOT here — it is
#: code-appended to every unit via ``unit_grounding.UNIT_GROUNDING_CLAUSE``, so
#: it needs no descriptor copy at all.
UNIT_READ_CONTRACT: str = "\n\n".join(
    (
        "HOUSE READ CONTRACT — identical on every desk. Follow it; do not "
        "restate it in your output.",
        UNIT_BODY_SHAPE,
        UNIT_VERDICT_RULE,
        UNIT_BLUF_ABSENCE_RULE,
        NO_INSTRUMENT_READINGS,
    )
)


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
    "assigned: a block whose own unit called it \"no material change\", "
    "\"routine\", or \"holding steady\", or tagged it severity:low, may NOT be "
    "your lead however well-sourced it is. Confidence may be DISPLAYED in "
    "words where it changes how a reader should act on a claim, but it is "
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


__all__ = [
    "ANALYTIC_PREAMBLE",
    "BANNED_PHRASE_MARKERS",
    "BANNED_TEMPLATE_PHRASES",
    "COMPOSITION_BODY_SHAPE",
    "CONSEQUENCE_RULE",
    "NO_INSTRUMENT_READINGS",
    "RETRIEVED_CONTEXT_RULE",
    "UNIT_BLUF_ABSENCE_RULE",
    "UNIT_BODY_SHAPE",
    "UNIT_READ_CONTRACT",
    "UNIT_VERDICT_RULE",
    "as_of_rule",
    "with_preamble",
    "with_preamble_if_absent",
]
