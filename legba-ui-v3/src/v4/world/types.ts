/**
 * The World — shared contract (orchestrator-owned). Wave-1 Track A agents code
 * against these shapes; they never import each other's files.
 */
export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info'

export interface WorldSignal {
  id: string
  lat: number
  lon: number
  /** ISO2 codes from signals.geo. */
  countries: string[]
  severity: Severity
  sourceId: string | null
  /** epoch ms (fetched_at). */
  ts: number
  title: string
  language?: string | null
}

export interface WorldFinding {
  id: string
  lat: number | null
  lon: number | null
  countries: string[]
  severity: Severity
  targetId: string | null
  analystId: string | null
  ts: number
  title: string
  confidence?: number | null
}

export type SituationLifecycle = 'active' | 'escalating' | 'resolved'

/**
 * V3/P6 — the event five-state lifecycle (migration 0202 CHECK). An unknown
 * value coerces to `active` (an event the ledger stamped is live unless it
 * says resolved).
 */
export type EventLifecycle =
  | 'emerging'
  | 'developing'
  | 'active'
  | 'evolving'
  | 'resolved'

/**
 * The event lifecycle → badge color. THE SINGLE definition (same rule as
 * `SEVERITY_COLOR`): `lib/timelinePoints`, `lib/timelineWindows` and both
 * World-map renderers read it so an event reads identically on every
 * timeline and the map. `resolved` recedes to the quiet grey — the same
 * "low recedes" principle as the severity ramp.
 */
export const EVENT_LIFECYCLE_COLOR: Record<EventLifecycle, string> = {
  emerging: '#60a5fa', // blue-400
  developing: '#d29922', // amber
  active: '#fb7185', // rose-400
  evolving: '#a78bfa', // violet-400
  resolved: '#6e7681', // quiet grey
}

/** deck.gl/MapLibre need a lookup keyed on a plain string — widen the type
 *  at the edge (a row carrying an unmapped lifecycle falls back to the
 *  `active` color in mapData). */
export function eventLifecycleColor(v: string): string {
  return (EVENT_LIFECYCLE_COLOR as Record<string, string>)[v] ?? EVENT_LIFECYCLE_COLOR.active
}

/**
 * V3/P6 — a bounded real-world occurrence on the World map. `lat`/`lon`
 * come from `events.geo_lat`/`geo_lon` ONLY — an event with no stamped
 * coordinate does NOT place (the spec's explicit point: no country-centroid
 * fallback, which would claim a precision the event never asserted).
 */
export interface WorldEvent {
  id: string
  lat: number
  lon: number
  /** ISO2 codes from events.geo[]. */
  countries: string[]
  severity: Severity
  lifecycle: EventLifecycle
  targetId: string | null
  /** epoch ms — occurrence anchor (time_start, produced_at fallback). */
  ts: number
  title: string
}

export interface WorldSituation {
  id: string
  title: string
  lifecycle: SituationLifecycle
  countries: string[]
  ts: number
}

/**
 * CSS hex per severity — CHANNEL A, the product's ONE warm ramp
 * (UI_HOLISTIC_DESIGN_2026-08-24 §5.2/§5.3). Mirrors tailwind `severity.*` /
 * the `--sev-*` tokens, kept as literals here because the map layers
 * (deck.gl, MapLibre paint expressions) cannot read a CSS variable.
 *
 * THE RE-KEY: this was the neon ramp (#ff5555 / #ff9955 / #ffdd55 / #55ff55,
 * UI_V4_PLAN D8, tuned for the dark basemap). `low` at pure neon green made
 * the calmest datum on the screen the brightest colour on it — on the map, in
 * the feed, in every severity badge. Low severity now RECEDES to a quiet grey,
 * and the rest are Primer's graduated rungs. Safe because colour is already
 * redundant on these surfaces: SeverityBadge and ProvenanceBadge carry the
 * meaning in icon shape + text label.
 *
 * This is the SINGLE definition — `lib/timelinePoints`, `lib/timelineWindows`
 * and the target Map overlay all read it rather than keeping their own copies
 * (three of which had drifted onto the `accent.*` ramp, where "medium" was
 * blue and "low" was green).
 */
export const SEVERITY_COLOR: Record<Severity, string> = {
  critical: '#f85149',
  high: '#db6d28',
  medium: '#d29922',
  low: '#6e7681',
  info: '#4493f8',
}

/** deck.gl RGBA per severity — the same ramp, with the existing alpha ladder. */
export const SEVERITY_RGBA: Record<Severity, [number, number, number, number]> = {
  critical: [248, 81, 73, 220],
  high: [219, 109, 40, 200],
  medium: [210, 153, 34, 180],
  low: [110, 118, 129, 160],
  info: [68, 147, 248, 160],
}

/**
 * Situation lifecycle → hex, on the SAME warm ramp as severity
 * (UI_HOLISTIC_DESIGN_2026-08-24 §5.2 — "severity is the only warm ramp;
 * nothing else in the app is allowed to be red, amber, or green"). Escalating
 * reads critical, active reads medium, and RESOLVED recedes to the same quiet
 * grey low severity uses — a resolved situation is the thing on the map that
 * needs no attention, and it was the brightest marker on it (#55ff55).
 */
export const SITUATION_COLOR: Record<SituationLifecycle, string> = {
  active: SEVERITY_COLOR.medium,
  escalating: SEVERITY_COLOR.critical,
  resolved: SEVERITY_COLOR.low,
}
