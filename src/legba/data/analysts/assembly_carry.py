# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE CARRY — what a tier does with an input that ALREADY carries words.

D-1 §0.3 / D-5 §4.1 / P3-B. One rule, in one module, for the two tiers that
read an assembled row: **carry the block, never re-quote the body**.

WHY THIS MODULE EXISTS (the defect it closes, measured 2026-09-18). The world
assembler's candidates are COUNTRY and THEMATIC assemblies, and until this
module it cut their spans the way it cuts a desk head's — with
``assembly_spans.extract_lead_span``, which reads the candidate's rendered
``**BLUF:**`` line. On an assembled child that line is not a claim about the
world at all; it is the child's own CONNECTIVE:

    "3 reads lead this cycle, weighted and uncrowned: [[ref:5]], [[ref:1]], [[ref:4]]."
    "No single read leads this cycle; 8 reads carried below, in order."

Live over seven days, 2 to 7 of the 8 blocks in every world read quoted such a
line, defused on the way out to ``(child ref 5), (child ref 1), (child ref 4)``
— and the Assessment voice then read those as WORLD ordinals and wrote a
structural claim about its own record that was false. The read was quoting a
sentence that asserts nothing, attributing it to a head that did not write it,
and handing a reader three numbers that point at the wrong page.

THE RULE, WHICH THE REGION ROLLUP ALREADY STATED. ``region_rollup``'s banner:
*"carries block objects; it does not re-quote — a lead block is the member
country assembly's own ``blocks[]`` object, byte-identical, including
``origin.head_id`` still pointing at the DESK HEAD"*. That is exactly right and
it was written for one tier. This module is that rule, extracted, so the world
assembler and the region rollup select the carried block with ONE function and
cannot come to different conclusions about the same child.

WHAT A CARRY PRESERVES, AND WHAT IT MOVES. The block object is copied
byte-identical — ``spans[]`` with their byte offsets, ``origin.head_id``,
``body_sha256``, ``question``, ``desk``, ``target_id`` / ``target_name``,
``produced_at``, ``verify``, ``salience``, ``signals``. Three things are added
and NOTHING is rewritten: ``ordinal`` is re-numbered into the carrying read's
own space, and ``via_head_id`` / ``via_ordinal`` / ``carry_reason`` record the
row it came through, so a drill-down walks world → country assembly → desk head
and an auditor can see WHICH RULE picked it.

THE DEPTH-1 PROPERTY IS WHAT MAKES THIS SAFE. Because the carried block still
names the desk head as its origin, quote fidelity at the carrying tier is the
SAME depth-1 byte test against the SAME body, with one implementation. There is
no second quoting act, so there is no new way to be unfaithful — which is the
whole argument, and the reason a carry is strictly better than a re-cut rather
than merely tidier.

...AND WHAT IT COSTS, NAMED. A carried block's origin body is not on the
carrying row: D-3's arms are DB-free and resolve an origin through the row's
own citation bridge, which names the CANDIDATE (the country assembly), not the
desk head underneath it. So a carry publishes its own bridge —
:func:`carried_origin_record` lifts the child's OWN citation for that desk head
(``ref_id``, ``evidence_text``, ``source``, ``target_id``, ``produced_at``) and
the payload republishes it under :data:`CARRIED_ORIGINS_KEY`. The arms then
audit a carried block at depth 1 against the desk head's body, which is the
check they were written for. Where the child published no such citation the
bridge entry is OMITTED, never fabricated: ARM 3 charges
``attribution_head_unresolved`` and says the true thing — nothing on this page
can trace that block.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from ..provenance.assembly_arms import (
    ASSEMBLY_SCHEMA,
    CARRIED_ORIGINS_KEY,
    REGIME_ASSEMBLY,
    SCOPE_TOKEN_COLLECTION_DENOMINATOR,
)
from ..provenance.composition_integrity import (
    desk_verdict_text,
    has_collection_denominator_scope,
)
from ..provenance.text_fold import normalize_for_match
from .assembly_spans import build_context_span, quoted_spans

__all__ = [
    "CARRIED_ORIGINS_KEY",
    "CARRY_BY_MASS",
    "CARRY_EARNED_LEAD",
    "CARRY_FALLBACK_ORDINAL_1",
    "CARRY_MASS_FLOOR_DEFAULT",
    "CARRY_REASONS",
    "CONTEXT_MATCH_KEY",
    "CONTEXT_ORIGIN_KEY",
    "LEADING_NEGATION_IDIOMS",
    "LEADING_NEGATION_TOKENS",
    "LEAD_CONNECTIVE_PHRASES",
    "MAX_ORIGIN_EVIDENCE_CHARS",
    "attach_country_context",
    "block_is_absence",
    "block_mass",
    "carried_lead_block",
    "carried_origin_record",
    "carried_verdict_text",
    "carry_block",
    "child_assembly",
    "connective_leak",
    "leading_negation",
]

#: ``lead.kind`` an assembled child publishes when its concentration test
#: CROWNED one block. Spelled here rather than imported from
#: ``assembly_payload`` because that module imports THIS one; the pin test
#: holds the two spellings equal, the same way ``ASSEMBLY_SCHEMA`` is held.
LEAD_EARNED_SINGLE: str = "earned_single"

#: STEP E — ``blocks[].context_origin_id``: the id of the row whose body the
#: block's ``context_body`` span was cut from. At the WORLD tier that row is a
#: ``country_assessment`` head — a DIFFERENT row from the block's origin, which
#: is still the desk head — so the block names both rather than letting a reader
#: infer one from the other. The citation republishes this key (the drill target
#: for "open the country read this sentence came out of"); the span's own
#: ``origin.head_id`` carries the same id and is what the byte-identity gate
#: actually checked, so the two can be held equal by a test.
CONTEXT_ORIGIN_KEY: str = "context_origin_id"

#: STEP E — ``blocks[].context_match``: WHICH RULE found that row. Either the
#: assessment was written from THIS record (``derived_from[0]`` is the candidate
#: the block came through) or it is the target's newest assessment inside the
#: window. The two are not the same claim and the block says which, because a
#: fallback silently presented as an exact match is a currency claim the record
#: cannot support. The vocabulary lives in ``composition_slice``, with the read
#: that assigns it; this key is only the place it is published.
CONTEXT_MATCH_KEY: str = "context_match"

#: THE LEADING-NEGATION OPENERS, DERIVED FROM THE LIVE CORPUS (Amendment 4c).
#:
#: Measured over all 926 spans in the 129 replayed country assemblies
#: (2026-09-05T11:30Z → 2026-09-06T23:31Z): **121 spans (13.1%) open with a
#: negation, and every single one of them opens with the token ``no``.** Zero
#: open with ``none`` / ``nothing`` / ``neither`` / ``nor`` / ``never`` /
#: ``not`` / ``without`` / ``absent``; zero use the ``there is no`` idiom. The
#: 30 distinct opener trigrams are all ``no <noun-phrase>``:
#:
#:     58  "no coordinated narrative"      5  "no credible indication"
#:      5  "no economic coercion"          4  "no new military"
#:      4  "no external economic"          4  "no unrest or"
#:      4  "no observable shift"           3  "no credible evidence"
#:      3  "no military-posture-relevant"  3  "no significant internal"
#:      3  "no new economic"               2  "no new coordinated"  … (+18 more)
#:
#: So the union of the corpus's openers folds to ONE token, and shipping the 30
#: trigrams literally would be brittle in the exact way that matters: next
#: cycle writes "no fresh unrest" and a literal list misses it while a reader
#: sees the same sentence. The zero-count members below are carried anyway
#: because they are the same class and a member that never fires costs nothing
#: — what would cost something is the first ``Nothing in this window…`` span
#: sliding through a list that was fitted to one week.
#:
#: WHAT THIS IS NOT. It is not a claim that a negative finding is worthless: a
#: desk that looked and found nothing has said something real, and it stays in
#: the read, in its own block, at its own ordinal, with its own cited mass. It
#: is a claim about the LEAD POSITION specifically — the block a country read
#: crowns and the one sentence a region rollup carries forward — where
#: "nothing happened" displaces the thing that did.
LEADING_NEGATION_TOKENS: tuple[str, ...] = (
    "no",       # 121 of 121 live openers
    "none",     # 0 live — carried as class members, see above
    "nothing",
    "neither",
    "nor",
    "never",
)

#: The one multi-word paraphrase of the same shape. 0 live, same reasoning.
LEADING_NEGATION_IDIOMS: tuple[str, ...] = (
    "there is no", "there are no", "there has been no", "there have been no",
)


def leading_negation(text: Any) -> bool:
    """Does this span OPEN by saying that something is not there?

    Folded through :func:`normalize_for_match` — the one shared fold — so a
    span that arrives with a full-width or non-breaking form is read the same
    way the rest of the plane reads it. Total: unusable input is ``False``,
    because a predicate that cannot see its text should decide nothing, and
    deciding nothing here means the block stays eligible.

    FIRST TOKEN ONLY, deliberately. "Niger's posture shows no material shift"
    is a negation too, and it is NOT caught: its subject is the day's posture
    and a reader takes it as a finding about Niger. "No coordinated narrative
    is evident" has no subject but the absence itself. The line between them is
    exactly where the sentence starts, which is why this predicate is about the
    opener and not about the presence of a negative word — 236 spans carry one
    in their first clause and only 121 lead with it.
    """
    folded = normalize_for_match(text)
    if not folded:
        return False
    for idiom in LEADING_NEGATION_IDIOMS:
        if folded.startswith(idiom + " "):
            return True
    head = folded.split(" ", 1)[0].strip(",;:.")
    return head in LEADING_NEGATION_TOKENS


def block_is_absence(block: Mapping[str, Any]) -> bool:
    """Is this block's lead span a statement that NOTHING HAPPENED?

    THE ONE DEFINITION, called by both halves of Amendment 4 — the country
    read's crown and the region rollup's carry — so the two tiers cannot come
    to different conclusions about the same sentence. Three signals, any one of
    which is enough:

      * ``has_collection_denominator_scope`` over the span text — ARM 2's own
        SCOPE_TRUNCATED truthmaker, reused VERBATIM and not rewritten. Reuse is
        the point: the class has false-positived three times and a second copy
        would have to be re-audited alone.
      * the ``collection_denominator`` token the span already DECLARES, which
        catches ARM 2's ``scope_truncated`` case — the qualifier trimmed out of
        the quoted span but still on the payload.
      * :func:`leading_negation`, because the first two answer a narrower
        question than this one does. Measured on the live corpus: of the 16
        crowns and 8 region carries that open with a negation under Amendment
        4, ``has_collection_denominator_scope`` catches ONE. It is documented
        to fire on a COLLECTION denominator ("in collected reporting", "among
        the monitored sources") and explicitly not on a clock ("in this
        window") — and the live absence spans are bounded by a clock or by a
        named national media set: *"No coordinated narrative is evident in
        Argentina's media over the past three days"*.

    Asymmetric on purpose. Wrongly excluding a block costs the read its
    second-heaviest thread; wrongly including one costs it a headline that says
    nothing happened.
    """
    if not isinstance(block, Mapping):
        return False
    # P3-A: QUOTED spans only (identity on a block with no context span) —
    # asking "is the LEAD an absence" of a whole desk read answers the wrong text.
    for span in quoted_spans(block):
        if not isinstance(span, Mapping):
            continue
        tokens = span.get("scope_tokens")
        if isinstance(tokens, Sequence) and not isinstance(tokens, (str, bytes)):
            if any(str(t) == SCOPE_TOKEN_COLLECTION_DENOMINATOR for t in tokens):
                return True
        text = str(span.get("text") or "")
        if leading_negation(text):
            return True
        try:
            if has_collection_denominator_scope(text):
                return True
        except Exception:  # a predicate must never take a read down
            continue
    return False


#: ``carry_reason`` — WHICH RULE picked the block that was carried, on the
#: region rollup's ``members[]`` and on a world assembly's carried ``blocks[]``
#: alike (Amendment 4a, 2026-09-07; extended to the world tier 2026-09-18).
#: ``lead_source`` says WHETHER anything was carried; this says why THAT one,
#: which is the question the 09-06 region rows could not answer:
#:
#:   * ``earned_lead``        — the member's own concentration test crowned a
#:                              single block and the rollup carries it,
#:                              unchanged. 17 of 129 carries in the measured
#:                              window.
#:   * ``by_mass``            — no earned crown, so the rollup carries the
#:                              member's HEAVIEST block by ``cited_mass``
#:                              (ties → lowest ordinal, the member's own total
#:                              order), after the §3.2 exclusion below.
#:   * ``fallback_ordinal_1`` — nothing survives to rank: every block scored
#:                              zero cited mass, or every block with mass is a
#:                              collection-scoped absence claim. Then the carry
#:                              is the top of the member's own order — what this
#:                              tier always did, now SAID rather than assumed.
#:
#: WHY MASS AND NOT POSITION. Position inside a member assembly is
#: ``severity DESC, cited_mass DESC, …`` — severity FIRST — and severity is
#: declared by each desk against its own prompt, so it does not compare across
#: desks: measured over 129 country reads, ``lead.block_ordinals[0]`` is 1 on
#: 129 of 129 carries under ALL THREE lead kinds (the co-lead band is anchored
#: on ordinal 1 by construction), the carried block is not the read's top-mass
#: block on 78 of 129, and 22 carry a ``cited_mass`` of 0.00 while the same read
#: holds evidence elsewhere. On 2026-09-06 23:30Z that put Ukraine's *"a new
#: IAEA-brokered cease-fire … eases the immediate blackout risk"* at mass 0.00
#: into the regional read while the same desk's escalation block carried 1.58.
#: A region rollup is a statement about where its members' evidence sits; the
#: block it carries has to be the block the evidence is under.
#:
#: WHY THE EXCLUSION. ``argmax(cited_mass)`` alone promotes a well-cited
#: NEGATIVE: Indonesia's narrative_coordination block reads *"no coordinated
#: narrative is evident"* across 92 signals and scores 5.79 — the heaviest block
#: in the read, and a statement that nothing happened. The test is
#: :func:`block_is_absence`: ARM 2's SCOPE_TRUNCATED truthmaker
#: ``has_collection_denominator_scope`` reused VERBATIM (reuse is the point —
#: the class has false-positived three times and a second copy would have to be
#: re-audited alone), the ``scope_tokens`` the span declares, and the
#: corpus-derived leading-negation opener. The third was added on measurement:
#: ARM 2's predicate fires on a COLLECTION denominator and explicitly not on a
#: clock, and the live absence spans are clock-bounded, so it caught ONE of the
#: eight carries that opened with a negation. See that function for the counts.
CARRY_EARNED_LEAD: str = "earned_lead"
CARRY_BY_MASS: str = "by_mass"
CARRY_FALLBACK_ORDINAL_1: str = "fallback_ordinal_1"
CARRY_REASONS: tuple[str, ...] = (
    CARRY_EARNED_LEAD,
    CARRY_BY_MASS,
    CARRY_FALLBACK_ORDINAL_1,
)

#: The by-mass carry's noise floor when the caller states none. ``0.0`` — carry
#: anything with mass at all — which is what both tiers did before the knob
#: existed and is byte-identical to it. ``region_rollup`` layers its env/option
#: resolution on top (:func:`region_rollup.rollup_mass_floor`); the world
#: assembler takes the default, because a world carry that fell back to the
#: child's ordinal 1 would land on exactly the severity-first position D-5
#: measured as the wrong one.
CARRY_MASS_FLOOR_DEFAULT: float = 0.0


def block_mass(block: Mapping[str, Any]) -> float:
    """A carried block's own ``salience.cited_mass``, read off the block.

    Read, never recomputed: a carry re-derives nothing about the evidence, only
    about which block travels. An unparsable or absent key is 0.0 — the same
    fail-safe the assembler's ``_mass`` uses, and it sorts such a block last
    rather than mistaking it for a heavy one.
    """
    sal = block.get("salience")
    if not isinstance(sal, Mapping):
        return 0.0
    try:
        return float(sal.get("cited_mass") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def child_assembly(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The candidate row's own ``assembly.v1`` payload, when it HAS one.

    ``None`` — meaning "this candidate is a desk head, cut a span from it the
    way this tier always has" — for every row that is not an assembled read:

      * a first-order desk head (no ``assembly`` key at all);
      * a LEGACY-regime composition, which carries ``{schema, regime:
        "legacy"}`` and no blocks. That is the flag-off arm and it MUST keep the
        old path byte for byte, which this return value is what guarantees;
      * a ROLLUP (``regime: "rollup"``), which carries no ``blocks`` either —
        and the world does not read region rows under the regime anyway
        (``composition_slice.world_reads_countries``).

    The regime is checked EXPLICITLY rather than inferred from the presence of
    blocks: a row is carried from only when it says it was assembled, so a
    future producer that ships blocks under a regime this rule was not written
    for is read as a desk head instead of being silently carried.
    """
    data = row.get("data")
    if not isinstance(data, Mapping):
        return None
    inner = data.get("data")
    inner = inner if isinstance(inner, Mapping) else data
    asm = inner.get("assembly")
    if not isinstance(asm, Mapping):
        return None
    if str(asm.get("schema") or "") != ASSEMBLY_SCHEMA:
        return None
    if str(asm.get("regime") or "") != REGIME_ASSEMBLY:
        return None
    blocks = asm.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        return None
    return asm if blocks else None


def carried_lead_block(
    asm: Mapping[str, Any], *, mass_floor: float = CARRY_MASS_FLOOR_DEFAULT,
) -> tuple[Mapping[str, Any] | None, int | None, str | None]:
    """The child assembly's carried block, its ordinal there, and WHY that one.

    Amendment 4a (2026-09-07), pre-T0. See :data:`CARRY_REASONS` for the rule
    and the measurement behind it. In one line: an EARNED crown is honoured as
    the child issued it; otherwise the carry is the child's heaviest block by
    ``cited_mass`` once absence claims are set aside (:func:`block_is_absence` —
    collection-scoped OR opening with a negation); and when nothing survives to
    rank, ordinal 1 — what a carrying tier always did, now stated on the row
    instead of assumed.

    THE ABSENCE EXCLUSION IS UNCONDITIONAL HERE, and that is not the same
    decision ``_lead_block`` makes about a CROWN. The crown's exclusion rides
    ``LEAD_TEST_V2`` because moving ``lead.kind`` is a policy change on a scored
    number; a CARRY is this tier choosing which of the child's already-published
    blocks to quote, changes no number the child published, and has shipped
    excluding absences at the region tier since Amendment 4a. Two tiers reaching
    different verdicts about the same sentence is the failure mode this one
    function exists to prevent.

    Re-derives NOTHING about the evidence. Every number it sorts on was computed
    by the child assembly and is persisted on the child's own block; this only
    chooses which of them travels.
    """
    blocks = [b for b in (asm.get("blocks") or []) if isinstance(b, Mapping)]
    if not blocks:
        return None, None, None

    def _at(ordinal: int) -> Mapping[str, Any] | None:
        for b in blocks:
            if int(b.get("ordinal") or 0) == ordinal:
                return b
        return None

    lead = asm.get("lead") if isinstance(asm.get("lead"), Mapping) else {}
    ordinals = [int(o) for o in (lead.get("block_ordinals") or []) if str(o).isdigit()]
    if str(lead.get("kind") or "") == LEAD_EARNED_SINGLE and ordinals:
        crowned = _at(ordinals[0])
        if crowned is not None:
            return crowned, ordinals[0], CARRY_EARNED_LEAD

    ranked = sorted(
        (
            b for b in blocks
            if block_mass(b) > mass_floor and not block_is_absence(b)
        ),
        key=lambda b: (-block_mass(b), int(b.get("ordinal") or 0)),
    )
    if ranked:
        best = ranked[0]
        return best, int(best.get("ordinal") or 1), CARRY_BY_MASS

    first = _at(1) or blocks[0]
    return first, int(first.get("ordinal") or 1), CARRY_FALLBACK_ORDINAL_1


def carry_block(
    block: Mapping[str, Any],
    *,
    ordinal: int,
    via_head_id: str,
    via_ordinal: int | None,
    carry_reason: str | None,
) -> dict[str, Any]:
    """The child's block, BYTE-IDENTICAL, re-numbered into this read's space.

    A deep copy, so the carrying payload can never alias — and therefore can
    never mutate — the candidate row's own nested structures. Exactly one
    shipped key changes (``ordinal``, which is this read's position and is what
    pairs the block with its ``[[ref:N]]`` marker and its citation); three are
    ADDED:

      * ``via_head_id`` — the candidate row the block came THROUGH. The drill
        walks world → country assembly → desk head, and ``derived_from`` on the
        carrying row still names the candidate, unchanged;
      * ``via_ordinal`` — where the block sat in the child's own order, so a
        reader can open the child read and land on the same block;
      * ``carry_reason`` — :data:`CARRY_REASONS`, which rule picked it.

    Everything else — ``finding_id``, ``spans[]`` with their byte offsets and
    ``origin.head_id``, ``desk``, ``target_id``, ``produced_at``, ``verify``,
    ``salience``, ``signals`` — is the DESK HEAD's, untouched. The attribution
    line this renders therefore describes the head that wrote the sentence,
    which is the only thing it could honestly describe.
    """
    out = copy.deepcopy(dict(block))
    # P3-A × P3-B: the world CARRIES the sentence, not the desk body. A country
    # block's ``context_body`` span is the country voice's context and stays at
    # the country tier — the rule region_rollup's leads[] carry already applies.
    out["spans"] = [copy.deepcopy(dict(sp)) for sp in quoted_spans(block)]
    out["ordinal"] = int(ordinal)
    out["via_head_id"] = str(via_head_id)
    out["via_ordinal"] = int(via_ordinal) if via_ordinal is not None else None
    out["carry_reason"] = str(carry_reason) if carry_reason else None
    return out


def attach_country_context(
    block: dict, context: Mapping[str, Any] | None
) -> dict:
    """STEP E — hang the block's COUNTRY ASSESSMENT on it as a context span.

    Mutates and returns ``block``. ``None`` or an empty map is a NO-OP, which is
    what every thematic candidate and every pre-STEP-E caller gets: the block is
    byte-for-byte the one :func:`carry_block` produced.

    THE DEFECT, measured 2026-09-20. The COUNTRY voice averages judge 0.86 over
    126 LLM-judged reads; the WORLD voice swings 0.50-0.95, and its 12:15Z read
    spent its argument on the record's own arithmetic ("its verification score
    (0.87) and high severity", "smaller cited mass (2.99)", "the arithmetic
    deems") rather than on the world. The v2 and v3 prompt clauses already forbid
    exactly those sentences, so a third clause was the obvious move and the wrong
    one: the world voice's whole input was EIGHT DESK SENTENCES. A voice with
    nothing to argue FROM argues about its page.

    THE FIX IS P3-A, ONE TIER UP, WITH ONE WORD CHANGED. P3-A gave the country
    voice each desk's read in full because a lead sentence per desk cannot carry
    a cross-DIMENSION argument; a lead sentence per country cannot carry a
    cross-COUNTRY one. So the world reads the COUNTRY VOICES — 32 reads a day at
    0.86, already fenced, already graded.

    TWO ORIGINS, STATED AS TWO. The carried block's own origin stays the DESK
    HEAD (:func:`carry_block` copied it byte-identical, so quote fidelity here is
    the same depth-1 test on the same body). The context span's origin is a
    DIFFERENT row — the ``country_assessment`` head — so the block publishes it
    under :data:`CONTEXT_ORIGIN_KEY` beside, never merged into, its own origin,
    and :data:`CONTEXT_MATCH_KEY` says which rule found it. The citation
    republishes both, which is what lets a drill open the country read.

    Raises :class:`assembly_spans.SpanConstructionError` on a body that cannot
    be carried byte-identically — the caller turns it into a construction
    failure, because a read with a silent hole where its context should be is
    the thing the no-stubs rule exists to stop.
    """
    if not context:
        return block
    head_id = str(context.get("head_id") or "")
    # Through ``build_context_span``, hence through ``build_span``:
    # byte-identity is PROVED here by the function that proves it for every
    # quoted span, which is the whole reason this is a span and not a string.
    span = build_context_span(context, head_id=head_id)
    block["spans"] = list(block.get("spans") or []) + [span]
    block[CONTEXT_ORIGIN_KEY] = head_id
    block[CONTEXT_MATCH_KEY] = str(context.get("match") or "")
    return block


#: The width of the origin body a carried block's bridge entry republishes.
#: MIRRORS ``composition_citations.MAX_EVIDENCE_TEXT_CHARS`` and is pinned equal
#: by a test rather than imported: that module imports ``region_rollup``, which
#: imports the assembler, which imports this one. Same number, one direction, no
#: cycle — and a drift in either place turns the pin red.
MAX_ORIGIN_EVIDENCE_CHARS: int = 4000

# ``CARRIED_ORIGINS_KEY`` — ``data.data.assembly.carried_origins``, the key the
# bridge below is published under. IMPORTED from ``provenance.assembly_arms``,
# which is the module that has to RECOGNISE it: a guard may not import an
# analyst to know what it is grading, so the marker lives on that side and the
# producer takes it from there (``kinds.ROLLUP_PAYLOAD_SCHEMA``'s direction).


def _row_citations(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    env = row.get("data")
    if not isinstance(env, Mapping):
        return []
    inner = env.get("data")
    inner = inner if isinstance(inner, Mapping) else env
    cits = inner.get("citations")
    return [c for c in cits if isinstance(c, Mapping)] if isinstance(cits, list) else []


def carried_origin_record(
    row: Mapping[str, Any], block: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The desk head's origin record, LIFTED from the child row's own citation.

    The child assembly already captured exactly what an auditor needs about the
    head it quoted — ``evidence_text`` (the head's body, point-in-time),
    ``source`` (its desk), ``target_id`` and ``produced_at`` — under a
    ``ref_id`` that IS the carried block's ``finding_id``. Republishing that
    record beside the carried block is what lets D-3 grade the carry at depth 1
    against the body the span was actually cut from.

    ``None`` when the child published no citation for that head. NEVER a record
    built from the block itself: a bridge assembled out of the thing it is
    supposed to check would make ARM 3 compare a block against a copy of itself
    and report a pass it never established. An absent bridge entry costs the
    block one honest ``attribution_head_unresolved`` instead.
    """
    want = str(block.get("finding_id") or "")
    if not want:
        return None
    for c in _row_citations(row):
        if str(c.get("ref_id") or "") != want:
            continue
        record: dict[str, Any] = {
            "ref_id": want,
            "ref_kind": "finding",
            "evidence_text": str(c.get("evidence_text") or "")[
                :MAX_ORIGIN_EVIDENCE_CHARS
            ],
        }
        for key in ("source", "target_id", "produced_at"):
            value = c.get(key)
            if value is not None:
                record[key] = str(value)
        return record
    return None


#: THE LEAD CONNECTIVES, as the RENDER emits them. Both are members of
#: ``assembly_render.CONNECTIVES`` and a test pins that — they are spelled here
#: because the renderer imports the assembler and the assembler imports this
#: module, so the guard cannot read the vocabulary it guards against.
#:
#: These two strings are the whole defect this module closes. They are the
#: sentences a composed read prints when its lead was NOT earned:
#:
#:     "**BLUF:** 3 reads lead this cycle, weighted and uncrowned: …"
#:     "**BLUF:** No single read leads this cycle; 8 reads carried below, …"
#:
#: A connective asserts nothing about the world — that is the property the
#: closed vocabulary is chosen for — so a span carrying one is a span carrying
#: no claim, attributed to a head that did not write it.
LEAD_CONNECTIVE_PHRASES: tuple[str, ...] = (
    "lead this cycle, weighted and uncrowned",
    "No single read leads this cycle",
)


def connective_leak(text: Any) -> str | None:
    """The lead connective this span text carries, or ``None``. THE GUARD.

    CONTAINMENT, not a prefix match, and the reason is the co-lead spelling:
    that line opens with a COUNT (*"3 reads lead this cycle, …"*), so a
    prefix test sees a digit and passes the exact sentence the guard exists to
    stop. Folded through the one shared fold, so a full-width or non-breaking
    variant is read the way the rest of the plane reads it.

    Total: unusable input is ``None``. A predicate that cannot see its text
    decides nothing, and deciding nothing here means the span is not accused.

    STEP E — WHAT THE CALLER RUNS IT OVER, and why that is the guard's own
    sentence rather than a loosening of it. ``build_assembly`` runs this over
    :func:`quoted_spans`, not over ``block["spans"]`` raw. SPAN here means the
    thing the record QUOTES: the harm named above is a composed read's lead
    line published as a desk's claim, carrying child ordinals a reader resolves
    against the wrong page. A ``context_body`` span is neither quoted nor
    rendered into the body, and ``assessment_prompts.desk_reads_in_full`` puts
    it through ``assembly_render.quoted_text``, which DEFUSES exactly those
    child markers — so the harm cannot arise from one. It is the same filter,
    for the same reason, that ``assembly_arms._spans``, ``render_assembly_body``
    and ``region_rollup`` already apply: a check whose denominator includes
    context measures something other than what it claims to. Without it, a
    country voice that happened to quote its own record's connective would take
    the whole WORLD run down with a construction error about a sentence nobody
    publishes.
    """
    folded = normalize_for_match(text)
    if not folded:
        return None
    for phrase in LEAD_CONNECTIVE_PHRASES:
        if normalize_for_match(phrase) in folded:
            return phrase
    return None


def carried_verdict_text(row: Mapping[str, Any]) -> str:
    """The sentence a DROPPED candidate would be quoted for — carried, not cut.

    The tension detector's other side. It reads a dropped row's own verdict via
    ``desk_verdict_text`` (the head's ``**BLUF:**``), and on an assembled child
    that is the same CONNECTIVE the carry exists to keep out of the record: a
    declared cross-tier tension publishes it verbatim as ``b_ref.span``, where
    the voice reads it as something the uncarried read said.

    So a candidate that carries an assembly contributes the sentence it WOULD
    have carried — the block :func:`carried_lead_block` picks — and everything
    else (a desk head, a legacy row, a rollup) takes ``desk_verdict_text``
    unchanged. At the country tier, whose dropped rows are desk heads, this
    function IS ``desk_verdict_text`` on every call.
    """
    child = child_assembly(row)
    if child is not None:
        block, _, _ = carried_lead_block(child)
        if isinstance(block, Mapping):
            spans = quoted_spans(block)
            if spans and isinstance(spans[0], Mapping):
                text = str(spans[0].get("text") or "")
                if text:
                    return text
    return desk_verdict_text(str(row.get("body") or ""))
