/**
 * Layer-divergence model (Program 6 L2, 7b-v) — the arithmetic behind the
 * divergence map, kept pure so the honesty rules can be pinned without a DOM.
 *
 * The unit this reads measures the CHANGE in a country's own layer-to-layer
 * gap against its own rolling baseline. Everything it computes already carries
 * a method version and an audit stamp, so the rule here is narrow and strict:
 *
 *   1. **This file computes no statistic.** It classifies rows the server
 *      already carried and turns numbers into SVG coordinates. There is no
 *      second z, no re-derived direction, no trend across desks. A number this
 *      module invented would be an instrument with no method version behind it.
 *   2. **A null is a null.** `z: null` means the trailing baseline was not
 *      usable yet — it is not zero, and `formatZ` renders it as
 *      {@link UNMEASURED} rather than `0.00`. The sparkline BREAKS its z track
 *      across such days instead of bridging them with a straight line, because
 *      a bridge is a drawn claim about days that were never scored.
 *   3. **An excluded layer keeps its reason.** `aperture_excluded` is never
 *      shown on its own: the layer AND the operator's sentence travel with it,
 *      because the whole point of the aperture declaration is that a missing
 *      layer must not read as agreement.
 *
 * WHICH COUNT THE SPARKLINE DRAWS. The receipt carries three numbers per
 * (layer, day): `raw` (rows seen), `folded` (wire copies absorbed) and `kept`
 * (`raw − folded`, the survivors). The ratio, the baseline and the z are all
 * computed from `kept`, so `kept` is what the line draws — drawing `folded`
 * would plot the copies that were REMOVED against a z derived from the ones
 * that stayed. The pair's own `series[].a` / `.b` ARE those kept counts, which
 * is why the chart reads the series rather than joining the layer maps: the
 * line and the z it sits under can then never come from different arithmetic.
 * Every tooltip still carries all three, so nothing is hidden by the choice.
 */

import type {
  LayerDivergenceDesk,
  LayerDivergenceFired,
  LayerDivergencePair,
  LayerDivergenceResponse,
  LayerDivergenceSeriesPoint,
} from '@/lib/api'

/** Rendered wherever a figure has no measurement behind it. */
export const UNMEASURED = '—'

/**
 * The five states a pair row can be in, in the order they are decided.
 *
 *   * `aperture_excluded` — a side of the pair is not `present` for this desk.
 *     The pair was never evaluated; the excluded layer and its reason are the
 *     content of the row.
 *   * `fired` — this desk's newest divergence names this pair.
 *   * `thin` — evaluated, and on the last measured day a side carried fewer
 *     than `thin_min_per_day` items. A ratio between two handfuls is
 *     arithmetic, not evidence, and the badge says so before the z is read.
 *   * `below_threshold` — evaluated and did not clear |z| ≥ threshold on two
 *     consecutive days. The ordinary quiet state.
 *   * `evaluable` — evaluated, no fire, and the reason is one of the other
 *     named ones (`baseline_thin`, `blank_day`, `no_z`, `sign_flipped`,
 *     `window_too_short`), which the row prints by name.
 */
export type PairState =
  | 'aperture_excluded'
  | 'fired'
  | 'thin'
  | 'below_threshold'
  | 'evaluable'

/** The last day of a pair's series, or null when it has none. */
export function lastPoint(
  pair: LayerDivergencePair,
): LayerDivergenceSeriesPoint | null {
  const series = pair.series
  if (!series || series.length === 0) return null
  return series[series.length - 1]
}

/**
 * Is the last measured day thin on either side?
 *
 * Returns `null` — not `false` — when the floor is unknown or the pair has no
 * series, because "we did not check" and "we checked and it is not thin" are
 * different findings.
 */
export function lastDayThin(
  pair: LayerDivergencePair,
  thinMinPerDay: number | null,
): boolean | null {
  const point = lastPoint(pair)
  if (point == null || thinMinPerDay == null) return null
  return point.a < thinMinPerDay || point.b < thinMinPerDay
}

/** Classify one pair row. See {@link PairState} for the precedence. */
export function pairState(
  pair: LayerDivergencePair,
  fired: LayerDivergenceFired | null,
  thinMinPerDay: number | null,
): PairState {
  if (!pair.evaluable) return 'aperture_excluded'
  if (fired != null && fired.pair_id === pair.pair_id) return 'fired'
  if (lastDayThin(pair, thinMinPerDay) === true) return 'thin'
  if (pair.no_fire_reason === 'below_threshold') return 'below_threshold'
  return 'evaluable'
}

/**
 * Which way the last day sat against its OWN baseline.
 *
 * This is the sign of the z and nothing more — deliberately not the handler's
 * `widening` / `narrowing`, which compares |log-ratio| against |baseline
 * centre| and is only computed for a pair that actually fired. Re-deriving that
 * word here for a pair that did not fire would be this file inventing a verdict
 * the instrument declined to issue.
 */
export function zSide(z: number | null | undefined): 'above' | 'below' | null {
  if (z == null || Number.isNaN(z)) return null
  if (z === 0) return null
  return z > 0 ? 'above' : 'below'
}

/** Which layer the last day was relatively LOUDER on, from the log-ratio. */
export function louderLayer(
  pair: LayerDivergencePair,
): { layer: string; quieter: string } | null {
  const point = lastPoint(pair)
  if (point == null || point.log_ratio == null || point.log_ratio === 0) {
    return null
  }
  return point.log_ratio > 0
    ? { layer: pair.layer_a, quieter: pair.layer_b }
    : { layer: pair.layer_b, quieter: pair.layer_a }
}

/** A z with its unit, or the honest-absence mark. */
export function formatZ(z: number | null | undefined): string {
  if (z == null || Number.isNaN(z)) return UNMEASURED
  return `z ${z > 0 ? '+' : ''}${z.toFixed(2)}σ`
}

/** A count with its unit. Counts are always real measurements, so 0 is 0. */
export function formatCount(n: number): string {
  return `${n} item${n === 1 ? '' : 's'}/day`
}

// ---------------------------------------------------------------------------
// The paired sparkline
// ---------------------------------------------------------------------------

export interface SparklineOptions {
  width?: number
  /** Height of the count band (the two layer lines). */
  countHeight?: number
  /** Height of the z track drawn beneath it. */
  zHeight?: number
  /** The |z| the instrument fires at, drawn as a pair of rules. */
  zThreshold?: number | null
}

export interface SparklineGeometry {
  width: number
  countHeight: number
  zHeight: number
  /** The largest kept count on either layer — the count axis top. Never 0. */
  countMax: number
  /** SVG `points` for layer A's kept counts, oldest → newest. */
  aPoints: string
  bPoints: string
  /**
   * One `points` string per CONTIGUOUS run of scored days. A day with `z:
   * null` breaks the track rather than being bridged — the gap is the honest
   * rendering of "no baseline yet".
   */
  zSegments: string[]
  /** The |z| the track was scaled to, or null when no day was scored. */
  zMax: number | null
  /** y of the +/- threshold rules inside the z track, or null. */
  zThresholdY: { hi: number; lo: number } | null
  /** y of z = 0 inside the z track. */
  zZeroY: number
  days: string[]
}

const DEFAULTS = {
  width: 200,
  countHeight: 30,
  zHeight: 18,
}

function xAt(i: number, n: number, width: number): number {
  if (n <= 1) return width / 2
  return (i / (n - 1)) * width
}

function round(v: number): number {
  return Math.round(v * 100) / 100
}

/**
 * Turn one pair's series into drawable geometry.
 *
 * The count band is scaled to the largest kept count across BOTH layers, so
 * the two lines are always read against the same axis — scaling each to its own
 * maximum would make a layer carrying 2 items look like one carrying 200.
 *
 * The z track is scaled to `max(|z|, threshold)` so the threshold rules are
 * always on screen: a window whose every z is tiny must still show how far it
 * is from firing, or a quiet pair and a nearly-firing one draw identically.
 */
export function pairedSparkline(
  series: LayerDivergenceSeriesPoint[] | undefined,
  opts: SparklineOptions = {},
): SparklineGeometry | null {
  if (!series || series.length === 0) return null
  const width = opts.width ?? DEFAULTS.width
  const countHeight = opts.countHeight ?? DEFAULTS.countHeight
  const zHeight = opts.zHeight ?? DEFAULTS.zHeight
  const n = series.length

  const countMax = Math.max(
    1,
    ...series.map((p) => Math.max(p.a, p.b)),
  )
  const yCount = (v: number) => round(countHeight - (v / countMax) * countHeight)

  const aPoints = series
    .map((p, i) => `${round(xAt(i, n, width))},${yCount(p.a)}`)
    .join(' ')
  const bPoints = series
    .map((p, i) => `${round(xAt(i, n, width))},${yCount(p.b)}`)
    .join(' ')

  const scored = series.filter((p) => p.z != null && !Number.isNaN(p.z))
  const threshold =
    opts.zThreshold != null && opts.zThreshold > 0 ? opts.zThreshold : null
  const zMax = scored.length
    ? Math.max(
        ...scored.map((p) => Math.abs(p.z as number)),
        threshold ?? 0,
        // A window of all-zero z would divide by zero; the floor keeps the
        // track drawable and flat, which is what an all-zero window IS.
        0.5,
      )
    : null
  const half = zHeight / 2
  const yZ = (z: number) => round(half - (z / (zMax as number)) * half)

  const zSegments: string[] = []
  let run: string[] = []
  series.forEach((p, i) => {
    if (p.z == null || Number.isNaN(p.z) || zMax == null) {
      if (run.length) zSegments.push(run.join(' '))
      run = []
      return
    }
    run.push(`${round(xAt(i, n, width))},${yZ(p.z)}`)
  })
  if (run.length) zSegments.push(run.join(' '))

  return {
    width,
    countHeight,
    zHeight,
    countMax,
    aPoints,
    bPoints,
    zSegments,
    zMax,
    zThresholdY:
      threshold != null && zMax != null
        ? { hi: yZ(threshold), lo: yZ(-threshold) }
        : null,
    zZeroY: round(half),
    days: series.map((p) => p.day),
  }
}

// ---------------------------------------------------------------------------
// Header / desk summaries
// ---------------------------------------------------------------------------

/**
 * The panel subtitle.
 *
 * `measured: false` is a FAILED READ and says so; an empty map with no receipt
 * is "no run yet", which is a different and equally honest statement.
 */
export function summaryLine(res: LayerDivergenceResponse): string {
  if (!res.measured) {
    return 'could not read the divergence receipt — this is a failed read, not a quiet instrument'
  }
  if (res.receipt_run_id == null) {
    return 'no run yet — the unit has never written a receipt'
  }
  const desks = res.desks.length
  const fired = res.desks.filter((d) => d.fired != null).length
  const window = res.window_days != null ? `${res.window_days}d window` : 'window not stated'
  const asOf = res.as_of ? `as of ${res.as_of} (UTC day)` : 'as-of not stated'
  return (
    `${desks} desk${desks === 1 ? '' : 's'} · ${fired} with a fired pair · ` +
    `${window} · ${asOf}`
  )
}

/** The desk card's one-line state, worst news first. */
export function deskLine(
  desk: LayerDivergenceDesk,
  thinMinPerDay: number | null,
): string {
  const states = desk.pairs.map((p) =>
    pairState(p, desk.fired, thinMinPerDay),
  )
  const fired = states.filter((s) => s === 'fired').length
  const excluded = states.filter((s) => s === 'aperture_excluded').length
  const present = (desk.aperture.present ?? []).length
  const bits = [
    `${present} of 6 layers present`,
    `${desk.sources_mapped} source${desk.sources_mapped === 1 ? '' : 's'} mapped`,
    `${desk.rows_scanned} row${desk.rows_scanned === 1 ? '' : 's'} scanned`,
  ]
  if (fired) bits.unshift(`${fired} pair${fired === 1 ? '' : 's'} fired`)
  if (excluded) {
    bits.push(`${excluded} pair${excluded === 1 ? '' : 's'} not evaluable`)
  }
  return bits.join(' · ')
}

/**
 * Desks worst-first: a fired desk leads, then one with pairs the aperture shut
 * out (an unmeasurable gap is news about the curation), then by country so the
 * order is stable across two reads of the same map.
 */
export function orderDesks(
  desks: LayerDivergenceDesk[],
  thinMinPerDay: number | null,
): LayerDivergenceDesk[] {
  const rank = (d: LayerDivergenceDesk): number => {
    if (d.fired != null) return 0
    const states = d.pairs.map((p) => pairState(p, d.fired, thinMinPerDay))
    if (states.includes('thin')) return 1
    if (states.includes('aperture_excluded')) return 2
    return 3
  }
  return [...desks].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      a.country.localeCompare(b.country) ||
      a.target_id.localeCompare(b.target_id),
  )
}

/** The excluded side(s) of a non-evaluable pair, with the reason text. */
export function exclusionNotes(
  pair: LayerDivergencePair,
): { layer: string; state: string; reason: string }[] {
  return pair.excluded_layers ?? []
}
