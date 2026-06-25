/**
 * S6b / UI-5 (Tier F). Runtime Actor Health (`system.actor_health`).
 *
 * Richer than the `system.runtime` table: a kind/lifecycle rollup header, the
 * NEW `source` actor kind (SourceActor) first-classed alongside
 * target/analyst/discovery/consult, per-actor last-run / last-outcome /
 * cooldown / error-count, and an expandable last-error inspector.
 *
 * Reads `GET /api/v1/v3/runtime/actors` → one `actor_state` row per actor.
 * Polls every 5s; rows colour-code by lifecycle. Rollup + relTime live in
 * `@/lib/evalOps` (unit-tested).
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import { ACTOR_KINDS, actorRollup, relTime, type ActorKindFilter } from '@/lib/evalOps'
import type { PanelProps } from '@/types'

interface ActorRow {
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
}

const LIFECYCLES = [
  'all',
  'active',
  'paused',
  'error',
  'retired',
  'configured',
  'draft',
] as const

const LIFECYCLE_PILL: Record<string, string> = {
  active: 'bg-emerald-900 text-emerald-200',
  paused: 'bg-amber-900 text-amber-200',
  error: 'bg-rose-900 text-rose-200',
  retired: 'bg-slate-800 text-slate-400',
  configured: 'bg-sky-900 text-sky-200',
  draft: 'bg-slate-700 text-slate-300',
}

const KIND_PILL: Record<string, string> = {
  target: 'bg-indigo-900 text-indigo-200',
  analyst: 'bg-violet-900 text-violet-200',
  discovery: 'bg-cyan-900 text-cyan-200',
  source: 'bg-teal-900 text-teal-200',
  consult: 'bg-fuchsia-900 text-fuchsia-200',
}

export default function ActorHealthPanel({ registration }: PanelProps) {
  const [kindFilter, setKindFilter] = useState<ActorKindFilter>('all')
  const [lcFilter, setLcFilter] = useState<(typeof LIFECYCLES)[number]>('all')
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery<ActorRow[]>({
    queryKey: ['actor-health'],
    queryFn: () => apiGet<ActorRow[]>('/v3/runtime/actors'),
    refetchInterval: 5_000,
  })

  const rows = data ?? []
  const rollup = useMemo(() => actorRollup(rows), [rows])

  const filtered = useMemo(() => {
    return rows.filter((r) => {
      if (kindFilter !== 'all' && r.actor_kind !== kindFilter) return false
      if (lcFilter !== 'all' && r.lifecycle !== lcFilter) return false
      if (query && !r.actor_id.toLowerCase().includes(query.toLowerCase())) return false
      return true
    })
  }, [rows, kindFilter, lcFilter, query])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${rows.length} actors · ${rollup.stale} in error · ${rollup.errors} total errors`}
      onRefresh={() => refetch()}
    >
      {/* kind rollup chips */}
      <div className="flex flex-wrap gap-1 mb-2 text-[10px]" data-testid="actor-rollup">
        {Object.entries(rollup.byKind)
          .sort((a, b) => b[1] - a[1])
          .map(([kind, n]) => (
            <button
              key={kind}
              onClick={() =>
                setKindFilter(kindFilter === kind ? 'all' : (kind as ActorKindFilter))
              }
              className={`rounded px-1.5 py-0.5 border ${
                kindFilter === kind ? 'border-accent-info' : 'border-slate-700'
              } ${KIND_PILL[kind] ?? 'bg-slate-800 text-slate-300'}`}
              data-testid={`actor-rollup-${kind}`}
            >
              {kind}: {n}
            </button>
          ))}
      </div>

      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <input
          className="flex-1 min-w-[150px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="filter by actor_id…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="actor-query"
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value as ActorKindFilter)}
          data-testid="actor-kind-filter"
        >
          {ACTOR_KINDS.map((k) => (
            <option key={k} value={k}>
              kind: {k}
            </option>
          ))}
        </select>
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={lcFilter}
          onChange={(e) => setLcFilter(e.target.value as (typeof LIFECYCLES)[number])}
          data-testid="actor-lifecycle-filter"
        >
          {LIFECYCLES.map((l) => (
            <option key={l} value={l}>
              lifecycle: {l}
            </option>
          ))}
        </select>
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1" data-testid="actor-list">
        {!isLoading && filtered.length === 0 && (
          <div className="text-slate-500 text-center py-4">no actors match</div>
        )}
        {filtered.map((r) => {
          const open = expanded === r.actor_id
          const hasError = r.error_count > 0 || r.lifecycle === 'error'
          return (
            <div
              key={r.actor_id}
              className={`bg-surface-100 border rounded p-2 ${
                hasError ? 'border-rose-900/40' : 'border-slate-800'
              }`}
              data-testid={`actor-row-${r.actor_id}`}
            >
              <button
                className="w-full text-left"
                onClick={() => setExpanded(open ? null : r.actor_id)}
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${
                      KIND_PILL[r.actor_kind] ?? 'bg-slate-800 text-slate-300'
                    }`}
                    data-testid={`actor-kind-pill-${r.actor_kind}`}
                  >
                    {r.actor_kind}
                  </span>
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${
                      LIFECYCLE_PILL[r.lifecycle] ?? 'bg-slate-700 text-slate-300'
                    }`}
                  >
                    {r.lifecycle}
                  </span>
                  <span className="font-mono text-slate-300 truncate flex-1">{r.actor_id}</span>
                  {r.error_count > 0 && (
                    <span className="text-rose-400 font-mono shrink-0">
                      {r.error_count} err
                    </span>
                  )}
                  <span className="text-slate-500 shrink-0">{relTime(r.last_run_at)}</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-slate-500">
                  <span>outcome: {r.last_outcome ?? '—'}</span>
                  {r.cooldown_until && (
                    <span className="text-amber-400">cooldown {relTime(r.cooldown_until)}</span>
                  )}
                </div>
              </button>
              {open && (
                <div className="mt-2 border-t border-slate-800 pt-2 space-y-1.5 text-[10px]">
                  <div className="text-slate-500 font-mono break-all">
                    {r.descriptor_id}@{r.descriptor_version.slice(0, 8)}
                  </div>
                  <div className="text-slate-500">updated {relTime(r.updated_at)}</div>
                  {r.last_error && (
                    <div>
                      <div className="text-slate-500 uppercase tracking-wide mb-1">last error</div>
                      <pre className="bg-rose-950/40 border border-rose-900/50 p-2 rounded overflow-x-auto text-rose-200 max-h-40 whitespace-pre-wrap">
                        {r.last_error}
                      </pre>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
