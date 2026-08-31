# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``unit_correctness_scorer`` sub-handler — P2-T5, rewired M-1 (2026-08-03).

Per-bounded-unit CORRECTNESS scorer. Phase 2 measures each small reasoning UNIT
individually; this handler answers, per unit, "was the unit's read RIGHT?" — a
question DISTINCT from faithfulness (P0-T2), which only asks "is the prose
faithful to its own cites?". A finding can be perfectly faithful to citations
that do not support the conclusion the operator would have drawn; the 2026-07-28
gold-set round measured exactly that gap (operator correctness ≈0.625 against a
same-window faithfulness ≈0.92).

TWO CORRECTNESS AXES, AND THE M-1 REWIRE
----------------------------------------
There are two gold tables, and until 2026-08-03 this handler was wired to the
one that is never fed:

* **PRIMARY — the OPERATOR gold set** (``correctness_labels``, mig 0096): the
  weekly labeling loop's per-finding SEMANTIC verdicts. It is the platform's
  only JUDGE-INDEPENDENT quality signal, it is the table the operator actually
  writes to, and it surfaced in exactly one API overlay and nowhere else. The
  weighting and the tiny-n honesty rules live in :mod:`legba.data.correctness_axis`
  so this handler, the eval scoreboard, the scorecard, the v3 route and the GEPA
  gate all read ONE definition.
* **SECONDARY (diagnostic) — source-id overlap vs ``unit_reference_labels``**
  (mig 0057): the deterministic, LLM-free, $0 recall metric specified below. It
  is KEPT, not retired — the math is correct, it costs nothing, and it goes live
  the instant anyone writes a reference label for a live unit. But it is no
  longer the headline: the table holds ONE row, for a retired analyst, with zero
  ``canonical_source_ids``, so by the handler's own null rule it has reported
  ``None`` every day of its life and would have gone on doing so forever.

The two axes are NEVER pooled or averaged (standing rule, ``labels_api`` P2-5).
They answer different questions with different evidence: one is a human's read of
the prose, the other is set overlap on provenance ids. They are reported in
separate keys, with separate n, and either can be null while the other is not.

THE SECONDARY METRIC — Source-ID Overlap, canonical-source RECALL
-----------------------------------------------------------------

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
    sub_handler          "unit_correctness_scorer"
    units                {unit: per-unit record}
    correctness_operator float | None  (FLEET headline, all verdicts pooled once)
    operator_fleet       the full :func:`correctness_axis.score` record
    scored_unit_count    int  (units with a non-None SECONDARY correctness)
    operator_scored_unit_count  int  (units with any scored operator verdict)
    total_gold_labels    int  (``unit_reference_labels`` rows — the secondary axis)
    total_operator_labels int (``correctness_labels`` rows — the primary axis)
    lookback_days        int
    warnings             [str]

Per-unit record:
    unit                        str
    faithfulness                float | None   (mean faithfulness critique score)
    faithfulness_population     {judge_pipeline_version, judge_pipeline_versions,
                                 pooling, n_scored, excluded_other_pipeline,
                                 prior_populations}
    correctness_operator        float | None   (PRIMARY axis; None = no verdicts)
    n_operator_labels           int   (verdicts incl. unresolvable)
    n_operator_scored           int   (verdicts that entered the mean)
    operator_sufficient         bool  (n_operator_scored >= the floor)
    operator_mix                {label: count}  (the tiny-n display)
    operator_status             str
    correctness_vs_reference    float | None   (SECONDARY/diagnostic axis)
    n_labeled                   int   (gold label ROWS for the unit)
    n_findings                  int   (head findings for the unit in-window)
    correctness_citations_only  float | None   (DIAGNOSTIC)
    jaccard_diagnostic          float | None   (DIAGNOSTIC)
    labeled_target_count        int   (distinct targets with >=1 label)
    scored_target_count         int   (len(scored))
    per_target                  {target: {match, best_label_id, intersection_size,
                                          gold_size, cited_size, jaccard,
                                          match_citations_only, reason?}}
    status                      str   (SECONDARY axis status)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from uuid import UUID

from ... import correctness_axis
from ...provenance.judge_pipeline_version import (
    METRIC_FAITHFULNESS_SCORE,
    poolable_stamps,
)
from ...provenance.models import FindingPayload
from ...provenance.verify import JUDGE_PIPELINE_VERSION
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
#
# 2026-08-02 (P3 §5a) — ...and NOT an unrelated JUDGE either. A mean over a
# window that straddles a judge swap describes a population that never existed:
# swapping the grading model on 07-30 20:14Z moved mean faithfulness +7pp on
# its own, which would read here as a unit that got better overnight. The
# `judge_pipeline_version` stamp was built for exactly this and had no reader.
# The row's `data` column is the whole CritiquePayload dump, so the stamp sits
# one level down at `data.data.verification`.
#
# 2026-08-29 — LINEAGE-AWARE POOLING, and it applies HERE for the same reason it
# applies to band calibration and by the identical argument: the number this
# module reports IS mean `faithfulness_score`, so `faithfulness_score` is
# exactly the metric family `STAMP_EXPECTED_SHIFTS` characterises. Where the
# lineage AFFIRMATIVELY declares that a boundary cannot move the score — a pure
# hard->soft demotion train, in 2026-08-20/1's own words "the demotion train
# never moves the score, only the severity label" — a mean across it is not a
# mean across two instruments, and excluding it buys no honesty and costs real
# n. The campaign readout measured what the strict filter cost here: 573 of
# 26,949 faithfulness critiques usable, per-unit n ~19-32
# (CAMPAIGN_2026-08-29/PREMISE_GRADING_LOOP.md A-7).
#
# The filter is therefore the POOL, not the single head stamp, computed by the
# same `poolable_stamps` this module's sibling uses so the two readouts can
# never disagree about where the boundary is. Two properties are deliberately
# preserved: a pool of one is byte-identical to the old behaviour (which is what
# it is at the current head — 2026-08-29/1 pools with nothing), and the
# exclusion counter and the prior-population rollup read the SAME pool as the
# headline, so "excluded" continues to mean exactly "not in the population above".
_FAITHFULNESS_SQL = """
    SELECT confidence
    FROM analyst_outputs
    WHERE kind = 'critique'
      AND analyst_id = $1
      AND title LIKE 'Faithfulness verify%'
      AND produced_at > NOW() - make_interval(days => $2)
      AND data->'data'->'verification'->>'judge_pipeline_version' = ANY($3::text[])
"""

# What the pipeline filter left out — reported, never silently dropped. The
# COALESCE keeps a NULL (pre-stamp) row counted as excluded rather than
# vanishing into SQL three-valued logic, exactly as before; only the comparison
# widened from one stamp to the pool.
_FAITHFULNESS_EXCLUDED_SQL = """
    SELECT count(*)::int AS n
    FROM analyst_outputs
    WHERE kind = 'critique'
      AND analyst_id = $1
      AND title LIKE 'Faithfulness verify%'
      AND produced_at > NOW() - make_interval(days => $2)
      AND NOT (COALESCE(
            data->'data'->'verification'->>'judge_pipeline_version', ''
          ) = ANY($3::text[]))
"""

# M-2 — the PRIOR populations, ANNOTATED rather than mixed. The current-stamp
# mean above is the headline; every superseded (or pre-stamp) pipeline gets its
# OWN mean and its OWN n beside it, so the reader can see that the number moved
# when the judge changed WITHOUT the two ever being averaged together. A NULL
# stamp is a real population too — everything graded before the split key
# existed — and is labelled as such rather than dropped.
_FAITHFULNESS_PRIORS_SQL = """
    SELECT data->'data'->'verification'->>'judge_pipeline_version' AS version,
           count(*)::int AS n_scored,
           avg(confidence)::float8 AS mean_faithfulness
    FROM analyst_outputs
    WHERE kind = 'critique'
      AND analyst_id = $1
      AND title LIKE 'Faithfulness verify%'
      AND produced_at > NOW() - make_interval(days => $2)
      AND NOT (COALESCE(
            data->'data'->'verification'->>'judge_pipeline_version', ''
          ) = ANY($3::text[]))
    GROUP BY 1
    ORDER BY n_scored DESC
"""

# The PRIMARY axis pull — every operator verdict, one round trip for the whole
# fleet (the table is small by construction: it is hand-labelled). Grouping and
# weighting happen in :mod:`legba.data.correctness_axis` so exactly one
# implementation of the weights exists.
_OPERATOR_LABELS_SQL = correctness_axis.UNIT_LABELS_SQL

#: THE metric family this mean IS. Pooling is licensed for this family and no
#: other (see `_FAITHFULNESS_SQL`'s 2026-08-29 note).
CALIBRATED_METRIC_FAMILY = METRIC_FAITHFULNESS_SCORE

_POPULATION_NOTE = (
    "Faithfulness means cover ONE judge population — the lineage-declared POOL "
    "for the faithfulness_score family (judge_pipeline_versions below). Stamps "
    "join it only across boundaries their own lineage entry declares cannot "
    "move this family; any declared shift is a hard stop and an unregistered "
    "stamp pools with nothing. Priors are reported beside the headline with "
    "their own n and NEVER averaged into it — a mean across a judge swap "
    "describes a population that never existed."
)


async def _pull_unit(
    conn: Any, unit: str, lookback_days: int
) -> tuple[
    dict[Any, dict[str, set[UUID]]],
    int,
    dict[Any, list[dict[str, Any]]],
    int,
    float | None,
    dict[str, Any],
]:
    """Pull one unit's latest-head findings (by target), gold labels (by target),
    and mean faithfulness FOR THE CURRENT JUDGE PIPELINE. Returns
    ``(findings_by_target, n_findings, labels_by_target, n_labeled,
    faithfulness, faithfulness_population)``.

    The last element is the honest boundary on the faithfulness mean: which
    judge pipeline it covers, how many critiques it averaged, and how many were
    excluded as pre-stamp or superseded-judge rather than pooled across a swap
    (P3 §5a)."""
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

    # ONE pool, computed once and handed to all three reads, so the headline,
    # the exclusion counter and the priors can never partition differently.
    pool = list(poolable_stamps(JUDGE_PIPELINE_VERSION, CALIBRATED_METRIC_FAMILY))
    faith_rows = await conn.fetch(_FAITHFULNESS_SQL, unit, lookback_days, pool)
    faithfulness = _mean([row["confidence"] for row in faith_rows])
    excluded_row = await conn.fetchrow(
        _FAITHFULNESS_EXCLUDED_SQL, unit, lookback_days, pool
    )
    prior_rows = await conn.fetch(_FAITHFULNESS_PRIORS_SQL, unit, lookback_days, pool)
    faithfulness_population = {
        # The head stamp — what a critique graded right now carries.
        "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
        # The population actually averaged. A single-element list means the head
        # pooled with nothing; it must stay visible as that, never as a pool.
        "judge_pipeline_versions": pool,
        "pooling": {
            "metric_family": CALIBRATED_METRIC_FAMILY,
            "stamps": pool,
            "stamp_count": len(pool),
            "widened_by": len(pool) - 1,
        },
        "n_scored": len(faith_rows),
        "excluded_other_pipeline": (
            int(excluded_row["n"]) if excluded_row else 0
        ),
        # M-2 — priors ANNOTATED, never mixed.
        "prior_populations": [
            {
                "judge_pipeline_version": row["version"],
                "pre_stamp": row["version"] is None,
                "n_scored": int(row["n_scored"] or 0),
                "faithfulness": (
                    float(row["mean_faithfulness"])
                    if row["mean_faithfulness"] is not None
                    else None
                ),
            }
            for row in prior_rows
        ],
        "note": _POPULATION_NOTE,
    }

    return (
        findings_by_target,
        n_findings,
        labels_by_target,
        n_labeled,
        faithfulness,
        faithfulness_population,
    )


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    records: list[dict[str, Any]],
    *,
    lookback_days: int,
    warnings: list[str],
    operator_fleet: dict[str, Any],
) -> FindingPayload:
    scored_units = [r for r in records if r["correctness_vs_reference"] is not None]
    op_scored_units = [r for r in records if r["n_operator_scored"] > 0]
    total_gold = sum(int(r["n_labeled"]) for r in records)
    total_operator = sum(int(r["n_operator_labels"]) for r in records)

    # THE HEADLINE is the PRIMARY (operator) axis. The secondary source-overlap
    # axis moved into the body when M-1 rewired this handler — it had reported
    # `None` every day of its life, so leading with it hid the one number the
    # gold-set loop actually produced.
    if operator_fleet.get("correctness") is not None:
        head = (
            "Unit correctness (operator gold set): "
            f"{correctness_axis.describe(operator_fleet)} across "
            f"{len(op_scored_units)}/{len(records)} units"
        )
    else:
        head = (
            "Unit correctness (operator gold set): no scorable operator "
            f"verdicts — {operator_fleet.get('status')} (honest null)"
        )

    body_lines = [
        f"PRIMARY axis (operator gold set, judge-independent, never pooled):",
        f"  fleet: {correctness_axis.describe(operator_fleet)}",
    ]
    body_lines += [
        (
            f"  {r['unit']}: correctness_operator={r['correctness_operator']} "
            f"n_scored={r['n_operator_scored']}/{r['n_operator_labels']} "
            f"sufficient={r['operator_sufficient']} "
            f"mix={r['operator_mix']} status={r['operator_status']}"
        )
        for r in records
    ]
    body_lines.append(
        "SECONDARY axis (deterministic source-id overlap vs "
        "unit_reference_labels — DIAGNOSTIC, never pooled with the above):"
    )
    body_lines += [
        (
            f"  {r['unit']}: correctness={r['correctness_vs_reference']} "
            f"faithfulness={r['faithfulness']} "
            f"n_labeled={r['n_labeled']} n_findings={r['n_findings']} "
            f"scored={r['scored_target_count']}/{r['labeled_target_count']} "
            f"jaccard={r['jaccard_diagnostic']} "
            f"citations_only={r['correctness_citations_only']} "
            f"status={r['status']}"
        )
        for r in records
    ]
    body_lines.append(f"population: {_POPULATION_NOTE}")

    tags = ["deterministic", "unit_correctness_scorer"]
    if not op_scored_units:
        # HONESTY tag: the PRIMARY axis is null for every unit — say WHY.
        tags.append(
            "unit_correctness_no_operator_labels" if total_operator == 0
            else "unit_correctness_operator_unscorable"
        )
    elif not operator_fleet.get("sufficient"):
        # The number exists but is below the floor — flagged so a downstream
        # reader cannot mistake an indicative n=8 for a measured rate.
        tags.append("unit_correctness_operator_tiny_n")
    if not scored_units:
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
            # PRIMARY axis, fleet-level: scored over EVERY verdict at once, not
            # a mean of per-unit means (most units carry n=1 and a mean of means
            # would weight one verdict like a fully-labelled unit).
            "correctness_operator": operator_fleet.get("correctness"),
            "operator_fleet": operator_fleet,
            "operator_scored_unit_count": len(op_scored_units),
            "total_operator_labels": total_operator,
            # SECONDARY axis.
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
    everything is pulled from the substrate. ``deps`` (or its pool) being None
    degrades to the HONEST empty result on BOTH axes — every unit reports
    ``correctness_operator`` None with ``operator_status='no operator verdicts'``
    and ``correctness_vs_reference`` None with ``status='no gold labels'`` — NOT
    a stub."""
    units = tuple(options.get("units") or _DEFAULT_UNITS)
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    warnings: list[str] = []
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    # PRIMARY axis — one round trip for the whole fleet (a hand-labelled table
    # is small by construction). A failure here degrades to the honest empty
    # axis (every unit reports `no operator verdicts`), never a stub.
    operator_by_unit: dict[str, dict[str, Any]] = {}
    operator_fleet: dict[str, Any] = correctness_axis.score(
        (), min_labels=correctness_axis.MIN_FLEET_LABELS
    )
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                op_rows = await conn.fetch(_OPERATOR_LABELS_SQL)
            operator_by_unit, operator_fleet = correctness_axis.score_by_unit(
                op_rows
            )
        except Exception as exc:  # noqa: BLE001 — never break the sweep
            logger.warning("unit_correctness_scorer.operator_pull_failed err=%s", exc)
            warnings.append("unit_correctness_scorer.operator_pull_failed")

    records: list[dict[str, Any]] = []
    for unit in units:
        findings_by_target: dict[Any, dict[str, set[UUID]]] = {}
        labels_by_target: dict[Any, list[dict[str, Any]]] = {}
        n_findings = 0
        n_labeled = 0
        faithfulness: float | None = None
        # The failed-pull shape must carry the SAME keys as the measured one, or
        # a reader has to special-case an outage. It names the pipeline (and now
        # the pool it WOULD have read) with n_scored=0 — never an implied
        # measured zero.
        _failed_pull_pool = list(
            poolable_stamps(JUDGE_PIPELINE_VERSION, CALIBRATED_METRIC_FAMILY)
        )
        faithfulness_population: dict[str, Any] = {
            "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
            "judge_pipeline_versions": _failed_pull_pool,
            "pooling": {
                "metric_family": CALIBRATED_METRIC_FAMILY,
                "stamps": _failed_pull_pool,
                "stamp_count": len(_failed_pull_pool),
                "widened_by": len(_failed_pull_pool) - 1,
            },
            "n_scored": 0,
            "excluded_other_pipeline": 0,
            "prior_populations": [],
            "note": _POPULATION_NOTE,
        }
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    (
                        findings_by_target,
                        n_findings,
                        labels_by_target,
                        n_labeled,
                        faithfulness,
                        faithfulness_population,
                    ) = await _pull_unit(conn, unit, lookback_days)
            except Exception as exc:  # noqa: BLE001 — never break the sweep
                logger.warning(
                    "unit_correctness_scorer.pull_failed unit=%s err=%s", unit, exc
                )
                warnings.append(f"unit_correctness_scorer.pull_failed unit={unit}")

        scored = score_unit(findings_by_target, labels_by_target)
        op_record = operator_by_unit.get(unit) or correctness_axis.score(
            (), min_labels=correctness_axis.MIN_UNIT_LABELS
        )
        records.append({
            "unit": unit,
            "faithfulness": faithfulness,
            # WHICH judge graded that mean, and what was excluded to keep it one
            # population — a mean pooled across a judge swap is not a mean of
            # anything (P3 §5a). Never a bare number without its boundary.
            "faithfulness_population": faithfulness_population,
            # PRIMARY AXIS — the operator gold set. Judge-independent, reported
            # with its n and its verdict mix ALWAYS (tiny-n rule), and never
            # pooled with faithfulness or with the secondary axis below.
            **correctness_axis.as_payload(op_record),
            # SECONDARY (diagnostic) AXIS — deterministic source-id overlap.
            # None per the null rule whenever nothing is scorable; a real 0.0
            # only when a finding cited NONE of the canonical evidence for its
            # best label.
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
        records,
        lookback_days=lookback_days,
        warnings=warnings,
        operator_fleet=operator_fleet,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]
