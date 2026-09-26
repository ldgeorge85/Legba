# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1/G3 — THE CALIBRATION INTERLOCK: no passing gate, no grading.

Program 1's whole finding is that a correctness number means nothing until the
INSTRUMENT agrees with itself. R4 measured pooled agreement 0.42 on a five-label
rubric and the verdict was instrument-limited; ANNEX C v4 measured 0.8444 on a
fresh draw and the gate PASSED. The difference between those two rounds is the
only thing standing between a published share and a number nobody should read.

So the job REFUSES TO GRADE unless a ``grader_calibrations`` row exists that

  1. names the SAME ``rubric_sha`` this build grades under, and
  2. covers EVERY model id the run is about to use, and
  3. says ``gate_pass``.

COVERAGE IS A SUPERSET TEST, and the asymmetry is deliberate. A calibration is
a statement about a SET of families reading one rubric the same way; running a
SUBSET of that set (F0 alone, at a $0 ceiling) is inside what was measured, and
refusing it would mean a $0 deployment could never publish anything. Running a
model the gate never saw is OUTSIDE it — repoint the Mistral component at a
different model and no passing row covers it, so the grader stops. That is the
G3 re-gate trigger, enforced rather than remembered.

The FIRST row is seeded at deploy from the v4 result
(``scripts/load_unit_reference.py --seed-calibration``); later ones are written
by ``scripts/correctness_regate.py``, which re-runs the calibration on a FRESH
draw whenever the rubric, a model id or the claim grain moves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

#: The newest passing rows for a rubric, newest first. The Python side does the
#: coverage test, because "is this JSON object's value set a superset of these
#: strings" is not a predicate worth expressing in SQL twice.
SELECT_CALIBRATIONS_SQL = """
SELECT id, rubric_sha, model_ids, pooled, pairwise, gate_pass, packet_sha,
       n_atoms, created_at
  FROM grader_calibrations
 WHERE rubric_sha = $1 AND gate_pass
 ORDER BY created_at DESC
 LIMIT 50
"""

#: Why a run was refused. Each value is written onto the receipt verbatim, so an
#: operator reads the reason rather than inferring it from an empty table.
NO_CALIBRATION = "no_passing_calibration_for_rubric"
MODELS_NOT_COVERED = "models_not_covered_by_any_passing_calibration"


class CalibrationRefusal(RuntimeError):
    """No passing calibration covers this (rubric_sha, model ids).

    A ``RuntimeError`` guard rail, not a stub: nothing is faked and nothing
    degrades to a plausible-looking zero. The sweep records the refusal on its
    receipt and grades nothing.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _model_set(raw: Any) -> set[str]:
    """The model ids a stored ``model_ids`` value names, as a set of strings.

    asyncpg hands back ``jsonb`` as ``str``; a dict or a list is accepted too so
    the pure function is testable without a database.
    """
    value = raw
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return set()
    if isinstance(value, Mapping):
        return {str(v) for v in value.values() if v}
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value if v}
    return set()


def select_calibration(
    rows: Sequence[Mapping[str, Any]], required_models: Sequence[str]
) -> dict[str, Any]:
    """The newest passing row covering every required model, or a refusal.

    Pure over already-fetched rows so the interlock is testable without a DB.
    """
    required = {str(m) for m in required_models if m}
    if not rows:
        raise CalibrationRefusal(
            NO_CALIBRATION,
            "grader_calibrations holds no gate_pass row for this rubric_sha. "
            "Seed one from the v4 result at deploy "
            "(scripts/load_unit_reference.py --seed-calibration) or run "
            "scripts/correctness_regate.py. The instrument is not calibrated; "
            "refusing to publish a correctness number.",
        )
    for row in rows:
        covered = _model_set(row.get("model_ids"))
        if required <= covered:
            return dict(row)
    seen = sorted({m for row in rows for m in _model_set(row.get("model_ids"))})
    raise CalibrationRefusal(
        MODELS_NOT_COVERED,
        f"the run would grade with {sorted(required)}; the passing "
        f"calibrations for this rubric cover only {seen}. A model id that was "
        "never calibrated cannot publish a number under this rubric's sha — "
        "re-gate on a fresh draw (scripts/correctness_regate.py) first.",
    )


async def resolve_calibration(
    conn: Any, rubric_sha: str, required_models: Sequence[str]
) -> dict[str, Any]:
    """Fetch and select in one step. Raises :class:`CalibrationRefusal`."""
    rows = await conn.fetch(SELECT_CALIBRATIONS_SQL, rubric_sha)
    calibration = select_calibration(rows, required_models)
    logger.info(
        "correctness_grader.calibration_resolved rubric_sha=%s pooled=%s "
        "packet_sha=%s models=%s",
        rubric_sha, calibration.get("pooled"), calibration.get("packet_sha"),
        sorted(str(m) for m in required_models),
    )
    return calibration


# ---------------------------------------------------------------------------
# G3 — THE FRESH DRAW
# ---------------------------------------------------------------------------
#
# PREREG_P1 Amendment 2's rule, carried: a re-gate is NOT the last gate's atoms
# re-scored. Re-scoring the sample the instrument was tuned against measures the
# tuning, not the instrument, and v4 was deliberately drawn from the 53 atoms v3
# had never spent. Here the sample is drawn from the LIVE claim population, and
# FRESHNESS is enforced two ways that do not depend on anyone remembering:
#
#   * the SEED is derived from (rubric_sha, the model ids, the as-of date), so
#     re-gating a changed rubric or a repointed model draws a different sample
#     by construction — and re-running the same re-gate on the same day is the
#     SAME draw, which is honest: it is not new evidence;
#   * ``uq_grader_calibrations_draw`` on (rubric_sha, packet_sha, model_ids)
#     refuses to write a second row for a draw already scored, so a second roll
#     of the same dice cannot be presented as independent confirmation.

#: PREREG_P1 §2's n. Thirty atoms is what the two gates that have run were run
#: on, and changing it changes what the pooled rate is comparable to.
#: H12 — the METHOD/SCALE version of the calibration GATE this module enforces,
#: stamped ``method_version`` on each ``grader_calibrations`` row the regate /
#: seed scripts write. Covers the draw size (:data:`DEFAULT_DRAW_N`), the seeded
#: draw itself (:func:`draw_seed` / :func:`draw_calibration_sample`) and the
#: superset-coverage gate in :func:`select_calibration` — bump it when the draw
#: or the coverage rule moves, so a calibration row's vintage is legible without
#: a deploy-date guess.
METHOD_VERSION = "correctness_calibration/2026-09.1"

DEFAULT_DRAW_N = 30


def draw_seed(rubric_sha: str, model_ids: Sequence[str], as_of_date: str) -> str:
    """The draw's seed — derived, never chosen.

    A seed somebody picks is a seed somebody can pick again until the gate
    passes. This one falls out of what is being calibrated and when.
    """
    material = "|".join([
        rubric_sha, ",".join(sorted(str(m) for m in model_ids)), as_of_date,
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def draw_calibration_sample(
    claims: Sequence[Mapping[str, Any]], n: int, seed: str
) -> list[dict[str, Any]]:
    """A seeded UNIFORM draw of ``n`` claims, in id order.

    No stratification by unit or grain, exactly as PREREG_P1 §3 registered it:
    the realised per-unit counts are reported rather than engineered. Ordering
    the result by id means the packet's item order discloses nothing about the
    draw.
    """
    pool = sorted(claims, key=lambda c: str(c["id"]))
    if n >= len(pool):
        return [dict(c) for c in pool]
    rng = random.Random(seed)
    picked = rng.sample(range(len(pool)), n)
    return [dict(pool[i]) for i in sorted(picked)]


__all__ = [
    "DEFAULT_DRAW_N",
    "MODELS_NOT_COVERED",
    "NO_CALIBRATION",
    "SELECT_CALIBRATIONS_SQL",
    "CalibrationRefusal",
    "resolve_calibration",
    "draw_calibration_sample",
    "draw_seed",
    "select_calibration",
]
