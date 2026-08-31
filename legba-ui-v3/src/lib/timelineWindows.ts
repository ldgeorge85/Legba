/**
 * timelineWindows — the `system.timeline` validity-window panel's non-DOM logic
 * (P4-4).
 *
 * The temporal substrate (facts with `[valid_from, valid_until)`, situations
 * with a lifecycle window, findings with `[produced_at, superseded_at)` +
 * supersession chains) had NO temporal view. This is the pure shaping layer for
 * it: turn the `GET /api/v1/v3/timeline` ranged-item rows into laned, bounded
 * spans the panel draws as an SVG timeline — brushable against a time axis,
 * zoomable ms→months. Kept out of the component so the shaping is unit-testable
 * without a DOM (the `timelinePoints` / `wallModel` precedent).
 *
 * Honesty rules carried through:
 *   * `end=null` from the server is an OPEN window (still the current head / no
 *     close stamp). We resolve it to "now" for LAYOUT only and flag `open` so
 *     the panel renders it as a live, un-terminated bar — never a fabricated
 *     close.
 *   * A supersession edge is only drawn when BOTH endpoints are in view — a
 *     dangling `superseded_by` pointer (replacement outside the window) is
 *     surfaced on the item, not invented as a floating edge.
 */

import { SEVERITY_COLOR as SEVERITY_RAMP } from '@/v4/world/types'

// ---------------------------------------------------------------------------
// Wire shapes — mirror `timeline_api.TimelineItem` / `TimelineResponse` 1:1.
// ---------------------------------------------------------------------------

export type TimelineItemKind = 'fact' | 'situation' | 'finding'

export interface TimelineItem {
  id: string
  kind: TimelineItemKind
  label: string
  start: string // ISO
  end: string | null // ISO, or null = open window
  status?: string | null
  severity?: string | null
  category?: string | null
  target_id?: string | null
  superseded_by?: string | null
}

export interface TimelineResponse {
  days: number
  server_now: string
  target_id: string | null
  items: TimelineItem[]
  counts: Record<string, number>
  truncated: Record<string, boolean>
}

// ---------------------------------------------------------------------------
// Lanes + palette
// ---------------------------------------------------------------------------

/** Lane order (top → bottom). Situations first (the widest spans), then the
 *  finding supersession sequence, then the fact validity bands. */
export const LANE_ORDER: readonly TimelineItemKind[] = ['situation', 'finding', 'fact']

export const LANE: Record<TimelineItemKind, number> = {
  situation: 0,
  finding: 1,
  fact: 2,
}

export const LANE_LABEL: Record<TimelineItemKind, string> = {
  situation: 'situations',
  finding: 'findings',
  fact: 'facts',
}

/** Base per-kind color (findings recolor by severity — see `itemColor`). */
export const KIND_COLOR: Record<TimelineItemKind, string> = {
  situation: '#fb7185', // rose-400
  finding: '#fbbf24', // amber-400
  fact: '#34d399', // emerald-400
}

/**
 * Severity → finding-bar color. The ONE severity ramp (v4/world/types.ts), not
 * a private copy — see the note in `lib/timelinePoints.ts`.
 */
export const SEVERITY_COLOR: Record<string, string> = SEVERITY_RAMP

// ---------------------------------------------------------------------------
// Shaped item
// ---------------------------------------------------------------------------

export interface ShapedItem {
  id: string
  kind: TimelineItemKind
  label: string
  /** Window start (epoch ms). */
  startMs: number
  /** Window end (epoch ms) — resolved to `nowMs` when the window is open. */
  endMs: number
  /** True when the server sent `end=null` (still-valid, un-terminated window). */
  open: boolean
  /** Lane index (see `LANE`). */
  lane: number
  status?: string | null
  severity?: string | null
  category?: string | null
  supersededBy?: string | null
  targetId?: string | null
}

function ms(iso: string | null | undefined): number {
  if (!iso) return NaN
  return new Date(iso).getTime()
}

/**
 * Shape one wire item for layout. Returns `null` when the start is unparseable
 * (a row we cannot place is dropped, never guessed onto the axis).
 *
 * The open-window end resolves to `nowMs` for LAYOUT and sets `open=true`. A
 * pathological `end < start` (clock skew) clamps to a zero-width bar at `start`
 * rather than drawing backwards.
 */
export function shapeItem(item: TimelineItem, nowMs: number): ShapedItem | null {
  const startMs = ms(item.start)
  if (!Number.isFinite(startMs)) return null
  const rawEnd = ms(item.end)
  const open = item.end == null || !Number.isFinite(rawEnd)
  let endMs = open ? nowMs : rawEnd
  if (endMs < startMs) endMs = startMs
  return {
    id: item.id,
    kind: item.kind,
    label: item.label,
    startMs,
    endMs,
    open,
    lane: LANE[item.kind] ?? 0,
    status: item.status ?? null,
    severity: item.severity ?? null,
    category: item.category ?? null,
    supersededBy: item.superseded_by ?? null,
    targetId: item.target_id ?? null,
  }
}

/** Shape + drop unplaceable rows, preserving input order. */
export function shapeItems(items: TimelineItem[], nowMs: number): ShapedItem[] {
  const out: ShapedItem[] = []
  for (const it of items) {
    const s = shapeItem(it, nowMs)
    if (s) out.push(s)
  }
  return out
}

/** The bar color for a shaped item — findings by severity, else the kind color. */
export function itemColor(item: ShapedItem): string {
  if (item.kind === 'finding' && item.severity && SEVERITY_COLOR[item.severity]) {
    return SEVERITY_COLOR[item.severity]
  }
  return KIND_COLOR[item.kind]
}

// ---------------------------------------------------------------------------
// Domain + zoom/brush (pure)
// ---------------------------------------------------------------------------

/** Padded [min,max] epoch-ms domain across all shaped windows, or undefined
 *  when there is nothing to show. */
export function timeDomain(items: ShapedItem[]): [number, number] | undefined {
  if (items.length === 0) return undefined
  let min = items[0].startMs
  let max = items[0].endMs
  for (const it of items) {
    if (it.startMs < min) min = it.startMs
    if (it.endMs > max) max = it.endMs
  }
  const pad = max === min ? 5 * 60_000 : (max - min) * 0.03
  return [min - pad, max + pad]
}

/** Minimum zoom window — 1 second — so the axis can drill ms→months without
 *  ever collapsing to a zero-width (divide-by-zero) domain. */
export const MIN_ZOOM_MS = 1000

/**
 * Zoom a `[lo,hi]` domain by `factor` (<1 zooms in, >1 zooms out) about a pivot
 * fraction `pivot` in [0,1] (0=left edge, 0.5=center, 1=right edge). Pure; the
 * window is clamped to `MIN_ZOOM_MS` so it never inverts or collapses.
 */
export function zoomDomain(
  domain: [number, number],
  factor: number,
  pivot = 0.5,
): [number, number] {
  const [lo, hi] = domain
  const span = hi - lo
  const anchor = lo + span * pivot
  let next = span * factor
  if (next < MIN_ZOOM_MS) next = MIN_ZOOM_MS
  const nlo = anchor - next * pivot
  const nhi = nlo + next
  return [nlo, nhi]
}

/**
 * Pan a domain by `deltaMs` (a brush drag). Pure; window width preserved.
 */
export function panDomain(domain: [number, number], deltaMs: number): [number, number] {
  return [domain[0] + deltaMs, domain[1] + deltaMs]
}

/** Does a shaped item's window overlap the visible `[lo,hi]` domain at all? */
export function isVisible(item: ShapedItem, domain: [number, number]): boolean {
  return item.endMs >= domain[0] && item.startMs <= domain[1]
}

/** Items whose window overlaps the visible domain (brush filter). */
export function visibleItems(items: ShapedItem[], domain: [number, number]): ShapedItem[] {
  return items.filter((it) => isVisible(it, domain))
}

/**
 * Map an epoch-ms value onto an x pixel within a plot of width `width`, given
 * the visible `domain`. Clamps to [0,width] so an off-window edge draws at the
 * boundary rather than outside the plot.
 */
export function xOf(value: number, domain: [number, number], width: number): number {
  const [lo, hi] = domain
  const span = hi - lo || 1
  const x = ((value - lo) / span) * width
  if (x < 0) return 0
  if (x > width) return width
  return x
}

// ---------------------------------------------------------------------------
// Supersession sequencing
// ---------------------------------------------------------------------------

export interface SupersessionEdge {
  /** The superseded (older) item. */
  from: ShapedItem
  /** The replacement item — only present when it's also in view. */
  to: ShapedItem
}

/**
 * Build the supersession-chain edges: for each item carrying `supersededBy`,
 * draw an edge to its replacement — but ONLY when the replacement is also in
 * the shaped set. A dangling pointer (replacement outside the window) yields no
 * floating edge; the item still surfaces its `supersededBy` for the tooltip.
 * Pure; deterministic (input order preserved).
 */
export function supersessionEdges(items: ShapedItem[]): SupersessionEdge[] {
  const byId = new Map<string, ShapedItem>()
  for (const it of items) byId.set(it.id, it)
  const edges: SupersessionEdge[] = []
  for (const it of items) {
    if (!it.supersededBy) continue
    const to = byId.get(it.supersededBy)
    if (to) edges.push({ from: it, to })
  }
  return edges
}

/** Per-kind visible counts + the honest "showing N of total (truncated)"
 *  subtitle inputs, folding the wire counts/truncated with what's in view. */
export interface KindTally {
  kind: TimelineItemKind
  shown: number
  total: number
  truncated: boolean
}

export function kindTallies(
  shaped: ShapedItem[],
  counts: Record<string, number>,
  truncated: Record<string, boolean>,
): KindTally[] {
  return LANE_ORDER.map((kind) => ({
    kind,
    shown: shaped.filter((it) => it.kind === kind).length,
    total: counts[kind] ?? 0,
    truncated: truncated[kind] ?? false,
  }))
}
