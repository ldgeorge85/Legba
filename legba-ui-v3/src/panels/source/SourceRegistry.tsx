/**
 * UI-2 / Tier C — `registry.sources`.
 *
 * The source-first pivot's headline registry: every shared SourceDescriptor,
 * listed with the source-specific surface (kind / acquisition / scope /
 * subscription policy / output subject) lifted to the row so you read it
 * without opening `body`.
 *
 * Reads the FROZEN projected list: GET /api/v1/registry/sources (P-05,
 * SourceDescriptorOut). Per-row:
 *   - expand → inline detail (descriptor body + a "open detail panel" deep link)
 *   - edit / create → the existing inline DescriptorEditor (family='source')
 *     pre-seeded with a working starter descriptor when creating
 *   - lifecycle transitions (draft→configured→active⇄paused) via
 *     POST /registry/descriptors/source/{id}/transition
 *   - retire (handled inside DescriptorEditor's Retire button)
 *
 * Clicking "open detail" fires `legba:open-source-detail` so the source.detail
 * panel picks it up if mounted (same cross-panel-event pattern as Findings →
 * Lineage).
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { DescriptorEditor } from '@/components/DescriptorEditor'
import { DescriptorView } from '@/components/DescriptorView'
import { apiGet, apiPost, ApiError } from '@/lib/api'
import { tryDecodeClaims, getToken } from '@/auth/jwt'
import type { PanelProps } from '@/types'
import { useSelection } from '@/state/selection'
import {
  FORWARD_TRANSITIONS,
  SOURCE_STATES,
  starterSourceDescriptor,
  type SourceDescriptorOut,
  type SourceStateFilter,
} from './sourceTypes'

function stateClass(state: string): string {
  switch (state) {
    case 'active':
      return 'bg-emerald-900 text-emerald-200'
    case 'paused':
      return 'bg-amber-900 text-amber-200'
    case 'retired':
      return 'bg-slate-800 text-slate-400'
    case 'configured':
      return 'bg-sky-900 text-sky-200'
    default:
      return 'bg-slate-700 text-slate-200'
  }
}

export function openSourceDetail(descriptorId: string) {
  // Redesign Move 2: selecting a source brushes every room AND opens the
  // Inspector (was a legacy window event firing into the void).
  useSelection.getState().select({ kind: 'source', id: descriptorId, label: descriptorId, origin: 'source-registry' })
}

export default function SourceRegistryPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [query, setQuery] = useState('')
  const [stateFilter, setStateFilter] = useState<SourceStateFilter>('all')
  const [kindFilter, setKindFilter] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [creatingNew, setCreatingNew] = useState(false)
  const [transitionError, setTransitionError] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery<SourceDescriptorOut[]>({
    queryKey: ['registry-sources'],
    queryFn: () => apiGet<SourceDescriptorOut[]>('/registry/sources?head_only=true&limit=500'),
    refetchInterval: 60_000,
  })

  const onSaved = (newVersion: string) => {
    setEditingId(null)
    setCreatingNew(false)
    qc.invalidateQueries({ queryKey: ['registry-sources'] })
    refetch()
    if (newVersion !== 'retired') window.alert(`Saved — new version ${newVersion.slice(0, 16)}`)
  }

  async function transition(id: string, toState: string) {
    setTransitionError(null)
    try {
      await apiPost(`/registry/descriptors/source/${encodeURIComponent(id)}/transition`, {
        to_state: toState,
      })
      qc.invalidateQueries({ queryKey: ['registry-sources'] })
      refetch()
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? typeof e.body === 'object' && e.body && 'detail' in e.body
            ? String((e.body as { detail: unknown }).detail)
            : `HTTP ${e.status}`
          : (e as Error).message
      setTransitionError(`transition → ${toState} failed: ${msg}`)
    }
  }

  const owner = useMemo(() => {
    return tryDecodeClaims(getToken())?.sub || 'operator'
  }, [])

  const kinds = useMemo(() => {
    const s = new Set<string>()
    for (const r of data ?? []) if (r.kind) s.add(r.kind)
    return Array.from(s).sort()
  }, [data])

  const filtered = useMemo(() => {
    return (data ?? []).filter((row) => {
      if (stateFilter !== 'all' && row.state !== stateFilter) return false
      if (kindFilter && row.kind !== kindFilter) return false
      if (query) {
        const q = query.toLowerCase()
        const hay = `${row.descriptor_id} ${row.name} ${(row.tags ?? []).join(' ')}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      return true
    })
  }, [data, query, stateFilter, kindFilter])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${filtered.length} source${filtered.length === 1 ? '' : 's'}`}
      onRefresh={() => refetch()}
    >
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <input
          className="flex-1 min-w-[140px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="filter by id / name / tag…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          data-testid="sources-search"
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value as SourceStateFilter)}
          data-testid="sources-state-filter"
        >
          {SOURCE_STATES.map((s) => (
            <option key={s} value={s}>
              state: {s}
            </option>
          ))}
        </select>
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
          data-testid="sources-kind-filter"
        >
          <option value="">kind: all</option>
          {kinds.map((k) => (
            <option key={k} value={k}>
              kind: {k}
            </option>
          ))}
        </select>
        <button
          onClick={() => {
            setCreatingNew(true)
            setEditingId(null)
          }}
          className="bg-emerald-900 hover:bg-emerald-800 text-emerald-200 rounded px-2 py-1 text-xs"
          data-testid="sources-new"
        >
          + new source
        </button>
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {creatingNew && (
        <DescriptorEditor
          family="source"
          initialBody={starterSourceDescriptor(owner)}
          onSaved={onSaved}
          onCancel={() => setCreatingNew(false)}
        />
      )}

      {error instanceof Error && <div className="text-rose-400 text-sm">error: {error.message}</div>}
      {transitionError && (
        <div className="text-rose-400 text-xs mb-2" data-testid="sources-transition-error">
          {transitionError}
        </div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1" data-testid="sources-list">
        {filtered.length === 0 && !isLoading && (
          <div className="text-slate-500 text-center py-4">no sources match</div>
        )}
        {filtered.map((row) => {
          const expanded = expandedId === row.descriptor_id
          const nextStates = FORWARD_TRANSITIONS[row.state] ?? []
          return (
            <div
              key={row.descriptor_id}
              className="bg-surface-100 border border-slate-800 rounded p-2"
              data-testid={`source-row-${row.descriptor_id}`}
            >
              <button
                onClick={() => setExpandedId(expanded ? null : row.descriptor_id)}
                className="w-full text-left"
              >
                <div className="flex items-baseline gap-2">
                  <span className={`shrink-0 rounded px-1 text-[10px] ${stateClass(row.state)}`}>
                    {row.state}
                  </span>
                  {row.kind && (
                    <span className="shrink-0 rounded px-1 text-[10px] bg-slate-800 text-slate-300">
                      {row.kind}
                    </span>
                  )}
                  <span className="shrink-0 text-slate-500 text-[10px]">{row.acquisition}</span>
                  <span className="text-slate-200 truncate flex-1">{row.descriptor_id}</span>
                  <span className="text-slate-600 font-mono text-[10px] shrink-0">
                    @{row.version.slice(0, 8)}
                  </span>
                </div>
                <div className="text-slate-500 mt-1 truncate">{row.name}</div>
                <div className="text-slate-600 text-[10px] mt-0.5 flex gap-3 flex-wrap">
                  <span>policy: {row.subscription_policy ?? 'open'}</span>
                  {row.owner_tenant && <span>tenant: {row.owner_tenant}</span>}
                  {row.geo.length > 0 && <span>geo: {row.geo.join(', ')}</span>}
                  {row.tags.length > 0 && <span>tags: {row.tags.join(', ')}</span>}
                  {row.has_discovery && <span className="text-violet-400">discovery</span>}
                  {row.has_provision && <span className="text-sky-400">provision</span>}
                </div>
              </button>

              {expanded && (
                <div className="mt-2 space-y-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      onClick={() => openSourceDetail(row.descriptor_id)}
                      className="bg-slate-800 hover:bg-slate-700 text-slate-200 rounded px-2 py-1 text-[10px]"
                      data-testid={`source-open-detail-${row.descriptor_id}`}
                    >
                      open detail panel ↗
                    </button>
                    <button
                      onClick={() =>
                        setEditingId(editingId === row.descriptor_id ? null : row.descriptor_id)
                      }
                      className="bg-sky-900 hover:bg-sky-800 text-sky-200 rounded px-2 py-1 text-[10px]"
                      data-testid={`source-edit-${row.descriptor_id}`}
                    >
                      {editingId === row.descriptor_id ? 'cancel edit' : 'edit'}
                    </button>
                    {nextStates.map((to) => (
                      <button
                        key={to}
                        onClick={() => transition(row.descriptor_id, to)}
                        className="bg-emerald-950 hover:bg-emerald-900 text-emerald-300 rounded px-2 py-1 text-[10px]"
                        data-testid={`source-transition-${row.descriptor_id}-${to}`}
                      >
                        → {to}
                      </button>
                    ))}
                    <span className="text-slate-600 text-[10px] ml-auto">
                      out: {row.output_subject}
                    </span>
                  </div>

                  {editingId === row.descriptor_id ? (
                    <DescriptorEditor
                      family="source"
                      descriptorId={row.descriptor_id}
                      initialBody={row.body}
                      onSaved={onSaved}
                      onCancel={() => setEditingId(null)}
                    />
                  ) : (
                    <DescriptorView
                      body={row.body}
                      primaryKeys={['name', 'kind', 'modality', 'acquisition', 'scope', 'state']}
                    />
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
