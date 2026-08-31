# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scorecard_banding`` — P4-T1 deterministic banded-verdict rule engine.

The HONEST top of the system. A pure, read-only banding function over a
single country target: it gathers that country's ALREADY-VERIFIED claims (the
four P2 bounded-reasoning UNIT findings + the P3 ``country_composition``) and,
for each of the four fixed DIMENSIONS, emits a band from a FEW high-precision
rules over the finding's ``severity:<level>`` tag and its folded
``effective_confidence = min(confidence, faithfulness_score)`` — where each band
NAMES the ``>=1`` verified-claim id(s) it was derived from.

Honesty is the whole point. A dimension with NO qualifying verified claim (the
unit did not fire, or its finding never passed a faithfulness verify, or it is
below the confidence floor, or it carries no severity tag) returns
``band = "insufficient-evidence"`` with an EMPTY-but-explicit basis and a
machine ``reason`` — NEVER a fabricated band, never a hand-weighted number over
raw prose, never a synthesized basis id. Basis ids are ONLY ever real
``analyst_outputs.id`` rows returned by the gather query.

FRAME-3 — SEVERITY AS STATE (``planning/FRAME_PROGRAM_2026-08-20.md`` §0.6, §7
train 3). R4 has always mapped the severity tag; what changed is what that tag
MEANS. A desk used to tag the severity of its SLICE DELTA, so a war in its
fourth month tagged ``severity:low`` on a week that added nothing to it and
this engine banded the dimension ``low`` off that tag — CORRECTNESS-R1 measured
37/37 non-exact bands sitting BELOW the reference, and named it the C-B driver.
The unit contract now splits the tag: ``severity`` is the STANDING level of the
dimension and the slice movement rides a separate
``severity_delta:<rose|fell|steady|new>``.

Three consequences, all of them deliberate:

  * **R4 bands the STANDING level.** No rule changed here — the tag it reads
    simply now answers the question the band was always claiming to answer.
  * **THE DELTA NEVER TOUCHES THE BAND.** It is reported on the verdict beside
    the band, never folded into it. A band is a statement about a CONDITION;
    letting movement move it would re-import the defect from the other side
    (a war "steady" for a fortnight would decay a rung a fortnight).
  * **DAMPING WAS UNTOUCHED** — and H3 below reversed exactly that decision.

An ABSENT delta is first-class and expected — every head written before the
prompt flip reached its desk has none — and reads ``None``, never ``steady``.
:data:`BANDING_SEMANTICS` stamps which contract a verdict was computed under so
a before/after band diff is a machine comparison rather than a guess about when
the desks flipped.

H3 (1) — THE DAMPER IS RETIRED FROM THE BAND PATH
-------------------------------------------------
CORRECTNESS-R1 measured the one-rung confidence demote as costing ~1
discordance and concluded "leave damping alone". CORRECTNESS-R2 measured it
again after FRAME-3 landed and found **the opposite sign**: 22 of 49 banded
dimensions carried ``damped: true``, 22 of 22 landed exactly one rung below
``SEVERITY_TO_BAND[severity_tag]``, and **12 lost a real rung** (the other 10
were already clamped at ``low``). It was net-negative in SIX independent lanes.
Its two decisive cases name the mechanism better than any argument: CD
``internal_stability`` carried ``severity:moderate`` **and**
``severity_delta:rose`` and shipped ``low``; IR ``escalation`` was the only
dimension in its read carrying ``severity_delta:rose``, was the only one damped,
and shipped ``watch`` in month six of a shooting war.

**Why the sign flipped.** The damper is a uniform subtraction that keys on
effective confidence and nothing else. Before FRAME-3 it demoted a DELTA — a
weakly-evidenced claim about a week's movement, where reading one rung low is a
defensible hedge. After FRAME-3 the pre-damping severity is the dimension's
STANDING LEVEL, so the identical subtraction now demotes a STATE: it says "we
are only 55% sure this desk read the war correctly, so call the war one rung
smaller". That is not a hedge, it is a wrong answer with a smaller number, and
it moved bands systematically AWAY from evidence: recomputing the round's band
comparisons against the graders' committed ``ref_bands`` gives **41 of the 49
scored slots BELOW the reference and 2 above** — both over-reads on the AR desk,
which the AR-B grader's own C-B rows label ``over-read`` (SCORES.md's summary
sentence says 43/43 below and 0 above; the discrepancy is in that sentence, not
in the graders' rows, and it does not move the direction). The asymmetry is the
finding either way, and the damper is one of its mechanisms.

The demote is therefore REMOVED from the band path. Low effective confidence is
still a fact about the claim and is still reported — the numbers that drove the
damper (``effective_confidence``, ``confidence``, ``critic_score``) were always
on the verdict, and R4 now additionally records
:attr:`DimensionVerdict.damped_would_have_been` — the band the retired damper
WOULD have shipped — so the change is observable per-row rather than inferable.
``damped`` survives as a field and is now ALWAYS ``False``: consumers keyed on
it (the v3 UI's ``DimensionBand``) keep their shape and correctly read "this
band was not damped", because none are.

An uncertainty that cannot be resolved by lowering the band is resolved by
SAYING SO. The honest instrument for a claim too weak to band is the
insufficient-evidence path (R1/R1b/R2), which already exists, already names a
reason, and does not hand the reader a confident-looking smaller number.
:data:`DAMPING_SEMANTICS` stamps which contract a card was computed under, for
the same reason :data:`BANDING_SEMANTICS` does: "``watch``" means two different
things either side of this change and a band diff must not have to guess.

Reversibility: the rule is one branch and :func:`demote_one` is retained (it
computes ``damped_would_have_been``), so restoring the damper is a one-line
revert against a live measurement rather than a rebuild.

H3 (2) — BASIS ALIGNMENT: THE CARD BANDS WHAT THE COMPOSITION CONSUMED
----------------------------------------------------------------------
R2's third-largest failure mechanism was **basis divergence**, and its number
was not a sample: **21 of 21** product-``insufficient-evidence`` dimension slots
in the round sat beside a composition that had CONSUMED a verified head for that
same desk, and **20 of the 21** had a head that already cleared the
composition's own 0.50 admission bar (BF ``energy_security`` at eff **0.90**).
The reader got BF's prose saying *"All seven desks produced verified reads"*
beside a card abstaining on six of seven.

**The divergence site, exactly.** Both artefacts gather the same rows and apply
the same floors — in the OPPOSITE ORDER:

  * the composition
    (:func:`legba.data.analysts.meta_findings_synthesizer.read_other_analyst_findings`)
    puts the floor in the ``WHERE`` and the head-fold in the ``DISTINCT ON``, so
    it selects **the freshest head that CLEARS the floor**; FRAME-1's
    :func:`legba.data.analysts.composition_window.read_floor_fallback_heads`
    then explicitly routes that older passing head into BASIS while the newer
    failing head stays in periphery, DATED;
  * this engine's ``_GATHER_SQL`` puts the head-fold first and the floors after,
    in Python, so it selects **the freshest head, full stop** — and then abstains
    when that one row fails, with a passing head sitting one cycle behind it,
    unread.

Freshest-then-floor versus floor-then-freshest. That is the whole bug, and it is
why the divergence is "grain, not freshness": nothing is stale, the two
artefacts simply admit different rows from the same table.

**The fix wires the CONSUMED BASIS through rather than re-deriving it.** The
card already names the composition head it prints beside; that row's
``derived_from`` IS the set of heads the composition actually consumed. When a
dimension would otherwise abstain, the engine walks that dimension's consumed
heads NEWEST-FIRST through the unchanged R0-R4 guards and bands the first one
that qualifies — the same row the prose is resting on, so the two artefacts can
no longer contradict each other about whether a desk read the dimension.

**Relation to B0-5.** :mod:`legba.data.registry.scorecard_reconcile` already
DETECTS this divergence at READ time, reconciling a stored card against the
CURRENT composition head and emitting ``disagreements`` rows. H3 does not replace
it and does not make it redundant: B0-5 reports a contradiction to whoever opens
the panel, H3 stops the card from writing one. What B0-5 will keep finding after
H3 is the residue H3 cannot reach from inside the card — the ``banded-unconsumed``
direction, the ``consumed-unbandable`` rows, and above all the SCHEDULING RACE
(R2 measured 30 of 31 targets composing after their card froze), because a card
can only align to the composition that existed when it ran.

Nothing is fabricated and no guard is relaxed: a consumed head is banded only if
it passes every rule an ordinary claim passes. When NO consumed head can be
banded the dimension still reads ``insufficient-evidence`` — but it may never do
so silently. Every dimension now carries a ``basis_alignment`` block naming a
state from :data:`BASIS_ALIGNMENT_STATES`, the consumed ids, and the reason the
consumed head could not be banded. The six states are exhaustive and cover BOTH
directions of divergence, including the one that does not move a band:
``banded-unconsumed`` fires when the card bands a row the composition never
used (R2 found that too — BF ``economic_coercion`` banded at eff 0.40, under the
composition's floor), so the disagreement is visible instead of implicit.

Design constraints (why keying/severity are what they are):

  * **Dimension key is the unit ``analyst_id``, NOT the LLM topic tag.** The
    ``escalation`` unit emits a BARE ``"escalation"`` topic tag while the other
    three emit ``"topic:<unit>"`` — the topic tags are inconsistent, so keying
    off them would silently mis-bucket escalation. ``analyst_id`` is set by the
    runtime and is deterministic.
  * **Severity comes from the ``severity:<level>`` tag in ``data -> 'tags'``**,
    NOT the ``analyst_outputs.severity`` column, which is ``NULL`` for findings
    (``FindingPayload`` has no severity field; the write path ``getattr``s it to
    ``None``). The tag is consistent across all four units.
  * **``effective_confidence`` folds the FAITHFULNESS-verify critique**
    (``title LIKE 'Faithfulness verify%'``), mirroring
    :func:`legba.runtime.substrate_query_port.list_findings` (~L858-891) and the
    composition verify-gate in
    :mod:`legba.data.analysts.meta_findings_synthesizer` (~L675-696). When no
    such critique was ever folded (verify never ran / failed) the effective
    confidence is ``None`` and the dimension is ``insufficient-evidence``
    (``reason='verify-failed'``) — the verified floor, not the raw confidence.

The engine splits so the rules are unit-testable with NO database:

  * :func:`_band_claim` — pure ``Claim | None -> DimensionVerdict``: the R0-R4
    rule table over ONE claim, and the ONLY place a claim is judged. H3's
    alignment runs the same function over a second candidate, so the consumed
    path cannot drift into a softer path even by accident.
  * :func:`band_dimension` — ``_band_claim`` plus the basis-alignment layer.
  * :func:`band_target` — pure ``(claims_by_dim, composition) -> verdict dict``.
  * :func:`gather_and_band` — the async run-entry that executes the verify-folded
    gather SQL, resolves the composition's consumed basis, and then calls
    :func:`band_target` (the acceptance entry). Its optional ``as_of`` pin makes
    a past card REPRODUCIBLE — the same gather against the substrate as it stood
    at an instant — which is what turns "we changed the damper" into a measured
    band diff. ``as_of=None`` is the production path and runs the byte-identical
    legacy SQL.

This is T1 ONLY: the pure rules + a run-entry. Wrapping this in a producer
analyst and a ``scorecard`` OutputKind is T2 and out of scope here. Banding is
idempotent and read-only over already-verified claims — it NEVER mutates or
re-verifies a finding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from ...provenance.models import severity_delta_from_tags

# ---------------------------------------------------------------------------
# Constants — the whole rule table.
# ---------------------------------------------------------------------------

#: The fixed dimensions = the bounded-reasoning unit ANALYST_IDs (runtime-set,
#: deterministic). Ordered; the verdict reports every one, always. internal_stability
#: (S1-T4) and military_posture (S1-T5) join the original four P2 units, and
#: economic_coercion (S1-T7, the 7th PMESII dimension: sanctions/trade/currency)
#: joins them — all BROAD units (blanket g20+watch predicate, every desk fires
#: them) so a FIXED dimension is safe. Keep in sync with
#: country_composition.other_analysts,
#: substrate_query_port._ASSESSMENT_PRODUCER_ANALYSTS, and
#: unit_correctness_scorer._DEFAULT_UNITS.
DIMENSIONS: tuple[str, ...] = (
    "leadership_transition",
    "energy_security",
    "escalation",
    "narrative_coordination",
    "internal_stability",
    "military_posture",
    "economic_coercion",
)

#: The P3 per-country aggregate. Surfaced as its OWN node (naming its basis id),
#: NEVER folded into a fabricated overall band.
COMPOSITION_ANALYST_ID: str = "country_composition"

#: A claim below this folded effective_confidence is NOT strong enough to band
#: (``reason='below-floor'``).
CONF_FLOOR: float = 0.35

#: The CONFIDENT threshold. At/above this folded effective_confidence a band is
#: reported ``qualified``; between :data:`CONF_FLOOR` and this it is reported
#: ``qualified-low-confidence`` — the SAME band, with the weakness named.
#:
#: H3: this constant used to be the damper's knee ("low-faithfulness reads
#: lower", one rung down). It no longer moves any band; it now only splits the
#: R4 ``reason`` and drives :attr:`DimensionVerdict.damped_would_have_been`, so
#: the retired subtraction stays measurable without being applied.
CONF_CONFIDENT: float = 0.60

#: P4-T5 — the DEDICATED faithfulness floor. A claim whose per-claim
#: faithfulness-verify score (``critic_score``) is below this is EXCLUDED from
#: the basis with a dedicated ``reason='low-faithfulness'`` (distinct from the
#: ``below-floor`` effective-confidence guard). ``effective_confidence =
#: min(confidence, faithfulness)`` already partially catches this, but the
#: dedicated higher faith-specific floor + reason makes the demotion LEGIBLE:
#: a high-confidence / mediocre-faithfulness claim (conf .9 / faith .45) now
#: reads ``low-faithfulness`` (the operator literally sees the band basis
#: exclude it), NOT ``below-floor``.
FAITH_FLOOR: float = 0.50

#: The band ladder, ascending. Demotion walks one step DOWN, clamped at "low".
BAND_LADDER: tuple[str, ...] = ("low", "watch", "elevated", "high", "critical")

#: The five valid ``severity:<level>`` levels mapped to their base band. Note
#: ``moderate`` severity maps to the ``watch`` band (there is no "moderate"
#: rung on the band ladder).
SEVERITY_TO_BAND: dict[str, str] = {
    "low": "low",
    "moderate": "watch",
    "elevated": "elevated",
    "high": "high",
    "critical": "critical",
}

#: Sentinel band for a dimension with no qualifying verified claim.
INSUFFICIENT: str = "insufficient-evidence"

#: FRAME-3 — WHICH severity contract a verdict was computed under, stamped on
#: every banded card. The engine's rules did not change; the MEANING of the tag
#: they read did, which is the kind of change that is invisible in a band value
#: and therefore has to be recorded beside it. ``delta`` means severity is the
#: dimension's slice movement (pre-FRAME-3); ``standing`` means it is the
#: dimension's standing state, with the movement in ``severity_delta``.
#:
#: A CONSTANT, not a computation: the semantics are a property of the prompt
#: contract the desks run under, and a card cannot detect from one row's tags
#: which contract wrote it. It moves when the flip lands, and the §7 gate — a
#: before/after band diff on one conflict desk and one quiet desk — is exactly
#: the check that it moved for the right reason.
BANDING_SEMANTICS: str = "standing"

#: H3 — WHICH DAMPING contract a verdict was computed under, stamped beside
#: :data:`BANDING_SEMANTICS` for exactly the same reason and read the same way.
#: ``demote`` means a band between the floor and :data:`CONF_CONFIDENT` was
#: shipped one rung DOWN (every card produced before this change); ``off`` means
#: the band is the severity tag's band and the low-confidence case is reported,
#: not subtracted. A ``watch`` under ``demote`` and a ``watch`` under ``off`` are
#: the same five characters meaning two different things, and a band diff across
#: the change must be able to tell them apart mechanically rather than by
#: knowing the deploy date.
DAMPING_SEMANTICS: str = "off"

# ---------------------------------------------------------------------------
# H3-GUARD — semantics-mismatch classification. ``alert_trigger_scan`` and
# ``band_calibration_tracker`` both compare a desk×dimension's CURRENT band
# against a PRIOR one; if the two cards were computed under different
# BANDING_SEMANTICS / DAMPING_SEMANTICS contracts, that comparison is a
# MEASUREMENT artifact, not a verified-state transition, and must never read
# as deterioration / evidence-gained / a resolvable calibration claim. The H3
# deploy is the reference case the guard exists for: the FIRST post-deploy
# sweep sees ``damping_semantics`` go from ABSENT (every pre-H3 card) to
# ``"off"`` on every card at once, legitimately moving ~30 bands fleet-wide
# for a reason that has nothing to do with the world.
# ---------------------------------------------------------------------------

#: The `direction` a desk×dimension band comparison reads when the prior and
#: current card were computed under different semantics — see
#: :func:`semantics_changed`. Pre-empts every other classification in
#: ``alert_trigger_scan.classify_band_transition``: never
#: deterioration/improvement/evidence-gained/evidence-lost, because the two
#: cards are not comparable on the ladder in the first place.
SEMANTICS_MIGRATION: str = "semantics-migration"

#: The alert severity a semantics-migration transition carries — informational
#: only, never `medium`/`high` (the "something in the world moved" severities),
#: because nothing in the world moved.
SEMANTICS_MIGRATION_SEVERITY: str = "low"


def bands_semantics(
    bands: Optional[Mapping[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    """``(banding_semantics, damping_semantics)`` off one card's ``bands``
    mapping — the SAME dict :func:`build_verdict` returns and the shape
    ``alert_trigger_scan`` / ``band_calibration_tracker`` already parse out of
    a scorecard row's ``data`` column. Malformed or missing reads as
    ``(None, None)`` — a card this cannot read is exactly as informative as
    one written before the stamps existed.
    """
    if not isinstance(bands, Mapping):
        return None, None
    banding = bands.get("banding_semantics")
    damping = bands.get("damping_semantics")
    return (
        str(banding) if banding is not None else None,
        str(damping) if damping is not None else None,
    )


def _stamp_changed(prev: Optional[str], curr: Optional[str]) -> bool:
    """One semantics stamp's before/after comparison.

    A stamp ABSENT on the PRIOR side reads as differing whenever the CURRENT
    side carries a value the prior card could not have had — a card written
    before the stamp existed meeting one written after IS the migration
    boundary (H3: ``damping_semantics`` is a NEW key, so every pre-H3 card
    lacks it while every post-H3 card carries ``"off"``). Both cards missing
    the SAME stamp compare as unchanged: two cards written before it existed
    carry no information that they differ, and reading ``None == None`` as a
    mismatch would reclassify every pre-stamp transition in history — and
    every fixture that never bothered stamping one — as a migration event,
    which is not what this guard is for.
    """
    if prev is None:
        return curr is not None
    return prev != curr


def semantics_changed(
    prev: tuple[Optional[str], Optional[str]],
    curr: tuple[Optional[str], Optional[str]],
) -> bool:
    """True when EITHER banding or damping semantics stamp differs between a
    prior and a current card's :func:`bands_semantics` pair. ``prev`` /
    ``curr`` are each ``(banding_semantics, damping_semantics)``; see
    :func:`_stamp_changed` for the absent-stamp rule.
    """
    return _stamp_changed(prev[0], curr[0]) or _stamp_changed(prev[1], curr[1])


# ---------------------------------------------------------------------------
# H3 — the basis-alignment state enum (the card vs the composition's basis).
# ---------------------------------------------------------------------------

#: The composition head consumed this dimension's banded row: the card and the
#: prose rest on the SAME evidence. The only fully-aligned state.
BASIS_ALIGNED: str = "aligned"

#: The card's own freshest head could not be banded, so the band was taken from
#: a head the composition CONSUMED — the divergence, repaired. The newer head
#: the card could not band rides along in ``newer_head``, dated, mirroring
#: FRAME-1's periphery rule (showing a dated passing read beside a dated failing
#: one is strictly more honest than showing neither).
BASIS_CONSUMED: str = "consumed-basis"

#: The composition consumed head(s) for this dimension and NOT ONE of them can
#: be banded. The dimension stays ``insufficient-evidence`` — but names the
#: consumed ids and the rule that rejected the best of them, so the abstention
#: is falsifiable instead of silent. This is the state that keeps "the scorecard
#: may not silently abstain beside a composition that read the desk" true even
#: when no band is honestly available.
BASIS_CONSUMED_UNBANDABLE: str = "consumed-unbandable"

#: The card banded a row the composition never consumed. NO band changes — this
#: state exists so the OTHER direction of R2's divergence is visible: a claim
#: above this engine's 0.35 floor but below the composition's 0.50 admission bar
#: bands here and is absent from the prose (BF ``economic_coercion``, eff 0.40).
BASIS_UNCONSUMED: str = "banded-unconsumed"

#: A composition is present but consumed NO head for this dimension — the desk
#: is genuinely absent from the prose too. The card and the composition agree.
BASIS_NOT_CONSUMED: str = "not-consumed"

#: No composition head to align to (``composition.present`` is False). There is
#: no second artefact on the page, so there is nothing to diverge from.
BASIS_NO_COMPOSITION: str = "no-composition"

#: Every alignment state, ordered. Exhaustive by construction: a dimension is
#: assigned exactly one and the assignment has no fallthrough.
BASIS_ALIGNMENT_STATES: tuple[str, ...] = (
    BASIS_ALIGNED,
    BASIS_CONSUMED,
    BASIS_CONSUMED_UNBANDABLE,
    BASIS_UNCONSUMED,
    BASIS_NOT_CONSUMED,
    BASIS_NO_COMPOSITION,
)

#: Coerce-fallback tags: a finding whose body could not be structured. Its
#: score is *vacuously* faithful, so it must be dropped by tag (mirrors the
#: composition gate drop in meta_findings_synthesizer ~L690).
_COERCE_TAGS: frozenset[str] = frozenset({"unstructured", "coerce_failed"})


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One gathered verified claim (a unit finding or the composition).

    ``faithfulness_score`` is the folded ``Faithfulness verify%`` critique score
    (``None`` when verify never ran for this finding). ``effective_confidence``
    is the honest floor ``min(confidence, faithfulness_score)`` — ``None`` the
    moment either input is missing, which is a first-class ``verify-failed``
    state, not a value to paper over.
    """

    finding_id: str
    analyst_id: str
    confidence: Optional[float]
    faithfulness_score: Optional[float]
    tags: tuple[str, ...] = ()
    produced_at: Optional[str] = None

    @property
    def effective_confidence(self) -> Optional[float]:
        if self.confidence is None or self.faithfulness_score is None:
            return None
        return min(self.confidence, self.faithfulness_score)

    @property
    def is_coerce_fallback(self) -> bool:
        return any(t in _COERCE_TAGS for t in self.tags)

    @property
    def severity_delta(self) -> Optional[str]:
        """FRAME-3 — the desk's own movement call, or ``None`` when unstamped.

        Read from the SAME tag list the standing severity comes from, through
        the one shared reader, so the two can never disagree about which tags a
        claim carries. ``None`` for every head written before the flip reached
        its desk — reported as absent, never defaulted to ``steady``.
        """
        return severity_delta_from_tags(self.tags)


@dataclass(frozen=True)
class DimensionVerdict:
    """A single dimension's band + the honest evidence it was derived from.

    For a qualifying claim: a real ``band`` naming ``basis=[finding_id]`` with
    the severity tag and the folded numbers. For anything else:
    ``band=INSUFFICIENT``, ``basis=[]``, all numeric fields ``None``, and a
    machine ``reason``.

    FRAME-3 adds ``severity_delta`` — the desk's movement call, carried BESIDE
    the band and never inside it. ``band`` answers "what is the state of this
    dimension"; ``severity_delta`` answers "what did this cycle do to it". They
    are separate questions and this is the row where a reader can finally see
    both: a ``high`` band with ``steady`` is a war that is still on, which used
    to be indistinguishable from a war nobody looked at.

    H3 adds two things beside the band, both of them RECORDS rather than rules:

      * ``damped`` is retained and is now ALWAYS ``False`` (the demote is
        retired), with ``damped_would_have_been`` naming the band the retired
        damper WOULD have shipped for this row — ``None`` whenever it would not
        have fired. A card is therefore self-describing about a change that is
        otherwise invisible in a band value.
      * the ``basis_alignment`` block (``basis_state`` + ``consumed_basis`` +
        ``consumed_reason`` + ``newer_head``) says how this dimension's evidence
        relates to the evidence the composition on the same page consumed.
    """

    band: str
    basis: list[str]
    severity_tag: Optional[str] = None
    severity_delta: Optional[str] = None
    effective_confidence: Optional[float] = None
    confidence: Optional[float] = None
    critic_score: Optional[float] = None  # the folded faithfulness score
    damped: bool = False
    reason: str = ""
    produced_at: Optional[str] = None
    #: H3 — the band the RETIRED damper would have shipped for this row, or
    #: ``None`` when it would not have fired. Never applied; recorded so the
    #: reversal is auditable per-row.
    damped_would_have_been: Optional[str] = None
    #: H3 — one of :data:`BASIS_ALIGNMENT_STATES`.
    basis_state: str = BASIS_NO_COMPOSITION
    #: H3 — every head the composition consumed for this dimension, newest
    #: first. Real ``analyst_outputs.id`` rows only, like ``basis``.
    consumed_basis: tuple[str, ...] = ()
    #: H3 — when the band is insufficient DESPITE a consumed head: the rule that
    #: rejected the newest consumed head. Never ``None`` in the
    #: ``consumed-unbandable`` state — that is the whole point of the state.
    consumed_reason: Optional[str] = None
    #: H3 — when the band came from a consumed head instead of the freshest one:
    #: the newer head, DATED, with the rule that rejected it. The reader sees
    #: both reads rather than only the one that survived.
    newer_head: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "band": self.band,
            "basis": list(self.basis),
            "severity_tag": self.severity_tag,
            "severity_delta": self.severity_delta,
            "effective_confidence": self.effective_confidence,
            "confidence": self.confidence,
            "critic_score": self.critic_score,
            "damped": self.damped,
            "damped_would_have_been": self.damped_would_have_been,
            "reason": self.reason,
            "produced_at": self.produced_at,
            "basis_alignment": {
                "state": self.basis_state,
                "consumed_basis": list(self.consumed_basis),
                "reason": self.consumed_reason,
                "newer_head": dict(self.newer_head) if self.newer_head else None,
            },
        }


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def _severity_from_tags(tags: Sequence[str]) -> Optional[str]:
    """Return the ``severity:<level>`` level (a valid one) from a tag list.

    The LAST valid ``severity:*`` tag wins if several are present (the analyst
    contract emits exactly one). Returns ``None`` when absent or the level is
    not one of the five known severities.
    """
    level: Optional[str] = None
    for tag in tags:
        if not isinstance(tag, str) or not tag.startswith("severity:"):
            continue
        candidate = tag.split(":", 1)[1].strip().lower()
        if candidate in SEVERITY_TO_BAND:
            level = candidate
    return level


def demote_one(band: str) -> str:
    """Walk one rung DOWN the band ladder, clamped at ``"low"``. NEVER promotes.

    ``band`` is expected to be a ladder value (every :data:`SEVERITY_TO_BAND`
    value is). An off-ladder value returns unchanged (defensive; unreachable via
    the rule engine).

    H3: this is no longer applied to any band. It survives as the definition of
    the RETIRED damper — R4 calls it to fill ``damped_would_have_been`` — so the
    reversal is measurable from the card itself and restoring the damper is a
    one-branch revert rather than a re-derivation.
    """
    try:
        idx = BAND_LADDER.index(band)
    except ValueError:
        return band
    return BAND_LADDER[max(0, idx - 1)]


def _insufficient(reason: str) -> DimensionVerdict:
    """The one honest empty verdict: band=insufficient-evidence, basis=[]."""
    return DimensionVerdict(band=INSUFFICIENT, basis=[], reason=reason)


# ---------------------------------------------------------------------------
# The rule engine.
# ---------------------------------------------------------------------------


def _band_claim(
    claim: Optional[Claim],
    *,
    conf_floor: float,
    conf_confident: float,
    faith_floor: float,
) -> DimensionVerdict:
    """The R0-R4 rule table over ONE claim. See :func:`band_dimension`.

    Split out because H3's basis alignment has to run the SAME rules over a
    SECOND candidate (a head the composition consumed) without duplicating a
    single guard — the alignment path may never be a softer path. Every
    consumed head is admitted or rejected by exactly this function.
    """
    # R0 — nothing fired.
    if claim is None:
        return _insufficient("no-finding")

    eff = claim.effective_confidence

    # R1 — verify never folded, or a coerce-fallback body.
    if eff is None or claim.is_coerce_fallback:
        return _insufficient("verify-failed")

    # R1b (P4-T5) — the per-claim faithfulness is present but below the dedicated
    # faithfulness floor. Excluded from the basis with its OWN reason so the
    # demotion is legible + distinct from the effective-confidence below-floor.
    if claim.faithfulness_score is not None and claim.faithfulness_score < faith_floor:
        return _insufficient("low-faithfulness")

    # R2 — below the confidence floor.
    if eff < conf_floor:
        return _insufficient("below-floor")

    # R3 — no usable severity tag.
    level = _severity_from_tags(claim.tags)
    if level is None:
        return _insufficient("no-severity-tag")

    # R4 — band from severity. H3: the band IS the severity tag's band; the
    # confidence split names the weakness in the ``reason`` and records what the
    # retired damper would have done, and moves NOTHING.
    band = SEVERITY_TO_BAND[level]
    if eff >= conf_confident:
        reason, would_have_been = "qualified", None
    else:
        reason, would_have_been = "qualified-low-confidence", demote_one(band)

    return DimensionVerdict(
        band=band,
        basis=[claim.finding_id],
        severity_tag=level,
        severity_delta=claim.severity_delta,
        effective_confidence=eff,
        confidence=claim.confidence,
        critic_score=claim.faithfulness_score,
        damped=False,
        damped_would_have_been=would_have_been,
        reason=reason,
        produced_at=claim.produced_at,
    )


def band_dimension(
    claim: Optional[Claim],
    *,
    conf_floor: float = CONF_FLOOR,
    conf_confident: float = CONF_CONFIDENT,
    faith_floor: float = FAITH_FLOOR,
    consumed: Sequence[Claim] = (),
    composition_present: bool = False,
) -> DimensionVerdict:
    """Band ONE dimension from its freshest verified claim (or lack of one).

    Rules, evaluated in order (R0-R3 are the insufficient-evidence guards; R4 is
    the only path that assigns a real band, and only once every guard passes):

      * **R0 ``no-finding``** — ``claim is None``: the unit did not fire / no
        ``kind=finding`` row for this ``analyst_id`` + target in the window.
      * **R1 ``verify-failed``** — ``effective_confidence`` is ``None`` (no
        ``Faithfulness verify%`` critique was ever folded — verify never ran or
        failed) OR the finding carries a ``coerce_failed`` / ``unstructured``
        tag (a garbage body is vacuously faithful; drop by tag).
      * **R1b ``low-faithfulness``** (P4-T5) — the per-claim faithfulness score
        is present but ``< faith_floor``. EXCLUDES the claim from the basis with
        a DEDICATED reason so the operator literally sees the band basis exclude
        it — distinct from ``below-floor`` (a claim with conf .9 / faith .45 now
        reads ``low-faithfulness``, not ``below-floor``). Evaluated AFTER R1
        (verify-failed, faithfulness absent) and BEFORE R2 (below-floor) so the
        two demotions never collide.
      * **R2 ``below-floor``** — ``effective_confidence < conf_floor``.
      * **R3 ``no-severity-tag``** — no valid ``severity:<level>`` tag.
      * **R4 band** — ``band = SEVERITY_TO_BAND[level]``, where ``level`` is
        the dimension's STANDING state (FRAME-3; see the module note). The band
        is the severity tag's band, FULL STOP. ``effective_confidence`` splits
        only the ``reason`` (``qualified`` at/above ``conf_confident``,
        ``qualified-low-confidence`` between the floor and it) and fills
        ``damped_would_have_been`` with the rung the RETIRED damper would have
        shipped. ``damped`` is always ``False`` (H3, see the module note).

    The claim's ``severity_delta`` is carried onto the R4 verdict UNUSED by any
    rule — the band is a statement about the condition, and no movement call
    may raise, demote or damp it. R0-R3 carry no delta at all: an insufficient
    verdict has no claim it is entitled to report anything about.

    The basis of any real band is exactly ``[<the banded row's id>]`` — the
    exact row that drove it. An insufficient verdict always carries ``basis=[]``:
    a band never exists without a real basis id, and a basis id never appears
    without a band it actually drove. H3 keeps BOTH halves of that invariant —
    the consumed ids it consults ride in ``consumed_basis``, never in ``basis``.

    H3 BASIS ALIGNMENT (the second half of this function)
    ----------------------------------------------------
    ``consumed`` is this dimension's heads from the composition head's
    ``derived_from``, NEWEST FIRST — the rows the prose on the same page is
    actually resting on. ``composition_present`` says whether there is a
    composition to align to at all (an empty ``consumed`` means two different
    things either side of it, and neither may be guessed).

    Exactly one state from :data:`BASIS_ALIGNMENT_STATES` is assigned:

      * the freshest claim BANDED and the composition consumed that row →
        :data:`BASIS_ALIGNED`;
      * the freshest claim BANDED and the composition consumed other rows (or
        this row is not among them) → :data:`BASIS_UNCONSUMED`. **The band does
        not change** — the state exists to make the reverse divergence visible;
      * the freshest claim ABSTAINED and a consumed head passes the identical
        R0-R4 guards → :data:`BASIS_CONSUMED`, banded from the NEWEST such head,
        with the newer rejected head recorded in ``newer_head``, dated;
      * the freshest claim ABSTAINED and no consumed head passes →
        :data:`BASIS_CONSUMED_UNBANDABLE`: still ``insufficient-evidence``, but
        naming ``consumed_basis`` and the rule that rejected the newest of them.
        This is the state that forbids a SILENT abstention beside a composition
        that read the desk;
      * a composition with no head for this dimension →
        :data:`BASIS_NOT_CONSUMED`; no composition at all →
        :data:`BASIS_NO_COMPOSITION`.
    """
    floors = {
        "conf_floor": conf_floor,
        "conf_confident": conf_confident,
        "faith_floor": faith_floor,
    }
    verdict = _band_claim(claim, **floors)
    consumed_ids = tuple(c.finding_id for c in consumed)

    # No second artefact on the page ⇒ nothing to diverge from. Returned before
    # any consumed row is looked at, so a caller that passes ``consumed`` without
    # a composition cannot silently change a band.
    if not composition_present:
        return replace(verdict, basis_state=BASIS_NO_COMPOSITION)

    if verdict.band != INSUFFICIENT:
        # The card banded. Aligned iff the composition consumed the very row the
        # band rests on; otherwise the two artefacts are reading different
        # evidence and say so. NEITHER branch touches the band.
        state = (
            BASIS_ALIGNED
            if verdict.basis and verdict.basis[0] in consumed_ids
            else BASIS_UNCONSUMED if consumed_ids
            else BASIS_NOT_CONSUMED
        )
        return replace(verdict, basis_state=state, consumed_basis=consumed_ids)

    if not consumed_ids:
        # The composition read this desk no more than the card did. They agree.
        return replace(verdict, basis_state=BASIS_NOT_CONSUMED)

    # The card abstained beside a composition that consumed a head here. Walk
    # the consumed heads newest-first through the SAME guards — never a softer
    # path — and band the first that qualifies.
    first_reason: Optional[str] = None
    for candidate in consumed:
        attempt = _band_claim(candidate, **floors)
        if first_reason is None:
            first_reason = attempt.reason
        if attempt.band == INSUFFICIENT:
            continue
        return replace(
            attempt,
            basis_state=BASIS_CONSUMED,
            consumed_basis=consumed_ids,
            newer_head=(
                {
                    "finding_id": claim.finding_id,
                    "reason": verdict.reason,
                    "produced_at": claim.produced_at,
                }
                if claim is not None
                else None
            ),
        )

    # Nothing the composition consumed can be banded honestly. Abstain — LOUDLY.
    return replace(
        verdict,
        basis_state=BASIS_CONSUMED_UNBANDABLE,
        consumed_basis=consumed_ids,
        consumed_reason=first_reason,
    )


def band_target(
    target_id: str,
    claims_by_dim: Mapping[str, Optional[Claim]],
    composition: Optional[Claim] = None,
    *,
    conf_floor: float = CONF_FLOOR,
    conf_confident: float = CONF_CONFIDENT,
    faith_floor: float = FAITH_FLOOR,
    generated_at: Optional[str] = None,
    consumed_by_dim: Optional[Mapping[str, Sequence[Claim]]] = None,
) -> dict[str, Any]:
    """Band every dimension for one target and surface the composition node.

    ``claims_by_dim`` maps a unit ``analyst_id`` -> its freshest :class:`Claim`
    (or ``None`` when it did not fire). Any dimension absent from the map is
    treated as ``None`` (``no-finding``), so the verdict ALWAYS reports all four.

    The ``composition`` claim is surfaced as its own aggregate node naming its
    basis id — it is NEVER folded into a fabricated overall band. When absent the
    node is ``{present: False, basis: []}``.

    ``consumed_by_dim`` (H3) maps a unit ``analyst_id`` -> the heads THAT
    composition consumed for it, newest first, resolved from its ``derived_from``
    by :func:`gather_and_band`. Omitted / empty reproduces the pre-H3 banding
    exactly, and the alignment states then honestly read ``no-composition`` or
    ``not-consumed`` rather than claiming an alignment nobody checked. It is
    ignored entirely when ``composition`` is absent: without the composition on
    the page there is no second artefact to align to, and a consumed set with no
    consumer would be a band moved by evidence the reader never sees.

    The verdict carries :data:`BANDING_SEMANTICS` and :data:`DAMPING_SEMANTICS`
    so a card records WHICH contracts produced it. Without them a ``low`` band
    from before the FRAME-3 flip and a ``low`` band after it are the same three
    characters meaning two different things — and so are a ``watch`` either side
    of the H3 damper retirement. A before/after band diff has to be a machine
    comparison, not a guess about deploy dates.

    ``basis_alignment`` at the verdict level counts what the per-dimension states
    found, so an operator sees the divergence rate for the country without
    walking seven blocks: ``realigned`` is the number of dimensions this card
    would have abstained on before H3 and now bands from the evidence the prose
    used, and ``unbandable`` the number that still abstain BESIDE a composition
    that read the desk — the honest residue, which is a real number and not zero.
    """
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    comp_present = composition is not None
    consumed_map = consumed_by_dim or {}

    dimensions: dict[str, Any] = {}
    counts = {
        BASIS_ALIGNED: 0,
        BASIS_CONSUMED: 0,
        BASIS_CONSUMED_UNBANDABLE: 0,
        BASIS_UNCONSUMED: 0,
        BASIS_NOT_CONSUMED: 0,
        BASIS_NO_COMPOSITION: 0,
    }
    for unit in DIMENSIONS:
        verdict = band_dimension(
            claims_by_dim.get(unit),
            conf_floor=conf_floor,
            conf_confident=conf_confident,
            faith_floor=faith_floor,
            consumed=tuple(consumed_map.get(unit) or ()),
            composition_present=comp_present,
        )
        counts[verdict.basis_state] = counts.get(verdict.basis_state, 0) + 1
        dimensions[unit] = verdict.to_dict()

    comp_node: dict[str, Any] = {
        "present": comp_present,
        "basis": [composition.finding_id] if comp_present else [],
        "effective_confidence": composition.effective_confidence
            if comp_present else None,
        "produced_at": composition.produced_at if comp_present else None,
    }

    return {
        "target_id": target_id,
        "generated_at": ts,
        "banding_semantics": BANDING_SEMANTICS,
        # H3 — which DAMPING contract this card was computed under.
        "damping_semantics": DAMPING_SEMANTICS,
        "floors": {
            "conf_floor": conf_floor,
            "conf_confident": conf_confident,
            # P4-T5 — the dedicated faithfulness floor surfaced so the operator
            # can see the threshold the low-faithfulness exclusion used.
            "faith_floor": faith_floor,
        },
        "dimensions": dimensions,
        "composition": comp_node,
        # H3 — the card's own divergence census against the composition it
        # prints beside. Per-state counts, never a ratio: "3 of 7 realigned"
        # and "1 still unbandable" are different facts and both are actionable.
        "basis_alignment": {
            "composition_present": comp_present,
            "states": counts,
            "realigned": counts[BASIS_CONSUMED],
            "unbandable": counts[BASIS_CONSUMED_UNBANDABLE],
        },
    }


# ---------------------------------------------------------------------------
# The async run-entry — gather this target's verified claims, then band.
# ---------------------------------------------------------------------------

#: Default lookback for the freshest claim per dimension. Generous relative to
#: the unit + composition cadences so a healthy country always has a head
#: finding in-window; an out-of-window (stale) unit reads as ``no-finding``.
DEFAULT_LOOKBACK_HOURS: int = 24 * 14

# The gather query. For each of the four unit analyst_ids (+ the composition)
# it takes the FRESHEST active head finding for this target and LEFT JOINs the
# LATEST paired ``Faithfulness verify%`` critique so the fold can be computed.
#
# LEFT (not INNER) is deliberate: a finding with no verify critique must still
# be SEEN here so the engine can report ``verify-failed`` honestly — an INNER
# join would silently vanish it and be indistinguishable from ``no-finding``.
# The fold itself is done in Python (``Claim.effective_confidence``) so the
# ``None`` (verify absent) case is explicit rather than SQL-``LEAST`` swallowing
# it.
_GATHER_SQL = """
    SELECT DISTINCT ON (f.analyst_id)
           f.id::text        AS finding_id,
           f.analyst_id      AS analyst_id,
           f.confidence      AS confidence,
           f.produced_at     AS produced_at,
           f.data -> 'tags'  AS tags,
           f.derived_from    AS derived_from,
           v.faithfulness_score AS faithfulness_score
      FROM analyst_outputs f
      LEFT JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.target_id = $1
       AND f.analyst_id = ANY($2::TEXT[])
       AND f.superseded_by IS NULL
       AND f.produced_at > NOW() - make_interval(hours => $3)
     ORDER BY f.analyst_id, f.produced_at DESC, f.id DESC
"""

# The AS-OF variant of the gather (H3, replay). Identical projection and
# identical ordering; the two predicates that name "now" are pinned to $4:
#
#   * ``produced_at`` is bounded ABOVE by the pin and the lookback measured back
#     from it, so a replay sees the window the card actually saw;
#   * the head-fold becomes head-AS-OF. ``superseded_by IS NULL`` is a statement
#     about the substrate NOW, and every R2-era head has since been superseded by
#     a later cycle — filtering on it would return an empty replay and look like
#     a defect in the code under test. ``superseded_at`` (the column exists and
#     is stamped by the same write) makes the correct predicate exact rather than
#     approximate: a row was a head at T iff it was not yet superseded at T.
#
# Kept as a SEPARATE constant rather than an interpolated ``NOW()``/``$4`` switch
# so that the production string is provably untouched — a test asserts
# ``as_of=None`` executes :data:`_GATHER_SQL` verbatim.
_GATHER_SQL_AS_OF = """
    SELECT DISTINCT ON (f.analyst_id)
           f.id::text        AS finding_id,
           f.analyst_id      AS analyst_id,
           f.confidence      AS confidence,
           f.produced_at     AS produced_at,
           f.data -> 'tags'  AS tags,
           f.derived_from    AS derived_from,
           v.faithfulness_score AS faithfulness_score
      FROM analyst_outputs f
      LEFT JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
             AND cr.produced_at <= $4
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.target_id = $1
       AND f.analyst_id = ANY($2::TEXT[])
       AND (f.superseded_by IS NULL OR f.superseded_at > $4)
       AND f.produced_at <= $4
       AND f.produced_at > $4 - make_interval(hours => $3)
     ORDER BY f.analyst_id, f.produced_at DESC, f.id DESC
"""

# H3 — resolve the composition's CONSUMED heads: the exact rows named by its
# ``derived_from``, restricted to this target's unit dimensions. Same verify
# lateral as the gather above, so a consumed head's folded numbers are computed
# by the identical rule the freshest head's are — the alignment path may differ
# in WHICH row it looks at, never in HOW it judges one.
#
# Newest-first within a dimension: the walk in :func:`band_dimension` takes the
# first that qualifies, which is FRAME-1's rule verbatim ("the newest head that
# cleared the floor is in BASIS; the newer failing head stays in periphery,
# dated").
#
# ``$4`` is the NULLABLE as-of pin. This query has no legacy string to preserve
# (it is new in H3), so one statement carries both modes: NULL is production and
# the predicate is a no-op; a timestamp bounds the verify lateral so a replayed
# consumed head is scored by the critique that existed at the pin, not by one
# written since. The rows themselves need no ``produced_at`` bound — they are
# named by id from a composition that was itself gathered as-of.
_CONSUMED_SQL = """
    SELECT f.id::text        AS finding_id,
           f.analyst_id      AS analyst_id,
           f.confidence      AS confidence,
           f.produced_at     AS produced_at,
           f.data -> 'tags'  AS tags,
           v.faithfulness_score AS faithfulness_score
      FROM analyst_outputs f
      LEFT JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
             AND ($4::timestamptz IS NULL OR cr.produced_at <= $4)
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.id = ANY($1::UUID[])
       AND f.kind = 'finding'
       AND f.target_id = $2
       AND f.analyst_id = ANY($3::TEXT[])
     ORDER BY f.analyst_id, f.produced_at DESC, f.id DESC
"""


def _coerce_tags(raw: Any) -> tuple[str, ...]:
    """Normalise the ``data -> 'tags'`` JSONB (list, JSON-encoded str, or None)
    into a tuple of tag strings."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(t) for t in raw)
    return ()


def _claim_from_row(row: Mapping[str, Any]) -> Claim:
    conf = row["confidence"]
    faith = row["faithfulness_score"]
    produced = row["produced_at"]
    return Claim(
        finding_id=str(row["finding_id"]),
        analyst_id=str(row["analyst_id"]),
        confidence=float(conf) if conf is not None else None,
        faithfulness_score=float(faith) if faith is not None else None,
        tags=_coerce_tags(row["tags"]),
        produced_at=produced.isoformat()
            if isinstance(produced, datetime) else produced,
    )


def _consumed_ids(row: Mapping[str, Any]) -> list[str]:
    """The ``derived_from`` uuids of a gathered row, as strings.

    Tolerant by design: a row projected WITHOUT the column (a hand-built test
    row, a caller on an older projection) yields ``[]`` — the honest "we did not
    look", which the alignment layer reports as ``not-consumed`` rather than
    treating as a divergence it never checked for.
    """
    try:
        raw = row["derived_from"]
    except (KeyError, IndexError, TypeError):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    # Coerced through UUID so an unparseable lineage entry is DROPPED here
    # rather than aborting the whole gather on a codec error one line later —
    # the composition node still names its basis, and the alignment for that
    # dimension degrades to ``not-consumed``, which is the honest reading.
    out: list[str] = []
    for entry in raw:
        try:
            out.append(str(UUID(str(entry))))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


async def gather_and_band(
    pool: Any,
    target_id: str,
    *,
    conf_floor: float = CONF_FLOOR,
    conf_confident: float = CONF_CONFIDENT,
    faith_floor: float = FAITH_FLOOR,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    as_of: Optional[Any] = None,
) -> dict[str, Any]:
    """Gather this target's verified claims and return the banded verdict.

    The runnable acceptance entry: runs the verify-folded gather SQL over
    ``target_id``'s freshest active unit findings + composition, resolves the
    composition's CONSUMED heads (H3), then delegates to the pure
    :func:`band_target`. Read-only; mutates nothing.

    Two queries, not one, and the second fires ONLY when a composition head was
    found and names a basis — on a country with no composition the cost is
    exactly the pre-H3 cost. The consumed set is READ from the composition's own
    ``derived_from`` rather than re-derived from the substrate: re-deriving would
    produce a *plausible* consumed set, and the whole point of the alignment is
    that the card rests on the rows the prose ACTUALLY used.

    ``as_of`` (optional, replay) pins the gather to an instant: ``None`` is the
    production path and executes :data:`_GATHER_SQL` unchanged; a timestamp
    executes :data:`_GATHER_SQL_AS_OF`, which bounds the window above and reads
    the head-fold as of that instant. It exists so a card produced in the past
    can be REBUILT under new code and diffed band-for-band — a change to a
    banding rule is otherwise unfalsifiable until the next cadence tick, and
    "we believe this moves bands toward the evidence" is not a measurement.
    """
    analyst_ids = list(DIMENSIONS) + [COMPOSITION_ANALYST_ID]
    async with pool.acquire() as conn:
        if as_of is None:
            records = await conn.fetch(
                _GATHER_SQL, str(target_id), analyst_ids, int(lookback_hours)
            )
        else:
            records = await conn.fetch(
                _GATHER_SQL_AS_OF,
                str(target_id), analyst_ids, int(lookback_hours), as_of,
            )

        claims_by_dim: dict[str, Optional[Claim]] = {
            unit: None for unit in DIMENSIONS
        }
        composition: Optional[Claim] = None
        composition_basis: list[str] = []
        for record in records:
            claim = _claim_from_row(record)
            if claim.analyst_id == COMPOSITION_ANALYST_ID:
                composition = claim
                composition_basis = _consumed_ids(record)
            elif claim.analyst_id in claims_by_dim:
                claims_by_dim[claim.analyst_id] = claim

        consumed_by_dim: dict[str, list[Claim]] = {}
        if composition is not None and composition_basis:
            consumed_rows = await conn.fetch(
                _CONSUMED_SQL,
                [UUID(b) for b in composition_basis],
                str(target_id),
                list(DIMENSIONS),
                as_of,
            )
            for record in consumed_rows:
                consumed = _claim_from_row(record)
                consumed_by_dim.setdefault(consumed.analyst_id, []).append(consumed)

    return band_target(
        str(target_id),
        claims_by_dim,
        composition,
        conf_floor=conf_floor,
        conf_confident=conf_confident,
        faith_floor=faith_floor,
        consumed_by_dim=consumed_by_dim,
    )
