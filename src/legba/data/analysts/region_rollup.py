"""THE REGION ROLLUP — the region tier stops writing and starts adding up.

D-5, the CASCADE (planning/DEMOTION_D1_SPEC_2026-09-04.md §4). Under the
assembly regime ``region_composition`` retires its GENERATIVE path: no LLM, no
prompt, no faithfulness judge. What it emits instead is an arithmetic statement
about its member country assemblies — ``region_rollup.v1``.

WHAT THE ROLLUP IS, AND WHAT IT REFUSES TO BE.

The rollup **carries block objects; it does not re-quote**. A region lead block
is the member country assembly's own ``blocks[]`` object, byte-identical,
including ``origin.head_id`` still pointing at the DESK HEAD that wrote the
sentence. That is the span-carries-origin principle doing its job: quote
fidelity at the region tier is the *same depth-1 test against the same desk
head*, with one implementation and no second quoting act to get wrong. There is
no re-extraction here, so there is no new way to be unfaithful — which is the
entire argument for retiring the tier's prose.

THE ONE HONEST LOSS, NAMED. A generative region read carried a SYNTHESIS across
its members: a cross-border sentence no single country read contains. The rollup
cannot write that sentence and does not pretend to — the tier's connective
vocabulary (:data:`ROLLUP_CONNECTIVES`) may say "these members were seen" and
may not say anything about the world. Where the synthesis has to live is one
floor up: the world assembler now reads the COUNTRY assemblies directly (§4.2),
so a cross-border claim is made once, at the tier that can cite both sides.

THE VERIFY STORY, AND WHY IT IS NOT A HOLE.

A deterministic rollup has NO prose to grade, so no faithfulness critique is
written for it, and none is faked. What replaces it is a REAL check, not a
missing one: the rollup declares its arithmetic in ``data.structural_claims``
(:func:`rollup_structural_claims`) and the existing C2b structural-claims
profile re-derives every number from the constituent set the row itself
recorded. A rollup that misstates its own member count is caught
deterministically; a rollup that could not be re-derived is
``unverifiable_structural``, never a fake pass.

**Because that critique is not a faithfulness verdict, a rollup can never be
admitted through a ``verify_floor`` INNER JOIN.** That is the trap §4.2 names,
and this module does not try to defeat it — ``composition_slice`` removes the
dependency instead: the world path stops reading region heads entirely. Any
FUTURE consumer that gates on a faithfulness score will silently drop region
rows, and the fix is the same one: read the tier that has the words.

THE REGIME LABEL. ``data.assembly.regime`` is ``"rollup"`` on these rows —
neither ``legacy`` (prose a model wrote) nor ``assembly`` (spans this tier cut).
Three values, three genuinely different producers, so every pooling reader can
split them and nobody reports a rollup's arithmetic beside an assembly's quote
fidelity as though they measured the same thing.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Mapping, Sequence

from .assembly_payload import (
    ASSEMBLY_SCHEMA,
    REGIME_ROLLUP,
    _iso,
    _severity_rank,
    assembly_enabled,
    block_is_absence,
)
# THE CARRY RULE, imported ONE WAY and re-exported. It was written here and it
# is not this tier's alone: the WORLD assembler carries country and thematic
# blocks by the same rule (P3-B), and two tiers deciding differently about the
# same child is the failure this module's own banner warns about. So the rule,
# its why-classes and the block-mass reader moved to `assembly_carry` and this
# module aliases them — every name below resolves exactly as it did.
from .assembly_carry import (  # noqa: F401 — re-exported surface
    CARRY_BY_MASS,
    CARRY_EARNED_LEAD,
    CARRY_FALLBACK_ORDINAL_1,
    CARRY_REASONS,
    block_mass as _block_mass,
    carried_lead_block,
)
from .assembly_spans import quoted_spans as _quoted_spans
from .composition_slice import REGION_MEMBERSHIP_ROW_KEY
from .composition_window import MAX_TITLE_CHARS, _defuse_child_ref_markers, head_age_hours
from ..provenance.kinds import (
    ROLLUP_LEAD_CARRIED,
    ROLLUP_PAYLOAD_SCHEMA,
    is_deterministic_rollup as is_rollup_payload,
)
from ..provenance.structural_claims import STRUCTURAL_CLAIMS_DATA_KEY

logger = logging.getLogger(__name__)

__all__ = [
    "ROLLUP_SCHEMA",
    "STRUCTURAL_CLAIMS_DATA_KEY",
    "REGION_MEMBERSHIP_ROW_KEY",
    "ROLLUP_TIER",
    "ROLLUP_CONNECTIVES",
    "MEMBER_IN_BASIS",
    "MEMBER_NO_ASSEMBLY",
    "MEMBER_STALE",
    "MEMBER_STATUSES",
    "LEAD_CARRIED",
    "LEAD_NONE",
    "CARRY_EARNED_LEAD",
    "CARRY_BY_MASS",
    "CARRY_FALLBACK_ORDINAL_1",
    "CARRY_REASONS",
    "REGISTER_LIVE",
    "REGISTER_CITED_NOTHING",
    "REGISTER_SUB_FLOOR_ONLY",
    "REGISTERS",
    "ROLLUP_MASS_FLOOR_ENV",
    "ROLLUP_MASS_FLOOR_OPTION",
    "ROLLUP_MASS_FLOOR_DEFAULT",
    "ROLLUP_MASS_FLOOR_SAFE_MAX",
    "rollup_mass_floor",
    "assemble_region_rollup",
    "build_region_rollup",
    "is_rollup_payload",
    "region_rollup_enabled",
    "render_rollup_body",
    "rollup_assembly_stamp",
    "rollup_inputs",
    "rollup_ordinal_index",
    "ROLLUP_CONFIDENCE_NOT_EVIDENCE",
    "rollup_confidence",
    "rollup_finding_json",
    "rollup_severity",
    "rollup_step",
    "rollup_structural_claims",
    "rollup_tags",
    "rollup_title",
]

#: ``data.data.rollup.schema``. Versioned like every other payload on this row.
#:
#: DEFINED IN ``provenance.kinds`` and aliased here, not the other way round.
#: Every guard that must RECOGNISE a rollup — the faithfulness scope guard, the
#: structural-claims opt-in, the badge — sits on the provenance side of the
#: layering and none of them may import an analyst module to do it. The producer
#: importing its own marker from the registry is the direction that works.
ROLLUP_SCHEMA: str = ROLLUP_PAYLOAD_SCHEMA

#: The tier label. The assembly's tier enum is country|world|thematic and
#: deliberately has no REGION value (§4 retires the region GENERATIVE path
#: outright), so the rollup carries its own — a rollup is not an assembly and a
#: reader that treats it as one would compare a sum against a quotation.
ROLLUP_TIER: str = "region"

#: Per-member coverage status. The spec's three tokens, defined precisely:
#:
#:   * ``in_basis``     — an ASSEMBLED member head was admitted and its lead
#:                        block is carried forward into ``leads[]``.
#:   * ``no_assembly``  — the member contributed no assembled head this window.
#:                        Either it had no admitted head at all (``has_head``
#:                        false) or its head is a LEGACY-regime row with no
#:                        ``assembly.blocks`` to carry (``has_head`` true). The
#:                        entry says which; the rollup carries no lead for it
#:                        either way, because inventing one would mean quoting,
#:                        and quoting is the act this tier just retired.
#:   * ``stale``        — an assembled head was carried, but its origin is older
#:                        than the run's admissibility horizon. Carried AND
#:                        flagged: the FRAME-1 doctrine is that an old read is
#:                        admitted with its age stated, never silently dropped.
MEMBER_IN_BASIS: str = "in_basis"
MEMBER_NO_ASSEMBLY: str = "no_assembly"
MEMBER_STALE: str = "stale"
MEMBER_STATUSES: tuple[str, ...] = (MEMBER_IN_BASIS, MEMBER_NO_ASSEMBLY, MEMBER_STALE)

#: ``members[].lead_source``. A lead is CARRIED (the member assembly's own block
#: object, unchanged) or there is NONE. There is deliberately no third value:
#: "constructed here" would be a new quoting act at a tier that no longer
#: quotes, and it would break the depth-1-to-a-desk-head property that makes one
#: quote-fidelity implementation cover both tiers.
#: ALIASED from ``provenance.kinds`` rather than re-typed, same direction and
#: same reason as :data:`ROLLUP_SCHEMA`: the export's read-side re-map of
#: historical rows filters on this token and sits on the provenance side of the
#: layering, so the token has to live where both can reach it.
LEAD_CARRIED: str = ROLLUP_LEAD_CARRIED
LEAD_NONE: str = "none"

#: ``members[].carry_reason`` — WHICH RULE picked the block that was carried.
#: The vocabulary and the measurement behind it now live with the rule itself
#: (``assembly_carry.CARRY_REASONS``), aliased above; it is shared with the
#: world tier's carried blocks and a second copy of it here could drift.

#: The NOISE FLOOR on a by-mass carry (Amendment 4a). A block must EXCEED this
#: to be carried by mass; a member whose every block is at or under it falls
#: back to ordinal 1. Default 0.0 — carry anything with mass at all — which is
#: exactly the rule above and is byte-identical to it, so this knob ships DARK
#: and can be measured before it is used.
#:
#: Documented SAFE BAND ``(0, 0.10]``, measured over 102 live member-carries:
#: 0.10 moves 28 carries (27.5%), 0.25 moves 50 (49.0%), 0.50 moves 70 (68.6%).
#: Above the band it stops being a noise floor: a member whose every block is
#: under the floor carries LESS evidence than before, because ordinal 1 is
#: where it lands, and the floor was supposed to be a cure for exactly that.
ROLLUP_MASS_FLOOR_ENV: str = "LEGBA_ROLLUP_MASS_FLOOR"
ROLLUP_MASS_FLOOR_OPTION: str = "rollup_mass_floor"
ROLLUP_MASS_FLOOR_DEFAULT: float = 0.0
ROLLUP_MASS_FLOOR_SAFE_MAX: float = 0.10

#: ``members[].register`` — a LABEL on each carried member, never a number and
#: never a filter (§3.3). A pure function of the carried block's own
#: ``salience``, which is the point: it says which of three DIFFERENT things a
#: ``cited mass 0.00`` line means, where today a reader sees one string and has
#: to guess.
#:
#:   * ``live``           — the block carries cited mass. 23 of 32 at the
#:                          measured cycle.
#:   * ``cited_nothing``  — the block cites NO signals at all, so there is no
#:                          mass to have. 2 of 32. This is the read saying it
#:                          found nothing, and it is a real state.
#:   * ``sub_floor_only`` — the block DID cite signals and every one of them
#:                          scored under the salience floor, so the mass is
#:                          0.00 for a completely different reason. 7 of 32.
#:
#: The two zero classes are what the external reviewer read as nine "unchanged
#: at 0.00" carries; they are not the same finding and a label is the cheapest
#: honest way to say so. No score moves, the title is untouched, and nothing is
#: hidden — this only names what the row already contains.
REGISTER_LIVE: str = "live"
REGISTER_CITED_NOTHING: str = "cited_nothing"
REGISTER_SUB_FLOOR_ONLY: str = "sub_floor_only"
REGISTERS: tuple[str, ...] = (
    REGISTER_LIVE,
    REGISTER_CITED_NOTHING,
    REGISTER_SUB_FLOOR_ONLY,
)

#: The register's DISPLAY spelling in the body. The payload keeps the
#: identifier; the page gets English. Both are closed sets and this mapping is
#: the only bridge, so a new register cannot reach a reader unnamed.
_REGISTER_WORDS: dict[str, str] = {
    REGISTER_LIVE: "live",
    REGISTER_CITED_NOTHING: "cited nothing",
    REGISTER_SUB_FLOOR_ONLY: "sub-floor only",
}

#: THE CLOSED CONNECTIVE VOCABULARY, same mechanism as the assembly's
#: (``assembly_render.CONNECTIVES``) and the same reviewer question: DOES IT
#: ASSERT A STATE OF THE WORLD? Every entry below answers no. They describe the
#: ROLLUP — what it added up, what it could not see — never the region.
ROLLUP_CONNECTIVES: tuple[str, ...] = (
    "## Member reads",
    "## Members not carried",
    "## Coverage",
    "rolled up from",
    "member country read",
    "member country reads",
    "no assembled read this cycle",
    "no read inside the horizon",
    "carried from",
    "of the region's",
    "members carried",
    "in basis",
    "no assembly",
    "stale",
    "severity",
    "cited mass",
    "register",
    "live",
    "cited nothing",
    "sub-floor only",
    "This is a deterministic rollup: no sentence below was written for it.",
)

#: How much of a carried lead span the title may quote (mirrors
#: ``assembly_render.MAX_TITLE_FRAGMENT_CHARS``).
MAX_TITLE_FRAGMENT_CHARS: int = 110


def region_rollup_enabled(analyst_id: Any) -> bool:
    """Is the ROLLUP regime on for ``analyst_id``?

    ONE regime switch, not two (operator ruling for D-5). The spec's §5.2 table
    proposed a separate ``LEGBA_REGION_ROLLUP``; the ruling collapses it into
    ``LEGBA_COMPOSITION_ASSEMBLY`` so the cascade cannot be half-flipped — a
    world reading country assemblies while the region tier still generates, or
    a rolled-up region tier still being read by the world, are exactly the two
    states §4.2's trap lives in. This is an alias with a docstring rather than a
    second predicate, so nothing can drift between them.
    """
    return assembly_enabled(analyst_id)


def _assembly_of(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The member row's own ``data.data.assembly``, when it has one with blocks.

    A legacy-regime row carries ``{schema, regime: "legacy"}`` and NO blocks —
    that is not an assembly to carry from, and this returns ``None`` so the
    member lands in ``no_assembly`` with the reason on its entry.
    """
    data = row.get("data")
    if not isinstance(data, Mapping):
        return None
    inner = data.get("data")
    inner = inner if isinstance(inner, Mapping) else data
    asm = inner.get("assembly")
    if not isinstance(asm, Mapping):
        return None
    blocks = asm.get("blocks")
    if not isinstance(blocks, Sequence) or not blocks:
        return None
    return asm


def rollup_mass_floor(options: Mapping[str, Any] | None = None) -> float:
    """The by-mass carry floor: env base, descriptor option WINS over it.

    The house ``_coerce`` idiom
    (``_coverage_floor_scan.CoverageFloorConfig.from_options``), spelled for one
    float. TOTAL: an unparsable value on either channel keeps the value it
    would have had and is logged, because a floor that silently became 0.5
    because someone typed ``"0.5 "`` wrong would rewrite every region read.

    Negative clamps to 0.0. A value above :data:`ROLLUP_MASS_FLOOR_SAFE_MAX` is
    HONOURED and logged as out-of-band — the operator is allowed to leave the
    documented band, but never by accident and never silently.
    """
    value = ROLLUP_MASS_FLOOR_DEFAULT
    raw_env = os.environ.get(ROLLUP_MASS_FLOOR_ENV)
    if raw_env is not None:
        value = _coerce_float(raw_env, value, source="env")
    if options:
        raw_opt = options.get(ROLLUP_MASS_FLOOR_OPTION)
        if raw_opt is not None:
            value = _coerce_float(raw_opt, value, source="option")
    if value < 0.0:
        return ROLLUP_MASS_FLOOR_DEFAULT
    if value > ROLLUP_MASS_FLOOR_SAFE_MAX:
        logger.warning(
            "region_rollup.mass_floor_out_of_band value=%s safe_max=%s — "
            "honoured, but this is an instrument change, not a retune",
            value, ROLLUP_MASS_FLOOR_SAFE_MAX,
        )
    return value


def _coerce_float(raw: Any, fallback: float, *, source: str) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        logger.info(
            "region_rollup.bad_%s name=%s value=%r — keeping %r",
            source, ROLLUP_MASS_FLOOR_OPTION, raw, fallback,
        )
        return fallback


def member_register(block: Mapping[str, Any] | None) -> str | None:
    """The carried block's REGISTER (:data:`REGISTERS`), or ``None`` if nothing
    was carried.

    A pure function of the block's own ``salience``, re-derived nowhere else:
    ``n_cited == 0`` is the read having cited nothing, a zero mass with
    citations behind it is a read whose every citation scored under the
    salience floor, and anything with mass is live. An assembly whose block
    carries no ``salience`` object at all reads as ``cited_nothing`` — it cited
    nothing this instrument can see, which is the honest thing to say about it
    and is exactly what the zero it would otherwise show means.
    """
    if not isinstance(block, Mapping):
        return None
    sal = block.get("salience")
    sal = sal if isinstance(sal, Mapping) else {}
    try:
        n_cited = int(sal.get("n_cited") or 0)
    except (TypeError, ValueError):
        n_cited = 0
    if _block_mass(block) > 0.0:
        return REGISTER_LIVE
    return REGISTER_CITED_NOTHING if n_cited <= 0 else REGISTER_SUB_FLOOR_ONLY


#: The absence test, IMPORTED rather than re-implemented (Amendment 4c).
#:
#: ``assembly_carry.block_is_absence`` — ARM 2's collection-denominator
#: truthmaker, the declared ``scope_tokens``, and the corpus-derived
#: leading-negation opener, in one definition. It has to be one definition:
#: the country read uses it to decide what to CROWN and this tier uses it to
#: decide what to CARRY, and two tiers reaching different verdicts about the
#: same sentence is how a regional read ends up disagreeing with the country
#: read it is quoting.
_is_absence_scoped = block_is_absence


#: The member assembly's carried block, its ordinal and WHY that one —
#: ``assembly_carry.carried_lead_block``, ALIASED rather than re-implemented.
#:
#: The rule was written here (Amendment 4a, 2026-09-07) and the world tier now
#: applies the same one to its country and thematic candidates (P3-B), so it
#: moved next to the absence test it already shared. The default ``mass_floor``
#: matches this tier's (:data:`ROLLUP_MASS_FLOOR_DEFAULT` is 0.0) and
#: ``assemble_region_rollup`` passes the env/option-resolved value explicitly,
#: so the knob behaves exactly as it did.
_lead_block = carried_lead_block


def _drop_count(asm: Mapping[str, Any]) -> int | None:
    counts = (asm.get("drops") or {}).get("counts") if isinstance(asm.get("drops"), Mapping) else None
    if not isinstance(counts, Mapping):
        return None
    try:
        return int(counts.get("shown_not_carried") or 0)
    except (TypeError, ValueError):
        return None


def _cited_mass(asm: Mapping[str, Any], block: Mapping[str, Any] | None) -> float | None:
    """The member's own lead-block cited mass, read off the block it carries.

    Read from the CARRIED block rather than recomputed here: the rollup's whole
    contract is that it re-derives nothing about the evidence, only about the
    membership.
    """
    if not isinstance(block, Mapping):
        return None
    if not isinstance(block.get("salience"), Mapping):
        return None
    return round(_block_mass(block), 4)


def build_region_rollup(
    *,
    region_id: str,
    region_name: str,
    member_ids: Sequence[str],
    member_names: Mapping[str, str] | None = None,
    heads: Sequence[Mapping[str, Any]],
    as_of: Any,
    horizon_hours: float | None = None,
    mass_floor: float | None = None,
) -> dict[str, Any]:
    """Build ``data.data.rollup`` (schema ``region_rollup.v1``), per D-1 §4.1.

    ``member_ids`` is the region frame's AUTHORITATIVE member roster (the
    deterministic tag-membership SQL, reused verbatim); ``heads`` is whatever
    the slice actually admitted. The rollup is the DIFF between those two, which
    is why it can name what it did not see: a member with no admitted head is in
    ``members_missing`` by construction rather than by an absence check somebody
    remembered to write.

    Unlike ``build_assembly`` this NEVER raises on an empty carry. A region whose
    members all went quiet is a real, reportable state — the honest rollup says
    ``0 of 7 members carried`` — whereas an assembly with zero blocks is a
    construction failure because a read with nothing quoted in it is a page with
    nothing on it. Different products, different empty semantics, stated rather
    than inherited.
    """
    names = dict(member_names or {})
    roster = [str(m) for m in member_ids]
    by_target: dict[str, Mapping[str, Any]] = {}
    for row in heads:
        tid = str(row.get("target_id") or "")
        if tid and tid not in by_target:
            by_target[tid] = row

    # Resolved ONCE per rollup rather than per member: a floor that changed
    # mid-walk would make the members of one region incomparable to each other,
    # which is the one comparison this tier exists to publish.
    floor = rollup_mass_floor() if mass_floor is None else float(mass_floor)

    members: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    leads: list[Mapping[str, Any]] = []
    missing: list[str] = []
    produced: list[str] = []

    # Iterate the ROSTER, not the heads: the roster is the denominator, and
    # walking it is what makes an absent member a row rather than a silence.
    # A head whose target is not on the roster (a desk retagged mid-window) is
    # appended after, so honest data is never discarded — the same pass-through
    # the world slice does for a deregistered region frame.
    roster_set = set(roster)
    extra = [t for t in sorted(by_target) if t not in roster_set]
    for target_id in list(roster) + extra:
        row = by_target.get(target_id)
        name = names.get(target_id) or target_id
        asm = _assembly_of(row) if row is not None else None
        block, ordinal, carry_reason = (
            _lead_block(asm, mass_floor=floor)
            if asm is not None else (None, None, None)
        )
        # P3-A: THE ROLLUP CARRIES A LEAD, so the member's context span is cut
        # HERE, at the carry, rather than filtered at each reader. A country
        # assembly's block now also holds its origin desk head in FULL
        # (``context_body``); copying that into ``leads[]`` would put five to
        # eight whole desk reads into a row that renders none of them and is
        # defined as a byte-identical carry of one sentence each. Every other
        # field of the block is carried untouched, so the payload invariant
        # ``leads[i]`` pairs with ``members[i]`` is undisturbed.
        if block is not None and len(block.get("spans") or []) != len(
            _quoted_spans(block)
        ):
            block = dict(block, spans=list(_quoted_spans(block)))
        age = head_age_hours(row) if row is not None else None
        stale = (
            horizon_hours is not None
            and age is not None
            and float(age) > float(horizon_hours)
        )
        entry: dict[str, Any] = {
            "target_id": target_id,
            "target_name": name,
            "has_head": row is not None,
            "on_roster": target_id in roster_set,
            "assembly_id": str(row.get("id")) if row is not None else None,
            "lead_block_ordinal": ordinal,
            "lead_source": LEAD_CARRIED if block is not None else LEAD_NONE,
            # `None` — never a reason string — when nothing was carried: a
            # member with no assembly did not "fall back", it was absent, and a
            # reason there would be a sentence about a choice nobody made.
            "carry_reason": carry_reason if block is not None else None,
            # A LABEL, never a filter and never a number — see :data:`REGISTERS`.
            "register": member_register(block),
            "severity": (str(row.get("severity") or "") or None) if row is not None else None,
            "cited_mass": _cited_mass(asm, block) if asm is not None else None,
            "produced_at": _iso(row.get("produced_at")) if row is not None else None,
            "drop_count": _drop_count(asm) if asm is not None else None,
            "evidence_age_h": None if age is None else round(float(age), 2),
        }
        members.append(entry)

        if block is not None:
            leads.append(block)
            status = MEMBER_STALE if stale else MEMBER_IN_BASIS
            if entry["produced_at"]:
                produced.append(str(entry["produced_at"]))
        else:
            status = MEMBER_NO_ASSEMBLY
            if entry["on_roster"]:
                missing.append(target_id)
        coverage.append({
            "target_id": target_id,
            "target_name": name,
            "status": status,
            "has_head": entry["has_head"],
            # Why there is no lead, in the vocabulary of the thing that is
            # missing — a head, or an assembly inside a head that exists.
            "why": (
                None if block is not None
                else ("no_head_in_horizon" if row is None else "legacy_regime_head")
            ),
            "age_h": entry["evidence_age_h"],
        })

    severity = None
    best = -1
    for m in members:
        if m["lead_source"] != LEAD_CARRIED:
            continue
        r = _severity_rank(m.get("severity"))
        if r > best:
            best, severity = r, (str(m.get("severity") or "").strip().lower() or None)

    on_roster = [m for m in members if m["on_roster"]]
    return {
        "schema": ROLLUP_SCHEMA,
        "regime": REGIME_ROLLUP,
        "tier": ROLLUP_TIER,
        "as_of": _iso(as_of),
        "region_id": str(region_id),
        "region_name": str(region_name or region_id),
        "members": members,
        "member_count": len(on_roster),
        "members_with_head": sum(1 for m in on_roster if m["has_head"]),
        # ON-ROSTER only, deliberately. A head whose target left the roster
        # mid-window is still CARRIED (honest data is never discarded) but it
        # must not appear in the numerator of a fraction whose denominator is
        # the roster — otherwise the body reads "7 of 6 members carried" and
        # `rollup_confidence` returns a probability above 1.0. The off-roster
        # carries are counted separately so they are visible rather than folded.
        "members_carried": sum(
            1 for m in on_roster if m["lead_source"] == LEAD_CARRIED
        ),
        "extra_carried": sum(
            1 for m in members
            if not m["on_roster"] and m["lead_source"] == LEAD_CARRIED
        ),
        "members_missing": missing,
        "leads": [dict(b) for b in leads],
        "severity": severity,
        # min/max over the CARRIED members' origins. `None` on both when nothing
        # was carried — an empty window is stated as empty, never as a zero-width
        # instant at the current clock, which would read as "composed just now".
        "evidence_window": {
            "earliest": min(produced) if produced else None,
            "latest": max(produced) if produced else None,
        },
        "coverage": coverage,
        "connectives": {"vocabulary_version": ROLLUP_SCHEMA},
    }


def rollup_severity(payload: Mapping[str, Any]) -> str | None:
    """The row's severity — the MAX over CARRIED members. ``None`` when nothing
    was carried or no carried member declares one."""
    sev = payload.get("severity")
    return str(sev) if sev else None


#: W-2a — why the composition CORRELATION GUARD declines to cap a rollup's
#: confidence. Stamped into ``data.correlation_guard.confidence_cap_skipped`` so
#: the absence of a cap is a published fact and not a silence somebody has to
#: infer. See :func:`rollup_confidence` for the live numbers that forced it.
ROLLUP_CONFIDENCE_NOT_EVIDENCE: str = (
    "rollup_confidence is the carried FRACTION OF THE ROSTER (a completeness "
    "probability), not an evidence belief; the de-duplicated evidence ceiling "
    "is published beside it and does not bound it"
)


def rollup_confidence(payload: Mapping[str, Any]) -> float:
    """The rollup's ``confidence`` — the CARRIED FRACTION of the roster.

    Deliberately not a synthesised belief and not a flat 1.0. This tier asserts
    exactly one thing — "these are the region's member reads" — and the honest
    probability of that statement being COMPLETE is the fraction of the roster it
    could carry. A region that carried 4 of 7 says 0.571 and a reader can act on
    it; a flat 1.0 over a half-empty region is the quiet overclaim the whole
    demotion exists to stop. An empty roster is 0.0, not a division by zero
    dressed as certainty.

    W-2a (2026-09-06) — AND IT IS NOT AN EVIDENCE BELIEF, which is why
    :data:`ROLLUP_CONFIDENCE_NOT_EVIDENCE` exists. The composition CORRELATION
    GUARD caps a row's confidence at the de-duplicated evidence ceiling (the max
    ``effective_confidence`` over independent components) and it ran on this row
    too, because the guard runs for every composition that carries citations.
    Live on 2026-09-06 it rewrote 1.0 to 0.50, 0.60 and 0.6667 on five region
    rows whose ``independent_components`` EQUALLED their member count — it found
    no shared lineage at all and capped anyway, because the strongest member's
    evidence score is simply lower than a full roster. A reader then saw
    "6 of 6 member country reads carried" in the body beside a confidence of
    0.5, and the number meant neither completeness nor evidence strength.

    A completeness fraction and an evidence ceiling are different quantities and
    neither bounds the other. The guard's audit still runs and is still stamped
    beside this number; it no longer overwrites it.
    """
    total = int(payload.get("member_count") or 0)
    if total <= 0:
        return 0.0
    return round(float(int(payload.get("members_carried") or 0)) / float(total), 4)


def rollup_tags(payload: Mapping[str, Any]) -> list[str]:
    """The row's ``tags`` — and how ``severity`` reaches its READ COLUMN.

    Same mechanism as ``assembly_tags``: severity travels as a
    ``severity:<level>`` TAG the write path lifts into
    ``analyst_outputs.severity``. Region rows carry NULL severity on 100% of
    today's population (0 of 145 in 14 days), so this is the first time a region
    row can light the severity dot — and the value is the max over the members
    it actually CARRIED, never over members it merely knows exist.

    ``incomplete_roster`` is the cheap SQL handle on the one failure this tier
    can have: a region reported without all of its members. It is a tag rather
    than a buried count because "which regions were incomplete this week" should
    not require a JSONB path.
    """
    tags = ["rollup", f"regime:{REGIME_ROLLUP}"]
    sev = rollup_severity(payload)
    if sev:
        tags.append(f"severity:{sev}")
    if payload.get("members_missing"):
        tags.append("incomplete_roster")
    return tags


def rollup_title(payload: Mapping[str, Any]) -> str:
    """The row's ``title`` — deterministic, and carrying no authored claim.

    ``analyst_outputs.title`` is NOT NULL and is the page ``<h1>`` (§F-9). The
    rollup's is a COUNT plus the region's own name, because a count is the only
    thing this tier knows. It never quotes a member's sentence into the headline:
    crowning one member as the region's story is precisely the interpretive act
    the tier just gave up, and doing it in the ``<h1>`` while the body carries a
    flat list would be the demotion shipping honesty everywhere except the
    headline.
    """
    name = str(payload.get("region_name") or payload.get("region_id") or "Region")
    as_of = str(payload.get("as_of") or "")[:10]
    carried = int(payload.get("members_carried") or 0)
    total = int(payload.get("member_count") or 0)
    reads = "member country read" if carried == 1 else "member country reads"
    prefix = f"{name}, {as_of}" if as_of else name
    return f"{prefix} — {carried} of {total} {reads} carried"[:MAX_TITLE_CHARS]


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _quoted(span: Mapping[str, Any]) -> str:
    """A carried span's text as it appears in the BODY.

    Byte-identical to ``spans[].text`` except for the child-marker defuse the
    assembly render documents — a span cut from a lower tier can carry that
    tier's ``[[ref:N]]``, which would collide with this row's ordinal space. The
    CANONICAL text stays ``leads[].spans[].text`` in the payload, which is
    byte-identical to the desk head's body and is what every quote check reads.
    """
    return _defuse_child_ref_markers(str(span.get("text") or ""))


def render_rollup_body(payload: Mapping[str, Any]) -> str:
    """The row's ``body``, rendered from ``payload``. Byte-stable.

    Ordinal markers are ``[[ref:N]]`` over the ROLLUP's own numbering (one per
    carried member, in roster order), so the existing CITE resolution mints
    exactly one citation per carried member and ``|leads| == |citations| ==
    |distinct markers|`` holds by construction. The carried block's ORIGINAL
    ordinal (its ordinal inside the member assembly) is printed in the
    attribution line, so a reader can walk back to the exact block without this
    tier having to preserve a numbering that was never global.
    """
    lines: list[str] = []
    members = [m for m in (payload.get("members") or []) if isinstance(m, Mapping)]
    carried = [m for m in members if m.get("lead_source") == LEAD_CARRIED]
    # THE PAYLOAD INVARIANT this render rests on: ``leads[i]`` is the block
    # carried for the i-th CARRIED member, because ``build_region_rollup``
    # appends to both in one walk of the roster. Pairing them by index rather
    # than by an id is deliberate — the member's ``assembly_id`` is the COUNTRY
    # ROW's id while the block's ``finding_id`` is the DESK HEAD's, and joining
    # on either would silently render an empty block the day one of them is
    # absent. ``test_leads_pair_with_carried_members_by_position`` pins it.
    leads = [b for b in (payload.get("leads") or []) if isinstance(b, Mapping)]
    total = int(payload.get("member_count") or 0)
    n = int(payload.get("members_carried") or 0)
    extra = int(payload.get("extra_carried") or 0)
    reads = "member country read" if n == 1 else "member country reads"
    lines.append(
        f"*As of {str(payload.get('as_of') or '')[:19]}; rolled up from {n} "
        f"{reads} of the region's {total} members"
        # An off-roster carry is stated, never folded into the fraction: "7 of
        # 6" is not a coverage statement, it is a bug report about a tag.
        + (
            f", plus {extra} carried from a desk no longer tagged into this "
            "region" if extra else ""
        )
        + ".*"
    )
    lines.append("")
    lines.append(
        "This is a deterministic rollup: no sentence below was written for it."
    )
    lines.extend(["", "## Member reads", ""])

    for ordinal, (m, block) in enumerate(zip(carried, leads), start=1):
        subject = m.get("target_name") or m.get("target_id")
        lines.append(f"### {ordinal} — {subject}")
        # P3-A: QUOTED spans only. The member block is a COUNTRY block and now
        # carries its origin desk head in full under ``context_body``; the
        # rollup's contract is a BYTE-IDENTICAL carry of the member's LEAD, so
        # rendering the context here would turn a one-sentence carry into a
        # whole desk read. Identity on every pre-P3 member payload.
        for span in _quoted_spans(block):
            for para in _quoted(span).split("\n"):
                lines.append(f"> {para}" if para.strip() else ">")
        lines.append("")
        lines.append(
            " · ".join([
                f"[[ref:{ordinal}]]",
                f"carried from {block.get('desk') or 'unattributed desk'}",
                str(m.get("produced_at") or "")[:19],
                f"severity {m.get('severity') or 'unstated'}",
                f"cited mass {_fmt(m.get('cited_mass'))}",
                # The REGISTER, beside the number it disambiguates rather than
                # in a legend somewhere else: a `cited mass 0.00` alone cannot
                # tell a reader whether the desk cited nothing or cited only
                # things below the salience floor, and those are different
                # findings. `register unstated` never appears on a carried
                # member — `member_register` is total over a carried block —
                # but the fallback is spelled rather than crashing a render.
                f"register {_REGISTER_WORDS.get(str(m.get('register') or ''), 'unstated')}",
            ])
        )
        lines.append("")

    missing = [m for m in members if m.get("lead_source") != LEAD_CARRIED]
    lines.extend(["## Members not carried", ""])
    if missing:
        for m in missing:
            why = (
                "no read inside the horizon" if not m.get("has_head")
                else "no assembled read this cycle"
            )
            lines.append(f"- {m.get('target_name') or m.get('target_id')}: {why}.")
    else:
        lines.append("- every member of this region carried a read.")
    lines.append("")

    lines.extend(["## Coverage", ""])
    for c in payload.get("coverage") or []:
        status = {
            MEMBER_IN_BASIS: "in basis",
            MEMBER_NO_ASSEMBLY: "no assembly",
            MEMBER_STALE: "stale",
        }.get(str(c.get("status") or ""), str(c.get("status") or ""))
        age = c.get("age_h")
        suffix = f", {_fmt(age, 1)}h old" if age is not None else ""
        lines.append(f"- {c.get('target_name') or c.get('target_id')}: {status}{suffix}")
    lines.append("")
    lines.append(f"<!-- connectives: {ROLLUP_SCHEMA} -->")
    return "\n".join(lines).rstrip() + "\n"


def rollup_ordinal_index(
    payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> dict[int, Mapping[str, Any]]:
    """``{[[ref:N]] -> the slice row that section N quotes}``.

    THE ONE ORDERING, published for the CITE step. ``render_rollup_body`` numbers
    its member sections 1..n over the CARRIED members **in payload order** (the
    region's roster order), and this returns the same walk — so ordinal N names
    section N's OWN head and nothing else.

    WHY THIS EXISTS AT ALL. The generic composition CITE step resolves ``N`` to
    ``sliced[N-1]``, which is correct for every tier that renders its blocks in
    slice order (``build_assembly`` mints ``blocks[i].ordinal = i+1`` straight
    off ``carried = sliced[:BLOCK_CAP]``, so the two orders are the same object).
    A rollup is the ONE producer that does not: its sections follow the ROSTER
    (the denominator it is reporting against), while the slice arrives
    salience-ordered. Two orderings over one ordinal space is a label pointing at
    the wrong country — 2026-09-05..07 shipped exactly that — so the ordering the
    reader SEES is the one that is published here, and the slice order stops
    being an ordinal space at all on this path.

    Keyed through ``members[].assembly_id``, which ``build_region_rollup`` copies
    verbatim off ``row["id"]``, so the row handed back at ordinal N is the same
    object the section was rendered from. A member whose row cannot be recovered
    yields NO entry rather than a neighbour's row: the caller drops that marker
    and counts it, which is the house rule — never a fabricated ref.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        rid = str(row.get("id") or "")
        if rid and rid not in by_id:
            by_id[rid] = row
    index: dict[int, Mapping[str, Any]] = {}
    ordinal = 0
    for member in payload.get("members") or []:
        if not isinstance(member, Mapping):
            continue
        if member.get("lead_source") != LEAD_CARRIED:
            continue
        ordinal += 1
        row = by_id.get(str(member.get("assembly_id") or ""))
        if row is not None:
            index[ordinal] = row
    return index

def rollup_structural_claims(
    payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The C2b ``data.structural_claims`` block — the rollup's REAL verify.

    A rollup has no prose, so the faithfulness judge has nothing to grade and
    none is written. What IS checkable is the arithmetic, and every number the
    row states is re-derivable from a constituent set the row also records —
    which is exactly the contract ``structural_claims.verify_structural_claims``
    was built for. So the region tier does not lose verification when it loses
    the judge; it swaps a graded opinion for a re-derivation that cannot be
    wrong about whether it is wrong.

    Four claims, each over a set the payload itself carries:

      * ``member_count``     — the roster size vs the member entries on it
      * ``members_carried``  — the carried count vs the leads actually carried
      * ``members_missing``  — the missing list vs the roster members with no lead
      * ``derived_from``     — the member HEADS admitted, re-derived against the
        row's ACTUAL ``derived_from`` by the sentinel

    Every claim's basis is a population the payload itself carries, and each one
    is the SAME population as the field it checks. A claim that re-derives
    against a different set than the number it verifies is not a check, it is a
    second bug.
    """
    members = [m for m in (payload.get("members") or []) if isinstance(m, Mapping)]
    on_roster = [m for m in members if m.get("on_roster")]
    # ON-ROSTER carries, matching ``members_carried``'s own definition — a claim
    # that re-derives against a different population than the field it checks is
    # a miscount detector that fires on its own arithmetic.
    carried = [m for m in on_roster if m.get("lead_source") == LEAD_CARRIED]
    with_head = [m for m in members if m.get("has_head")]
    region = str(payload.get("region_id") or "region")
    return [
        {
            "id": f"{region}:member_count",
            "statement": (
                f"{len(on_roster)} member desks on this region's roster"
            ),
            "op": "count",
            "asserted": int(payload.get("member_count") or 0),
            "basis": [str(m.get("target_id") or "") for m in on_roster],
        },
        {
            "id": f"{region}:members_carried",
            "statement": (
                f"{len(carried)} member reads carried into this rollup"
            ),
            "op": "count",
            "asserted": int(payload.get("members_carried") or 0),
            "basis": [str(m.get("assembly_id") or "") for m in carried],
        },
        {
            "id": f"{region}:members_missing",
            "statement": (
                f"{len(payload.get('members_missing') or [])} roster members "
                "contributed no assembled read"
            ),
            "op": "count",
            "asserted": len(list(payload.get("members_missing") or [])),
            "basis": [
                str(m.get("target_id") or "")
                for m in on_roster
                if m.get("lead_source") != LEAD_CARRIED
            ],
        },
        {
            "id": f"{region}:derived_from",
            "statement": (
                f"{len(with_head)} member country heads in the lineage"
            ),
            "op": "count",
            # ``derived_from`` is every head the SLICE admitted, not every head
            # the rollup CARRIED — a legacy-regime member is in the lineage and
            # contributes no lead. Asserting the carried count here would fire a
            # `structural_miscount` on a perfectly honest rollup the first time
            # one member lagged the cutover, which is a detector that cries
            # wolf about the rollout it exists to watch.
            "asserted": len(with_head),
            # The sentinel re-derives against the row's REAL derived_from, so
            # this is a check on the LINEAGE, not on a number typed twice.
            "basis": "@derived_from",
        },
    ]


def rollup_finding_json(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The ``_coerce_finding`` input for a rollup row — same shape the assembly
    path hands it, so one coercion path covers both regimes."""
    return {
        "title": rollup_title(payload),
        "body": render_rollup_body(payload),
        "confidence": rollup_confidence(payload),
        "tags": rollup_tags(payload),
    }


def rollup_step(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The ``intermediate_steps`` receipt for the rollup phase."""
    return {
        "phase": "rollup",
        "kind": ROLLUP_SCHEMA,
        "region_id": payload.get("region_id"),
        "members": int(payload.get("member_count") or 0),
        "carried": int(payload.get("members_carried") or 0),
        "missing": len(list(payload.get("members_missing") or [])),
        "severity": rollup_severity(payload),
    }


def rollup_inputs(
    rows: Sequence[Mapping[str, Any]], *, region_id: Any, horizon_hours: Any = None
) -> dict[str, Any]:
    """Assemble :func:`build_region_rollup`'s keyword inputs from a slice.

    The member ROSTER arrives denormalised on the rows
    (``composition_slice.REGION_MEMBERSHIP_ROW_KEY``) because the DB-less
    ``_run`` cannot read it — and the roster is the one input the rollup cannot
    infer from what arrived, since its whole job is naming what did not. A slice
    with no membership stamp (a direct caller, a legacy row) degrades to the
    roster the rows themselves imply: honest, and visibly incomplete, rather
    than a crash.
    """
    membership: Mapping[str, Any] = {}
    for row in rows:
        stamp = row.get(REGION_MEMBERSHIP_ROW_KEY)
        if isinstance(stamp, Mapping):
            membership = stamp
            break
    members = [m for m in (membership.get("members") or []) if isinstance(m, Mapping)]
    if members:
        member_ids = [str(m.get("target_id") or "") for m in members]
        names = {
            str(m.get("target_id") or ""): str(m.get("target_name") or "")
            for m in members
        }
    else:
        member_ids = sorted({str(r.get("target_id") or "") for r in rows if r.get("target_id")})
        names = {}
    try:
        horizon = float(horizon_hours) if horizon_hours else None
    except (TypeError, ValueError):
        horizon = None
    return {
        "region_id": str(membership.get("region_id") or region_id or ""),
        "region_name": str(membership.get("region_name") or region_id or ""),
        "member_ids": member_ids,
        "member_names": names,
        "heads": list(rows),
        "horizon_hours": horizon,
    }


def assemble_region_rollup(
    rows: Sequence[Mapping[str, Any]],
    *,
    region_id: Any,
    horizon_hours: Any,
    as_of: Any,
    coerce: Any,
    contributing_analysts: Sequence[str],
    mass_floor: float | None = None,
) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    """The whole ROLLUP arm of ``_run``, in one call.

    Returns ``(payload, finding, steps)``. ``coerce`` is the synthesizer's
    ``_coerce_finding`` injected as a parameter — the same shape and for the
    same reason as ``composition_slice``'s ``basis_reader``: the coercion is a
    parameter of the arm, not a fact about it, and taking it here keeps this
    module free of an import back into the host.
    """
    payload = build_region_rollup(
        as_of=as_of,
        mass_floor=mass_floor,
        **rollup_inputs(rows, region_id=region_id, horizon_hours=horizon_hours),
    )
    import json as _json

    finding = coerce(
        _json.dumps(rollup_finding_json(payload)),
        fallback_title="Region rollup",
        contributing_analysts=list(contributing_analysts),
    )
    # ``_coerce_finding`` stamps ``raw_llm_response`` — "the LLM's raw JSON for
    # audit". NO LLM RAN. A field whose NAME is false is the small dishonesty
    # this program is about, so it is dropped rather than filled; the rollup is
    # reproducible from ``data.rollup`` by construction, which is strictly more
    # than a raw response ever gave.
    finding.data.pop("raw_llm_response", None)
    # THE REPLACEMENT VERIFY (see the module banner). A graded opinion swapped
    # for a re-derivation, not a verification hole.
    finding.data[STRUCTURAL_CLAIMS_DATA_KEY] = rollup_structural_claims(payload)
    steps = [
        rollup_step(payload),
        {
            "phase": "reflect",
            "kind": "coerce_finding",
            "confidence": finding.confidence,
            "evidence_count": len(finding.evidence),
            "structured": "unstructured" not in finding.tags,
        },
    ]
    return payload, finding, steps


def rollup_assembly_stamp() -> dict[str, Any]:
    """The ``data.assembly`` block a ROLLUP row carries.

    §5.2 mandates ``data.data.assembly.regime`` on EVERY composition row so the
    A/B boundary is never invisible inside one judge stamp. A rollup is a third
    arm, so it stamps the third value and carries no blocks — the payload lives
    at ``data.data.rollup``, and a reader that finds ``regime == "rollup"`` here
    knows exactly where to look and that a quote-fidelity number would be a
    category error.
    """
    return {
        "schema": ASSEMBLY_SCHEMA,
        "regime": REGIME_ROLLUP,
        "tier": ROLLUP_TIER,
    }


def carried_lead_ids(payload: Mapping[str, Any]) -> list[str]:
    """The member assembly ids whose lead blocks this rollup carries."""
    return [
        str(m.get("assembly_id") or "")
        for m in (payload.get("members") or [])
        if isinstance(m, Mapping) and m.get("lead_source") == LEAD_CARRIED
    ]


def iter_carried_spans(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Every span the rollup carries, for a byte-fidelity replay."""
    for block in payload.get("leads") or []:
        if not isinstance(block, Mapping):
            continue
        # P3-A: the fidelity replay walks what the rollup CARRIES, which is the
        # member's lead. A context span is byte-identical by construction, so
        # including it would only pad the denominator with guaranteed passes —
        # a dilution, which on a fidelity number is indistinguishable from a
        # fake pass. Identity on every pre-P3 member payload.
        yield from _quoted_spans(block)
