/**
 * T6. Target Sources (`target.sources`) — UI-3 (Tier B) rebuilt.
 *
 * There is no per-target source-handler read endpoint on the frozen surface.
 * The honest, source-first rollup is computed from the target's own signals
 * (`GET /api/v1/signals?target_id=…`) grouped by `source_id`, left-joined
 * with the source descriptor registry
 * (`GET /api/v1/registry/descriptors?family=source&head_only=true`) for the
 * friendly name + handler kind.
 *
 * v2 parity (Sources scored a per-source health table):
 *   - one row per `source_id` contributing to this target;
 *   - 24h vs total ingest counts; geocoded %; latest-ingest timestamp;
 *   - left-join: descriptor name + kind (or "unregistered" when a source_id
 *     has no head descriptor);
 *   - clicking a row fires the cross-panel selector event to filter Signals.
 *
 * The rollup math is kept pure + exported (`rollupSources`) so it can be
 * unit-tested without a DOM (see Sources.test.tsx).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import type { PanelProps } from '@/types'
import { cn } from '@/lib/cn'

interface SignalGeo {
  lat?: number
  lon?: number
  country?: string
  country_iso2?: string
}

/** Minimal signal shape the rollup needs. */
export interface SignalForRollup {
  source_id: string
  produced_at: string
  geo?: string[]
  data?: ({ geo?: SignalGeo } & Record<string, unknown>) | null
}

/** Head source descriptor (registry row), as returned by the descriptors API. */
export interface SourceDescriptor {
  descriptor_id: string
  name?: string | null
  body?: { identity?: { id?: string; kind?: string; name?: string } } & Record<
    string,
    unknown
  >
}

export interface SourceRollup {
  source_id: string
  name: string | null
  kind: string | null
  total: number
  last24h: number
  geocoded: number
  latest: string | null
}

interface Page<T> {
  data: T[]
  next_cursor: string | null
}

/** True when the signal carries a usable geocode (lat/lon or a country). */
function isGeocoded(s: SignalForRollup): boolean {
  const g = s.data?.geo
  if (g && (typeof g.lat === 'number' || g.country || g.country_iso2)) return true
  return Array.isArray(s.geo) && s.geo.length > 0
}

/**
 * Per-`source_id` rollup, left-joined with the source descriptor registry.
 * Pure — no I/O — so it unit-tests cleanly. `now` is injectable for tests.
 */
export function rollupSources(
  signals: SignalForRollup[],
  descriptors: SourceDescriptor[],
  now: number = Date.now(),
): SourceRollup[] {
  // Index descriptors by both the registry id and the emitted identity.id —
  // signals carry the latter as `source_id`. Defensive against a non-array
  // payload (the descriptors read may 404/500 → fall back to no join).
  const byId = new Map<string, SourceDescriptor>()
  for (const d of Array.isArray(descriptors) ? descriptors : []) {
    const ident = d.body?.identity?.id
    if (d.descriptor_id) byId.set(d.descriptor_id, d)
    if (ident) byId.set(ident, d)
  }

  const cutoff = now - 24 * 60 * 60 * 1000
  const acc = new Map<string, SourceRollup>()

  for (const s of Array.isArray(signals) ? signals : []) {
    let r = acc.get(s.source_id)
    if (!r) {
      const d = byId.get(s.source_id)
      r = {
        source_id: s.source_id,
        name: d?.name ?? d?.body?.identity?.name ?? null,
        kind: d?.body?.identity?.kind ?? null,
        total: 0,
        last24h: 0,
        geocoded: 0,
        latest: null,
      }
      acc.set(s.source_id, r)
    }
    r.total += 1
    const t = Date.parse(s.produced_at)
    if (!Number.isNaN(t)) {
      if (t >= cutoff) r.last24h += 1
      if (r.latest === null || s.produced_at > r.latest) r.latest = s.produced_at
    }
    if (isGeocoded(s)) r.geocoded += 1
  }

  // Busiest sources first.
  return Array.from(acc.values()).sort((a, b) => b.total - a.total)
}

function selectSource(source_id: string) {
  window.dispatchEvent(
    new CustomEvent('legba:select-source', { detail: { source_id } }),
  )
}

export default function TargetSourcesPanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id

  const signalsQ = useQuery<Page<SignalForRollup>>({
    enabled: !!target_id,
    queryKey: ['target-sources-signals', target_id],
    queryFn: async () => {
      try {
        return await apiGet<Page<SignalForRollup>>(
          `/signals?target_id=${encodeURIComponent(target_id)}&limit=500`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  // Descriptor registry — tolerant of 404/500 (left-join is optional metadata).
  const descriptorsQ = useQuery<SourceDescriptor[]>({
    queryKey: ['source-descriptors'],
    queryFn: async () => {
      try {
        return await apiGet<SourceDescriptor[]>(
          `/registry/descriptors?family=source&head_only=true`,
        )
      } catch (e) {
        if (e instanceof ApiError && (e.status === 404 || e.status >= 500)) return []
        throw e
      }
    },
    refetchInterval: 300_000,
  })

  const rollups = useMemo(
    () => rollupSources(signalsQ.data?.data ?? [], descriptorsQ.data ?? []),
    [signalsQ.data, descriptorsQ.data],
  )

  const error = signalsQ.error
  const isLoading = signalsQ.isLoading

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rollups.length} source${rollups.length === 1 ? '' : 's'} · target ${target_id}`}
      onRefresh={() => {
        signalsQ.refetch()
        descriptorsQ.refetch()
      }}
    >
      {isLoading && <div className="text-xs text-slate-400">Loading sources…</div>}
      {error && (
        <div className="text-xs text-accent-critical">
          Failed to load: {(error as Error).message}
        </div>
      )}
      {!isLoading && !error && rollups.length === 0 && (
        <div className="text-xs text-slate-400" data-testid="target-sources-empty">
          {signalsQ.isFetching ? 'Loading…' : 'No signals ingested for this target yet.'}
        </div>
      )}
      {rollups.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-slate-400 text-left border-b border-slate-700/60">
            <tr>
              <th className="py-1 pr-2">Source</th>
              <th className="py-1 pr-2 text-right">24h</th>
              <th className="py-1 pr-2 text-right">Total</th>
              <th className="py-1 pr-2 text-right">Geo%</th>
              <th className="py-1 pr-2">Latest</th>
            </tr>
          </thead>
          <tbody>
            {rollups.map((r) => (
              <SourceRowView key={r.source_id} rollup={r} />
            ))}
          </tbody>
        </table>
      )}
    </PanelChrome>
  )
}

function SourceRowView({ rollup: r }: { rollup: SourceRollup }) {
  const geoPct = r.total > 0 ? Math.round((r.geocoded / r.total) * 100) : 0
  return (
    <tr
      className="border-b border-slate-800/40 cursor-pointer hover:bg-surface-50/40"
      onClick={() => selectSource(r.source_id)}
      title={`filter signals to ${r.source_id}`}
      data-testid={`target-source-row-${r.source_id}`}
    >
      <td className="py-1 pr-2">
        <div className="truncate max-w-[16rem]">{r.name ?? r.source_id}</div>
        <div className="font-mono text-[10px] text-slate-500 truncate max-w-[16rem]">
          {r.source_id}
          {r.kind ? (
            <span className="ml-1 text-slate-600">· {r.kind}</span>
          ) : (
            <span className="ml-1 text-accent-warning">· unregistered</span>
          )}
        </div>
      </td>
      <td className="py-1 pr-2 text-right font-mono text-slate-300">{r.last24h}</td>
      <td className="py-1 pr-2 text-right font-mono text-slate-400">{r.total}</td>
      <td className="py-1 pr-2 text-right">
        <span
          className={cn(
            'font-mono',
            geoPct >= 75 ? 'text-accent-ok' : geoPct >= 25 ? 'text-accent-info' : 'text-slate-500',
          )}
          data-testid={`target-source-geo-${r.source_id}`}
        >
          {geoPct}%
        </span>
      </td>
      <td className="py-1 pr-2 font-mono text-slate-500 whitespace-nowrap">
        {r.latest ? new Date(r.latest).toLocaleString() : '—'}
      </td>
    </tr>
  )
}
