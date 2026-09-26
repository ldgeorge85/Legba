"""THE ASSESSMENT CHANNEL'S PROMPT — the voice that survives, fenced (D-1 §2).

The demotion takes the composition tier's prose away and replaces it with
quotation. This module is where the interpretive voice goes instead: a separate
row, in its own band, written FROM the assembled record and from nothing else.

THE FENCE IS A SIGNATURE, NOT A SENTENCE. :func:`build_assessment_prompt` takes
``payload: Mapping`` — the ``data.data.assembly`` object — as its ONLY data
argument. No connection, no slice, no reader, no desk head. That is §2.1's
enforcement point 2, and a test pins the signature rather than trusting the
prose: a prompt builder that cannot reach the substrate cannot quote it.

WHAT THE VOICE IS HANDED. The rendered record, byte-for-byte the body the
canonical row publishes (``assembly_render.render_assembly_body``), plus the
record's own ARITHMETIC — the concentration verdict, the drop counts, the
tension counters. Handing it the rendered body rather than a second rendering is
deliberate: the voice and the reader then see the same words in the same order,
so "the record says X" is checkable by looking at the page.

THE REGISTER, AND WHY IT DEPARTS FROM THE COMPOSITION PROMPTS.
``VOICE_ORGANIC_REVIEW_2026-09-01`` §3.3 measured what makes the journal read
organic while the reads read templated, and §3.4 sorted the properties into
transferable and load-bearing. This prompt takes the transferable ones:

  * **A1 — prose out, markers in.** No strict-JSON envelope. The journal proves
    the parse works at equal faithfulness, and the JSON envelope is the single
    most likely cause of the register. The title is scraped from the prose the
    way ``journal_assessor._derive_title`` scrapes it.
  * **A2 — the two citation classes.** FACT carries an ordinal; PERSPECTIVE — the
    weighing, which is most of an Assessment — does not have to be a restatement
    of a block. It still NAMES the ordinals it is weighing, because that is the
    channel's fence, but it is allowed to be an argument rather than a paraphrase.
  * **A4 — open on the world**, not on the as-of line. The stamp is metadata.
  * **A6 — the declared blind spot.** The record publishes its own drop ledger;
    a voice that reads it and says nothing about the aperture is the weaker
    voice. AIMED AT THE WORLD since v2: the counts say how much is missing, and
    the section says what that costs the reader — a blind-spot section spent
    reviewing the tier ("the record lacks granular export volumes") has named
    no blind spot at all. NAMED since v3: the counts were not enough, because a
    voice told to name a blind spot and handed only a number guesses one
    ("no coverage of Central Asia", on a record that never mentions it), so
    :func:`declared_aperture` now hands over the drop ledger's own desks,
    targets and head titles.
  * **B1 — threads, weighted, uncrowned**, with the earned-single state kept
    legal and now DECIDED by an arithmetic the voice is handed as a fact.

And it refuses the ones §3.4 marks load-bearing (C1–C5): no uncited assertion, no
speculative first person, no refusal to state a bottom line, no flag-never-strip
consequence model for the RECORD (this channel's own flags are a different
thing — see below), and no non-attribution of the machine.

THE v2 CLAUSE — THE INSTRUMENT IS NOT THE WORLD (D-6 §7.6, prereg E-2 / N2).
D-6's replay diagnosed the weakest specimen (fidelity floor 0.545) as *"prose
about the record's own machinery … an analyst narrating an instrument rather
than a world"* and named one prompt clause as the lever. The first natural
post-fix live Assessment measured the diagnosis exactly: ``citation_support``
**0.4286** on 2026-09-06 00:15Z, all four ``soft_fail`` claims in that single
class ("its cited mass and verification score are considerably lower", "the
remaining six reads … consist mainly of meta-statements about read counts", "the
tier does not contain granular data on export volumes", "this single, high-mass
(9.10) and well-verified (0.85) read outweighs …"), and EVERY substantive world
claim supported.

The clause draws the line where the evidence map already draws it. P1
(``fa3d2d85``) put the record's ARITHMETIC BLOCK into every citation's
``evidence_text`` under ``EVIDENCE_ARITHMETIC_RULE``, so those counters are
gradeable behind any ordinal the voice cites — and the live row proves it, since
"six reads fell below the verification floor" graded *supported* in the same body
whose per-block numbers did not. What is NOT in the map is a BLOCK's attribution
line (cited mass, verify score, severity, produced-at): rendered on the record's
page by ``assembly_render._attribution``, absent from ``spine_span_text``, and
therefore unsupported by construction however accurately it is copied. So the
prompt says both halves in terms — read the arithmetic to DECIDE, state a counter
only in the arithmetic block's own words with an ordinal, never narrate the
instrument — and ``assessment_unsupported.instrument_prose`` marks the residue
deterministically rather than leaving it to a judge to find.

THE v3 REPAIR — THE TWO RESIDUALS (G3, D-6 §6.1). The v2 clause worked on its
target and the failures moved house: across the N=5 replay the INSTRUMENT class
fell from 7 of 11 judge failures to 3 of 8, while CROSS-BLOCK WEIGHTING rose 1 → 3
and the APERTURE stayed at 2. Neither residual is fabrication and neither is
fixable by telling the voice to stop:

  * **Weighting.** ``THREADS, WEIGHTED, UNCROWNED`` *asks* for a comparison
    across blocks, and the composition rubric grades each claim against the ONE
    sub-claim its marker names — so the sentence the channel mandated was the
    sentence the judge could only call unsupported. The grader half of the
    repair is ``provenance.assessment_weighting``, which licenses a comparison
    that relays the record's OWN published ranking (the ordinal order and the
    earned-lead verdict, both already in the evidence map). The prompt half is
    here: the section now states the SHAPE that makes a weighing checkable —
    name both blocks in the sentence that weighs them.
  * **Aperture.** The mandated blind-spot section had no vocabulary. The record
    knew the answer all along — the drop ledger carries the desk, the target and
    the title of every read it saw and did not carry — and threw it away at the
    prompt boundary. :func:`declared_aperture` publishes it, it rides into every
    citation's ``evidence_text`` with the rest of the arithmetic, and
    ``assessment_unsupported.aperture_unrostered`` marks a blind-spot sentence
    that names a unit the record never declared.

THE ONE PRIVILEGE THIS CHANNEL HAS, AND WHY IT IS SAFE. Its unsupported spans are
MARKED, not dropped (§2.4) — the journal's off-chain privilege, granted here
because the Assessment cannot write a fact into the record. The record is the
assembly; the assembly is quotation; nothing this voice says changes a byte of
it. The prompt tells the model this outright, because a writer who knows a
hedge is cheaper than a flag hedges honestly, and a writer who does not know
guesses.

WHAT IS DELIBERATELY NOT IMPORTED: ``_tradecraft.with_preamble``. The shared
``ANALYTIC_PREAMBLE`` ends with *"Respond with EXACTLY the JSON object your task
specifies … The first character must be { and the last must be }"* and opens with
a single-judgment BLUF mandate. Both are precisely what this channel drops. The
journal made the same call for the same reason (``journal_assessor`` module
banner, §4.2), and the standards worth keeping are restated below in the
channel's own words rather than inherited with their envelope attached.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..provenance.composition_integrity import desk_absence_sentences
from ._tradecraft import (
    ASSESSMENT_STANDARDS as _STANDARDS,
    _body_shape,
    _TITLE_RULE_COMPOSITION,
)
from .assembly_payload import TIER_COUNTRY
from .assembly_render import quoted_text, render_assembly_body
from .assembly_spans import SPAN_ROLE_CONTEXT_BODY, context_spans
# STEP E — the world's v4 voice is a LEAF beside this module (the size gate's
# own instruction: extract a cohesive unit, re-export from the original, and
# every importer is zero-edit). The SELECTORS stay here, because choosing a
# voice means seeing all three.
from .assessment_prompt_world_v4 import (  # noqa: F401 — re-exported surface
    COUNTRY_SECTION_HEADER,
    PROMPT_VERSION_V4,
    PROMPT_VERSION_V5,
    WORLD_ASSESSMENT_BODY_SHAPE_V4,
    WORLD_ASSESSMENT_SYSTEM_V4,
    WORLD_ASSESSMENT_SYSTEM_V5,
)
from .unit_names import payload_names, unit_label

__all__ = [
    "APERTURE_NAME_CAP",
    "CONTEXT_SECTION_HEADER",
    "COUNTRY_ASSESSMENT_BODY_SHAPE",
    "COUNTRY_ASSESSMENT_SYSTEM",
    "COUNTRY_PROMPT_VERSION",
    "COUNTRY_SECTION_HEADER",
    "desk_reads_in_full",
    "has_context_spans",
    "prompt_version_for",
    "system_prompt_for",
    "APERTURE_TITLE_CHARS",
    "ASSESSMENT_BODY_SHAPE",
    "ASSESSMENT_SYSTEM",
    "PROMPT_VERSION",
    "PROMPT_VERSION_V4",
    "PROMPT_VERSION_V5",
    "WORLD_ASSESSMENT_BODY_SHAPE_V4",
    "WORLD_ASSESSMENT_SYSTEM_V4",
    "WORLD_ASSESSMENT_SYSTEM_V5",
    "TENSION_BUDGET_CHARS",
    "TENSION_RENDER_CAP",
    "TENSION_SPAN_CHARS",
    "build_assessment_prompt",
    "declared_aperture",
    "declared_tensions",
    "record_arithmetic",
    "render_lead_test",
]

#: Bumped whenever the system prompt changes, and stamped on the row. A voice
#: change that is invisible in the data is a voice change nobody can attribute.
#:
#: v2 (2026-09-05) — THE WEAK-SPECIMEN CLAUSE. D-6 §7.6 named it: the weakest
#: replay specimen (floor 0.545) "spends sentences on the record's own machinery
#: … an analyst narrating an instrument rather than a world", and *"if one thing
#: is tuned before R5, it is this"*. The first natural post-fix live Assessment
#: (2026-09-06 00:15Z) then measured it exactly: ``citation_support`` 0.4286
#: against the G3 bar of 0.90, with ALL FOUR ``soft_fail`` claims in that one
#: class and every substantive world claim supported. Two sections carry the
#: fix — ``THE INSTRUMENT IS NOT THE WORLD`` and the aperture section's
#: re-pointing at the world — and the version is what lets a row be attributed
#: to one side of it. R4 note: this is prereg item E-2 / instrument-plan N2, and
#: it lands BEFORE T0 (2026-09-12 13:00Z), so it is not a mid-window
#: co-intervention.
#:
#: v3 (2026-09-06) — THE G3 REPAIR, the two residual classes D-6 §6.1 named.
#: The v2 replay moved the mean 0.7428 -> 0.7893 and reached the 0.90 bar on
#: 2 of 5, because the instrument class fell (7 of 11 judge failures -> 3 of 8)
#: and the failures REDISTRIBUTED into two shapes the clause could not reach:
#: CROSS-BLOCK WEIGHTING (1 -> 3), which the voice contract itself mandates and
#: the grader read as an unsupported comparison, and the APERTURE (3 -> 2), where
#: the voice was told to name a blind spot and handed only counts to name it
#: from. Two prompt sections move for them — ``THREADS, WEIGHTED, UNCROWNED``
#: gains the shape that makes a weighing CHECKABLE (name both blocks, in one
#: sentence), and the aperture section is re-pointed at the record's own
#: DECLARED APERTURE, which ``record_arithmetic`` now publishes BY NAME. The
#: grader half is ``provenance.assessment_weighting`` and the marker half is
#: ``assessment_unsupported.aperture_unrostered``; this version is what lets a
#: row be attributed to one side of the pair.
PROMPT_VERSION: str = "assessment_prompt.v3"


#: The Assessment's section list, run through the SAME ``_body_shape`` mechanics
#: every other tier uses — headers alone on a line, no bold pseudo-headers, no
#: glued headers, the title cap — and the SAME 2026-09-01 composition title rule.
#:
#: TWO SECTIONS OF ``COMPOSITION_BODY_SHAPE`` ARE DELIBERATELY GONE, and the
#: reason is the demotion itself rather than taste:
#:
#:   * ``## Coverage`` — the record now PERSISTS its coverage ledger (§1.4c) and
#:     the reader renders it from the array. A prose restatement is what produced
#:     the live ``metadata_mismatch`` specimen (a body asserting a tier the ledger
#:     contradicts) and what ``_PIPELINE_VOICE`` forbids in the same breath as
#:     mandating it.
#:   * ``## Tension`` — tensions are DETECTED now (§1.6), and the tier may
#:     neither manufacture nor dissolve one. A model asked for a Tension section
#:     writes one; 13.8% of world bodies currently close it by asserting
#:     unanimity in a pipeline whose desks are structurally prevented from
#:     disagreeing.
#:
#: What replaces them is the section neither tier has ever had: the aperture the
#: voice itself is missing, which the record can now hand it as a number.
ASSESSMENT_BODY_SHAPE: str = _body_shape(
    "(1) '**BLUF:**' — the bottom line in ONE or TWO sentences: the two or "
    "three threads that most define this record and how they WEIGH against each "
    "other, or ONE alone where the record's own arithmetic says the "
    "concentration was earned. Weighted, never merely listed; an unranked roll "
    "of threads is a roll call, not a bottom line, and \"no single thread "
    "dominates this cycle\" is itself a legitimate bottom line when the record "
    "says so; "
    "(2) '## The reading' — connected argument: what these blocks TOGETHER mean "
    "that none means alone, what you privilege and why, where two blocks pull "
    "against each other and which way you read it. Not a paragraph per block "
    "and not a summary of the record — the record is already on the page above "
    "you, in the desks' own words; "
    "(3) '## What would change this' — the ONE observation that would most move "
    "this reading, and what on the record you are least sure of; "
    "(4) '## What this reading misses' — the aperture, and it is about the "
    "WORLD, not about this instrument. The record hands you THE DECLARED "
    "APERTURE: the units it could have carried and did not, BY NAME — the "
    "desk and target of every read it ranked below the cut, everything that "
    "sat below the verification floor, and any declared roster unit with no "
    "read at all. NAME THOSE, in the arithmetic block's own words and with "
    "an ordinal, and then say what their absence costs the reader: what is "
    "going on in those places that this record therefore could not see. A "
    "place, actor or region that appears NOWHERE in that list and nowhere in "
    "a block you cite is a GUESS — \"no coverage of Central Asia\" on a record "
    "that never mentions Central Asia is a sentence you invented, it is "
    "marked as one, and the real uncovered units were sitting in the ledger "
    "unnamed. Where the ledger says a count has no names behind it, say how "
    "much is unseen and never which. What this section is NOT is a review of "
    "the tier: \"the record lacks granular export volumes\" is a note about "
    "the machine, and a reading that spends its blind-spot section on the "
    "machine has not named a blind spot at all. A reading that hides its own "
    "blind spot has drifted off its warrant.",
    title_rule=_TITLE_RULE_COMPOSITION,
)


ASSESSMENT_SYSTEM: str = f"""TASK — THE ASSESSMENT. You are handed ONE assembled record: a day's verified desk reads, quoted verbatim, each under an ordinal handle [[ref:N]], with the arithmetic that ordered them. Write the interpretive read that sits BESIDE that record, in its own clearly-labelled band, under your own headline.

WHAT YOU ARE. You are not the record and you are not the machine. You are an argument ABOUT the record. The record states what the desks found, in their words, with their origins; you say what it MEANS — what you weigh heavily, what you discount, what you would watch. That is the whole of your job, and it is not a small one: the record deliberately cannot do it.

WHAT YOU ARE HANDED, AND WHAT YOU ARE NOT. You are handed the record and nothing else — no wire items, no desk read beyond what the record quotes, no search. Every block below is a desk's own sentence, cut byte-for-byte from a read that was verified before it got here. If a fact is not in a block, YOU DO NOT HAVE IT. Not "you should be careful with it" — you do not have it.

THE ORDINAL FENCE. Cite with [[ref:N]] using ONLY the ordinals shown below. Write the marker EXACTLY like this — [[ref:3]] — two square brackets, the word ref, a colon, the number, two closing brackets. Not (ref 3), not [3], not "block 3": those are not citations and a read that carries none is not published. An ordinal you invent, or one outside the range you are shown, FAILS THE RUN — it is a construction error, not a style note. Every sentence you write names at least one ordinal, including the interpretive ones: naming the blocks you are weighing is how a reader checks your argument against the words it rests on.

FACT vs PERSPECTIVE — the two citation classes. A sentence stating WHAT A BLOCK SAYS is a FACT: it carries the ordinal of the block that says it, and it may not go beyond what that block states. Your own weighing — what the record MEANS, which reading you privilege, what you would watch next, where you think the desks are thin — is PERSPECTIVE: it still names the ordinals it is about, but it is allowed to be an ARGUMENT rather than a paraphrase. Most of a good Assessment is legitimately perspective. What perspective may NEVER do is smuggle in a fact: a new place, actor, number, date or event that no block states is fabrication whether it is hedged or not.

THREADS, WEIGHTED, UNCROWNED — and the verdict you are handed. The record's own arithmetic has already decided whether one thread genuinely outweighs the rest, on a measured key, over the whole day's candidate pool. That verdict is printed for you below. YOU MAY NOT CROWN AGAINST IT: if the record says concentration was NOT earned this cycle, you do not open by declaring one driver the top risk — you carry two to four threads and say how they weigh against each other. If it says concentration WAS earned, one thread may lead and you name the specific development rather than the category. Where you disagree with the arithmetic, say so as a disagreement and name the number you are disagreeing with; do not quietly write over it. AND THERE IS A SHAPE THAT MAKES A WEIGHING CHECKABLE, which is the difference between an argument a reader can follow and a sentence that reads as an assertion: when you say one thread outweighs, leads, or is secondary to another, NAME BOTH BLOCKS IN THAT SENTENCE — [[ref:1]] against [[ref:4]], not [[ref:1]] against “the others”. The record ranked the whole day's pool and printed the result as the ordinals themselves, so a comparison naming both sides is one a reader can check against the order; a comparison naming one side and gesturing at the rest is one nobody can. The same rule the other way: if the arithmetic says concentration was NOT earned, do not crown — say plainly that no single thread dominates and weigh the ones you carry against each other.

WHAT YOU MAY NOT SET. The record's headline, its severity and its confidence are the RECORD's columns, decided deterministically from what it carries. You do not set them, restate them as your own verdict, or write a sentence whose only content is the severity word. Your headline is yours and appears only in your own band.

THE INSTRUMENT IS NOT THE WORLD, and this is the clause most likely to cost you. You are shown the record's machinery: each block's cited mass, verify score, severity and age on its attribution line, and below the record how many reads were carried, how many sat under the floor, how many candidates were ranked. All of it is handed to you SO YOU CAN DECIDE — which thread to lead with, which to discount, where to hedge, what to call thin. It is your instrument panel. It is not your subject. Do NOT write that a read's cited mass is 9.10, that its verification score is higher or lower than another's, that six blocks "consist mainly of meta-statements", or that the tier "does not contain" some class of data. Write the DECISION the number produced, in the world's own nouns: not "this high-mass, well-verified read outweighs the others" but "Iran's export capacity is the crisis this cycle [[ref:1]]". The weight lives in which thread you lead with and how confidently you write it — never in a number recited back about the machine. A reader who wanted the arithmetic has it on the record above you and on the badge beside you; a sentence spent narrating it is a sentence that read the instrument instead of the world.

THE ONE PLACE A RECORD NUMBER IS STILL YOURS TO STATE, and the exact shape it must take. The counters in THE RECORD'S OWN ARITHMETIC block below — reads carried, shown and not carried, candidates ranked, reads below the verification floor, tensions declared out of pairs examined, roster units covered, and the lead test's own share and ratio — ARE yours to state. They travel with your evidence: every ordinal you cite carries that whole arithmetic block beneath the rule "--- THE RECORD THIS BLOCK SITS IN ---", which is how a number of yours gets checked at all. So if you state one, state it in the arithmetic block's own words AND carry a [[ref:N]] in the same sentence. Anything printed on a BLOCK's attribution line instead — cited mass, verify score, severity, produced-at — does NOT travel with your evidence, and a sentence resting on one is unsupported however accurately you copied it off the page.

YOUR MARKS PUBLISH WITH YOU. A deterministic pass reads what you write and marks, inline and in public, any span that the record cannot support: a cross-block RANKING claim the record never performed; an unbounded SCOPE word ("globally", "worldwide") over blocks each bounded to one target; a CAUSAL connective welding two blocks the record only places side by side ("indicating", "driving", "because"); a collection-scoped absence in a block republished as a fact about the world; and a sentence whose subject is the INSTRUMENT rather than the world — a cited mass, a verify score, a block count, what the tier "lacks" — stated in words the record's own arithmetic does not use. Those marks appear beside your prose with their reasons — your sentence is never deleted and never edited. So the cheap move is the honest one: attribute the ranking to the record's own order, keep the scope word the block used, and say "these two sit side by side" where that is what the record shows.

{_STANDARDS}

REGISTER. Specificity is the craft target: dates, magnitudes, named actors, the named brake on a trend — all of them already in the blocks, so every one adds checkable surface rather than risk. No atmosphere, no scene-setting, no inferred motive, no adjective doing the work a cited number should do. Do not open on the as-of stamp — open on the world. Do not rebuild yesterday's headline with today's nouns. Never write "the most plausible near-term trajectory", "the dominant <X> vector", or "steady tension"; if a sentence could have run on any day of the year, it has not read this record.

{ASSESSMENT_BODY_SHAPE}

OUTPUT. Respond with PROSE, in markdown — NOT JSON. The FIRST line is your headline, alone, in bold (**like this**). Then a blank line, then the body. No fences, no preamble, no commentary about the task."""


#: P3 LANE A — THE COUNTRY TIER'S OWN VERSION STRING, and it is a SEPARATE
#: counter rather than a bump of :data:`PROMPT_VERSION`.
#:
#: The two prompts ask different questions of different records. A world
#: Assessment argues across REGIONS about a record whose blocks are themselves
#: assemblies; a country Assessment argues across DIMENSIONS about one desk,
#: with each dimension's full desk read in front of it. Pooling them under one
#: version string would make every before/after comparison on either of them
#: uninterpretable — the exact defect the v2/v3 notes above exist to prevent.
#:
#: v1 (2026-09-18) — the first country voice since the demotion. The world
#: prompt's text and :data:`PROMPT_VERSION` are BYTE-UNCHANGED by this train;
#: a test pins both.
COUNTRY_PROMPT_VERSION: str = "country_assessment_prompt.v1"

#: The header the context section is rendered under. A module constant because
#: the test that proves the country prompt carries it — and the world prompt
#: does not — has to name the same bytes the builder writes.
CONTEXT_SECTION_HEADER: str = "THE DESK READS IN FULL"


#: The country tier's section list. The SAME four sections as the world voice
#: (a reader who has learned one band can read the other), through the SAME
#: ``_body_shape`` mechanics and the SAME 2026-09-01 title rule — but every
#: section is re-pointed at the ONE question this tier exists to answer.
#:
#: WHAT CHANGES, AND WHY EACH CHANGE IS FORCED:
#:
#:   * '## The reading' asks for CORRELATION AND TENSION BETWEEN DIMENSIONS.
#:     The world voice weighs threads that are already separate stories; the
#:     country voice is looking at eight readings OF THE SAME PLACE in the same
#:     window, where the whole value is whether the energy read and the
#:     internal-stability read are one story or two. "A paragraph per dimension"
#:     is the failure mode here, and it is a different failure from the world
#:     tier's "summary of the record".
#:   * THE HEDGE VOCABULARY IS MANDATED IN WORDS ("consistent with",
#:     "insufficient on its own"). A cross-dimension claim is the single easiest
#:     place in this product to assert a causal link two desks never made, and
#:     the deterministic marker (``assessment_unsupported``'s causal class)
#:     fires on exactly that. Handing the model the phrasing that survives the
#:     marker is cheaper than marking it afterwards.
#:   * The aperture section keeps the v3 re-pointing VERBATIM — the record's own
#:     DECLARED APERTURE, by name, aimed at the WORLD and not at the instrument.
#:     That clause is the answer to a measured defect (0.4286 citation support,
#:     all four failures in the instrument class), and it is not tier-specific.
COUNTRY_ASSESSMENT_BODY_SHAPE: str = _body_shape(
    "(1) '**BLUF:**' — the bottom line for THIS COUNTRY in ONE or TWO "
    "sentences: what the dimensions TOGETHER say about it in this window. "
    "Weighted, never merely listed; where the record's own arithmetic says the "
    "concentration was earned, one dimension may lead and you name the "
    "specific development rather than the category; where it does not, say "
    "plainly that no single dimension dominates and carry the two or three "
    "that do the work; "
    "(2) '## The reading' — THE CROSS-DIMENSION READ, and this is the whole "
    "job. Not a paragraph per dimension: the record already prints each desk's "
    "own sentence under its ordinal and each desk's full read below that. What "
    "you add is what they mean TOGETHER — where two dimensions CORROBORATE "
    "each other (the same pressure showing up in two independent desks), where "
    "they PULL AGAINST each other (one desk's stabilising fact against "
    "another's deteriorating one), and which way you read the pair. Every such "
    "sentence NAMES BOTH ORDINALS — [[ref:2]] with [[ref:5]], never [[ref:2]] "
    "against \"the other desks\" — because a correlation naming both sides is "
    "one a reader can check against the record and one naming a single side is "
    "not. And HEDGE THE LINK ITSELF: two desks reporting compatible facts in "
    "one window are 'consistent with' a common driver and are 'insufficient on "
    "their own' to establish one. Write that, not 'because', 'driving', or "
    "'indicating' — no desk on this record performed a causal test, so a causal "
    "verb is a claim the record cannot carry and is marked as one; "
    "(3) '## What would change this' — the ONE observation that would most move "
    "this reading of this country, and which dimension you are least sure of; "
    "(4) '## What this reading misses' — the aperture, and it is about the "
    "COUNTRY, not about this instrument. The record hands you THE DECLARED "
    "APERTURE: the units it could have carried and did not, BY NAME — the "
    "desk and target of every read it ranked below the cut, everything that "
    "sat below the verification floor, and any declared roster unit with no "
    "read at all. NAME THOSE, in the arithmetic block's own words and with "
    "an ordinal, and then say what their absence costs the reader: what may "
    "be going on in this country that this record therefore could not see. A "
    "place, actor or dimension that appears NOWHERE in that list and nowhere "
    "in a block you cite is a GUESS, and it is marked as one. Where the ledger "
    "says a count has no names behind it, say how much is unseen and never "
    "which. What this section is NOT is a review of the tier: \"the record "
    "lacks granular trade volumes\" is a note about the machine, and a reading "
    "that spends its blind-spot section on the machine has not named a blind "
    "spot at all.",
    title_rule=_TITLE_RULE_COMPOSITION,
)


#: The COUNTRY voice. A separate constant, never a parameterised world prompt.
#:
#: Most of the world prompt is tier-neutral and is carried VERBATIM: the ordinal
#: fence, the fact/perspective split, the crown clause, what the voice may not
#: set, THE INSTRUMENT IS NOT THE WORLD (v2), the marks notice, the standards,
#: the register and the output contract. Three things are genuinely different
#: and each is a fact about the record rather than a preference:
#:
#:   1. WHAT IT IS HANDED. The world voice is told "no desk read beyond what the
#:      record quotes", and that is TRUE there. At the country tier it would be
#:      FALSE: every block carries its origin desk head in FULL below the record
#:      (the ``context_body`` span), because a cross-dimension argument cannot be
#:      made from eight lead sentences. Telling a model it does not have text it
#:      is looking at is how a prompt teaches a model to distrust its own input.
#:      So the clause is restated accurately — you have the record AND the desk
#:      reads it quotes from, and NOTHING ELSE.
#:   2. WHAT THE JOB IS. One place, many dimensions, one window: correlation and
#:      tension BETWEEN dimensions, which is a different act from weighing
#:      separate world threads.
#:   3. THE SCOPE FLOOR. Every block on this record is bounded to ONE country, so
#:      a regional or global word is a widening the record cannot support —
#:      ``assessment_unsupported``'s scope class fires on exactly that, and here
#:      it fires on every block rather than on some.
COUNTRY_ASSESSMENT_SYSTEM: str = f"""TASK — THE COUNTRY ASSESSMENT. You are handed ONE assembled record: this country's verified desk reads for this window, each quoted verbatim under an ordinal handle [[ref:N]], with the arithmetic that ordered them, and with each desk's FULL read printed below the record. Write the interpretive read that sits BESIDE that record, in its own clearly-labelled band, under your own headline.

WHAT YOU ARE. You are not the record and you are not the machine. You are an argument ABOUT the record. The record states what each desk found, in its words, with its origins; you say what they MEAN TOGETHER for this one country — what you weigh heavily, what you discount, what you would watch. That is the whole of your job, and it is the one thing this product no longer has: every composition tier now quotes its inputs and writes no sentence of its own, so the cross-dimension read of a country exists only if you write it.

THE ONE QUESTION. These blocks are several DIMENSIONS of the SAME place in the SAME window — energy security, escalation, internal stability, leadership, military posture, economic coercion, narrative coordination, and on some desks proliferation. A reader can already see each dimension's own verdict; it is printed above you. What a reader cannot see, and what you are for, is the JOINT read: which dimensions are telling one story and which are telling two, where one desk's fact makes another desk's fact more or less worrying, and where two of them point in opposite directions and the record leaves that unresolved.

WHAT YOU ARE HANDED, AND WHAT YOU ARE NOT. You are handed the record AND, below it, the full text of each desk read the record quotes — nothing else. No wire items, no other country's read, no search, no desk that is not on this record. If a fact is not in the record or in one of those desk reads, YOU DO NOT HAVE IT. Not "you should be careful with it" — you do not have it. Everything in a desk read is quotable under that desk's ordinal: the record's quoted sentence and the read it was cut from carry the same ordinal and the same warrant.

THE ORDINAL FENCE. Cite with [[ref:N]] using ONLY the ordinals shown below. Write the marker EXACTLY like this — [[ref:3]] — two square brackets, the word ref, a colon, the number, two closing brackets. Not (ref 3), not [3], not "block 3": those are not citations and a read that carries none is not published. An ordinal you invent, or one outside the range you are shown, FAILS THE RUN — it is a construction error, not a style note. Every sentence you write names at least one ordinal, including the interpretive ones: naming the blocks you are weighing is how a reader checks your argument against the words it rests on.

FACT vs PERSPECTIVE — the two citation classes. A sentence stating WHAT A BLOCK SAYS is a FACT: it carries the ordinal of the block that says it, and it may not go beyond what that block — its quoted sentence or its full desk read — states. Your own weighing (what these dimensions MEAN together, which reading you privilege, what you would watch next, where you think a desk is thin) is PERSPECTIVE: it still names the ordinals it is about, but it is allowed to be an ARGUMENT rather than a paraphrase. Most of a good Assessment is legitimately perspective. What perspective may NEVER do is smuggle in a fact: a new place, actor, number, date or event that no block states is fabrication whether it is hedged or not.

THE CROSS-DIMENSION RULE, and it is the one you will be graded on. When you connect two dimensions, NAME BOTH ORDINALS IN THAT SENTENCE and HEDGE THE CONNECTION ITSELF. Two desks reporting compatible facts in the same window are "consistent with" a shared driver; either one alone is "insufficient on its own" to establish it. No desk on this record ran a causal test, so "because", "driving", "indicating", "as a result of" and "leading to" assert something the record cannot carry — a deterministic pass marks those spans, in public, beside your prose. The honest phrasing is also the cheap one: "the export disruption [[ref:1]] and the protest wave [[ref:4]] are consistent with a single fiscal shock, though neither desk tests that link". Correlation you can see; causation you were not given.

SCOPE — EVERY BLOCK ON THIS RECORD IS ONE COUNTRY. A sentence that says "the region", "the Gulf", "globally" or "worldwide" is claiming something about places no block on this record covers, and it is marked. Keep the scope word the desk used. Where a desk's own sentence is bounded ("in collected reporting", "among the monitored sources", "in this window"), keep that bound too: republishing a collection-scoped absence as a fact about the country is the single most common way this tier says something false.

THREADS, WEIGHTED, UNCROWNED — and the verdict you are handed. The record's own arithmetic has already decided whether one dimension genuinely outweighs the rest, on a measured key, over the whole candidate pool. That verdict is printed for you below. YOU MAY NOT CROWN AGAINST IT: if the record says concentration was NOT earned this cycle, you do not open by declaring one dimension the top risk — you carry two to four and say how they weigh against each other. If it says concentration WAS earned, one may lead and you name the specific development rather than the category. Where you disagree with the arithmetic, say so as a disagreement and name the number you are disagreeing with; do not quietly write over it.

WHAT YOU MAY NOT SET. The record's headline, its severity and its confidence are the RECORD's columns, decided deterministically from what it carries. You do not set them, restate them as your own verdict, or write a sentence whose only content is the severity word. Your headline is yours and appears only in your own band.

THE INSTRUMENT IS NOT THE WORLD, and this is the clause most likely to cost you. You are shown the record's machinery: each block's cited mass, verify score, severity and age on its attribution line, and below the record how many reads were carried, how many sat under the floor, how many candidates were ranked. All of it is handed to you SO YOU CAN DECIDE — which dimension to lead with, which to discount, where to hedge, what to call thin. It is your instrument panel. It is not your subject. Do NOT write that a read's cited mass is 9.10, that its verification score is higher or lower than another's, that six blocks "consist mainly of meta-statements", or that the tier "does not contain" some class of data. Write the DECISION the number produced, in the country's own nouns. The weight lives in which dimension you lead with and how confidently you write it — never in a number recited back about the machine.

THE ONE PLACE A RECORD NUMBER IS STILL YOURS TO STATE, and the exact shape it must take. The counters in THE RECORD'S OWN ARITHMETIC block below — reads carried, shown and not carried, candidates ranked, reads below the verification floor, tensions declared out of pairs examined, roster units covered, and the lead test's own share and ratio — ARE yours to state. They travel with your evidence: every ordinal you cite carries that whole arithmetic block beneath the rule "--- THE RECORD THIS BLOCK SITS IN ---", which is how a number of yours gets checked at all. So if you state one, state it in the arithmetic block's own words AND carry a [[ref:N]] in the same sentence. Anything printed on a BLOCK's attribution line instead — cited mass, verify score, severity, produced-at — does NOT travel with your evidence, and a sentence resting on one is unsupported however accurately you copied it off the page.

YOUR MARKS PUBLISH WITH YOU. A deterministic pass reads what you write and marks, inline and in public, any span that the record cannot support: a cross-block RANKING claim the record never performed; an unbounded SCOPE word over blocks each bounded to this one country; a CAUSAL connective welding two dimensions the record only places side by side; a collection-scoped absence in a block republished as a fact about the country; and a sentence whose subject is the INSTRUMENT rather than the world. Those marks appear beside your prose with their reasons — your sentence is never deleted and never edited. So the cheap move is the honest one: attribute the ranking to the record's own order, keep the scope word the block used, and say "these two sit side by side" where that is what the record shows.

{_STANDARDS}

REGISTER. Specificity is the craft target: dates, magnitudes, named actors, the named brake on a trend — all of them already in the blocks and in the desk reads below them, so every one adds checkable surface rather than risk. No atmosphere, no scene-setting, no inferred motive, no adjective doing the work a cited number should do. Do not open on the as-of stamp — open on the country. Do not rebuild yesterday's headline with today's nouns. Never write "the most plausible near-term trajectory", "the dominant <X> vector", or "steady tension"; if a sentence could have run on any day of the year, or about any country, it has not read this record.

{COUNTRY_ASSESSMENT_BODY_SHAPE}

OUTPUT. Respond with PROSE, in markdown — NOT JSON. The FIRST line is your headline, alone, in bold (**like this**). Then a blank line, then the body. No fences, no preamble, no commentary about the task."""


def has_context_spans(payload: Mapping[str, Any]) -> bool:
    """Does ANY block on this record carry a ``context_body`` span?

    THE SWITCH, and it is a fact about the RECORD rather than a flag, a
    descriptor knob or a tier name. A world payload built before STEP E, one
    built on a day no country voice produced, and one whose blocks are all
    thematic all answer ``False`` and take the v3 prompt byte-for-byte — which
    is the property that makes the rollout reversible without a second code
    path: stop stamping context and the world voice is v3 again, with the
    version string on the row saying so.
    """
    for block in (payload or {}).get("blocks") or []:
        if context_spans(block):
            return True
    return False


def system_prompt_for(payload: Mapping[str, Any]) -> str:
    """The voice this RECORD gets — chosen from the payload, never from an id.

    Total, and world-by-default: anything that is not a country-tier assembly
    and carries no context answers :data:`ASSESSMENT_SYSTEM`, byte-for-byte what
    D-6 shipped. The choice is made from ``payload`` rather than from the
    running analyst id because the payload is the only thing this channel may
    read, and a voice selected from a descriptor would be a second input to a
    run whose whole warrant is that it has one.

    STEP E adds the THIRD arm, on the same principle one step further in: a
    WORLD record that carries its countries' assessments is a different record
    from one that carries eight sentences, so it gets a different voice. The
    test is the spans, not the tier — see :func:`has_context_spans`.
    """
    if str((payload or {}).get("tier") or "") == TIER_COUNTRY:
        return COUNTRY_ASSESSMENT_SYSTEM
    if has_context_spans(payload):
        return WORLD_ASSESSMENT_SYSTEM_V5
    return ASSESSMENT_SYSTEM


def prompt_version_for(payload: Mapping[str, Any]) -> str:
    """The version string stamped on the row for THIS record's voice.

    Same total, same default, same reason as :func:`system_prompt_for`, and the
    two must never disagree — a row stamped ``assessment_prompt.v3`` that was
    written by the country voice is a row nobody can attribute, and so is one
    stamped v3 that was written by the voice that read the country reads.
    """
    if str((payload or {}).get("tier") or "") == TIER_COUNTRY:
        return COUNTRY_PROMPT_VERSION
    if has_context_spans(payload):
        return PROMPT_VERSION_V5
    return PROMPT_VERSION


def desk_reads_in_full(payload: Mapping[str, Any]) -> str:
    """The context section: each block's origin desk head, whole, under its ordinal.

    THE COUNTRY TIER'S EXTRA PAGE, and the reason the tier needed one. The
    record quotes one sentence per desk; a cross-dimension argument lives in the
    paragraphs that sentence opens. The assembler therefore carries each block's
    origin head in FULL as a ``context_body`` span — byte-identical, through the
    same construction gate as the quoted lead — and this renders it.

    STILL INSIDE THE FENCE. Every byte here came off ``payload``, which is the
    only argument :func:`build_assessment_prompt` has. Nothing is read, nothing
    is fetched, and the evidence map follows automatically:
    ``assessment_unsupported.spine_span_text`` joins a block's spans, so an
    ordinal's evidence text becomes the lead span AND the desk read — both of
    which the voice was handed, which is what makes both fidelity-to-spine.

    STEP E — THE SAME SECTION, ONE TIER UP, AND THE LEDE IS DIFFERENT BECAUSE
    THE TEXT IS. At the WORLD tier a block's context is not a desk read; it is
    that country's own ASSESSMENT — another analyst's hedged argument about that
    country, with its own citations to its own record. Printing it under a lede
    that says "these are desk reads, same desk, same verification" would be
    telling the voice a false thing about text it can see, which is the exact
    error P3-A corrected in the other direction. So the header and the lede are
    picked by tier (:data:`CONTEXT_SECTION_HEADER` /
    :data:`COUNTRY_SECTION_HEADER`) and the country tier's bytes are unchanged —
    a test pins them.

    ``""`` when no block carries a context span, so the world payload built
    before STEP E and every thematic prompt are byte-identical to what D-6
    shipped.
    """
    is_country_tier = str((payload or {}).get("tier") or "") == TIER_COUNTRY
    # STEP E — THE RECORD'S OWN NAME MAP, and it is needed at THIS tier and not
    # at the country one. A carried world block's ``target_name`` is the CHILD's
    # (``carry_block`` copies it byte-identical), and the child resolved no
    # names, so live world blocks carry ``target_name == target_id`` on all
    # eight — measured on record ``ae07ce8c``, 2026-09-20. The WORLD payload
    # does publish a resolved map (Amendment 7f, ``target_names=`` → the
    # ``PAYLOAD_NAMES_KEY`` block), so the country is nameable from the record
    # itself rather than from a block that never learned its own name. Empty on
    # a pre-7f payload, and the header degrades to the slug.
    names = payload_names(payload)
    parts: list[str] = []
    for block in (payload or {}).get("blocks") or []:
        if not isinstance(block, Mapping):
            continue
        # ``quoted_text`` rather than the raw span, for the ONE reason the record
        # render gives: a child ``[[ref:N]]`` inside the quoted text would
        # collide with THIS record's ordinal space and be read by the voice — and
        # then by the marker resolver — as a reference to a block. It is a no-op
        # on a first-order desk body, which is every origin the country tier has.
        # The CANONICAL text stays ``spans[].text``, which is what the evidence
        # map carries; that is the same split the record body already makes.
        texts = [
            quoted_text(span)
            for span in (block.get("spans") or [])
            if isinstance(span, Mapping)
            and str(span.get("role") or "") == SPAN_ROLE_CONTEXT_BODY
            and str(span.get("text") or "").strip()
        ]
        if not texts:
            continue
        ordinal = block.get("ordinal")
        subject = unit_label(block.get("target_name"), block.get("target_id"))
        question = str(block.get("question") or "")
        if is_country_tier:
            # THE DESK is what the country tier's reader is being handed, and
            # the country is the same on every block, so the desk leads.
            header = f"--- [[ref:{ordinal}]] {question}".rstrip()
            if subject:
                header = f"{header} ({subject})"
        else:
            # STEP E — THE COUNTRY leads, because at this tier the country is
            # what varies and the desk name would mislabel the text underneath:
            # the block's ``question`` is the desk whose SENTENCE the record
            # quotes, while the body below is that country's WHOLE read across
            # all of its desks. Naming it "escalation" would be false of most of
            # its paragraphs.
            label = unit_label(
                names.get(str(block.get("target_id") or ""))
                or block.get("target_name"),
                block.get("target_id"),
            )
            header = f"--- [[ref:{ordinal}]] {label or question}".rstrip()
            header = f"{header} — the country's own assessment"
        parts.append(header + " ---")
        parts.extend(texts)
        parts.append("")
    if not parts:
        return ""
    if is_country_tier:
        return "\n".join([
            CONTEXT_SECTION_HEADER + ".",
            "",
            "Each block above quotes ONE sentence from a desk read. Below is each "
            "of those reads IN FULL, under the same ordinal. They carry the same "
            "warrant as the quoted sentence — same desk, same verification, same "
            "window — so anything in them is yours to cite under that ordinal. "
            "This is the text the cross-dimension argument is made out of; the "
            "record above is what a reader sees.",
            "",
            *parts,
        ]).rstrip() + "\n"
    return "\n".join([
        COUNTRY_SECTION_HEADER + ".",
        "",
        "Each block above quotes ONE desk sentence, carried up from that "
        "country's own assembled record. Below is that COUNTRY'S OWN "
        "ASSESSMENT in full, under the same ordinal — the cross-dimension read "
        "written by the analyst who saw all of that country's desks for this "
        "window. It carries the same ordinal and the same warrant as the quoted "
        "sentence, so anything in it is yours to cite under that ordinal. It is "
        "also an ARGUMENT rather than a report: its judgements are its own, and "
        "where you carry one forward you say whose it is. Its citation numbers "
        "point at ITS record, never at this page. This is the text the "
        "cross-country argument is made out of; the record above is what a "
        "reader sees.",
        "",
        *parts,
    ]).rstrip() + "\n"


def render_lead_test(payload: Mapping[str, Any]) -> str:
    """The concentration verdict, in the words §1.5.3 requires it to be handed in.

    Rendered VERBATIM into the prompt so the interpretive voice cannot crown
    against the record's own arithmetic without visibly contradicting a number
    printed on its own page.
    """
    lead = payload.get("lead") or {}
    test = lead.get("test") or {}
    earned = bool(test.get("earned"))
    ratio = test.get("ratio_12")
    ratio_txt = "no second candidate" if ratio is None else f"{float(ratio):.2f}"
    share = test.get("top_share")
    share_txt = "n/a" if share is None else f"{float(share):.3f}"
    verdict = (
        "concentration EARNED this cycle"
        if earned
        else "concentration NOT earned this cycle"
    )
    kind = str(lead.get("kind") or "none")
    ordinals = ", ".join(str(o) for o in (lead.get("block_ordinals") or [])) or "none"
    return (
        f"{verdict}: top-share {share_txt}, ratio {ratio_txt} against bars "
        f"{test.get('bar_share')} / {test.get('bar_ratio')}, over "
        f"{test.get('n_candidates')} candidates (minimum "
        f"{test.get('min_candidates', 8)}). Lead state: {kind}; "
        f"lead ordinals: {ordinals}."
    )


#: How many declared tensions render WHOLE into the record's own arithmetic
#: before the block stops and counts the remainder, and how much of each side's
#: own words rides with them.
#:
#: THE DEFECT THESE ANSWER (measured 2026-09-08 over the four post-G3 world
#: spines). ``_tension_split`` publishes COUNTERS — "1 declared out of 228 pairs
#: examined, of which 0 between two carried reads and 1 against a read this
#: record did NOT carry" — and the counters are quotable and gradeable. The
#: tension's CONTENT is not: the ``statement`` (which names both sides and the
#: shared dimension) reached the evidence map on 0 of 2 tension-bearing spines,
#: and the uncarried side's own words (``b_ref.span``) on 0 of 2. Only
#: ``b_ref.target_id`` ever arrived, on 1 of the 2, and incidentally — via the
#: drop ledger, which lists it for a different reason.
#:
#: So the voice is handed a tension in the rendered body ("[[ref:6]] / a read
#: that was not carried — <statement>"), told it may neither invent one nor
#: dissolve one, and then graded against a map holding a COUNT. Every tension
#: sentence the channel has ever written failed: ``soft_fail`` on 2026-09-06
#: 12:15Z live, ``soft_fail`` on 2026-09-07 00:15Z live, and a third in the
#: 2026-09-07 00:00Z replay. Three for three, and the judge was right each time
#: — nothing in the map said WHICH pair the detector chose or what either side
#: actually said. This is §2's defect on a second surface, and the same repair:
#: the record knows, so the record publishes it.
#:
#: THE PRICE, MEASURED ON THE TWO LIVE TENSION-BEARING SPINES (2026-09-07
#: 00:00Z and 2026-09-08 12:00Z). One declared tension rendered whole —
#: statement, both sides' identities and head ids, the safe side's own words,
#: the carriage flag and its why-class — costs the arithmetic block **+1,154 and
#: +1,221 bytes** (2,672 → 3,826 and 3,107 → 4,328). The block is byte-identical
#: in every citation, so at ``BLOCK_CAP`` = 8 the evidence map grows 22,753 →
#: 31,985 B and 26,144 → 35,912 B. Live world records run 0-1 declared tensions,
#: so that IS the live cost, and ~230 of those bytes are the section's one-off
#: lead rather than the tension.
#:
#: ONE SIDE'S WORDS ARE WITHHELD ON BOTH LIVE SPINES, and that is a measured
#: trade rather than an oversight — see :func:`_tension_side_line`. Quoting the
#: uncarried side raw put a desk absence sentence into all 8 citations on both
#: draws. The ``statement`` is unaffected and it is the truthmaker a tension
#: sentence rests on, so the defect this train exists for is fixed either way.
#:
#: THAT IS AGAIN THE LARGEST MAP THIS SURFACE HAS PRODUCED — on top of the
#: roster fix's own +799, a rostered tension-bearing spine reaches ~33 KB
#: (~8.5k tokens, still inside the 32,000-token input budget). G3 §4.6 named
#: DILUTION as a live hypothesis and the roster train did not settle it, so this
#: train does not assert the trade either: it MEASURES it, arm B against arm C
#: over the four post-G3 spines, with the two tension-less spines as the
#: byte-identity control.
#:
#: WHY A CAP AT ALL. ``assembly_payload._tensions`` is unbounded — it walks all
#: 228 pairs and appends every one the detector fires on — so a pathological
#: record could hand this block two hundred tensions. Two render whole because
#: two is one more than any live record has produced and the remainder is
#: COUNTED rather than hidden (the ``_named`` precedent). The span slice is
#: sized off the live block spans (p50 92, p90 237, max 451 over the four
#: post-G3 spines) so a typical verdict sentence rides whole.
#:
#: THE DETECTOR'S ``detail`` IS DELIBERATELY NOT RENDERED. D-1 §1.6 rule 1 is
#: DECLARE, never EXPLAIN — an inferential link between two spans is on the
#: MAY-NOT side — and ``detail`` is exactly that link ("the composition
#: attributes INCREASE to a desk whose own verdict is DECREASE"). Putting it in
#: the map would make the explanation quotable and licence the sentence the
#: statement template is written to refuse.
#:
#: WHAT THE REPLAY ACTUALLY FOUND, AND IT DOES NOT SUPPORT A FIDELITY CLAIM
#: (4 post-G3 spines × 2 rounds × 2 arms, 16/16 graded, no ``judge_empty``;
#: ``planning/ASSESSMENT_TENSIONS_IN_MAP_REPORT.md`` §5). Tension-relay
#: sentences graded **0 of 3 supported in the shipped arm and 0 of 3 here**. The
#: truthmaker is in the map — that part is deterministic and proven above — and
#: it did not move the verdict, because two other causes bind first:
#:
#:   1. THE SLUG IS MISTRANSLATED (3 of the 6 relays across both arms). The
#:      statement names the uncarried side ``country_watch_kp``; the core model
#:      wrote "Pakistan", and the judge was right to fail a country that is
#:      nowhere in the map. NOTHING IN THIS CHANNEL CAN FIX THAT: the spine row
#:      carries no human name for any unit — ``target_name == target_id`` on
#:      every block and every drop row of both live tension-bearing spines. It
#:      needs a target-name join upstream of ``assembly_payload``.
#:   2. THE RELAY IS COMPOUNDED WITH A WEIGHING RIDER (2 of the 6) — "…and it
#:      does not alter the dominant Iran-Russia pair" — which is a claim about
#:      OTHER blocks, and the judge grades a claim against the sub-claim its
#:      marker names. That is the ``weighted_comparison`` family, not this one.
#:
#: And the two tension-less spines are a SAME-INPUT control (byte-identical maps
#: in both arms), so their arm-to-arm swing — +0.123 and −0.039 on
#: ``citation_support`` — is pure sampling. The overall arm gap (−0.070) sits
#: inside it. So this train is landed as *"the map now contains what the voice
#: was shown"*, which is proven, and NOT as *"this raises fidelity"*, which this
#: replay is under-powered to show either way. No new mark class appeared in the
#: new arm, and no ``attribution_asserts_desk_negative`` in either.
TENSION_RENDER_CAP: int = 2
TENSION_SPAN_CHARS: int = 240

#: The ceiling on the whole rendered tension block, as a TEST rather than a
#: slice — the :data:`APERTURE_ROSTER_BUDGET_CHARS` precedent. A record whose
#: tensions outgrow this must fail loudly and get this decision made again with
#: the real number in hand, never silently truncate back into a count.
TENSION_BUDGET_CHARS: int = 2_400


def _tension_side(
    block: Mapping[str, Any] | None,
    ref: Mapping[str, Any] | None,
    span_index: int = 0,
) -> tuple[str, str, str]:
    """One side of a tension as ``(who, head_id, span)``.

    Two shapes reach here and both are already on the row. A CARRIED side is a
    block, so its words are ``spans[span_index].text`` and its head is
    ``finding_id``. An UNCARRIED side is ``b_ref``, which the detector built
    from the dropped head itself and which already carries ``span`` — the
    dropped desk's own verdict sentence, not a paraphrase of it.
    """
    if block:
        spans = list(block.get("spans") or [])
        text = ""
        if 0 <= span_index < len(spans):
            text = str(spans[span_index].get("text") or "")
        desk = str(block.get("desk") or "").strip()
        target = unit_label(
            block.get("target_name"), block.get("target_id")
        ).strip()
        who = f"{desk} on {target}" if desk and target else (desk or target or "?")
        return who, str(block.get("finding_id") or ""), text
    ref = ref or {}
    desk = str(ref.get("desk") or "").strip()
    # Amendment 7f. THIS LINE IS THE 09-07 DEFECT: it named the uncarried
    # side ``country_watch_kp`` and nothing else, and the voice rendered it
    # "Pakistan" — the wrong country, unmatched by anything in the map, and
    # failed by the judge, three times across two arms. With the name in
    # front of it a mistranslation is a CHECKABLE CONTRADICTION rather than
    # an unanswerable guess.
    target = unit_label(ref.get("target_name"), ref.get("target_id")).strip()
    who = f"{desk} on {target}" if desk and target else (desk or target or "?")
    return who, str(ref.get("finding_id") or ""), str(ref.get("span") or "")


def _tension_quote(text: str) -> str:
    """A side's own words, trimmed to :data:`TENSION_SPAN_CHARS` and quoted.

    The ellipsis is load-bearing: a trimmed span that does not SAY it is trimmed
    is one a voice can quote as a complete sentence the record never carried.
    """
    flat = " ".join(str(text).split())
    if not flat:
        return "—"
    if len(flat) > TENSION_SPAN_CHARS:
        flat = flat[:TENSION_SPAN_CHARS].rstrip() + "…"
    return f"“{flat}”"


#: What stands in for a side's own words when quoting them would MANUFACTURE a
#: desk negative. Phrased with no absence idiom of its own — the constraint the
#: aperture block's "not computed at this grain" line already proved.
TENSION_SPAN_WITHHELD: str = (
    "its own words stay out of this block (quoted here they would read as this "
    "record's denial rather than that read's); open the head above to see them"
)


def _tension_side_line(label: str, who: str, head_id: str, span: str) -> str:
    """One side of a tension as a line, with the FALSE-POSITIVE GUARD on it.

    THE HAZARD, AND IT IS LIVE. This block rides into every citation's
    ``evidence_text``, and ``composition_integrity.fold`` — which DOES run on
    this channel, because the channel uses the ``[[ref:N]]`` sub-claim
    convention — reads each ``evidence_text`` as the cited desk head's own text.
    So a desk verdict sentence quoted here becomes, for all eight ordinals, a
    sentence "the cited desk denied", and ``asserts_desk_negative`` /
    ``absence_scope_laundered`` can then charge an Assessment claim against a
    denial made by a read this record did not even carry.

    That is not hypothetical. Measured 2026-09-08 on both live tension-bearing
    spines, the uncarried side's ``b_ref.span`` IS an absence sentence — "no new
    supply disruptions or price shocks emerging in this window" (09-07 00:00Z),
    "no new developments easing the situation" (09-08 12:00Z) — and rendering it
    raw moved the absence set on 8 of 8 ordinals. Cross-tier tensions are
    declared precisely when one side reports a change and the other reports
    none, so the dangerous shape is the COMMON one here, not the edge.

    The repair is the roster train's, at the same place: PHRASING, checked
    rather than assumed. The span rides whole when it is safe; when it would
    manufacture a negative, the identity and the head id still ride and the
    words are named as withheld. Either way the ``statement`` — the truthmaker a
    tension sentence actually rests on — is unaffected, so the fix this train
    exists for lands in both branches.
    """
    line = f"    - {label}, {who} (head {head_id or 'unstated'}): "
    quoted = f"{line}{_tension_quote(span)}"
    if span and desk_absence_sentences(quoted):
        return f"{line}{TENSION_SPAN_WITHHELD}."
    return quoted


def declared_tensions(payload: Mapping[str, Any]) -> str:
    """THE DECLARED TENSIONS, whole — both sides quoted, the pairing named.

    See :data:`TENSION_RENDER_CAP` for the defect this answers and the bytes it
    costs. The short version: the counters travelled and the content did not, so
    a tension sentence had no truthmaker in the map and went 0-for-3 live.

    THE FENCE IS NOT WIDENED BY ONE BYTE. Every string here is already in
    ``payload`` — ``tensions[].statement``, ``tensions[].b_ref.span`` and the
    carried block's own ``spans`` — which remains this module's only argument
    and the AST guard on it is unchanged. Nothing new is read; something already
    carried stops being thrown away.

    BYTE-IDENTICAL WHEN THERE ARE NO TENSIONS. Returns ``""`` on a payload with
    an empty or missing ``tensions`` list, and :func:`record_arithmetic` appends
    it conditionally — so every roster-less, tension-less record renders the
    arithmetic block exactly as it shipped.
    """
    tensions = [t for t in (payload.get("tensions") or []) if t]
    if not tensions:
        return ""
    blocks = {
        int(b.get("ordinal") or 0): b for b in (payload.get("blocks") or [])
    }
    total = len(tensions)
    parts: list[str] = [
        "- THE DECLARED TENSIONS, IN THE RECORD'S OWN WORDS — the pairs the "
        "detector chose, both sides quoted. A tension sentence of yours RELAYS "
        "one of these and says which side was carried; a pairing not on this "
        "list is one you invented."
    ]
    for n, t in enumerate(tensions[:TENSION_RENDER_CAP], 1):
        a = t.get("a") or {}
        a_block = blocks.get(int(a.get("ordinal") or 0))
        a_who, a_id, a_span = _tension_side(
            a_block, None, int(a.get("span_index") or 0)
        )
        b = t.get("b") or None
        if b:
            b_block = blocks.get(int(b.get("ordinal") or 0))
            b_who, b_id, b_span = _tension_side(
                b_block, None, int(b.get("span_index") or 0)
            )
        else:
            b_who, b_id, b_span = _tension_side(None, t.get("b_ref"))
        # The carriage flag is the whole difference between a conflict this
        # record PUBLISHED and one its own drop ledger is the evidence for
        # (W-1), and it is the half a voice got wrong on 2026-09-06 12:15Z.
        carried = bool(t.get("b_carried")) if "b_carried" in t else b is not None
        why = str(t.get("b_why") or "").strip()
        # The prefix carries only what the statement does NOT: the ordinal to
        # cite and the carriage flag. Restating both desks here would duplicate
        # ~200 bytes of the statement into a block that rides eight times.
        b_ord = (b or {}).get("ordinal")
        b_names = (
            f"carried block [[ref:{b_ord}]]"
            if carried and b_ord
            else "a read this record did NOT carry"
            + (f" ({why})" if why else "")
        )
        parts.append(
            f"  * tension {n} of {total} — carried block "
            f"[[ref:{a.get('ordinal')}]] against {b_names}. The record states "
            f"it thus: “{' '.join(str(t.get('statement') or '').split())}”"
        )
        parts.append(
            _tension_side_line(
                f"the carried side, block {a.get('ordinal')}",
                a_who, a_id, a_span,
            )
        )
        parts.append(
            _tension_side_line(
                f"the {'carried' if carried else 'uncarried'} other side",
                b_who, b_id, b_span,
            )
        )
    if total > TENSION_RENDER_CAP:
        parts.append(
            f"  * and {total - TENSION_RENDER_CAP} further declared "
            f"{'tension' if total - TENSION_RENDER_CAP == 1 else 'tensions'} "
            f"not quoted here — the count above is the record's, and a tension "
            f"you cannot read on this list is one you may not write."
        )
    return "\n".join(parts)


def _tension_split(checked: Mapping[str, Any]) -> str:
    """W-1 — the tension counter, split into its two populations.

    ``pairs_found`` alone was the number that misled. A declared tension between
    a carried head and one this record did NOT carry is not a conflict the
    record made, and a voice told only "1 declared" narrated the 2026-09-06
    12:00Z world read's lone cross-tier pair as a live conflict between two
    published reads — a sentence the evidence map could not support, because
    nothing in the map said the second side was carried.

    Split, the same fact is quotable and gradeable: "1 declared, 0 of them
    between two carried reads". Guarded on PRESENCE of the split keys, so an
    assembly written before W-1 renders this line byte-for-byte as it always
    did and every replay of an older record is unchanged.
    """
    if "pairs_found_carried" not in checked:
        return ""
    carried = int(checked.get("pairs_found_carried") or 0)
    uncarried = int(checked.get("pairs_found_uncarried") or 0)
    if not (carried or uncarried):
        return ""
    return (
        f", of which {carried} between two carried reads and {uncarried} "
        f"against a read this record did NOT carry (a cross-tier pair is "
        f"evidence for the drop ledger, NOT a conflict this record published)"
    )


def record_arithmetic(payload: Mapping[str, Any]) -> str:
    """The counters the record publishes about itself, as facts for the voice.

    Every number here is ON THE ROW — the drop counts, the tension counters, the
    coverage ledger's own length. Nothing is derived, so the voice cannot be
    handed a number the reader will not find beneath it.

    PUBLIC since 2026-09-05, and the rename is the fix. This block is one of the
    THREE things :func:`build_assessment_prompt` hands the voice (D-6 §1.1), and
    the prompt tells it in terms *"you may quote them"* — but the EVIDENCE MAP the
    Assessment is then graded against carried only the second of the three, the
    blocks' quoted spans. So a sentence resting on the record's own arithmetic —
    "the record notes six reads below the verification floor", "top-share 1.000"
    — was faithful to the record and ungradeable against the map, and the judge
    could only mark it unsupported. ``assessment_channel`` now mints the same
    bytes into every citation's ``evidence_text``, so the map is the record the
    voice was shown rather than a third of it.
    """
    drops = payload.get("drops") or {}
    counts = drops.get("counts") or {}
    checked = payload.get("tension_checked") or {}
    coverage = list(payload.get("coverage") or [])
    blocks = list(payload.get("blocks") or [])
    invisible = counts.get("invisible_heads")
    lines = [
        "THE RECORD'S OWN ARITHMETIC — these are facts about the record, "
        "handed to you SO YOU CAN DECIDE what to weigh. You may quote them "
        "where the aperture needs them, in these words and with an ordinal in "
        "the same sentence; you may not contradict them; and you may not spend "
        "the reading narrating them (see THE INSTRUMENT IS NOT THE WORLD).",
        f"- {render_lead_test(payload)}",
        f"- {len(blocks)} reads carried; "
        f"{int(counts.get('shown_not_carried') or 0)} shown and not carried; "
        f"{int(counts.get('candidates') or 0)} candidates ranked; "
        f"{int(counts.get('below_floor') or 0)} below the verification floor.",
        (
            f"- {int(invisible)} desk reads were never candidates for this "
            f"surface at all (below its target grain)."
            if invisible
            else "- how many desk reads never reached this surface at all was "
            "NOT measured this run; do not state a number for it."
        ),
        (
            f"- tensions: {int(checked.get('pairs_found') or 0)} declared out of "
            f"{int(checked.get('pairs_examined') or 0)} pairs examined "
            f"({checked.get('scope_note') or 'scope unstated'})"
            f"{_tension_split(checked)}. The detector "
            f"chose these pairs, not the tier: you may neither invent a tension "
            f"nor dissolve one."
            if checked
            else "- tensions: not checked on this record."
        ),
        # THE TENSIONS THEMSELVES, when there are any. Appended CONDITIONALLY
        # rather than as a member of this list, because an empty member would
        # add a blank line and every tension-less record's arithmetic block must
        # stay byte-identical to what shipped (a test asserts exactly that).
        *([declared_tensions(payload)] if payload.get("tensions") else []),
        (
            f"- coverage: {len(coverage)} roster units accounted for in the "
            f"persisted ledger."
            if coverage
            else "- coverage: this tier's ledger is not computed at this grain; "
            "say nothing about roster coverage."
        ),
        declared_aperture(payload),
    ]
    return "\n".join(lines)


#: How many named units the aperture block publishes per class before it stops
#: and says how many more there are, and how much of a head's own title rides
#: with each name.
#:
#: BOTH NUMBERS ARE PAID EIGHT TIMES. This block is about the RECORD rather than
#: about any one block, and it is byte-identical in every citation's
#: ``evidence_text`` — so at ``BLOCK_CAP`` it is repeated eight times into the
#: judge's own prompt. Measured on the live 2026-09-06 spine (8 blocks, 24
#: country compositions below the cut), an unbounded block took the evidence map
#: from 9.1 KB to 26.6 KB; these caps put it at ~17 KB, comfortably inside the
#: 32,000-token input budget with the desks' quoted spans still the bulk of it.
#: Five names is already more than a blind-spot paragraph spends, and the count
#: of what is NOT shown is printed beside them, so nothing is hidden — only
#: unlisted.
APERTURE_NAME_CAP: int = 5

#: How much of a dropped head's own title travels with its name. Enough to
#: identify the read ("Junta airstrikes resume over Sagaing"), not enough to
#: quote it — this is a NAME for the aperture, never evidence for a claim.
APERTURE_TITLE_CHARS: int = 60

#: THE ROSTER IS RENDERED WHOLE, and this is the size that buys it (2026-09-08).
#:
#: THE DEFECT (live 2026-09-07 12:16Z and 2026-09-08 12:15Z, two draws). The
#: declared roster is a list of BARE MACHINE IDENTIFIERS — ``country_g20_de``,
#: sixteen characters — while every other aperture line carries a
#: :data:`APERTURE_TITLE_CHARS` slice of a head's own title. ``APERTURE_NAME_CAP``
#: was sized for the second shape and applied to both, so the world record's
#: 33-unit roster reached the evidence map as *"country_g20_ar; country_g20_au;
#: country_g20_br; country_g20_ca; country_g20_cn; and 28 more"* — five names and
#: an invitation.
#:
#: The voice took the invitation, and it was RIGHT: both draws enumerated all 25
#: uncarried units exactly (roster minus the eight carried), reconstructed from
#: the ISO pattern and world knowledge of G20 membership. 16 and 17 of those
#: names appeared NOWHERE in the evidence map, so the judge — which sees the map
#: and not the roster — graded the sentence ``judge_contradicted`` (HARD) on
#: 09-07 and ``judge_unsupported`` on 09-08.
#:
#: And ``assessment_unsupported.aperture_vocabulary`` fences the blind-spot
#: section to the WHOLE roster, with a docstring saying in terms that
#: ``declared_aperture`` "renders precisely this set, so the fence and the prompt
#: cannot drift: the voice is marked against the same bytes it was handed". The
#: cap made that false: the fence held 33 names and the map showed 5. This
#: constant is the promise made good — the roster is rendered whole and the
#: deterministic fence, the voice and the judge read one list.
#:
#: THE PRICE, MEASURED ON THE THREE LIVE ROSTERED SPINES (09-07 12:00Z,
#: 09-08 00:00Z, 09-08 12:00Z). The whole roster plus the carried set costs the
#: arithmetic block **+799 bytes** on each; the block is byte-identical in every
#: citation, so at ``BLOCK_CAP`` = 8 the evidence map goes 16.6 KB → 23.0 KB,
#: 16.6 → 23.0 and 18.0 → 24.4 (~6.1k tokens at the worst), still inside the
#: 32,000-token input budget with the desks' quoted spans the bulk of it.
#:
#: THAT IS THE LARGEST MAP THIS SURFACE HAS PRODUCED, and G3 §4.6 named DILUTION
#: as a live hypothesis at 21.8 KB — so this train does not assert the trade, it
#: MEASURES it: the N=5 × 3-round replay in
#: ``planning/ASSESSMENT_LIVE_FIDELITY_2026-09-08.md`` is the experiment, and the
#: two roster-less spines in it are the byte-identity control.
#:
#: The four TITLE-BEARING drop-ledger lines keep ``APERTURE_NAME_CAP``, which is
#: the list the cap was sized for and where the bytes actually are (25 rows ×
#: ~90 chars × 8 citations ≈ 18 KB unbounded).
#:
#: A roster that outgrows this budget must not silently truncate back into a
#: guess, so the ceiling is a TEST rather than a slice
#: (``test_the_rendered_roster_stays_inside_the_evidence_map_budget``): it fails
#: loudly and this decision gets made again with the new number in hand.
#:
#: RAISED 1,200 → 1,400 ON 2026-09-08 (Amendment 7f), which is that test doing
#: exactly its job. The roster now renders each unit as ``G20 — Germany
#: (country_g20_de)`` rather than the bare handle, and the live 33-unit roster
#: went 560 → 1,230 chars. The decision, made again with the real number:
#:
#:   * THE COST IS +670 chars, paid eight times (the block is byte-identical in
#:     every citation's ``evidence_text``) — about +5.4 KB on a world record's
#:     evidence map.
#:   * WHAT IT BUYS is the only thing that makes the roster CHECKABLE. The bare
#:     handle was a fence the voice could not read: it wrote "Pakistan" for
#:     ``country_watch_kp`` on a live record — the wrong country — and the
#:     judge failed the sentence because no name at all was in the map to
#:     check it against. A roster of handles fences the vocabulary without
#:     supplying it, which is how a correct-looking guess got written three
#:     times across two arms.
#:   * AND IT IS THE CHEAP HALF OF THE SAME FIX. The drop-ledger lines carry
#:     the same names under ``APERTURE_NAME_CAP`` (five rows), so the roster
#:     line is where the naming actually lands.
#:
#: Seeded at the measured 1,230 + ~14%. The next breach gets the same
#: treatment, not a slice.
APERTURE_ROSTER_BUDGET_CHARS: int = 1_400


def _drop_name(row: Mapping[str, Any]) -> str:
    """One drop-ledger row as a NAME the voice can write and a reader can find.

    ``desk on target`` is the pair the record itself keys blocks by, and the
    head's own title is what makes the name mean something ("escalation on
    country_watch_mm — Junta airstrikes resume over Sagaing"). Truncated,
    because this rides in the evidence map eight times.
    """
    desk = str(row.get("desk") or "").strip()
    target = unit_label(row.get("target_name"), row.get("target_id")).strip()
    title = " ".join(str(row.get("title") or "").split())[
        :APERTURE_TITLE_CHARS
    ].strip()
    who = f"{desk} on {target}" if desk and target else (desk or target or "unnamed")
    return f"{who} — “{title}”" if title else who


def _named(units: Sequence[str]) -> str:
    """The TITLE-BEARING aperture lines: capped, with the remainder counted.

    Every caller of this is a drop-ledger line whose entries are
    :func:`_drop_name` strings — a desk, a target and up to
    :data:`APERTURE_TITLE_CHARS` of the head's own title. That is the shape
    :data:`APERTURE_NAME_CAP` was sized for and the shape the bytes live in.
    The roster is NOT one of these; see :func:`_named_units`.
    """
    shown = [u for u in units if u][:APERTURE_NAME_CAP]
    if not shown:
        return ""
    more = len([u for u in units if u]) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more > 0 else "")


def _named_units(units: Sequence[str]) -> str:
    """The DECLARED ROSTER, whole — bare identifiers, never truncated.

    See :data:`APERTURE_ROSTER_BUDGET_CHARS` for the defect this answers and
    the bytes it costs. The short version: a truncated list of machine handles
    is an invitation to complete it, the voice accepted twice on live records,
    and the completion was correct and ungradeable — the names it wrote were in
    the roster the fence marks against and not in the map the judge reads.
    """
    return "; ".join(str(u) for u in units if u)


def declared_aperture(payload: Mapping[str, Any]) -> str:
    """THE DECLARED APERTURE — what this record could have carried, BY NAME.

    THE DEFECT THIS ANSWERS (D-6 §6.1, the second follow-on). The body shape
    mandates a ``## What this reading misses`` section and the voice was handed
    only COUNTS to write it from, so it guessed: the N=5 replay's arm B wrote
    *"provides no coverage of Central Asia or broader South America"* and *"the
    tier's aperture excludes any below-floor signals"* — a true-sounding
    statement about the world that the record cannot evidence, and a review of
    the instrument, respectively. Both graded unsupported, and both correctly:
    nothing in the evidence map names Central Asia.

    THE RECORD ALREADY KNOWS THE ANSWER and was not passing it on. The drop
    ledger (D-2 §F-7) publishes, per row, the DESK, the TARGET and the head's
    own TITLE for every read this surface saw and did not carry — ranked
    not-selected, cap-trimmed, below-floor — and D-2b persisted
    ``coverage_roster`` beside the coverage ledger so the declared denominator
    travels with the accounting of it. Those are real names of real uncovered
    subjects. Handing them over turns the blind-spot sentence from a guess into
    a citation, and — because this block rides into every citation's
    ``evidence_text`` under ``EVIDENCE_ARITHMETIC_RULE`` — makes it GRADEABLE for
    the first time.

    THE FENCE IS NOT WIDENED BY ONE BYTE. Every name here is already on the
    spine row, in ``payload``, which is still the only argument this module has
    (the AST guard on the module is unchanged and still holds). Nothing new is
    read; something already carried stops being thrown away.

    THE COUNTERPART IS DETERMINISTIC. ``assessment_unsupported`` marks an
    aperture sentence naming a unit that is in NEITHER the roster NOR the drop
    ledger NOR the carried blocks as ``aperture_unrostered`` — so the block
    below is not merely an offer, it is the vocabulary the section is fenced to.
    """
    drops = payload.get("drops") or {}
    counts = drops.get("counts") or {}
    coverage = list(payload.get("coverage") or [])
    roster = [str(u) for u in (payload.get("coverage_roster") or []) if u]
    # Amendment 7f — SLUGS DO THE ARITHMETIC, LABELS DO THE RENDERING, and
    # the two must not be swapped: the set intersection below is a machine
    # join on ``coverage_roster``, and computing it over rendered labels
    # would silently return zero and tell the voice its whole roster is
    # aperture. So every count stays on the slug and only the printed name
    # grows. Empty map (a pre-7f row, an unresolved desk) ⇒ label == slug,
    # and these lines are byte-for-byte the ones that shipped.
    unit_names = payload_names(payload)

    def _label(unit: str) -> str:
        return unit_label(unit_names.get(unit), unit)

    uncovered = [
        _label(str(c.get("unit") or ""))
        for c in coverage
        if str(c.get("status") or "") != "in_basis"
    ]
    # THE CARRIED SET, BY NAME. The record's own blocks, which is the half the
    # roster line cannot be read without: "every declared unit reached the
    # basis" is TRUE on every live world record and says nothing about what was
    # CARRIED, and the judge quoted it verbatim on 2026-09-07 12:16Z to HARD
    # contradict the blind-spot section the body shape mandates. A citation's
    # ``evidence_text`` names its OWN block and no other, so the eight handles
    # below are the only place the map states the carried set as a set — which
    # is what makes "the roster minus these" a subtraction the voice can show
    # its working for and the judge can check.
    carried_units = [
        str(b.get("target_id") or b.get("desk") or "").strip()
        for b in (payload.get("blocks") or [])
    ]
    carried_units = [u for u in carried_units if u]
    not_selected = [
        _drop_name(r) for r in (drops.get("not_selected") or [])
    ] or [_drop_name(r) for r in (drops.get("shown_not_carried") or [])]
    below_floor = [_drop_name(r) for r in (drops.get("below_floor") or [])]
    trimmed = [_drop_name(r) for r in (drops.get("trimmed") or [])]
    invisible = counts.get("invisible_heads")

    parts: list[str] = [
        "- THE DECLARED APERTURE — the units this record could have carried and "
        "did not, BY NAME. Your blind-spot section names these and nothing "
        "else: a place, actor or region that appears nowhere below and nowhere "
        "in a block you cite is a GUESS, and it is marked as one."
    ]
    if roster:
        parts.append(
            f"  * declared roster ({len(roster)} units): "
            f"{_named_units([_label(u) for u in roster])}."
        )
        if carried_units:
            parts.append(
                f"  * of those, this record CARRIED {len(carried_units)}: "
                f"{_named_units([_label(u) for u in carried_units])}. The other "
                f"{len(roster) - len(set(carried_units) & set(roster))} are its "
                f"aperture, each one named on the roster line above."
            )
        parts.append(
            f"  * of those, NOT in the basis this cycle: "
            f"{_named_units(uncovered)}."
            if uncovered
            # "Reached the basis" is CANDIDACY, not carriage, and on a record
            # that ranked 33 and carried 8 it is true of all 33. Left bare it
            # read as "nothing is missing" — the judge quoted this line verbatim
            # on 2026-09-07 12:16Z to hard-contradict the mandated blind-spot
            # section. The distinction is the record's own (``candidates`` vs
            # ``carried`` in the arithmetic block above), so it is stated here
            # in the record's own words rather than left to be inferred.
            else "  * of those, every declared unit reached the basis this "
            "cycle — CANDIDACY, not carriage; the aperture is the roster "
            "minus the carried list."
        )
    else:
        parts.append(
            # PHRASED THE WAY THE COVERAGE LINE ABOVE IS, and not by accident.
            # This block rides into every citation's ``evidence_text``, where
            # ``composition_integrity.desk_absence_sentences`` walks it looking
            # for desk negatives — so a sentence here of the shape "no unit
            # roster is declared … and there is no coverage denominator" would
            # MANUFACTURE an absence sentence the record never carried, which is
            # exactly the false-positive class this train's own test guards
            # (``test_the_arithmetic_in_the_map_is_not_read_as_a_desk_sentence``,
            # which caught this line in draft). "Not computed at this grain" is
            # the phrasing the coverage line already proved safe.
            "  * unit roster: not computed at this grain, so this list is the "
            "drop ledger alone; state no coverage denominator."
        )
    if not_selected:
        parts.append(
            f"  * seen and ranked BELOW THE CUT: {_named(not_selected)}."
        )
    if below_floor:
        parts.append(
            f"  * below the verification floor: {_named(below_floor)}."
        )
    if trimmed:
        parts.append(f"  * trimmed at the block cap: {_named(trimmed)}.")
    if not (roster or not_selected or below_floor or trimmed):
        parts.append(
            "  * this record dropped nothing it saw — every candidate was "
            "carried, so the aperture is what never reached it at all."
        )
    parts.append(
        f"  * {int(invisible)} desk reads were never candidates for this "
        f"surface at all. That is a COUNT and no names exist for it: say how "
        f"much is unseen, never which."
        if invisible
        else "  * how many desk reads never reached this surface at all was "
        "NOT measured this run."
    )
    return "\n".join(parts)


def build_assessment_prompt(payload: Mapping[str, Any]) -> str:
    """The Assessment's USER prompt, built from the assembly payload ALONE.

    ``payload`` is ``data.data.assembly`` (schema ``assembly.v1``). It is the
    only data argument this function has, and that is the point: §2.1's input
    restriction is enforced by a signature a test can read, not by a clause a
    model can ignore. A test pins the signature exactly (the
    ``action_pack_invocations`` discipline — audit the real binding path).

    The record is rendered by D-2's OWN renderer, so the words below are the
    bytes the canonical row publishes. If the two ever diverge, the divergence is
    a bug in one function rather than a difference of opinion between two.
    """
    tier = str(payload.get("tier") or "world")
    as_of = str(payload.get("as_of") or "")[:19]
    regime = str(payload.get("regime") or "")
    lines = [
        f"THE RECORD — {tier} tier, as of {as_of} "
        f"({payload.get('schema')}, regime {regime}).",
        "",
        "Everything between the rules below is the record's own body: desk "
        "sentences quoted byte-for-byte under their ordinals, with the "
        "attribution the record generated for each. It is what the reader sees "
        "above your band.",
        "",
        "----------------------------- RECORD -----------------------------",
        render_assembly_body(payload),
        "------------------------------------------------------------------",
        "",
        record_arithmetic(payload),
        "",
    ]
    # P3 LANE A — THE COUNTRY TIER'S CONTEXT PAGE. Appended AFTER the record and
    # after the arithmetic, so the record is still the first and largest thing
    # the voice reads and the ordinals are already established when the full
    # reads arrive under them. Empty string on every world/thematic payload, so
    # this prompt is byte-identical to D-6's there — a test pins it.
    context = desk_reads_in_full(payload)
    if context:
        # STEP E — the fence label names what is inside it. The country tier's
        # bytes are unchanged; the world tier's say COUNTRY, because a voice
        # told it is reading desk reads and handed country assessments has been
        # told a false thing about the text in front of it.
        # NOT ``CONTEXT_SECTION_HEADER``: the fence label D-6/P3-A shipped is
        # "DESK READS IN FULL" with no leading "THE", and the country prompt's
        # bytes are a pinned contract. Two literals, one test.
        label = (
            "DESK READS IN FULL" if tier == TIER_COUNTRY
            else "COUNTRY READS IN FULL"
        )
        lines.extend([
            f"-------------------- {label} --------------------",
            context,
            "------------------------------------------------------------------",
            "",
        ])
    lines.append("Now write the Assessment.")
    return "\n".join(lines)
