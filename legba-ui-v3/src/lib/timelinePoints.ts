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
 *   4 = event            (V3/P6 bounded occurrences — time_start → time_end,
 *                         lifecycle state as the badge)
 */

import { SEVERITY_COLOR as SEVERITY_RAMP, EVENT_LIFECYCLE_COLOR as EVENT_LC } from '@/v4/world/types'

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

/** Substrate event row (V3/P6 — the `/v3/events` wire shape). */
export interface TLEvent {
  id: string
  title: string
  /** The five-state lifecycle vocabulary (emerging/developing/active/
   *  evolving/resolved) — rendered as the span/point badge. */
  lifecycle_state: string
  severity?: string | null
  category?: string | null
  /** Occurrence bounds — either may be null (an ongoing event carries no
   *  time_end; produced_at is the placement fallback when both are null). */
  time_start?: string | null
  time_end?: string | null
  produced_at?: string | null
}

export type TimelineKind = 'signal' | 'finding' | 'situation' | 'event'

export const BAND: Record<TimelineKind, number> = {
  signal: 1,
  finding: 2,
  situation: 3,
  event: 4,
}

export const BAND_LABELS: Record<number, TimelineKind> = {
  1: 'signal',
  2: 'finding',
  3: 'situation',
  4: 'event',
}

export const KIND_COLOR: Record<TimelineKind, string> = {
  signal: '#60a5fa', // blue-400
  finding: '#fbbf24', // amber-400
  situation: '#fb7185', // rose-400
  event: '#a78bfa', // violet-400
}

/** V3/P6 — the event five-state lifecycle → badge color. Re-exported from
 *  the ONE definition in `v4/world/types.ts` (same rule as the severity
 *  ramp) so the timeline, the validity panel and the World map can never
 *  drift onto different colors for the same state. */
export const EVENT_LIFECYCLE_COLOR: Record<string, string> = EVENT_LC

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

/**
 * Severity → finding-mark color. Re-exported from the ONE severity ramp
 * (v4/world/types.ts) rather than kept as a fourth private copy — this map had
 * drifted onto the `accent.*` ramp, where "medium" was blue and "low" was
 * green, so the same severity read differently on the timeline than on the map
 * (UI_HOLISTIC_DESIGN_2026-08-24 §5.1).
 */
export const SEVERITY_COLOR: Record<string, string> = SEVERITY_RAMP

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

/** An event occurrence span (V3/P6) — time_start → time_end, the lifecycle
 *  state carried for the badge color; `open` marks a still-unfolding event
 *  (no time_end — resolved to `now` for layout only, never a fabricated
 *  close, same honesty rule as `timelineWindows`). */
export interface EventSpan {
  id: string
  title: string
  lifecycle: string
  severity?: string | null
  band: number
  start: number // epoch ms — time_start (produced_at fallback)
  end: number   // epoch ms — time_end (or `nowMs` when open)
  open: boolean
}

/** Event emission marks — one point at the event's start anchor. */
export function eventPoints(rows: TLEvent[]): TimelinePoint[] {
  return rows
    .map((e) => {
      const anchor = ms(e.time_start ?? undefined)
      const fallback = ms(e.produced_at ?? undefined)
      return {
        id: e.id,
        title: e.title,
        ts: Number.isFinite(anchor) ? anchor : fallback,
        band: BAND.event,
        kind: 'event' as const,
        subtitle: e.lifecycle_state,
        severity: e.severity,
      }
    })
    .filter((p) => Number.isFinite(p.ts))
}

/** Event occurrence spans — time_start → time_end (V3/P6). A row with no
 *  placeable start is dropped, never guessed; a NULL time_end resolves to
 *  `nowMs` for layout and flags `open` (the panel draws it as the ongoing
 *  bar). `lifecycle` is the badge — the panel colors by
 *  `EVENT_LIFECYCLE_COLOR`. */
export function eventSpans(rows: TLEvent[], nowMs: number): EventSpan[] {
  const out: EventSpan[] = []
  for (const e of rows) {
    const start = Number.isFinite(ms(e.time_start ?? undefined))
      ? ms(e.time_start ?? undefined)
      : ms(e.produced_at ?? undefined)
    if (!Number.isFinite(start)) continue
    const rawEnd = ms(e.time_end ?? undefined)
    const open = e.time_end == null || !Number.isFinite(rawEnd)
    let end = open ? nowMs : rawEnd
    if (end < start) end = start // clock-skew clamp — never draw backwards
    out.push({
      id: e.id,
      title: e.title,
      lifecycle: e.lifecycle_state,
      severity: e.severity,
      band: BAND.event,
      start,
      end,
      open,
    })
  }
  return out
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
  spans: readonly { start: number; end: number }[] = [],
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
