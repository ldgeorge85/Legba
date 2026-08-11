# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OPERATOR correctness axis — one definition, shared by every reader (M-1).

Legba measures two different things and has always been careful not to confuse
them:

* **Faithfulness** — "is the prose faithful to its own citations?" Produced by
  the verify gate's LLM judge. Split by ``judge_pipeline_version`` (M-2), because
  a mean across a judge swap describes a population that never existed.
* **Correctness** — "was the read RIGHT?" Judge-INDEPENDENT by construction: it
  compares the finding against something outside the pipeline.

Correctness in turn has two sources, and until 2026-08-03 the wrong one was
wired to the headline:

* ``unit_reference_labels`` (mig 0057) — operator-authored reference answers
  grounded to ``canonical_source_ids``; scored deterministically as source-id
  overlap recall by :mod:`..analysts.deterministic_handlers.unit_correctness_scorer`.
  **One row exists, for a retired analyst, with zero source ids.** The scorer has
  therefore reported ``None`` every day of its life, and GEPA's promotion gate
  counted that same empty table and was permanently degenerate.
* ``correctness_labels`` (mig 0096) — the weekly gold-set loop's per-finding
  operator verdicts. **This is the table that actually gets fed** (8 verdicts
  across 7 units, 2026-W30/W31) and the platform's ONLY judge-independent
  quality signal. It surfaced in exactly one API overlay and nowhere else.

This module is the single definition of the second axis, kept stdlib-only so the
deterministic handler, the registry's slim API image, and the GEPA worker can all
import the SAME arithmetic (the 2026-08-02 engine review found the weighting
written out in prose in ``labels_api`` and reimplemented inline in SQL — one
divergence away from two different "correctness" numbers).

THE WEIGHTING (unchanged from the ``labels_api`` contract, now enforced in code)
------------------------------------------------------------------------------
``correct`` 1.0 · ``partially_correct`` 0.5 · ``incorrect`` 0.0.
``unresolvable`` is a first-class honest state — the operator looked and could
not judge — and is excluded from BOTH the numerator and the denominator. It is
never dropped silently (it is reported in the mix and in ``n_unresolvable``) and
never scored as wrongness.

TINY-n (the honesty rule this axis lives or dies by)
----------------------------------------------------
The gold set is operator-labelled and does not scale by construction: n=8 total,
n=1 for most units. A per-unit mean over one verdict is not a measurement. So
every reader gets, always:

* the weighted mean — REPORTED even below the floor, because it is the only
  judge-independent signal that exists and hiding it is how it stayed invisible;
* ``n_scored`` and the full verdict ``mix`` — the raw evidence, so a reader sees
  "one 'correct'", not "1.00";
* ``sufficient`` — False below :data:`MIN_UNIT_LABELS` / :data:`MIN_FLEET_LABELS`;
* ``status`` — a sentence naming the n, so a number is never bare.

NO confidence interval is emitted. The vocabulary is three-valued, so a binomial
proportion interval would be arithmetically wrong here; the verdict mix is the
honest small-sample display.

NEVER POOLED (standing rule, ``labels_api.py`` P2-5)
-----------------------------------------------------
The operator axis is SEGREGATED from the deterministic source-overlap axis and
from every faithfulness aggregate. It is not averaged with them, not folded into
calibration, and not used to adjust a Brier or a band. Its keys are distinct
everywhere (``correctness_operator*``), it gets its OWN v3 route rather than a
section of ``/eval/calibration``, and :func:`assert_not_pooled` is wired into the
tests that guard the boundary.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: The closed label vocabulary (mirrors the ``correctness_labels`` CHECK).
LABEL_CORRECT = "correct"
LABEL_PARTIALLY_CORRECT = "partially_correct"
LABEL_INCORRECT = "incorrect"
LABEL_UNRESOLVABLE = "unresolvable"

VOCABULARY: tuple[str, ...] = (
    LABEL_CORRECT,
    LABEL_PARTIALLY_CORRECT,
    LABEL_INCORRECT,
    LABEL_UNRESOLVABLE,
)

#: The scoring weights. ``unresolvable`` is deliberately ABSENT — a label with no
#: weight is excluded from both numerator and denominator (see :func:`score`).
WEIGHTS: Mapping[str, float] = {
    LABEL_CORRECT: 1.0,
    LABEL_PARTIALLY_CORRECT: 0.5,
    LABEL_INCORRECT: 0.0,
}

#: Below this many SCORED verdicts a per-unit mean is reported but never called
#: measured. Chosen to match GEPA's long-standing ``_MIN_REFERENCE_LABELS`` floor
#: of 20 halved for a per-unit slice — the point is that it is well above today's
#: n=1, not that 10 is magic.
MIN_UNIT_LABELS = 10

#: The same floor for the cross-unit (fleet) aggregate. Today's fleet n is 8.
MIN_FLEET_LABELS = 30

#: The axis' machine-readable name, used as a key prefix and in receipts.
AXIS_NAME = "correctness_operator"

#: Keys this axis owns. Any aggregate that is NOT the operator correctness axis
#: must not carry them — see :func:`assert_not_pooled`.
AXIS_KEYS: tuple[str, ...] = (
    "correctness_operator",
    "n_operator_labels",
    "n_operator_scored",
    "operator_sufficient",
    "operator_mix",
)

_STATUS_NONE = "no operator verdicts"
_STATUS_ALL_UNRESOLVABLE = "all verdicts unresolvable — nothing scorable"


def _empty_mix() -> dict[str, int]:
    return {label: 0 for label in VOCABULARY}


def score(
    labels: Iterable[str],
    *,
    min_labels: int = MIN_UNIT_LABELS,
) -> dict[str, Any]:
    """Score one population of operator verdicts.

    ``labels`` is any iterable of label strings (a unit's rows, the whole fleet's
    rows, one desk's rows — the arithmetic is identical). Returns the axis record:

    ``correctness``       weighted mean over the SCORED verdicts, or ``None``
                          when nothing is scorable (never 0.0 — a real 0.0 means
                          every scorable verdict was ``incorrect``)
    ``n_labels``          every verdict seen, including ``unresolvable``
    ``n_scored``          verdicts that entered the mean
    ``n_unresolvable``    excluded from both numerator and denominator
    ``mix``               full per-label counts (the tiny-n display)
    ``sufficient``        ``n_scored >= min_labels``
    ``min_labels``        the floor applied, so the caller need not know it
    ``status``            a sentence naming the n — a number is never bare

    An UNKNOWN label (outside the vocabulary — only reachable if the DB CHECK is
    ever loosened) is counted into ``n_labels`` and into a ``mix`` entry of its
    own, but never into the mean: an unrecognised verdict is not a score.
    """
    mix = _empty_mix()
    weighted: list[float] = []
    n_labels = 0
    for raw in labels:
        label = str(raw)
        n_labels += 1
        mix[label] = mix.get(label, 0) + 1
        weight = WEIGHTS.get(label)
        if weight is not None:
            weighted.append(weight)

    n_scored = len(weighted)
    n_unresolvable = mix.get(LABEL_UNRESOLVABLE, 0)
    correctness = (sum(weighted) / n_scored) if n_scored else None
    sufficient = n_scored >= int(min_labels)

    if n_labels == 0:
        status = _STATUS_NONE
    elif n_scored == 0:
        status = _STATUS_ALL_UNRESOLVABLE
    elif sufficient:
        status = f"scored (n={n_scored})"
    else:
        status = (
            f"indicative only — n={n_scored} scored verdict"
            f"{'' if n_scored == 1 else 's'}, below the {int(min_labels)} floor"
        )

    return {
        "correctness": correctness,
        "n_labels": n_labels,
        "n_scored": n_scored,
        "n_unresolvable": n_unresolvable,
        "mix": mix,
        "sufficient": sufficient,
        "min_labels": int(min_labels),
        "status": status,
    }


def describe(record: Mapping[str, Any]) -> str:
    """One human line for a :func:`score` record — the tiny-n display.

    ``correctness 0.62 (n=8 scored: 3 correct / 4 partial / 1 incorrect) —
    indicative only — n=8 scored verdicts, below the 30 floor``

    Never emits a bare ratio: the mix and the status always travel with it.
    """
    value = record.get("correctness")
    if value is None:
        return f"correctness unmeasured — {record.get('status')}"
    mix = record.get("mix") or {}
    parts = [
        f"{int(mix.get(LABEL_CORRECT, 0))} correct",
        f"{int(mix.get(LABEL_PARTIALLY_CORRECT, 0))} partial",
        f"{int(mix.get(LABEL_INCORRECT, 0))} incorrect",
    ]
    n_unres = int(record.get("n_unresolvable") or 0)
    if n_unres:
        parts.append(f"{n_unres} unresolvable (excluded)")
    return (
        f"correctness {float(value):.2f} "
        f"(n={int(record.get('n_scored') or 0)} scored: {' / '.join(parts)}) — "
        f"{record.get('status')}"
    )


def as_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project a :func:`score` record onto the axis' PUBLIC key names.

    One place decides what the operator axis is called on the wire, so the
    scorer finding, the scorecard eval block, the eval scoreboard and the v3
    route cannot drift into three spellings of the same number.
    """
    return {
        "correctness_operator": record.get("correctness"),
        "n_operator_labels": int(record.get("n_labels") or 0),
        "n_operator_scored": int(record.get("n_scored") or 0),
        "operator_sufficient": bool(record.get("sufficient")),
        "operator_mix": dict(record.get("mix") or {}),
        "operator_status": record.get("status"),
    }


def assert_not_pooled(payload: Mapping[str, Any], *, what: str) -> None:
    """Raise if a NON-correctness aggregate carries an operator-axis key.

    The standing rule (``labels_api`` P2-5) is that the operator gold-set number
    is SEGREGATED — never pooled into faithfulness, the Brier plane, the band
    rates, or the deterministic source-overlap axis. Prose said so; nothing
    enforced it. This is the enforcement point, called from the tests that guard
    the boundary (and cheap enough to call from a writer that wants to be sure).
    """
    leaked = sorted(k for k in AXIS_KEYS if k in payload)
    if leaked:
        raise AssertionError(
            f"{what} carries operator-correctness key(s) {leaked}: the operator "
            "gold-set axis is judge-independent and must never be pooled into a "
            "faithfulness / calibration aggregate (labels_api P2-5)."
        )


#: Per-unit aggregate over ``correctness_labels``. Returns one row per unit with
#: its raw verdict counts; the WEIGHTING is applied in Python by :func:`score` so
#: there is exactly one implementation of it (the review found the weights
#: hand-rolled in SQL in one place and in prose in another).
UNIT_LABELS_SQL = """
    SELECT unit_analyst_id, label
      FROM correctness_labels
"""

#: The same, scoped to one unit (GEPA's per-analyst gate).
ONE_UNIT_LABELS_SQL = """
    SELECT label
      FROM correctness_labels
     WHERE unit_analyst_id = $1
"""


def group_labels(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Group :data:`UNIT_LABELS_SQL` rows into ``{unit: [label, ...]}``."""
    out: dict[str, list[str]] = {}
    for row in rows:
        unit = str(row["unit_analyst_id"])
        out.setdefault(unit, []).append(str(row["label"]))
    return out


def score_by_unit(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """``({unit: record}, fleet_record)`` from :data:`UNIT_LABELS_SQL` rows.

    The fleet record is scored over EVERY verdict at once (not a mean of unit
    means): with n=1 for most units, a mean of means would weight a single
    verdict as heavily as a fully-labelled unit. It carries its own, higher
    floor (:data:`MIN_FLEET_LABELS`).
    """
    grouped = group_labels(rows)
    by_unit = {
        unit: score(labels, min_labels=MIN_UNIT_LABELS)
        for unit, labels in sorted(grouped.items())
    }
    all_labels = [label for labels in grouped.values() for label in labels]
    fleet = score(all_labels, min_labels=MIN_FLEET_LABELS)
    return by_unit, fleet


__all__ = [
    "AXIS_KEYS",
    "AXIS_NAME",
    "LABEL_CORRECT",
    "LABEL_INCORRECT",
    "LABEL_PARTIALLY_CORRECT",
    "LABEL_UNRESOLVABLE",
    "MIN_FLEET_LABELS",
    "MIN_UNIT_LABELS",
    "ONE_UNIT_LABELS_SQL",
    "UNIT_LABELS_SQL",
    "VOCABULARY",
    "WEIGHTS",
    "as_payload",
    "assert_not_pooled",
    "describe",
    "group_labels",
    "score",
    "score_by_unit",
    "score_unit_rows",
]


def score_unit_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score one unit's :data:`ONE_UNIT_LABELS_SQL` rows (GEPA's gate)."""
    return score((str(r["label"]) for r in rows), min_labels=MIN_UNIT_LABELS)
