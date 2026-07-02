/**
 * Timeline data layer for the Target Timeline (UI-3 / Tier B).
 *
 * Pure, testable transforms that turn substrate rows into the banded
 * scatter + situation-lifecycle spans the Timeline panel renders. Kept
 * out of the component so recharts/DOM aren't needed to test the logic.
 *
 * Bands (Y axis):
 *   1 = signal           (source emissions)
 *   2 = finding          (analyst-output emission marks)
 *   3 = situation        (situation lifecycle — open → last_event)
 */

export interface TLSignal {
  id: string
  title: string
  category: string
  produced_at: string
  /** When the EVENT happened (source payload), vs produced_at = when we
   *  fetched it. Preferred for plotting so the timeline reads true. */
  published_at?: string | null
}

export interface TLFinding {
  id: string
  title: string
  analyst_id: string | null
  severity: string | null
  produced_at: string
  /** Event time, preferred over produced_at when present. */
  published_at?: string | null
}

/** Substrate situation row (frozen shape — `status`, `last_event_at`, etc.). */
export interface TLSituation {
  id: string
  name: string
  status: string
  category: string
  produced_at: string
  last_event_at: string | null
  event_count: number
  intensity_score: number
}

export type TimelineKind = 'signal' | 'finding' | 'situation'

export const BAND: Record<TimelineKind, number> = {
  signal: 1,
  finding: 2,
  situation: 3,
}

export const BAND_LABELS: Record<number, TimelineKind> = {
  1: 'signal',
  2: 'finding',
  3: 'situation',
}

export const KIND_COLOR: Record<TimelineKind, string> = {
  signal: '#60a5fa', // blue-400
  finding: '#fbbf24', // amber-400
  situation: '#fb7185', // rose-400
}

export interface TimelinePoint {
  id: string
  title: string
  ts: number // epoch ms — X axis
  band: number
  kind: TimelineKind
  subtitle: string
  /** Present on finding points — drives the per-severity <Cell> color. */
  severity?: string | null
}

/** Severity → finding-mark color (matches the Map overlay palette). */
export const SEVERITY_COLOR: Record<string, string> = {
  critical: '#ef4444',
  high: '#f59e0b',
  medium: '#3b82f6',
  low: '#10b981',
}

/** Color for a finding mark — its severity color, else the finding default. */
export function findingMarkColor(severity: string | null | undefined): string {
  if (severity && SEVERITY_COLOR[severity]) return SEVERITY_COLOR[severity]
  return KIND_COLOR.finding
}

/** A situation lifecycle span (open → last event) rendered as a bar. */
export interface SituationSpan {
  id: string
  name: string
  status: string
  band: number
  start: number // epoch ms — opened (produced_at)
  end: number // epoch ms — last_event_at (or start when unknown)
  intensity: number
}

function ms(iso: string | null | undefined): number {
  if (!iso) return NaN
  return new Date(iso).getTime()
}

/** Prefer event time (published_at) over fetch time (produced_at). */
function eventMs(published: string | null | undefined, produced: string): number {
  const p = ms(published)
  return Number.isFinite(p) ? p : ms(produced)
}

export function signalPoints(rows: TLSignal[]): TimelinePoint[] {
  return rows
    .map((s) => ({
      id: s.id,
      title: s.title,
      ts: eventMs(s.published_at, s.produced_at),
      band: BAND.signal,
      kind: 'signal' as const,
      subtitle: s.category,
    }))
    .filter((p) => Number.isFinite(p.ts))
}

export function findingPoints(rows: TLFinding[]): TimelinePoint[] {
  return rows
    .map((f) => ({
      id: f.id,
      title: f.title,
      ts: eventMs(f.published_at, f.produced_at),
      band: BAND.finding,
      kind: 'finding' as const,
      subtitle: [f.analyst_id, f.severity].filter(Boolean).join(' · '),
      severity: f.severity,
    }))
    .filter((p) => Number.isFinite(p.ts))
}

/** Opacity for a situation lifecycle span — the visual "breathe". Fades with
 *  lifecycle status (active → dormant → closed) and the recency-weighted
 *  intensity, so a quieting situation visibly dims and a re-activated one
 *  brightens. Returns [0.1, 1]. */
export function spanOpacity(span: { status: string; intensity: number }): number {
  const base =
    span.status === 'closed' ? 0.25 : span.status === 'dormant' ? 0.5 : 1.0
  const intensityFactor = Math.max(0.4, Math.min(1, (span.intensity || 0) / 4))
  return Math.round(Math.max(0.1, base * intensityFactor) * 100) / 100
}

/** Recency fade for a point — older marks dim toward `minOpacity` over a
 *  half-life, so the timeline reads as events arriving and fading rather than
 *  an ever-accumulating wall of equal-weight dots. Returns [minOpacity, 1]. */
export function pointOpacity(
  ts: number,
  now: number,
  halfLifeMs = 3 * 24 * 60 * 60 * 1000,
  minOpacity = 0.2,
): number {
  if (!Number.isFinite(ts) || ts >= now) return 1
  const decay = Math.pow(0.5, (now - ts) / halfLifeMs)
  return Math.round((minOpacity + (1 - minOpacity) * decay) * 100) / 100
}

/** Situation emission marks — one point at the situation's open time. */
export function situationPoints(rows: TLSituation[]): TimelinePoint[] {
  return rows
    .map((s) => ({
      id: s.id,
      title: s.name,
      ts: ms(s.produced_at),
      band: BAND.situation,
      kind: 'situation' as const,
      subtitle: `${s.status} · ${s.event_count} events`,
    }))
    .filter((p) => Number.isFinite(p.ts))
}

/** Situation lifecycle spans — opened (produced_at) → last_event_at. */
export function situationSpans(rows: TLSituation[]): SituationSpan[] {
  const out: SituationSpan[] = []
  for (const s of rows) {
    const start = ms(s.produced_at)
    if (!Number.isFinite(start)) continue
    const last = ms(s.last_event_at)
    const end = Number.isFinite(last) && last >= start ? last : start
    out.push({
      id: s.id,
      name: s.name,
      status: s.status,
      band: BAND.situation,
      start,
      end,
      intensity: s.intensity_score,
    })
  }
  return out
}

/**
 * Read-scoped evidence partition (P1-T7) — split a timeline's points into the
 * read's DIRECTLY-CITED evidence vs. the surrounding context, given the set of
 * cited substrate ids (the read finding's `data.citations[].signal_id`, and
 * optionally the read finding's own id). The temporal lens emphasises the
 * evidence and fades the context. Pure; never mutates the input.
 */
export function partitionByEvidence(
  points: TimelinePoint[],
  evidenceIds: Set<string>,
): { evidence: TimelinePoint[]; context: TimelinePoint[] } {
  const evidence: TimelinePoint[] = []
  const context: TimelinePoint[] = []
  for (const p of points) {
    if (evidenceIds.has(p.id)) evidence.push(p)
    else context.push(p)
  }
  return { evidence, context }
}

/** Padded [min,max] X domain across all points + spans, or undefined. */
export function timeDomain(
  points: TimelinePoint[],
  spans: SituationSpan[] = [],
): [number, number] | undefined {
  const ts: number[] = []
  for (const p of points) ts.push(p.ts)
  for (const s of spans) {
    ts.push(s.start, s.end)
  }
  if (ts.length === 0) return undefined
  let min = ts[0]
  let max = ts[0]
  for (const t of ts) {
    if (t < min) min = t
    if (t > max) max = t
  }
  const pad = max === min ? 5 * 60_000 : (max - min) * 0.02
  return [min - pad, max + pad]
}
