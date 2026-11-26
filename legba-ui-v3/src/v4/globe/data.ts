/**
 * The globe's reads (U-4) — every one a route that exists today, each gated
 * on the region being shown AND its layer being on ("no layer fetches until
 * it is on"; "hidden means idle"). They are projected here to the small
 * shapes `prep.ts` takes, so the prep worker never sees a raw row.
 *
 * Shared caches, on purpose: the change wash reads the Board list's own diff
 * (`['morning-judgment', 'since', cursor]`) so the list row and the map mark
 * are the same object from the same answer; the roster, the ledger, events
 * and situations reuse the keys their other readers already hold.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError, apiGet } from '@/lib/api'
import { SUBJECT_ROSTER_QUERY_KEY, SUBJECT_ROSTER_ROUTE, subjectName } from '@/lib/deskNames'
import { changedList, pageVisit } from '@/lib/morningReadBands'
import { LEDGER_ROUTE, type LedgerForecastsResponse } from '@/lib/ledgerModel'
import { subjectReadsQuery, type SubjectReadsPage } from '@/lib/subjectReads'
import { PROFILE_UI, subjectTierIds } from '@/lib/profileUi'
import { useWorldEvents, useWorldSituations } from '@/v4/world/mapData'
import { SERIES_SOURCES } from './chokepoints'
import type { GlobeLayerId } from './catalog'
import type {
  PrepChange,
  PrepForecast,
  PrepIncident,
  PrepPoint,
  PrepRead,
  PrepSeries,
  PrepSubject,
} from './prep'

const REFETCH_MS = 120_000

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}
const ms = (iso: unknown) => (typeof iso === 'string' ? Date.parse(iso) : Number.NaN)

/**
 * Whether a roster row's subject declares a source: the whole body's `sources`
 * list, or the summary weight's `declared_sources` count (the roster reads
 * `fields=summary`, which cuts `sources` away — read from
 * `sources` alone, every subject declared none and every place was hatched
 * "not covered", over every changed area). Neither: null, not a guess.
 */
export function declaresSources(body: Record<string, unknown>): boolean | null {
  if (Array.isArray(body.sources)) return body.sources.length > 0
  const n = body.declared_sources
  return typeof n === 'number' && Number.isFinite(n) ? n > 0 : null
}

/** The roster listing → subjects with their declared codes and whether they declare a source. */
export function subjectsFromRoster(rows: unknown): PrepSubject[] {
  if (!Array.isArray(rows)) return []
  const out: PrepSubject[] = []
  for (const r of rows) {
    const row = obj(r)
    const id = typeof row.descriptor_id === 'string' ? row.descriptor_id : null
    if (!id) continue
    const body = obj(row.body)
    const scope = obj(body.scope)
    const geo = Array.isArray(scope.geo) ? scope.geo.filter((g): g is string => typeof g === 'string') : []
    out.push({ id, name: subjectName(id) ?? (typeof row.name === 'string' ? row.name : id), geo, hasSources: declaresSources(body) })
  }
  return out
}

/** The profile's subject-level read producers (the frames with no child frame). */
export function subjectReadProducers(): string[] {
  const leaf = subjectTierIds(PROFILE_UI.frames)
  return PROFILE_UI.frames.filter((f) => leaf.has(f.id) && f.producer).map((f) => f.producer as string)
}

export interface AtSeaResponse {
  available: boolean
  total: number
  truncated: boolean
  incidents: Array<{
    id: string
    source_id?: string | null
    title?: string
    lat: number
    lon: number
    location_name?: string | null
    inferred?: Array<{ iso2: string; distance_km?: number | null }>
    published_at?: string | null
    fetched_at: string
  }>
}

export interface SeriesResponse {
  available: boolean
  unit?: string | null
  as_of?: string | null
  total_subjects: number
  subjects: Array<{
    subject: string
    name?: string | null
    latest_day: string
    latest: number
    pct_of_baseline?: number | null
    revised?: boolean
    values?: Array<{ d: string; v: number }>
  }>
}

export interface GlobeReads {
  subjects: PrepSubject[] | null
  changes: PrepChange[] | null
  reads: PrepRead[] | null
  points: PrepPoint[] | null
  incidents: PrepIncident[] | null
  forecasts: PrepForecast[] | null
  series: PrepSeries[] | null
  /** The as-of a layer can state (series: the newest provider record). */
  seriesAsOf: string | null
  seriesUnit: string | null
  /** A route that answered "cannot be read" (available: false). */
  unreadable: Partial<Record<GlobeLayerId, true>>
}

export function useGlobeReads(active: boolean, on: Record<GlobeLayerId, boolean>): GlobeReads {
  const roster = useQuery<unknown>({
    queryKey: SUBJECT_ROSTER_QUERY_KEY,
    queryFn: () => apiGet<unknown>(SUBJECT_ROSTER_ROUTE),
    staleTime: 300_000,
    enabled: active,
  })

  // The Board list's own diff, same key, same cursor (this page load's one "since").
  const cursor = useMemo(() => pageVisit().cursor, [])
  const since = useQuery<unknown>({
    queryKey: ['morning-judgment', 'since', cursor],
    queryFn: () => apiGet<unknown>(`/v3/since?cursor=${encodeURIComponent(cursor)}`),
    refetchInterval: REFETCH_MS,
    enabled: active && on.change,
  })

  // The subject reads at verdict weight, under the key the Wall and the rail
  // share (`lib/subjectReads.ts`): one fetch, whoever asks.
  const producer = subjectReadProducers()[0] ?? null
  const reads = useQuery<SubjectReadsPage>({
    ...subjectReadsQuery(producer ?? ''),
    enabled: active && on.blanks && !!producer,
  })

  const ev = useWorldEvents({ enabled: active && on.events })
  const sit = useWorldSituations({ enabled: active && on.events })

  // A map route a registry not yet rebuilt does not serve answers 404: that is
  // an answer (the row says it cannot be read), never retried — a retry would
  // only ask twice (U-7: the --no-map-routes walk counted a duplicate).
  const retryUnless404 = (n: number, e: unknown) => !(e instanceof ApiError && e.status === 404) && n < 3
  const atSea = useQuery<AtSeaResponse>({
    queryKey: ['globe-at-sea', 30],
    queryFn: () => apiGet<AtSeaResponse>('/v3/map/at_sea?days=30&limit=500'),
    refetchInterval: REFETCH_MS,
    retry: retryUnless404,
    enabled: active && on.at_sea,
  })

  const ledger = useQuery<LedgerForecastsResponse | null>({
    queryKey: ['ledger-forecasts'],
    queryFn: () => apiGet<LedgerForecastsResponse>(LEDGER_ROUTE),
    refetchInterval: REFETCH_MS,
    enabled: active && on.forecasts,
  })

  const src = SERIES_SOURCES[0]
  const series = useQuery<SeriesResponse>({
    queryKey: ['globe-series', src?.collectionId, src?.seriesId],
    queryFn: () =>
      apiGet<SeriesResponse>(
        `/v3/map/series?collection_id=${encodeURIComponent(src.collectionId)}&series_id=${encodeURIComponent(src.seriesId)}&days=30`,
      ),
    refetchInterval: 600_000,
    retry: retryUnless404,
    enabled: active && on.series && !!src,
  })

  const subjects = useMemo(() => (roster.data === undefined ? null : subjectsFromRoster(roster.data)), [roster.data])

  const changes = useMemo<PrepChange[] | null>(() => {
    if (since.data === undefined) return null
    return changedList(since.data as Parameters<typeof changedList>[0]).map((r) => ({ id: r.id, targetId: r.targetId, atMs: ms(r.at) }))
  }, [since.data])

  const subjectReads = useMemo<PrepRead[] | null>(() => {
    if (reads.data === undefined) return null
    return (reads.data.data ?? [])
      .filter((r) => typeof r.target_id === 'string' && r.target_id)
      .map((r) => ({ targetId: r.target_id as string, atMs: ms(r.produced_at) }))
  }, [reads.data])

  const points = useMemo<PrepPoint[] | null>(() => {
    if (!ev.read && !sit.read) return null
    const out: PrepPoint[] = []
    for (const e of ev.events) out.push({ id: e.id, kind: 'event', lat: e.lat, lon: e.lon, ts: e.ts, title: e.title, severity: e.severity, lifecycle: e.lifecycle })
    for (const s of sit.situations) {
      if (s.lat == null || s.lon == null) continue
      out.push({ id: s.id, kind: 'situation', lat: s.lat, lon: s.lon, ts: s.ts, title: s.title, severity: null, lifecycle: s.lifecycle })
    }
    return out
  }, [ev.events, ev.read, sit.situations, sit.read])

  const incidents = useMemo<PrepIncident[] | null>(() => {
    if (!atSea.data) return null
    return (atSea.data.incidents ?? []).map((i) => ({
      id: i.id,
      lat: i.lat,
      lon: i.lon,
      ts: ms(i.published_at),
      recordedMs: ms(i.fetched_at),
      title: i.title ?? '',
      place: i.location_name ?? null,
      coasts: (i.inferred ?? []).map((c) => c.iso2),
      sourceId: i.source_id ?? null,
    }))
  }, [atSea.data])

  const forecasts = useMemo<PrepForecast[] | null>(() => {
    if (ledger.data === undefined) return null
    return (ledger.data?.forecasts ?? [])
      .filter((f) => f.status === 'open')
      .map((f) => ({
        id: f.id,
        question: f.question,
        startMs: ms(f.window_start ?? f.sealed_at ?? null),
        dueMs: ms(f.due_at),
        subjectKeys: f.subject_keys ?? [],
      }))
  }, [ledger.data])

  const seriesRows = useMemo<PrepSeries[] | null>(() => {
    if (!series.data) return null
    return (series.data.subjects ?? []).map((s) => ({
      subject: s.subject,
      name: s.name ?? null,
      latest: s.latest,
      pct: s.pct_of_baseline ?? null,
      revised: !!s.revised,
      day: s.latest_day,
      values: (s.values ?? []).map((v) => v.v),
    }))
  }, [series.data])

  // A route that answered "cannot be read", or that failed (a registry not yet
  // rebuilt with the map route answers 404), says so instead of "no read yet".
  const unreadable: GlobeReads['unreadable'] = {}
  if ((atSea.data && atSea.data.available === false) || (atSea.isError && !atSea.data)) unreadable.at_sea = true
  if ((series.data && series.data.available === false) || (series.isError && !series.data)) unreadable.series = true
  if ((ledger.data && (ledger.data as { available?: boolean }).available === false) || (ledger.isError && !ledger.data)) unreadable.forecasts = true
  if (since.isError && !since.data) unreadable.change = true
  if ((reads.data && reads.data.available === false) || (reads.isError && !reads.data)) unreadable.blanks = true

  return {
    subjects,
    changes,
    reads: subjectReads,
    points,
    incidents,
    forecasts,
    series: seriesRows,
    seriesAsOf: series.data?.as_of ?? null,
    seriesUnit: series.data?.unit ?? null,
    unreadable,
  }
}
