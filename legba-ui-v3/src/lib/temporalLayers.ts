/**
 * Temporal layers — VALID time vs RECORD time (UI-D1 §2.1, the temporal-layer
 * object; W8 UI-B4/time).
 *
 * Every row the workstation draws on a clock has two instants, and they are
 * different clocks:
 *
 *   - VALID time  — when the world was so (a publisher's date, the occurrence
 *     a finding asserts from, a situation's last event, an event's start);
 *   - RECORD time — when the platform learned it (the row's own write).
 *
 * A temporal layer is one of those two clocks over one source of rows, inside
 * one window: `{id, time, window, source}`. The scrubber draws BOTH axes for
 * every map layer that is on; the Events lanes of `system.timeline` plot on
 * either and print both in the tooltip; a figure's AS-OF is the record time,
 * because "as of" means "what we had learned by then".
 *
 * WHAT THE ROUTES ALREADY RETURN (no new route; read live 2026-10-04):
 *
 *   | source     | valid time                         | record time                    |
 *   |------------|------------------------------------|--------------------------------|
 *   | signals    | `data.published_at` (the publisher)| `created_at` (= fetched_at)    |
 *   | findings   | `produced_at`                      | `created_at`                   |
 *   | situations | `last_event_at`                    | `created_at`                   |
 *   | events     | `valid_from` → `time_start`        | `produced_at`                  |
 *
 * The `/signals` route projects the row's `fetched_at` onto `event_timestamp`,
 * `produced_at` AND `created_at` (`substrate_reads_api._hydrate_signal`), so
 * none of those is a valid time — they are all the record time. The valid time
 * is the publisher's own date inside the payload, and a signal whose source
 * printed none has NO valid time: it is never borrowed from the record clock.
 * A row missing an instant is absent from that axis and counted as such.
 *
 * A HISTORY CITATION keeps its tense marker verbatim. The producer
 * (`analysts/history_grounding.stale_tense_marker`) renders
 * `(historical: valid YYYY..YYYY, recorded YYYY-MM)` — valid and record time
 * in one string — and every surface prints that string as served
 * (`lib/citationsModel.ts` → `CitedProse`, the desk brief's temporal notes).
 * Nothing here re-derives it; `temporalLayers.test.ts` pins the bytes.
 *
 * Pure: no React, no fetch. `useTemporalRows` is the one reader.
 */
import type { AbsentFigure, FigureData } from '@/lib/Figure'
import type { WorldLayer } from '@/v4/world/worldState'

/** The two clocks. */
export type TemporalAxis = 'valid' | 'record'

export const TEMPORAL_AXES: readonly TemporalAxis[] = ['valid', 'record']

/** What each clock means, in the reader's words (scrubber + timeline tooltips). */
export const AXIS_MEANING: Record<TemporalAxis, string> = {
  valid: 'valid time — when the world was so',
  record: 'record time — when the platform learned it',
}

/** The sources a temporal layer can be over: the map layers that carry a time. */
export type TemporalSource = Exclude<WorldLayer, 'entities'>

export const TEMPORAL_SOURCES: readonly TemporalSource[] = ['signals', 'findings', 'situations', 'events']

export interface TemporalWindow {
  startMs: number
  endMs: number
}

/** One temporal layer: one clock over one source, inside one window. */
export interface TemporalLayer {
  /** `<source>:<time>`, e.g. `signals:valid`. */
  id: string
  time: TemporalAxis
  window: TemporalWindow
  source: TemporalSource
}

/** The row kind a source's rows select as (the selection store's kinds). */
export type TemporalRowKind = 'signal' | 'finding' | 'situation' | 'event'

export const SOURCE_KIND: Record<TemporalSource, TemporalRowKind> = {
  signals: 'signal',
  findings: 'finding',
  situations: 'situation',
  events: 'event',
}

/** One row on the two clocks. `null` = the route carries no such instant. */
export interface TemporalRow {
  id: string
  source: TemporalSource
  kind: TemporalRowKind
  title: string
  validMs: number | null
  recordMs: number | null
  /** Lane annotation (a signal's category, a finding's severity, a state). */
  subtitle: string
  severity?: string | null
  category?: string
}

// ---------------------------------------------------------------------------
// Row adapters — the route shapes, read as they are served.
// ---------------------------------------------------------------------------

/** An ISO instant → epoch ms; `null` when absent or unparseable (never 0). */
export function instantMs(iso: unknown): number | null {
  if (typeof iso !== 'string' || iso === '') return null
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

function firstInstant(...values: unknown[]): number | null {
  for (const v of values) {
    const t = instantMs(v)
    if (t !== null) return t
  }
  return null
}

function text(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/** `/signals` row. */
export interface SignalWire {
  id: string
  title?: string | null
  category?: string | null
  created_at?: string | null
  event_timestamp?: string | null
  produced_at?: string | null
  data?: { title?: string | null; published_at?: string | null } | null
}

export function signalRow(s: SignalWire): TemporalRow {
  const category = text(s.category).toLowerCase()
  return {
    id: s.id,
    source: 'signals',
    kind: 'signal',
    title: s.title || s.data?.title || '(untitled)',
    validMs: instantMs(s.data?.published_at),
    // All three are the row's fetched_at on this route; created_at first.
    recordMs: firstInstant(s.created_at, s.event_timestamp, s.produced_at),
    subtitle: category,
    category,
  }
}

/** `/findings` row. */
export interface FindingWire {
  id: string
  title?: string | null
  severity?: string | null
  produced_at?: string | null
  created_at?: string | null
}

export function findingRow(f: FindingWire): TemporalRow {
  return {
    id: f.id,
    source: 'findings',
    kind: 'finding',
    title: f.title || '(untitled)',
    validMs: instantMs(f.produced_at),
    recordMs: instantMs(f.created_at),
    subtitle: text(f.severity),
    severity: f.severity ?? null,
  }
}

/** `/situations` row (the substrate shape: `name`, `status`, `last_event_at`). */
export interface SituationWire {
  id: string
  name?: string | null
  title?: string | null
  status?: string | null
  state?: string | null
  last_event_at?: string | null
  created_at?: string | null
}

export function situationRow(s: SituationWire): TemporalRow {
  return {
    id: s.id,
    source: 'situations',
    kind: 'situation',
    title: s.name || s.title || '(untitled)',
    validMs: instantMs(s.last_event_at),
    recordMs: instantMs(s.created_at),
    subtitle: text(s.status ?? s.state),
  }
}

/** `/v3/events` row. */
export interface EventWire {
  id: string
  title?: string | null
  lifecycle_state?: string | null
  severity?: string | null
  valid_from?: string | null
  time_start?: string | null
  produced_at?: string | null
}

export function eventRow(e: EventWire): TemporalRow {
  return {
    id: e.id,
    source: 'events',
    kind: 'event',
    title: e.title || '(untitled)',
    validMs: firstInstant(e.valid_from, e.time_start),
    recordMs: instantMs(e.produced_at),
    subtitle: text(e.lifecycle_state),
    severity: e.severity ?? null,
  }
}

// ---------------------------------------------------------------------------
// Layers, windows, density.
// ---------------------------------------------------------------------------

/** The instant a row carries on one axis. */
export function axisMs(row: TemporalRow, axis: TemporalAxis): number | null {
  return axis === 'valid' ? row.validMs : row.recordMs
}

/** The temporal layers for the sources that are on: both clocks for each. */
export function temporalLayers(
  sources: readonly TemporalSource[],
  window: TemporalWindow,
): TemporalLayer[] {
  const out: TemporalLayer[] = []
  for (const source of sources) {
    for (const time of TEMPORAL_AXES) {
      out.push({ id: `${source}:${time}`, time, window, source })
    }
  }
  return out
}

/** The temporal sources among the map's layer toggles that are on. */
export function sourcesOn(layers: Partial<Record<WorldLayer, boolean>>): TemporalSource[] {
  return TEMPORAL_SOURCES.filter((s) => layers[s])
}

/** The rows a layer holds: its source's rows whose instant on its clock is inside its window. */
export function rowsInLayer(rows: readonly TemporalRow[], layer: TemporalLayer): TemporalRow[] {
  return rows.filter((r) => {
    if (r.source !== layer.source) return false
    const t = axisMs(r, layer.time)
    return t !== null && t >= layer.window.startMs && t <= layer.window.endMs
  })
}

/** Rows of a source with NO instant on an axis (absent from it, not at 0). */
export function rowsWithoutAxis(
  rows: readonly TemporalRow[],
  axis: TemporalAxis,
  source?: TemporalSource,
): number {
  let n = 0
  for (const r of rows) {
    if (source && r.source !== source) continue
    if (axisMs(r, axis) === null) n++
  }
  return n
}

/** `YYYY-MM-DD HH:mmZ` — the as-of form every temporal figure prints. */
export function instantZ(ms: number): string {
  const iso = new Date(ms).toISOString()
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}Z`
}

/** The latest record time among rows — the as-of of anything counted over them. */
export function latestRecordMs(rows: readonly TemporalRow[]): number | null {
  let best: number | null = null
  for (const r of rows) {
    if (r.recordMs !== null && (best === null || r.recordMs > best)) best = r.recordMs
  }
  return best
}

const NO_ROWS: AbsentFigure = {
  state: 'no_read_yet',
  unit: 'rows',
  asOf: { absent: 'no row in the window' },
}

/**
 * The figure a layer prints: its row count in the window, AS OF the latest
 * record time among those rows. A layer with no rows in the window prints
 * *no read yet* — never `0 rows`, because an empty window on a 500-row read
 * says nothing about whether the world was quiet.
 */
export function layerFigure(rows: readonly TemporalRow[], layer: TemporalLayer): FigureData {
  const inLayer = rowsInLayer(rows, layer)
  if (inLayer.length === 0) return NO_ROWS
  const asOf = latestRecordMs(inLayer)
  return {
    state: 'measured',
    value: inLayer.length,
    unit: 'rows',
    asOf: asOf === null ? { absent: 'no record time on these rows' } : instantZ(asOf),
  }
}

/**
 * Bucket counts of the rows on one clock across `[lo, hi]` — the density
 * strip the scrubber draws under its thumbs. Rows outside, or with no instant
 * on that clock, are not counted.
 */
export function densityBuckets(
  rows: readonly TemporalRow[],
  axis: TemporalAxis,
  lo: number,
  hi: number,
  buckets: number,
): number[] {
  const out = new Array<number>(Math.max(1, buckets)).fill(0)
  const span = hi - lo
  if (!(span > 0)) return out
  for (const r of rows) {
    const t = axisMs(r, axis)
    if (t === null || t < lo || t > hi) continue
    const i = Math.min(out.length - 1, Math.floor(((t - lo) / span) * out.length))
    out[i]++
  }
  return out
}
