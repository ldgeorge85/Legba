/**
 * P-5. Provenance Lineage (`system.lineage`).
 *
 * Walks the `derived_from` DAG upstream or downstream from any substrate
 * row. Reads `GET /api/v1/lineage/{row_kind}/{row_id}?direction=&depth=`
 * (recursive CTE on the backend, ≤10 hops, ~11 ms median at depth=3).
 *
 * Picks up `window.dispatchEvent(new CustomEvent('legba:open-lineage',
 * {detail: {row_kind, row_id, title}}))` from other panels (Findings,
 * TargetDetail, AnalystDetail) so deep-linking works without route
 * coupling.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet, ApiError } from '@/lib/api'
import { ModalityRef } from '@/lib/modalityRenderers'
import { useSelection } from '@/state/selection'
import type { PanelProps } from '@/types'

interface LineageNode {
  id: string
  row_kind: string
  title: string | null
  produced_at: string
  target_id: string | null
  analyst_id: string | null
  schema_uri: string
  depth: number
  // Link/media surface — populated for signal rows (the acquisition source).
  canonical_url?: string | null
  media_ref?: string | null
  modality?: string | null
  mime_type?: string | null
}

interface LineageEdge {
  parent: string
  child: string
}

interface LineageResponse {
  root: LineageNode
  nodes: LineageNode[]
  edges?: LineageEdge[]
  truncated_at_depth?: boolean
}

// A walkable substrate row sourced by-target (so the operator picks a row to
// walk instead of pasting a UUID). Findings + signals are the two anchors the
// roadmap's provenance walk starts from.
interface WalkRow {
  id: string
  title: string | null
}
interface Page<T> {
  data: T[]
  next_cursor: string | null
}

/** Tolerant by-target list: 404 (no rows for this target) → empty, not error. */
async function listByTarget(path: string): Promise<WalkRow[]> {
  try {
    const r = await apiGet<Page<WalkRow>>(path)
    return Array.isArray(r?.data) ? r.data : []
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return []
    throw e
  }
}

type Direction = 'upstream' | 'downstream' | 'both'

// The supported root row_kinds — mirrors `_TABLES_BY_KIND` in
// `lineage_api.py` exactly (unknown kinds 400). This is the finding →
// signal → … provenance walk the roadmap (Tier A/F) calls for; the
// originating row must be one of these substrate kinds.
const ROW_KINDS = [
  'finding',
  'signal',
  'situation',
  'hypothesis',
  'prediction',
  'meta_finding',
  'alert',
  'critique',
  'prompt_module_candidate',
] as const

const KIND_PILL: Record<string, string> = {
  finding: 'bg-emerald-900 text-emerald-200',
  meta_finding: 'bg-emerald-950 text-emerald-300',
  signal: 'bg-sky-900 text-sky-200',
  situation: 'bg-indigo-900 text-indigo-200',
  hypothesis: 'bg-violet-900 text-violet-200',
  prediction: 'bg-fuchsia-900 text-fuchsia-200',
  critique: 'bg-amber-900 text-amber-200',
  alert: 'bg-rose-900 text-rose-200',
  prompt_module_candidate: 'bg-slate-700 text-slate-300',
}

export default function LineagePanel({ registration }: PanelProps) {
  const [rowKind, setRowKind] = useState<(typeof ROW_KINDS)[number]>('finding')
  const [rowId, setRowId] = useState('')
  const [direction, setDirection] = useState<Direction>('upstream')
  const [depth, setDepth] = useState(3)

  // "Walk-by-target" helper: pick a target → list its findings/signals → click
  // one to seed the walk. The raw row-id box below still accepts pasted ids.
  const [walkTarget, setWalkTarget] = useState('')

  const targetFindings = useQuery<WalkRow[]>({
    enabled: walkTarget.trim().length > 0,
    queryKey: ['lineage-walk-findings', walkTarget],
    queryFn: () =>
      listByTarget(`/findings?target_id=${encodeURIComponent(walkTarget)}&limit=25`),
  })
  const targetSignals = useQuery<WalkRow[]>({
    enabled: walkTarget.trim().length > 0,
    queryKey: ['lineage-walk-signals', walkTarget],
    queryFn: () =>
      listByTarget(`/signals?target_id=${encodeURIComponent(walkTarget)}&limit=25`),
  })

  /** Seed the walk from a picked row (no UUID paste). */
  function walkRow(kind: (typeof ROW_KINDS)[number], id: string) {
    setRowKind(kind)
    setRowId(id)
  }

  // Redesign Move 2: seed the walk from the shared selection store (replaces the
  // legacy `legba:open-lineage` window listener). When a walkable row is
  // selected anywhere, this panel loads its lineage. `instanceKey` preserves the
  // true substrate kind when the cross-room kind was coerced (e.g. hypothesis).
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (!selection) return
    const rawKind = selection.instanceKey ?? selection.kind
    if ((ROW_KINDS as readonly string[]).includes(rawKind)) {
      setRowKind(rawKind as (typeof ROW_KINDS)[number])
    }
    setRowId(selection.id)
  }, [selection])

  const enabled = rowId.trim().length > 0
  const { data, error, isLoading, refetch } = useQuery<LineageResponse>({
    enabled,
    queryKey: ['lineage', rowKind, rowId, direction, depth],
    queryFn: () =>
      apiGet<LineageResponse>(
        `/lineage/${rowKind}/${encodeURIComponent(rowId)}?direction=${direction}&depth=${depth}`,
      ),
  })

  const nodesByDepth = useMemo(() => {
    if (!data) return new Map<number, LineageNode[]>()
    const m = new Map<number, LineageNode[]>()
    for (const n of data.nodes) {
      const arr = m.get(n.depth) ?? []
      arr.push(n)
      m.set(n.depth, arr)
    }
    return m
  }, [data])

  return (
    <PanelChrome
      registration={registration}
      subtitle={data ? `root + ${data.nodes.length} nodes` : 'enter a row id or click a finding'}
      onRefresh={enabled ? () => refetch() : undefined}
    >
      {/* Walk-by-target — pick a target, then click one of its findings/signals
          to seed the walk, so the operator doesn't have to paste a UUID. */}
      <div className="mb-2 bg-surface-100 border border-slate-800 rounded p-2">
        <div className="flex items-center gap-2 text-xs flex-wrap">
          <label className="text-slate-400 text-[11px]">walk a target's rows</label>
          <ScopePicker
            family="target"
            value={walkTarget}
            onChange={setWalkTarget}
            placeholder="pick a target…"
            testId="lineage-walk-target"
          />
        </div>
        {walkTarget && (
          <div className="grid grid-cols-2 gap-2 mt-2" data-testid="lineage-walk-rows">
            <WalkColumn
              label="findings"
              rows={targetFindings.data ?? []}
              loading={targetFindings.isLoading}
              active={rowKind === 'finding' ? rowId : ''}
              onPick={(id) => walkRow('finding', id)}
              testidPrefix="lineage-walk-finding"
            />
            <WalkColumn
              label="signals"
              rows={targetSignals.data ?? []}
              loading={targetSignals.isLoading}
              active={rowKind === 'signal' ? rowId : ''}
              onPick={(id) => walkRow('signal', id)}
              testidPrefix="lineage-walk-signal"
            />
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={rowKind}
          onChange={(e) => setRowKind(e.target.value as (typeof ROW_KINDS)[number])}
        >
          {ROW_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <input
          className="flex-1 min-w-[200px] bg-surface-200 border border-slate-700 rounded p-1 px-2 font-mono"
          placeholder="row uuid…"
          value={rowId}
          onChange={(e) => setRowId(e.target.value)}
          data-testid="lineage-row-id"
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={direction}
          onChange={(e) => setDirection(e.target.value as Direction)}
          data-testid="lineage-direction"
        >
          <option value="upstream">upstream (inputs)</option>
          <option value="downstream">downstream (derived)</option>
          <option value="both">both</option>
        </select>
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={depth}
          onChange={(e) => setDepth(Number(e.target.value))}
        >
          {[1, 2, 3, 5, 10].map((d) => (
            <option key={d} value={d}>
              depth ≤ {d}
            </option>
          ))}
        </select>
      </div>

      {!enabled && (
        <div className="text-slate-500 text-sm py-4 text-center">
          paste a row id above, or click any finding in the Findings panel to load its lineage
        </div>
      )}
      {isLoading && <div className="text-slate-500 text-sm">loading lineage…</div>}
      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      {data && (
        <div className="flex-1 overflow-auto text-xs space-y-3">
          {data.truncated_at_depth && (
            <div
              className="bg-amber-950/60 border border-amber-800/60 text-amber-300 rounded p-1.5 text-[10px]"
              data-testid="lineage-truncated"
            >
              walk truncated at depth {depth} — increase depth to see the full provenance chain
            </div>
          )}
          <div className="bg-surface-200 border border-amber-700 rounded p-2">
            <div className="text-amber-300 text-[10px] uppercase tracking-wide">root</div>
            <div className="text-slate-200 font-medium">{data.root.title ?? '(untitled)'}</div>
            <div className="text-slate-500 mt-1 flex flex-wrap gap-x-3 items-baseline">
              <KindPill kind={data.root.row_kind} />
              {data.root.target_id && <span>target: {data.root.target_id}</span>}
              {data.root.analyst_id && <span>analyst: {data.root.analyst_id}</span>}
              <span className="font-mono text-slate-600">{data.root.schema_uri}</span>
              <span>{new Date(data.root.produced_at).toLocaleString()}</span>
            </div>
            <ModalityRef node={data.root} />
          </div>

          {Array.from(nodesByDepth.entries())
            .sort((a, b) => a[0] - b[0])
            .map(([d, group]) => (
              <div key={d}>
                <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
                  depth {d} ({group.length} {group.length === 1 ? 'node' : 'nodes'})
                </div>
                <div className="space-y-1">
                  {group.map((n) => (
                    <div
                      key={n.id}
                      className="bg-surface-100 border border-slate-800 rounded"
                    >
                      <button
                        onClick={() => {
                          setRowKind(
                            (ROW_KINDS as readonly string[]).includes(n.row_kind)
                              ? (n.row_kind as (typeof ROW_KINDS)[number])
                              : rowKind,
                          )
                          setRowId(n.id)
                        }}
                        className="w-full text-left hover:bg-surface-200 rounded p-2"
                      >
                        <div className="flex items-baseline gap-2">
                          <KindPill kind={n.row_kind} />
                          <span className="text-slate-200 truncate">{n.title ?? '(untitled)'}</span>
                        </div>
                        <div className="text-slate-600 mt-1 flex gap-3">
                          {n.target_id && <span>target: {n.target_id}</span>}
                          {n.analyst_id && <span>analyst: {n.analyst_id}</span>}
                          <span>{new Date(n.produced_at).toLocaleString()}</span>
                        </div>
                      </button>
                      <ModalityRef node={n} className="px-2 pb-2" />
                    </div>
                  ))}
                </div>
              </div>
            ))}

          {data.nodes.length === 0 && (
            <div className="text-slate-500 text-sm py-2 text-center">
              no{' '}
              {direction === 'upstream'
                ? 'inputs'
                : direction === 'downstream'
                  ? 'derived rows'
                  : 'related rows'}{' '}
              (depth ≤ {depth})
            </div>
          )}
        </div>
      )}
    </PanelChrome>
  )
}

function WalkColumn({
  label,
  rows,
  loading,
  active,
  onPick,
  testidPrefix,
}: {
  label: string
  rows: WalkRow[]
  loading: boolean
  active: string
  onPick: (id: string) => void
  testidPrefix: string
}) {
  return (
    <div>
      <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
        {label} ({rows.length})
      </div>
      <div className="space-y-0.5 max-h-32 overflow-auto">
        {loading && <div className="text-slate-600 text-[11px]">loading…</div>}
        {!loading && rows.length === 0 && (
          <div className="text-slate-600 text-[11px]">none for this target</div>
        )}
        {rows.map((r) => (
          <button
            key={r.id}
            onClick={() => onPick(r.id)}
            className={`w-full text-left text-[11px] rounded px-2 py-0.5 truncate ${
              active === r.id
                ? 'bg-amber-900/40 border border-amber-700 text-amber-200'
                : 'bg-surface-200 hover:bg-surface-300 text-slate-300'
            }`}
            title={r.id}
            data-testid={`${testidPrefix}-${r.id}`}
          >
            {r.title ?? '(untitled)'}
          </button>
        ))}
      </div>
    </div>
  )
}

function KindPill({ kind }: { kind: string }) {
  return (
    <span
      className={`shrink-0 rounded px-1 text-[10px] ${
        KIND_PILL[kind] ?? 'bg-slate-800 text-slate-300'
      }`}
    >
      {kind}
    </span>
  )
}
