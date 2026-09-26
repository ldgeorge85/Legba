# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE COMPOSITION CORRECTNESS GATE — authority climbs only as far as the
verification underneath it reaches (Program 2, track G2).

The spine's rule, `PLATFORM_DIRECTION_SPINE_2026-06-30` §3: *"authority climbs
only as far as the verification underneath it reaches. A floor-3 read inherits
exactly the confidence its cited floor-2 claims earned."* Until 2026-09-16 the
platform had no way to obey it for CORRECTNESS, because nothing measured
whether a desk read was TRUE — only whether it was faithful to its own
citations. Program 1 built that instrument; track G1 made it a daily job
writing `unit_correctness` rows. This module is the rule, applied.

WHAT IT DOES, in one sentence: with the flag ON, a desk unit whose latest
`unit_correctness` row is missing, weak, or single-family is QUOTED by
`country_composition` instead of COMPOSED over.

── QUOTED, NOT DROPPED ─────────────────────────────────────────────────────

"Quoted" is not a new mechanism and not a new render. It is exactly what
C-TIER's PERIPHERY tier already is (`composition_window.PERIPHERY_TIER`): the
row is excluded from the load-bearing basis, does not consume the input cap,
does not drive salience or `contributing_analysts`, and is rendered as an
explicitly delimited, hedged, capped-excerpt section that the composition may
refer to but never rests on. A gated unit is therefore still VISIBLE and still
lineage-linked — the failure mode this gate must not have is a desk silently
vanishing from the read, which would make a composition over three units look
identical to a composition over eight.

The demotion is expressed by stamping the SAME row marker C-TIER stamps
(`_evidence_tier = periphery`), deliberately: `_run`'s basis/periphery
partition is data-driven off that marker, so there is ONE partition in the
tree, not two that can disagree. What this module does NOT stamp is
`_evidence_floor` — that key is C-TIER's FAITHFULNESS floor and drives the
`evidence_tiers` envelope. Correctness is a different measurement; conflating
them in one envelope block is how a reader ends up averaging a faithfulness
floor with a correctness share. This gate records itself under its own
`correctness_gate` key.

── THE THREE OUTCOMES, AND WHY `no_number` IS NOT `quoted` ─────────────────

    composed   — a number exists and clears both thresholds.
    quoted     — a number exists and does NOT clear them (or is single-family
                 while single-family units are not allowed).
    no_number  — no `unit_correctness` row exists for this unit at all.

`no_number` and `quoted` have the same EFFECT (both are quoted, never
composed) and are different FACTS, so the payload records them apart. A unit
nobody has graded and a unit graded at 40% correct are both unsafe to compose
over and must not be explained to a reader the same way: one is a gap in the
instrument, the other is a finding about the read.

── THE THRESHOLDS ARE THE OPERATOR'S ───────────────────────────────────────

They are ENV, set at deploy, exactly like `LEGBA_GRADER_DAILY_CEILING_USD` —
NOT descriptor options. The same discipline and for the same reason: a bar
that decides what the platform is willing to assert is a policy the operator
owns, and a descriptor PUT is a per-analyst knob that any registry write could
move. An unparseable value logs and falls back to the shipped default rather
than to "no bar".

    LEGBA_COMPOSITION_CORRECTNESS_GATE          default 0   (off)
    LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS      default 0.8
    LEGBA_COMPOSITION_GATE_MIN_COVERAGE         default 0.2
    LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY  default 0   (off)

FLAG OFF ⇒ nothing here runs, no query is issued, no row is marked, no key is
stamped, and the composition is byte-identical to a tree without this module
(`tests/data_pkg/test_composition_correctness_gate.py`).

── WHY 0.2 COVERAGE, AND WHY COVERAGE AT ALL ───────────────────────────────

Because `correctness_share` alone is trivially gamed by silence. The Israel
run on 2026-09-16 measured `economic_coercion` at 100% correct — on ONE decided
claim out of seven. A correctness-only gate would rank that desk above
`internal_stability` at 67% over three. Coverage is the denominator that makes
the first number mean something, which is why migration 0196 stores them
together and why this gate reads them together. 0.2 is a floor on "the
reference bore on at least a fifth of what this desk said", roughly where the
first live measurement landed for the desks as a whole (24.4%).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

from .composition_window import (
    CORRECTNESS_QUARANTINE_KEY,
    PERIPHERY_TIER,
    _EVIDENCE_TIER_KEY,
)

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# The flag + the operator's thresholds
# ---------------------------------------------------------------------------

#: The master flag. OFF is the code default and the shipped default.
GATE_ENV: str = "LEGBA_COMPOSITION_CORRECTNESS_GATE"

#: The correctness bar a unit must clear to be COMPOSED over.
MIN_CORRECTNESS_ENV: str = "LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS"
DEFAULT_MIN_CORRECTNESS: float = 0.8

#: The coverage bar. Without it, `correctness_share` is gamed by silence.
MIN_COVERAGE_ENV: str = "LEGBA_COMPOSITION_GATE_MIN_COVERAGE"
DEFAULT_MIN_COVERAGE: float = 0.2

#: Whether a number ONE grader family produced may carry a composition.
ALLOW_SINGLE_FAMILY_ENV: str = "LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY"

#: The three outcomes, recorded per unit in the composition's payload.
GATE_COMPOSED: str = "composed"
GATE_QUOTED: str = "quoted"
GATE_NO_NUMBER: str = "no_number"

#: Where the per-run ledger rides from `READ_SLICE` (which has a connection) to
#: `_run` (which does not) — the `_region_coverage` / `HORIZON_ROW_KEY`
#: denormalize-onto-every-row idiom, so a slice whose only surviving row is a
#: continuity ref still knows what the gate decided.
GATE_LEDGER_KEY: str = "_correctness_gate"


def gate_enabled() -> bool:
    """``LEGBA_COMPOSITION_CORRECTNESS_GATE`` — code default OFF.

    Off, `apply_gate` issues no query and marks no row: the composition path is
    byte-for-byte a tree without this module.
    """
    return os.getenv(GATE_ENV, "").strip().lower() in _TRUTHY


def allow_single_family() -> bool:
    """``LEGBA_COMPOSITION_GATE_ALLOW_SINGLE_FAMILY`` — default OFF.

    At a ``LEGBA_GRADER_DAILY_CEILING_USD`` of $0 — the shipped default —
    EVERY number is single-family, because only the free core-plane family
    ever runs. So the gate's default posture is deliberately strict: a $0
    fleet composes over nothing until the operator either funds a second
    grader family or says, explicitly, that one family is enough.
    """
    return os.getenv(ALLOW_SINGLE_FAMILY_ENV, "").strip().lower() in _TRUTHY


def _share_env(env: str, default: float) -> float:
    """A share-valued threshold in ``[0, 1]``, or its shipped default.

    House style (``daily_ceiling_usd``, ``_resolve_verify_floor``): an
    unparseable value LOGS and falls back to the default — never to 0.0, which
    here would read as "no bar at all" and quietly compose over everything the
    operator meant to exclude.
    """
    raw = (os.getenv(env) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "composition_correctness_gate.threshold_unparseable env=%s raw=%r "
            "— falling back to the shipped default %.2f. A typo must not "
            "remove the bar.", env, raw, default,
        )
        return default
    return max(0.0, min(1.0, value))


def min_correctness() -> float:
    """``LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS`` — default 0.8."""
    return _share_env(MIN_CORRECTNESS_ENV, DEFAULT_MIN_CORRECTNESS)


def min_coverage() -> float:
    """``LEGBA_COMPOSITION_GATE_MIN_COVERAGE`` — default 0.2."""
    return _share_env(MIN_COVERAGE_ENV, DEFAULT_MIN_COVERAGE)


# ---------------------------------------------------------------------------
# The read — SELECT only, latest row per unit.
# ---------------------------------------------------------------------------

#: The same "newest per unit" shape the read surface uses
#: (``unit_correctness_api._LATEST_PER_UNIT_SQL``). Deliberately the same
#: predicate: the badge a reader sees beside a desk and the number this gate
#: judged it on must be THE SAME ROW, or the surface explains a decision the
#: gate did not make.
_LATEST_NUMBERS_SQL = """
SELECT DISTINCT ON (analyst_id)
       analyst_id, as_of, grain, n_claims, n_contains, n_contradicts,
       n_single_family, correctness_share, coverage_share, families
  FROM unit_correctness
 WHERE target_id = $1 AND analyst_id = ANY($2::text[])
 ORDER BY analyst_id, as_of DESC, created_at DESC
"""


async def read_unit_numbers(
    conn: Any, *, target_id: str, analyst_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Latest `unit_correctness` row per unit, keyed by ``analyst_id``.

    A unit absent from the result has no number — which is a fact the caller
    records as ``no_number``, never one it papers over.
    """
    if not analyst_ids:
        return {}
    rows = await conn.fetch(_LATEST_NUMBERS_SQL, target_id, list(analyst_ids))
    return {str(r["analyst_id"]): dict(r) for r in rows}


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def verdict(
    number: Mapping[str, Any] | None,
    *,
    min_correctness_share: float,
    min_coverage_share: float,
    single_family_allowed: bool,
) -> dict[str, Any]:
    """One unit's gate outcome, with the numbers that produced it.

    Pure, so the rule is testable without a database and the payload a reader
    sees is built from exactly the values the decision used. The returned dict
    IS the per-unit entry in ``data.correctness_gate.units``.

    Order of the checks is load-bearing for the `reason` a reader gets: no row
    at all is the first and most important thing to say; a single-family
    number is reported as such even when it would also have missed a bar,
    because "one grader said so" is the more actionable objection.
    """
    # DEFERRED, for the reason ``composition_slice._default_basis_reader``
    # gives: the read surface OWNS this predicate (it is what the badge says),
    # and the gate must not grow a second opinion about what "one family stood
    # behind it" means — but a module-level import from an analyst module into
    # the registry's route package would tie the worker's import graph to the
    # web surface. Inside a function body it is a plain attribute lookup on an
    # already-loaded module, and the lockstep is structural rather than
    # remembered.
    from ..registry.unit_correctness_api import single_family_row

    if number is None:
        return {
            "gate": GATE_NO_NUMBER,
            "reason": "no unit_correctness row for this unit",
            "correctness_share": None,
            "coverage_share": None,
            "n_claims": None,
            "n_decided": None,
            "single_family": None,
            "as_of": None,
        }

    correctness = _as_float(number.get("correctness_share"))
    coverage = _as_float(number.get("coverage_share"))
    n_contains = int(number.get("n_contains") or 0)
    n_contradicts = int(number.get("n_contradicts") or 0)
    n_claims = int(number.get("n_claims") or 0)
    as_of = number.get("as_of")
    single = single_family_row(
        number.get("families"), number.get("n_single_family"), n_claims
    )

    entry: dict[str, Any] = {
        "gate": GATE_COMPOSED,
        "reason": "",
        "correctness_share": correctness,
        "coverage_share": coverage,
        "n_claims": n_claims,
        "n_decided": n_contains + n_contradicts,
        "single_family": single,
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else as_of,
    }

    if single and not single_family_allowed:
        entry["gate"] = GATE_QUOTED
        entry["reason"] = (
            "single-family number: one grader family stood behind it and "
            f"{ALLOW_SINGLE_FAMILY_ENV} is not set"
        )
        return entry
    if correctness is None:
        # A number whose denominator was empty. NOT a zero — the reference
        # bore on nothing this unit said, so there is nothing to clear a bar
        # with, and composing over it would assert authority the measurement
        # never earned.
        entry["gate"] = GATE_QUOTED
        entry["reason"] = (
            f"correctness unmeasured: the reference bore on none of the "
            f"{n_claims} claim(s), so no share exists to test"
        )
        return entry
    if correctness < min_correctness_share:
        entry["gate"] = GATE_QUOTED
        entry["reason"] = (
            f"correctness {correctness:.3f} is below the operator's bar "
            f"{min_correctness_share:.3f} ({MIN_CORRECTNESS_ENV})"
        )
        return entry
    if coverage is None or coverage < min_coverage_share:
        shown = "unmeasured" if coverage is None else f"{coverage:.3f}"
        entry["gate"] = GATE_QUOTED
        entry["reason"] = (
            f"coverage {shown} is below the operator's bar "
            f"{min_coverage_share:.3f} ({MIN_COVERAGE_ENV}) — a correctness "
            "share over too little of what the unit said"
        )
        return entry

    entry["reason"] = (
        f"correctness {correctness:.3f} >= {min_correctness_share:.3f} and "
        f"coverage {coverage:.3f} >= {min_coverage_share:.3f}"
    )
    return entry


# ---------------------------------------------------------------------------
# The application — mark the rows, carry the ledger.
# ---------------------------------------------------------------------------


async def apply_gate(
    conn: Any, rows: list[dict[str, Any]], *, target_id: str
) -> list[dict[str, Any]] | None:
    """Gate one per-country composition's basis rows, in place.

    Returns the per-unit ledger (also stamped onto every row for `_run`), or
    ``None`` when the flag is off — in which case NOTHING has been read,
    marked or stamped and the caller's rows are untouched.

    A row already marked periphery by C-TIER is left alone: it is already
    quoted, and re-deciding it here would let the correctness gate silently
    PROMOTE a read the faithfulness floor withheld.
    """
    if not gate_enabled():
        return None

    bar_correctness = min_correctness()
    bar_coverage = min_coverage()
    single_ok = allow_single_family()

    candidates = [
        r for r in rows
        if r.get(_EVIDENCE_TIER_KEY) != PERIPHERY_TIER and r.get("analyst_id")
    ]
    analyst_ids = sorted({str(r["analyst_id"]) for r in candidates})
    numbers = await read_unit_numbers(
        conn, target_id=target_id, analyst_ids=analyst_ids
    )

    by_unit: dict[str, dict[str, Any]] = {}
    for unit in analyst_ids:
        entry = verdict(
            numbers.get(unit),
            min_correctness_share=bar_correctness,
            min_coverage_share=bar_coverage,
            single_family_allowed=single_ok,
        )
        entry["unit"] = unit
        by_unit[unit] = entry

    for row in candidates:
        entry = by_unit[str(row["analyst_id"])]
        if entry["gate"] != GATE_COMPOSED:
            row[_EVIDENCE_TIER_KEY] = PERIPHERY_TIER
            # The per-row WHY, which the periphery render reads so the prompt
            # says "withheld by the correctness gate" rather than the false
            # "did NOT clear the verification floor" — a gated row may have
            # cleared that floor perfectly.
            row[CORRECTNESS_QUARANTINE_KEY] = entry

    ledger = [by_unit[u] for u in analyst_ids]
    stamp_gate_ledger(rows, ledger)
    if ledger:
        logger.info(
            "composition_correctness_gate.applied target=%s composed=%d "
            "quoted=%d no_number=%d min_correctness=%.2f min_coverage=%.2f "
            "single_family_allowed=%s",
            target_id,
            sum(1 for e in ledger if e["gate"] == GATE_COMPOSED),
            sum(1 for e in ledger if e["gate"] == GATE_QUOTED),
            sum(1 for e in ledger if e["gate"] == GATE_NO_NUMBER),
            bar_correctness, bar_coverage, single_ok,
        )
    return ledger


def stamp_gate_ledger(
    rows: Sequence[dict[str, Any]], ledger: Sequence[Mapping[str, Any]]
) -> None:
    """Denormalize the ledger onto every row, so the DB-less `_run` sees it."""
    carried = list(ledger)
    for row in rows:
        row[GATE_LEDGER_KEY] = carried


def gate_ledger_of(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """The ledger `READ_SLICE` stamped, or ``None`` on an ungated slice.

    ``None`` — not ``[]`` — for a slice no gate touched: an empty ledger is a
    gated run over zero units, which is a different fact and gets a different
    envelope.
    """
    for row in rows:
        carried = row.get(GATE_LEDGER_KEY)
        if carried is not None:
            return [dict(e) for e in carried]
    return None


def gate_envelope(
    ledger: Sequence[Mapping[str, Any]], *, basis_count: int
) -> dict[str, Any]:
    """``finding.data["correctness_gate"]`` — why each unit was or was not
    composed, with the bars in force at the time.

    The bars travel WITH the verdicts on purpose. A reader six months from now
    asking why a desk was quoted needs the number and the bar it missed; an
    envelope carrying only the verdict makes them go and guess what the env
    said that day.
    """
    return {
        "min_correctness": min_correctness(),
        "min_coverage": min_coverage(),
        "allow_single_family": allow_single_family(),
        "basis_count": basis_count,
        "composed": [
            e["unit"] for e in ledger if e.get("gate") == GATE_COMPOSED
        ],
        "quoted": [e["unit"] for e in ledger if e.get("gate") == GATE_QUOTED],
        "no_number": [
            e["unit"] for e in ledger if e.get("gate") == GATE_NO_NUMBER
        ],
        "units": [dict(e) for e in ledger],
    }


def stamp_gate_envelope(
    data: dict[str, Any],
    ledger: Sequence[Mapping[str, Any]] | None,
    *,
    basis_count: int,
) -> None:
    """Write ``data["correctness_gate"]``, or nothing at all on an ungated run.

    The ``None`` guard lives HERE rather than at the two call sites in
    ``meta_findings_synthesizer`` deliberately: that module sits one line under
    its size ceiling, and the ungated path must stay a single call that
    provably writes no key.
    """
    if ledger is None:
        return
    data["correctness_gate"] = gate_envelope(ledger, basis_count=basis_count)


def gate_empty_stamp(
    data: dict[str, Any],
    ledger: Sequence[Mapping[str, Any]] | None,
    *,
    title: str,
    body: str,
    window_text: str = "",
) -> tuple[str, str]:
    """The empty-basis half: stamp the envelope and, when the GATE is what
    emptied the basis, replace the sentence that would otherwise claim an
    absence of reads. Returns the (possibly unchanged) title and body."""
    stamp_gate_envelope(data, ledger, basis_count=0)
    if ledger is None:
        return title, body
    replacement = gate_empty_sentence(ledger, window_text=window_text)
    return replacement if replacement is not None else (title, body)


def gate_empty_sentence(
    ledger: Sequence[Mapping[str, Any]], *, window_text: str = ""
) -> tuple[str, str] | None:
    """The honest (title, body) when the GATE — not absence, not the
    faithfulness floor — is what emptied the basis.

    Returns ``None`` when the gate composed at least one unit, so the caller
    keeps whatever sentence it would otherwise have printed. "No desk read
    exists" over eight desks that all read fine and all failed a correctness
    bar is the same class of lie as the BF-desk "No source findings to
    synthesize" this tree already paid for once.
    """
    if not ledger or any(e.get("gate") == GATE_COMPOSED for e in ledger):
        return None
    n_quoted = sum(1 for e in ledger if e.get("gate") == GATE_QUOTED)
    n_none = sum(1 for e in ledger if e.get("gate") == GATE_NO_NUMBER)
    body = (
        f"Every read on this desk{window_text} was withheld from composition "
        f"by the CORRECTNESS GATE: {n_quoted} unit(s) carry a measured "
        f"correctness or coverage below the operator's bar "
        f"(>= {min_correctness():.2f} correctness, >= {min_coverage():.2f} "
        f"coverage) and {n_none} carry no measured number at all. The reads "
        "themselves are quoted below and recorded in data.correctness_gate. "
        "This is a CORRECTNESS withholding — authority climbs only as far as "
        "the verification underneath it reaches — not an absence of reads."
    )
    return "All reads withheld by the correctness gate", body
