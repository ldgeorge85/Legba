# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-3 — THE VALIDITY HARNESS. Nothing is trusted until three arms pass.

The shipped coverage-floor detector went live at outcome precision 2 of 3 and
SAID SO. This instrument says so BEFORE it is quoted: the three arms below are
pre-registered, their bars live in CODE as :func:`instrument_status`, and every
consumer reads that status rather than the raw ratio.

  **V1 — the permuted control** (continuous, ``$0``, every sweep). Re-run diff
  (b) with each reference swapped against a DIFFERENT target's desk of the same
  unit. A reference about Ukraine scored against the Saudi energy desk should
  almost never look "engaged". Bar: permuted ``attention_rate`` <=
  :data:`V1_PERMUTED_BAR` (0.10). Above it, the matcher is matching BOILERPLATE
  — desks share vocabulary, and a matcher keying on "sanctions" or "escalation"
  would score everything against everything — and the instrument disarms itself
  that day. One extra Python pass over rows already fetched: the single cheapest
  honesty arm in the design.

  **V2 — the reference's own precision** (weekly, n=20). *Is this a real
  development, in this window, on this bounded question, for this country?*
  Bar: >= :data:`V2_PRECISION_BAR` (0.85). Below it the instrument is measuring
  the reference model's imagination and everything downstream declines.

  **V3 — the attention verdict's precision and recall** (weekly, n=40, blind).
  *Did this desk engage this story?* Bars: precision >= 0.80 on the GAP class,
  recall >= 0.70. The precedent for setting a bar honestly and low is the
  coverage floor's own 2-of-3.

WHERE THE GRADES LIVE, AND THE SCHEMA CONSTRAINT THAT SHAPES IT
---------------------------------------------------------------
``correctness_labels`` (migration 0096) already carries ``unit_analyst_id``,
``target_id``, ``label``, ``rationale``, ``labeled_by`` and
``finding_snapshot``. One labelling surface for the correctness loop and this
instrument, no new table.

It also carries ``UNIQUE (finding_id)`` and ``label NOT NULL`` with a four-value
CHECK. Both are load-bearing here and the sampler is built around them rather
than against them:

  * ONE sample row per desk head per pipeline version, carrying BOTH arms'
    payload under ``finding_snapshot`` — never one row per arm, which the unique
    index would reject as soon as a head was drawn for V2 and V3 both;
  * the placeholder label is ``unresolvable``, which is the honest value for an
    UNGRADED sample and is the one label :mod:`legba.data.correctness_axis`
    already excludes from its mean;
  * ``ON CONFLICT (finding_id) DO NOTHING`` — a sampler must NEVER displace an
    operator's real grade.

THE F-2 GATE APPLIES HERE TOO. Sample rows land under
:data:`SAMPLE_LABELED_BY_PREFIX`, which ``correctness_axis.UNIT_LABELS_SQL``
excludes, so the sampling frame can never be read as operator verdicts. A row
becomes a GRADE when a human (or an operator-invoked frontier model — legitimate
here and only here, because it is operator-invoked rather than scheduled)
overwrites ``label`` and ``labeled_by``. That is the same transition the weekly
labelling loop already performs.

A STALE GRADE NEVER COUNTS AS A PASS
------------------------------------
A grade whose ``finding_snapshot.pipeline_version`` is not the running one, or
whose ``labeled_at`` is older than :data:`MAX_GRADE_AGE_DAYS`, is dropped from
its arm. If that empties an arm the status is ``stale`` — deliberately DISTINCT
from ``unvalidated`` ("never graded"), because "we measured this and the
measurement expired" and "we never measured this" call for different actions.
Neither is ``valid``, and only ``valid`` licenses a number to be quoted or a
page to be sent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from ... import correctness_axis

logger = logging.getLogger(__name__)

# --- the pre-registered bars, in code -------------------------------------
#: V1: above this, the matcher is matching boilerplate and the day disarms.
V1_PERMUTED_BAR = 0.10
#: V2: the reference's own precision floor.
V2_PRECISION_BAR = 0.85
#: V3: precision on the GAP class, and recall of real gaps.
V3_PRECISION_BAR = 0.80
V3_RECALL_BAR = 0.70

#: Minimum graded n before an arm reports at all. Below it the arm is
#: ``unvalidated``, never "passing on n=2" — the tiny-n rule the operator axis
#: already enforces.
MIN_GRADED_V2 = 10
MIN_GRADED_V3 = 20

#: A grade older than this no longer describes the running instrument.
MAX_GRADE_AGE_DAYS = 30

#: The sampling frame's ``labeled_by`` prefix. Excluded by the F-2 clause in
#: ``correctness_axis.UNIT_LABELS_SQL`` so an ungraded sample can never be read
#: as an operator verdict.
SAMPLE_LABELED_BY_PREFIX = "desk_reference_sample/"

#: The marker every row this harness writes carries, and the key both readers
#: join on.
INSTRUMENT_MARKER = "desk_reference"

#: Sampling caps (design §2.5: 20 items/week for V2, 40 verdicts for V3). One
#: row serves both arms, so the row cap is the larger of the two.
DEFAULT_SAMPLE_SIZE = 40

#: Instrument statuses. Only ``valid`` licenses a quoted number or a page.
STATUS_VALID = "valid"
STATUS_UNVALIDATED = "unvalidated"
STATUS_STALE = "stale"


# ---------------------------------------------------------------------------
# V1 — the permuted control
# ---------------------------------------------------------------------------


def permute_references(
    references: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], str]]:
    """Pair each reference with a DIFFERENT target's desk of the SAME unit.

    Same-unit is the point: a control that swapped units too would be trivially
    easy to pass, because an energy reference against an escalation desk shares
    less vocabulary than two escalation desks do. The hard case — and the one
    that would expose a boilerplate matcher — is Ukraine's escalation reference
    against Saudi Arabia's escalation desk, where both heads talk about
    "escalation risk" in the same words.

    A cyclic shift by one within each unit group, over a sorted list: pure,
    replayable from the row set alone, and it visits every reference exactly
    once. A unit with only one reference contributes nothing (there is no other
    desk to swap with) and is silently absent rather than paired with itself.
    """
    by_unit: dict[str, list[Mapping[str, Any]]] = {}
    for ref in references:
        by_unit.setdefault(str(ref.get("unit") or ""), []).append(ref)
    out: list[tuple[Mapping[str, Any], str]] = []
    for unit in sorted(by_unit):
        group = sorted(by_unit[unit], key=lambda r: str(r.get("target_id") or ""))
        if len(group) < 2:
            continue
        for i, ref in enumerate(group):
            other = group[(i + 1) % len(group)]
            out.append((ref, str(other.get("target_id") or "")))
    return out


def permuted_attention_rate(
    references: Sequence[Mapping[str, Any]],
    *,
    home_by_target: Mapping[str, str],
    slices: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    heads: Mapping[tuple[str, str], Mapping[str, Any]],
    frames: Mapping[str, Sequence[str]],
) -> tuple[float | None, int]:
    """Re-run diff (b) on the permuted pairing. Returns ``(rate, n_pairs)``.

    Pooled by SUMMING both sides rather than averaging per-pair rates, for the
    same reason the operator axis pools verdicts: a pair with one collected item
    must not weigh as much as a pair with five. ``None`` when nothing was
    scorable — an empty control is not a passing control, and
    :func:`instrument_status` treats it as such.
    """
    from ._reference_diff import SCORABLE_STATUSES, score_pair

    engaged = collected = 0
    n_pairs = 0
    for ref, other_target in permute_references(references):
        if str(ref.get("status") or "") not in SCORABLE_STATUSES:
            continue
        unit = str(ref.get("unit") or "")
        key = (other_target, unit)
        head = heads.get(key) or {}
        record = score_pair(
            items=list(ref.get("items") or []),
            status=str(ref.get("status") or ""),
            # The OTHER desk's home country: an item naming Saudi Arabia is
            # unanchorable against the Saudi desk, exactly as it would be for a
            # real reference there. Using the ORIGINAL home blob would leave the
            # control easier than the thing it controls.
            home_blob=home_by_target.get(other_target, ""),
            slice_rows=slices.get(key, []),
            head_prose=str(head.get("prose") or ""),
            head_cited=head.get("cited") or (),
            frame_names=frames.get(other_target, []),
        )
        n_pairs += 1
        engaged += int(record["n_engaged"])
        collected += int(record["n_collected"])
    if collected <= 0:
        return None, n_pairs
    return engaged / collected, n_pairs


# ---------------------------------------------------------------------------
# V2 / V3 — the graded arms
# ---------------------------------------------------------------------------

#: Every row this harness has ever written or a grader has ever touched, for the
#: running pipeline family. The ``finding_snapshot`` marker is the join key: it
#: is what distinguishes an attention-instrument sample from the weekly
#: correctness loop's own rows in the same table.
_GRADES_SQL = """
SELECT id, finding_id, unit_analyst_id, target_id, label, labeled_by,
       labeled_at, finding_snapshot
  FROM correctness_labels
 WHERE finding_snapshot ->> 'instrument' = $1
 ORDER BY labeled_at DESC
 LIMIT $2
"""

_INSERT_SAMPLE_SQL = """
INSERT INTO correctness_labels (
    id, finding_id, unit_analyst_id, target_id, label, rationale,
    labeled_by, finding_snapshot
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
ON CONFLICT (finding_id) DO NOTHING
RETURNING id
"""

_MAX_GRADE_ROWS = 2_000


def _is_sample_row(row: Mapping[str, Any]) -> bool:
    """True while a row is still the SAMPLING FRAME — nobody has graded it."""
    return str(row.get("labeled_by") or "").startswith(SAMPLE_LABELED_BY_PREFIX)


def _grade_is_current(
    row: Mapping[str, Any], *, pipeline_version: str, now: datetime
) -> bool:
    """A grade counts only while it describes the RUNNING instrument.

    Two ways to stop: the snapshot names a different ``pipeline_version`` (the
    query table, the system prompt or the fence changed under it), or it is
    older than :data:`MAX_GRADE_AGE_DAYS`. Either way it is dropped from its
    arm — a stale grade never counts as a pass.
    """
    snapshot = row.get("finding_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, Mapping):
        return False
    if str(snapshot.get("pipeline_version") or "") != pipeline_version:
        return False
    labeled_at = row.get("labeled_at")
    if not isinstance(labeled_at, datetime):
        return False
    if labeled_at.tzinfo is None:
        labeled_at = labeled_at.replace(tzinfo=timezone.utc)
    return (now - labeled_at) <= timedelta(days=MAX_GRADE_AGE_DAYS)


def _snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = row.get("finding_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    return dict(snapshot) if isinstance(snapshot, Mapping) else {}


def v2_precision(graded: Sequence[Mapping[str, Any]]) -> tuple[float | None, int]:
    """*Is this a real development, on this question, in this window?*

    Scored with :mod:`legba.data.correctness_axis`'s OWN weights (correct 1.0,
    partially_correct 0.5, incorrect 0.0, ``unresolvable`` excluded) so this
    number and the operator correctness number mean the same thing by the same
    arithmetic. They are still never POOLED — different evidence, different
    question, separate keys.
    """
    labels = [str(r.get("label") or "") for r in graded]
    record = correctness_axis.score(labels, min_labels=MIN_GRADED_V2)
    return record.get("correctness"), int(record.get("n_scored") or 0)


def v3_precision_recall(
    graded: Sequence[Mapping[str, Any]],
) -> tuple[float | None, float | None, int]:
    """Precision on the GAP class, and recall of real gaps.

    Each graded row carries the instrument's own verdict in
    ``finding_snapshot.v3.engaged`` and a human's verdict in ``label``:
    ``correct`` means the instrument was right about this desk-item,
    ``incorrect`` means it was wrong. From that 2x2:

      * the instrument PREDICTS a gap when ``engaged is False``;
      * a predicted gap the human confirmed (``correct``) is a TRUE POSITIVE;
      * a predicted gap the human overturned (``incorrect``) is a FALSE
        POSITIVE — the desk did engage and we said it did not;
      * an ``engaged=True`` verdict the human overturned is a FALSE NEGATIVE —
        a real gap the instrument missed.

    ``precision = TP / (TP + FP)``, ``recall = TP / (TP + FN)``. Both ``None``
    when their denominator is empty; ``partially_correct`` and ``unresolvable``
    are excluded from both, because a half-verdict about a binary claim settles
    neither cell.
    """
    tp = fp = fn = 0
    n = 0
    for row in graded:
        label = str(row.get("label") or "")
        if label not in (
            correctness_axis.LABEL_CORRECT, correctness_axis.LABEL_INCORRECT
        ):
            continue
        arm = _snapshot(row).get("v3") or {}
        if "engaged" not in arm:
            continue
        n += 1
        predicted_gap = not bool(arm.get("engaged"))
        right = label == correctness_axis.LABEL_CORRECT
        if predicted_gap and right:
            tp += 1
        elif predicted_gap and not right:
            fp += 1
        elif not predicted_gap and not right:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return precision, recall, n


# ---------------------------------------------------------------------------
# The status
# ---------------------------------------------------------------------------


def instrument_status(
    *,
    permuted: float | None,
    v2: float | None,
    v2_n: int,
    v3_precision: float | None,
    v3_recall: float | None,
    v3_n: int,
    had_stale_grades: bool,
) -> tuple[str, list[str]]:
    """``(status, reasons)`` — ``valid`` only when ALL THREE arms pass.

    The ordering is deliberate. V1 is checked FIRST and is a hard stop even
    when V2 and V3 pass: a permuted control above its bar means the matcher is
    scoring boilerplate, which makes every other arm's grade a grade of the
    wrong thing.

    ``stale`` is reported in preference to ``unvalidated`` when an arm is empty
    ONLY because its grades expired — "we measured this and the measurement
    expired" and "we never measured this" call for different actions, and
    collapsing them would hide a lapsed validation behind a never-started one.
    """
    reasons: list[str] = []
    if permuted is None:
        reasons.append("V1: permuted control produced no scorable pair")
    elif permuted > V1_PERMUTED_BAR:
        reasons.append(
            f"V1: permuted attention_rate {permuted:.3f} > {V1_PERMUTED_BAR} — "
            "the matcher is matching boilerplate; the instrument disarms"
        )
    if v2 is None or v2_n < MIN_GRADED_V2:
        reasons.append(f"V2: {v2_n} graded reference item(s) < {MIN_GRADED_V2}")
    elif v2 < V2_PRECISION_BAR:
        reasons.append(f"V2: reference precision {v2:.3f} < {V2_PRECISION_BAR}")
    if v3_n < MIN_GRADED_V3:
        reasons.append(f"V3: {v3_n} graded verdict(s) < {MIN_GRADED_V3}")
    else:
        if v3_precision is None or v3_precision < V3_PRECISION_BAR:
            reasons.append(
                f"V3: gap-class precision {v3_precision} < {V3_PRECISION_BAR}"
            )
        if v3_recall is None or v3_recall < V3_RECALL_BAR:
            reasons.append(f"V3: gap recall {v3_recall} < {V3_RECALL_BAR}")

    if not reasons:
        return STATUS_VALID, []
    if had_stale_grades and any(r.startswith(("V2:", "V3:")) for r in reasons):
        return STATUS_STALE, reasons
    return STATUS_UNVALIDATED, reasons


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------


def choose_sample(
    scored_pairs: Sequence[Mapping[str, Any]],
    *,
    day_key: str,
    size: int,
) -> list[Mapping[str, Any]]:
    """A deterministic, unit-STRATIFIED sample of scored pairs.

    Stratified so the graded set cannot end up all-escalation: pairs are taken
    round-robin across units, and within a unit ordered by a date-seeded digest
    of the pair key. Seeded by the UTC DATE rather than an RNG, so the sample a
    run drew is replayable from its own timestamp — the ``rotate_desks`` rule.
    """
    by_unit: dict[str, list[Mapping[str, Any]]] = {}
    for pair in scored_pairs:
        by_unit.setdefault(str(pair.get("unit") or ""), []).append(pair)
    for unit in by_unit:
        by_unit[unit].sort(
            key=lambda p: hashlib.sha256(
                f"{day_key}|{p.get('target_id')}|{p.get('unit')}".encode()
            ).hexdigest()
        )
    out: list[Mapping[str, Any]] = []
    units = sorted(by_unit)
    depth = 0
    while len(out) < size and units:
        progressed = False
        for unit in units:
            if depth < len(by_unit[unit]):
                out.append(by_unit[unit][depth])
                progressed = True
                if len(out) >= size:
                    break
        if not progressed:
            break
        depth += 1
    return out


def build_sample_snapshot(
    pair: Mapping[str, Any], *, pipeline_version: str, sampled_at: datetime
) -> dict[str, Any]:
    """The ``finding_snapshot`` a grader reads.

    Carries BOTH arms because ``correctness_labels`` is ``UNIQUE (finding_id)``
    and one desk head can therefore hold exactly one row: V2's question (are the
    reference items real?) and V3's question (did this desk engage this one?)
    ride together, each with the instrument's own verdict beside it so the human
    is grading a stated claim rather than reconstructing one.
    """
    items = list(pair.get("uncollected") or [])
    return {
        "instrument": INSTRUMENT_MARKER,
        "pipeline_version": pipeline_version,
        "sampled_at": sampled_at.isoformat(),
        "target_id": pair.get("target_id"),
        "unit": pair.get("unit"),
        "v2": {
            "question": (
                "Is each reference item a real development, in this window, on "
                "this unit's bounded question, for this country?"
            ),
            "n_items": pair.get("n_items"),
            "n_anchorable": pair.get("n_anchorable"),
            "items_sample": items[:5],
        },
        "v3": {
            "question": "Did this desk engage this story?",
            # The instrument's own verdict, stated. A blind grader is shown the
            # item and the head; this field is what the grade is scored against.
            "engaged": bool(pair.get("n_engaged")),
            "n_collected": pair.get("n_collected"),
            "n_engaged": pair.get("n_engaged"),
            "attention_rate": pair.get("attention_rate"),
            "arms": dict(pair.get("attention_arms") or {}),
        },
    }


async def write_sample_rows(
    conn: Any,
    sample: Sequence[Mapping[str, Any]],
    *,
    pipeline_version: str,
    now: datetime,
) -> tuple[int, int]:
    """Write the sampling frame. Returns ``(written, skipped)``.

    ``ON CONFLICT (finding_id) DO NOTHING`` — a head that already carries a
    label (an operator's real verdict, or an earlier sample) is left alone. A
    sampler that overwrote an operator grade would destroy the only
    judge-independent signal this platform has.
    """
    written = skipped = 0
    for pair in sample:
        finding_id = pair.get("output_id")
        if not isinstance(finding_id, UUID):
            try:
                finding_id = UUID(str(finding_id))
            except (TypeError, ValueError, AttributeError):
                skipped += 1
                continue
        snapshot = build_sample_snapshot(
            pair, pipeline_version=pipeline_version, sampled_at=now
        )
        try:
            row = await conn.fetchrow(
                _INSERT_SAMPLE_SQL,
                uuid4(),
                finding_id,
                str(pair.get("unit") or ""),
                str(pair.get("target_id") or "") or None,
                # The honest placeholder for an UNGRADED sample, and the one
                # label the correctness axis already excludes from its mean.
                correctness_axis.LABEL_UNRESOLVABLE,
                (
                    "Attention-instrument sampling frame (A-3). NOT a verdict: "
                    "a grader overwrites `label` and `labeled_by` to record one."
                ),
                f"{SAMPLE_LABELED_BY_PREFIX}{pipeline_version}",
                json.dumps(snapshot),
            )
        except Exception as exc:  # noqa: BLE001 — never break the sweep
            logger.warning("reference_validity.sample_write_failed err=%s", exc)
            skipped += 1
            continue
        if row is None:
            skipped += 1
        else:
            written += 1
    return written, skipped


# ---------------------------------------------------------------------------
# The entry point the diff calls
# ---------------------------------------------------------------------------


async def assess_instrument(
    conn: Any,
    *,
    scored_pairs: Sequence[Mapping[str, Any]],
    references: Sequence[Mapping[str, Any]],
    home_by_target: Mapping[str, str],
    slices: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    heads: Mapping[tuple[str, str], Mapping[str, Any]],
    frames: Mapping[str, Sequence[str]],
    pipeline_version: str,
    degraded_pairs: int,
    now: datetime,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Run V1, read back V2/V3, refresh the sampling frame, decide the status.

    This is the whole of ``data.instrument``. It gates nothing in THIS train —
    A-5 (the ``attention_gap`` trigger class) is the consumer that will refuse
    to write when the status is not ``valid``, and it is deliberately not built
    here: no page from an ungraded instrument, and the instrument is ungraded
    until an operator has run V2/V3 at least once.
    """
    permuted, permuted_pairs = permuted_attention_rate(
        references,
        home_by_target=home_by_target,
        slices=slices,
        heads=heads,
        frames=frames,
    )

    graded_rows: list[Mapping[str, Any]] = []
    stale = 0
    try:
        rows = await conn.fetch(_GRADES_SQL, INSTRUMENT_MARKER, _MAX_GRADE_ROWS)
    except Exception as exc:  # noqa: BLE001 — a missing table takes the ARM
        logger.warning("reference_validity.grade_read_failed err=%s", exc)
        rows = []
    for row in rows:
        if _is_sample_row(row):
            continue  # still the sampling frame; nobody has graded it
        if not _grade_is_current(row, pipeline_version=pipeline_version, now=now):
            stale += 1
            continue
        graded_rows.append(row)

    v2_rows = [r for r in graded_rows if "v2" in _snapshot(r)]
    v3_rows = [r for r in graded_rows if "v3" in _snapshot(r)]
    v2_value, v2_n = v2_precision(v2_rows)
    v3_p, v3_r, v3_n = v3_precision_recall(v3_rows)

    status, reasons = instrument_status(
        permuted=permuted,
        v2=v2_value,
        v2_n=v2_n,
        v3_precision=v3_p,
        v3_recall=v3_r,
        v3_n=v3_n,
        had_stale_grades=bool(stale),
    )

    sample = choose_sample(
        scored_pairs, day_key=now.date().isoformat(), size=sample_size
    )
    written, skipped = await write_sample_rows(
        conn, sample, pipeline_version=pipeline_version, now=now
    )

    return {
        "status": status,
        "reasons": reasons,
        "permuted_attention_rate": permuted,
        "permuted_pairs": permuted_pairs,
        "permuted_bar": V1_PERMUTED_BAR,
        "v2_precision": v2_value,
        "v2_graded_n": v2_n,
        "v2_bar": V2_PRECISION_BAR,
        "v3_precision": v3_p,
        "v3_recall": v3_r,
        "v3_graded_n": v3_n,
        "v3_bars": {"precision": V3_PRECISION_BAR, "recall": V3_RECALL_BAR},
        "stale_grades": stale,
        "max_grade_age_days": MAX_GRADE_AGE_DAYS,
        "sample_rows_written": written,
        "sample_rows_skipped": skipped,
        "degraded_pairs": degraded_pairs,
        "pipeline_version": pipeline_version,
        "note": (
            "Only status='valid' licenses a quoted weekly number or a page. "
            "V2/V3 are operator-invoked (a human, or an operator-invoked "
            "frontier model) — they are never scheduled, and a stale grade "
            "never counts as a pass."
        ),
    }


__all__ = [
    "DEFAULT_SAMPLE_SIZE",
    "INSTRUMENT_MARKER",
    "MAX_GRADE_AGE_DAYS",
    "MIN_GRADED_V2",
    "MIN_GRADED_V3",
    "SAMPLE_LABELED_BY_PREFIX",
    "STATUS_STALE",
    "STATUS_UNVALIDATED",
    "STATUS_VALID",
    "V1_PERMUTED_BAR",
    "V2_PRECISION_BAR",
    "V3_PRECISION_BAR",
    "V3_RECALL_BAR",
    "assess_instrument",
    "build_sample_snapshot",
    "choose_sample",
    "instrument_status",
    "permute_references",
    "permuted_attention_rate",
    "v2_precision",
    "v3_precision_recall",
    "write_sample_rows",
]
