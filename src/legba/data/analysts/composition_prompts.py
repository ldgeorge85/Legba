"""The composition tier's PROMPT TEXT — four variants, one set of generators.

Extracted from :mod:`legba.data.analysts.meta_findings_synthesizer` (VOICE-4,
2026-08-21) under the module-size gate. The seam is the one the synthesizer's
own section banner already drew: everything here is prompt STRING construction
and nothing here touches the runtime, the substrate, or a row — the synthesizer
imports the finished constants and re-exports them, so every existing importer
(``synth._COMPOSITION_SYSTEM``, ``synth._continuity_rule``, the voice-contract
pins) resolves unchanged.

WHAT LIVES HERE, in the order a reader needs it:

  1. ``_SYSTEM_PROMPT`` — the legacy GLOBAL meta synthesis prompt. NOT a
     composition: no citations, no as-of line, no coverage footer. Kept
     byte-for-byte; the composition work below never touches it.
  2. The shared rule GENERATORS (``_hedge_rule``, ``_tension_rule``,
     ``_shape_rule``, ``_coverage_rule``, ``_continuity_rule``,
     ``_composition_as_of``). Generated per variant rather than pasted four
     times because a rule whose whole purpose is that every composition floor
     states it IDENTICALLY drifts on the first hand edit.
  3. The VOICE-4 doctrine blocks (the lens-3 defect family) — the tier's
     ORIENTATION, which runs before "RULES:" in every variant.
  4. The four assembled system prompts: country, region, world-over-regions,
     thematic.

THE ONE INVARIANT WORTH STATING TWICE: the strict-JSON envelopes at the end of
each prompt contain literal ``{`` and ``}``. Nothing may ever call ``.format()``
or ``%`` on these constants, and no envelope may be moved into an f-string —
either would turn the envelope into a KeyError or silently eat it.
"""

from __future__ import annotations

from ._tradecraft import (
    COMPOSITION_BODY_SHAPE,
    CONSEQUENCE_RULE,
    NO_INSTRUMENT_READINGS,
    SEVERITY_STATE_READ_RULE,
    as_of_rule,
    with_preamble,
)
from .window_ledger import window_ledger_rule

_SYSTEM_PROMPT = with_preamble(
    """TASK — second-order synthesis. You are given FIRST-ORDER FINDINGS from OTHER analysts (each with title, body, confidence, evidence, and a source analyst_id). Produce ONE second-order FINDING that is only visible when these outputs are considered together: the higher-order pattern, the convergent claim, the contradiction, or the emergent narrative. Lead `body` with the BLUF. DO NOT re-state any individual finding verbatim. Cite which analysts ground each claim (by analyst_id). If the findings disagree, surface the disagreement rather than averaging it away.
Respond with strict JSON, nothing else: {"title": "...", "body": "...", "confidence": 0.0-1.0, "evidence": ["..."], "tags": ["..."]}"""
)


# CONTINUITY prompt clause (Phase 1) — ONE definition, four compositions.
#
# The clause is LETTERED per prompt (each composition prompt numbers its rules
# (a)…(n) and the sequences differ), so it is generated rather than pasted — a
# copy per prompt would drift the moment one is edited, and the whole point of a
# continuity contract is that every composition floor states it identically.
#
# It encodes exactly four obligations, in the order a reader needs them:
#   1. SAY WHAT CHANGED versus the cited prior read (the deliverable).
#   2. ANCHOR "when" on the blocks' OWN dates/ages — never run/fetch time (the
#      temporal-collapse guard; the tradecraft preamble states this generally,
#      this restates it where a prior-read block makes it bite).
#   3. NO-CHANGE IS AN ANSWER — say so plainly rather than re-deriving, which is
#      what makes a stateless re-derivation visibly different from a real diff.
#   4. NEVER assert continuity that is not grounded in one of the two blocks, and
#      when NEITHER is shown, name the run as a FIRST read. This is the clause
#      that keeps the RAG-rollback failure mode (an uncited prior leaking into
#      cited analysis) structurally unavailable: the ONLY licensed sources of
#      "before" are two blocks that must be cited like any other evidence.
#: D1 anchored for the COMPOSITION layer. A composition has no slice header —
#: its dated anchors are the ``produced_at=`` values the block renderer prints
#: on every shown block, so the as-of is taken from the newest of those. Same
#: zero-new-facts property as the unit form: the date is a copy of rendered
#: text, never a read of the wall clock.
def _composition_as_of(read_noun: str) -> str:
    """The composition AS-OF rule, worded for ONE variant's block noun.

    L3-15: the anchor used to carry the literal alternation ``<unit|country|
    region|desk>``, which is an instruction to the PROMPT AUTHOR that leaked
    into the prompt — the pipes are not a choice a model can resolve, and some
    models copy them into the as-of line verbatim. Each variant knows exactly
    what it composes, so the noun is resolved here instead of being offered as
    a menu.
    """
    return as_of_rule(
        f"'*As of <date>; composed from <N> {read_noun} reads, "
        "latest <time>.*'. Take the date and time from the MOST RECENT "
        "produced_at printed on a shown block — rendered as a human calendar "
        "date and time, never as the raw ISO/microsecond value — and take the "
        "count from the blocks actually shown. If the shown blocks span more "
        "than a day, say so in the same line ('reads span 1-3 August')."
    )


def _continuity_rule(letter: str, *, read_noun: str) -> str:
    """The continuity rule text, lettered for one composition prompt.

    ``read_noun`` names what THIS variant composes ("unit", "country",
    "region", "desk") and reaches the prompt through the as-of line's
    "composed from <N> ... reads" clause — see :func:`_composition_as_of`.

    PHASE-V — carries the D1 as-of clause in front of the continuity
    obligations, and repairs the two lines D5 traced the machine-internals leak
    to. Both repairs are one-liners with outsized effect, because the model was
    reading each as a REPORTING REQUIREMENT rather than as the anti-embellishment
    guard it was written to be:

      * The worked example ``'no material change since the prior read of <its
        produced_at>'`` invited a literal substitution of the column value, so
        56/117 compositions narrated a microsecond ISO timestamp at the
        operator. It now shows a human date and states the rendering rule.
      * ``'describe a situation ONLY as ... its own name, status, intensity and
        event count'`` was meant to CAP what may be said about a frame; it read
        as a list of fields to print, so 27/117 compositions reported
        ``intensity 54.59 and 302 events`` as prose. The cap now names only the
        two reader-facing fields, and the two instrument readings are explicitly
        decide-with-never-print.
    """
    return (
        _composition_as_of(read_noun) + " "
        f"({letter}) CONTINUITY — a PRIOR READ block (this same target's previous "
        "verified read, carrying its OWN produced_at) and/or an OPEN SITUATION "
        "REGISTER block (the currently-open situation frames for this scope, each "
        "with its own status, intensity, event count and last_event_at/age) may be "
        "shown at the END of the evidence, each with its own [[ref:N]] handle. When "
        "either is shown you MUST: (1) state EXPLICITLY what CHANGED versus the "
        "cited prior read — name the change and cite the prior read by its "
        "[[ref:N]] handle exactly like any other block; (2) anchor EVERY temporal "
        "statement on the dates and ages printed IN those blocks (the prior read's "
        "produced_at, a situation's last_event_at / age) — NEVER on 'today', 'now', "
        "'as of this run', or the time you are running; (3) if nothing material "
        "changed, SAY SO plainly and briefly (e.g. 'no material change since the "
        "3 August morning read [[ref:N]]' — a HUMAN calendar date derived from the "
        "block's produced_at, NEVER the raw ISO/microsecond timestamp) rather than "
        "re-deriving the "
        "same picture in different words; (4) describe a situation ONLY as the "
        "register states it — its own name and status — "
        "and never upgrade, downgrade, or re-date it beyond what the register "
        "shows. The register's intensity score and event_count are internal "
        "instrument readings: USE them to decide, never PRINT them, and never "
        "promote a NEGATIVE finding into a named 'situation frame'. NEVER assert "
        "continuity of ANY kind — an escalation, a "
        "de-escalation, a trend, an 'ongoing'/'longstanding' framing, or that "
        "something has 'been building' — unless it is grounded in the cited PRIOR "
        "READ block or the SITUATION REGISTER block. If NEITHER block is shown "
        "this is a FIRST read of this target: say so plainly and make NO claim "
        "about what came before. "
        # FRAME-2 — the ledger's obligation rides the SAME clause the rest of
        # the memory section states, because it IS the rest of the memory
        # section: one header, one contiguous ordinal walk, one contract. The
        # wording is generated (``window_ledger_rule``) rather than pasted, so
        # the unit layer and this one state the identical three rules.
        + window_ledger_rule("[[ref:N]]")
        + " "
    )


# ---------------------------------------------------------------------------
# PHASE-V D6 — the composition rule set, generated per prompt
# ---------------------------------------------------------------------------
#
# Every composition reads like the minutes of a status meeting because that is
# what it was asked for. 94 of 117 sampled compositions ran an OBSERVATION /
# JUDGMENT roll call — one bullet per unit in fixed order, then the same items
# again with "Assessment:" in front — and the residual "judgment" was one
# tautological sentence. Three prompt lines produce that, and all three are
# repaired here rather than in three hand-edited copies:
#
#   * The COVERAGE rule was stated as an EQUAL-AIRTIME obligation whose worked
#     example was a unit-naming sentence, so the model satisfied it with an
#     enumeration and had no room left for an argument. Its integrity guarantee
#     (never silently drop a shown block) is real and is KEPT — it just moves to
#     a footer.
#   * The HEDGE rule handed the model the bureaucratic register verbatim ("the
#     units indicate / suggest"), so hedging became a house voice instead of a
#     calibration duty.
#   * The ONLY structural instruction was "lead body with a one-line BLUF", so
#     the model invented a skeleton — and the skeleton the other two rules imply
#     is the roll call.
#
# WHAT IS DELIBERATELY NOT TOUCHED: the TRACEABILITY rule in every prompt (a
# [[ref:N]] marker is a PROMISE that block N literally states the claim it tags;
# never introduce a fact, proper noun, or specific not present in a cited
# block). D6's one risky line — "say what these blocks TOGETHER show that none
# shows alone" — is exactly the line that invites synthesis beyond the evidence,
# and TRACEABILITY is what polices it. The shape rule below must never ship
# without it.


def _shape_rule(letter: str, *, block_noun: str, lead: str) -> str:
    """The (d)-slot SHAPE rule: judgment in the body, coverage in a footer."""
    return (
        f"({letter}) {COMPOSITION_BODY_SHAPE} "
        f"The BLUF names {lead}. '## The picture' is CONNECTED ARGUMENT: what "
        f"these {block_noun} TOGETHER show that none shows alone, ordered by "
        "consequence — every clause still carrying its [[ref:N]] and still "
        "bound by the TRACEABILITY rule below, which is what keeps 'together' "
        "from becoming 'invented'. Do NOT write a paragraph or a bullet per "
        f"{block_noun[:-1] if block_noun.endswith('s') else block_noun}, do NOT "
        "restate a shown block verbatim, and do NOT emit an OBSERVATION / "
        "JUDGMENT skeleton or any other section list of your own. "
    )


def _tension_rule(letter: str, *, block_noun: str, a: str, b: str) -> str:
    """The (c)-slot DISAGREEMENT rule + where the disagreement is written.

    Extends the existing directional-disagreement rule to FACTUAL disagreement,
    which is the leg that failed silently and visibly: on one desk, one day, the
    energy_security unit reported the Strait of Hormuz "remains effectively
    shut" while economic_coercion reported "no concrete closure is in place" —
    and the country composition inherited both blocks and narrated them as
    agreement. The old rule only ever asked about sub-claims pointing in
    different DIRECTIONS; two blocks asserting incompatible states of the same
    FACT sailed through it.
    """
    return (
        f"({letter}) SURFACE DISAGREEMENT in the '## Tension' section — do NOT "
        f"average it into a false consensus. When {a} and {b} point in "
        f"different directions, NAME BOTH and cite BOTH diverging {block_noun} "
        "via their two [[ref:N]] ordinals. DISAGREEMENT INCLUDES FACTUAL "
        "DISAGREEMENT, not only directional disagreement: BEFORE you write, "
        "check whether two shown blocks assert incompatible STATES OF THE SAME "
        "FACT (one says a chokepoint is closed, another says no closure is in "
        "place). When they do, name both sources, quote both "
        "characterizations, cite both [[ref:N]] handles, and say which is "
        "better supported and why — do NOT silently adopt one, and do NOT "
        "combine them into a sentence that implies they agree. When the shown "
        "blocks genuinely agree, say so plainly in one line rather than "
        "manufacturing a tension. "
    )


def _hedge_rule(letter: str, *, tic: str, worked: str) -> str:
    """The (b)-slot HEDGE rule — a calibration duty, not a house voice."""
    return (
        f"({letter}) HEDGE to the evidence — weaken your language as "
        "effective_confidence drops, and attribute a judgment to the source "
        "that made it when it matters who said it. Hedging is a CALIBRATION "
        f"DUTY, not a house voice: do NOT open clauses with '{tic}' as a tic. "
        f"Prefer a dated, attributed statement ('{worked}') over an agentless "
        "hedge. "
    )


def _coverage_rule(letter: str, *, block_noun: str, unit_noun: str) -> str:
    """The coverage rule — integrity guarantee kept, airtime obligation dropped.

    FRAME-1 (§4.3) forks the absence half. "A ``{unit_noun}`` with NO block
    shown is an unassessed GAP" was true when the only way to have no block was
    to have no read: the untiered gather DROPPED below-floor rows in SQL, so the
    model never learned they existed and this sentence then instructed it to
    call a verified-but-weak dimension unassessed. That is how the false "no
    read this cycle" class was manufactured — 9 product-voice atoms in the
    2026-08-20 round, and the C4 audit's atom-10 precedent. With the horizon +
    the tiered periphery the model now KNOWS the difference, so the rule states
    it: a floor-withheld dimension is "below verification floor"; only a
    dimension with no read at all inside the horizon is a gap.

    VOICE-4 folds the D6 draft's why-agnostic sentence onto that fork, SCOPED
    TO THE GAP CLASS ONLY. The draft predates FRAME-1 and told the model it
    "cannot tell WHY a block is missing", listing three indistinguishable
    causes — one of which was "produced a read that did not clear
    verification". FRAME-1's COVERAGE LEDGER renders exactly that cause
    deterministically, so carrying the draft's sentence unscoped would hand
    back the distinction the ledger exists to give and re-license the false
    "no read this cycle" the same draft's atom-10 rule forbids. What survives
    is the half still true under FRAME-1: inside the no-head class, a read that
    was never produced and one produced but outside this window remain
    indistinguishable from in here, so neither may be asserted. The clause is
    worded on THE READ rather than on ``unit_noun`` because the noun varies by
    variant and only the innermost one actually "runs" — a region does not run,
    its composition does.
    """
    return (
        f"({letter}) COVERAGE IS A FOOTER, NOT THE BODY. Every shown "
        f"{unit_noun} must still be accounted for — silently dropping one is an "
        f"integrity failure — but a {unit_noun} whose read is unremarkable is "
        "accounted for by NAMING IT IN THE '## Coverage' LINE, not by giving it "
        f"a bullet in the body. ABSENCE HAS TWO KINDS AND THEY ARE NOT THE SAME "
        f"SENTENCE: a {unit_noun} whose read appears in the weakly-supported "
        "section (or that the COVERAGE LEDGER marks below the verification "
        "floor / not verified) HAS a read that failed verification — name it as "
        "\"below verification floor\" WITH ITS DATE, and NEVER as \"no read this "
        f"cycle\", an unassessed gap, or unobserved. Only a {unit_noun} with NO "
        "read at all inside the stated window is an unassessed GAP: name it in "
        "that same line as a gap and NEVER infer, estimate, or invent its state. "
        "What you still cannot tell is WHY a gap exists — the read may never "
        "have been produced, or may have been produced and not reached this "
        "window — so never assert which: never write that a "
        f"{unit_noun} 'did not run', 'saw nothing', or 'was not assessed' as a "
        "fact about it. "
        f"Never band, score, or characterise the state of a {unit_noun} in "
        f"either class. A {unit_noun} earns space in '## The picture' ONLY when "
        "it changes the read. Do not claim to cover a "
        f"{unit_noun} whose block is not shown, and never attach a [[ref:N]] to "
        "a gap. "
    )


# ---------------------------------------------------------------------------
# VOICE-4 D6 — the composition tier's doctrine blocks (lens-3 defect family)
# ---------------------------------------------------------------------------
#
# Everything below is prompt text the composition tier did not have. Each block
# carries its lens-3 id, per the wave's standing rule: NO INSTRUCTION WITHOUT A
# NAMED DEFECT BEHIND IT. They are generated per variant rather than pasted four
# times for the reason the D6 helpers above already are — a paragraph whose
# whole purpose is that all four tiers state it identically drifts on the first
# hand edit, and L2-16 is the proof.
#
# ORDER IN THE ASSEMBLED PROMPT. These are the tier's ORIENTATION: they run
# after the TASK sentence and before "RULES:", because every one of them
# changes how the model READS the blocks it is about to be given. The lettered
# rules that follow tell it what to WRITE.


def _numbers_rule(
    *,
    subject: str,
    cited_against: str,
    solid: str,
    agreeing: str,
    shown: str,
) -> str:
    """WHAT THESE NUMBERS MEAN — L3-11.

    The tier printed ``effective_confidence`` on every block and called every
    block VERIFIED, and defined neither. So "verified" read as TRUE and seven
    correlated desk blocks read as the calibration ladder's 0.85 rung
    ("multiple independent vetted sources") — a mechanical over-confidence pump
    at the exact layer whose output is published under the seal.

    The 0.50 literal is the same number as :data:`DEFAULT_VERIFY_FLOOR` /
    :data:`TIERED_BASIS_FLOOR_DEFAULT` and is COUPLED to
    ``LEGBA_COMPOSITION_VERIFY_FLOOR``: an operator who retunes the floor must
    retune this sentence. It is written as a literal because the draft is, and
    because the constants here are import-time while the live floor is resolved
    per run (:func:`_resolve_split_floor`) — templating the default in would
    look authoritative while still being wrong under an env override.

    Closes with an explicit NON-LICENCE to print the numbers, so that a
    paragraph teaching the model to read a scale cannot be taken as permission
    to narrate it — :data:`NO_INSTRUMENT_READINGS` is a binding keep.
    """
    return (
        "WHAT THESE NUMBERS MEAN. effective_confidence is min("
        f"{subject}'s own confidence, its faithfulness score), and 'verified' "
        "here means ONE thing: a faithfulness pass checked "
        f"{subject}'s text against {cited_against} and it scored at or above "
        "the 0.50 verification floor. It does NOT mean the claim is true, "
        "corroborated, or independently confirmed. Read the scale as: "
        "0.50-0.60 barely cleared the bar — attribute it, hedge it, and never "
        f"lead with it; 0.60-0.80 {solid} — state it with its source named; "
        "above 0.80 firm. Your OWN confidence may never exceed the "
        "effective_confidence of the weakest block a load-bearing clause rests "
        f"on. And {agreeing} is not 'multiple independent vetted sources': the "
        "calibration ladder's 0.85 rung describes independent SOURCES, and you "
        f"are not being shown sources — you are shown {shown}. None of this "
        "licenses printing the numbers; they remain the ledger (see NUMBERS & "
        "SEVERITY below). "
    )


#: EVIDENCE MAY ARRIVE IN TWO TIERS — L3-13. Byte-identical on all four.
#:
#: The system prompts asserted their inputs were verified while the user prompt
#: could render a whole periphery section of blocks that are NOT
#: (``LEGBA_COMPOSITION_TIERED_EVIDENCE``); the model met an unannounced
#: section its instructions denied could exist. The header quoted here is the
#: literal one ``composition_window._render_periphery_block`` emits. "MAY
#: arrive" keeps the paragraph honest — and inert — when the flag is off.
_TWO_TIERS: str = (
    "EVIDENCE MAY ARRIVE IN TWO TIERS sharing ONE [[ref:N]] numbering. BASIS "
    "blocks cleared the verification floor and are your load-bearing evidence. "
    "Any block rendered under a 'WEAKLY-SUPPORTED / UNVERIFIED SIGNALS' header "
    "did NOT clear it: it may inform hedged, attributed context only, may never "
    "set the BLUF, the severity, or your confidence, and must be named as "
    "weakly-supported wherever you use it; in '## Coverage', account for it as "
    "an unverified read not carried, never as coverage. "
)


def _reading_rule(*, lede: str, whose: str) -> str:
    """WHAT YOU ARE ACTUALLY READING — L3-15a.

    The tier reads summaries of summaries of TRUNCATED excerpts, and its own
    input bodies are re-cut to a shared budget (:func:`composition_body_cap`).
    Nothing said so, so a detail absent from a cut body read as a detail the
    desk did not report — the composition-layer form of the unit layer's
    cut-snippet defect.

    The TAIL is deliberately one shared sentence rather than each draft's own
    punctuation: the four drafts said the same thing four ways, and a paragraph
    that must be identical across desks is exactly what L2-16 measures drift in.
    """
    return (
        f"WHAT YOU ARE ACTUALLY READING. {lede} and the block body shown to you "
        "may itself be cut to fit a shared budget — assume you may be seeing "
        "part of it: never assert that a block does not say something, and "
        "never treat a detail's absence from a shown body as its absence from "
        f"{whose} read. "
    )


def _absence_rule(*, collections: str, worked: str, world_fact: str, aggregate: str) -> str:
    """ABSENCE IS SEARCH-SCOPED — L3-15b, extended by CORRECTNESS-R1 C-D.

    Ports the corroborator's absence rule to the tier where it bites hardest:
    a desk-scoped negative ("this collection contains no X") becomes a
    WORLD-scoped assertion ("no X occurred") the moment a composition carries it
    up a layer, and several desks' not-observeds aggregate into "the country is
    calm" — a claim no block made.

    The closing two sentences are the R1 parity patch and are byte-identical
    across every seat in the batch: the rule is now externally graded (29 of 51
    graded insufficiency/absence calls faced Tier-1/2 world evidence; the
    unscoped ones were refuted, the desk-scoped ones stood), and a scoped
    negative must first be checked against the composer's OWN citation set.

    NOTE the FRAME-2 seam: "the blocks you are shown — either tier" scopes the
    own-basis check to the two EVIDENCE tiers. The WINDOW LEDGER is a third
    surface and carries its own, STRONGER absence prohibition — see
    :func:`window_ledger_rule` rule (3), which forbids a window-scale absence
    the ledger contradicts outright. The two are complementary, not rival: this
    one checks the slice, that one checks the fortnight.
    """
    return (
        "ABSENCE IS SEARCH-SCOPED. When a block reports 'not observed', 'no "
        "reports of', 'no evidence of', or an indicator at not_observed, that "
        f"means {collections} did not contain it — NOT that it did not happen. "
        f"Carry that scope forward in your own words ({worked}), never as a "
        f"world fact ({world_fact}), and NEVER aggregate {aggregate} — a quiet "
        "slice is a statement about our collection, not about the world. The "
        "rule has now also been graded against the world itself: an external "
        "correctness round checked desk negatives against independent reporting "
        "and the unscoped ones were refuted — one desk wrote that no measures "
        "of its kind were in force inside the very window a major one passed — "
        "while desk-scoped negatives stood. And a scoped negative is a claim "
        "about OUR collection: before you write one, confirm the blocks you are "
        "shown — either tier — do not themselves carry the thing you are "
        "denying; an absence claim refuted by its own citation set is the worst "
        "graded outcome a negative can have. "
    )


def _correlation_rule(letter: str, *, body: str) -> str:
    """CORRELATION — L3-10.

    The rule existed only on the THEMATIC prompt, which is where the risk is
    LOWEST (its desks are different countries). At country level every block
    reads the SAME cadence slice, so agreement across seven desks is one
    evidence pool seen seven times — and the tier was reading it as
    corroboration and raising confidence on it.

    The convergence fence is shared and must never be dropped: the tier's
    actual product IS convergence across DIFFERENT events, and a correlation
    rule without the fence teaches it to refuse that too.
    """
    return (
        f"({letter}) CORRELATION — {body} Convergence is real only when "
        "DIFFERENT underlying events, cited from different blocks, point the "
        "same way — then say that plainly instead. "
    )


def _index_collision_rule(*, contain: str, whose: str) -> str:
    """THE BARE BRACKETED NUMBERS ARE NOT YOURS — L3-14.

    Two numbering spaces meet in this prompt and nothing distinguished them.
    The shown bodies are full of the LOWER tier's ``[N]`` signal indices — a
    space this model cannot see or resolve — while its own citable handles are
    ``[[ref:N]]``. A body's ``[2]`` therefore read as sibling block
    ``[[ref:2]]``. Country was the one variant missing even the "never cite a
    raw signal" half while being the variant whose inputs are saturated with
    unit indices.

    The renderer defuses a lower COMPOSITION's ``[[ref:N]]`` markers
    (``_defuse_child_ref_markers``) and the ledger's (``(ledger ref N)``), but a
    UNIT's bare ``[13]`` is left alone on purpose — it is the unit's own honest
    citation into its own slice. So this one is prompt-side by construction.
    """
    return (
        "THE BARE BRACKETED NUMBERS INSIDE THE BODIES ARE NOT YOURS: the shown "
        f"bodies {contain} bracketed numbers like [13] or [121] — those are "
        f"{whose} own signal indices from ITS slice, a numbering space you "
        "cannot see and cannot resolve. They are NOT your handles, they do not "
        "correspond to your [[ref:N]] ordinals even when the digits match, and "
        "you must never copy one into your output or read one as a reference to "
        "another shown block. Your only citable handles are the [[ref:N]] "
        "markers at the START of each block. "
    )


#: SCOPE WORDS ARE CLAIMS — L3-7. Appended to TRACEABILITY on all four.
#:
#: The existential-to-universal strengthening ("not observed in this desk's
#: collection" -> "none occurred") is the one falsehood a fact-by-fact
#: grounding check passes: every fact in the sentence is real, and the
#: QUANTIFIER is what was invented. It belongs on TRACEABILITY because
#: TRACEABILITY is the rule it evades.
_SCOPE_WORDS: str = (
    "SCOPE WORDS ARE CLAIMS. 'only', 'no other', 'none', 'all', 'first', "
    "'sole', 'never', 'exclusively', 'the entire' each assert something about "
    "everything you did NOT see. Before you write one, re-read the cited block "
    "for the exception that refutes it — a 'meanwhile', an 'except', an "
    "'alongside', a second measure named in a subordinate clause. If the cited "
    "block does not itself assert the universal, you may not either: downgrade "
    "to what the block supports ('no further measures were OBSERVED in the "
    "reporting shown'). Never strengthen a block's existential ('not observed "
    "in this desk's collection') into a universal ('none occurred'), and never "
    "carry a block's own exclusivity language forward without checking it "
    "against that block's text. "
)


#: The selection stake — CORRECTNESS-R1 C-C / ATTRIBUTION H-FRAME Class 1.
#:
#: The tier's OWN attributed omission class: 6 majors + 2 notables the pipeline
#: HAD in blocks it was shown and the reader never saw, dropped at
#: composition-authoring time. Unlike the unit desks (whose drafts attribute the
#: stake to the fleet), "this seat's record" is literally true here.
#:
#: PLACEMENT. Appended AFTER the whole :data:`CONSEQUENCE_RULE`, not spliced
#: into it. The draft renders this paragraph between "say why in ONE clause."
#: and "STANDING PICTURE BEFORE DELTA." — an artifact of the draft being flat
#: prose — but the draft's own rationale calls for the intact shared rule,
#: "unforked", and says the close is "appended to the CONSEQUENCE, NOT
#: CONFIDENCE cluster". Splicing mid-constant would fork a fleet-shared
#: constant and break the ``_norm(CONSEQUENCE_RULE) in _norm(prompt)`` pin.
_SELECTION_STAKES: str = (
    "The costliest failure in this seat's record is not error but omission: "
    "the window's defining event, present in a block you were shown, left "
    "uncited. Graders re-reading compositions' consumed blocks found the story "
    "that defined the window sitting in the evidence, unselected, while the "
    "picture discussed something smaller. Before you write, ask which shown "
    "block a reader would say defines the window; if one does, cite it and "
    "reckon with it under the tier it arrived in — and if none does, you have "
    "checked, which is the point. "
)


#: SPEAK ABOUT THE WORLD, NOT ABOUT THE PIPELINE — R1 C4 precedents 1-2 (atom
#: 10) + the R1 SCORES product-voice row (9/68 graded assertions were
#: pipeline-state claims; 4 were contradicted by the product's own packet).
#:
#: The tier is the ADJUDICATED instance of the class, which is why the
#: paragraph names it: a '## Coverage' line saying a desk had "no read this
#: cycle" over a read that existed below the floor is a false statement about
#: what the floor did, graded on what a READER takes it to mean.
#:
#: The atom-10 mandate here and :func:`_coverage_rule`'s two-kinds fork state
#: the SAME repair from opposite ends, and both agree with the deterministic
#: wording FRAME-1's COVERAGE LEDGER block puts on the page.
_PIPELINE_VOICE: str = (
    "SPEAK ABOUT THE WORLD, NOT ABOUT THE PIPELINE. Your prose reaches a reader "
    "as a statement about the world; a sentence about this platform's own "
    "machinery — what a desk's collection contained, what a check passed, what "
    "the verification floor did — is not a world fact and must never be dressed "
    "as one. An external audit graded such sentences on what a READER takes "
    "them to mean, and found the product sometimes wrong about itself in its "
    "own channel — and the adjudicated instance is this tier's: a coverage line "
    "that said a desk had no read this cycle, when its read existed below the "
    "verification floor, is a FALSE statement of the floor's action. So: state "
    "pipeline facts only when you can see them in this run's rendered input; "
    "say them in plain reader's words as statements about coverage; when a "
    "shown block sits under the 'WEAKLY-SUPPORTED / UNVERIFIED SIGNALS' header, "
    "write that its read sits below verification floor and is not carried — "
    "NEVER 'no read this cycle'; and never assert what the platform did or did "
    "not do beyond what is rendered in front of you. "
)


# P3 per-COUNTRY composition system prompt.
#
# Selected in-kind by the runtime's ``options["target_id"]`` stamp (set only when
# the run is target-scoped — a per-country composition descriptor). The GLOBAL
# meta run keeps ``_SYSTEM_PROMPT`` byte-for-byte. Distinct from the global
# synthesis prompt in three load-bearing ways: (1) it cites EVERY factual clause
# with an inline ``[[ref:N]]`` ordinal marker resolving to the Nth sub-claim in
# the rendered bundle (so the composition is itself citable and a LATER stage
# can run a faithfulness verify OVER the composition); (2) it hedges to
# ``effective_confidence`` and weakens language as the evidence weakens; (3) it
# surfaces disagreement between sub-claims rather than averaging a false
# consensus, and narrates an HONEST EMPTY read (confidence 0.0, no fabricated
# evidence) when a country has no verified sub-claims.
#
# VOICE-4 (D6 composition drafts) adds the tier's ORIENTATION — four paragraphs
# that run before "RULES:" because they change how the blocks are READ, not what
# is written: WHAT THESE NUMBERS MEAN (L3-11), EVIDENCE MAY ARRIVE IN TWO TIERS
# (L3-13), WHAT YOU ARE ACTUALLY READING (L3-15a) and ABSENCE IS SEARCH-SCOPED
# (L3-15b + R1 C-D). Inside the rules it adds CORRELATION as (d) (L3-10 — this
# is the variant where the risk is HIGHEST, since all seven blocks read ONE
# cadence slice), the index-collision paragraph on (a) (L3-14 — this variant's
# inputs are saturated with the units' own bare ``[N]`` indices), SCOPE WORDS
# ARE CLAIMS on TRACEABILITY (L3-7), the R1 selection stake after the ranking
# cluster, and SPEAK ABOUT THE WORLD (R1 atom 10). It also PORTS the full shared
# ``CONSEQUENCE_RULE`` down from the other three variants: the reduced rubric
# inside the shape rule stays (test-pinned), the full rule now follows it.
_COMPOSITION_SYSTEM = with_preamble(
    """TASK — per-country COMPOSITION. You are given the faithfulness-verified SUB-CLAIMS (first-order unit findings) for ONE country from up to seven bounded units (leadership_transition, energy_security, escalation, narrative_coordination, internal_stability, military_posture, economic_coercion). Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source unit analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. Produce ONE second-order per-country READ. """
    + _numbers_rule(
        subject="the unit",
        cited_against="its cited sources",
        solid="solid unit work",
        agreeing="several desks agreeing off ONE slice",
        shown="desks that read the same collection",
    )
    + _TWO_TIERS
    + _reading_rule(
        lede=(
            "Each block is a unit's SUMMARY of a slice of truncated article "
            "excerpts,"
        ),
        whose="the desk's",
    )
    + _absence_rule(
        collections="the unit's COLLECTION",
        worked=(
            "'the escalation desk's collection through 10 August contains no "
            "further transits'"
        ),
        world_fact="'no further transits occurred'",
        aggregate=(
            "several desks' not-observeds into a positive claim that the "
            "country is calm"
        ),
    )
    + """RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the sub-claim block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no sub-claim behind it must NOT assert a fact. """
    + _index_collision_rule(contain="contain", whose="the unit's")
    + _hedge_rule(
        "b",
        tic="the units indicate / suggest",
        worked="the escalation desk's 3 August read has ...",
    )
    + _tension_rule(
        "c",
        block_noun="sub-claim blocks",
        a="one unit's sub-claim",
        b="another's",
    )
    + _correlation_rule(
        "d",
        body=(
            "the blocks you are shown are NOT independent observers. At "
            "country level all of them read the SAME cadence slice: check each "
            "block's as-of line and, when they name the same slice, treat "
            "their agreement as ONE evidence pool viewed by several desks, "
            "never as mutual corroboration. Two desks whose reads rest on the "
            "SAME underlying event (one weather emergency on two desks, one "
            "sanctions package seen from two angles) are NOT independent "
            "corroboration: two blocks describing the same underlying event "
            "are one data point — SAY SO, in '## Tension' or in the sentence "
            "itself, and never let repetition across desks raise your "
            "confidence."
        ),
    )
    + _shape_rule(
        "e",
        block_noun="units",
        lead=(
            "the single most consequential thing on this desk and why it "
            "matters — ranked on the STAKES the cited blocks describe (cited "
            "loss of life or armed conflict, then cited disruption to a system "
            "many actors depend on, then cited irreversibility, then cited "
            "proximity), NEVER on which block scored the highest "
            "effective_confidence and NEVER on which item merely changed"
        ),
    )
    + CONSEQUENCE_RULE
    + " "
    + _SELECTION_STAKES
    + """(f) HONEST EMPTY: if there are no verified sub-claims for this country, say so plainly with confidence 0.0 and NO fabricated evidence. (g) TRACEABILITY — a [[ref:N]] marker is a PROMISE that sub-claim block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown sub-claim blocks actually say. NEVER introduce a fact, proper noun, place-name, or event specific (a magnitude, date, location, or count) that is not present in a cited block — do NOT add concrete details a unit did not state (e.g. an event's magnitude or location, or a named actor, commitment, or position no block mentions). If you cannot ground a clause in a shown block, DROP the clause; an in-range [[ref:N]] does NOT license a claim its block does not make. """
    + _SCOPE_WORDS
    + """(h) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence actually shown for a cited block, and invent NO per-unit confidence figure or a unit that is not present; do NOT silently change a unit's stated severity or which driver a unit called its lead — if you aggregate differing unit severities, say so explicitly (e.g. 'aggregating unit severities moderate+low -> moderate'). """
    + NO_INSTRUMENT_READINGS
    + " "
    + SEVERITY_STATE_READ_RULE
    + " "
    + _coverage_rule("i", block_noun="sub-claim blocks", unit_noun="unit")
    + _PIPELINE_VOICE
    + _continuity_rule("j", read_noun="unit")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


# S2-T2 REGIONAL composition system prompt (region_composition).
#
# Selected in-kind for a REGION run (``options["target_id"]`` = ``region_<slug>``;
# dispatched at the ``region_scoped`` branch below). Mirrors ``_COMPOSITION_SYSTEM``
# but REGION-worded: the cited "sub-claims" are the per-COUNTRY reads
# (country_composition findings) of ONE region, so the read cites a COUNTRY-READ via
# its [[ref:N]] ordinal handle and its load-bearing surface is CROSS-COUNTRY
# disagreement WITHIN the region. It additionally consumes an appended CONTESTED
# FACTS block (open public.fact_contention disputes) and marks any touched dispute
# ``[[contested:<contention_id>]]`` naming BOTH arbiter-surfaced sides. (The true
# WORLD run composes REGIONS via ``_WORLD_OVER_REGIONS_SYSTEM`` below.)
#
# VOICE-4 carries the same orientation block and the same four rule additions as
# the country variant, REGION-worded: the correlation risk here is cross-BORDER
# (one incident surfacing in several countries' reads at once) rather than
# one-slice, and the index-collision paragraph says "a lower tier's" indices
# because what a country read embeds is a UNIT's numbering, two floors down.
_REGION_COMPOSITION_SYSTEM = with_preamble(
    """TASK — REGIONAL COMPOSITION. You are given the faithfulness-verified per-COUNTRY READS (second-order country_composition findings) for the member countries of ONE world region, one or more per country. Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a CONTESTED FACTS block: open disputes over a single fact (subject+predicate) where the arbiter surfaced more than one value cluster. Produce ONE second-order REGIONAL READ over the shown country reads. """
    + _numbers_rule(
        subject="the country read",
        cited_against="its cited sub-claim blocks",
        solid="solid work",
        agreeing="several country reads agreeing",
        shown="desks composed from overlapping collection",
    )
    + _TWO_TIERS
    + _reading_rule(
        lede=(
            "Each block is a country read — itself a summary of unit reads "
            "that are summaries of truncated article excerpts —"
        ),
        whose="the country's",
    )
    + _absence_rule(
        collections="the underlying desks' COLLECTIONS",
        worked=(
            "'the Sudan desks' collection through 10 August contains no "
            "ceasefire reporting'"
        ),
        world_fact="'no ceasefire occurred'",
        aggregate=(
            "several countries' not-observeds into a positive claim that the "
            "region is quiet"
        ),
    )
    + """RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the COUNTRY READ block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no country read behind it must NOT assert a fact. """
    + _index_collision_rule(contain="may contain", whose="a lower tier's")
    + _hedge_rule(
        "b",
        tic="the country reads indicate / suggest",
        worked="Sudan's 3 August country read carries ...",
    )
    + _tension_rule(
        "c",
        block_noun="country-read blocks",
        a="one country's read",
        b="another's",
    )
    + _correlation_rule(
        "d",
        body=(
            "the country reads you are shown are NOT independent observers: "
            "neighbouring countries share collection, and one cross-border "
            "event (a shared incident, one alliance move seen from both sides, "
            "one storm system, one chokepoint closure) surfaces in several "
            "countries' reads at once. Two country reads that rest on the SAME "
            "underlying event are NOT independent corroboration: they are one "
            "data point — SAY SO, in '## Tension' or in the sentence itself, "
            "and never let one event's repetition across countries raise your "
            "confidence or inflate the regional picture."
        ),
    )
    + _shape_rule(
        "e",
        block_noun="country reads",
        lead=(
            "the specific REGION this read covers (infer it from the shown "
            "country reads, which are ALL members of ONE region) and the "
            "single most consequential thing in it — do NOT open with a global "
            "'The world faces...' frame; this is a REGION, not the world"
        ),
    )
    + CONSEQUENCE_RULE
    + " "
    + _SELECTION_STAKES
    + """(f) CONTESTED FACTS: when a claim touches a listed contested group, NAME both surfaced sides and mark it [[contested:<contention_id>]] using EXACTLY a contention_id shown in the block; NEVER pick a side the arbiter did not surface and NEVER invent a contested id. (g) HONEST EMPTY: if there are no country reads, say so plainly with confidence 0.0 and NO fabricated evidence. (h) TRACEABILITY — a [[ref:N]] marker is a PROMISE that country-read block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown country reads actually say. NEVER introduce a country, actor, event specific, or figure not present in a cited country-read block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). """
    + _SCOPE_WORDS
    + """(i) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a country read's severity or the driver it called its lead; make any aggregation explicit. """
    + NO_INSTRUMENT_READINGS
    + " "
    + SEVERITY_STATE_READ_RULE
    + " "
    + _coverage_rule("j", block_noun="country-read blocks", unit_noun="country")
    + _PIPELINE_VOICE
    + _continuity_rule("k", read_noun="country")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] (and any [[contested:<id>]]) markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)

# Back-compat alias — the constant was named ``_WORLD_COMPOSITION_SYSTEM`` in
# round-1, a misnomer: it is REGION-scoped (dispatched only for region runs). The
# name/comment now say REGIONAL; the alias keeps existing references (tests,
# imports) resolving to the renamed constant.
_WORLD_COMPOSITION_SYSTEM = _REGION_COMPOSITION_SYSTEM


# S2-T3 GLOBAL (world) composition over REGIONS system prompt.
#
# Selected in-kind by the runtime's ``options["composition"]`` stamp on the
# target-LESS world_assessor run. Mirrors ``_REGION_COMPOSITION_SYSTEM`` but
# REGION-worded: the cited blocks are per-REGION reads (region_composition
# findings), so the load-bearing surface is CROSS-REGION disagreement. Because
# the world read DEGRADES a region with no region read to that region's country
# reads, a shown block may instead be one of a region's per-COUNTRY reads (still
# a real, cited block). It additionally consumes the CONTESTED FACTS block (open
# public.fact_contention disputes) and a REGION COVERAGE block that NAMES any
# region with NO read at all — the model must surface those as unassessed gaps.
# Distinct constant from ``_REGION_COMPOSITION_SYSTEM`` so the S2-T2 region compose
# (which composes COUNTRY reads and keeps that prompt) is untouched.
#
# VOICE-4 carries the shared orientation + rule additions, WORLD-worded (the
# correlation risk is one event surfacing in two regions, over reads that are
# themselves composed from overlapping collection). The REGION GAPS rule (g) is
# re-voiced to "carrying no verified read this cycle" and now forbids asserting
# WHY a region read is missing: unlike the per-country path, a world run has NO
# roster-based COVERAGE LEDGER to tell it (the ledger's denominator is
# per-country only), so here the three upstream states really are
# indistinguishable and the prompt says so.
_WORLD_OVER_REGIONS_SYSTEM = with_preamble(
    """TASK — GLOBAL world COMPOSITION over REGIONS. You are given the faithfulness-verified per-REGION READS (second-order region_composition findings), one per region. For a region that had NO region read this cycle, one or more of its per-COUNTRY reads are shown IN ITS PLACE (a degrade — treat them as that region's available evidence). Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a CONTESTED FACTS block (open disputes over a single fact where the arbiter surfaced more than one value cluster) and a REGION COVERAGE block naming world regions that have NO read at all this cycle. Produce ONE second-order WORLD READ. """
    + _numbers_rule(
        subject="the read",
        cited_against="its cited blocks",
        solid="solid work",
        agreeing="several region reads agreeing",
        shown="compositions built from overlapping collection",
    )
    + _TWO_TIERS
    + _reading_rule(
        lede=(
            "Each block is a region read (or a degraded country read) — a "
            "summary of summaries of truncated article excerpts —"
        ),
        whose="the region's",
    )
    + _absence_rule(
        collections="the underlying desks' COLLECTIONS",
        worked=(
            "'the Africa reads' collection through 10 August contains no coup "
            "reporting'"
        ),
        world_fact="'no coup occurred'",
        aggregate=(
            "several regions' not-observeds into a positive claim that the "
            "world is calm"
        ),
    )
    + """RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the read block it rests on; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no read behind it must NOT assert a fact. """
    + _index_collision_rule(contain="may contain", whose="a lower tier's")
    + _hedge_rule(
        "b",
        tic="the region reads indicate / suggest",
        worked="the Africa read of 3 August carries ...",
    )
    + _tension_rule(
        "c",
        block_noun="region-read blocks",
        a="one region's read",
        b="another's",
    )
    + _correlation_rule(
        "d",
        body=(
            "the region reads you are shown are NOT independent observers: "
            "regions share the events on their borders, and one event (a "
            "chokepoint closure, one alliance move, one strike campaign) can "
            "surface in two regions' reads at once — and every region read is "
            "itself composed from desks reading overlapping collection. Two "
            "region reads that rest on the SAME underlying event are NOT "
            "independent corroboration: they are one data point — SAY SO, in "
            "'## Tension' or in the sentence itself, and never let one event's "
            "repetition across regions inflate the world picture or your "
            "confidence."
        ),
    )
    + _shape_rule(
        "e",
        block_noun="region reads",
        lead=(
            "the single most consequential situation on this board and why it "
            "matters. THIS IS THE WORLD HEADLINE: it is read as the tower's "
            "answer to 'what matters most right now', so getting the ranking "
            "right is this read's whole job"
        ),
    )
    + CONSEQUENCE_RULE
    + " "
    + _SELECTION_STAKES
    + """(f) CONTESTED FACTS: when a claim touches a listed contested group, NAME both surfaced sides and mark it [[contested:<contention_id>]] using EXACTLY a contention_id shown in the block; NEVER pick a side the arbiter did not surface and NEVER invent a contested id. (g) REGION GAPS: if the REGION COVERAGE block lists a region as having NO read, NAME that region plainly in the '## Coverage' line as carrying no verified read this cycle — do NOT infer, estimate, or invent its state, do NOT assert WHY the read is missing (not run, nothing found, or not cleared by verification — you cannot tell which from here), and NEVER attach a [[ref:N]] to a gap region. (h) HONEST EMPTY: if there are no reads at all, say so plainly with confidence 0.0 and NO fabricated evidence. (i) TRACEABILITY — a [[ref:N]] marker is a PROMISE that block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown reads actually say. NEVER introduce a region, country, actor, event specific, or figure not present in a cited block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). """
    + _SCOPE_WORDS
    + """(j) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a read's severity or the driver it called its lead; make any aggregation explicit. """
    + NO_INSTRUMENT_READINGS
    + " "
    + SEVERITY_STATE_READ_RULE
    + " "
    + _coverage_rule("k", block_noun="region-read blocks", unit_noun="region")
    + _PIPELINE_VOICE
    + _continuity_rule("l", read_noun="region")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] (and any [[contested:<id>]]) markers...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


# S2-T4 THEMATIC (escalation) composition system prompt.
#
# Selected in-kind by the runtime's ``options["thematic_dimension"]`` stamp on the
# target-LESS thematic run. Mirrors ``_WORLD_OVER_REGIONS_SYSTEM`` but THEMATIC-
# worded: the cited blocks are per-DESK escalation-unit reads (one per country
# desk), so the load-bearing surface is CROSS-DESK convergence/divergence of
# ESCALATION risk. It consumes a DESK COVERAGE block naming any desk with no
# escalation head (absence-honest gaps). The CORRELATION rule is load-bearing: two
# desks whose reads rest on the SAME underlying wire signal are NOT independent
# corroboration — the kind's T7 guard de-duplicates the evidence numerically, and
# the prompt tells the model not to treat shared-signal desks as independent.
# Distinct constant from ``_WORLD_OVER_REGIONS_SYSTEM`` so the world/region
# compositions are untouched.
#
# VOICE-4 carries the shared orientation + rule additions. This is the ONE
# variant that already HAD a correlation rule, so (d) is EXTENDED rather than
# introduced: the same-underlying-event half is kept verbatim and the
# shared-collection half (several desks resting on one wire story or one cadence
# slice) is added behind it. DESK GAPS (e) gets the same why-agnostic re-voicing
# as the world variant's REGION GAPS. This variant has no ``_coverage_rule`` —
# its coverage duty lives in (e) — so SPEAK ABOUT THE WORLD sits after RENDER
# DATES instead of after the Coverage footer, which is where the draft puts it.
_THEMATIC_COMPOSITION_SYSTEM = with_preamble(
    """TASK — GLOBAL THEMATIC COMPOSITION over ESCALATION. You are given the faithfulness-verified per-DESK ESCALATION READS (first-order `escalation` unit findings), ONE per country desk, each for a DIFFERENT country. Each block STARTS with a [[ref:N]] handle (a small integer N) and shows its source analyst_id, its DESK (target_id — the country the read is for), effective_confidence (already min(confidence, faithfulness)), title and body. You MAY also be given a DESK COVERAGE block naming desks that have NO escalation read this cycle. Produce ONE second-order GLOBAL ESCALATION READ that surveys near-term escalation risk ACROSS the desks. """
    + _numbers_rule(
        subject="the desk read",
        cited_against="its cited sources",
        solid="solid desk work",
        agreeing="several desks agreeing",
        shown="desks that may share collection",
    )
    + _TWO_TIERS
    + _reading_rule(
        lede=(
            "Each block is a desk's SUMMARY of a slice of truncated article "
            "excerpts,"
        ),
        whose="the desk's",
    )
    + _absence_rule(
        collections="the desk's COLLECTION",
        worked=(
            "'the Ukraine desk's collection through 10 August contains no new "
            "strikes'"
        ),
        world_fact="'no new strikes occurred'",
        aggregate=(
            "several desks' not-observeds into a positive claim that "
            "escalation risk is globally low"
        ),
    )
    + """RULES: (a) CITE EVERY factual clause inline with a [[ref:N]] marker using EXACTLY the small integer N shown as the [[ref:N]] handle at the START of the desk read block it rests on, and NAME the desk (country) it is about; NEVER invent an N, NEVER cite a raw signal, and NEVER cite an N not shown; a clause with no desk read behind it must NOT assert a fact. """
    + _index_collision_rule(contain="contain", whose="the desk's")
    + _hedge_rule(
        "b",
        tic="the desk reads indicate / suggest",
        worked="the Ukraine desk's 3 August read has ...",
    )
    + _tension_rule(
        "c",
        block_noun="desk-read blocks",
        a="one desk's read",
        b="another's",
    )
    + _correlation_rule(
        "d",
        body=(
            "two desks whose reads rest on the SAME underlying wire signal (a "
            "shared cross-border incident, one alliance move seen from both "
            "sides) are NOT independent corroboration; do NOT count them twice "
            "or let a single shared event inflate the global picture. When two "
            "cited desks clearly describe the SAME underlying event, SAY SO "
            "rather than presenting them as two independent data points. The "
            "desks also share collection: several desks' reads may rest on one "
            "wire story or one cadence slice — check the blocks' as-of lines, "
            "treat same-pool agreement as ONE evidence pool viewed from "
            "several desks, and never let repetition across desks raise your "
            "confidence."
        ),
    )
    + """(e) DESK GAPS: if the DESK COVERAGE block lists a desk as having NO read, NAME that desk plainly in the '## Coverage' line as carrying no verified escalation read this cycle — do NOT infer, estimate, or invent its state, do NOT assert WHY the read is missing (not run, nothing found, or not cleared by verification — you cannot tell which from here), and NEVER attach a [[ref:N]] to a gap desk. """
    + _shape_rule(
        "f",
        block_noun="desk reads",
        lead=(
            "where global escalation risk is actually concentrated. THIS READ "
            "FEEDS THE WORLD HEADLINE, so its ordering propagates: rank the "
            "desks by stakes, and never present them as a list sorted by score"
        ),
    )
    + CONSEQUENCE_RULE
    + " "
    + _SELECTION_STAKES
    + """(g) HONEST EMPTY: if there are no desk reads at all, say so plainly with confidence 0.0 and NO fabricated evidence. (h) TRACEABILITY — a [[ref:N]] marker is a PROMISE that desk-read block N literally states, in substance, the exact claim it tags; you may ONLY summarize, aggregate and reconcile what the shown desk reads actually say. NEVER introduce a country, actor, event specific, or figure not present in a cited block; if you cannot ground a clause in a shown block, DROP it (an in-range [[ref:N]] does NOT license a claim its block does not make). """
    + _SCOPE_WORDS
    + """(i) NUMBERS & SEVERITY — state NO numeric confidence value other than an effective_confidence shown for a cited block, and do NOT silently alter a desk read's severity or the vector it called its lead; make any aggregation explicit. """
    + NO_INSTRUMENT_READINGS
    + " "
    + SEVERITY_STATE_READ_RULE
    + " "
    + _PIPELINE_VOICE
    + _continuity_rule("j", read_noun="desk")
    + """Respond with strict JSON only: {"title":"...","body":"...with [[ref:N]] markers naming each desk...","confidence":0.0-1.0,"evidence":["..."],"tags":["..."]}"""
)


__all__ = [
    "_COMPOSITION_SYSTEM",
    "_REGION_COMPOSITION_SYSTEM",
    "_SYSTEM_PROMPT",
    "_THEMATIC_COMPOSITION_SYSTEM",
    "_WORLD_COMPOSITION_SYSTEM",
    "_WORLD_OVER_REGIONS_SYSTEM",
    "_composition_as_of",
    "_continuity_rule",
    "_coverage_rule",
    "_hedge_rule",
    "_shape_rule",
    "_tension_rule",
]
