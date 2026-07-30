/**
 * P-7. Target Detail (`target.overview`).
 *
 * The comprehensive per-target home: descriptor metadata, runtime actor
 * roster (source cursors + last_pulled_at + error counts), recent
 * signals, recent findings.
 *
 * Reads three endpoints concurrently:
 *  - GET /api/v1/targets/{id}/runtime  — descriptor + actor_state + sources
 *  - GET /api/v1/signals?target_id=&limit=20
 *  - GET /api/v1/findings?target_id=&limit=20
 *
 * Row clicks dispatch `legba:open-lineage` so the Lineage panel picks
 * up the deep-link.
 */

import { useQuery } from '@tanstack/react-query'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import type { PanelProps } from '@/types'
import { selectRow } from '@/state/selection'
import { humanizeAnalystId } from '@/lib/analystNames'

interface TargetRuntimeResponse {
  descriptor_id: string
  active_descriptor: {
    descriptor_id: string
    version: string
    schema_uri: string
    state: string
    name: string
    abstraction_level: string | null
    source_count: number
  } | null
  actors: Array<{
    actor_id: string
    actor_kind: string
    descriptor_id: string
    descriptor_version: string
    lifecycle: string
    last_run_at: string | null
    last_outcome: string | null
    cooldown_until: string | null
    error_count: number
    last_error: string | null
    updated_at: string
    sources: Array<{
      source_id: string
      last_pulled_at: string | null
      rows_pulled: number
      last_error: string | null
    }>
  }>
}

interface SignalRow {
  id: string
  title: string
  category: string
  language: string
  confidence: number
  source_url: string
  produced_at: string
}

interface FindingRow {
  id: string
  title: string
  body: string
  confidence: number | null
  severity: string | null
  analyst_id: string | null
  produced_at: string
}

function openLineage(rowKind: string, rowId: string, title?: string) {
  // Redesign Move 2: unified selection store → opens the Inspector + brushes
  // every room (was a legacy window event firing into the void).
  selectRow(rowKind, rowId, title, { origin: 'target-overview' })
}

export default function TargetOverviewPanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id

  const runtimeQ = useQuery<TargetRuntimeResponse | null>({
    enabled: !!target_id,
    queryKey: ['target-runtime', target_id],
    queryFn: async () => {
      try {
        return await apiGet<TargetRuntimeResponse>(
          `/targets/${encodeURIComponent(target_id)}/runtime`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null
        throw e
      }
    },
    refetchInterval: 30_000,
  })

  const signalsQ = useQuery<{ data: SignalRow[] }>({
    enabled: !!target_id,
    queryKey: ['target-signals', target_id],
    queryFn: () =>
      apiGet<{ data: SignalRow[] }>(`/signals?target_id=${encodeURIComponent(target_id)}&limit=20`),
    refetchInterval: 30_000,
  })

  const findingsQ = useQuery<{ data: FindingRow[] }>({
    enabled: !!target_id,
    queryKey: ['target-findings', target_id],
    queryFn: () =>
      apiGet<{ data: FindingRow[] }>(`/findings?target_id=${encodeURIComponent(target_id)}&limit=20`),
    refetchInterval: 30_000,
  })

  const desc = runtimeQ.data?.active_descriptor
  const actors = runtimeQ.data?.actors ?? []
  const signals = signalsQ.data?.data ?? []
  const findings = findingsQ.data?.data ?? []

  return (
    <PanelChrome
      registration={registration}
      subtitle={`target ${target_id}`}
      onRefresh={() => {
        runtimeQ.refetch()
        signalsQ.refetch()
        findingsQ.refetch()
      }}
    >
      <div className="flex-1 overflow-auto space-y-3 text-xs">
        {/* Descriptor block */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">descriptor</div>
          {desc ? (
            <div className="bg-surface-100 border border-slate-800 rounded p-2 grid grid-cols-2 gap-x-3 gap-y-1">
              <span className="text-slate-500">name</span>
              <span className="text-slate-200">{desc.name}</span>
              <span className="text-slate-500">state</span>
              <span
                className={
                  desc.state === 'active' ? 'text-emerald-400' : 'text-amber-400'
                }
              >
                {desc.state}
              </span>
              <span className="text-slate-500">level</span>
              <span>{desc.abstraction_level ?? '—'}</span>
              <span className="text-slate-500">sources</span>
              <span>{desc.source_count}</span>
              <span className="text-slate-500">version</span>
              <span className="font-mono text-[10px] text-slate-400 truncate">
                {desc.version.slice(0, 16)}
              </span>
            </div>
          ) : (
            <div className="text-slate-500">no descriptor row</div>
          )}
        </section>

        {/* Actor + source cursor block */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            actors ({actors.length})
          </div>
          {actors.map((a) => (
            <div
              key={a.actor_id}
              className="bg-surface-100 border border-slate-800 rounded p-2 mb-1"
            >
              <div className="flex items-baseline gap-3 mb-1">
                <span
                  className={`shrink-0 rounded px-1 text-[10px] ${
                    a.lifecycle === 'active'
                      ? 'bg-emerald-900 text-emerald-200'
                      : 'bg-slate-700 text-slate-200'
                  }`}
                >
                  {a.lifecycle}
                </span>
                <span className="font-mono text-[10px] text-slate-400 truncate flex-1">
                  {a.actor_id}
                </span>
                {a.error_count > 0 && (
                  <span className="text-rose-400">errors: {a.error_count}</span>
                )}
              </div>
              <div className="text-slate-500 flex gap-3">
                <span>last_run: {a.last_run_at ? new Date(a.last_run_at).toLocaleString() : 'never'}</span>
                {a.last_outcome && <span>outcome: {a.last_outcome}</span>}
              </div>
              {a.last_error && (
                <div className="text-rose-400 mt-1 truncate">last err: {a.last_error}</div>
              )}
              {a.sources.length > 0 && (
                <div className="mt-1 space-y-0.5">
                  {a.sources.map((s) => (
                    <div key={s.source_id} className="flex gap-2">
                      <span className="text-slate-500 w-32 shrink-0 truncate">{s.source_id}</span>
                      <span className="text-slate-600">
                        last:{' '}
                        {s.last_pulled_at ? new Date(s.last_pulled_at).toLocaleString() : 'never'}
                      </span>
                      <span className="text-slate-600">rows: {s.rows_pulled}</span>
                      {s.last_error && (
                        <span className="text-rose-400 truncate">err: {s.last_error}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          {actors.length === 0 && !runtimeQ.isLoading && (
            <div className="text-slate-500">no active actors</div>
          )}
        </section>

        {/* Recent signals */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            recent signals ({signals.length})
          </div>
          <div className="space-y-1">
            {signals.map((s) => (
              <button
                key={s.id}
                onClick={() => openLineage('signal', s.id, s.title)}
                className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2"
              >
                <div className="flex items-baseline gap-2">
                  <span className="text-slate-500 shrink-0 w-16">{s.category}</span>
                  <span className="text-slate-200 truncate flex-1">{s.title}</span>
                  <span className="text-slate-600 shrink-0">{s.language}</span>
                </div>
                <div className="text-slate-600 text-[10px] mt-0.5">
                  {new Date(s.produced_at).toLocaleString()}
                </div>
              </button>
            ))}
            {signals.length === 0 && !signalsQ.isLoading && (
              <div className="text-slate-500">no signals</div>
            )}
          </div>
        </section>

        {/* Recent findings */}
        <section>
          <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
            recent findings ({findings.length})
          </div>
          <div className="space-y-1">
            {findings.map((f) => (
              <button
                key={f.id}
                onClick={() => openLineage('finding', f.id, f.title)}
                className="w-full text-left bg-surface-100 hover:bg-surface-200 border border-slate-800 rounded p-2"
              >
                <div className="text-slate-200 font-medium">{f.title}</div>
                {f.body && (
                  <div className="text-slate-400 line-clamp-2 mt-1">{f.body}</div>
                )}
                <div className="text-slate-600 mt-1 flex gap-2">
                  {f.analyst_id && <span title={f.analyst_id}>{humanizeAnalystId(f.analyst_id)}</span>}
                  {f.confidence !== null && <span>c={f.confidence.toFixed(2)}</span>}
                  <span>{new Date(f.produced_at).toLocaleString()}</span>
                </div>
              </button>
            ))}
            {findings.length === 0 && !findingsQ.isLoading && (
              <div className="text-slate-500">no findings</div>
            )}
          </div>
        </section>
      </div>
    </PanelChrome>
  )
}
