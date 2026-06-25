/**
 * O1. Target Registry (`registry.targets`).
 *
 * Browse/search target descriptors via
 * `GET /api/v1/registry/descriptors?family=target&head_only=true`.
 *
 * Inspect-only for Pass 2: filter by id / state / abstraction_level;
 * click a row to expand the full descriptor body (read-only JSON view).
 * Create / edit / retire mutations land in Pass 3 (the inline YAML
 * editor with pydantic validation per legba_ui_panels_v2.md §3.4 O1).
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { DescriptorEditor } from '@/components/DescriptorEditor'
import { DescriptorBuilder } from '@/components/DescriptorBuilder'
import { DescriptorView } from '@/components/DescriptorView'
import { StarterPicker } from '@/components/StarterPicker'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'

interface DescriptorRow {
  descriptor_id: string
  version: string
  schema_uri: string
  is_head: boolean
  state: string
  owner: string
  name: string
  family: string
  abstraction_level?: string | null
  kind?: string | null
  body: Record<string, unknown>
}

const STATE_OPTIONS = ['all', 'draft', 'configured', 'active', 'paused', 'retired'] as const

export default function RegistryTargetsPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [query, setQuery] = useState('')
  const [stateFilter, setStateFilter] = useState<(typeof STATE_OPTIONS)[number]>('all')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [creatingNew, setCreatingNew] = useState(false)
  // UI-4: guided builder + starter-clone (the "less raw" authoring surface).
  const [buildingNew, setBuildingNew] = useState(false)
  const [pickingStarter, setPickingStarter] = useState(false)
  const [clonedBody, setClonedBody] = useState<Record<string, unknown> | null>(null)

  const { data, isLoading, error, refetch } = useQuery<DescriptorRow[]>({
    queryKey: ['registry-targets'],
    queryFn: () =>
      apiGet<DescriptorRow[]>(
        '/registry/descriptors?family=target&head_only=true&limit=500',
      ),
    refetchInterval: 60_000,
  })

  const onSaved = (newVersion: string) => {
    setEditingId(null)
    setCreatingNew(false)
    setBuildingNew(false)
    setPickingStarter(false)
    setClonedBody(null)
    qc.invalidateQueries({ queryKey: ['registry-targets'] })
    refetch()
    if (newVersion !== 'retired') {
      window.alert(`Saved — new version ${newVersion.slice(0, 16)}`)
    }
  }

  const filtered = useMemo(() => {
    return (data ?? []).filter((row) => {
      if (stateFilter !== 'all' && row.state !== stateFilter) return false
      if (query && !row.descriptor_id.toLowerCase().includes(query.toLowerCase())) return false
      return true
    })
  }, [data, query, stateFilter])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${filtered.length} target descriptors`}
      onRefresh={() => refetch()}
    >
      <div className="flex items-center gap-2 mb-2 text-xs">
        <input
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="filter by id…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value as (typeof STATE_OPTIONS)[number])}
        >
          {STATE_OPTIONS.map((s) => (
            <option key={s} value={s}>
              state: {s}
            </option>
          ))}
        </select>
        <button
          onClick={() => { setBuildingNew(true); setCreatingNew(false); setPickingStarter(false); setEditingId(null) }}
          className="bg-emerald-900 hover:bg-emerald-800 text-emerald-200 rounded px-2 py-1 text-xs"
          data-testid="target-build"
        >
          + build
        </button>
        <button
          onClick={() => { setPickingStarter(true); setBuildingNew(false); setCreatingNew(false); setEditingId(null) }}
          className="bg-sky-900 hover:bg-sky-800 text-sky-200 rounded px-2 py-1 text-xs"
          data-testid="target-starter"
        >
          + starter
        </button>
        <button
          onClick={() => { setCreatingNew(true); setBuildingNew(false); setPickingStarter(false); setClonedBody(null); setEditingId(null) }}
          className="bg-surface-200 hover:bg-surface-300 border border-slate-700 text-slate-300 rounded px-2 py-1 text-xs"
          data-testid="target-new-yaml"
        >
          + raw YAML
        </button>
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {buildingNew && (
        <DescriptorBuilder
          family="target"
          onSaved={onSaved}
          onCancel={() => setBuildingNew(false)}
        />
      )}

      {pickingStarter && (
        <StarterPicker
          family="target"
          onClone={(body) => { setClonedBody(body); setCreatingNew(true); setPickingStarter(false) }}
          onCancel={() => setPickingStarter(false)}
        />
      )}

      {creatingNew && (
        <DescriptorEditor
          family="target"
          initialBody={clonedBody ?? undefined}
          onSaved={onSaved}
          onCancel={() => { setCreatingNew(false); setClonedBody(null) }}
        />
      )}

      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-1">
        {filtered.length === 0 && !isLoading && (
          <div className="text-slate-500 text-center py-4">no target descriptors match</div>
        )}
        {filtered.map((row) => {
          const expanded = expandedId === row.descriptor_id
          return (
            <div key={row.descriptor_id} className="bg-surface-100 border border-slate-800 rounded p-2">
              <button
                onClick={() => setExpandedId(expanded ? null : row.descriptor_id)}
                className="w-full text-left"
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${
                      row.state === 'active'
                        ? 'bg-emerald-900 text-emerald-200'
                        : row.state === 'paused'
                          ? 'bg-amber-900 text-amber-200'
                          : row.state === 'retired'
                            ? 'bg-slate-800 text-slate-400'
                            : 'bg-slate-700 text-slate-200'
                    }`}
                  >
                    {row.state}
                  </span>
                  {row.abstraction_level && (
                    <span className="shrink-0 text-slate-500 text-[10px]">{row.abstraction_level}</span>
                  )}
                  <span className="text-slate-200 truncate flex-1">{row.descriptor_id}</span>
                  <span className="text-slate-600 font-mono text-[10px] shrink-0">
                    @{row.version.slice(0, 8)}
                  </span>
                </div>
                <div className="text-slate-500 mt-1 truncate">{row.name}</div>
                <div className="text-slate-600 text-[10px] mt-0.5">owner: {row.owner}</div>
              </button>
              {expanded && (
                <>
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        setEditingId(editingId === row.descriptor_id ? null : row.descriptor_id)
                      }}
                      className="bg-sky-900 hover:bg-sky-800 text-sky-200 rounded px-2 py-1 text-[10px]"
                    >
                      {editingId === row.descriptor_id ? 'cancel edit' : 'edit'}
                    </button>
                  </div>
                  {editingId === row.descriptor_id ? (
                    <DescriptorEditor
                      family="target"
                      descriptorId={row.descriptor_id}
                      initialBody={row.body}
                      onSaved={onSaved}
                      onCancel={() => setEditingId(null)}
                    />
                  ) : (
                    <DescriptorView
                      body={row.body}
                      primaryKeys={['name', 'abstraction_level', 'state', 'description']}
                    />
                  )}
                </>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
