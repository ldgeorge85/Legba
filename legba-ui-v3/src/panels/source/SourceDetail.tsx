/**
 * UI-2 / Tier C — `source.detail`.
 *
 * Per-source operator view. Four sections:
 *   1. Descriptor body + identity/scope/output summary (GET /registry/sources/{id})
 *   2. Cursor / health — derived from the most-recent published signal for
 *      this source (GET /signals?... newest first) + the descriptor's cadence,
 *      so you see "last published / staleness vs schedule" without a dedicated
 *      health endpoint (none is frozen for P-15 yet).
 *   3. JetStream consumer roster + lag — the output stream config (subject /
 *      retention / delivery / max_age) projected from the descriptor, plus the
 *      observed publish-rate. (Live consumer-lag is a W3 ops surface; until its
 *      REST lands this shows the stream's declared shape + computed throughput.)
 *   4. Recent published signals — the fan-out's first hop, newest first.
 *
 * Source selection: panel scope (`data_query.source_id`) OR the
 * `legba:open-source-detail` cross-panel event (fired by registry.sources).
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { DescriptorView } from '@/components/DescriptorView'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet } from '@/lib/api'
import { useLiveTail } from '@/lib/useLiveTail'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import {
  signalGeo,
  unwrapFactory,
  type SignalRow,
  type SignalsPage,
  type SourceDescriptorOut,
} from './sourceTypes'

function fmtAge(ms: number): string {
  if (ms < 0) return 'in the future'
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

export default function SourceDetailPanel({ registration, scope }: PanelProps) {
  const initial =
    (registration.data_query?.source_id as string | undefined) ??
    (scope as { source_id?: string }).source_id ??
    ''
  const [sourceId, setSourceId] = useState(initial)
  const [live, setLive] = useState(true)

  // Redesign Move 2: follow the shared selection when it's a source (replaces
  // the legacy `legba:open-source-detail` window listener).
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (selection?.kind === 'source') setSourceId(selection.id)
  }, [selection])

  const enabled = sourceId.trim().length > 0

  const desc = useQuery<SourceDescriptorOut>({
    enabled,
    queryKey: ['source-detail', sourceId],
    queryFn: () => apiGet<SourceDescriptorOut>(`/registry/sources/${encodeURIComponent(sourceId)}`),
  })

  // Recent published signals for this source. `/signals?source_id=` filters
  // server-side by the signals.source_id FK (= the descriptor id), so we get
  // this source's stream directly rather than scanning the global page. We
  // still match defensively on either id field client-side in case a row
  // carries the descriptor id under descriptor_source_id only.
  const signals = useQuery<SignalsPage>({
    enabled,
    queryKey: ['source-signals', sourceId],
    queryFn: () =>
      apiGet<SignalsPage>(`/signals?source_id=${encodeURIComponent(sourceId)}&limit=100`),
    refetchInterval: 30_000,
  })

  // Live signal-tail for this source: a new published signal refetches the
  // source's recent-signals window (and therefore the derived health/fan-out).
  // Gated by the `live` toggle + an active source (inert under test — the stub
  // WS never fires). Server-side filter still narrows by source on refetch.
  const { connected } = useLiveTail(
    'legba.signals.>',
    () => {
      if (enabled) signals.refetch()
    },
    live && enabled,
  )

  const sourceSignals: SignalRow[] = useMemo(() => {
    const all = signals.data?.data ?? []
    return all.filter(
      (s) => s.descriptor_source_id === sourceId || s.source_id === sourceId,
    )
  }, [signals.data, sourceId])

  const health = useMemo(() => {
    if (sourceSignals.length === 0) return null
    const newest = sourceSignals.reduce((a, b) =>
      Date.parse(a.produced_at) > Date.parse(b.produced_at) ? a : b,
    )
    const lastMs = Date.now() - Date.parse(newest.produced_at)
    // 24h sliding throughput from the loaded window.
    const cutoff = Date.now() - 24 * 3600_000
    const last24 = sourceSignals.filter((s) => Date.parse(s.produced_at) >= cutoff).length
    return { lastMs, lastAt: newest.produced_at, count: sourceSignals.length, last24 }
  }, [sourceSignals])

  const cadenceSchedule = (() => {
    const c = desc.data?.body?.cadence as { schedule?: unknown } | undefined
    // `cadence.schedule` is a property-factory wrapper on the wire
    // ({raw, ui_hint, factory_kind}), not a bare string — unwrap to .raw.
    return unwrapFactory(c?.schedule)
  })()

  const output = (desc.data?.body?.output ?? {}) as {
    retention?: string
    delivery?: string
    max_age_seconds?: number
    max_msgs?: number
  }

  return (
    <PanelChrome
      registration={registration}
      subtitle={enabled ? sourceId : 'select a source'}
      onRefresh={
        enabled
          ? () => {
              desc.refetch()
              signals.refetch()
            }
          : undefined
      }
    >
      <div className="flex items-center gap-2 mb-3 text-xs">
        <ScopePicker
          family="source"
          value={sourceId}
          onChange={setSourceId}
          placeholder="select a source…"
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2 font-mono text-slate-200"
          testId="source-detail-id"
        />
      </div>

      {!enabled && (
        <div className="text-slate-500 text-sm py-4 text-center" data-testid="source-detail-empty">
          enter a source id, or click "open detail" from the Source Registry
        </div>
      )}

      {desc.error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {desc.error.message}</div>
      )}

      {desc.data && (
        <div className="flex-1 overflow-auto text-xs space-y-3" data-testid="source-detail-body">
          {/* identity / scope summary */}
          <section className="bg-surface-100 border border-slate-800 rounded p-2">
            <div className="flex items-baseline gap-2">
              <span className="rounded px-1 text-[10px] bg-slate-800 text-slate-300">
                {desc.data.state}
              </span>
              {desc.data.kind && (
                <span className="rounded px-1 text-[10px] bg-slate-800 text-slate-300">
                  {desc.data.kind}
                </span>
              )}
              <span className="text-slate-200 font-medium">{desc.data.name}</span>
              <span className="text-slate-600 font-mono text-[10px] ml-auto">
                @{desc.data.version.slice(0, 12)}
              </span>
            </div>
            <div className="text-slate-500 mt-1 flex gap-3 flex-wrap">
              <span>acquisition: {desc.data.acquisition}</span>
              <span>policy: {desc.data.subscription_policy ?? 'open'}</span>
              {desc.data.owner_tenant && <span>tenant: {desc.data.owner_tenant}</span>}
              {desc.data.geo.length > 0 && <span>geo: {desc.data.geo.join(', ')}</span>}
              {desc.data.languages.length > 0 && (
                <span>lang: {desc.data.languages.join(', ')}</span>
              )}
              {desc.data.tags.length > 0 && <span>tags: {desc.data.tags.join(', ')}</span>}
            </div>
          </section>

          {/* cursor / health */}
          <section className="bg-surface-100 border border-slate-800 rounded p-2" data-testid="source-detail-health">
            <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
              cursor / health
            </div>
            {health ? (
              <div className="grid grid-cols-2 gap-y-1 text-[11px]">
                <span className="text-slate-500">last published</span>
                <span className="text-slate-200" data-testid="source-detail-last">
                  {fmtAge(health.lastMs)} ({new Date(health.lastAt).toLocaleString()})
                </span>
                <span className="text-slate-500">signals (24h window)</span>
                <span className="text-slate-200">{health.last24}</span>
                <span className="text-slate-500">cadence</span>
                <span className="text-slate-200 font-mono">{cadenceSchedule ?? '(push / none)'}</span>
                <span className="text-slate-500">status</span>
                <span
                  className={
                    cadenceSchedule && health.lastMs > 6 * 3600_000
                      ? 'text-amber-400'
                      : 'text-emerald-400'
                  }
                >
                  {cadenceSchedule && health.lastMs > 6 * 3600_000 ? 'stale (>6h since publish)' : 'healthy'}
                </span>
              </div>
            ) : (
              <div className="text-slate-500">
                {signals.isLoading ? 'loading…' : 'no published signals observed in the recent window'}
              </div>
            )}
          </section>

          {/* JetStream consumer roster + output stream shape */}
          <section className="bg-surface-100 border border-slate-800 rounded p-2" data-testid="source-detail-stream">
            <div className="text-slate-400 text-[10px] uppercase tracking-wide mb-1">
              output stream + consumers
            </div>
            <div className="grid grid-cols-2 gap-y-1 text-[11px]">
              <span className="text-slate-500">subject</span>
              <span className="text-slate-200 font-mono break-all">{desc.data.output_subject}</span>
              <span className="text-slate-500">retention</span>
              <span className="text-slate-200">{output.retention ?? 'interest'}</span>
              <span className="text-slate-500">delivery</span>
              <span className="text-slate-200">{output.delivery ?? 'lossy'}</span>
              <span className="text-slate-500">max age</span>
              <span className="text-slate-200">{(output.max_age_seconds ?? 86400) / 3600}h</span>
            </div>
            <FanoutByGeo signals={sourceSignals} />
          </section>

          {/* recent published signals (fan-out hop 1) */}
          <section data-testid="source-detail-signals">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-slate-400 text-[10px] uppercase tracking-wide">
                recent published signals ({sourceSignals.length})
              </span>
              <button
                onClick={() => setLive((v) => !v)}
                className={
                  'ml-auto text-[10px] px-2 py-0.5 rounded border ' +
                  (live
                    ? connected
                      ? 'border-emerald-700 text-emerald-400'
                      : 'border-amber-700 text-amber-400'
                    : 'border-slate-700 text-slate-500')
                }
                title={
                  live
                    ? connected
                      ? 'Live signal-tail connected — click to pause'
                      : 'Live signal-tail connecting…'
                    : 'Live signal-tail paused — click to resume'
                }
                data-testid="source-detail-live"
              >
                {live ? '● live' : '○ paused'}
              </button>
            </div>
            <div className="space-y-1">
              {sourceSignals.length === 0 && (
                <div className="text-slate-500">no recent signals from this source in the loaded window</div>
              )}
              {sourceSignals.slice(0, 25).map((s) => (
                <button
                  key={s.id}
                  onClick={() => selectRow('signal', s.id, s.title ?? undefined, { origin: 'source-detail' })}
                  className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2"
                  data-testid={`source-signal-${s.id}`}
                >
                  <div className="flex items-baseline gap-2">
                    <span className="text-slate-500 shrink-0 w-10 truncate uppercase">
                      {s.geo[0] ?? signalGeo(s)?.country_iso2 ?? s.language ?? '—'}
                    </span>
                    <span className="text-slate-200 truncate flex-1">{s.title}</span>
                    <span className="text-slate-600 shrink-0">
                      {new Date(s.produced_at).toLocaleTimeString()}
                    </span>
                  </div>
                </button>
              ))}
            </div>
          </section>

          {/* raw descriptor body */}
          <details>
            <summary className="text-slate-500 text-[10px] uppercase tracking-wide cursor-pointer">
              descriptor body
            </summary>
            <DescriptorView body={desc.data.body as Record<string, unknown>} />
          </details>
        </div>
      )}
    </PanelChrome>
  )
}

/**
 * Fan-out by geo — the per-source signal distribution across the routing
 * facet (geo). Signals are target-agnostic, so there is no per-target consumer
 * column to roster off; the meaningful "who picks this up" axis is the
 * indexed `geo` facet a target's subscription matches on (target_id filters by
 * scope.geo). For each ISO code we show the volume + freshest-signal age, i.e.
 * the country slices this source feeds and how live each one is. (The
 * authoritative JetStream consumer-lag stream is a W3 ops surface.)
 */
function FanoutByGeo({ signals }: { signals: SignalRow[] }) {
  const slices = useMemo(() => {
    const byGeo = new Map<string, { count: number; newest: number }>()
    for (const s of signals) {
      const codes = s.geo.length > 0 ? s.geo : [signalGeo(s)?.country_iso2 ?? '(ungeocoded)']
      const ts = Date.parse(s.produced_at)
      for (const code of codes) {
        const prev = byGeo.get(code)
        if (prev) {
          prev.count += 1
          prev.newest = Math.max(prev.newest, ts)
        } else {
          byGeo.set(code, { count: 1, newest: ts })
        }
      }
    }
    return Array.from(byGeo.entries())
      .map(([geo, v]) => ({ geo, ...v }))
      .sort((a, b) => b.count - a.count)
  }, [signals])

  if (slices.length === 0) return null

  return (
    <div className="mt-2" data-testid="source-detail-consumers">
      <div className="text-slate-500 text-[10px] mb-1">
        fan-out by geo facet (subscription routing axis)
      </div>
      <div className="space-y-0.5">
        {slices.map((c) => (
          <div
            key={c.geo}
            className="flex items-baseline gap-2 text-[11px] bg-surface-200 rounded px-2 py-0.5"
          >
            <span className="text-slate-300 truncate flex-1 uppercase font-mono">{c.geo}</span>
            <span className="text-slate-500">{c.count} sig</span>
            <span className="text-slate-600">freshest {fmtAge(Date.now() - c.newest)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
