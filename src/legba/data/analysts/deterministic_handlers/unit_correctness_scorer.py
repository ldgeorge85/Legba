# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``unit_correctness_scorer`` sub-handler — P2-T5.

Per-bounded-unit CORRECTNESS-vs-reference scorer. Phase 2 measures each small
reasoning UNIT individually; this handler answers, per unit, the machine-checkable
question "did the unit rest its read on the canonical evidence?" by comparing the
unit's live finding grounding against the operator-authored gold rows in
``unit_reference_labels`` (migration 0057). It is the DETERMINISTIC, LLM-free,
$0 floor beneath a later semantic correctness judge — exactly as the calibration
loop's exogenous Brier is the floor beneath the segregated forecast pilot.

DISTINCT from FAITHFULNESS (P0-T2): faithfulness asks "is the prose faithful to
its own cites?"; correctness asks "is the read RIGHT vs a gold answer?". This
handler reports BOTH per unit (faithfulness = mean of the unit's faithfulness
critique scores; correctness = the source-id overlap metric below).

The metric — Source-ID Overlap, canonical-source RECALL (P2-T5 chosen metric)
----------------------------------------------------------------------------

ATOMIC UNIT = one head finding ``f`` vs one gold-label row ``ℓ`` that share
``(unit_analyst_id, target_id)`` (a label with ``target_id IS NULL`` matches the
unit's meta / no-target finding).

ID-SET BUILD (every id coerced to a canonical UUID exactly as
``labels_api._validate_source_ids`` does — ``UUID(str(x))``; a None / unparseable
id is skipped so it never silently joins or misses the set):

    C(f) = { signal_id of each resolved nested data->'data'->'citations' entry }
           ∪ { each top-level derived_from uuid }
    G(ℓ) = { each canonical_source_ids uuid }

PER-(finding, label) RECALL — recall, NOT Jaccard/precision, because
``canonical_source_ids`` is the provenance the GOLD answer rests on, not an
exhaustive whitelist of acceptable sources, so a unit that ALSO cites broader
sources is not "wrong":

    recall(f, ℓ) = |C(f) ∩ G(ℓ)| / |G(ℓ)|   when |G(ℓ)| > 0
    recall(f, ℓ) = None (UNDEFINED)          when |G(ℓ)| == 0   # text-only label, skipped

BEST-LABEL (disjunctive) per (unit, target) — each operator-authored row is a
self-contained acceptable answer, so take the best match. ``f*`` = the unit's
LATEST head finding for the target; ``L_t`` = the SCORABLE labels (|G| > 0):

    match(unit, target) = max( recall(f*, ℓ) for ℓ in L_t )   if f* AND L_t
    match(unit, target) = None                                otherwise  # missing finding → unscored, NOT 0

AGGREGATION — per unit, simple group-mean (mirrors
``calibration_tracking._per_analyst_brier`` — no weighting, no cross-unit pool):

    scored = [ match(unit, t) for t in labeled_targets(unit) if match is not None ]
    correctness_vs_reference(unit) = mean(scored)   if scored   else   None

NULL RULE (the T5 honesty done-criterion) — ``correctness_vs_reference`` is None
(NEVER 0.0, never a default) whenever no target yields a scorable match, each
carrying a status string so a null is never ambiguous:
  * 0 gold rows for the unit (today's state)            → ``no gold labels``
  * labels exist but all have empty canonical_source_ids → ``labels present but
    none scorable (text-only / empty canonical_source_ids)``
  * labeled targets exist but the unit has no head finding for ANY of them
                                                         → ``labeled targets have
    no finding to score``
A real 0.0 arises ONLY when f* exists, |G(ℓ)| > 0, and C(f*) ∩ G(ℓ) is genuinely
empty for the best label — a TRUE correctness signal, not a default.

DIAGNOSTICS (carried alongside, NEVER folded into the headline): companion
Jaccard (exposes cite-the-world inflation), correctness_citations_only (the same
recall with C = citations-only, dropping derived_from — a tighter prose-bound
variant), and a per-target map. Known blind spot, documented not hidden:
source-id overlap can't see "right answer via different valid sources" and recall
is gameable by over-citation — which is why Jaccard rides along and a semantic
pass is layered on top later; this must not stand ALONE as "right".

Output ``data`` keys:
    sub_handler         "unit_correctness_scorer"
    units               {unit: per-unit record}
    scored_unit_count   int  (units with a non-None correctness)
    total_gold_labels   int
    lookback_days       int
    warnings            [str]

Per-unit record:
    unit                        str
    faithfulness                float | None   (mean faithfulness critique score)
    correctness_vs_reference    float | None   (the headline; None per the null rule)
    n_labeled                   int   (gold label ROWS for the unit)
    n_findings                  int   (head findings for the unit in-window)
    correctness_citations_only  float | None   (DIAGNOSTIC)
    jaccard_diagnostic          float | None   (DIAGNOSTIC)
    labeled_target_count        int   (distinct targets with >=1 label)
    scored_target_count         int   (len(scored))
    per_target                  {target: {match, best_label_id, intersection_size,
                                          gold_size, cited_size, jaccard,
                                          match_citations_only, reason?}}
    status                      str
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from uuid import UUID

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

# The bounded reasoning UNITS (kind=inline_target descriptors) Phase 2 measures
# individually. Overridable via options['units'] for tests / a future unit set,
# but defaulted here so the descriptor stays a bare global sweep. internal_stability
# (S1-T4) + military_posture (S1-T5) + economic_coercion (S1-T7) join the original
# four; keep in sync with scorecard_banding.DIMENSIONS.
_DEFAULT_UNITS: tuple[str, ...] = (
    "leadership_transition",
    "energy_security",
    "escalation",
    "narrative_coordination",
    "internal_stability",
    "military_posture",
    "economic_coercion",
)

# Window for "recent" head findings + faithfulness critiques. The per-target read
# always takes the LATEST head finding within the window, so this only bounds how
# far back a stale-but-head finding is still scored.
_DEFAULT_LOOKBACK_DAYS = 365

# Sentinel key for the meta / no-target bucket in the JSON-serialized per_target
# map (a None dict key would be coerced to "null" by the JSON encoder — make the
# intent explicit instead of relying on that).
_META_TARGET_KEY = "__no_target__"

# Status strings (the calibration headline-None idiom: a null is never ambiguous).
_STATUS_NO_LABELS = "no gold labels"
_STATUS_NONE_SCORABLE = (
    "labels present but none scorable (text-only / empty canonical_source_ids)"
)
_STATUS_NO_FINDING = "labeled targets have no finding to score"
_STATUS_SCORED = "scored"


# ---------------------------------------------------------------------------
# ID-set construction (UUID-canonical, polarity-immune at the set level)
# ---------------------------------------------------------------------------


def _coerce_uuid(x: Any) -> UUID | None:
    """Coerce one id to a canonical ``UUID`` exactly as
    ``labels_api._validate_source_ids`` does (``UUID(str(x))``), returning None
    on a None / unparseable id so it never silently joins OR misses a set.
    Canonicalizing means case/string-variant-equal ids cannot miss the
    intersection."""
    if x is None:
        return None
    try:
        return UUID(str(x))
    except (ValueError, AttributeError, TypeError):
        return None


def _finding_source_ids(
    row_data: Any,
    derived_from: Any,
    *,
    citations_only: bool = False,
) -> set[UUID]:
    """Build C(f) — the finding's grounding id-set.

    ``row_data`` is the ``analyst_outputs.data`` JSONB column; the resolved
    citations nest under ``row_data['data']['citations']`` (one entry per resolved
    [N] marker, ``{marker, signal_id, ...}``). ``derived_from`` is the top-level
    ``derived_from uuid[]`` column. The union is safe BECAUSE the score is recall
    (extra cited sources cannot lower it). ``citations_only=True`` drops
    ``derived_from`` for the tighter prose-bound diagnostic variant."""
    nested = row_data.get("data") if isinstance(row_data, Mapping) else None
    citations = (nested.get("citations") if isinstance(nested, Mapping) else None) or []
    ids: set[UUID] = set()
    for c in citations:
        if isinstance(c, Mapping) and c.get("signal_id"):
            u = _coerce_uuid(c.get("signal_id"))
            if u is not None:
                ids.add(u)
    if not citations_only:
        for x in (derived_from or []):
            u = _coerce_uuid(x)
            if u is not None:
                ids.add(u)
    return ids


def _gold_source_ids(canonical_source_ids: Any) -> set[UUID]:
    """Build G(ℓ) — the gold answer's load-bearing grounding rows."""
    ids: set[UUID] = set()
    for x in (canonical_source_ids or []):
        u = _coerce_uuid(x)
        if u is not None:
            ids.add(u)
    return ids


# ---------------------------------------------------------------------------
# Core set metrics
# ---------------------------------------------------------------------------


def _recall(cited: set[UUID], gold: set[UUID]) -> float | None:
    """|C ∩ G| / |G|; None when |G| == 0 (text-only label → skipped, never 0)."""
    if not gold:
        return None
    return len(cited & gold) / len(gold)


def _jaccard(cited: set[UUID], gold: set[UUID]) -> float | None:
    """|C ∩ G| / |C ∪ G| — DIAGNOSTIC only (exposes cite-the-world inflation).
    None when the union is empty (nothing to compare)."""
    union = cited | gold
    if not union:
        return None
    return len(cited & gold) / len(union)


def _mean(values: list[float]) -> float | None:
    """``mean(sims) if sims else None`` — the exact ``_brier`` / per-analyst
    idiom. Callers pass an already-None-filtered list of floats."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


# ---------------------------------------------------------------------------
# Per-unit scoring (pure — testable without a DB)
# ---------------------------------------------------------------------------


def _status(
    labeled_target_count: int,
    has_scorable_label: bool,
    scored_target_count: int,
) -> str:
    if labeled_target_count == 0:
        return _STATUS_NO_LABELS
    if not has_scorable_label:
        return _STATUS_NONE_SCORABLE
    if scored_target_count == 0:
        return _STATUS_NO_FINDING
    return _STATUS_SCORED


def score_unit(
    findings_by_target: Mapping[Any, Mapping[str, set[UUID]]],
    labels_by_target: Mapping[Any, list[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Compute one unit's correctness record from its latest head findings + gold.

    ``findings_by_target`` maps a ``target_id`` (str or None) to the LATEST head
    finding's id-sets ``{"cited_ids": set, "citations_only_ids": set}``.
    ``labels_by_target`` maps a ``target_id`` to the unit's gold rows for that
    target, each ``{"label_id": str, "gold_ids": set}``. Aggregates ONLY over the
    unit's labeled targets (findings without a label are irrelevant to scoring)."""
    per_target: dict[str, Any] = {}
    matches: list[float] = []
    matches_co: list[float] = []
    jaccards: list[float] = []
    has_scorable_label = False

    for target, labels in labels_by_target.items():
        scorable = [ll for ll in labels if ll.get("gold_ids")]
        if scorable:
            has_scorable_label = True
        finding = findings_by_target.get(target)
        key = _META_TARGET_KEY if target is None else str(target)

        # match is None (UNSCORED, never 0) when there is no head finding OR no
        # scorable label for the target — cadence latency / a text-only label is
        # not scored as wrongness.
        if finding is None or not scorable:
            per_target[key] = {
                "match": None,
                "best_label_id": None,
                "intersection_size": None,
                "gold_size": None,
                "cited_size": (len(finding["cited_ids"]) if finding else None),
                "jaccard": None,
                "match_citations_only": None,
                "reason": "no_finding" if finding is None else "no_scorable_label",
            }
            continue

        cited = finding["cited_ids"]
        cited_co = finding["citations_only_ids"]
        # Disjunctive max over the scorable labels (full C). A genuinely-empty
        # intersection yields a real 0.0 here — a true correctness signal.
        best_recall = -1.0
        best_label = scorable[0]
        best_inter = 0
        best_gold = 0
        for ll in scorable:
            gold = ll["gold_ids"]
            r = _recall(cited, gold)  # gold nonempty → float
            if r is not None and r > best_recall:
                best_recall = r
                best_label = ll
                best_inter = len(cited & gold)
                best_gold = len(gold)
        # citations-only disjunctive max (its own best label — a diagnostic).
        co_vals = [_recall(cited_co, ll["gold_ids"]) for ll in scorable]
        match_co = max(v for v in co_vals if v is not None)
        jacc = _jaccard(cited, best_label["gold_ids"])

        per_target[key] = {
            "match": best_recall,
            "best_label_id": best_label["label_id"],
            "intersection_size": best_inter,
            "gold_size": best_gold,
            "cited_size": len(cited),
            "jaccard": jacc,
            "match_citations_only": match_co,
        }
        matches.append(best_recall)
        matches_co.append(match_co)
        if jacc is not None:
            jaccards.append(jacc)

    labeled_target_count = len(labels_by_target)
    scored_target_count = len(matches)
    return {
        "correctness_vs_reference": _mean(matches),
        "correctness_citations_only": _mean(matches_co),
        "jaccard_diagnostic": _mean(jaccards),
        "labeled_target_count": labeled_target_count,
        "scored_target_count": scored_target_count,
        "per_target": per_target,
        "status": _status(
            labeled_target_count, has_scorable_label, scored_target_count
        ),
    }


# ---------------------------------------------------------------------------
# Live-substrate pull (best-effort)
# ---------------------------------------------------------------------------


_FINDINGS_SQL = """
    SELECT target_id, data, derived_from
    FROM analyst_outputs
    WHERE kind = 'finding'
      AND analyst_id = $1
      AND superseded_by IS NULL
      AND produced_at > NOW() - make_interval(days => $2)
    ORDER BY produced_at DESC, id DESC
"""

_LABELS_SQL = """
    SELECT id::text AS label_id, target_id, canonical_source_ids
    FROM unit_reference_labels
    WHERE unit_analyst_id = $1
"""

# Faithfulness critiques are the in-run verify side-writes (actor_critic): a
# `critique` row carrying the unit's analyst_id whose title is the distinctive
# "Faithfulness verify (score X.XX)" and whose confidence IS the faithfulness
# score. Other critiques (e.g. country_critic) carry a different analyst_id +
# title, so this never folds an unrelated score in.
_FAITHFULNESS_SQL = """
    SELECT confidence
    FROM analyst_outputs
    WHERE kind = 'critique'
      AND analyst_id = $1
      AND title LIKE 'Faithfulness verify%'
      AND produced_at > NOW() - make_interval(days => $2)
"""


async def _pull_unit(
    conn: Any, unit: str, lookback_days: int
) -> tuple[dict[Any, dict[str, set[UUID]]], int, dict[Any, list[dict[str, Any]]], int, float | None]:
    """Pull one unit's latest-head findings (by target), gold labels (by target),
    and mean faithfulness. Returns
    ``(findings_by_target, n_findings, labels_by_target, n_labeled, faithfulness)``."""
    finding_rows = await conn.fetch(_FINDINGS_SQL, unit, lookback_days)
    findings_by_target: dict[Any, dict[str, set[UUID]]] = {}
    for row in finding_rows:
        target = row["target_id"]
        # Rows are produced_at DESC, id DESC — the FIRST seen per target is its
        # latest head finding (f*); later (older) rows for that target are ignored.
        if target in findings_by_target:
            continue
        data = row["data"] or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:  # noqa: BLE001 — a malformed blob just yields empty ids
                data = {}
        derived_from = row["derived_from"]
        findings_by_target[target] = {
            "cited_ids": _finding_source_ids(data, derived_from),
            "citations_only_ids": _finding_source_ids(
                data, derived_from, citations_only=True
            ),
        }
    n_findings = len(finding_rows)

    label_rows = await conn.fetch(_LABELS_SQL, unit)
    labels_by_target: dict[Any, list[dict[str, Any]]] = {}
    for row in label_rows:
        labels_by_target.setdefault(row["target_id"], []).append({
            "label_id": row["label_id"],
            "gold_ids": _gold_source_ids(row["canonical_source_ids"]),
        })
    n_labeled = len(label_rows)

    faith_rows = await conn.fetch(_FAITHFULNESS_SQL, unit, lookback_days)
    faithfulness = _mean([row["confidence"] for row in faith_rows])

    return findings_by_target, n_findings, labels_by_target, n_labeled, faithfulness


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    records: list[dict[str, Any]],
    *,
    lookback_days: int,
    warnings: list[str],
) -> FindingPayload:
    scored_units = [r for r in records if r["correctness_vs_reference"] is not None]
    total_gold = sum(int(r["n_labeled"]) for r in records)

    if scored_units:
        head = (
            f"Unit correctness vs reference: {len(scored_units)}/{len(records)} "
            f"units scored (gold rows={total_gold})"
        )
    elif total_gold > 0:
        head = (
            f"Unit correctness vs reference: gold present but none scorable "
            f"(gold rows={total_gold}) — correctness None for all units"
        )
    else:
        head = (
            "Unit correctness vs reference: 0 gold labels — correctness None for "
            "all units (honest null)"
        )

    body_lines = [
        (
            f"{r['unit']}: correctness={r['correctness_vs_reference']} "
            f"faithfulness={r['faithfulness']} "
            f"n_labeled={r['n_labeled']} n_findings={r['n_findings']} "
            f"scored={r['scored_target_count']}/{r['labeled_target_count']} "
            f"jaccard={r['jaccard_diagnostic']} "
            f"citations_only={r['correctness_citations_only']} "
            f"status={r['status']}"
        )
        for r in records
    ]

    tags = ["deterministic", "unit_correctness_scorer"]
    if not scored_units:
        # HONESTY tag: the headline is None for every unit — say WHY.
        tags.append(
            "unit_correctness_no_gold" if total_gold == 0
            else "unit_correctness_unscorable"
        )

    return FindingPayload(
        title=head[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "unit_correctness_scorer",
            "units": {r["unit"]: r for r in records},
            "scored_unit_count": len(scored_units),
            "total_gold_labels": total_gold,
            "lookback_days": lookback_days,
            "warnings": warnings,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    A META single global sweep: the cadence actor hands this handler the generic
    signals slice as ``inputs``, which is NOT a correctness input — it is ignored;
    everything is pulled per-unit from the substrate. ``deps`` (or its pool) being
    None degrades to the HONEST empty result — every unit reports correctness None
    with ``status='no gold labels'`` (the table's state today), NOT a stub."""
    units = tuple(options.get("units") or _DEFAULT_UNITS)
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    warnings: list[str] = []
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    records: list[dict[str, Any]] = []
    for unit in units:
        findings_by_target: dict[Any, dict[str, set[UUID]]] = {}
        labels_by_target: dict[Any, list[dict[str, Any]]] = {}
        n_findings = 0
        n_labeled = 0
        faithfulness: float | None = None
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    (
                        findings_by_target,
                        n_findings,
                        labels_by_target,
                        n_labeled,
                        faithfulness,
                    ) = await _pull_unit(conn, unit, lookback_days)
            except Exception as exc:  # noqa: BLE001 — never break the sweep
                logger.warning(
                    "unit_correctness_scorer.pull_failed unit=%s err=%s", unit, exc
                )
                warnings.append(f"unit_correctness_scorer.pull_failed unit={unit}")

        scored = score_unit(findings_by_target, labels_by_target)
        records.append({
            "unit": unit,
            "faithfulness": faithfulness,
            # The HONEST headline — None per the null rule whenever nothing is
            # scorable; a real 0.0 only when a finding cited NONE of the canonical
            # evidence for its best label.
            "correctness_vs_reference": scored["correctness_vs_reference"],
            "n_labeled": n_labeled,
            "n_findings": n_findings,
            # DIAGNOSTICS — never the headline.
            "correctness_citations_only": scored["correctness_citations_only"],
            "jaccard_diagnostic": scored["jaccard_diagnostic"],
            "labeled_target_count": scored["labeled_target_count"],
            "scored_target_count": scored["scored_target_count"],
            "per_target": scored["per_target"],
            "status": scored["status"],
        })

    finding = _build_finding(
        records, lookback_days=lookback_days, warnings=warnings
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
