/**
 * The World — rails · KPI strip.
 *
 * A horizontal row of compact stat cards over the substrate read API.
 *
 * WEIGHT (W11-F262, U-8). A count needs rows, never bodies.
 *
 * WHAT THE "24h" CARDS COUNT. "Signals 24h" and "Findings 24h"
 * used to print the length of the newest-500 page of `/signals` and
 * `/findings` — no time bound, so on any real day both read "500+" and the
 * label was not true. They now read `GET /v3/counts?window_hours=24`
 * (registry/window_counts_api.py): two server-side `count(*)` over the same
 * filters the list routes apply (canonical signals by `fetched_at`; rows of
 * kind `finding` by `produced_at`), one ~100-byte answer. The map's live
 * store counts are counts of PLACED marks, not of a day, so these two cards
 * no longer prefer them. A registry without the route (404) leaves the card
 * unread ("—"), never a number that is not a 24-hour count.
 *   - situations: the route's own `state` filter, per active state;
 *   - sources: `/registry/sources?state=active` — the route's own state
 *     filter, so the client no longer downloads paused/retired rows to drop.
 */
import { useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet, ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import { SOURCE_ROSTER_ROUTE } from '@/lib/deskNames'

/**
 * Track the last non-trivial change in a numeric KPI across polls — the "which
 * desks changed" delta the mission-control strip surfaces. Returns the delta
 * from the previous DISTINCT value (persists until the value next changes), so
 * the arrow reads "moved by +N since the last update" rather than flickering to
 * zero on every re-render.
 */
function useDelta(value: number | null): number | null {
  const ref = useRef<{ prev: number | null; delta: number | null }>({ prev: null, delta: null })
  if (value != null && value !== ref.current.prev) {
    if (ref.current.prev != null) ref.current.delta = value - ref.current.prev
    ref.current.prev = value
  }
  return ref.current.delta
}

/** Page cap for the situation reads (the route pages; a `next_cursor` = `+`). */
const PAGE_LIMIT = 500

/** The window the two "24h" cards count over, in hours. */
export const KPI_WINDOW_HOURS = 24

/** The strip's reads, exported for the weight guard test (W11-F262, W11-F271). */
export const KPI_READS = {
  counts: `/v3/counts?window_hours=${KPI_WINDOW_HOURS}`,
  // RA-1: the reader's source roster (the card counts `state === 'active'`)
  sources: SOURCE_ROSTER_ROUTE,
} as const

/** `GET /v3/counts` (registry/window_counts_api.py). */
export interface WindowCounts {
  window_hours: number
  since: string
  signals: number
  findings: number
}

/** The window counts; `null` when the route is absent (404) or answers without
 *  both numbers: unknown, never 0. */
async function getWindowCounts(): Promise<WindowCounts | null> {
  try {
    const res = await apiGet<Partial<WindowCounts> | null>(KPI_READS.counts)
    const ok = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n)
    return res && ok(res.signals) && ok(res.findings) ? (res as WindowCounts) : null
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

/** How often the strip re-reads its counts (a count strip, not a feed). */
export const KPI_REFETCH_MS = 60_000

/** The situation states the strip counts as "active" — the route's own
 *  `state` filter values (`substrate_reads_api.SituationState`). */
const ACTIVE_SITUATION_STATES = ['active', 'escalating'] as const

/** One `/situations` page — the route PAGES (`{data, next_cursor}`); it is not
 *  a plain list. */
interface SituationsPage {
  data: { id: string }[]
  next_cursor?: string | null
}

/**
 * The active-situation count, read with the route's own `state` filter.
 *
 * C2 — this used to read `/situations` as a plain LIST and filter a `state`
 * field the rows do not carry (they carry `status`), so the tile said "0
 * active situations" over a register with active rows. Now each active state
 * is asked for by name; a page with a `next_cursor` is a lower bound (`+`).
 * `null` when the route is absent (404): unknown, never 0.
 */
async function getActiveSituations(): Promise<{ n: number; capped: boolean } | null> {
  try {
    const pages = await Promise.all(
      ACTIVE_SITUATION_STATES.map((s) =>
        apiGet<SituationsPage>(`/situations?state=${s}&limit=${PAGE_LIMIT}`),
      ),
    )
    return {
      n: pages.reduce((acc, p) => acc + (p.data ?? []).length, 0),
      capped: pages.some((p) => !!p.next_cursor),
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

interface SourceRow {
  descriptor_id: string
  state: string
  name: string
}

async function getList<T>(path: string): Promise<T[]> {
  try {
    const res = await apiGet<T[]>(path)
    return Array.isArray(res) ? res : []
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return []
    throw e
  }
}

export default function KpiStrip() {
  const counts = useQuery({
    queryKey: ['world-kpi', 'counts', KPI_WINDOW_HOURS],
    queryFn: getWindowCounts,
    refetchInterval: KPI_REFETCH_MS,
  })

  const situations = useQuery({
    queryKey: ['world-kpi', 'situations', 'active'],
    queryFn: getActiveSituations,
    refetchInterval: KPI_REFETCH_MS,
  })

  const sources = useQuery({
    queryKey: ['world-kpi', 'sources'],
    queryFn: () => getList<SourceRow>(KPI_READS.sources),
    refetchInterval: KPI_REFETCH_MS,
  })

  const windowCounts = counts.data ?? null
  const signalsValue = windowCounts ? exact(windowCounts.signals) : LOADING
  const findingsValue = windowCounts ? exact(windowCounts.findings) : LOADING

  const activeSituations = situations.data ?? null
  const activeSources = (sources.data ?? []).filter((s) => s.state === 'active').length

  // Numeric current values for the band-change deltas (null while loading).
  const signalsNum = windowCounts ? windowCounts.signals : null
  const findingsNum = windowCounts ? windowCounts.findings : null
  const situationsNum = activeSituations ? activeSituations.n : null
  const sourcesNum = sources.isLoading || sources.isError ? null : activeSources

  const signalsDelta = useDelta(signalsNum)
  const findingsDelta = useDelta(findingsNum)
  const situationsDelta = useDelta(situationsNum)
  const sourcesDelta = useDelta(sourcesNum)

  return (
    <div
      className="flex shrink-0 items-stretch gap-2 border-b border-slate-800 bg-surface-300 px-3 py-2"
      data-testid="world-kpi-strip"
    >
      <KpiCard label={`Signals ${KPI_WINDOW_HOURS}h`} value={signalsValue} delta={signalsDelta} loading={counts.isLoading} />
      <KpiCard label={`Findings ${KPI_WINDOW_HOURS}h`} value={findingsValue} delta={findingsDelta} loading={counts.isLoading} />
      <KpiCard
        label="Active situations"
        value={
          !activeSituations
            ? LOADING
            : activeSituations.capped
              ? { text: `${activeSituations.n.toLocaleString()}+`, capped: true }
              : exact(activeSituations.n)
        }
        delta={situationsDelta}
        loading={situations.isLoading}
      />
      <KpiCard
        label="Sources active"
        // a failed read is '—', never a false zero (RA-1: a refused read drew 0)
        value={sources.isLoading || sources.isError ? LOADING : exact(activeSources)}
        delta={sourcesDelta}
        loading={sources.isLoading}
      />
    </div>
  )
}

/** A KPI's display text plus whether it's an honest page-cap, not a live count. */
interface KpiValue {
  text: string
  /** True when `text` is `N+` — a page cap (the query limit), never a real
   *  total. `KpiCard` surfaces this via a tooltip so the "+" reads as what it
   *  is instead of looking like a suspiciously round count. */
  capped: boolean
}

const LOADING: KpiValue = { text: '—', capped: false }

function exact(n: number): KpiValue {
  return { text: n.toLocaleString(), capped: false }
}

function KpiCard({
  label,
  value,
  delta,
  loading,
}: {
  label: string
  value: KpiValue
  delta?: number | null
  loading: boolean
}) {
  return (
    <div
      className="flex min-w-[7rem] flex-1 flex-col rounded border border-slate-800 bg-surface-200 px-3 py-2"
      data-testid="world-kpi-card"
    >
      <div className="flex items-baseline gap-1.5">
        <span
          className={cn(
            'text-2xl font-semibold tabular-nums leading-none text-slate-100',
            loading && 'text-slate-500',
            value.capped && 'cursor-help decoration-dotted decoration-slate-500 underline underline-offset-4',
          )}
          title={
            value.capped
              ? `${value.text} is a display cap (the query limit) — the true count is higher, not a live total`
              : undefined
          }
          data-testid={value.capped ? 'world-kpi-capped' : undefined}
        >
          {value.text}
        </span>
        {delta != null && delta !== 0 && (
          <span
            className={cn(
              'text-label font-medium tabular-nums leading-none',
              delta > 0 ? 'text-accent-ok' : 'text-accent-warning',
            )}
            title={`${delta > 0 ? '+' : ''}${delta} since last update`}
            data-testid="world-kpi-delta"
          >
            {delta > 0 ? '▲' : '▼'}
            {Math.abs(delta).toLocaleString()}
          </span>
        )}
      </div>
      <span className="mt-1 text-label uppercase tracking-wide text-ink-3">
        {label}
      </span>
    </div>
  )
}
