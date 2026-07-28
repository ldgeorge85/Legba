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

Registered via ``scripts/bringup_register_band_calibration_tracker.py`` — NOT
inline through a test fixture. Ships ``state: draft``; migration 0093 must be
applied BEFORE first activation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ...provenance.models import FindingPayload
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
    rows: list[Mapping[str, Any]], *, lookback_days: int
) -> dict[str, Any]:
    """Fold claim rows into the cumulative band-calibration aggregate.

    ``rows`` carry at least ``dimension`` / ``direction`` / ``outcome_14`` /
    ``outcome_28``. Pure + deterministic; every horizon block reports outcome
    counts, the confirmed/reverted split, and rates with honest-None on empty
    denominators. Split three ways: overall, by direction (deteriorations vs
    improvements), by dimension.
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
        # The honesty contract, machine-checkable + verbatim.
        "no_brier": True,
        "honesty_note": HONESTY_NOTE,
    }


def build_finding(
    *,
    summary: Mapping[str, Any],
    logged: int,
    resolved_by_horizon: Mapping[str, int],
    skipped_non_directional: int,
    scanned_rows: int,
    warnings: list[str],
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
    body_lines.append(f"honesty: {HONESTY_NOTE}")
    if warnings:
        body_lines.append(f"warnings={warnings}")
    data: dict[str, Any] = {
        "sub_handler": SUB_HANDLER_NAME,
        "band_calibration": {
            **dict(summary),
            "logged_this_run": int(logged),
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
         horizon_14_at, horizon_28_at)
    VALUES ($1, $2, $3, $4, $5, $6::timestamptz, $7::uuid, $8::uuid, $9,
            $6::timestamptz + interval '14 days',
            $6::timestamptz + interval '28 days')
    ON CONFLICT (desk, dimension, scorecard_row_id) DO NOTHING
"""


async def _scan_and_log_claims(
    conn: Any, *, max_scan_rows: int
) -> dict[str, int]:
    """Pair-compare scorecard rows past the watermark; INSERT one claim per
    ladder→ladder transition (dup-proof via the unique index). Advances the
    watermark ONLY over rows actually processed (a cap-truncated backlog
    resumes next run). Returns the run counters."""
    watermark = await _load_watermark(conn)
    rows = await conn.fetch(_SCAN_SQL, watermark, int(max_scan_rows))

    by_desk: dict[str, list[Any]] = {}
    for row in rows:
        desk = str(row["target_id"] or "")
        if desk:
            by_desk.setdefault(desk, []).append(row)

    logged = 0
    skipped_non_directional = 0
    scanned_new = 0
    max_processed: Optional[datetime] = None
    for desk in sorted(by_desk):
        chain = sorted(by_desk[desk], key=lambda r: (r["produced_at"], r["row_id"]))
        prev_row: Any = None
        prev_dims: dict[str, str] = {}
        for row in chain:
            dims = extract_dimension_bands(row["data"])
            if row["is_new"]:
                scanned_new += 1
                ts = row["produced_at"]
                if max_processed is None or ts > max_processed:
                    max_processed = ts
                if prev_row is not None:
                    for dim, band in sorted(dims.items()):
                        prev_band = prev_dims.get(dim)
                        if prev_band is None or prev_band == band:
                            continue
                        direction, _severity = classify_band_transition(
                            prev_band, band
                        )
                        if direction not in CLAIMABLE_DIRECTIONS:
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
                        )
                        if isinstance(res, str) and res.endswith("1"):
                            logged += 1
            prev_row = row
            prev_dims = dims

    if max_processed is not None:
        await _save_watermark(conn, max_processed)
    return {
        "logged": logged,
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

_AGG_SQL = """
    SELECT dimension, direction, outcome_14, outcome_28
      FROM band_calibration_claims
     WHERE transition_at > now() - make_interval(days => $1)
     ORDER BY transition_at
     LIMIT $2
"""


async def _pull_claims_for_summary(
    conn: Any, *, lookback_days: int
) -> list[dict[str, Any]]:
    rows = await conn.fetch(_AGG_SQL, int(lookback_days), _MAX_AGG_ROWS)
    return [
        {
            "dimension": r["dimension"],
            "direction": r["direction"],
            "outcome_14": r["outcome_14"],
            "outcome_28": r["outcome_28"],
        }
        for r in rows
    ]


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
        claim_rows = await _pull_claims_for_summary(
            conn, lookback_days=lookback_days
        )

    summary = summarize_claims(claim_rows, lookback_days=lookback_days)
    logger.info(
        "band_calibration_tracker.tick logged=%d resolved=%s claims_total=%d "
        "scanned=%d skipped_non_directional=%d",
        scan["logged"],
        resolved_by_horizon,
        summary["claims_total"],
        scan["scanned_rows"],
        scan["skipped_non_directional"],
    )
    finding = build_finding(
        summary=summary,
        logged=scan["logged"],
        resolved_by_horizon=resolved_by_horizon,
        skipped_non_directional=scan["skipped_non_directional"],
        scanned_rows=scan["scanned_rows"],
        warnings=warnings,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "CLAIMABLE_DIRECTIONS",
    "CONFIRMED_OUTCOMES",
    "HONESTY_NOTE",
    "HORIZON_DAYS",
    "RESOLUTION_SPEC",
    "RESOLVED_BY",
    "SUB_HANDLER_NAME",
    "build_finding",
    "classify_horizon_outcome",
    "extract_dimension_bands",
    "handle",
    "summarize_claims",
]
