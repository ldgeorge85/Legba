/**
 * The Why / Graph room — selection-driven (reads the global selection store):
 * a finding/situation/signal → provenance trail + lineage DAG; an entity →
 * trail + relationship ego-graph. (#90: the visual graph surface; the world
 * assessment is a FINDING read in the Inspector, not shown here.)
 */
import { useSelection } from '@/state/selection'
import ProvenanceTrail from './ProvenanceTrail'
import LineageGraph from './LineageGraph'
import EntityGraph from './EntityGraph'
import { SELECTION_TO_ROW_KIND } from './types'

export default function WhyRoom() {
  const sel = useSelection((s) => s.selection)

  if (!sel) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-2 bg-surface-300 px-6 text-center">
        <div className="text-sm font-medium text-slate-300">Graph & provenance</div>
        <div className="max-w-sm text-xs leading-relaxed text-slate-500">
          Select a finding, situation, or signal to trace its lineage DAG — or an
          entity to explore its relationship graph.
        </div>
      </div>
    )
  }

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
