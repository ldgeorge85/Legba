/**
 * The World — rails · KPI strip.
 *
 * A horizontal row of compact stat cards over the substrate read API. Counts
 * are intentionally coarse (the substrate has no count endpoint): we read a
 * capped page and show its length, suffixing `+` when the page is saturated so
 * the operator knows the true figure is at least that. Where the shared world
 * store already carries a live count (set by the map/feed) we prefer it.
 */
import { useQuery } from '@tanstack/react-query'
import { apiGet, ApiError } from '@/lib/api'
import { useWorldState } from './worldState'
import { cn } from '@/lib/cn'

/** Page cap used for the "length-or-more" counts. */
const PAGE_LIMIT = 500

interface Page<T> {
  data: T[]
}

interface SituationRow {
  id: string
  title: string
  state: 'active' | 'escalating' | 'resolved'
}

interface SourceRow {
  descriptor_id: string
  state: string
  name: string
}

/** Tolerant fetch: 404 (endpoint absent in a slim deploy) → empty page. */
async function getPage<T>(path: string): Promise<T[]> {
  try {
    const res = await apiGet<Page<T>>(path)
    return res.data ?? []
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return []
    throw e
  }
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
  // Live counts the map/feed may already have pushed into the world store.
  const liveSignalCount = useWorldState((s) => s.counts.signals)
  const liveFindingCount = useWorldState((s) => s.counts.findings)

  // Always fetch a fallback count — the map's live count can legitimately be 0
  // (e.g. findings the map couldn't place), and a real page count is better.
  const signals = useQuery({
    queryKey: ['world-kpi', 'signals'],
    queryFn: () => getPage<{ id: string }>(`/signals?limit=${PAGE_LIMIT}`),
    refetchInterval: 30_000,
  })

  const findings = useQuery({
    queryKey: ['world-kpi', 'findings'],
    queryFn: () => getPage<{ id: string }>(`/findings?limit=${PAGE_LIMIT}`),
    refetchInterval: 30_000,
  })

  const situations = useQuery({
    queryKey: ['world-kpi', 'situations'],
    queryFn: () => getList<SituationRow>('/situations'),
    refetchInterval: 30_000,
  })

  const sources = useQuery({
    queryKey: ['world-kpi', 'sources'],
    queryFn: () => getList<SourceRow>('/registry/sources'),
    refetchInterval: 30_000,
  })

  const signalsValue =
    liveSignalCount != null && liveSignalCount > 0
      ? formatExact(liveSignalCount)
      : formatPage(signals.data, signals.isLoading)
  const findingsValue =
    liveFindingCount != null && liveFindingCount > 0
      ? formatExact(liveFindingCount)
      : formatPage(findings.data, findings.isLoading)

  const activeSituations = (situations.data ?? []).filter(
    (s) => s.state === 'active' || s.state === 'escalating',
  ).length
  const activeSources = (sources.data ?? []).filter((s) => s.state === 'active').length

  return (
    <div
      className="flex shrink-0 items-stretch gap-2 border-b border-slate-800 bg-surface-300 px-3 py-2"
      data-testid="world-kpi-strip"
    >
      <KpiCard label="Signals 24h" value={signalsValue} loading={signalLoading(signals.isLoading, liveSignalCount)} />
      <KpiCard label="Findings 24h" value={findingsValue} loading={signalLoading(findings.isLoading, liveFindingCount)} />
      <KpiCard
        label="Active situations"
        value={situations.isLoading ? '—' : formatExact(activeSituations)}
        loading={situations.isLoading}
      />
      <KpiCard
        label="Sources active"
        value={sources.isLoading ? '—' : formatExact(activeSources)}
        loading={sources.isLoading}
      />
    </div>
  )
}

function signalLoading(isLoading: boolean, live: number | undefined): boolean {
  return live == null && isLoading
}

function formatExact(n: number): string {
  return n.toLocaleString()
}

/** Page length, suffixed `+` when the page is saturated (true count is higher). */
function formatPage(rows: { id: string }[] | undefined, loading: boolean): string {
  if (loading || rows == null) return '—'
  const n = rows.length
  return n >= PAGE_LIMIT ? `${n.toLocaleString()}+` : n.toLocaleString()
}

function KpiCard({
  label,
  value,
  loading,
}: {
  label: string
  value: string
  loading: boolean
}) {
  return (
    <div
      className="flex min-w-[7rem] flex-1 flex-col rounded border border-slate-800 bg-surface-200 px-3 py-2"
      data-testid="world-kpi-card"
    >
      <span
        className={cn(
          'text-2xl font-semibold tabular-nums leading-none text-slate-100',
          loading && 'text-slate-500',
        )}
      >
        {value}
      </span>
      <span className="mt-1 text-[10px] uppercase tracking-wide text-slate-500">
        {label}
      </span>
    </div>
  )
}
