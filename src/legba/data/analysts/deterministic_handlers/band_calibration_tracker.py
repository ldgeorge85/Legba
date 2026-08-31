# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``band_calibration_tracker`` sub-handler — P2-3 scorecard calibration harness.

Band changes become RESOLVABLE claims, auto-resolved and scored over time. A
deterministic META analyst on a daily cadence that, each run:

  (a) **logs new calibration claims** from scorecard band TRANSITIONS since its
      watermark — the same desk×dimension ladder→ladder transitions P1-3's
      ``alert_trigger_scan`` detects (this module reuses that handler's PURE
      comparison shape via :func:`classify_band_transition`; it never touches
      alert_trigger_scan's watermarks). Each claim pins the implied directional
      statement ("escalation band for desk X moved low→elevated at T0") into
      ``band_calibration_claims`` (migration 0093) with a HARD resolution spec.
  (b) **resolves claims whose horizons have passed** — at T0+14d and T0+28d the
      resolver reads the then-current band from LATER scorecard rows ONLY
      (deterministic SQL, no LLM) and grades the claim against the ladder.
  (c) **emits ONE honest summary finding per run** (claims logged / resolved /
      persistence + reversal rates) tagged ``calibration`` — NEVER a skill
      boast; the integrity_sweep contract (a finding every run, honest zero
      states) and the calibration_tracking read shape (metrics in ``data``).

Resolution spec (``hard_band_at_horizon_v1``)
---------------------------------------------
For a claim (desk, dimension, from→to, T0) and horizon H ∈ {14d, 28d}: read the
LATEST scorecard row for the desk with ``transition_at < produced_at <=
T0 + H`` (superseded rows included — they are the historical record) and take
its band for the dimension. Grade on the band ladder relative to ``to_band``:

  * ``held``         — band == to_band (the transition held exactly).
  * ``worsened``     — deterioration claim, band moved FURTHER up the ladder
                       (the transition confirmed and extended).
  * ``improved``     — improvement claim, band moved further DOWN (confirmed
                       and extended).
  * ``reverted``     — band moved back AGAINST the claimed direction.
  * ``insufficient`` — the band at horizon read ``insufficient-evidence``
                       (coverage lost — neither confirms nor reverts).
  * ``unresolvable`` — no later scorecard row exists inside (T0, T0+H], or the
                       band read is off-ladder (the desk stopped producing /
                       unreadable) — an honest abstain, never a fabricated
                       outcome.

Because scorecard rows are only ever appended (``produced_at`` = write time),
the row set inside (T0, T0+H] is FROZEN once H has passed — resolution is
stable and independently recomputable. An already-resolved horizon is NEVER
overwritten (operator labels win) and a ``voided:``-prefixed ``resolved_by`` is
skipped by the resolver (the P7-F6 acute_forecasts void contract).

Scoring — and what is deliberately NOT claimed
----------------------------------------------
Confirmation = ``held`` + ``worsened`` / ``improved``. Per horizon (overall,
per direction, per dimension):

  * band-persistence rate = confirmed / (confirmed + reverted)
  * reversal rate         = reverted  / (confirmed + reverted)

``insufficient`` / ``unresolvable`` are reported but EXCLUDED from both
denominators; a zero denominator yields an honest ``None`` rate, never a
fabricated 0.0/1.0. **NO Brier or Brier-skill score is computed or implied**:
bands are ordinal risk categories, not probabilities — the summary finding's
``honesty_note`` + ``no_brier`` flag state this explicitly, and the aggregate
is surfaced in its OWN ``band_calibration`` section of
``GET /api/v1/v3/eval/calibration``, never pooled into any Brier key.

Statefulness — watermark + dedup floor
--------------------------------------
The scan watermark (one ``scorecard_scan`` row in
``band_calibration_scan_state``) fingerprints the last pair-compared scorecard
``produced_at``; each run compares only rows past it (plus one base row per
desk for the FROM side). Claim identity is the UNIQUE
``(desk, dimension, scorecard_row_id)`` index — INSERT ON CONFLICT DO NOTHING —
so a lost watermark or overlapping re-scan can never duplicate a claim.

Unlike alert_trigger_scan (whose first scan seeds silently because alerts page
an operator), the FIRST run here backfills claims from scorecard history
(bounded per run by ``max_scan_rows``): a historical transition is a real
transition with a known T0, its resolution reads the same frozen later rows,
and calibration claims page nobody — backfilling honestly seeds the sample.

Evidence transitions (in/out of ``insufficient-evidence``) and off-ladder
``indeterminate`` changes carry NO directional risk statement, so no claim is
logged for them — they are counted in the receipt as
``skipped_non_directional`` (coverage events are not resolvable claims).

H3-GUARD — semantics-migration claims (2026-08-27)
---------------------------------------------------
A ladder→ladder transition whose prior and current scorecard were computed
under DIFFERENT ``banding_semantics`` / ``damping_semantics`` stamps (see
:func:`scorecard_banding.semantics_changed`) is not a real deterioration or
improvement — the two cards disagree about what the band even MEANS, so "did
the band hold" is not a question this table can honestly answer for them. The
H3 deploy is the reference case: ``damping_semantics`` is a NEW stamp, so
every pre-H3 card lacks it and every post-H3 card carries ``"off"`` — the
first post-deploy sweep would otherwise mint ~30 fleet-wide calibration claims
whose "resolution" measures a semantics change, not the world.

``classify_band_transition`` (shared with ``alert_trigger_scan``) reads these
as ``direction='semantics-migration'`` instead of
``deterioration``/``improvement``. They are LOGGED, never silently dropped —
an operator can see the transition happened and why it was excluded — with
``semantics_migration=True`` (migration 0187), a HARD flag rather than a
cosmetic tag: the aggregation query (:func:`_pull_claims_for_summary`)
filters ``WHERE NOT semantics_migration``, so these rows can never enter
:func:`summarize_claims`'s ``overall`` / ``by_direction`` / ``by_dimension``
blocks, and the excluded count is surfaced honestly as
``population.excluded_semantics_migration`` (the same "report what you
excluded" shape M-2 uses for a judge-pipeline swap). They still resolve at
their horizons like any other claim (the resolver has no direction filter)
and read ``unresolvable`` there — off-ladder classification, never a
fabricated grade — but that outcome is moot for scoring: the exclusion
already happened upstream, at the population pull.

LINEAGE-AWARE STAMP POOLING (2026-08-29) — why the split key stopped splitting
------------------------------------------------------------------------------
The judge-pipeline filter above is right and its parameterisation was
self-defeating. A claim resolves at T0+14d at the earliest; the stamp's mean
lifetime is ~2.3 days (12 distinct stamps in the 26 days this table covers). A
claim can therefore never be BOTH current-stamped AND resolved, so
``n_scored`` has been 0 on every run since 2026-08-04 — 1,802 claims, all
excluded, daily, for 25 days (CAMPAIGN_2026-08-29/PREMISE_GRADING_LOOP.md A-7).
The apparatus optimised so hard against pooling a lie that it reliably reported
nothing, and refusing to measure is not the honest end of that trade.

The fix is NOT a looser filter. It is reading the lineage the stamps already
carry: :data:`~legba.data.provenance.judge_pipeline_version.STAMP_EXPECTED_SHIFTS`
records, per stamp and per metric family, whether that boundary is declared to
move the family at all. Consecutive stamps whose boundaries ALL declare
``'none'`` for the family this harness calibrates are one population for it and
are pooled; any boundary that declares a real shift is HARD and the pool stops
there. The relation is transitive over consecutive ``'none'`` boundaries and
nothing else joins.

The family here is ``faithfulness_score`` (:data:`CALIBRATED_METRIC_FAMILY`) —
a band is a verdict about faithfulness-gated findings, so it is the SCORE that
has to be comparable, not the hard/soft split or the reason census. A stamp
that only re-labels severities (2026-08-20/1's demotion train, in its own
words: "the demotion train never moves the score, only the severity label") is
poolable HERE and would not be for a panel reading hard-fail share.

WHAT THIS DOES NOT DO. It never widens silently: the pooled stamp SET, its
size, the family it was computed for and everything still excluded are all
reported on the finding (``population.pooling``). It cannot widen past a
written declaration — an unregistered or ambiguous stamp pools with nothing, so
the failure mode is the OLD behaviour, never a fabricated population. And it
does not manufacture a sample: if the pool is still too thin, the rates stay
honest ``None`` over their real n exactly as before.

MEASURED YIELD AT THE HEAD, recorded because it is the finding and not a
footnote: ``2026-08-29/1`` pools with NOTHING. Every boundary back to
2026-08-21/1 declares a real score shift, and the two poolable runs in the whole
lineage ({2026-08-09/1, 2026-08-10/1} and {2026-08-15/1, 2026-08-20/1}) are both
strictly historical. So this change does not, today, move ``n_scored`` off zero
— the remaining defect is the stamp CADENCE, not this reader. What it does is
make the reader correct, so the moment a score-neutral train ships on top (a
pure demotion or severity relabel, the 08-20/1 shape) the population widens by
itself instead of staying dark for another 25 days.

Registered via ``scripts/bringup_register_band_calibration_tracker.py`` — NOT
inline through a test fixture. Ships ``state: draft``; migration 0093 must be
applied BEFORE first activation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ...provenance.judge_pipeline_version import (
    METRIC_FAITHFULNESS_SCORE,
    poolable_stamps,
)
from ...provenance.models import FindingPayload
from ...provenance.verify import JUDGE_PIPELINE_VERSION
from ....runtime.analyst_method import AnalystMethodResult
from . import scorecard_banding
from .alert_trigger_scan import classify_band_transition

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "band_calibration_tracker"

#: The pinned resolution-spec id every logged claim carries.
RESOLUTION_SPEC = "hard_band_at_horizon_v1"

#: Deterministic-resolver stamp (mirrors forecast_acute.RESOLVED_BY — a
#: dedicated value so operator labels 'operator:<id>' are distinguishable).
RESOLVED_BY = "band_calibration_deterministic"

#: P7-F6 void contract mirror: a horizon whose resolved_by carries this prefix
#: is withdrawn from grading — the resolver skips it and never overwrites it.
VOID_PREFIX = "voided:"

#: The two hard horizons, in days. Each maps to its own column family
#: (outcome_14 / outcome_28, ...) in band_calibration_claims.
HORIZON_DAYS: tuple[int, ...] = (14, 28)

#: The directions a claim is logged for — ladder→ladder transitions only.
#: Evidence / indeterminate transitions are coverage events, not claims.
CLAIMABLE_DIRECTIONS: frozenset[str] = frozenset({"deterioration", "improvement"})

# Closed outcome vocabulary (see module docstring).
OUTCOME_HELD = "held"
OUTCOME_WORSENED = "worsened"
OUTCOME_IMPROVED = "improved"
OUTCOME_REVERTED = "reverted"
OUTCOME_INSUFFICIENT = "insufficient"
OUTCOME_UNRESOLVABLE = "unresolvable"

#: Outcomes that CONFIRM the transition (the persistence-rate numerator).
CONFIRMED_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_HELD, OUTCOME_WORSENED, OUTCOME_IMPROVED}
)

#: The honesty note carried verbatim on every summary finding + the eval read.
HONESTY_NOTE = (
    "Band-persistence and reversal rates are ordinal stability measures over "
    "later scorecard rows. Bands are categorical risk verdicts, not "
    "probabilities: no Brier score, Brier skill score, or forecast-skill "
    "claim exists (or can exist) for this harness."
)

#: Per-run cap on NEW scorecard rows pair-compared (the watermark advances only
#: over processed rows, so a backlog catches up across runs, never silently
#: skips).
DEFAULT_MAX_SCAN_ROWS = 2000

#: Per-run cap on due claims resolved per horizon (retry next tick).
_MAX_RESOLVE_PER_HORIZON = 500

#: THE metric family this harness calibrates (module docstring, "LINEAGE-AWARE
#: STAMP POOLING"). Band persistence is a statement about faithfulness-gated
#: findings, so the population has to be comparable on the SCORE — a stamp that
#: only moved the hard/soft split or a reason census did not move what is being
#: measured here. Pooling is computed for this family and no other.
CALIBRATED_METRIC_FAMILY = METRIC_FAITHFULNESS_SCORE

#: Aggregation lookback (days) for the summary metrics.
DEFAULT_LOOKBACK_DAYS = 365
_MAX_AGG_ROWS = 20000

_SCAN_STATE_KEY = "scorecard_scan"

#: The epoch floor used when no watermark exists yet (first-ever scan).
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure helpers (testable with NO database)
# ---------------------------------------------------------------------------


def classify_horizon_outcome(
    direction: str, to_band: str, band_at_horizon: Optional[str]
) -> str:
    """Grade one horizon read against the claim — the hard truth table.

    ``band_at_horizon`` is the then-current band read from the latest later
    scorecard row (``None`` when no such row / no such dimension existed).
    Deliberately CLOSED vocabulary; a read this function cannot place on the
    ladder is ``unresolvable``, never a guessed outcome.
    """
    ladder = scorecard_banding.BAND_LADDER
    if band_at_horizon is None:
        return OUTCOME_UNRESOLVABLE
    if band_at_horizon == scorecard_banding.INSUFFICIENT:
        return OUTCOME_INSUFFICIENT
    if band_at_horizon not in ladder or to_band not in ladder:
        return OUTCOME_UNRESOLVABLE
    bi = ladder.index(band_at_horizon)
    ti = ladder.index(to_band)
    if bi == ti:
        return OUTCOME_HELD
    if direction == "deterioration":
        return OUTCOME_WORSENED if bi > ti else OUTCOME_REVERTED
    if direction == "improvement":
        return OUTCOME_IMPROVED if bi < ti else OUTCOME_REVERTED
    # A claim with an unknown direction should not exist (the logger gates on
    # CLAIMABLE_DIRECTIONS) — refuse to fabricate a grade for one.
    return OUTCOME_UNRESOLVABLE


def _parse_jsonish(raw: Any) -> Any:
    """JSONB columns arrive as dict/list (asyncpg codec) or str — normalise."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def extract_dimension_bands(data: Any) -> dict[str, str]:
    """``{dimension: band}`` from one scorecard row's ``data`` column.

    The column stores the FULL payload dump, so the producer's payload ``data``
    dict is NESTED under ``'data'`` (the alert_trigger_scan / v3_api
    eval_country_scorecard contract); a bare ``{'bands': ...}`` is accepted too
    (defensive). Unreadable rows yield ``{}`` — the caller treats that as
    nothing to compare / an unresolvable read, never a fabricated band.
    """
    payload = _parse_jsonish(data) or {}
    if not isinstance(payload, Mapping):
        return {}
    inner = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    bands = inner.get("bands") if isinstance(inner, Mapping) else None
    dims = bands.get("dimensions") if isinstance(bands, Mapping) else None
    if not isinstance(dims, Mapping):
        return {}
    out: dict[str, str] = {}
    for dim, verdict in dims.items():
        if not isinstance(verdict, Mapping):
            continue
        band = str(verdict.get("band") or "")
        if band:
            out[str(dim)] = band
    return out


def extract_card_semantics(data: Any) -> tuple[Optional[str], Optional[str]]:
    """``(banding_semantics, damping_semantics)`` from one scorecard row's
    ``data`` column — the SAME parse :func:`extract_dimension_bands` performs,
    stopping one level higher (the stamps sit BESIDE ``dimensions`` on the
    card, not inside it; see :func:`scorecard_banding.bands_semantics`).
    Unreadable rows yield ``(None, None)``, exactly as informative as a card
    written before the stamps existed — the H3-GUARD's "treat absent as
    differing from a present value" rule (module docstring) handles the rest.
    """
    payload = _parse_jsonish(data) or {}
    if not isinstance(payload, Mapping):
        return None, None
    inner = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    bands = inner.get("bands") if isinstance(inner, Mapping) else None
    return scorecard_banding.bands_semantics(bands)


def _rate_block(outcomes: Mapping[str, int]) -> dict[str, Any]:
    """persistence/reversal rates from an outcome-count map — honest ``None``
    on a zero denominator (never a fabricated 0.0 or 1.0)."""
    confirmed = sum(outcomes.get(o, 0) for o in CONFIRMED_OUTCOMES)
    reverted = outcomes.get(OUTCOME_REVERTED, 0)
    scored = confirmed + reverted
    return {
        "confirmed": confirmed,
        "reverted": reverted,
        "scored": scored,
        "excluded_insufficient": outcomes.get(OUTCOME_INSUFFICIENT, 0),
        "excluded_unresolvable": outcomes.get(OUTCOME_UNRESOLVABLE, 0),
        "persistence_rate": (confirmed / scored) if scored > 0 else None,
        "reversal_rate": (reverted / scored) if scored > 0 else None,
    }


def summarize_claims(
    rows: list[Mapping[str, Any]],
    *,
    lookback_days: int,
    population: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold claim rows into the cumulative band-calibration aggregate.

    ``rows`` carry at least ``dimension`` / ``direction`` / ``outcome_14`` /
    ``outcome_28``. Pure + deterministic; every horizon block reports outcome
    counts, the confirmed/reverted split, and rates with honest-None on empty
    denominators. Split three ways: overall, by direction (deteriorations vs
    improvements), by dimension.

    ``population`` is the judge-pipeline boundary block (P3 §5a): which stamp
    these rates describe and how many claims were EXCLUDED rather than pooled.
    It is carried through verbatim so a reader can never mistake a
    single-pipeline rate for an all-time one.
    """
    horizons = {f"{d}d": f"outcome_{d}" for d in HORIZON_DAYS}

    def _outcome_counts(sub: list[Mapping[str, Any]], col: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in sub:
            o = r.get(col)
            if o:
                counts[str(o)] = counts.get(str(o), 0) + 1
        return counts

    def _block(sub: list[Mapping[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"claims": len(sub)}
        for label, col in horizons.items():
            counts = _outcome_counts(sub, col)
            resolved = sum(counts.values())
            out[label] = {
                "resolved": resolved,
                "open": len(sub) - resolved,
                "outcomes": counts,
                **_rate_block(counts),
            }
        return out

    overall = _block(rows)
    by_direction = {
        d: _block([r for r in rows if str(r.get("direction") or "") == d])
        for d in sorted({str(r.get("direction") or "") for r in rows})
        if d
    }
    by_dimension = {
        dim: _block([r for r in rows if str(r.get("dimension") or "") == dim])
        for dim in sorted({str(r.get("dimension") or "") for r in rows})
        if dim
    }
    return {
        "claims_total": len(rows),
        "lookback_days": int(lookback_days),
        "resolution_spec": RESOLUTION_SPEC,
        "horizons": {label: overall[label] for label in horizons},
        "by_direction": by_direction,
        "by_dimension": by_dimension,
        # WHICH judge population these rates describe, and what was excluded to
        # keep them one population (P3 §5a — the split key finally has a reader).
        "population": dict(population) if population else None,
        # The honesty contract, machine-checkable + verbatim.
        "no_brier": True,
        "honesty_note": HONESTY_NOTE,
    }


def summarize_prior_populations(
    rows: list[Mapping[str, Any]],
    *,
    lookback_days: int,
) -> list[dict[str, Any]]:
    """M-2 — one aggregate PER superseded judge pipeline, never merged.

    ``rows`` are the claims the current-stamp filter excluded, each carrying its
    own ``judge_pipeline_version`` (``None`` = logged before the split key
    existed — a real population, not a gap). Returns one block per stamp with
    that stamp's own ``claims_total`` and horizon rates, largest first.

    This is the difference between "we refuse to pool" and "we refuse to show
    you": the headline stays one population, and the history stays legible
    beside it. Nothing here is ever added to or averaged with the headline —
    each block stands alone, and the reader does the comparing.
    """
    grouped: dict[Any, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get("judge_pipeline_version"), []).append(row)

    out: list[dict[str, Any]] = []
    for version, sub in grouped.items():
        # Reuse the SAME fold as the headline — a prior population must be
        # computed identically or the comparison is meaningless.
        summary = summarize_claims(sub, lookback_days=lookback_days)
        out.append({
            "judge_pipeline_version": version,
            "pre_stamp": version is None,
            "claims_total": summary["claims_total"],
            "horizons": summary["horizons"],
        })
    out.sort(key=lambda b: (-int(b["claims_total"]), str(b["judge_pipeline_version"])))
    return out


def build_finding(
    *,
    summary: Mapping[str, Any],
    logged: int,
    resolved_by_horizon: Mapping[str, int],
    skipped_non_directional: int,
    scanned_rows: int,
    warnings: list[str],
    logged_semantics_migration: int = 0,
) -> FindingPayload:
    """The per-run honest summary finding — counts + rates, NEVER a boast.

    A finding EVERY run (the integrity_sweep contract): the zero state reads as
    an explicit "no band transitions observed / no persistence claim made", and
    a populated state carries rates WITH their sample sizes and the no-Brier
    honesty note — the title never editorializes skill.
    """
    resolved_this_run = sum(int(v) for v in resolved_by_horizon.values())
    claims_total = int(summary.get("claims_total") or 0)
    if claims_total == 0 and logged == 0:
        head = (
            "Band calibration: 0 claims on record "
            "(no band transitions observed; no persistence claim made)"
        )
    else:
        head = (
            f"Band calibration: logged={logged} resolved={resolved_this_run} "
            f"this run (claims={claims_total})"
        )
    body_lines = [
        f"logged_this_run={logged}",
        f"resolved_this_run={resolved_this_run} by_horizon={dict(resolved_by_horizon)}",
        f"claims_total={claims_total}",
        f"scanned_scorecard_rows={scanned_rows}",
        f"skipped_non_directional={skipped_non_directional}",
        # H3-GUARD — logged, not skipped: a semantics-migration claim IS
        # recorded (semantics_migration=True), just excluded from scoring.
        f"logged_semantics_migration={logged_semantics_migration}",
        f"resolution_spec={summary.get('resolution_spec')}",
    ]
    horizons = summary.get("horizons") or {}
    if isinstance(horizons, Mapping):
        for label in sorted(horizons):
            h = horizons[label]
            if not isinstance(h, Mapping):
                continue
            body_lines.append(
                f"{label}: resolved={h.get('resolved')} open={h.get('open')} "
                f"confirmed={h.get('confirmed')} reverted={h.get('reverted')} "
                f"persistence_rate={h.get('persistence_rate')} "
                f"reversal_rate={h.get('reversal_rate')} "
                f"(n_scored={h.get('scored')}, "
                f"excluded: insufficient={h.get('excluded_insufficient')} "
                f"unresolvable={h.get('excluded_unresolvable')})"
            )
    pop = summary.get("population")
    if isinstance(pop, Mapping):
        body_lines.append(
            f"population: judge_pipeline_version={pop.get('judge_pipeline_version')} "
            f"excluded_pre_stamp={pop.get('excluded_pre_stamp')} "
            f"excluded_other_pipeline={pop.get('excluded_other_pipeline')} "
            f"excluded_semantics_migration={pop.get('excluded_semantics_migration')} "
            "(rates cover ONE judge population; cross-boundary AND semantics-"
            "migration claims are excluded, never pooled)"
        )
        # The POOLED SET, spelled out — a rate over several stamps must never
        # read as a rate over the one stamp named above, and a pool of one must
        # never read as a pool at all.
        pooling = pop.get("pooling")
        if isinstance(pooling, Mapping):
            stamps = list(pooling.get("stamps") or [])
            widened = pooling.get("widened_by")
            body_lines.append(
                f"pooled_judge_pipeline_versions={stamps} "
                f"(n_stamps={pooling.get('stamp_count')} "
                f"widened_by={widened} "
                f"metric_family={pooling.get('metric_family')}) — "
                + (
                    "pooled ACROSS boundaries the lineage declares cannot move "
                    "this family; any declared shift is a hard stop"
                    if isinstance(widened, int) and widened > 0
                    else "NO widening: every reachable boundary declares a real "
                    "shift for this family, so this is the head stamp alone"
                )
            )
        # M-2 — each excluded pipeline as its OWN readout. Reporting only the
        # excluded COUNT would have turned a working readout into a blank one
        # the day the split filter went live (every existing claim is
        # NULL-stamped, mig 0122 having correctly refused to backfill).
        for prior in (pop.get("prior_populations") or []):
            if not isinstance(prior, Mapping):
                continue
            label = (
                "pre-stamp" if prior.get("pre_stamp")
                else str(prior.get("judge_pipeline_version"))
            )
            rates = " ".join(
                f"{h}:persistence={(prior.get('horizons') or {}).get(h, {}).get('persistence_rate')}"
                f"/n={(prior.get('horizons') or {}).get(h, {}).get('scored')}"
                for h in sorted(prior.get("horizons") or {})
            )
            body_lines.append(
                f"prior_population[{label}]: claims={prior.get('claims_total')} "
                f"{rates} (its OWN population — never summed into the headline)"
            )
    body_lines.append(f"honesty: {HONESTY_NOTE}")
    if warnings:
        body_lines.append(f"warnings={warnings}")
    data: dict[str, Any] = {
        "sub_handler": SUB_HANDLER_NAME,
        "band_calibration": {
            **dict(summary),
            "logged_this_run": int(logged),
            "logged_semantics_migration_this_run": int(logged_semantics_migration),
            "resolved_this_run": resolved_this_run,
            "resolved_this_run_by_horizon": dict(resolved_by_horizon),
            "skipped_non_directional": int(skipped_non_directional),
            "scanned_scorecard_rows": int(scanned_rows),
            "resolved_by": RESOLVED_BY,
        },
        "warnings": warnings,
    }
    return FindingPayload(
        title=head[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "calibration"],
        data=data,
    )


# ---------------------------------------------------------------------------
# Watermark I/O (the tracker's OWN state — never alert_trigger_watermarks)
# ---------------------------------------------------------------------------

_WM_LOAD_SQL = """
    SELECT state FROM band_calibration_scan_state WHERE state_key = $1
"""

_WM_SAVE_SQL = """
    INSERT INTO band_calibration_scan_state (state_key, state, updated_at)
    VALUES ($1, $2::jsonb, now())
    ON CONFLICT (state_key) DO UPDATE
       SET state = EXCLUDED.state, updated_at = now()
"""


async def _load_watermark(conn: Any) -> datetime:
    row = await conn.fetchrow(_WM_LOAD_SQL, _SCAN_STATE_KEY)
    if row is None:
        return _EPOCH
    state = _parse_jsonish(row["state"]) or {}
    raw = state.get("last_produced_at") if isinstance(state, Mapping) else None
    if not raw:
        return _EPOCH
    try:
        ts = datetime.fromisoformat(str(raw))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return _EPOCH


async def _save_watermark(conn: Any, ts: datetime) -> None:
    await conn.execute(
        _WM_SAVE_SQL,
        _SCAN_STATE_KEY,
        json.dumps({"last_produced_at": ts.isoformat()}),
    )


# ---------------------------------------------------------------------------
# (a) Claim logging — band transitions since the watermark
# ---------------------------------------------------------------------------

# New rows past the watermark (earliest-first + capped, so a backlog advances
# contiguously) + ONE base row per affected desk (the FROM side of the first
# comparison). Superseded rows are included on both sides — the transition
# history IS the superseded chain.
_SCAN_SQL = """
    WITH new_rows AS (
        SELECT id, target_id, produced_at, data
          FROM analyst_outputs
         WHERE kind = 'scorecard'
           AND target_id IS NOT NULL
           AND produced_at > $1::timestamptz
         ORDER BY produced_at, id
         LIMIT $2
    ), base_rows AS (
        SELECT DISTINCT ON (target_id) id, target_id, produced_at, data
          FROM analyst_outputs
         WHERE kind = 'scorecard'
           AND produced_at <= $1::timestamptz
           AND target_id IN (SELECT DISTINCT target_id FROM new_rows)
         ORDER BY target_id, produced_at DESC, id DESC
    )
    SELECT id::text AS row_id, target_id, produced_at, data, TRUE  AS is_new
      FROM new_rows
    UNION ALL
    SELECT id::text AS row_id, target_id, produced_at, data, FALSE AS is_new
      FROM base_rows
"""

_INSERT_CLAIM_SQL = """
    INSERT INTO band_calibration_claims
        (desk, dimension, from_band, to_band, direction, transition_at,
         scorecard_row_id, prev_scorecard_row_id, resolution_spec,
         horizon_14_at, horizon_28_at, judge_pipeline_version,
         semantics_migration)
    VALUES ($1, $2, $3, $4, $5, $6::timestamptz, $7::uuid, $8::uuid, $9,
            $6::timestamptz + interval '14 days',
            $6::timestamptz + interval '28 days', $10, $11)
    ON CONFLICT (desk, dimension, scorecard_row_id) DO NOTHING
"""


async def _scan_and_log_claims(
    conn: Any, *, max_scan_rows: int
) -> dict[str, int]:
    """Pair-compare scorecard rows past the watermark; INSERT one claim per
    ladder→ladder transition (dup-proof via the unique index). Advances the
    watermark ONLY over rows actually processed (a cap-truncated backlog
    resumes next run). Returns the run counters.

    H3-GUARD: a transition whose prior/current card carry different
    ``banding_semantics``/``damping_semantics`` (module docstring) is still
    LOGGED — with ``semantics_migration=True`` and ``direction=
    'semantics-migration'`` — never folded into ``skipped_non_directional``,
    which stays reserved for genuine evidence/indeterminate coverage events.
    """
    watermark = await _load_watermark(conn)
    rows = await conn.fetch(_SCAN_SQL, watermark, int(max_scan_rows))

    by_desk: dict[str, list[Any]] = {}
    for row in rows:
        desk = str(row["target_id"] or "")
        if desk:
            by_desk.setdefault(desk, []).append(row)

    logged = 0
    logged_semantics_migration = 0
    skipped_non_directional = 0
    scanned_new = 0
    max_processed: Optional[datetime] = None
    for desk in sorted(by_desk):
        chain = sorted(by_desk[desk], key=lambda r: (r["produced_at"], r["row_id"]))
        prev_row: Any = None
        prev_dims: dict[str, str] = {}
        prev_semantics: tuple[Optional[str], Optional[str]] = (None, None)
        for row in chain:
            dims = extract_dimension_bands(row["data"])
            # H3-GUARD — one card-level read, not per-dimension: every
            # dimension on a card shares the same semantics stamps.
            curr_semantics = extract_card_semantics(row["data"])
            if row["is_new"]:
                scanned_new += 1
                ts = row["produced_at"]
                if max_processed is None or ts > max_processed:
                    max_processed = ts
                if prev_row is not None:
                    changed = scorecard_banding.semantics_changed(
                        prev_semantics, curr_semantics
                    )
                    for dim, band in sorted(dims.items()):
                        prev_band = prev_dims.get(dim)
                        if prev_band is None or prev_band == band:
                            continue
                        direction, _severity = classify_band_transition(
                            prev_band, band, semantics_changed=changed
                        )
                        is_migration = (
                            direction == scorecard_banding.SEMANTICS_MIGRATION
                        )
                        if direction not in CLAIMABLE_DIRECTIONS and not is_migration:
                            # Evidence / indeterminate transitions carry no
                            # directional risk statement — not a claim.
                            skipped_non_directional += 1
                            continue
                        res = await conn.execute(
                            _INSERT_CLAIM_SQL,
                            desk,
                            dim,
                            prev_band,
                            band,
                            direction,
                            row["produced_at"],
                            row["row_id"],
                            prev_row["row_id"],
                            RESOLUTION_SPEC,
                            # The population SPLIT KEY, stamped at LOG time —
                            # the judge that graded the faithfulness the band
                            # rests on. Resolution happens 14/28 days later,
                            # possibly under a different judge; this is what
                            # lets the aggregate refuse to pool across a swap.
                            JUDGE_PIPELINE_VERSION,
                            is_migration,
                        )
                        if isinstance(res, str) and res.endswith("1"):
                            logged += 1
                            if is_migration:
                                logged_semantics_migration += 1
            prev_row = row
            prev_dims = dims
            prev_semantics = curr_semantics

    if max_processed is not None:
        await _save_watermark(conn, max_processed)
    return {
        "logged": logged,
        "logged_semantics_migration": logged_semantics_migration,
        "skipped_non_directional": skipped_non_directional,
        "scanned_rows": scanned_new,
    }


# ---------------------------------------------------------------------------
# (b) Resolution — horizons that have passed, later scorecard rows ONLY
# ---------------------------------------------------------------------------

# The band read: the latest scorecard row (superseded included — the frozen
# historical record) STRICTLY after the transition and at/before the horizon.
_BAND_AT_SQL = """
    SELECT data
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND target_id = $1
       AND produced_at > $2::timestamptz
       AND produced_at <= $3::timestamptz
     ORDER BY produced_at DESC, id DESC
     LIMIT 1
"""


def _horizon_sql(days: int) -> tuple[str, str]:
    """(due-claims SQL, update SQL) for one horizon's column family. ``days``
    comes from the closed HORIZON_DAYS tuple — never user input."""
    if days not in HORIZON_DAYS:  # defensive — closed vocabulary
        raise ValueError(f"unknown horizon {days!r} (valid: {HORIZON_DAYS})")
    due = f"""
        SELECT id::text AS claim_id, desk, dimension, direction, to_band,
               transition_at, horizon_{days}_at AS horizon_at
          FROM band_calibration_claims
         WHERE outcome_{days} IS NULL
           AND horizon_{days}_at < $1::timestamptz
           AND (resolved_by_{days} IS NULL
                OR resolved_by_{days} NOT LIKE 'voided:%')
         ORDER BY horizon_{days}_at
         LIMIT $2
    """
    update = f"""
        UPDATE band_calibration_claims
           SET resolved_band_{days} = $2,
               outcome_{days} = $3,
               resolved_by_{days} = $4,
               resolved_at_{days} = $5::timestamptz
         WHERE id = $1::uuid AND outcome_{days} IS NULL
    """
    return due, update


async def _band_at(
    conn: Any, desk: str, dimension: str, after: datetime, at_or_before: datetime
) -> Optional[str]:
    row = await conn.fetchrow(_BAND_AT_SQL, desk, after, at_or_before)
    if row is None:
        return None
    return extract_dimension_bands(row["data"]).get(dimension)


async def _resolve_due_claims(conn: Any, *, now: datetime) -> dict[str, int]:
    """Grade every claim whose horizon has passed. Never overwrites an
    already-resolved horizon (the UPDATE re-guards on ``outcome IS NULL``);
    voided horizons are excluded by the due query. Returns per-horizon counts."""
    resolved: dict[str, int] = {}
    for days in HORIZON_DAYS:
        due_sql, update_sql = _horizon_sql(days)
        count = 0
        due = await conn.fetch(due_sql, now, _MAX_RESOLVE_PER_HORIZON)
        for claim in due:
            band = await _band_at(
                conn,
                str(claim["desk"]),
                str(claim["dimension"]),
                claim["transition_at"],
                claim["horizon_at"],
            )
            outcome = classify_horizon_outcome(
                str(claim["direction"]), str(claim["to_band"]), band
            )
            res = await conn.execute(
                update_sql, claim["claim_id"], band, outcome, RESOLVED_BY, now
            )
            if isinstance(res, str) and res.endswith("1"):
                count += 1
        resolved[f"{days}d"] = count
    return resolved


# ---------------------------------------------------------------------------
# (c) Aggregate pull for the summary finding
# ---------------------------------------------------------------------------

# The aggregate is scoped to ONE judge population. Bands rest on
# faithfulness-gated findings, so pooling claims graded by different judges
# reports a rate for a population that never existed — and the live history
# straddles a swap (07-30 20:14Z) that moved mean faithfulness +7pp.
#
# 2026-08-29 — "ONE population" is now the POOL, not the single head stamp: the
# set of consecutive stamps whose lineage declares no `faithfulness_score` shift
# across their boundaries (module docstring; `poolable_stamps`). The parameter
# is a text[] rather than a scalar and every query below shares it, so the
# headline, the exclusion counters and the prior-population rollup can never
# disagree about where the boundary is. A pool of one is exactly the old
# behaviour, which is what makes this safe to widen only by declaration.
#
# H3-GUARD: `AND NOT semantics_migration` on every query below (this one, the
# excluded-count query, and the prior-populations query) — that axis is
# ORTHOGONAL to the judge-pipeline split (a semantics-migration claim can carry
# any judge_pipeline_version) and is reported on its OWN, via
# `_AGG_SEMANTICS_MIGRATION_SQL`, so it is never silently absorbed into either
# the headline or a prior-population block.
_AGG_SQL = """
    SELECT dimension, direction, outcome_14, outcome_28
      FROM band_calibration_claims
     WHERE transition_at > now() - make_interval(days => $1)
       AND judge_pipeline_version = ANY($3::text[])
       AND NOT semantics_migration
     ORDER BY transition_at
     LIMIT $2
"""

# What the filter left out, so the readout can SAY so rather than quietly
# shrinking. `NULL` = logged before the split key existed; a non-null stamp
# outside the pool = logged under a judge pipeline this one may not pool with.
_AGG_EXCLUDED_SQL = """
    SELECT
        count(*) FILTER (WHERE judge_pipeline_version IS NULL)::int  AS pre_stamp,
        count(*) FILTER (WHERE judge_pipeline_version IS NOT NULL
                           AND NOT (judge_pipeline_version = ANY($2::text[])))::int
                                                                     AS other_stamp
      FROM band_calibration_claims
     WHERE transition_at > now() - make_interval(days => $1)
       AND NOT semantics_migration
"""

# H3-GUARD — the semantics-migration exclusion count, over the SAME lookback
# window, independent of judge pipeline (a claim is excluded here for what its
# TWO SCORECARDS disagreed about, not which judge graded either one).
_AGG_SEMANTICS_MIGRATION_SQL = """
    SELECT count(*)::int AS n
      FROM band_calibration_claims
     WHERE transition_at > now() - make_interval(days => $1)
       AND semantics_migration
"""

# M-2 — the EXCLUDED claims, kept as ANNOTATED PRIOR POPULATIONS rather than a
# bare count.
#
# Migration 0122 added the split key and deliberately never backfilled it ("a
# guessed stamp would fabricate the very comparability this column exists to
# deny"), which is right — and it means that on the day the current-stamp filter
# goes live EVERY existing claim is NULL-stamped and the headline rates go to
# n=0. Reporting only `excluded_pre_stamp=777` turns a working readout into a
# blank one and calls that honesty.
#
# It is not the only honest option. Each prior pipeline is its OWN population
# with its OWN rates: reported side by side, labelled, and never summed or
# averaged into the headline. The operator keeps the history, and nothing pools.
#
# Priors stay split PER STAMP even though the headline pools: a prior block is
# never merged with another (this function's whole contract), and showing the
# history at its finest grain can only help a reader who wants to check the
# pooling decision for themselves. Pooling widens the HEADLINE; it never
# coarsens the record beside it.
_AGG_PRIORS_SQL = """
    SELECT judge_pipeline_version, dimension, direction, outcome_14, outcome_28
      FROM band_calibration_claims
     WHERE transition_at > now() - make_interval(days => $1)
       AND (judge_pipeline_version IS NULL
            OR NOT (judge_pipeline_version = ANY($3::text[])))
       AND NOT semantics_migration
     ORDER BY transition_at
     LIMIT $2
"""


async def _pull_claims_for_summary(
    conn: Any, *, lookback_days: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(claims for the CURRENT judge population, population-boundary block)``.

    P3 §5a — the `judge_pipeline_version` stamp had writers and no readers, so
    the split key split nothing. This is the reader: the rates describe one
    judge pipeline, and the boundary block reports exactly what that cost.

    2026-08-29 — "one judge pipeline" is the lineage-declared POOL for
    :data:`CALIBRATED_METRIC_FAMILY` (module docstring). The pool is computed
    ONCE here and handed to all three queries, so the headline population, the
    exclusion counters and the prior-population rollup are partitioned by the
    same boundary by construction rather than by three matching literals.
    """
    pool = poolable_stamps(JUDGE_PIPELINE_VERSION, CALIBRATED_METRIC_FAMILY)
    pool_list = list(pool)
    rows = await conn.fetch(_AGG_SQL, int(lookback_days), _MAX_AGG_ROWS, pool_list)
    exc = await conn.fetchrow(_AGG_EXCLUDED_SQL, int(lookback_days), pool_list)
    pre_stamp = int(exc["pre_stamp"]) if exc else 0
    other_stamp = int(exc["other_stamp"]) if exc else 0
    # H3-GUARD — the semantics-migration exclusion, ORTHOGONAL to the judge-
    # pipeline split above: counted once here, never folded into pre_stamp /
    # other_stamp, and never present in `rows` or `prior_rows` (both queries
    # already filter `NOT semantics_migration`).
    sm = await conn.fetchrow(_AGG_SEMANTICS_MIGRATION_SQL, int(lookback_days))
    excluded_semantics_migration = int(sm["n"]) if sm else 0
    prior_rows = await conn.fetch(
        _AGG_PRIORS_SQL, int(lookback_days), _MAX_AGG_ROWS, pool_list
    )
    boundary = {
        # The stamp claims are WRITTEN with — the head of the pool, and the one
        # a new claim logged this run carries.
        "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
        # ...and the population actually READ: the pooled SET, oldest first. A
        # single-element list here means the head pooled with nothing, which is
        # the old behaviour and must stay visible as such.
        "judge_pipeline_versions": list(pool),
        "excluded_pre_stamp": pre_stamp,
        "excluded_other_pipeline": other_stamp,
        "excluded_semantics_migration": excluded_semantics_migration,
        # M-2 — every excluded pipeline as its OWN readout, per STAMP even
        # though the headline pools (see `_AGG_PRIORS_SQL`).
        "prior_populations": summarize_prior_populations(
            prior_rows, lookback_days=lookback_days
        ),
        "pooling": {
            "metric_family": CALIBRATED_METRIC_FAMILY,
            "stamps": list(pool),
            "stamp_count": len(pool),
            # How many stamps beyond the head the lineage licensed. 0 = the
            # lineage declared a real shift at every reachable boundary and this
            # readout is exactly as narrow as it was before pooling existed.
            "widened_by": len(pool) - 1,
            "note": (
                "Stamps are pooled only across boundaries whose lineage entry "
                "declares NO expected shift for this metric family "
                "(judge_pipeline_version.STAMP_EXPECTED_SHIFTS); the relation "
                "is transitive over consecutive such boundaries and any "
                "declared shift is a HARD stop. An unregistered or ambiguous "
                "stamp pools with nothing. widened_by=0 means the pool is the "
                "head stamp alone — no widening happened and this rate is as "
                "thin as it looks."
            ),
        },
        "note": (
            "Rates cover ONE judge population — the lineage-declared POOL for "
            "the " + CALIBRATED_METRIC_FAMILY + " family, listed in "
            "judge_pipeline_versions. Claims graded under a pipeline outside "
            "that pool, or logged before the split key existed, are excluded "
            "rather than pooled — a band rests on faithfulness, and mixing "
            "judges that are declared to grade differently reports a rate for "
            "a population that never existed. Each excluded pipeline is "
            "reported as its OWN population below, with its own n and its own "
            "rates; nothing is summed across them. Semantics-migration claims "
            "(H3-GUARD: prior/current scorecard computed under different "
            "banding/damping semantics) are a SEPARATE exclusion axis — "
            "counted in excluded_semantics_migration, never in either count "
            "above."
        ),
    }
    claims = [
        {
            "dimension": r["dimension"],
            "direction": r["direction"],
            "outcome_14": r["outcome_14"],
            "outcome_28": r["outcome_28"],
        }
        for r in rows
    ]
    return claims, boundary


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — log → resolve → summarize (module docstring).

    REFUSES LOUD on a missing pool (the integrity_sweep contract): a
    calibration sweep that cannot read the substrate must error visibly, never
    report a quiet zero-claim run.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "band_calibration_tracker requires a live deps.pg_pool — refusing "
            "to report a zero-claim calibration run without reading the "
            "substrate"
        )

    max_scan_rows = int(options.get("max_scan_rows", DEFAULT_MAX_SCAN_ROWS))
    lookback_days = int(options.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    async with pool.acquire() as conn:
        # (a) Log new claims from band transitions since the watermark. NOT
        # wrapped — a broken scan must refuse loud, never under-report.
        scan = await _scan_and_log_claims(conn, max_scan_rows=max_scan_rows)
        # (b) Resolve claims whose horizons have passed (deterministic, reads
        # later scorecard rows only; never overwrites; skips voided).
        resolved_by_horizon = await _resolve_due_claims(conn, now=now)
        # (c) The cumulative aggregate for the honest summary finding.
        claim_rows, population = await _pull_claims_for_summary(
            conn, lookback_days=lookback_days
        )

    summary = summarize_claims(
        claim_rows, lookback_days=lookback_days, population=population
    )
    logger.info(
        "band_calibration_tracker.tick logged=%d logged_semantics_migration=%d "
        "resolved=%s claims_total=%d scanned=%d skipped_non_directional=%d "
        "judge_pipeline_version=%s pooled_versions=%s widened_by=%d "
        "metric_family=%s excluded_pre_stamp=%d "
        "excluded_other_pipeline=%d excluded_semantics_migration=%d",
        scan["logged"],
        scan["logged_semantics_migration"],
        resolved_by_horizon,
        summary["claims_total"],
        scan["scanned_rows"],
        scan["skipped_non_directional"],
        population["judge_pipeline_version"],
        population["judge_pipeline_versions"],
        population["pooling"]["widened_by"],
        population["pooling"]["metric_family"],
        population["excluded_pre_stamp"],
        population["excluded_other_pipeline"],
        population["excluded_semantics_migration"],
    )
    finding = build_finding(
        summary=summary,
        logged=scan["logged"],
        resolved_by_horizon=resolved_by_horizon,
        skipped_non_directional=scan["skipped_non_directional"],
        scanned_rows=scan["scanned_rows"],
        warnings=warnings,
        logged_semantics_migration=scan["logged_semantics_migration"],
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "CALIBRATED_METRIC_FAMILY",
    "CLAIMABLE_DIRECTIONS",
    "CONFIRMED_OUTCOMES",
    "HONESTY_NOTE",
    "HORIZON_DAYS",
    "RESOLUTION_SPEC",
    "RESOLVED_BY",
    "SUB_HANDLER_NAME",
    "build_finding",
    "classify_horizon_outcome",
    "extract_card_semantics",
    "extract_dimension_bands",
    "handle",
    "summarize_claims",
]
