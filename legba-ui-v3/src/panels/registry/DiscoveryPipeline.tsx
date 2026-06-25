/**
 * D3 (Tier C) — `registry.discovery`.
 *
 * The discovery-pipeline operator surface. Discovery materialises candidate
 * targets/sources via list/crawl/query with a validate-before-register gate;
 * discovered 'open' sources auto-wire into the substrate. This panel makes the
 * whole flow legible:
 *
 *   ┌─ discovery descriptors ────────────────────────────────────────────┐
 *   │ every TARGET/SOURCE template carrying a `discovery` block, with the  │
 *   │ kind / list_source / relabel-rule count / resync policy lifted to    │
 *   │ the row. Click one to scope the pipeline to its candidates.          │
 *   └─────────────────────────────────────────────────────────────────────┘
 *   ┌─ candidate pipeline ───────────────────────────────────────────────┐
 *   │ the materialised L1 children, columned by stage:                     │
 *   │   proposed → validated → registered           (+ rejected lane)      │
 *   │ a child belongs to a discovery iff its `inherits` carries the        │
 *   │ discovery descriptor's id (both materialisers append it).            │
 *   └─────────────────────────────────────────────────────────────────────┘
 *   ┌─ rejected (DLQ) ───────────────────────────────────────────────────┐
 *   │ candidates that failed validate-before-register — never written to   │
 *   │ the substrate — surfaced from the registry dead-letter table.        │
 *   └─────────────────────────────────────────────────────────────────────┘
 *
 * Reads only FROZEN generic registry routes (P-05): GET /registry/descriptors,
 * GET /registry/sources, GET /registry/dead_letter. No bespoke discovery REST.
 * Mirrors the source panels (SourceRegistry) + uses ScopePicker, per spec.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { DescriptorView } from '@/components/DescriptorView'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'
import {
  buildCandidates,
  groupByStage,
  isDiscoveryRejection,
  liftDiscovery,
  PIPELINE_STAGES,
  STAGE_LABEL,
  stageClass,
  type Candidate,
  type DescriptorRowOut,
  type DiscoveryDescriptor,
  type DLQEntryOut,
  type FamilyFilter,
} from './discoveryTypes'

function descStateClass(state: string): string {
  switch (state) {
    case 'active':
      return 'bg-emerald-900 text-emerald-200'
    case 'configured':
      return 'bg-sky-900 text-sky-200'
    case 'paused':
      return 'bg-amber-900 text-amber-200'
    case 'retired':
      return 'bg-slate-800 text-slate-400'
    default:
      return 'bg-slate-700 text-slate-200'
  }
}

export default function DiscoveryPipelinePanel({ registration }: PanelProps) {
  const [familyFilter, setFamilyFilter] = useState<FamilyFilter>('all')
  const [selected, setSelected] = useState<string>('') // discovery descriptor id, '' = all
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [showRejected, setShowRejected] = useState(true)

  // Full target + source descriptor rows (head only). The source family is
  // read through /sources but the panel only needs body+inherits+state, all of
  // which the SourceDescriptorOut also carries, so we read the generic
  // /descriptors route for both to keep one shape.
  const targets = useQuery<DescriptorRowOut[]>({
    queryKey: ['discovery-descriptors', 'target'],
    queryFn: () =>
      apiGet<DescriptorRowOut[]>('/registry/descriptors?family=target&head_only=true&limit=1000'),
    refetchInterval: 60_000,
  })
  const sources = useQuery<DescriptorRowOut[]>({
    queryKey: ['discovery-descriptors', 'source'],
    queryFn: () =>
      apiGet<DescriptorRowOut[]>('/registry/descriptors?family=source&head_only=true&limit=1000'),
    refetchInterval: 60_000,
  })
  const dlq = useQuery<DLQEntryOut[]>({
    queryKey: ['discovery-dlq'],
    queryFn: () => apiGet<DLQEntryOut[]>('/registry/dead_letter?limit=200'),
    refetchInterval: 60_000,
  })

  const refetchAll = () => {
    targets.refetch()
    sources.refetch()
    dlq.refetch()
  }

  // --- discovery descriptors (rows carrying a discovery block) ---
  const discoveries = useMemo<DiscoveryDescriptor[]>(() => {
    const out: DiscoveryDescriptor[] = []
    if (familyFilter !== 'source') {
      for (const r of targets.data ?? []) {
        const d = liftDiscovery(r, 'target')
        if (d) out.push(d)
      }
    }
    if (familyFilter !== 'target') {
      for (const r of sources.data ?? []) {
        const d = liftDiscovery(r, 'source')
        if (d) out.push(d)
      }
    }
    return out.sort((a, b) => a.descriptorId.localeCompare(b.descriptorId))
  }, [targets.data, sources.data, familyFilter])

  // --- candidate pipeline (children inheriting the selected discovery) ---
  const candidates = useMemo<Candidate[]>(() => {
    const selectedDiscoveries = selected
      ? discoveries.filter((d) => d.descriptorId === selected)
      : discoveries
    const targetIds = new Set(
      selectedDiscoveries.filter((d) => d.family === 'target').map((d) => d.descriptorId),
    )
    const sourceIds = new Set(
      selectedDiscoveries.filter((d) => d.family === 'source').map((d) => d.descriptorId),
    )
    const out: Candidate[] = []
    if (targetIds.size > 0) {
      out.push(...buildCandidates(targets.data ?? [], targetIds, 'target'))
    }
    if (sourceIds.size > 0) {
      out.push(...buildCandidates(sources.data ?? [], sourceIds, 'source'))
    }
    return out
  }, [discoveries, selected, targets.data, sources.data])

  const byStage = useMemo(() => groupByStage(candidates), [candidates])

  const rejections = useMemo(() => {
    return (dlq.data ?? []).filter(isDiscoveryRejection)
  }, [dlq.data])

  const isLoading = targets.isLoading || sources.isLoading
  const err = targets.error ?? sources.error ?? dlq.error

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${discoveries.length} discovery descriptor${discoveries.length === 1 ? '' : 's'} · ${candidates.length} candidate${candidates.length === 1 ? '' : 's'}`}
      onRefresh={refetchAll}
    >
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={familyFilter}
          onChange={(e) => {
            setFamilyFilter(e.target.value as FamilyFilter)
            setSelected('')
          }}
          data-testid="discovery-family-filter"
        >
          <option value="all">family: all</option>
          <option value="target">family: target</option>
          <option value="source">family: source</option>
        </select>
        <span className="text-slate-500">scope to a target template:</span>
        <ScopePicker
          family="target"
          value={selected}
          onChange={(v) => setSelected(v)}
          placeholder="all discovery"
          testId="discovery-scope-picker"
        />
        {selected && (
          <button
            onClick={() => setSelected('')}
            className="bg-slate-800 hover:bg-slate-700 text-slate-300 rounded px-2 py-1 text-[10px]"
            data-testid="discovery-clear-scope"
          >
            clear scope
          </button>
        )}
        {isLoading && <span className="text-slate-500">loading…</span>}
      </div>

      {err instanceof Error && (
        <div className="text-rose-400 text-sm mb-2" data-testid="discovery-error">
          error: {err.message}
        </div>
      )}

      <div className="flex-1 overflow-auto text-xs space-y-3" data-testid="discovery-body">
        {/* ---- discovery descriptors ---- */}
        <section>
          <h3 className="text-slate-400 uppercase tracking-wide text-[10px] mb-1">
            Discovery descriptors
          </h3>
          <div className="space-y-1" data-testid="discovery-descriptors">
            {discoveries.length === 0 && !isLoading && (
              <div className="text-slate-500 py-2">no discovery descriptors found</div>
            )}
            {discoveries.map((d) => {
              const expanded = expandedId === d.descriptorId
              const active = selected === d.descriptorId
              return (
                <div
                  key={d.descriptorId}
                  className={`bg-surface-100 border rounded p-2 ${active ? 'border-violet-600' : 'border-slate-800'}`}
                  data-testid={`discovery-row-${d.descriptorId}`}
                >
                  <div className="flex items-baseline gap-2">
                    <span className={`shrink-0 rounded px-1 text-[10px] ${descStateClass(d.state)}`}>
                      {d.state}
                    </span>
                    <span className="shrink-0 rounded px-1 text-[10px] bg-slate-800 text-slate-300">
                      {d.family}
                    </span>
                    <span className="shrink-0 rounded px-1 text-[10px] bg-violet-950 text-violet-300">
                      {d.block.kind}
                    </span>
                    {d.abstractionLevel && (
                      <span className="shrink-0 text-slate-500 text-[10px]">{d.abstractionLevel}</span>
                    )}
                    <button
                      onClick={() => setSelected(active ? '' : d.descriptorId)}
                      className="text-slate-200 truncate flex-1 text-left hover:text-violet-300"
                      data-testid={`discovery-select-${d.descriptorId}`}
                    >
                      {d.descriptorId}
                    </button>
                    <button
                      onClick={() => setExpandedId(expanded ? null : d.descriptorId)}
                      className="shrink-0 text-slate-500 text-[10px] hover:text-slate-300"
                      data-testid={`discovery-expand-${d.descriptorId}`}
                    >
                      {expanded ? '▲ body' : '▼ body'}
                    </button>
                  </div>
                  <div className="text-slate-600 text-[10px] mt-0.5 flex gap-3 flex-wrap">
                    {d.inheritsTemplate && <span>template: {d.inheritsTemplate}</span>}
                    {d.block.list_source && <span>list: {d.block.list_source}</span>}
                    <span>relabel rules: {(d.block.relabel ?? []).length}</span>
                    <span>
                      resync:{' '}
                      {d.block.resync_policy
                        ? JSON.stringify(d.block.resync_policy)
                        : 'handler default'}
                    </span>
                  </div>
                  {expanded && <DescriptorView body={d.body as Record<string, unknown>} />}
                </div>
              )
            })}
          </div>
        </section>

        {/* ---- candidate pipeline ---- */}
        <section>
          <h3 className="text-slate-400 uppercase tracking-wide text-[10px] mb-1">
            Candidate pipeline {selected ? `· ${selected}` : '· all discovery'}
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-2" data-testid="discovery-pipeline">
            {PIPELINE_STAGES.map((stage) => {
              const rows = byStage[stage]
              return (
                <div
                  key={stage}
                  className="bg-surface-100 border border-slate-800 rounded p-1.5"
                  data-testid={`pipeline-col-${stage}`}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className={`rounded px-1 text-[10px] ${stageClass(stage)}`}>
                      {STAGE_LABEL[stage]}
                    </span>
                    <span className="text-slate-500 text-[10px]" data-testid={`pipeline-count-${stage}`}>
                      {rows.length}
                    </span>
                  </div>
                  <div className="space-y-1">
                    {rows.length === 0 && (
                      <div className="text-slate-600 text-[10px] py-1 text-center">—</div>
                    )}
                    {rows.map((c) => (
                      <div
                        key={c.descriptorId}
                        className="bg-surface-200 rounded px-1.5 py-1"
                        data-testid={`candidate-${c.descriptorId}`}
                        title={`state=${c.state} family=${c.family}`}
                      >
                        <div className="text-slate-200 truncate text-[10px]">{c.descriptorId}</div>
                        <div className="text-slate-500 text-[9px] flex gap-2">
                          <span>{c.family}</span>
                          {c.naturalKey && <span>key: {c.naturalKey}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )
            })}
          </div>
        </section>

        {/* ---- rejected (DLQ) ---- */}
        <section>
          <button
            onClick={() => setShowRejected((s) => !s)}
            className="text-slate-400 uppercase tracking-wide text-[10px] mb-1 hover:text-slate-200"
            data-testid="discovery-toggle-dlq"
          >
            {showRejected ? '▼' : '▶'} Rejected — validate-before-register ({rejections.length})
          </button>
          {showRejected && (
            <div className="space-y-1" data-testid="discovery-dlq">
              {rejections.length === 0 && (
                <div className="text-slate-500 py-2 text-[10px]">
                  no discovery rejections in the dead-letter queue
                </div>
              )}
              {rejections.map((e) => (
                <div
                  key={e.id}
                  className="bg-surface-100 border border-rose-950 rounded p-2"
                  data-testid={`dlq-${e.id}`}
                >
                  <div className="flex items-baseline gap-2">
                    <span className="rounded px-1 text-[10px] bg-rose-950 text-rose-300 shrink-0">
                      rejected
                    </span>
                    <span className="text-slate-300 truncate flex-1">{e.actor}</span>
                    <span className="text-slate-600 text-[10px] shrink-0">
                      {new Date(e.attempted_at).toLocaleString()}
                    </span>
                  </div>
                  <div className="text-rose-300/80 text-[10px] mt-0.5">
                    {e.resolution ?? JSON.stringify(e.validation_error)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </PanelChrome>
  )
}
