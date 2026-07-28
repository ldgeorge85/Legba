/**
 * Map time-window logic (P4-3, feature 3) — the scrubber's pure core.
 *
 * The World map filters signals / findings / situations to a [t0, t1] window.
 * This module owns the window math (span presets, thumb clamping, membership,
 * filtering) so `TimeScrubber` is a thin DOM shell and the behaviour is
 * unit-tested without a browser. The prior scrubber only dragged the window
 * END across a fixed 24h span; this drives a genuine two-ended window with a
 * selectable outer span.
 */

export interface TimeWindow {
  startMs: number
  endMs: number
}

/** A selectable total span for the scrubber's outer bounds. */
export interface SpanPreset {
  id: string
  label: string
  ms: number
}

const HOUR = 3_600_000
const DAY = 24 * HOUR

/** Outer-span choices, shortest → longest. */
export const SPAN_PRESETS: readonly SpanPreset[] = [
  { id: '6h', label: '6h', ms: 6 * HOUR },
  { id: '24h', label: '24h', ms: DAY },
  { id: '7d', label: '7d', ms: 7 * DAY },
  { id: '30d', label: '30d', ms: 30 * DAY },
]

export const DEFAULT_SPAN_MS = DAY

/** The preset whose span equals `ms`, or null (a custom span). */
export function spanPresetFor(ms: number): SpanPreset | null {
  return SPAN_PRESETS.find((p) => p.ms === ms) ?? null
}

/** The window covering the last `spanMs`, ending at `now`. */
export function windowForSpan(now: number, spanMs: number): TimeWindow {
  return { startMs: now - spanMs, endMs: now }
}

/**
 * Clamp a proposed [start, end] into the outer [lo, hi] bounds while keeping
 * start <= end (swaps the thumbs if a caller drags one past the other).
 */
export function clampWindow(
  startMs: number,
  endMs: number,
  lo: number,
  hi: number,
): TimeWindow {
  let s = Math.min(Math.max(startMs, lo), hi)
  let e = Math.min(Math.max(endMs, lo), hi)
  if (s > e) [s, e] = [e, s]
  return { startMs: s, endMs: e }
}

/** Is `ts` inside [startMs, endMs] (inclusive)? */
export function withinWindow(ts: number, startMs: number, endMs: number): boolean {
  return ts >= startMs && ts <= endMs
}

/** Filter time-stamped items to the window (inclusive on both ends). */
export function filterByWindow<T extends { ts: number }>(
  items: readonly T[],
  startMs: number,
  endMs: number,
): T[] {
  return items.filter((i) => withinWindow(i.ts, startMs, endMs))
}

/** A slider step (ms) sized to the span so a drag is smooth, never jittery. */
export function sliderStep(spanMs: number): number {
  // ~500 steps across the span, floored at one minute.
  return Math.max(60_000, Math.round(spanMs / 500))
}

/** Fraction [0,1] of `ts` within [lo, hi] — for positioning a thumb / label. */
export function windowFraction(ts: number, lo: number, hi: number): number {
  if (hi <= lo) return 0
  return Math.min(1, Math.max(0, (ts - lo) / (hi - lo)))
}

/** True when the window end is within ~`toleranceMs` of `now` (the LIVE edge). */
export function isLiveWindow(endMs: number, now: number, toleranceMs = 60_000): boolean {
  return now - endMs <= toleranceMs
}
