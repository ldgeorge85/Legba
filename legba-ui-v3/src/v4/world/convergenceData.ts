/**
 * Convergence-alert data hook (P4-3, feature 4) — the data behind the World
 * map's geo_convergence markers.
 *
 * Fetches recent kind='alert' rows through `GET /v3/since` (the only reachable
 * read surface that carries alert titles), scoped server-side to the
 * `geo_convergence` channel (the additive `channel=` param — the alerts
 * section is severity-ranked under a 50-row cap, so an unscoped read lets
 * high-severity traffic crowd the medium convergence rows out of a busy
 * window), and reduces them to the currently-active convergence bins via the
 * pure `@/lib/convergence` transforms. `/v3/since` is a diff keyed on a
 * cursor, so we look back a bounded window (default 7 days) and surface it
 * honestly as recent convergence activity. Countries / cells with no reachable
 * placement are simply absent — never a fabricated marker.
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import {
  activeConvergenceMarkers,
  type ConvergenceMarker,
  type SinceAlertRow,
} from '@/lib/convergence'

/** The `/v3/since` envelope subset we consume (only the alerts section). */
interface SinceResponse {
  alerts?: { items?: SinceAlertRow[] }
}

/** Default lookback for the convergence diff (days). Bounded by the server's
 *  90-day `/v3/since` cap. */
const DEFAULT_LOOKBACK_DAYS = 7

async function fetchConvergenceMarkers(lookbackDays: number): Promise<ConvergenceMarker[]> {
  const cursor = new Date(Date.now() - lookbackDays * 86_400_000).toISOString()
  // `channel=geo_convergence` scopes the severity-ranked 50-row alerts cap to
  // this layer's channel. A registry predating the param ignores it (FastAPI
  // drops unknown query params) and returns the unscoped section — the
  // client-side channel check in `activeConvergenceMarkers` still applies, so
  // the layer degrades to whatever convergence rows survive the cap.
  const res = await apiGet<SinceResponse>(
    `/v3/since?cursor=${encodeURIComponent(cursor)}&channel=geo_convergence`,
  )
  return activeConvergenceMarkers(res.alerts?.items ?? [])
}

export interface UseConvergenceResult {
  markers: ConvergenceMarker[]
  isLoading: boolean
  isError: boolean
}

export function useConvergenceMarkers(
  lookbackDays: number = DEFAULT_LOOKBACK_DAYS,
): UseConvergenceResult {
  const q = useQuery<ConvergenceMarker[]>({
    queryKey: ['world-convergence', lookbackDays],
    queryFn: () => fetchConvergenceMarkers(lookbackDays),
    refetchInterval: 120_000,
    staleTime: 60_000,
    // A cursor 400 / transient error must never blank the map — degrade to an
    // empty marker set (the layer shows its honest empty state).
    retry: 1,
  })
  return {
    markers: q.data ?? [],
    isLoading: q.isLoading,
    isError: q.isError,
  }
}
