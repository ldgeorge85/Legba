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

export interface WorldSituation {
  id: string
  title: string
  lifecycle: SituationLifecycle
  countries: string[]
  ts: number
}

/** CSS hex per severity (matches tailwind `severity.*`). */
export const SEVERITY_COLOR: Record<Severity, string> = {
  critical: '#ff5555',
  high: '#ff9955',
  medium: '#ffdd55',
  low: '#55ff55',
  info: '#5599ff',
}

/** deck.gl RGBA per severity. */
export const SEVERITY_RGBA: Record<Severity, [number, number, number, number]> = {
  critical: [255, 85, 85, 220],
  high: [255, 153, 85, 200],
  medium: [255, 221, 85, 180],
  low: [85, 255, 85, 160],
  info: [85, 153, 255, 160],
}

export const SITUATION_COLOR: Record<SituationLifecycle, string> = {
  active: '#ffdd55',
  escalating: '#ff5555',
  resolved: '#55ff55',
}
