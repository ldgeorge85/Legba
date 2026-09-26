"""THE ASSEMBLY — `data.data.assembly`, the typed document (D-1 §1).

The composition demotion, in one sentence: the tier stops WRITING prose about
its inputs and starts CARRYING their words. This module builds the typed payload
that replaces the prose — blocks of quoted spans, each carrying its origin head
and byte offsets; a deterministic order; a lead position that must be EARNED by
a concentration test rather than assumed; declared tensions; and a drop ledger
that publishes what the read was shown and did not carry.

WHAT THIS IS NOT. It is not a new ``OutputKind`` and not a new ``analyst_id``
(D-1 §1.1). It is an ADDITIVE payload on the existing ``kind='finding'`` row of
the existing composition analysts, so ``situation_signature``, supersession, the
frame gauge, the lineage sweep, the feed, the alert scan and the Morning Read all
keep working on the row they already key on. Everything the row carries today —
``citations``, ``evidence_tiers``, ``evidence_window``, ``correlation_guard``,
``continuity``, ``salience``, ``contributing_analysts`` — stays, unchanged in
shape.

THE FOUR INVARIANTS THIS MODULE SHIPS AS TESTS (D-1 §1.3, §1.4b):

  * ``|blocks| == |citations| == |distinct [[ref:N]] markers|``
  * ``|derived_from| - |blocks| == drops.shown_not_carried + drops.trimmed``
  * selection is a STRICT PREFIX of the order, so no carried block ranks below
    a dropped one;
  * every span is byte-identical to its origin head's body — enforced at
    construction (``assembly_spans``), audited independently by D-3's arm 1.

THE HONEST WARNING D-1 §F-7 ATTACHES TO THE THIRD ONE. A strict prefix makes
the design's own drop-disclosure consistency check unfalsifiable: of course no
carried block ranks below a dropped one, the selection is the prefix of the
order. That check is free and is published anyway; the TEETH are in the ranked
``not_selected`` list, which publishes what the order put below the line. The
ledger's value is transparency, not enforcement, and R5's question should be
phrased that way.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Mapping, Sequence

from ..provenance.composition_integrity import (
    direction_conflict,
    has_collection_denominator_scope,
)
# THE CARRY, imported ONE WAY and re-exported. `block_is_absence` and its
# leading-negation corpus moved to `assembly_carry` beside the carry rule that
# is this module's second reader of them, so the country read's CROWN and the
# world read's CARRY go on sharing one definition — see that module's banner.
from .assembly_carry import (  # noqa: F401 — re-exported surface
    CARRIED_ORIGINS_KEY,
    CONTEXT_MATCH_KEY,
    CONTEXT_ORIGIN_KEY,
    LEADING_NEGATION_IDIOMS,
    LEADING_NEGATION_TOKENS,
    attach_country_context,
    block_is_absence,
    carried_lead_block,
    carried_origin_record,
    carried_verdict_text,
    carry_block,
    child_assembly,
    connective_leak,
    leading_negation,
)
from .assembly_spans import (
    SpanConstructionError, build_context_span, extract_lead_span, quoted_spans,
)
from .composition_window import head_age_hours
from .signal_salience import CITED_SALIENCE_VERSION, salience_over_ids
from .source_independence import corroboration_block, head_citations
from .unit_names import (
    PAYLOAD_NAMES_KEY,
    name_of,
    published_names,
    tension_statement,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ASSEMBLY_ENV",
    "ASSEMBLY_SCHEMA",
    "BLOCK_CAP",
    "CARRIED_ORIGINS_KEY",
    "CITED_MAGNITUDES_ROW_KEY",
    "CITED_SALIENCE_ROW_KEY",
    "CO_LEAD_BAND",
    "CO_LEAD_MAX",
    "CONNECTIVE_VOCABULARY_VERSION",
    "CONTEXT_MATCH_KEY",
    "CONTEXT_ORIGIN_KEY",
    "DESK_QUESTIONS_OPTION",
    "LEAD_CANDIDATE_FLOOR",
    "LEAD_CO_LEADS",
    "LEAD_EARNED_SINGLE",
    "LEAD_NONE",
    "LEAD_TEST_V2_ENV",
    "LEAD_TEST_V2_OPTION",
    "LEADING_NEGATION_IDIOMS",
    "LEADING_NEGATION_TOKENS",
    "MIN_LEAD_CANDIDATES",
    "RATIO_BAR",
    "REGIME_ASSEMBLY",
    "REGIME_LEGACY",
    "SHARE_BAR",
    "TIER_COUNTRY",
    "TIER_THEMATIC",
    "TIER_WORLD",
    "WHY_CLASSES",
    "AssemblyConstructionError",
    "assembly_confidence",
    "assembly_enabled",
    "assembly_magnitudes",
    "assembly_severity",
    "assembly_shared_signals",
    "assembly_tags",
    "attach_cited_salience",
    "attach_country_context",
    "block_is_absence",
    "build_assembly",
    "carried_lead_block",
    "carried_origin_record",
    "carried_verdict_text",
    "carry_block",
    "child_assembly",
    "connective_leak",
    "desk_display_name",
    "earned_lead",
    "lead_test_v2_enabled",
    "leading_negation",
    "legacy_regime_stamp",
    "min_lead_candidates",
    "now_iso",
    "order_key",
    "row_signal_ids",
]


class AssemblyConstructionError(RuntimeError):
    """The assembly could not be built. Raised, never degraded.

    D-1 §3.6: the assembler owns a publish decision and verify does not, so this
    is where a read that cannot be constructed honestly stops. A run that would
    publish zero blocks raises rather than publishing an empty read.
    """


#: The cutover flag (D-1 §5.2). ``0`` (absent) → the legacy prose path, byte for
#: byte. ``1`` → every composition analyst assembles. A comma-separated list of
#: analyst ids → only those assemble, which is how the rollout walks the tiers
#: (``world_assessor`` first, then country, then thematic) without a code change.
ASSEMBLY_ENV: str = "LEGBA_COMPOSITION_ASSEMBLY"

ASSEMBLY_SCHEMA: str = "assembly.v1"
CONNECTIVE_VOCABULARY_VERSION: str = "connective.v1"

#: ``data.data.assembly.regime`` — the A/B arm label, stamped on EVERY
#: composition row from the moment D-2 merges, flag on or off. D-1 §F-4: the
#: judge stamp splits on CODE, and this cutover is a runtime FLAG, so without
#: this field the A/B boundary is invisible inside one stamp — which is exactly
#: the 08-12 failure shape (a stamp that pooled a judge outage with a working
#: period). Every pooling reader groups by it.
REGIME_ASSEMBLY: str = "assembly"
REGIME_LEGACY: str = "legacy"

#: D-5 (§4.1) — the CASCADE's third arm. The region tier under the assembly
#: regime is neither ``legacy`` (prose a model wrote) nor ``assembly`` (spans
#: this tier cut from its inputs): it is a deterministic ROLLUP that carries its
#: members' blocks forward without re-quoting. Three producers, three labels, so
#: no pooling reader can put a rollup's arithmetic beside an assembly's quote
#: fidelity and report them as one population. The payload lives at
#: ``data.data.rollup`` (``region_rollup.v1``); see ``region_rollup.py``.
REGIME_ROLLUP: str = "rollup"

TIER_COUNTRY: str = "country"
TIER_WORLD: str = "world"
TIER_THEMATIC: str = "thematic"

#: D-1 §F-10. Eight reads as a page rather than a wall; live country reads
#: already carry 7.1 citations on average, so this changes almost nothing at the
#: country tier and is a real cut at the world tier (~32 candidates).
BLOCK_CAP: int = 8

#: The earned-lead bars (D-1 §1.5.3, operator ruling 8). Measured over 8
#: consecutive live days: ``top_share`` 0.086-0.311, ``ratio_12`` 1.03-2.17 —
#: the test fires on 1 day in 7. Today the world title crowns a single driver on
#: 93.1% of reads while 67% of consecutive runs disagree about WHO. Two numbers
#: in one place, so an operator who thinks one-in-seven is too silent moves them
#: here and nowhere else.
RATIO_BAR: float = 1.50
SHARE_BAR: float = 0.15
MIN_LEAD_CANDIDATES: int = 8

#: LEAD TEST v2 (Amendment 4b) — BEHIND :data:`LEAD_TEST_V2_ENV`, DEFAULT OFF.
#:
#: ``MIN_LEAD_CANDIDATES`` is a bar against a THIN read crowning a driver by
#: accident. It was written as an absolute 8 because §1.5.3 was measured on the
#: WORLD pool (32 candidates/day), where a count bar of 8 never binds. At the
#: COUNTRY tier the roster is exactly 8 dimensions, so one dimension with no
#: head inside the horizon — the ORDINARY case: 96 of 129 reads in the 09-05 →
#: 09-06 window, 27 of 32 desks at 2026-09-06 23:30Z — leaves 7 candidates and
#: the lead CANNOT BE EARNED at any concentration. Canada that cycle: ratio
#: 2.50, top-share 0.714, both substantive bars cleared by a wide margin,
#: ``earned=false`` on the count alone. 44 of 129 reads pass both substantive
#: bars and are refused by this count alone. That is not a thin read being
#: stopped; it is a COMPLETE read of a roster that could only field seven.
#:
#: v2 makes the bar count WHAT CAN EXIST: ``MIN_LEAD_CANDIDATES`` minus the
#: units the coverage ledger itself declares headless, never below this floor.
#: The original intent survives exactly — a desk that fields three of eight
#: still cannot crown anything — while a desk that fields everything it has is
#: allowed to.
#:
#: Six, not five and not seven, measured over the 3-day replay: the floor only
#: binds when ``no_head >= 3`` and no live row reached that; at 7 the one desk
#: with two headless dimensions (Australia, 6 of 8) would still be barred while
#: fielding its whole roster; at 5 a desk that had lost three of eight
#: dimensions could still crown one. Six is the smallest number that keeps
#: "most of a roster" and the largest that does not re-create the defect one
#: dimension further down.
LEAD_CANDIDATE_FLOOR: int = 6

#: THE FLAG EVERY CROWN CHANGE RIDES. Default OFF, and that default is not
#: caution for its own sake: ``lead.kind`` and ordinal 1 are what R4's G6
#: scores, T0 is 2026-09-12 13:00Z, and moving a scored number inside the read
#: regime is a POLICY change however plainly it is also a defect.
#:
#: FOUR rules, ONE switch, because each alone would leave the crown in a state
#: nobody measured:
#:
#:   1. the count bar becomes roster-relative (:func:`min_lead_candidates`), so
#:      a complete read of a seven-dimension roster can earn a lead at all;
#:   2. the earned crown is the ARGMAX-mass block, not unconditionally ordinal 1
#:      — on 2026-09-06, 4 of 5 earned leads crowned a block that did not hold
#:      the top mass (IL crowned 0.20 over a 1.58; RU 0.35 over a 0.60);
#:   3. the co-lead band is anchored on ``max(ms)`` and emitted in MASS order,
#:      not on ``ms[0]`` in ordinal order — DE's band ``[1,2,5,7]`` was anchored
#:      on 0.15 while block 5 carried 0.66;
#:   4. and both 2 and 3 skip ABSENCE blocks (:func:`block_is_absence`), because
#:      an argmax over cited mass promotes *"No coordinated narrative is
#:      evident"* — scored across 92 signals — over everything the day did.
#:
#: NOT a fourth rule, and the omission is measured rather than timid:
#: :func:`order_key` stays SEVERITY-FIRST under both regimes. Swapping it for
#: mass-first is the obvious next move and the policy lane found it makes
#: ``energy_security``'s dominance WORSE. The real repair there is a severity
#: scale that compares across desks (percentile severity), which is owed and
#: post-R4.
#:
#: Flag OFF is BYTE-IDENTICAL to the shipped payload, keys included: v1 stamps
#: no ``min_candidates_effective`` and no ``no_head``, so a flag-off row cannot
#: be told apart from a pre-amendment one. ``0``/unset → off, ``1`` → on,
#: anything else → a comma-separated allow-list of analyst ids, the same
#: three-state spelling :func:`assembly_enabled` uses, so the rollout can walk
#: one tier at a time without a deploy.
LEAD_TEST_V2_ENV: str = "LEGBA_LEAD_TEST_V2"

#: The descriptor-side spelling of the same switch. House ``_coerce`` idiom
#: (``_coverage_floor_scan.CoverageFloorConfig.from_options``): the ENV supplies
#: the base and a declared ``method.options`` entry WINS over it, so an operator
#: can flip one composition descriptor without touching the fleet's env.
LEAD_TEST_V2_OPTION: str = "lead_test_v2"

#: When the lead is not earned, candidates within this fraction of the top are
#: CO-LEADS — ordered, weighted, none crowned. Capped at four; past that the day
#: is flat and the honest answer is ``none``.
CO_LEAD_BAND: float = 0.60
CO_LEAD_MAX: int = 4

LEAD_EARNED_SINGLE: str = "earned_single"
LEAD_CO_LEADS: str = "co_leads"
LEAD_NONE: str = "none"

#: Where the repaired per-head key is denormalised onto a slice row by the
#: reader (``cited_mass.v1``). Private-by-underscore like every other slice-row
#: annotation (``_evidence_tier``, ``_admissibility_horizon_h``): the orient /
#: render / cite paths read only their own known keys.
CITED_SALIENCE_ROW_KEY: str = "_cited_salience"

#: The per-signal magnitudes behind that key, kept beside it so the
#: evidence-map strip renders from the SAME join the order sorted on.
CITED_MAGNITUDES_ROW_KEY: str = "_cited_magnitudes"

#: F-3, ratified. The block header is ruled to be the desk's BOUNDED QUESTION,
#: and **no descriptor field holds one today** — the mock's questions were
#: written by hand. A parallel descriptor train is authoring them one line per
#: descriptor. Until it lands, the assembler reads this option (a mapping
#: ``analyst_id -> question`` the runtime resolves from the desk descriptors)
#: and falls back to the desk DISPLAY NAME, stamping ``question_source`` so the
#: fallback rate is countable rather than invisible.
DESK_QUESTIONS_OPTION: str = "desk_questions"
QUESTION_SOURCE_DESCRIPTOR: str = "descriptor"
QUESTION_SOURCE_FALLBACK: str = "fallback_desk_name"

#: The closed why-class enum (D-1 §1.7). Every drop row carries one of these and
#: nothing else; a class outside the enum is D-3's ``drop_why_unknown``.
WHY_SHOWN_NOT_SELECTED: str = "shown_not_selected"
WHY_NOT_SELECTED: str = "not_selected"
WHY_CAP_TRIMMED: str = "cap_trimmed"
WHY_BELOW_FLOOR: str = "below_floor"
WHY_NO_HEAD: str = "no_head_in_horizon"
WHY_SUPERSEDED: str = "superseded"
WHY_CORRELATED_DUPLICATE: str = "correlated_duplicate"
WHY_NOT_A_CANDIDATE: str = "not_a_candidate"
WHY_CONSTRUCTION_FAILED: str = "construction_failed"
WHY_CLASSES: tuple[str, ...] = (
    WHY_SHOWN_NOT_SELECTED,
    WHY_NOT_SELECTED,
    WHY_CAP_TRIMMED,
    WHY_BELOW_FLOOR,
    WHY_NO_HEAD,
    WHY_SUPERSEDED,
    WHY_CORRELATED_DUPLICATE,
    WHY_NOT_A_CANDIDATE,
    WHY_CONSTRUCTION_FAILED,
)

#: Severity ranks, high to low. The assembly is the FIRST composition row ever to
#: carry a severity: it is NULL on 100% of composition rows today (0 of 1,108 in
#: 14 days) while desk heads carry it on ~99%. Setting it deterministically to
#: the max across carried blocks is what makes the dead severity dot in the
#: history list render for the first time, at zero extra cost.
_SEVERITY_ORDER: dict[str, int] = {
    "critical": 5, "high": 4, "elevated": 3,
    "moderate": 2, "medium": 2, "low": 1, "info": 0,
}

#: `tension_checked.scope_note` — and it is not decoration. `direction_conflict`
#: reads the head's BLUF only, and DECLINES any pair where the desk's own
#: verdict already carries the pole the other side asserts. Both limits are fine
#: (§1.4d makes the lead span the BLUF, so the pairs the detector can see are
#: exactly the pairs the assembly ranks on) — but a checked negative that does
#: not say WHAT IT CHECKED is the M-11 defect in a new costume.
TENSION_SCOPE_NOTE: str = "BLUF-grain; ambivalent pairs declined"
TENSION_DETECTOR: str = "direction_conflict"
TENSION_DETECTOR_VERSION: str = "composition_integrity.direction_conflict/2026-08"
TENSION_SCOPE_SHOWN: str = "shown_only"
TENSION_SCOPE_SHOWN_AND_DROPPED: str = "shown_and_dropped"


def assembly_enabled(analyst_id: Any) -> bool:
    """Is the assembly path on for ``analyst_id``?

    ``LEGBA_COMPOSITION_ASSEMBLY`` unset or ``0`` → False for everything, and
    the legacy prose path runs byte-for-byte (``test_assembly_flag_off_byte_
    identical`` is the proof, not the hope). ``1`` → on for every composition.
    Anything else is read as a comma-separated ALLOW-LIST of analyst ids, which
    is how the rollout walks ``world_assessor`` → country → thematic without a
    deploy.
    """
    return _flag_state(os.environ.get(ASSEMBLY_ENV), analyst_id)


def lead_test_v2_enabled(
    options: Mapping[str, Any] | None = None,
    analyst_id: Any = None,
) -> bool:
    """Is LEAD TEST v2 on? (:data:`LEAD_TEST_V2_ENV`, default OFF.)

    The house ``_coerce`` idiom, spelled for a boolean: the ENV supplies the
    base and a declared ``method.options[\"lead_test_v2\"]`` WINS over it, so
    one composition descriptor can be flipped without touching the fleet's env
    — and, more to the point for a crown change, one desk can be flipped and
    watched before thirty-two are.

    Env grammar mirrors :func:`assembly_enabled` exactly: unset/``0`` off,
    ``1`` on, anything else an allow-list of analyst ids. Read on every call,
    never cached at import, so the rollback is an env change and not a deploy.
    """
    on = _flag_state(os.environ.get(LEAD_TEST_V2_ENV), analyst_id)
    if options:
        raw = options.get(LEAD_TEST_V2_OPTION)
        if raw is not None:
            on = _coerce_flag(raw, on)
    return on


def _flag_state(raw: Any, analyst_id: Any) -> bool:
    """``assembly_enabled``'s three-state env grammar, factored for reuse."""
    text = (str(raw or "")).strip()
    if not text or text == "0":
        return False
    if text == "1":
        return True
    return str(analyst_id or "") in {p.strip() for p in text.split(",") if p.strip()}


def _coerce_flag(raw: Any, fallback: bool) -> bool:
    """A descriptor-supplied boolean, TOTAL — an unreadable value keeps the env
    base and is logged, never crashes a run and never silently reads as ON."""
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    logger.info(
        "assembly.bad_option name=%s value=%r — keeping %r",
        LEAD_TEST_V2_OPTION, raw, fallback,
    )
    return fallback



def desk_display_name(analyst_id: Any) -> str:
    """``narrative_coordination`` → ``Narrative coordination``. Display only."""
    slug = str(analyst_id or "").strip()
    if not slug:
        return "Unattributed desk"
    return slug.replace("_", " ").capitalize()


def _severity_rank(level: Any) -> int:
    return _SEVERITY_ORDER.get(str(level or "").strip().lower(), -1)


def assembly_severity(blocks: Sequence[Mapping[str, Any]]) -> str | None:
    """The read's severity = the MAX over carried blocks. ``None`` when no
    carried block declares one — an unstated severity is stated as unknown."""
    best, best_rank = None, -1
    for b in blocks:
        r = _severity_rank(b.get("severity"))
        if r > best_rank:
            best_rank, best = r, str(b.get("severity") or "").strip().lower()
    return best or None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def _row_cited(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The repaired key denormalised onto a slice row, or a zeroed shape.

    A row with no attached key contributes ``cited_mass = 0.0`` to the ORDER —
    which sorts it last inside its severity band, never above a scored peer.
    That is the same fail-safe ``_orient``'s ``-1.0`` magnitude uses: unscored
    sorts last, and is never mistaken for low-consequence.
    """
    v = row.get(CITED_SALIENCE_ROW_KEY)
    if isinstance(v, Mapping):
        return v
    return {
        "cited_max": 0.0, "cited_mass": 0.0, "n_above": 0,
        "n_cited_scored": 0, "n_cited": 0,
        "source": "unscored", "version": CITED_SALIENCE_VERSION,
    }


def _mass(row: Mapping[str, Any]) -> float:
    try:
        return float(_row_cited(row).get("cited_mass") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def order_key(row: Mapping[str, Any]) -> tuple[int, float, str, str]:
    """D-1 §1.5.1 — the composition order, TOTAL and REPRODUCIBLE.

    ``severity DESC, cited_mass DESC, produced_at DESC, finding_id ASC``.

    STILL SEVERITY-FIRST, deliberately, and it is worth saying why because the
    2026-09-07 audit makes the opposite case well: severity is declared by each
    desk against ITS OWN prompt and does not compare across desks
    (``leadership_transition`` declared ``high``/``elevated`` 0 times in 127
    blocks; ``narrative_coordination`` holds the top cited mass in 42 of 129
    reads and has reached ordinal 1 zero times), so a severity-first order does
    read as a DESK ranking wearing an evidence ranking's clothes. The policy
    lane then MEASURED the swap and found mass-first makes the dominance it was
    meant to cure worse, not better — ``energy_security`` gains. So the order
    stands and the real repair is a severity scale that means the same thing in
    two desks' mouths (RULE 3, percentile severity, owed and post-R4). What
    Amendment 4 changes is the CROWN and the CARRY — which block is called the
    lead and which one a region rollup carries — not which block is printed
    first.

    Two properties today's order does not have. TOTAL: no tie is resolved by
    clock jitter — the 09-01 world headline was decided by a FIVE-SECOND
    ``produced_at`` gap between two blocks tied at magnitude 0.95, which is a
    coin flip wearing a number. REPRODUCIBLE: the same candidate set yields the
    same order on replay, which is what makes the A/B arm meaningful at all.

    Returned as an ascending key with the descending fields negated, so callers
    use a plain ``sorted(...)`` and the ``finding_id ASC`` tiebreak stays ASC.
    """
    return (
        -_severity_rank(row.get("severity")),
        -_mass(row),
        _neg_iso(_iso(row.get("produced_at")) or ""),
        str(row.get("id") or ""),
    )


def _neg_iso(value: str) -> str:
    """Sort an ISO timestamp DESCENDING inside an ascending tuple key.

    Complementing each character keeps the total order (ISO-8601 is
    lexicographically ordered) without special-casing the comparator, and
    without the heterogeneous-key ``TypeError`` a datetime/str mix once used to
    hard-freeze the assessors.
    """
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in value)


def _no_head_count(no_head: Any) -> int:
    """``no_head`` as a non-negative int, TOTAL over any input.

    An unparsable or negative ledger count is read as zero, so a malformed
    coverage ledger can only ever make the count bar STRICTER — never looser.
    """
    try:
        missing = int(no_head)
    except (TypeError, ValueError):
        return 0
    return missing if missing > 0 else 0


def min_lead_candidates(no_head: Any = 0) -> int:
    """The count bar RELATIVE TO THE ROSTER THE DESK COULD FIELD (Amendment 4).

    ``max(LEAD_CANDIDATE_FLOOR, MIN_LEAD_CANDIDATES - no_head)`` — the specced
    8, discounted by the units the coverage ledger itself declares headless
    inside the horizon, and never below the floor.

    Pure and total (see :func:`_no_head_count`). The bar can be lowered only by
    a headless unit the ledger ALREADY PUBLISHED — the same array D-3's ARM 4(a)
    audits — so there is no way to buy a lead with a number no other instrument
    can see.
    """
    return max(LEAD_CANDIDATE_FLOOR, MIN_LEAD_CANDIDATES - _no_head_count(no_head))


def earned_lead(
    masses: Sequence[float], *, no_head: Any = 0, v2: bool = False
) -> dict[str, Any]:
    """THE EARNED-LEAD TEST (D-1 §1.5.3), pure and replayable.

    ``EARNED = ratio_12 >= 1.50 AND top_share >= 0.15 AND n_candidates >= 8``
    over the day's candidate pool, on the repaired key.

    Under ``v2`` (:data:`LEAD_TEST_V2_ENV`, default off) the third bar becomes
    ``n_candidates >= max(6, 8 - no_head)`` — see :func:`min_lead_candidates`
    for why it moved and what it still forbids — and the dict gains
    ``min_candidates_effective`` and ``no_head`` beside the unchanged specced
    ``min_candidates``, so a reader sees WHY the bar stood where it did without
    re-deriving it from the drop ledger. v1 stamps NEITHER key, so a flag-off
    row is byte-identical to a pre-amendment one.

    ``lead.test`` is stamped whichever branch fires, so the reason is on the row
    and not in a log — and it is rendered verbatim into the Assessment's prompt,
    so the interpretive voice cannot crown against the record's own arithmetic
    without visibly contradicting a number on its own page.
    """
    ms = sorted((float(m) for m in masses), reverse=True)
    n = len(ms)
    total = sum(ms)
    m1 = ms[0] if ms else 0.0
    m2 = ms[1] if n > 1 else 0.0
    top_share = round(m1 / total, 4) if total > 0 else 0.0
    ratio_12 = None if m2 <= 0.0 else round(m1 / m2, 4)
    missing = _no_head_count(no_head)
    min_effective = min_lead_candidates(missing) if v2 else MIN_LEAD_CANDIDATES
    earned = (
        n >= min_effective
        and top_share >= SHARE_BAR
        and (ratio_12 is None or ratio_12 >= RATIO_BAR)
        and m1 > 0.0
    )
    test = {
        "key": CITED_SALIENCE_VERSION,
        "top_share": top_share,
        # `null` is the honest spelling of "the runner-up scored zero, so the
        # ratio is unbounded" — an infinity in JSONB is not round-trippable and
        # a sentinel float would be read as a measurement.
        "ratio_12": ratio_12,
        "bar_share": SHARE_BAR,
        "bar_ratio": RATIO_BAR,
        # The SPECCED bar, published under BOTH regimes, so v2 reads as a stated
        # discount off a stated number rather than as a silently different one.
        "min_candidates": MIN_LEAD_CANDIDATES,
        "earned": bool(earned),
        "n_candidates": n,
    }
    if v2:
        # The bar this row was actually tested against, and the ledger fact that
        # moved it. Inserted before `earned`/`n_candidates` would be tidier to
        # read; appended instead because key ORDER is the only thing separating
        # a v1 dict from a v2 one on the wire, and a reader diffing two cycles
        # across the flip should see an APPENDED pair, not a reshuffle.
        test["min_candidates_effective"] = min_effective
        test["no_head"] = missing
    return test


def _lead_block(
    masses: Sequence[float],
    test: Mapping[str, Any],
    *,
    v2: bool = False,
    absence: Sequence[bool] = (),
) -> dict[str, Any]:
    """Resolve the ``lead`` object from the test verdict (D-1 §1.5.3 table).

    ``masses`` is in ORDINAL order — the order the blocks are rendered in — so
    ``masses[i]`` is the cited mass of ordinal ``i + 1``.

    v1 (shipped, and what flag-off still does) has one anchor bug in two
    places. The earned branch returns ``[1]`` without checking that ordinal 1 is
    the block that earned it, and the co-lead branch anchors its band on
    ``ms[0]`` — ordinal 1's mass — rather than on the maximum. Both were
    invisible while :func:`order_key` led with severity and ordinal 1 was
    usually the loudest block; measured on 2026-09-06 23:30Z, 4 of the 5 earned
    leads crowned a block that did not hold the top mass (Israel crowned a 0.20
    over a 1.58) and Germany's band ``[1,2,5,7]`` was anchored on 0.15 while
    block 5 carried 0.66.

    v2 anchors both on ``max(masses)``: the crown is the argmax block (ties →
    lowest ordinal, the same total order everything else here uses), and the
    band is emitted in MASS order so ``block_ordinals[0]`` is the heaviest
    co-lead rather than whichever one happened to sort first.

    ``absence`` is a per-ordinal flag from :func:`block_is_absence` — blocks
    whose lead span says nothing happened. Under v2 they are excluded from the
    crown and from the band, because the argmax alone promotes exactly the
    wrong kind of well-cited sentence: *"No coordinated narrative is evident"*
    is scored across 92 signals and outranks everything the day actually did.
    They are NOT removed from ``masses`` and NOT excluded from the earned/none
    TEST — ``top_share`` and ``ratio_12`` are a measurement of the day's shape
    over the whole pool, and a denominator that quietly dropped rows would make
    the concentration test say something it did not measure. What the exclusion
    decides is only WHICH BLOCK WEARS THE CROWN.

    If every block is an absence claim the exclusion is dropped rather than
    escalated: the read crowns its plain argmax and says so with the same
    arithmetic as any other day. A read whose every dimension found nothing has
    a lead position; it is just an honest one.
    """
    ms = [float(m) for m in masses]
    skip = {
        i + 1 for i, flag in enumerate(absence)
        if flag and i < len(ms)
    } if v2 else set()
    eligible = [i + 1 for i in range(len(ms)) if (i + 1) not in skip]
    if not eligible:
        eligible = list(range(1, len(ms) + 1))
    top = max((ms[o - 1] for o in eligible), default=0.0)
    if test.get("earned"):
        crown = _argmax_ordinal(ms, eligible) if v2 else 1
        return {
            "kind": LEAD_EARNED_SINGLE,
            "block_ordinals": [crown],
            "test": dict(test),
        }
    if not v2:
        top = ms[0] if ms else 0.0
    if top > 0.0:
        band = [
            o for o in (eligible if v2 else range(1, len(ms) + 1))
            if ms[o - 1] >= CO_LEAD_BAND * top
        ]
        if v2:
            band.sort(key=lambda o: (-ms[o - 1], o))
        if 2 <= len(band) <= CO_LEAD_MAX:
            return {
                "kind": LEAD_CO_LEADS,
                "block_ordinals": band,
                "test": dict(test),
            }
    # The `none` state is REQUIRED (VOICE §4.5.2) and is the honest reading of a
    # flat day: no lead position, blocks simply begin at ordinal 1.
    return {"kind": LEAD_NONE, "block_ordinals": [], "test": dict(test)}


def _argmax_ordinal(
    masses: Sequence[float], eligible: Sequence[int] | None = None
) -> int:
    """The 1-based ordinal holding the greatest mass; ties → lowest ordinal.

    ``eligible`` restricts the search to those ordinals (the absence exclusion);
    ``None`` searches all. ``1`` for an empty sequence — the earned branch only
    reaches this with a non-empty carry, and an ordinal that does not exist
    would be worse than the one ordinal that always does.
    """
    pool = list(eligible) if eligible else list(range(1, len(masses) + 1))
    best, best_mass = 1, None
    for o in pool:
        if not 1 <= o <= len(masses):
            continue
        m = float(masses[o - 1])
        if best_mass is None or m > best_mass:
            best, best_mass = o, m
    return best


#: The origin head's own cited SIGNALS. Owned by ``source_independence`` since
#: 7d, because the independence count and this projection must read the
#: identical list or the payload and the citation could disagree about a block.
_head_citations = head_citations


def _verify_block(row: Mapping[str, Any]) -> dict[str, Any]:
    """``blocks[].verify`` from what the admission JOIN already lifted.

    ``faithfulness_score`` and ``effective_confidence`` ride the row because the
    candidate gather's ``JOIN LATERAL`` projects them. ``checkable`` /
    ``supported`` are counted from the CLAIM LEDGER the same lateral lifts. What
    the projection does NOT carry — the judge's own status and score state — is
    reported as ``unavailable`` rather than guessed; a fabricated
    ``judge_status`` on a badge line is exactly the class of number this program
    exists to stop.
    """
    def _f(key: str) -> float | None:
        v = row.get(key)
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None

    verdicts = row.get("claim_verdicts")
    checkable = supported = None
    if isinstance(verdicts, list):
        checkable = len(verdicts)
        supported = sum(
            1 for v in verdicts
            if isinstance(v, Mapping) and str(v.get("verdict") or "").lower()
            in ("supported", "support", "true")
        )
    return {
        "overall_score": _f("faithfulness_score"),
        "effective_confidence": _f("effective_confidence"),
        "checkable_claims": checkable,
        "supported_claims": supported,
        "judge_status": "unavailable",
        "score_state": "scored" if _f("faithfulness_score") is not None else "unscored",
    }


def _signals_block(
    row: Mapping[str, Any],
    magnitudes: Mapping[str, float],
    also_cited_by: Mapping[str, list[dict[str, str]]],
    self_key: tuple[str, str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``blocks[].signals[]`` + ``blocks[].corroboration`` — a JOIN, not a build.

    Every field comes off the origin head's OWN ``data.data.citations[]``
    (``signal_id``, ``source_id``, ``title``, ``source`` = url) joined to
    ``signals.salience``. Verified live on 2026-09-03: 892 citation rows across
    227 desk heads, 628 distinct signals, 167 of them cited by more than one
    desk head.
    """
    out: list[dict[str, Any]] = []
    sharing: set[str] = set()
    cited = _head_citations(row)
    for c in cited:
        sid = c.get("signal_id")
        if not sid:
            continue
        key = str(sid)
        others = [
            d for d in also_cited_by.get(key, ())
            if (d.get("desk"), d.get("target_id")) != self_key
        ]
        for d in others:
            sharing.add(f"{d.get('desk')}::{d.get('target_id')}")
        src = c.get("source_id")
        entry: dict[str, Any] = {
            "marker": str(c.get("marker") or ""),
            "signal_id": key,
            "source_id": str(src) if src else None,
            "title": str(c.get("title") or ""),
            "url": str(c.get("source") or ""),
            "salience_magnitude": magnitudes.get(key),
            "also_cited_by": others,
        }
        out.append(entry)
    # 7d — the INDEPENDENCE COUNT rides here: ``n_sources`` keeps its meaning
    # and its position, and the fold keys are written only when they say
    # something (see ``source_independence.corroboration_block``).
    return out, corroboration_block(cited, n_desks_sharing=len(sharing))


def _drop_row(
    row: Mapping[str, Any],
    *,
    why: str,
    rank: int | None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One drop-ledger entry. Same shape in all five grains, so a reader renders
    one table and D-3 checks one schema.

    ``target_name`` was ``str(tid)`` — the slug, twice, on every row of every
    live record. Amendment 7f makes it the unit's HUMAN NAME when one
    resolved; ``target_id`` beside it is untouched, and an unresolved unit
    still renders the slug, byte for byte what shipped.
    """
    tid = row.get("target_id")
    return {
        "finding_id": str(row.get("id") or ""),
        "desk": str(row.get("analyst_id") or ""),
        "target_id": str(tid) if tid is not None else None,
        "target_name": name_of(names, tid) if tid is not None else None,
        "title": str(row.get("title") or "")[:300],
        "rank": rank,
        "tier": str(row.get("_evidence_tier") or "basis"),
        "severity": str(row.get("severity") or "") or None,
        "cited_mass": _mass(row),
        "why": why,
    }


def _tensions(
    blocks: Sequence[Mapping[str, Any]],
    dropped: Sequence[Mapping[str, Any]],
    dropped_why: str = WHY_SHOWN_NOT_SELECTED,
    names: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Declared tensions + the CHECKED-NEGATIVE counters (D-1 §1.6).

    Four rules, all from the design's permission table:

      1. DECLARE, never EXPLAIN. ``statement`` says THAT two blocks pull apart
         and on what shared dimension. It may not say WHY — an inferential link
         between two spans is on the MAY-NOT side. The template statement is the
         default; a judged characterisation is the only remaining judge role at
         this tier and rides D-6 with the channel.
      2. The checked negative is DETERMINISTIC. When ``pairs_found == 0`` the
         payload still carries ``tension_checked``, and the reader renders "no
         conflicting pair was detected among the N shown blocks" from the
         counters. The model can neither manufacture nor dissolve a tension.
         This retires M-11 and the live degenerate form measured at 13.8% of
         world bodies ("all verified reads are mutually consistent").
      3. Scope includes DROPS. A tension whose other half was dropped is the
         single sharpest argument for the drop ledger and must be renderable.
      4. A CROSS-TIER pair SAYS SO IN ITS OWN STATEMENT (W-1, 2026-09-06). Rule
         3 admits the pair; it never licensed the pair to *read* as one the
         record made. The template was symmetric — "A and B describe the same
         dimension and point in opposite directions" — so a carried head paired
         with a head this record did NOT carry reached the voice looking exactly
         like a live conflict between two published reads. It is not one:
         nothing was published on the B side to conflict with. Live on
         2026-09-06 12:00Z the world read's ONLY declared tension was that shape
         (carried ``country_g20_us`` against an uncarried ``country_g20_cn``),
         the Assessment narrated it as a conflict, and the judge could only
         grade that sentence unsupported.

         The repair is at the STATEMENT and the COUNTERS, not at the selection:
         ``b_carried`` / ``b_why`` state the asymmetry structurally, the
         cross-tier template names the uncarried side as uncarried, and
         ``tension_checked`` splits ``pairs_found`` into its two populations so
         the record's own arithmetic can say "1 declared, 0 of them between two
         carried reads" — a sentence the evidence map can actually support.
         Carried-pair statements are BYTE-IDENTICAL; only the cross-tier arm
         moves.
    """
    found: list[dict[str, Any]] = []
    examined = 0
    n = len(blocks)
    for i in range(n):
        for j in range(i + 1, n):
            examined += 1
            a, b = blocks[i], blocks[j]
            ta = a["spans"][0]["text"] if a.get("spans") else ""
            tb = b["spans"][0]["text"] if b.get("spans") else ""
            detail = direction_conflict(ta, tb, target_id=a.get("target_id"))
            if not detail:
                continue
            found.append({
                "kind": "carried_pair",
                "a": {"ordinal": a["ordinal"], "span_index": 0},
                "b": {"ordinal": b["ordinal"], "span_index": 0},
                "b_ref": None,
                "b_carried": True,
                "b_why": None,
                "statement": tension_statement(a, b),
                "statement_source": "template",
                "detector": TENSION_DETECTOR,
                "detector_version": TENSION_DETECTOR_VERSION,
                "same_target": a.get("target_id") == b.get("target_id"),
                "same_window_h": 72,
                "detail": detail,
            })
    for a in blocks:
        ta = a["spans"][0]["text"] if a.get("spans") else ""
        for d in dropped:
            examined += 1
            # P3-B — the OTHER side is carried too. `desk_verdict_text` reads a
            # dropped row's `**BLUF:**`, and on an assembled child that is the
            # connective; a declared cross-tier tension would then publish it
            # as `b_ref.span`. Identical to `desk_verdict_text` on every desk
            # head, i.e. on every dropped row the country tier ever sees.
            tb = carried_verdict_text(d)
            if not tb:
                continue
            detail = direction_conflict(ta, tb, target_id=a.get("target_id"))
            if not detail:
                continue
            found.append({
                "kind": "carried_vs_dropped",
                "a": {"ordinal": a["ordinal"], "span_index": 0},
                "b": None,
                "b_ref": {
                    "finding_id": str(d.get("id") or ""),
                    "desk": str(d.get("analyst_id") or ""),
                    "target_id": str(d.get("target_id") or "") or None,
                    # Amendment 7f — the uncarried side is the one the voice
                    # mistranslated ("Pakistan" for ``country_watch_kp``, 3 of
                    # 6 relays), because this ref was the only thing naming it
                    # and it named it in machine handles. ``None`` when the
                    # drop has no target at all, matching ``target_id``.
                    "target_name": (
                        name_of(names, d.get("target_id")) or None
                    ),
                    "span": tb,
                },
                # W-1 — the asymmetry, STRUCTURALLY. Every reader downstream
                # (the UI, the export, the record's own arithmetic) must be able
                # to tell the two populations apart without parsing prose, and
                # ``kind`` alone did not travel: it appears in neither the
                # rendered body nor the counters the voice is handed.
                "b_carried": False,
                "b_why": str(dropped_why),
                "statement": tension_statement(
                    a,
                    {
                        "desk": str(d.get("analyst_id") or ""),
                        "target_id": str(d.get("target_id") or ""),
                        "target_name": name_of(names, d.get("target_id")),
                    },
                    b_carried=False,
                    b_why=str(dropped_why),
                ),
                "statement_source": "template",
                "detector": TENSION_DETECTOR,
                "detector_version": TENSION_DETECTOR_VERSION,
                "same_target": a.get("target_id") == d.get("target_id"),
                "same_window_h": 72,
                "detail": detail,
            })
    checked = {
        "pairs_examined": examined,
        "pairs_found": len(found),
        # W-1 — the SPLIT, because the total was the number that misled. The two
        # always sum to ``pairs_found``; publishing only the sum told the voice
        # "a tension was declared" and nothing about which population it came
        # from, and the record's own arithmetic could say no more than the sum.
        "pairs_found_carried": sum(1 for t in found if t.get("b_carried")),
        "pairs_found_uncarried": sum(
            1 for t in found if not t.get("b_carried")
        ),
        "scope": (
            TENSION_SCOPE_SHOWN_AND_DROPPED if dropped else TENSION_SCOPE_SHOWN
        ),
        "scope_note": TENSION_SCOPE_NOTE,
        "blocks": n,
    }
    return found, checked


def build_assembly(
    *,
    tier: str,
    as_of: Any,
    candidates: Sequence[Mapping[str, Any]],
    carried: Sequence[Mapping[str, Any]],
    periphery: Sequence[Mapping[str, Any]] = (),
    trimmed: Sequence[Mapping[str, Any]] = (),
    coverage: Sequence[Mapping[str, Any]] = (),
    coverage_roster: Sequence[str] = (),
    magnitudes: Mapping[str, float] | None = None,
    also_cited_by: Mapping[str, list[dict[str, str]]] | None = None,
    questions: Mapping[str, str] | None = None,
    target_names: Mapping[str, str] | None = None,
    invisible_heads: int | None = None,
    regime: str = REGIME_ASSEMBLY,
    lead_test_v2: bool | None = None,
    context_heads: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build ``data.data.assembly`` (schema ``assembly.v1``).

    ``candidates`` is the full ordered candidate pool (the lead-test
    denominator); ``carried`` is its strict prefix, the blocks that will be
    rendered. The caller does the ordering with :func:`order_key` because it
    also has to mint ``derived_from`` in the same order — ordinal N of the
    assembly IS entry N of ``derived_from``, which is what makes the drop ledger
    computable at all.

    ``coverage_roster`` and ``coverage`` are ONE decision, not two: pass the
    exact roster the ledger was built from, or pass neither. A roster beside an
    empty ledger reads to ARM 4(a) as "every declared unit is missing".

    ``context_heads`` is STEP E's join: ``{candidate row id -> {head_id, body,
    match}}``, resolved by ``composition_slice.resolve_country_assessment_
    context`` where a connection exists and passed in here, because this
    function reads no database and must not start. See
    ``assembly_carry.attach_country_context``; an absent key is no context span
    — byte-for-byte the payload that shipped.

    ``lead_test_v2`` selects the crown regime (:data:`LEAD_TEST_V2_ENV`).
    ``None`` — the default — reads the flag here, so a caller that does not
    know about it gets the shipped behaviour; the synthesizer passes the
    OPTION-resolved value instead, because a descriptor knob has to win over the
    env for a per-desk rollout to mean anything.

    Raises :class:`AssemblyConstructionError` when a span cannot be built
    byte-identically, or when the run would publish zero blocks.
    """
    v2 = lead_test_v2_enabled() if lead_test_v2 is None else bool(lead_test_v2)
    mags = dict(magnitudes or {})
    shared = dict(also_cited_by or {})
    qmap = dict(questions or {})
    names = dict(target_names or {})
    ctx_heads = {str(k): v for k, v in (context_heads or {}).items()}

    blocks: list[dict[str, Any]] = []
    carried_origins: list[dict[str, Any]] = []
    for ordinal, row in enumerate(carried, start=1):
        head_id = str(row.get("id") or "")
        desk = str(row.get("analyst_id") or "")
        target_id = row.get("target_id")
        target_key = str(target_id) if target_id is not None else None

        # P3-B — A CANDIDATE THAT ALREADY CARRIES WORDS IS CARRIED, NOT RE-CUT,
        # and the WHY lives once, in the module holding the rule: see
        # ``assembly_carry``'s banner (the connective defect it closes, measured
        # live over seven days) and ``carry_block``. A desk head, a LEGACY row
        # and a rollup all resolve to ``None`` and take the path below.
        child = child_assembly(row)
        if child is not None:
            source_block, via_ordinal, carry_reason = carried_lead_block(child)
            if source_block is None:
                raise AssemblyConstructionError(
                    f"assembly block {ordinal} ({desk} / {target_key}) declares "
                    f"an {ASSEMBLY_SCHEMA} payload with no block to carry"
                )
            block = carry_block(
                source_block,
                ordinal=ordinal,
                via_head_id=head_id,
                via_ordinal=via_ordinal,
                carry_reason=carry_reason,
            )
            # STEP E — THE WORLD TIER'S CONTEXT IS THE COUNTRY'S VOICE; the
            # rule, the measurement and the two-origins argument live in
            # :func:`attach_country_context`.
            if str(tier) == TIER_WORLD:
                try:
                    attach_country_context(block, ctx_heads.get(head_id))
                except SpanConstructionError as exc:
                    raise AssemblyConstructionError(
                        f"assembly block {ordinal} ({desk} / {target_key}) "
                        f"could not carry its country assessment as context: "
                        f"{exc}"
                    ) from exc
            blocks.append(block)
            # The depth-1 bridge for THIS carry, lifted from the child's own
            # citation for the desk head. Omitted, never fabricated, when the
            # child published none — see `carried_origin_record`.
            origin_record = carried_origin_record(row, block)
            if origin_record is not None:
                carried_origins.append(origin_record)
            continue

        try:
            span = extract_lead_span(
                row,
                head_id=head_id,
                scope_predicate=has_collection_denominator_scope,
            )
            # P3 LANE A — THE CONTEXT SPAN, COUNTRY TIER ONLY: the origin desk
            # head in FULL, through the SAME gate (hence the same try). Never
            # rendered, never quoted; see ``assembly_spans.SPAN_ROLE_CONTEXT_BODY``.
            context_span = (
                build_context_span(row, head_id=head_id)
                if str(tier) == TIER_COUNTRY else None
            )
        except SpanConstructionError as exc:
            # F-12, ratified: a block whose span fails byte-identity against its
            # source finding CANNOT BE CONSTRUCTED. Fail loud into the existing
            # error path — the actor classifies it — rather than publishing a
            # read with a silent hole where a quotation should be.
            raise AssemblyConstructionError(
                f"assembly block {ordinal} ({desk} / {target_key}) could not be "
                f"constructed: {exc}"
            ) from exc
        signals, corroboration = _signals_block(
            row, mags, shared, (desk, target_key)
        )
        question = qmap.get(desk)
        blocks.append({
            "ordinal": ordinal,
            "finding_id": head_id,
            "desk": desk,
            "target_id": target_key,
            "target_name": names.get(target_key or "", target_key),
            "question": question or desk_display_name(desk),
            "question_source": (
                QUESTION_SOURCE_DESCRIPTOR if question else QUESTION_SOURCE_FALLBACK
            ),
            "produced_at": _iso(row.get("produced_at")),
            # `None` (never 0.0) when the row carries no parsable timestamp —
            # an unknown age is stated as unknown, because a zero would read as
            # "composed just now", which is exactly the lie FRAME-1 exists to
            # stop. `head_age_hours` already enforces that; use it rather than
            # re-deriving an age beside it.
            "evidence_age_h": (
                None if (_age := head_age_hours(row)) is None else round(_age, 2)
            ),
            "tier": str(row.get("_evidence_tier") or "basis"),
            "severity": str(row.get("severity") or "") or None,
            "verify": _verify_block(row),
            "salience": dict(_row_cited(row)),
            # LEAD stays index 0 (``_tensions``/``_lead_fragment``/the BLUF all
            # read ``spans[0]``); context rides behind it.
            "spans": [span] + ([context_span] if context_span else []),
            "signals": signals,
            "corroboration": corroboration,
        })

    if not blocks:
        raise AssemblyConstructionError(
            f"{tier} assembly would publish ZERO blocks from "
            f"{len(candidates)} candidates — a read with nothing in it is a "
            f"construction failure, not an empty page"
        )

    # A CONNECTIVE MAY NEVER BECOME A SPAN (P3-B). The carry above is what makes
    # this true on the common case; this is the gate that says so at
    # CONSTRUCTION, in the same posture `assembly_spans` takes on byte-identity
    # (D-1 §3.6, ratified F-12): the assembly is deterministic, so a connective
    # in a quote is a BUG in this module, not a score for verify to publish
    # around. It fires on the residue — a child shaped in a way the carry rule
    # did not recognise, a hand-built candidate in a test, a future producer
    # that renders its connectives somewhere new — and it fires LOUD rather
    # than shipping a read whose headline asserts nothing.
    #
    # STEP E — THE DENOMINATOR IS THE QUOTATIONS, and that is this guard's own
    # sentence read back rather than a loosening of it; ``connective_leak``'s
    # docstring carries the argument.
    for block in blocks:
        for span in quoted_spans(block):
            text = span.get("text") if isinstance(span, Mapping) else ""
            phrase = connective_leak(text)
            if phrase is not None:
                raise AssemblyConstructionError(
                    f"{tier} assembly block {block.get('ordinal')} "
                    f"({block.get('desk')} / {block.get('target_id')}) quotes a "
                    f"CONNECTIVE, not a claim: its span carries "
                    f"{phrase!r}, which is a composed read's own "
                    f"lead line and asserts nothing about the world. A "
                    f"candidate that carries an assembly payload contributes a "
                    f"carried desk block; one that reaches this line was cut "
                    f"from a composed body and must not be published"
                )

    # Computed BEFORE the lead test, not inside the drop ledger below, because
    # the count bar is now relative to it (Amendment 4): a unit the ledger
    # declares headless is a dimension that COULD NOT be fielded, and the bar
    # against a thin read must not also bar a complete one. Same list, one
    # place, so the number in `lead.test.no_head` and the number in
    # `drops.counts.no_head` cannot drift.
    no_head_units = [
        {"unit": str(c.get("unit") or ""), "why": WHY_NO_HEAD}
        for c in coverage
        if str(c.get("status") or "") == "no_head_in_horizon"
    ]

    masses = [_mass(r) for r in candidates]
    test = earned_lead(masses, no_head=len(no_head_units), v2=v2)
    # Derived from the BUILT blocks, not from the candidate rows: the span is
    # what a reader sees in the lead position, and the span is what
    # `extract_lead_span` cut — re-deriving the absence question off the head's
    # full body would be answering about text the read never carried.
    lead = _lead_block(
        [_mass(r) for r in carried],
        test,
        v2=v2,
        absence=[block_is_absence(b) for b in blocks],
    )

    carried_ids = {str(r.get("id") or "") for r in carried}
    not_carried = [
        _drop_row(r, why=WHY_SHOWN_NOT_SELECTED, rank=i + 1, names=names)
        for i, r in enumerate(candidates)
        if str(r.get("id") or "") not in carried_ids
    ]
    tensions, checked = _tensions(
        blocks,
        [r for r in candidates if str(r.get("id") or "") not in carried_ids],
        # The set handed over IS grain 1 of the ledger below, so the cross-tier
        # statement names the same why-class the drop row will carry rather than
        # a second vocabulary for the same fact.
        dropped_why=WHY_SHOWN_NOT_SELECTED,
        names=names,
    )

    drops = {
        "shown_not_carried": not_carried,
        # GRAIN 2 — the deliverable. §F-7: the prefix property is free and
        # always true; THIS is the list with teeth, because it publishes what
        # the order put below the line rather than merely that the selection
        # obeyed the order.
        #
        # The two grains coincide ROW-FOR-ROW under a strict-prefix selection,
        # and that is not a bug to collapse — it is §F-7's finding made
        # visible. Grain 1 asks "what was shown and not carried"; grain 2 asks
        # "what did the ORDER put below the cut". Today the answer is the same
        # set; the day a selection stops being a pure prefix (a correlated
        # duplicate skipped, a construction failure dropped) they diverge, and a
        # reader that had only one of them would not be able to tell. Each
        # carries its own why-class so the divergence is legible the first time
        # it happens.
        "not_selected": [
            _drop_row(r, why=WHY_NOT_SELECTED, rank=i + 1, names=names)
            for i, r in enumerate(candidates)
            if str(r.get("id") or "") not in carried_ids
        ],
        "trimmed": [
            _drop_row(r, why=WHY_CAP_TRIMMED, rank=None, names=names)
            for r in trimmed
        ],
        "below_floor": [
            _drop_row(r, why=WHY_BELOW_FLOOR, rank=None, names=names)
            for r in periphery
        ],
        "no_head": no_head_units,
        "why_classes": list(WHY_CLASSES),
    }
    drops["counts"] = {
        "shown": len(candidates) + len(trimmed),
        "carried": len(blocks),
        "shown_not_carried": len(not_carried),
        "candidates": len(candidates),
        "not_selected": len(not_carried),
        "trimmed": len(drops["trimmed"]),
        "below_floor": len(drops["below_floor"]),
        "no_head": len(drops["no_head"]),
        # A COUNT, never a list, and the spec says why: on 2026-09-03 the world
        # read's lineage reached 260 desk heads of which 228 were never
        # candidates for the world surface at all. No why-class is DERIVABLE for
        # them because nothing records one — they are below the target-grain
        # aperture. Publishing a fabricated reason for 228 rows would be worse
        # than publishing the count.
        #
        # `null`, not `0`, when the caller could not measure it. The count needs
        # a lineage walk the DB-free assembler cannot do; a zero here would read
        # as "this surface saw everything", which is the exact false statement
        # the ledger exists to retire.
        "invisible_heads": None if invisible_heads is None else int(invisible_heads),
    }

    return {
        "schema": ASSEMBLY_SCHEMA,
        "regime": regime,
        "tier": str(tier),
        "as_of": _iso(as_of),
        "lead": lead,
        "blocks": blocks,
        "tensions": tensions,
        "tension_checked": checked,
        "drops": drops,
        # P3-B — THE DEPTH-1 BRIDGE for carried blocks, and ONLY when there are
        # any: a read with no carry publishes no key and is byte-for-byte the
        # payload that shipped (every country assembly, every flag-off row).
        # D-3 reads it with the SAME `origin_records` reader it uses on the
        # row's citations, so a carried block is audited against the DESK head's
        # body rather than against the candidate the block merely came through.
        **({CARRIED_ORIGINS_KEY: carried_origins} if carried_origins else {}),
        # D-1 §1.4c — `build_coverage_ledger` is computed today, rendered into
        # the prompt and THROWN AWAY. Persisting it verbatim takes the
        # `metadata_mismatch` fail class (the model's `## Coverage` prose
        # disagreeing with the ledger it was handed) from "caught" to
        # "impossible", and makes coverage completeness a diff between two
        # persisted arrays rather than a judged rubric.
        "coverage": [dict(c) for c in coverage],
        # D-2b — the ledger's DENOMINATOR, persisted BESIDE the ledger it is the
        # denominator of. `build_coverage_ledger`'s own docstring says why the
        # two have to travel together: derive the roster from the rows that
        # ARRIVED and a missing unit becomes invisible, which is the original
        # defect. D-3's ARM 4(a) therefore refuses to reconstruct one and counts
        # `assembly_coverage_roster_absent` when it is absent — a DECLINE, never
        # a pass. Persisted, §B.6.4(a) coverage completeness becomes a diff
        # between two published arrays.
        #
        # `[]` — which the arm reads as absent and declines on — wherever the
        # ledger itself is empty: the roster-based ledger is per-COUNTRY only,
        # and a roster published beside an empty ledger would make every declared
        # unit read as MISSING. That is a fabricated fire, and worse than the
        # honest blindness D-2 §10 already names above the country tier.
        "coverage_roster": [str(u) for u in coverage_roster],
        # Amendment 7f — THE UNITS' HUMAN NAMES, published so the fenced
        # readers can render them. The Assessment prompt may reach nothing
        # but this payload, so a name that is not ON the row cannot be shown
        # to the voice, which is exactly how the voice came to write
        # "Pakistan" for ``country_watch_kp`` on a live record. Additive: no
        # shipped field changes shape, and `{}` on every record whose caller
        # passes no ``target_names`` — which is byte-for-byte the shipped row.
        #
        # Scoped to the units this record MENTIONS rather than the descriptor
        # table: the roster, the blocks, the ledger, the coverage entries and
        # the tension refs. See `unit_names.published_names`.
        PAYLOAD_NAMES_KEY: published_names(
            names,
            [str(u) for u in coverage_roster]
            + [b.get("target_id") for b in blocks]
            + [c.get("unit") for c in coverage]
            + [
                r.get("target_id")
                for grain in ("shown_not_carried", "not_selected", "trimmed",
                              "below_floor")
                for r in drops[grain]
            ]
            + [
                (t.get("b_ref") or {}).get("target_id")
                for t in tensions
            ],
        ),
        "connectives": {"vocabulary_version": CONNECTIVE_VOCABULARY_VERSION},
    }


def legacy_regime_stamp() -> dict[str, Any]:
    """The minimal ``assembly`` block stamped while the flag is OFF.

    D-1 §5.2: ``regime`` rides EVERY composition row from the moment D-2 merges,
    not from the moment the flag flips — otherwise the A/B boundary is invisible
    inside one judge stamp and every pooling reader silently mixes the arms.
    """
    return {
        "schema": ASSEMBLY_SCHEMA,
        "regime": REGIME_LEGACY,
    }


def candidate_masses(rows: Iterable[Mapping[str, Any]]) -> list[float]:
    """The lead-test denominator: one ``cited_mass`` per candidate."""
    return [_mass(r) for r in rows]


def row_signal_ids(
    row: Mapping[str, Any],
    child_signal_ids: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[str], str]:
    """The signal ids `cited_mass.v1` scores for ``row``, and WHICH GRAIN.

    Two grains, and the split is not cosmetic:

      * a DESK head cites wire items, so its ``citations[]`` carry ``signal_id``
        and the key is computed directly — ``cited_signals``;
      * a COMPOSITION head (a country or region read, which is what the world
        tier's candidates ARE) cites FINDINGS, so its citations carry ``ref_id``
        and no ``signal_id`` at all. Scoring it directly returns 0.0 for every
        candidate, which is how the world read gets a flat pool and a
        permanently unearned lead — a silent zero, not a measurement. Its ids
        are POOLED from its children's citations one hop down and the source is
        stamped ``pooled_child_signals``.

    Distinct ids on purpose: two desks resting on one shared wire item are one
    piece of evidence, which is the same de-duplication the correlation guard's
    ``shared_signals`` already computes at verify time.
    """
    direct = [
        str(c["signal_id"])
        for c in _head_citations(row)
        if c.get("signal_id")
    ]
    if direct:
        return direct, "cited_signals"
    pooled = (child_signal_ids or {}).get(str(row.get("id") or ""), ())
    return [str(s) for s in pooled], "pooled_child_signals"


def attach_cited_salience(
    rows: Sequence[Mapping[str, Any]],
    magnitudes: Mapping[str, float],
    child_signal_ids: Mapping[str, Sequence[str]] | None = None,
) -> None:
    """Denormalise `cited_mass.v1` onto each slice row, in place.

    Two keys, because the payload needs two different things from one join: the
    per-head KEY (which is what the order and the lead test read) and the
    per-signal MAGNITUDES (which the evidence-map strip renders). Stamping the
    magnitudes here rather than threading the whole join through ``_run`` keeps
    ``_run`` DB-free and keeps the annotation with the row it describes.
    """
    for row in rows:
        try:
            ids, source = row_signal_ids(row, child_signal_ids)
            row[CITED_SALIENCE_ROW_KEY] = salience_over_ids(  # type: ignore[index]
                ids, magnitudes, source=source
            )
            row[CITED_MAGNITUDES_ROW_KEY] = {  # type: ignore[index]
                s: magnitudes[s] for s in ids if s in magnitudes
            }
        except Exception:  # degrade-not-break: an unscored row sorts last
            logger.warning(
                "assembly.cited_salience failed for head %s", row.get("id")
            )


def assembly_magnitudes(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Re-merge the per-row magnitude maps into the one the payload wants."""
    out: dict[str, float] = {}
    for row in rows:
        m = row.get(CITED_MAGNITUDES_ROW_KEY)
        if isinstance(m, Mapping):
            for k, v in m.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
    return out


def assembly_shared_signals(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """``signal_id -> [{desk, target_id}]`` across the slice — `also_cited_by`.

    The cross-desk corroboration index, computed from rows already in hand. On
    2026-09-03 this was 167 of 628 distinct signals cited by more than one desk
    head, which is the number that makes the evidence-map strip worth rendering:
    a wire item two independent desks reached for is a different object from one
    that only one desk saw.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        desk = str(row.get("analyst_id") or "")
        tid = row.get("target_id")
        entry = {"desk": desk, "target_id": str(tid) if tid is not None else ""}
        for c in _head_citations(row):
            sid = c.get("signal_id")
            if not sid:
                continue
            bucket = out.setdefault(str(sid), [])
            if entry not in bucket:
                bucket.append(entry)
    return out


def assembly_confidence(payload: Mapping[str, Any]) -> float:
    """The row's ``confidence`` — the WEAKEST carried block's effective score.

    Deterministic and conservative, and it is the house's own idiom: every
    admission predicate in the tree folds to ``LEAST(confidence,
    faithfulness_score)``. A composed read is no stronger than the weakest thing
    it quotes, and under the assembly it quotes everything it carries — so the
    minimum is not a heuristic, it is the arithmetic of what the page contains.
    Blocks with no score do not drag it down (unscored is not weak); a payload
    with no scored block at all returns 0.5, the model default, because that is
    the honest "no basis to move it".
    """
    scores = []
    for b in payload.get("blocks") or ():
        v = (b.get("verify") or {}).get("effective_confidence")
        try:
            scores.append(float(v))
        except (TypeError, ValueError):
            continue
    return round(min(scores), 4) if scores else 0.5


def assembly_tags(payload: Mapping[str, Any]) -> list[str]:
    """The row's ``tags``, which is how ``severity`` reaches its READ COLUMN.

    Severity travels as a ``severity:<level>`` TAG that the write path lifts to
    ``analyst_outputs.severity`` (``models.severity_from_tags``). It is NULL on
    100% of composition rows today — 0 of 1,108 in 14 days — while desk heads
    carry it on ~99%, which is why the severity dot in the world history list has
    never rendered. The assembly sets it to the max across carried blocks, and
    the dot works for the first time at zero extra cost.
    """
    tags = ["assembly", f"regime:{payload.get('regime')}"]
    sev = assembly_severity(payload.get("blocks") or ())
    if sev:
        tags.append(f"severity:{sev}")
    lead = (payload.get("lead") or {}).get("kind")
    if lead:
        tags.append(f"lead:{lead}")
    return tags


def now_iso() -> str:
    """UTC now, ISO-8601 with a ``Z``-equivalent offset. One spelling."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
