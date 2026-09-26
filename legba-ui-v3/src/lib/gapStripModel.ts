/**
 * gapStripModel — the desk gap-strip state derivation (7b-ii).
 *
 * A desk is nine bounded units (docs/ANALYSIS.md §4.1): `leadership_transition`,
 * `energy_security`, `escalation`, `narrative_coordination`, `internal_stability`,
 * `military_posture`, `economic_coercion` and `proliferation_watch` fan out
 * across the country desks; `disruption_status` is the ninth, tag-scoped off the
 * country plane to the thematic lane instead. A reader today cannot see at a
 * glance which of the nine have a current read, which banded insufficient
 * evidence, which are stale, and which never ran — this module is the pure
 * classifier the gap-strip component renders, kept DOM-free so the four states
 * are tested without a browser.
 *
 * SOURCES — no new backend route, no changed route. Two routes the UI already
 * calls elsewhere carry every field the strip needs:
 *
 *   * `GET /findings?analyst_id_in=<unit ids>&target_id=` (the
 *     `CountryUnitsAssessment` precedent) — the actual latest finding per unit,
 *     which is the ground truth for "does a read exist" and "when".
 *   * `GET /v3/eval/country_scorecard?target_id=` (`CountryScorecard`, already
 *     wired in `lib/evalOps.ts` / `EvalScorecard.tsx`) — the banded scorecard's
 *     per-dimension verdict, which carries `band`, `reason` and `critic_score`
 *     (the folded judge/faithfulness number the band rests on).
 *
 * STATE — one of four, in priority order:
 *
 *   * `none`        — no finding at all for this unit (the plain `/findings`
 *     read found nothing). A declared gap, never a blank cell.
 *   * `insufficient` — a finding exists, and the scorecard excluded it from its
 *     dimension's basis for a REAL reason (`verify-failed` / `below-floor` /
 *     `low-faithfulness` / `no-severity-tag`) — `docs/ANALYSIS.md` §4.7's
 *     "insufficient evidence is a first-class outcome". `reason ===
 *     'no-finding'` does NOT count here: that is the scorecard's own 336h
 *     window seeing nothing, which is a staleness fact about an OLDER finding
 *     the `/findings` read still has, never a grading fact about it — see
 *     `deriveGapCell` below.
 *   * `stale`       — a real (non-insufficient) latest read older than the
 *     unit's own cadence, with grace.
 *   * `current`     — a real latest read inside the unit's cadence.
 *
 * CADENCE. Every one of the nine unit descriptors declares
 * `cadence.fallback_schedule` as a 2×/day staggered cron (e.g.
 * `descriptors/analyst_leadership_transition.yaml`: `"0 1,13 * * *"`,
 * `cooldown_seconds: 39600`) — a 12h interval today, for every unit. `GAP_STRIP_UNITS`
 * carries the interval PER UNIT (never one shared constant read by every cell)
 * so a future descriptor on a different cadence changes only its own row; nothing
 * here assumes the nine stay in lockstep.
 */
import { isInsufficient, type DimensionBand } from '@/lib/evalOps'

export type GapCellState = 'current' | 'insufficient' | 'stale' | 'none'

export interface GapUnit {
  id: string
  label: string
  /** Short chip glyph (2-3 chars) — the strip is a dense row of cells. */
  short: string
  /** Hours between scheduled fires, from the unit's OWN descriptor cadence. */
  cadenceHours: number
}

/** The nine bounded units, docs/ANALYSIS.md §4.1, display order. */
export const GAP_STRIP_UNITS: GapUnit[] = [
  { id: 'leadership_transition', label: 'Leadership transition', short: 'LT', cadenceHours: 12 },
  { id: 'energy_security', label: 'Energy security', short: 'ES', cadenceHours: 12 },
  { id: 'escalation', label: 'Escalation', short: 'ESC', cadenceHours: 12 },
  { id: 'narrative_coordination', label: 'Narrative / coordination', short: 'NC', cadenceHours: 12 },
  { id: 'internal_stability', label: 'Internal stability', short: 'IS', cadenceHours: 12 },
  { id: 'military_posture', label: 'Military posture', short: 'MP', cadenceHours: 12 },
  { id: 'economic_coercion', label: 'Economic coercion', short: 'EC', cadenceHours: 12 },
  { id: 'proliferation_watch', label: 'Proliferation watch', short: 'PW', cadenceHours: 12 },
  { id: 'disruption_status', label: 'Disruption status', short: 'DS', cadenceHours: 12 },
]

/**
 * Grace over the cadence interval before a real (non-insufficient) read reads
 * `stale` — covers the staggered-slot jitter, not a fresh cadence. Mirrors the
 * house idiom (`source_freshness.GRACE_MULTIPLE`) at a smaller multiple: these
 * are clock-scheduled fires, not event-driven polls.
 */
export const GAP_STRIP_GRACE_MULTIPLE = 1.5

/** The unit's actual latest finding, however it banded. */
export interface GapLatestFinding {
  id: string
  producedAt: string
  severity?: string | null
}

export interface GapCellResult {
  unitId: string
  state: GapCellState
  /** The latest read's own instant, or `null` for `none`. */
  producedAt: string | null
  /** The finding id to select into the Inspector, or `null` for `none`. */
  findingId: string | null
  /** The scorecard's folded judge/faithfulness score, or `null` when
   *  unmeasured — never fabricated. */
  judgeScore: number | null
  /** The scorecard's machine reason, present only for `insufficient`. */
  reason: string | null
}

/**
 * Classify one unit's cell. Pure — no fetch, no clock read unless `now` is
 * omitted (defaults to the real clock for callers, pinned in tests).
 */
export function deriveGapCell(params: {
  unit: GapUnit
  latestFinding: GapLatestFinding | null | undefined
  dimension: DimensionBand | null | undefined
  now?: Date
}): GapCellResult {
  const { unit, latestFinding, dimension, now = new Date() } = params

  // The scorecard's own judge/faithfulness number for this dimension, when it
  // has one — surfaced regardless of state so a `stale` or `current` cell can
  // still show it on hover.
  const judgeScore = typeof dimension?.critic_score === 'number' ? dimension.critic_score : null

  if (!latestFinding) {
    return {
      unitId: unit.id,
      state: 'none',
      producedAt: null,
      findingId: null,
      judgeScore,
      reason: null,
    }
  }

  // A real exclusion — the scorecard SAW this unit's latest-eligible finding
  // and refused to band it. `reason === 'no-finding'` means the opposite: the
  // scorecard's own window (336h) saw nothing, which says nothing about
  // whether the `/findings` read above is fresh or stale — that is decided by
  // the cadence check below, exactly as it would be with no scorecard at all.
  if (dimension && isInsufficient(dimension) && dimension.reason !== 'no-finding') {
    return {
      unitId: unit.id,
      state: 'insufficient',
      producedAt: latestFinding.producedAt,
      findingId: latestFinding.id,
      judgeScore,
      reason: dimension.reason,
    }
  }

  const ageMs = now.getTime() - Date.parse(latestFinding.producedAt)
  const staleThresholdMs = unit.cadenceHours * 60 * 60 * 1000 * GAP_STRIP_GRACE_MULTIPLE
  const stale = !Number.isFinite(ageMs) || ageMs > staleThresholdMs

  return {
    unitId: unit.id,
    state: stale ? 'stale' : 'current',
    producedAt: latestFinding.producedAt,
    findingId: latestFinding.id,
    judgeScore,
    reason: null,
  }
}

/** One cell per configured unit, keyed by `analyst_id` against the two feeds. */
export function deriveGapStrip(params: {
  units?: GapUnit[]
  latestByUnit: Map<string, GapLatestFinding>
  dimensions: Record<string, DimensionBand> | null | undefined
  now?: Date
}): GapCellResult[] {
  const { units = GAP_STRIP_UNITS, latestByUnit, dimensions, now } = params
  return units.map((unit) =>
    deriveGapCell({
      unit,
      latestFinding: latestByUnit.get(unit.id),
      dimension: dimensions?.[unit.id],
      now,
    }),
  )
}
