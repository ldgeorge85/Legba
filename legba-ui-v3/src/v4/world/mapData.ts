/**
 * The World — map data hooks (Track A · agent A).
 *
 * react-query hooks that fetch the global signal/finding/situation streams and
 * project the raw API rows onto the orchestrator-owned `WorldSignal` /
 * `WorldFinding` / `WorldSituation` contracts (./types). The map component
 * (WorldMap.tsx) consumes only these typed shapes — it never sees a raw Row.
 *
 * Endpoints (base `/api/v1`, bearer auto via `apiGet`):
 *   GET /signals?since&source_id&limit&cursor → { data: Row[], next_cursor }
 *   GET /findings?limit                       → { data: Row[], next_cursor }
 *   GET /situations                           → Situation[]
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { WORLD_EVENTS_KEY, WORLD_EVENTS_PATH, WORLD_SITUATIONS_KEY } from '@/lib/queryKeys'
import { resolveCountry } from '@/lib/countryGeo'
import {
  cameraFor,
  geometryFor,
  useSubjectGeometryIndex,
  type SubjectGeometryIndex,
} from '@/lib/subjectGeometry'
import type {
  EventLifecycle,
  Severity,
  SituationLifecycle,
  WorldEvent,
  WorldFinding,
  WorldSignal,
  WorldSituation,
} from './types'

// ---------------------------------------------------------------------------
// Raw API row shapes (what the endpoints actually return).
// ---------------------------------------------------------------------------

interface SignalGeo {
  lat?: number | null
  lon?: number | null
  country_iso2?: string | null
  precision?: string | null
}

interface SignalData {
  geo?: SignalGeo | null
  /** The publisher's own instant — the signal's VALID time. */
  published_at?: string | null
  title?: string | null
  summary?: string | null
  [k: string]: unknown
}

interface SignalRow {
  id: string
  source_id: string | null
  /** JSONB payload — the geocoder writes data.geo.{lat,lon}. There is NO
   *  top-level `payload`/`fetched_at`; the time is event_timestamp/produced_at. */
  data?: SignalData | null
  title?: string | null
  severity?: string | null
  /** ISO2 codes (signals.geo), top-level. */
  geo?: string[] | null
  event_timestamp?: string | null
  produced_at?: string | null
  created_at?: string | null
  language?: string | null
}

interface FindingData {
  title?: string | null
  geo?: SignalGeo | null
  [k: string]: unknown
}

interface FindingRow {
  id: string
  target_id: string | null
  analyst_id: string | null
  produced_at: string
  severity: string | null
  confidence?: number | null
  kind?: string | null
  title?: string | null
  data?: FindingData | null
  /** ISO2 codes, if the finding carries its own geo. */
  geo?: string[] | null
}

/**
 * The `/situations` row as the route serves it (a `{data, next_cursor}` page):
 * `name` and `status`, not `title` and `state`. `last_event_at` is the
 * situation's VALID time (its newest member event); `created_at` its record
 * time. No country list is served today, so a situation places only through
 * its subject's declared geometry, where it has a subject.
 */
interface SituationRow {
  id: string
  name?: string | null
  status?: string | null
  target_id?: string | null
  countries?: string[] | null
  last_event_at?: string | null
  produced_at?: string | null
  updated_at?: string | null
  created_at?: string | null
  [k: string]: unknown
}

/** V3/P6 — the `/v3/events` row (events_api.EventRow). */
interface EventRow {
  id: string
  title: string
  lifecycle_state: string
  severity?: string | null
  category?: string | null
  target_id?: string | null
  /** ISO2 codes (events.geo). */
  geo?: string[] | null
  geo_lat?: number | null
  geo_lon?: number | null
  time_start?: string | null
  time_end?: string | null
  produced_at?: string | null
}

interface Page<R> {
  data: R[]
  next_cursor: string | null
}

// ---------------------------------------------------------------------------
// Helpers.
// ---------------------------------------------------------------------------

// U-4/U-5 fetch diet: ONE page. The flat map used to walk seven sequential
// 500-row pages (~17.8 MB live) at the end of every landing; it now reads the
// newest 500 and says so (the window filter is applied client-side).
const SIGNAL_PAGE_LIMIT = 500
const SIGNAL_MAX_ROWS = 500
const FINDING_LIMIT = 500

const SEVERITIES: ReadonlySet<string> = new Set<Severity>([
  'critical',
  'high',
  'medium',
  'low',
  'info',
])

/** Coerce an arbitrary API value to a known Severity, defaulting to `info`. */
function asSeverity(v: unknown): Severity {
  return typeof v === 'string' && SEVERITIES.has(v) ? (v as Severity) : 'info'
}

const SITUATION_STATES: ReadonlySet<string> = new Set<SituationLifecycle>([
  'active',
  'escalating',
  'resolved',
])

function asLifecycle(v: unknown): SituationLifecycle {
  // The route's `status` says `closed` where the map's encoding says resolved.
  if (v === 'closed') return 'resolved'
  return typeof v === 'string' && SITUATION_STATES.has(v)
    ? (v as SituationLifecycle)
    : 'active'
}

/** V3/P6 — the event five-state vocabulary; an unknown value coerces to
 *  `active` (the ledger-stamped event is live unless it says resolved). */
const EVENT_LIFECYCLES: ReadonlySet<string> = new Set<EventLifecycle>([
  'emerging',
  'developing',
  'active',
  'evolving',
  'resolved',
])

function asEventLifecycle(v: unknown): EventLifecycle {
  return typeof v === 'string' && EVENT_LIFECYCLES.has(v)
    ? (v as EventLifecycle)
    : 'active'
}

/**
 * Parse an ISO timestamp to epoch ms; falls back to `now` so a malformed/absent
 * timestamp still places inside the default window rather than at epoch 0.
 */
function parseTs(iso: string | null | undefined): number {
  if (!iso) return Date.now()
  const t = Date.parse(iso)
  return Number.isNaN(t) ? Date.now() : t
}

/**
 * A VALID-time instant, or NaN when the row carries none. Never borrowed from
 * the record clock (`created_at` / `event_timestamp` / `produced_at` on
 * `/signals` all carry the fetch time): a NaN never falls inside a window, so a
 * row with no valid time is not drawn on the window rather than drawn at the
 * moment we fetched it. (Mirrors the UI-B4/time lane's `signalRow` field
 * choice: valid = `data.published_at`, record = `created_at`.)
 */
function validTs(iso: string | null | undefined): number {
  return iso ? Date.parse(iso) : Number.NaN
}

/** First country in the list that the gazetteer can place, as [lat, lon]. */
function centroidOf(countries: string[]): { lat: number; lon: number } | null {
  for (const iso of countries) {
    const fix = resolveCountry(iso)
    if (fix) return { lat: fix.lat, lon: fix.lon }
  }
  return null
}

// ---------------------------------------------------------------------------
// Signals — global stream, cursor-paginated up to ~3000 rows.
// ---------------------------------------------------------------------------

async function fetchWorldSignals(): Promise<WorldSignal[]> {
  const out: WorldSignal[] = []
  let cursor: string | null = null

  // Cursor-paginate up to the cap. The window filter is applied client-side by
  // the map (it owns the scrubber), so we fetch the most recent N globally.
  for (let guard = 0; guard < 12 && out.length < SIGNAL_MAX_ROWS; guard++) {
    // The light weight by name (roll 86): it keeps `data.geo` cut to the point
    // (lat/lon) and `data.published_at` — all this layer reads. The full
    // payload is ~4.5 KB a row (2.2 MB per 500-row page, up to six pages).
    const params = new URLSearchParams({ limit: String(SIGNAL_PAGE_LIMIT), fields: 'summary' })
    if (cursor) params.set('cursor', cursor)
    const page: Page<SignalRow> = await apiGet<Page<SignalRow>>(
      `/signals?${params.toString()}`,
    )

    for (const row of page.data) {
      const countries = row.geo ?? []
      const g = row.data?.geo
      let lat = typeof g?.lat === 'number' && !Number.isNaN(g.lat) ? g.lat : null
      let lon = typeof g?.lon === 'number' && !Number.isNaN(g.lon) ? g.lon : null
      // Fall back to the first country's gazetteer centroid so a signal geocoded
      // only to country level still places on the map.
      if (lat == null || lon == null) {
        const fix = centroidOf(countries)
        if (!fix) continue
        lat = fix.lat
        lon = fix.lon
      }

      out.push({
        id: row.id,
        lat,
        lon,
        countries,
        severity: asSeverity(row.severity),
        sourceId: row.source_id,
        // VALID time — the publisher's instant, not the fetch time.
        ts: validTs(row.data?.published_at),
        title: row.title ?? row.data?.title ?? '(untitled)',
        language: row.language ?? null,
      })
      if (out.length >= SIGNAL_MAX_ROWS) break
    }

    cursor = page.next_cursor
    if (!cursor) break
  }

  return out
}

export interface UseWorldSignalsResult {
  signals: WorldSignal[]
  isLoading: boolean
  /** True once the read has answered (an empty answer is a measured zero). */
  read: boolean
}

export function useWorldSignals(): UseWorldSignalsResult {
  const q = useQuery<WorldSignal[]>({
    queryKey: ['world-signals'],
    queryFn: fetchWorldSignals,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  return { signals: q.data ?? [], isLoading: q.isLoading, read: q.data !== undefined }
}

// ---------------------------------------------------------------------------
// Findings — single page; geo backfilled from countries[0] when absent.
// ---------------------------------------------------------------------------

async function fetchWorldFindings(): Promise<WorldFinding[]> {
  // U-4/U-5 fetch diet: the summary weight (`fields=summary`, a few hundred
  // bytes a row) — the default weight was ~29 MB live for 500 rows, read only
  // for `geo`. The summary carries no geo, so a finding places through its
  // subject's declared single place (`placeFinding`, below) — never at a
  // guessed centroid.
  const page: Page<FindingRow> = await apiGet<Page<FindingRow>>(
    `/findings?limit=${FINDING_LIMIT}&fields=summary`,
  )

  return page.data.map((row): WorldFinding => {
    const countries = row.geo ?? []
    const g = row.data?.geo
    let lat = typeof g?.lat === 'number' ? g.lat : null
    let lon = typeof g?.lon === 'number' ? g.lon : null
    if (lat == null || lon == null) {
      const fix = centroidOf(countries)
      lat = fix ? fix.lat : null
      lon = fix ? fix.lon : null
    }
    return {
      id: row.id,
      lat,
      lon,
      countries,
      severity: asSeverity(row.severity),
      targetId: row.target_id,
      analystId: row.analyst_id,
      ts: parseTs(row.produced_at),
      title: row.title ?? row.data?.title ?? '(untitled)',
      confidence: row.confidence ?? null,
    }
  })
}

/**
 * Place a finding that carries no geo of its own through its SUBJECT'S
 * DECLARED GEOMETRY (`lib/subjectGeometry.ts`) — never through an ISO code
 * parsed out of the subject's id. Only a geometry that names ONE place lends
 * a position (a point, or a single declared code): a multi-country or polygon
 * subject is a region, and pinning its finding to the middle of it would claim
 * a precision nobody asserted, so that finding stays unplaced.
 */
export function placeFinding(f: WorldFinding, index: SubjectGeometryIndex | null): WorldFinding {
  if (f.lat != null && f.lon != null) return f
  if (!f.targetId) return f
  const geometry = geometryFor({ kind: 'target', id: f.targetId }, index)
  const camera = cameraFor(geometry)
  const countries = f.countries.length > 0 ? f.countries : geometry.kind === 'iso2' ? geometry.codes : []
  if (camera?.kind !== 'center') return countries === f.countries ? f : { ...f, countries }
  return { ...f, countries, lat: camera.lat, lon: camera.lon }
}

export interface UseWorldFindingsResult {
  findings: WorldFinding[]
  isLoading: boolean
  /** True once the read has answered (an empty answer is a measured zero). */
  read: boolean
}

export function useWorldFindings(): UseWorldFindingsResult {
  const q = useQuery<WorldFinding[]>({
    queryKey: ['world-findings'],
    queryFn: fetchWorldFindings,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  const index = useSubjectGeometryIndex()
  const findings = useMemo(() => (q.data ?? []).map((f) => placeFinding(f, index)), [q.data, index])
  return { findings, isLoading: q.isLoading, read: q.data !== undefined }
}

// ---------------------------------------------------------------------------
// Situations — bare array; centroid resolved from the country list.
// ---------------------------------------------------------------------------

/** A situation as read, with the subject it is about (placement only). */
type SituationRead = WorldSituation & { targetId: string | null }

async function fetchWorldSituations(): Promise<SituationRead[]> {
  // The route serves a `{data, next_cursor}` page (not a bare array).
  const page: Page<SituationRow> = await apiGet<Page<SituationRow>>('/situations?limit=500')
  return (page.data ?? []).map((row): SituationRead => ({
    id: row.id,
    title: row.name ?? '(untitled)',
    lifecycle: asLifecycle(row.status),
    countries: row.countries ?? [],
    // VALID time — the newest member event; no fallback to the record clock.
    ts: validTs(row.last_event_at),
    targetId: row.target_id ?? null,
  }))
}

export interface SituationPlacement extends WorldSituation {
  lat: number | null
  lon: number | null
}

export interface UseWorldSituationsResult {
  situations: SituationPlacement[]
  isLoading: boolean
  /** True once the read has answered (an empty answer is a measured zero). */
  read: boolean
}

export function useWorldSituations(opts: { enabled?: boolean } = {}): UseWorldSituationsResult {
  const index = useSubjectGeometryIndex()
  const q = useQuery<SituationRead[]>({
    queryKey: WORLD_SITUATIONS_KEY,
    queryFn: fetchWorldSituations,
    refetchInterval: 60_000,
    staleTime: 30_000,
    // U-4: a hidden map region, or a layer that is off, fetches nothing.
    enabled: opts.enabled ?? true,
  })
  // Attach a placement: the gazetteer centroid of its countries, else its
  // subject's declared geometry when that names ONE place (`placeFinding`'s
  // rule) — never a position the row does not support.
  const situations: SituationPlacement[] = useMemo(
    () =>
      (q.data ?? []).map(({ targetId, ...s }) => {
        const fix = centroidOf(s.countries)
        if (fix) return { ...s, lat: fix.lat, lon: fix.lon }
        const placed = placeFinding(
          { id: s.id, lat: null, lon: null, countries: s.countries, severity: 'info', targetId, analystId: null, ts: s.ts, title: s.title },
          index,
        )
        return { ...s, countries: placed.countries, lat: placed.lat, lon: placed.lon }
      }),
    [q.data, index],
  )
  return { situations, isLoading: q.isLoading, read: q.data !== undefined }
}

// ---------------------------------------------------------------------------
// Events (V3/P6) — bounded occurrences, geo from events.geo_lat/geo_lon ONLY.
// ---------------------------------------------------------------------------

async function fetchWorldEvents(): Promise<WorldEvent[]> {
  // The newest 500 at the light weight, the same read as the time band's
  // events lane (queryKeys.WORLD_EVENTS_PATH; every field below survives it).
  const page: Page<EventRow> = await apiGet<Page<EventRow>>(WORLD_EVENTS_PATH)
  const out: WorldEvent[] = []
  for (const row of page.data) {
    // NO centroid fallback (spec §6.3 — deliberate): an event's placement is
    // the coordinate its writers stamped; a country-only event does not
    // claim a precision it never asserted and simply does not place.
    if (
      typeof row.geo_lat !== 'number' ||
      typeof row.geo_lon !== 'number' ||
      Number.isNaN(row.geo_lat) ||
      Number.isNaN(row.geo_lon)
    ) {
      continue
    }
    out.push({
      id: row.id,
      lat: row.geo_lat,
      lon: row.geo_lon,
      countries: row.geo ?? [],
      severity: asSeverity(row.severity),
      lifecycle: asEventLifecycle(row.lifecycle_state),
      targetId: row.target_id ?? null,
      ts: parseTs(row.time_start ?? row.produced_at),
      title: row.title ?? '(untitled)',
    })
  }
  return out
}

export interface UseWorldEventsResult {
  events: WorldEvent[]
  isLoading: boolean
  /** True once the read has answered (an empty answer is a measured zero). */
  read: boolean
}

export function useWorldEvents(opts: { enabled?: boolean } = {}): UseWorldEventsResult {
  const q = useQuery<WorldEvent[]>({
    queryKey: WORLD_EVENTS_KEY,
    queryFn: fetchWorldEvents,
    refetchInterval: 60_000,
    staleTime: 30_000,
    // U-4: a hidden map region, or a layer that is off, fetches nothing.
    enabled: opts.enabled ?? true,
  })
  return { events: q.data ?? [], isLoading: q.isLoading, read: q.data !== undefined }
}
