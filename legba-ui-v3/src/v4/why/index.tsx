/**
 * The Why / Graph room — selection-driven (reads the global selection store):
 * a finding/situation/signal → provenance trail + lineage DAG; an entity →
 * trail + relationship ego-graph. (#90: the visual graph surface; the world
 * assessment is a FINDING read in the Inspector, not shown here.)
 *
 * With nothing selected the room no longer dead-ends on placeholder text — it
 * renders an in-panel NODE PICKER (recent findings / situations / entities) so
 * the operator can drive the graph from inside the room. A pick calls the same
 * unified selection-store action a cross-room click would (selectRow for the
 * lineage-walkable kinds; an entity-by-canonical-name select for the ego-graph),
 * so the rest of the rooms brush identically.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, Boxes, FileText, GitBranch, Layers } from 'lucide-react'
import { apiGet, ApiError } from '@/lib/api'
import { selectRow, useSelection } from '@/state/selection'
import ProvenanceTrail from './ProvenanceTrail'
import LineageGraph from './LineageGraph'
import EntityGraph from './EntityGraph'
import { SELECTION_TO_ROW_KIND } from './types'

export default function WhyRoom() {
  const sel = useSelection((s) => s.selection)

  if (!sel) return <NodePicker />

  const rowKind = SELECTION_TO_ROW_KIND[sel.kind]

  return (
    <div className="h-full w-full flex flex-col bg-surface-300 min-h-0">
      <div className="shrink-0 border-b border-slate-800 p-3">
        <ProvenanceTrail selection={sel} />
      </div>
      <div className="flex-1 min-h-0">
        {sel.kind === 'entity' ? (
          <EntityGraph center={sel.id} />
        ) : rowKind ? (
          <LineageGraph kind={rowKind} id={sel.id} />
        ) : (
          <div className="h-full flex items-center justify-center text-slate-500 text-sm">
            No lineage to trace for {sel.kind} “{sel.label ?? sel.id}”.
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// In-panel node picker — the empty-state surface. A compact, scrollable list of
// recent findings / situations / entities the operator can click to drive the
// graph (no need to select something in another room first).
// ---------------------------------------------------------------------------

type PickerTab = 'findings' | 'situations' | 'entities'

const TABS: Array<{ id: PickerTab; label: string; icon: typeof FileText }> = [
  { id: 'findings', label: 'Findings', icon: FileText },
  { id: 'situations', label: 'Situations', icon: Layers },
  { id: 'entities', label: 'Entities', icon: Boxes },
]

interface FindingRow {
  id: string
  title?: string | null
  analyst_id?: string | null
  severity?: string | null
  produced_at?: string | null
}
interface SituationRow {
  id: string
  name: string
  status: string
  category: string
  intensity_score: number
}
interface EntityNode {
  id: string
  canonical_name: string
  entity_class: string
  mentions: number
}
interface ListPage<T> {
  data: T[]
}

/** Severity / status dot color from the v4 accent ramp. */
const DOT: Record<string, string> = {
  critical: 'bg-accent-critical',
  high: 'bg-accent-warning',
  medium: 'bg-accent-info',
  low: 'bg-accent-ok',
  escalating: 'bg-accent-critical',
  active: 'bg-accent-warning',
  resolved: 'bg-accent-ok',
}

const ENTITY_CLASS_DOT: Record<string, string> = {
  person: 'bg-amber-400',
  organization: 'bg-blue-400',
  location: 'bg-emerald-400',
  event: 'bg-violet-400',
  entity: 'bg-slate-400',
}

/** Map a situation's intensity to a severity bucket (mirrors target/Situations). */
function intensityKey(score: number): string {
  if (score >= 0.75) return 'critical'
  if (score >= 0.5) return 'high'
  if (score >= 0.25) return 'medium'
  return 'low'
}

/** GET helper that degrades a 404 (endpoint absent in some deploys) to empty. */
async function getList<T>(url: string): Promise<ListPage<T>> {
  try {
    return await apiGet<ListPage<T>>(url)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return { data: [] }
    throw e
  }
}

function NodePicker() {
  const [tab, setTab] = useState<PickerTab>('findings')

  return (
    <div className="flex h-full w-full flex-col bg-surface-300 min-h-0">
      <div className="shrink-0 border-b border-slate-800 px-4 pt-3 pb-2.5">
        <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
          <GitBranch className="h-4 w-4 text-slate-500" aria-hidden />
          Graph &amp; provenance
        </div>
        <div className="mt-1 text-xs leading-relaxed text-slate-500">
          Pick a node below — or select a finding, situation, or entity in any
          room — to trace its lineage DAG or relationship graph.
        </div>
        <div className="mt-3 flex gap-1">
          {TABS.map((t) => {
            const Icon = t.icon
            const on = t.id === tab
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={
                  'inline-flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs font-medium ' +
                  (on
                    ? 'border-slate-600 bg-surface-100 text-slate-100'
                    : 'border-transparent text-slate-400 hover:bg-surface-200 hover:text-slate-200')
                }
              >
                <Icon className="h-3.5 w-3.5" aria-hidden />
                {t.label}
              </button>
            )
          })}
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto p-2">
        {tab === 'findings' && <FindingsList />}
        {tab === 'situations' && <SituationsList />}
        {tab === 'entities' && <EntitiesList />}
      </div>
    </div>
  )
}

/** A clickable picker row — left dot + a title and a muted subtitle. */
function PickerRow({
  dot,
  title,
  subtitle,
  onClick,
}: {
  dot: string
  title: string
  subtitle?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left hover:bg-surface-100"
    >
      <span aria-hidden className={'h-2 w-2 shrink-0 rounded-full ' + dot} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm text-slate-200">{title}</span>
        {subtitle && (
          <span className="block truncate text-xs text-slate-500">{subtitle}</span>
        )}
      </span>
    </button>
  )
}

function PickerState({ icon: Icon, text }: { icon: typeof Activity; text: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <Icon className="h-6 w-6 text-slate-600" aria-hidden />
      <div className="text-xs text-slate-500">{text}</div>
    </div>
  )
}

function FindingsList() {
  const { data, isLoading, error } = useQuery<ListPage<FindingRow>>({
    queryKey: ['why-picker', 'findings'],
    queryFn: () => getList<FindingRow>('/findings?limit=40'),
    refetchInterval: 60_000,
  })
  if (isLoading) return <PickerState icon={Activity} text="Loading recent findings…" />
  if (error instanceof Error)
    return <PickerState icon={FileText} text={`Couldn’t load findings: ${error.message}`} />
  const rows = data?.data ?? []
  if (rows.length === 0) return <PickerState icon={FileText} text="No findings yet." />
  return (
    <div className="space-y-0.5">
      {rows.map((r) => (
        <PickerRow
          key={r.id}
          dot={DOT[r.severity ?? ''] ?? 'bg-slate-500'}
          title={r.title?.trim() || '(untitled finding)'}
          subtitle={r.analyst_id ?? undefined}
          onClick={() =>
            selectRow('finding', r.id, r.title ?? undefined, { origin: 'why-picker' })
          }
        />
      ))}
    </div>
  )
}

function SituationsList() {
  const { data, isLoading, error } = useQuery<ListPage<SituationRow>>({
    queryKey: ['why-picker', 'situations'],
    queryFn: () => getList<SituationRow>('/situations?limit=40'),
    refetchInterval: 60_000,
  })
  if (isLoading) return <PickerState icon={Activity} text="Loading situations…" />
  if (error instanceof Error)
    return <PickerState icon={Layers} text={`Couldn’t load situations: ${error.message}`} />
  const rows = data?.data ?? []
  if (rows.length === 0) return <PickerState icon={Layers} text="No situations yet." />
  return (
    <div className="space-y-0.5">
      {rows.map((r) => (
        <PickerRow
          key={r.id}
          dot={DOT[r.status] ?? DOT[intensityKey(r.intensity_score)] ?? 'bg-slate-500'}
          title={r.name?.trim() || '(unnamed situation)'}
          subtitle={[r.status, r.category].filter(Boolean).join(' · ') || undefined}
          onClick={() => selectRow('situation', r.id, r.name, { origin: 'why-picker' })}
        />
      ))}
    </div>
  )
}

function EntitiesList() {
  const { data, isLoading, error } = useQuery<ListPage<EntityNode>>({
    queryKey: ['why-picker', 'entities'],
    queryFn: () => getList<EntityNode>('/entities?limit=40'),
    refetchInterval: 60_000,
  })
  if (isLoading) return <PickerState icon={Activity} text="Loading entities…" />
  if (error instanceof Error)
    return <PickerState icon={Boxes} text={`Couldn’t load entities: ${error.message}`} />
  const rows = data?.data ?? []
  if (rows.length === 0) return <PickerState icon={Boxes} text="No entities yet." />
  return (
    <div className="space-y-0.5">
      {rows.map((r) => (
        <PickerRow
          key={r.id}
          dot={ENTITY_CLASS_DOT[r.entity_class] ?? 'bg-slate-400'}
          title={r.canonical_name?.trim() || '(unnamed entity)'}
          subtitle={
            [r.entity_class, r.mentions ? `${r.mentions} mentions` : '']
              .filter(Boolean)
              .join(' · ') || undefined
          }
          // The ego-graph centers on the entity's CANONICAL NAME (see EntityGraph
          // — edges key on names), so select the entity by name, mirroring the
          // Entities panel's openEntityGraph.
          onClick={() =>
            useSelection.getState().select({
              kind: 'entity',
              id: r.canonical_name,
              label: r.canonical_name,
              origin: 'why-picker',
            })
          }
        />
      ))}
    </div>
  )
}
