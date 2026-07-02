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

The engine splits three ways so the rules are unit-testable with NO database:

  * :func:`band_dimension` — pure ``Claim | None -> DimensionVerdict``.
  * :func:`band_target` — pure ``(claims_by_dim, composition) -> verdict dict``.
  * :func:`gather_and_band` — the async run-entry that executes the verify-folded
    gather SQL and then calls :func:`band_target` (the acceptance entry).

This is T1 ONLY: the pure rules + a run-entry. Wrapping this in a producer
analyst and a ``scorecard`` OutputKind is T2 and out of scope here. Banding is
idempotent and read-only over already-verified claims — it NEVER mutates or
re-verifies a finding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants — the whole rule table.
# ---------------------------------------------------------------------------

#: The fixed dimensions = the bounded-reasoning unit ANALYST_IDs (runtime-set,
#: deterministic). Ordered; the verdict reports every one, always. internal_stability
#: (S1-T4) and military_posture (S1-T5) join the original four P2 units — both are
#: BROAD units (blanket g20+watch predicate, every desk fires them) so a FIXED
#: dimension is safe. Keep in sync with country_composition.other_analysts,
#: substrate_query_port._ASSESSMENT_PRODUCER_ANALYSTS, and
#: unit_correctness_scorer._DEFAULT_UNITS.
DIMENSIONS: tuple[str, ...] = (
    "leadership_transition",
    "energy_security",
    "escalation",
    "narrative_coordination",
    "internal_stability",
    "military_posture",
)

#: The P3 per-country aggregate. Surfaced as its OWN node (naming its basis id),
#: NEVER folded into a fabricated overall band.
COMPOSITION_ANALYST_ID: str = "country_composition"

#: A claim below this folded effective_confidence is NOT strong enough to band
#: (``reason='below-floor'``).
CONF_FLOOR: float = 0.35

#: At/above this folded effective_confidence the severity band stands as-is;
#: between the floor and this it is demoted one rung ("low-faithfulness reads
#: lower").
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


@dataclass(frozen=True)
class DimensionVerdict:
    """A single dimension's band + the honest evidence it was derived from.

    For a qualifying claim: a real ``band`` naming ``basis=[finding_id]`` with
    the severity tag and the folded numbers. For anything else:
    ``band=INSUFFICIENT``, ``basis=[]``, all numeric fields ``None``, and a
    machine ``reason``.
    """

    band: str
    basis: list[str]
    severity_tag: Optional[str] = None
    effective_confidence: Optional[float] = None
    confidence: Optional[float] = None
    critic_score: Optional[float] = None  # the folded faithfulness score
    damped: bool = False
    reason: str = ""
    produced_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "band": self.band,
            "basis": list(self.basis),
            "severity_tag": self.severity_tag,
            "effective_confidence": self.effective_confidence,
            "confidence": self.confidence,
            "critic_score": self.critic_score,
            "damped": self.damped,
            "reason": self.reason,
            "produced_at": self.produced_at,
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


def band_dimension(
    claim: Optional[Claim],
    *,
    conf_floor: float = CONF_FLOOR,
    conf_confident: float = CONF_CONFIDENT,
    faith_floor: float = FAITH_FLOOR,
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
      * **R4 band** — ``base = SEVERITY_TO_BAND[level]``; if
        ``effective_confidence >= conf_confident`` the band is ``base``; if
        ``conf_floor <= effective_confidence < conf_confident`` the band is
        ``demote_one(base)`` with ``damped=True`` ("low-faithfulness reads
        lower" — one rung DOWN, clamped at ``low``, NEVER a promotion).

    The basis of any real band is exactly ``[claim.finding_id]`` — the exact row
    that drove it. An insufficient verdict always carries ``basis=[]``.
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

    # R4 — band from severity, damped if only weakly confident.
    base = SEVERITY_TO_BAND[level]
    if eff >= conf_confident:
        band, damped, reason = base, False, "qualified"
    else:
        band, damped, reason = demote_one(base), True, "damped"

    return DimensionVerdict(
        band=band,
        basis=[claim.finding_id],
        severity_tag=level,
        effective_confidence=eff,
        confidence=claim.confidence,
        critic_score=claim.faithfulness_score,
        damped=damped,
        reason=reason,
        produced_at=claim.produced_at,
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
) -> dict[str, Any]:
    """Band every dimension for one target and surface the composition node.

    ``claims_by_dim`` maps a unit ``analyst_id`` -> its freshest :class:`Claim`
    (or ``None`` when it did not fire). Any dimension absent from the map is
    treated as ``None`` (``no-finding``), so the verdict ALWAYS reports all four.

    The ``composition`` claim is surfaced as its own aggregate node naming its
    basis id — it is NEVER folded into a fabricated overall band. When absent the
    node is ``{present: False, basis: []}``.
    """
    ts = generated_at or datetime.now(timezone.utc).isoformat()

    dimensions: dict[str, Any] = {}
    for unit in DIMENSIONS:
        verdict = band_dimension(
            claims_by_dim.get(unit),
            conf_floor=conf_floor,
            conf_confident=conf_confident,
            faith_floor=faith_floor,
        )
        dimensions[unit] = verdict.to_dict()

    comp_present = composition is not None
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
        "floors": {
            "conf_floor": conf_floor,
            "conf_confident": conf_confident,
            # P4-T5 — the dedicated faithfulness floor surfaced so the operator
            # can see the threshold the low-faithfulness exclusion used.
            "faith_floor": faith_floor,
        },
        "dimensions": dimensions,
        "composition": comp_node,
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


async def gather_and_band(
    pool: Any,
    target_id: str,
    *,
    conf_floor: float = CONF_FLOOR,
    conf_confident: float = CONF_CONFIDENT,
    faith_floor: float = FAITH_FLOOR,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> dict[str, Any]:
    """Gather this target's verified claims and return the banded verdict.

    The runnable acceptance entry: runs the verify-folded gather SQL over
    ``target_id``'s freshest active unit findings + composition, then delegates
    to the pure :func:`band_target`. Read-only; mutates nothing.
    """
    analyst_ids = list(DIMENSIONS) + [COMPOSITION_ANALYST_ID]
    async with pool.acquire() as conn:
        records = await conn.fetch(
            _GATHER_SQL, str(target_id), analyst_ids, int(lookback_hours)
        )

    claims_by_dim: dict[str, Optional[Claim]] = {unit: None for unit in DIMENSIONS}
    composition: Optional[Claim] = None
    for record in records:
        claim = _claim_from_row(record)
        if claim.analyst_id == COMPOSITION_ANALYST_ID:
            composition = claim
        elif claim.analyst_id in claims_by_dim:
            claims_by_dim[claim.analyst_id] = claim

    return band_target(
        str(target_id),
        claims_by_dim,
        composition,
        conf_floor=conf_floor,
        conf_confident=conf_confident,
        faith_floor=faith_floor,
    )
