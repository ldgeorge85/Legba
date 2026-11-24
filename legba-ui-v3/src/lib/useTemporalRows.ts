/**
 * `useTemporalRows` — the one reader of the rows the temporal layers are over
 * (`lib/temporalLayers.ts`). The scrubber and the Events lanes of
 * `system.timeline` share it, so both clocks of one row read the same on both
 * surfaces and the routes are fetched once per minute, not once per surface.
 *
 * Existing routes only: `/signals`, `/findings`, `/situations`, `/v3/events`.
 * Each list route answers `{data, next_cursor}`; a bare array is accepted too
 * (older `/situations` builds), and a 404 is an empty list rather than an
 * error — a route a deployment does not mount has no rows to draw.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet, ApiError } from '@/lib/api'
import { WORLD_EVENTS_PATH } from '@/lib/queryKeys'
import {
  eventRow,
  findingRow,
  signalRow,
  situationRow,
  type EventWire,
  type FindingWire,
  type SignalWire,
  type SituationWire,
  type TemporalRow,
  type TemporalSource,
} from '@/lib/temporalLayers'

/** Rows read per paged source — a recent slice, the depth the lanes always
 *  read. `/situations` takes no limit and answers its whole recent page. */
export const TEMPORAL_READ_LIMIT = { signals: 500, findings: 200, events: 500 } as const

async function listRows<T>(path: string): Promise<T[]> {
  try {
    const res = await apiGet<{ data?: T[] } | T[]>(path)
    if (Array.isArray(res)) return res
    return Array.isArray(res?.data) ? res.data : []
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return []
    throw e
  }
}

const READERS: Record<TemporalSource, () => Promise<TemporalRow[]>> = {
  signals: async () =>
    // W11: the summary weight (a title, a category, two clocks) — the full
    // payload was 2.21 MB live at 500 rows, read on every landing.
    (await listRows<SignalWire>(`/signals?limit=${TEMPORAL_READ_LIMIT.signals}&fields=summary`)).map(signalRow),
  // U-5: the summary projection — a finding's lane reads only its id, title,
  // severity and two clocks, all of which `fields=summary` carries; the full
  // row was ~12 MB live for 200 rows.
  findings: async () =>
    (
      await listRows<FindingWire>(`/findings?limit=${TEMPORAL_READ_LIMIT.findings}&fields=summary`)
    ).map(findingRow),
  situations: async () => (await listRows<SituationWire>('/situations')).map(situationRow),
  // Roll 87: the light row by name (the title, severity, lifecycle, both
  // clocks) — the SAME read as the map's events layer (mapData.ts
  // WORLD_EVENTS_PATH), so the one GET answers both; whole, 0.42 MB live.
  events: async () => (await listRows<EventWire>(WORLD_EVENTS_PATH)).map(eventRow),
}

export interface TemporalRowsResult {
  rows: TemporalRow[]
  isLoading: boolean
  /** The sources whose read failed — their layers are unread, not empty. */
  failed: TemporalSource[]
  refetch: () => void
}

function useSource(source: TemporalSource) {
  return useQuery({
    queryKey: ['temporal-rows', source],
    queryFn: READERS[source],
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}

export function useTemporalRows(): TemporalRowsResult {
  const signals = useSource('signals')
  const findings = useSource('findings')
  const situations = useSource('situations')
  const events = useSource('events')
  const all = { signals, findings, situations, events }
  // One array per data change, so a consumer's memo over `rows` holds still
  // between polls.
  const rows = useMemo<TemporalRow[]>(
    () => [
      ...(signals.data ?? []),
      ...(findings.data ?? []),
      ...(situations.data ?? []),
      ...(events.data ?? []),
    ],
    [signals.data, findings.data, situations.data, events.data],
  )
  const failed = (Object.entries(all) as Array<[TemporalSource, typeof signals]>)
    .filter(([, q]) => q.error)
    .map(([source]) => source)
  return {
    rows,
    isLoading: signals.isLoading || findings.isLoading || situations.isLoading || events.isLoading,
    failed,
    refetch: () => {
      for (const q of Object.values(all)) void q.refetch()
    },
  }
}
