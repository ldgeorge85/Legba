/**
 * O3. Stack Registry (`registry.stack`).
 *
 * Browse stack components via `GET /api/v1/registry/stack`.  Eight
 * kinds: llm_provider / vector_store / embedding / nats / postgres /
 * redis / proxy_pool + nlp_service.
 *
 * Credentials are never returned by the backend (Property.Secret refs
 * resolve via the vault at call time, not at descriptor-fetch time).
 * Mutation surface (create / update / healthcheck dispatch) deferred
 * to Pass 3.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { DescriptorEditor } from '@/components/DescriptorEditor'
import { DescriptorView } from '@/components/DescriptorView'
import { StarterPicker } from '@/components/StarterPicker'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'

interface StackRow {
  component_id: string
  version: string
  schema_uri: string
  kind: string
  is_head: boolean
  state: string
  owner: string
  name: string
  body: Record<string, unknown>
}

export default function RegistryStackPanel({ registration }: PanelProps) {
  const qc = useQueryClient()
  const [query, setQuery] = useState('')
  const [kindFilter, setKindFilter] = useState<string>('all')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [creatingNew, setCreatingNew] = useState(false)
  // UI-4: starter-clone (the "less raw" authoring surface). Stack registers
  // via the dedicated /registry/stack path, so it uses the inline editor
  // pre-filled from a starter rather than the generic-descriptor form builder.
  const [pickingStarter, setPickingStarter] = useState(false)
  const [clonedBody, setClonedBody] = useState<Record<string, unknown> | null>(null)

  const { data, isLoading, error, refetch } = useQuery<StackRow[]>({
    queryKey: ['registry-stack'],
    queryFn: () => apiGet<StackRow[]>('/registry/stack?limit=500'),
    refetchInterval: 60_000,
  })

  const onSaved = (newVersion: string) => {
    setEditingId(null)
    setCreatingNew(false)
    setPickingStarter(false)
    setClonedBody(null)
    qc.invalidateQueries({ queryKey: ['registry-stack'] })
    refetch()
    if (newVersion !== 'retired') {
      window.alert(`Saved — new version ${newVersion.slice(0, 16)}`)
    }
  }

  const kinds = useMemo(() => {
    const s = new Set((data ?? []).map((r) => r.kind))
    return ['all', ...Array.from(s).sort()]
  }, [data])

  const filtered = useMemo(() => {
    return (data ?? []).filter((row) => {
      if (kindFilter !== 'all' && row.kind !== kindFilter) return false
      if (query && !row.component_id.toLowerCase().includes(query.toLowerCase())) return false
      return true
    })
  }, [data, query, kindFilter])

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${filtered.length} stack components`}
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
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
        >
          {kinds.map((k) => (
            <option key={k} value={k}>
              kind: {k}
            </option>
          ))}
        </select>
        <button
          onClick={() => { setPickingStarter(true); setCreatingNew(false); setEditingId(null) }}
          className="bg-sky-900 hover:bg-sky-800 text-sky-200 rounded px-2 py-1 text-xs"
          data-testid="stack-starter"
        >
          + starter
        </button>
        <button
          onClick={() => { setCreatingNew(true); setPickingStarter(false); setClonedBody(null); setEditingId(null) }}
          className="bg-surface-200 hover:bg-surface-300 border border-slate-700 text-slate-300 rounded px-2 py-1 text-xs"
          data-testid="stack-new-yaml"
        >
          + raw YAML
        </button>
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {pickingStarter && (
        <StarterPicker
          family="stack"
          onClone={(body) => { setClonedBody(body); setCreatingNew(true); setPickingStarter(false) }}
          onCancel={() => setPickingStarter(false)}
        />
      )}

      {creatingNew && (
        <DescriptorEditor
          family="stack"
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
          <div className="text-slate-500 text-center py-4">no stack components match</div>
        )}
        {/* `/registry/stack` returns EVERY version of a component (`is_head`
            marks the head), so component_id alone collides as soon as a
            component has history — key on the (component_id, version) pair. */}
        {filtered.map((row) => {
          const expanded = expandedId === row.component_id
          return (
            <div key={`${row.component_id}@${row.version}`} className="bg-surface-100 border border-slate-800 rounded p-2">
              <button
                onClick={() => setExpandedId(expanded ? null : row.component_id)}
                className="w-full text-left"
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className={`shrink-0 rounded px-1 text-[10px] ${
                      row.state === 'active'
                        ? 'bg-emerald-900 text-emerald-200'
                        : 'bg-slate-700 text-slate-200'
                    }`}
                  >
                    {row.state}
                  </span>
                  <span className="shrink-0 bg-violet-900 text-violet-200 rounded px-1 text-[10px]">
                    {row.kind}
                  </span>
                  <span className="text-slate-200 truncate flex-1">{row.component_id}</span>
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
                        setEditingId(editingId === row.component_id ? null : row.component_id)
                      }}
                      className="bg-sky-900 hover:bg-sky-800 text-sky-200 rounded px-2 py-1 text-[10px]"
                    >
                      {editingId === row.component_id ? 'cancel edit' : 'edit'}
                    </button>
                  </div>
                  {editingId === row.component_id ? (
                    <DescriptorEditor
                      family="stack"
                      descriptorId={row.component_id}
                      initialBody={row.body}
                      onSaved={onSaved}
                      onCancel={() => setEditingId(null)}
                    />
                  ) : (
                    <DescriptorView
                      body={row.body}
                      primaryKeys={['name', 'kind', 'provider', 'model', 'endpoint', 'description']}
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
