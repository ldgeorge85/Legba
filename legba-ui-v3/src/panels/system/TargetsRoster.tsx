/**
 * D1 / UI-5 (Tier F). Target Roster (`system.targets.roster`) — landing surface.
 *
 * Per L-092 §3.3 D1 — browse / search every registered target, with the
 * source-first scope surfaced inline (geo countries, domain, languages, the
 * per-target source/entity-class footprint) so the roster reads as an ops
 * inventory rather than a raw id list.
 *
 * Source of truth is the registry list endpoint:
 * `GET /api/v1/registry/descriptors?family=target&head_only=true`. The row
 * carries `name` / `abstraction_level` / `kind` and a `body.scope` block
 * ({geo, domain, tags, languages, entity_classes, …}); the panel pivots that.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'

interface TargetScope {
  geo?: string[]
  domain?: string | null
  tags?: string[]
  languages?: string[]
  entity_classes?: string[]
  time_horizon_days?: number | null
}

interface DescriptorRow {
  descriptor_id: string
  version: string
  state: string
  owner: string
  name: string | null
  family: string
  abstraction_level: string | null
  kind: string | null
  body: {
    scope?: TargetScope
    sources?: unknown[]
    analyst?: unknown[]
    [k: string]: unknown
  }
}

function scopeOf(row: DescriptorRow): TargetScope {
  return row.body?.scope ?? {}
}

function arrLen(v: unknown): number {
  return Array.isArray(v) ? v.length : 0
}

export default function TargetsRosterPanel({ registration }: PanelProps) {
  const [query, setQuery] = useState('')
  const [state, setState] = useState<string>('all')
  const [expanded, setExpanded] = useState<string | null>(null)

  const { data, error, isLoading, refetch } = useQuery<DescriptorRow[]>({
    queryKey: ['targets-roster'],
    queryFn: () =>
      apiGet<DescriptorRow[]>(
        '/registry/descriptors?family=target&head_only=true&limit=500',
      ),
    refetchInterval: 30_000,
  })

  const filtered = useMemo(() => {
    if (!data) return []
    const q = query.toLowerCase()
    return data.filter((row) => {
      if (state !== 'all' && row.state !== state) return false
      if (!q) return true
      const geo = (scopeOf(row).geo ?? []).join(' ')
      return (
        row.descriptor_id.toLowerCase().includes(q) ||
        (row.name ?? '').toLowerCase().includes(q) ||
        geo.toLowerCase().includes(q)
      )
    })
  }, [data, query, state])

  const byState = useMemo(() => {
    const m: Record<string, number> = {}
    for (const r of data ?? []) m[r.state] = (m[r.state] ?? 0) + 1
    return m
  }, [data])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${data?.length ?? 0} targets · ${filtered.length} shown`}
      onRefresh={() => refetch()}
    >
      {/* state rollup chips */}
      {Object.keys(byState).length > 0 && (
        <div className="flex flex-wrap gap-1 mb-2 text-[10px]" data-testid="roster-rollup">
          {Object.entries(byState)
            .sort((a, b) => b[1] - a[1])
            .map(([st, n]) => (
              <button
                key={st}
                onClick={() => setState(state === st ? 'all' : st)}
                className={`rounded px-1.5 py-0.5 border ${
                  state === st ? 'border-accent-info' : 'border-slate-700'
                }`}
                data-testid={`roster-rollup-${st}`}
              >
                <StateBadge state={st} /> {n}
              </button>
            ))}
        </div>
      )}

      <div className="flex items-center gap-2 mb-2 text-xs">
        <input
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="filter by id / name / country…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="roster-query"
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={state}
          onChange={(e) => setState(e.target.value)}
        >
          <option value="all">all states</option>
          <option value="draft">draft</option>
          <option value="configured">configured</option>
          <option value="active">active</option>
          <option value="paused">paused</option>
          <option value="retired">retired</option>
        </select>
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {error && (
        <div className="text-xs text-accent-critical">{(error as Error).message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1" data-testid="roster-rows">
        {!isLoading && filtered.length === 0 && (
          <div className="text-slate-400 text-center py-4">No targets match the filter.</div>
        )}
        {filtered.map((row) => {
          const scope = scopeOf(row)
          const open = expanded === row.descriptor_id
          const geo = scope.geo ?? []
          return (
            <div
              key={`${row.descriptor_id}:${row.version}`}
              className="bg-surface-100 border border-slate-800 rounded p-2"
              data-testid={`roster-row-${row.descriptor_id}`}
            >
              <button
                className="w-full text-left"
                onClick={() => setExpanded(open ? null : row.descriptor_id)}
              >
                <div className="flex items-baseline gap-2">
                  <StateBadge state={row.state} />
                  <span className="font-mono text-slate-300 truncate">{row.descriptor_id}</span>
                  {row.name && (
                    <span className="text-slate-500 truncate">{row.name}</span>
                  )}
                  {row.abstraction_level && (
                    <span className="text-slate-600 shrink-0">{row.abstraction_level}</span>
                  )}
                  <span className="ml-auto text-slate-600 font-mono shrink-0">
                    {row.version.slice(0, 8)}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {scope.domain && (
                    <span className="rounded px-1 text-[10px] bg-slate-800 text-slate-300">
                      {scope.domain}
                    </span>
                  )}
                  {geo.slice(0, 10).map((c) => (
                    <span
                      key={c}
                      className="rounded px-1 text-[10px] bg-indigo-950 text-indigo-200"
                    >
                      {c}
                    </span>
                  ))}
                  {geo.length > 10 && (
                    <span className="text-[10px] text-slate-500">+{geo.length - 10}</span>
                  )}
                </div>
              </button>
              {open && (
                <div className="mt-2 border-t border-slate-800 pt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[10px] text-slate-500">
                  <span>owner</span>
                  <span className="text-slate-300">{row.owner}</span>
                  <span>sources</span>
                  <span className="text-slate-300">{arrLen(row.body?.sources)}</span>
                  <span>analysts</span>
                  <span className="text-slate-300">{arrLen(row.body?.analyst)}</span>
                  <span>entity classes</span>
                  <span className="text-slate-300">{arrLen(scope.entity_classes)}</span>
                  <span>languages</span>
                  <span className="text-slate-300 truncate">
                    {(scope.languages ?? []).join(', ') || '—'}
                  </span>
                  <span>time horizon</span>
                  <span className="text-slate-300">
                    {scope.time_horizon_days != null ? `${scope.time_horizon_days}d` : '—'}
                  </span>
                  {(scope.tags ?? []).length > 0 && (
                    <>
                      <span>tags</span>
                      <span className="text-slate-300 truncate">
                        {(scope.tags ?? []).join(', ')}
                      </span>
                    </>
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

function StateBadge({ state }: { state: string }) {
  const color =
    state === 'active'
      ? 'bg-accent-ok/30 text-accent-ok'
      : state === 'paused'
        ? 'bg-accent-warning/30 text-accent-warning'
        : state === 'retired'
          ? 'bg-slate-700/40 text-slate-400'
          : 'bg-accent-info/30 text-accent-info'
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider shrink-0 ${color}`}>
      {state}
    </span>
  )
}
