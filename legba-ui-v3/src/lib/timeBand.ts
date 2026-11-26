/**
 * The time band's pure core: the domain it draws, the brush moves, the window
 * in words, the activity histogram and the forecast ticks on the future side.
 *
 * The band is the one control that moves the shared window
 * (`v4/world/worldState.ts::setWindow`). Everything here is arithmetic over
 * epoch milliseconds, so the component stays a thin shell and the behaviour is
 * tested without a browser.
 */
import { SPAN_PRESETS, type SpanPreset, type TimeWindow } from '@/lib/mapTime'
import { densityBuckets, type TemporalAxis, type TemporalRow } from '@/lib/temporalLayers'

const HOUR = 3_600_000
const DAY = 24 * HOUR

/** The spans the band offers as buttons. Older links may still name 6h; the
 *  address parser keeps it, the band does not offer it. */
export const BAND_PRESETS: readonly SpanPreset[] = SPAN_PRESETS.filter((p) =>
  ['24h', '7d', '30d'].includes(p.id),
)

/** How far past "now" the future side reaches (open forecasts due inside it). */
export const FUTURE_HORIZON_MS = 30 * DAY

/** Share of the band's width the future side takes. */
export const FUTURE_SHARE = 0.18

/** Bars across the past side. */
export const HISTOGRAM_BUCKETS = 72

/** The narrowest window the brush can be resized to. */
export const MIN_WINDOW_MS = HOUR

/**
 * The past side's extent: at least a week, four times the chosen span, and
 * always wide enough to hold the window's start. `hi` is now.
 */
export function pastDomain(spanMs: number, windowStartMs: number, now: number): { lo: number; hi: number } {
  const reach = Math.max(7 * DAY, 4 * spanMs)
  return { lo: Math.min(windowStartMs, now - reach), hi: now }
}

/** The keyboard step: about 1/48 of the past side, rounded to whole hours. */
export function keyStepMs(lo: number, hi: number): number {
  return Math.max(HOUR, Math.round((hi - lo) / 48 / HOUR) * HOUR)
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(Math.max(v, lo), hi)
}

/** Slide the window by `deltaMs`, keeping its width, inside [lo, hi]. */
export function moveWindow(w: TimeWindow, deltaMs: number, lo: number, hi: number): TimeWindow {
  const width = Math.min(w.endMs - w.startMs, hi - lo)
  const start = clamp(w.startMs + deltaMs, lo, hi - width)
  return { startMs: start, endMs: start + width }
}

/**
 * Move one edge of the window by `deltaMs` (the end by default), inside
 * [lo, hi], never narrower than {@link MIN_WINDOW_MS}.
 */
export function resizeWindow(
  w: TimeWindow,
  deltaMs: number,
  lo: number,
  hi: number,
  edge: 'start' | 'end' = 'end',
): TimeWindow {
  if (edge === 'start') {
    const start = clamp(w.startMs + deltaMs, lo, w.endMs - MIN_WINDOW_MS)
    return { startMs: start, endMs: w.endMs }
  }
  const end = clamp(w.endMs + deltaMs, w.startMs + MIN_WINDOW_MS, hi)
  return { startMs: w.startMs, endMs: end }
}

/** Centre the window on `atMs`, keeping its width, inside [lo, hi]. */
export function centreWindow(w: TimeWindow, atMs: number, lo: number, hi: number): TimeWindow {
  const width = w.endMs - w.startMs
  return moveWindow(w, atMs - (w.startMs + width / 2), lo, hi)
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** `25 Nov 09:00` in UTC — the band's one date form. */
export function dayTime(ms: number): string {
  const d = new Date(ms)
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${hh}:${mm}`
}

/** `25 Nov` in UTC. */
export function dayOnly(ms: number): string {
  const d = new Date(ms)
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`
}

/** A duration in the largest whole unit that reads naturally: `6 hours`, `7 days`. */
export function durationWords(ms: number): string {
  const hours = Math.round(ms / HOUR)
  if (hours < 48) return `${hours} hour${hours === 1 ? '' : 's'}`
  const days = Math.round(ms / DAY)
  return `${days} days`
}

/** True when the window ends within a minute of now. */
export function endsNow(w: TimeWindow, now: number): boolean {
  return now - w.endMs <= 60_000
}

/**
 * The window in words, for the band's label and its accessible value:
 * `last 7 days, to now` or `18 Nov 09:00 to 21 Nov 09:00 UTC (3 days)`.
 */
export function windowWords(w: TimeWindow, now: number): string {
  const span = durationWords(w.endMs - w.startMs)
  if (endsNow(w, now)) return `last ${span}, to now`
  return `${dayTime(w.startMs)} to ${dayTime(w.endMs)} UTC (${span})`
}

/** The preset whose width the window has, when it also ends now. */
export function activePreset(w: TimeWindow, now: number): SpanPreset | null {
  if (!endsNow(w, now)) return null
  const width = w.endMs - w.startMs
  return BAND_PRESETS.find((p) => Math.abs(p.ms - width) <= 60_000) ?? null
}

/** Counts of rows per bucket on one clock across [lo, hi]. */
export function activityHistogram(
  rows: readonly TemporalRow[],
  lo: number,
  hi: number,
  axis: TemporalAxis = 'valid',
  buckets = HISTOGRAM_BUCKETS,
): number[] {
  return densityBuckets(rows, axis, lo, hi, buckets)
}

/** A forecast as the band needs it (a subset of the ledger's row). */
export interface BandForecast {
  slug: string
  question: string
  due_at: string
  status: string
  subject_keys?: string[]
}

export interface ForecastTick {
  slug: string
  question: string
  dueMs: number
  /** Position across the future side, 0 at now, 1 at the horizon. */
  frac: number
}

/**
 * The open forecasts due between now and the horizon, soonest first, placed
 * on the future side. A subject narrows them to its own questions.
 */
export function forecastTicks(
  forecasts: readonly BandForecast[],
  now: number,
  opts: { horizonMs?: number; subject?: string | null } = {},
): { ticks: ForecastTick[]; later: number } {
  const horizon = opts.horizonMs ?? FUTURE_HORIZON_MS
  const ticks: ForecastTick[] = []
  let later = 0
  for (const f of forecasts) {
    if (f.status !== 'open') continue
    if (opts.subject && !(f.subject_keys ?? []).includes(opts.subject)) continue
    const due = Date.parse(f.due_at)
    if (Number.isNaN(due) || due < now) continue
    if (due > now + horizon) {
      later++
      continue
    }
    ticks.push({ slug: f.slug, question: f.question, dueMs: due, frac: (due - now) / horizon })
  }
  ticks.sort((a, b) => a.dueMs - b.dueMs || a.slug.localeCompare(b.slug))
  return { ticks, later }
}

/** One play step: the window slides a day later, and stops at now. */
export function playStep(w: TimeWindow, now: number): { window: TimeWindow; done: boolean } {
  const width = w.endMs - w.startMs
  const end = w.endMs + DAY
  if (end >= now) return { window: { startMs: now - width, endMs: now }, done: true }
  return { window: { startMs: w.startMs + DAY, endMs: end }, done: false }
}
