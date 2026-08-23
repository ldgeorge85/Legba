/**
 * evalBoards — the pure model behind `system.eval_boards`.
 *
 * THREE server boards had a live route and no UI consumer at all:
 *
 *   1. `GET /v3/eval/desk_baselines`  → `DeskBaselineBoard`
 *   2. `GET /v3/eval/band_trajectory` → `BandTrajectoryResponse`
 *   3. `GET /v3/eval/analyst_runtime` → `AnalystRuntimeRow[]`
 *
 * Each one ships a different honesty hazard, and each hazard is defused HERE —
 * as a pure function with a unit test — rather than in JSX where it would be
 * one careless edit away from disappearing:
 *
 *   * DESK BASELINES are a DESCRIPTIVE statistical band over our own substrate.
 *     They are not a forecast, not a prediction, and not a skill claim. The
 *     server ships that disclaimer in `note`; {@link baselineNote} hands it back
 *     VERBATIM so the panel renders the server's words rather than a paraphrase
 *     that could soften them. `available: false` is a board that was never
 *     computed — {@link baselineBoardState} keeps it distinct from a computed
 *     board that legitimately returned nothing, because "we didn't look" and
 *     "we looked and there was nothing" are different claims. A row with
 *     `insufficient_history` is discounted by {@link insufficientHistoryLabel}
 *     and never reads as a finding.
 *
 *   * BAND TRAJECTORY caps its scan. `truncated: true` means THE LAST DESK
 *     GROUP MAY BE INCOMPLETE, so {@link truncationWarning} says exactly that
 *     and a truncated series must never be read as a full one. `total_rows`
 *     counts SCORECARD ROWS SCANNED, not points — {@link trajectorySummaryLine}
 *     labels the two separately, since calling rows "points" would inflate the
 *     apparent density of the series by an unknown factor.
 *
 *   * ANALYST RUNTIME is the one board with NO server-side degradation wrapper.
 *     Its siblings answer a read failure with an all-defaults 200; this one
 *     answers with a real 500. {@link runtimeErrorText} therefore says the board
 *     could not be read — a panel that swallowed the error and drew an empty
 *     table would be asserting "no analyst ran", which is a different and
 *     probably false statement.
 *
 * CROSS-CUTTING, and the reason most of these return strings rather than
 * numbers: every rate or mean carries the n it was computed over
 * ({@link rateWithN}, {@link avgSecondsLabel}, {@link baselineSampleLabel}),
 * and a null statistic renders as {@link NOT_RECORDED} — an absence — never as
 * 0. `effective_confidence: null` is "the judge recorded nothing", which is not
 * the same as "the judge recorded no confidence".
 *
 * Band tone/ordering is NOT re-implemented here: `@/lib/evalOps` already owns
 * `bandTone` and `relTime`, and this module re-exports them so the eval
 * surfaces agree on what a band colour means.
 */

import { ApiError } from '@/lib/api'
import type {
  AnalystRuntimeRow,
  BandTrajectoryResponse,
  DeskBaselineBoard,
  DeskBaselineRow,
  DeskTrajectory,
  TrajectoryPoint,
} from '@/lib/api'
import { bandTone, relTime } from '@/lib/evalOps'
import type { BandTone } from '@/lib/evalOps'

// The eval surfaces share one band vocabulary — re-exported, not re-derived.
export { bandTone, relTime }
export type { BandTone }

// ===========================================================================
// Cross-cutting: absence, rates-with-their-n, durations, read failures
// ===========================================================================

/** The honest render of a null statistic. NEVER substitute 0 for this. */
export const NOT_RECORDED = 'not recorded'

/**
 * A rate that can never be read without its denominator.
 *
 * A zero denominator has no rate at all — it returns the empty label rather
 * than `0%`, because "0 of 0" is not a measurement of anything.
 */
export function rateWithN(
  numerator: number,
  denominator: number,
  opts: { emptyLabel?: string; digits?: number } = {},
): string {
  if (!Number.isFinite(denominator) || denominator <= 0) {
    return opts.emptyLabel ?? 'no observations yet (n=0)'
  }
  const pct = (numerator / denominator) * 100
  const digits = opts.digits ?? (pct > 0 && pct < 1 ? 1 : 0)
  return `${pct.toFixed(digits)}% (${numerator}/${denominator})`
}

/** A duration in seconds; null is an absence, never `0.00s`. */
export function formatSeconds(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return NOT_RECORDED
  if (seconds < 10) return `${seconds.toFixed(2)}s`
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const total = Math.round(seconds)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}m ${String(s).padStart(2, '0')}s`
}

/** A plain number for display — integers stay integers, ratios get 2dp. */
export function formatMetric(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NOT_RECORDED
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

function apiDetail(body: unknown): string {
  if (typeof body === 'string') return body
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail
    return typeof d === 'string' ? d : JSON.stringify(d)
  }
  return ''
}

/**
 * A failed read, said as a failed read.
 *
 * The tail sentence is the load-bearing part: it forbids reading the blank
 * space below the message as an empty board.
 */
export function boardErrorText(err: unknown, board: string): string {
  const tail = 'Nothing is shown below — this is a failed read, not an empty board.'
  if (err instanceof ApiError) {
    const detail = apiDetail(err.body)
    const suffix = detail ? ` — ${detail}` : ''
    if (err.status === 400) {
      return `${board}: the server REJECTED the request (400)${suffix}. It validates rather than clamps, so an out-of-range parameter fails outright. ${tail}`
    }
    if (err.status === 404) {
      return `${board}: the route answered 404${suffix} — this build is pointed at a registry that does not serve this board. ${tail}`
    }
    return `${board}: HTTP ${err.status}${suffix}. ${tail}`
  }
  const msg = err instanceof Error ? err.message : String(err)
  return `${board}: ${msg}. ${tail}`
}

// ===========================================================================
// Board 1 — desk baselines (`GET /v3/eval/desk_baselines`)
// ===========================================================================

/**
 * `unavailable` — the board was never computed (`available: false`).
 * `empty`       — computed, and it really did return no rows.
 * `ready`       — computed, with rows.
 *
 * Collapsing the first two would turn "we could not look" into "there is
 * nothing to see", which is the single most consequential lie this panel could
 * tell.
 */
export type BaselineBoardState = 'unavailable' | 'empty' | 'ready'

export function baselineBoardState(
  board: DeskBaselineBoard | null | undefined,
): BaselineBoardState {
  if (!board || !board.available) return 'unavailable'
  return board.rows && board.rows.length > 0 ? 'ready' : 'empty'
}

export function baselineStateMessage(state: BaselineBoardState): string {
  switch (state) {
    case 'unavailable':
      return (
        'NOT COMPUTED — the server reports `available: false` for this board. ' +
        'No baseline exists to read; this is an absent computation, not a quiet substrate.'
      )
    case 'empty':
      return (
        'Computed, and it returned no rows — a measured emptiness, not a failed read. ' +
        'Every desk-metric either has no baseline row or fell outside the current filter.'
      )
    default:
      return ''
  }
}

/**
 * The server's own disclaimer, VERBATIM.
 *
 * `verbatim: false` marks the fallback used when the server sent no note at
 * all, so the panel can show that the wording is ours rather than the server's.
 * The fallback still refuses the forecast reading — a band with no disclaimer
 * attached is exactly the case where one gets invented by the reader.
 */
export function baselineNote(
  board: DeskBaselineBoard | null | undefined,
): { text: string; verbatim: boolean } {
  const note = board?.note?.trim()
  if (note) return { text: note, verbatim: true }
  return {
    text:
      'The server sent no note with this board. Read these bands as DESCRIPTIVE statistics ' +
      'over our own substrate — not a forecast, not a prediction, not a skill claim.',
    verbatim: false,
  }
}

/** The server's counts, stated as counts — nothing derived, nothing inferred. */
export function baselineCountsLine(board: DeskBaselineBoard | null | undefined): string {
  if (!board) return 'no board read'
  const counts = board.counts ?? {}
  const total = counts.total ?? board.rows.length
  const parts = [`${total} desk-metric row${total === 1 ? '' : 's'}`]
  if (counts.above != null) parts.push(`${counts.above} above band`)
  if (counts.below != null) parts.push(`${counts.below} below band`)
  if (counts.insufficient_history != null) {
    parts.push(`${counts.insufficient_history} on insufficient history`)
  }
  return parts.join(' · ')
}

export type DeviationDirection = 'above' | 'below' | 'within' | 'unknown'

/** Never invents a direction: an unrecognised wire value stays `unknown`. */
export function deviationDirection(
  row: Pick<DeskBaselineRow, 'deviation'>,
): DeviationDirection {
  if (row.deviation === 'above' || row.deviation === 'below' || row.deviation === 'within') {
    return row.deviation
  }
  return 'unknown'
}

/** The deviation, with its σ — or with the honest note that σ is absent. */
export function deviationLabel(
  row: Pick<DeskBaselineRow, 'deviation' | 'deviation_sigma'>,
): string {
  const dir = deviationDirection(row)
  const sigma = row.deviation_sigma
  const sigmaPart = sigma == null ? 'σ not computed' : `${sigma.toFixed(2)}σ`
  switch (dir) {
    case 'above':
      return `above band (${sigmaPart})`
    case 'below':
      return `below band (${sigmaPart})`
    case 'within':
      return sigma == null ? 'within band' : `within band (${sigmaPart})`
    default:
      return `unrecognised deviation “${String(row.deviation)}”`
  }
}

/** The band itself, with the centre and the spread it was built from. */
export function bandLabel(
  row: Pick<
    DeskBaselineRow,
    'band_low' | 'band_high' | 'center_median' | 'robust_sigma' | 'n_sigma'
  >,
): string {
  return (
    `${formatMetric(row.band_low)} – ${formatMetric(row.band_high)} ` +
    `(median ${formatMetric(row.center_median)} ± ${formatMetric(row.robust_sigma)} × ${formatMetric(row.n_sigma)}σ)`
  )
}

/** The n behind the band — active days out of the sample, over the window. */
export function baselineSampleLabel(
  row: Pick<DeskBaselineRow, 'active_days' | 'sample_days' | 'baseline_days'>,
): string {
  return `${row.active_days}/${row.sample_days} active days over a ${row.baseline_days}d baseline`
}

/**
 * The discount on a thin row, or null when the row has real history behind it.
 * Returned as a string so the panel cannot render the flag without the reason.
 */
export function insufficientHistoryLabel(
  row: Pick<DeskBaselineRow, 'insufficient_history' | 'active_days' | 'baseline_days'>,
): string | null {
  if (!row.insufficient_history) return null
  return (
    `insufficient history — ${row.active_days} active of ${row.baseline_days} baseline days; ` +
    'DISCOUNTED, not a finding'
  )
}

/** The expanded row detail: every remaining wire field, said plainly. */
export function baselineRowFacts(row: DeskBaselineRow): Array<{ key: string; value: string }> {
  const featureKeys = Object.keys(row.features ?? {})
  return [
    { key: 'metric', value: row.metric },
    { key: 'geo', value: row.geo.length > 0 ? row.geo.join(', ') : '(no geo scope)' },
    { key: 'current', value: formatMetric(row.current) },
    { key: 'expected', value: formatMetric(row.expected) },
    { key: 'spillover current', value: formatMetric(row.spillover_current) },
    { key: 'min current floor', value: formatMetric(row.min_current_floor) },
    { key: 'band', value: bandLabel(row) },
    { key: 'sample', value: baselineSampleLabel(row) },
    {
      key: 'features',
      value: featureKeys.length > 0 ? featureKeys.join(', ') : '(none carried)',
    },
    {
      key: 'computed at',
      value: row.computed_at ? `${row.computed_at} (${relTime(row.computed_at)})` : 'not stamped',
    },
  ]
}

/**
 * Rows arrive most-deviating-first and that order is the server's judgement
 * about what deserves attention. Preserved as-is: this returns a copy so
 * callers cannot mutate the query cache, and re-sorts nothing.
 */
export function orderedBaselineRows(rows: DeskBaselineRow[]): DeskBaselineRow[] {
  return [...rows]
}

// ===========================================================================
// Board 2 — band trajectory (`GET /v3/eval/band_trajectory`)
// ===========================================================================

/** The server VALIDATES this range and 400s outside it — it does not clamp. */
export const TRAJECTORY_DAYS_MIN = 1
export const TRAJECTORY_DAYS_MAX = 90
export const TRAJECTORY_DAY_CHOICES: readonly number[] = [7, 14, 30, 60, 90]

export function isValidTrajectoryDays(days: number): boolean {
  return (
    Number.isInteger(days) && days >= TRAJECTORY_DAYS_MIN && days <= TRAJECTORY_DAYS_MAX
  )
}

export interface TrajectoryTotals {
  desks: number
  /** desk × dimension series, not dimension names. */
  dimensions: number
  /** actual banded POINTS — the thing `total_rows` is not. */
  points: number
  flagged: number
  withConfidence: number
  /** the server's `total_rows`: SCORECARD ROWS SCANNED. */
  scannedRows: number
  truncated: boolean
}

export function trajectoryTotals(
  resp: BandTrajectoryResponse | null | undefined,
): TrajectoryTotals {
  const empty: TrajectoryTotals = {
    desks: 0,
    dimensions: 0,
    points: 0,
    flagged: 0,
    withConfidence: 0,
    scannedRows: 0,
    truncated: false,
  }
  if (!resp) return empty
  let dimensions = 0
  let points = 0
  let flagged = 0
  let withConfidence = 0
  for (const desk of resp.desks ?? []) {
    for (const series of Object.values(desk.dimensions ?? {})) {
      dimensions += 1
      for (const p of series) {
        points += 1
        if (p.faithfulness_flagged) flagged += 1
        if (p.effective_confidence != null) withConfidence += 1
      }
    }
  }
  return {
    desks: (resp.desks ?? []).length,
    dimensions,
    points,
    flagged,
    withConfidence,
    scannedRows: resp.total_rows ?? 0,
    truncated: Boolean(resp.truncated),
  }
}

/**
 * The summary line. `points` and `scannedRows` are named separately and
 * `scannedRows` is spelled out as rows — labelling the server's `total_rows`
 * as "points" would misstate the density of every series on screen.
 */
export function trajectorySummaryLine(
  resp: BandTrajectoryResponse | null | undefined,
): string {
  const t = trajectoryTotals(resp)
  return (
    `${t.desks} desk${t.desks === 1 ? '' : 's'} · ` +
    `${t.dimensions} dimension series · ` +
    `${t.points} banded point${t.points === 1 ? '' : 's'} · ` +
    `${t.flagged} faithfulness-flagged · ` +
    `effective confidence recorded on ${t.withConfidence}/${t.points} · ` +
    `${t.scannedRows} scorecard rows scanned (rows, not points)`
  )
}

/** The truncation warning, or null when the scan completed. */
export function truncationWarning(
  resp: BandTrajectoryResponse | null | undefined,
): string | null {
  if (!resp?.truncated) return null
  return (
    `TRUNCATED — the server hit its row cap at ${resp.total_rows} scanned scorecard rows, ` +
    'so THE LAST DESK GROUP BELOW MAY BE INCOMPLETE. No series here can be read as a full one; ' +
    'narrow the window or the target before drawing a trend from it.'
  )
}

/** Dimension series in a stable (alphabetical) order for display. */
export function orderedTrajectoryDimensions(
  desk: DeskTrajectory,
): Array<[string, TrajectoryPoint[]]> {
  return Object.keys(desk.dimensions ?? {})
    .sort()
    .map((k) => [k, desk.dimensions[k]] as [string, TrajectoryPoint[]])
}

/** A confidence, or the absence — `null` is never 0. */
export function confidenceLabel(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NOT_RECORDED
  return value.toFixed(2)
}

/** Everything a single banded cell stands for, for its hover/title text. */
export function pointTitle(p: TrajectoryPoint): string {
  const parts = [
    p.ts,
    `band ${p.band}`,
    `effective confidence ${confidenceLabel(p.effective_confidence)}`,
  ]
  if (p.faithfulness_flagged) parts.push('FAITHFULNESS FLAGGED')
  parts.push(`scorecard row ${p.scorecard_row_id}`)
  return parts.join(' · ')
}

/** One series' own n, so a three-point strip cannot pass for a trend. */
export function seriesLabel(points: TrajectoryPoint[]): string {
  const n = points.length
  const flagged = points.filter((p) => p.faithfulness_flagged).length
  const withConf = points.filter((p) => p.effective_confidence != null).length
  return (
    `${n} point${n === 1 ? '' : 's'} · ${flagged} flagged · ` +
    `confidence on ${withConf}/${n}`
  )
}

// ===========================================================================
// Board 3 — analyst runtime (`GET /v3/eval/analyst_runtime`)
// ===========================================================================

export const RUNTIME_WINDOW_CHOICES: readonly number[] = [6, 24, 72, 168]

/**
 * `window_hours` is echoed on EVERY row; it is one window, not a per-row fact,
 * so it is stated once. A disagreement between rows is surfaced rather than
 * silently picking the first — that would be a fabricated window.
 */
export function runtimeWindowLabel(
  rows: AnalystRuntimeRow[] | null | undefined,
  requestedHours: number,
): string {
  const echoed = Array.from(
    new Set((rows ?? []).map((r) => r.window_hours).filter((h) => Number.isFinite(h))),
  ).sort((a, b) => a - b)
  if (echoed.length === 0) {
    return `${requestedHours}h window (requested — no rows came back to echo it)`
  }
  if (echoed.length === 1) return `${echoed[0]}h window`
  return `rows disagree on the window: ${echoed.join('h, ')}h`
}

/** The fleet line — the failure count always against its denominator. */
export function runtimeTotalsLine(rows: AnalystRuntimeRow[] | null | undefined): string {
  const list = rows ?? []
  if (list.length === 0) return 'no analyst runs recorded in this window (n=0)'
  const runs = list.reduce((a, r) => a + r.runs, 0)
  const nonSuccess = list.reduce((a, r) => a + r.non_success, 0)
  return (
    `${list.length} analyst${list.length === 1 ? '' : 's'} · ` +
    `${runs} run${runs === 1 ? '' : 's'} · ` +
    `non-success ${rateWithN(nonSuccess, runs, { emptyLabel: 'no runs to rate (n=0)' })}`
  )
}

/** A mean that always carries the n it was taken over; null is an absence. */
export function avgSecondsLabel(row: Pick<AnalystRuntimeRow, 'avg_seconds' | 'runs'>): string {
  if (row.avg_seconds == null) return NOT_RECORDED
  return `${formatSeconds(row.avg_seconds)} over ${row.runs} run${row.runs === 1 ? '' : 's'}`
}

export function maxSecondsLabel(row: Pick<AnalystRuntimeRow, 'max_seconds'>): string {
  return formatSeconds(row.max_seconds)
}

/** The failure count, never without its denominator. */
export function nonSuccessLabel(
  row: Pick<AnalystRuntimeRow, 'runs' | 'non_success'>,
): string {
  return rateWithN(row.non_success, row.runs, {
    emptyLabel: 'no runs in window (n=0)',
  })
}

/**
 * The runtime board's read failure.
 *
 * Named separately from {@link boardErrorText} because this board is the one
 * without a degradation wrapper: a 500 here is a real DB failure, and the
 * message says so rather than letting an operator read the silence as "no
 * analyst ran".
 */
export function runtimeErrorText(err: unknown): string {
  const base = boardErrorText(err, 'Analyst runtime')
  return `${base} This board has no server-side degradation wrapper — a read failure surfaces as a real error, and no row count can be inferred from it.`
}
