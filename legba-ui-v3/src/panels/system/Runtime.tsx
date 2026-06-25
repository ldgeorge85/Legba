/**
 * S6. Runtime Actor Health (`system.runtime`).
 *
 * Reads `GET /api/v1/v3/runtime/actors` — actor_state roster (lifecycle
 * + last_run_at + last_outcome + cooldown_until + error_count +
 * last_error per actor).  Polls every 5s; rows colour-code by
 * lifecycle.  Filter by kind / lifecycle / id substring.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import { ACTOR_KINDS, relTime, type ActorKindFilter } from '@/lib/evalOps'
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

const LIFECYCLES = ['all', 'active', 'paused', 'error', 'retired', 'configured', 'draft'] as const

const LIFECYCLE_PILL: Record<string, string> = {
  active: 'bg-emerald-900 text-emerald-200',
  paused: 'bg-amber-900 text-amber-200',
  error: 'bg-rose-900 text-rose-200',
  retired: 'bg-slate-800 text-slate-400',
  configured: 'bg-sky-900 text-sky-200',
  draft: 'bg-slate-700 text-slate-300',
}

export default function RuntimePanel({ registration }: PanelProps) {
  const [kindFilter, setKindFilter] = useState<ActorKindFilter>('all')
  const [lcFilter, setLcFilter] = useState<(typeof LIFECYCLES)[number]>('all')
  const [query, setQuery] = useState('')

  const { data, isLoading, error, refetch } = useQuery<ActorRow[]>({
    queryKey: ['runtime-actors'],
    queryFn: () => apiGet<ActorRow[]>('/v3/runtime/actors'),
    refetchInterval: 5_000,
  })

  const filtered = useMemo(() => {
    return (data ?? []).filter((r) => {
      if (kindFilter !== 'all' && r.actor_kind !== kindFilter) return false
      if (lcFilter !== 'all' && r.lifecycle !== lcFilter) return false
      if (query && !r.actor_id.toLowerCase().includes(query.toLowerCase())) return false
      return true
    })
  }, [data, kindFilter, lcFilter, query])

  const counts = useMemo(() => {
    const rows = data ?? []
    return {
      total: rows.length,
      active: rows.filter((r) => r.lifecycle === 'active').length,
      errors: rows.reduce((a, r) => a + r.error_count, 0),
    }
  }, [data])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${counts.active}/${counts.total} active · ${counts.errors} errors`}
      onRefresh={() => refetch()}
    >
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <input
          className="flex-1 min-w-[150px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="filter by actor_id…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value as ActorKindFilter)}
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

      <div className="flex-1 overflow-auto text-xs">
        {filtered.length === 0 && !isLoading && (
          <div className="text-slate-500 text-center py-4">no actors match</div>
        )}
        <table className="w-full">
          <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800 sticky top-0 bg-surface-100">
            <tr>
              <th className="py-1 px-1 text-left">actor</th>
              <th className="py-1 px-1 text-left">kind</th>
              <th className="py-1 px-1 text-left">lifecycle</th>
              <th className="py-1 px-1 text-left">last_run</th>
              <th className="py-1 px-1 text-left">outcome</th>
              <th className="py-1 px-1 text-right">errs</th>
              <th className="py-1 px-1 text-left">cooldown</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.actor_id} className="border-b border-slate-800/40 hover:bg-surface-100">
                <td className="py-1 px-1 font-mono text-slate-300 truncate max-w-[280px]">{r.actor_id}</td>
                <td className="py-1 px-1 text-slate-500">{r.actor_kind}</td>
                <td className="py-1 px-1">
                  <span className={`rounded px-1 text-[10px] ${LIFECYCLE_PILL[r.lifecycle] ?? 'bg-slate-700 text-slate-300'}`}>
                    {r.lifecycle}
                  </span>
                </td>
                <td className="py-1 px-1 text-slate-500">{relTime(r.last_run_at)}</td>
                <td className="py-1 px-1 text-slate-400">{r.last_outcome ?? '—'}</td>
                <td className={`py-1 px-1 text-right font-mono ${r.error_count > 0 ? 'text-rose-400' : 'text-slate-500'}`}>
                  {r.error_count}
                </td>
                <td className="py-1 px-1 text-slate-600 text-[10px]">
                  {r.cooldown_until ? relTime(r.cooldown_until) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </PanelChrome>
  )
}
