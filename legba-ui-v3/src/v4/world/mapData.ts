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
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { resolveCountry } from '@/lib/countryGeo'
import type {
  Severity,
  SituationLifecycle,
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

interface SituationRow {
  id: string
  title: string
  state: SituationLifecycle | string
  countries?: string[] | null
  produced_at?: string | null
  updated_at?: string | null
  created_at?: string | null
  [k: string]: unknown
}

interface Page<R> {
  data: R[]
  next_cursor: string | null
}

// ---------------------------------------------------------------------------
// Helpers.
// ---------------------------------------------------------------------------

const SIGNAL_PAGE_LIMIT = 500
const SIGNAL_MAX_ROWS = 3000
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
  return typeof v === 'string' && SITUATION_STATES.has(v)
    ? (v as SituationLifecycle)
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

/** First country in the list that the gazetteer can place, as [lat, lon]. */
function centroidOf(countries: string[]): { lat: number; lon: number } | null {
  for (const iso of countries) {
    const fix = resolveCountry(iso)
    if (fix) return { lat: fix.lat, lon: fix.lon }
  }
  return null
}

/** Country targets encode their ISO2 in the id (country_g20_us → US). */
function targetCountry(targetId: string | null): string | null {
  const m = targetId?.match(/country_g20_([a-z]{2})/i)
  return m ? m[1].toUpperCase() : null
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
    const params = new URLSearchParams({ limit: String(SIGNAL_PAGE_LIMIT) })
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
        ts: parseTs(row.event_timestamp ?? row.produced_at ?? row.created_at),
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
}

export function useWorldSignals(): UseWorldSignalsResult {
  const q = useQuery<WorldSignal[]>({
    queryKey: ['world-signals'],
    queryFn: fetchWorldSignals,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  return { signals: q.data ?? [], isLoading: q.isLoading }
}

// ---------------------------------------------------------------------------
// Findings — single page; geo backfilled from countries[0] when absent.
// ---------------------------------------------------------------------------

async function fetchWorldFindings(): Promise<WorldFinding[]> {
  const page: Page<FindingRow> = await apiGet<Page<FindingRow>>(
    `/findings?limit=${FINDING_LIMIT}`,
  )

  return page.data.map((row): WorldFinding => {
    // Findings rarely carry geo; fall back to the country target's centroid.
    const tc = targetCountry(row.target_id)
    const countries = row.geo ?? (tc ? [tc] : [])
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

export interface UseWorldFindingsResult {
  findings: WorldFinding[]
  isLoading: boolean
}

export function useWorldFindings(): UseWorldFindingsResult {
  const q = useQuery<WorldFinding[]>({
    queryKey: ['world-findings'],
    queryFn: fetchWorldFindings,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  return { findings: q.data ?? [], isLoading: q.isLoading }
}

// ---------------------------------------------------------------------------
// Situations — bare array; centroid resolved from the country list.
// ---------------------------------------------------------------------------

async function fetchWorldSituations(): Promise<WorldSituation[]> {
  const rows: SituationRow[] = await apiGet<SituationRow[]>('/situations')
  return rows.map((row): WorldSituation => {
    const countries = row.countries ?? []
    return {
      id: row.id,
      title: row.title,
      lifecycle: asLifecycle(row.state),
      countries,
      ts: parseTs(row.produced_at ?? row.updated_at ?? row.created_at),
    }
  })
}

export interface SituationPlacement extends WorldSituation {
  lat: number | null
  lon: number | null
}

export interface UseWorldSituationsResult {
  situations: SituationPlacement[]
  isLoading: boolean
}

export function useWorldSituations(): UseWorldSituationsResult {
  const q = useQuery<WorldSituation[]>({
    queryKey: ['world-situations'],
    queryFn: fetchWorldSituations,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  // Attach a placement centroid (gazetteer) so the map can plot each situation.
  const situations: SituationPlacement[] = (q.data ?? []).map((s) => {
    const fix = centroidOf(s.countries)
    return { ...s, lat: fix ? fix.lat : null, lon: fix ? fix.lon : null }
  })
  return { situations, isLoading: q.isLoading }
}
