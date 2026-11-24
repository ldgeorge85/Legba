/**
 * Flow (N-Q) — the left rail: the view groups (click to expand / collapse),
 * the aggregated relations with their counts, search hits (click → that
 * node's neighbourhood) and the whole-system report. Plain DOM, no canvas.
 */
import { cn } from '@/lib/cn'
import type { FlowGraphPayload, FlowNodePayload } from './flowTypes'
import { aggregateLine, nodeCount, viewGroupLabel, type FlowView } from './flowView'

function label(id: string): string {
  if (id.startsWith('group:')) return viewGroupLabel(id.slice('group:'.length))
  return id
}

export function ReportBox({ payload }: { payload: FlowGraphPayload }) {
  const r = payload.report
  return (
    <div className="space-y-2 text-[11px]" data-testid="flow-report">
      <div className="text-ink-2">
        {r.nodes} nodes · {r.edges} declared edges (whole system)
      </div>
      <div>
        <div className="text-ink-3">Edges by basis</div>
        <ul className="text-ink-2">
          {Object.entries(r.edges_by_basis).map(([k, v]) => <li key={k}>{k}: {v}</li>)}
        </ul>
      </div>
      <div>
        <div className="text-ink-3">Relations the descriptors cannot express (not drawn)</div>
        <ul className="text-ink-2">
          {r.inexpressible.map((x) => <li key={x.relation}>{x.relation}: {x.nodes} nodes</li>)}
        </ul>
      </div>
      {r.unresolved_refs.length > 0 && (
        <div className="text-ink-2">{r.unresolved_refs.length} references to heads that are not active or paused</div>
      )}
    </div>
  )
}

export function FlowSidebar({
  view,
  onToggle,
  hits,
  onHit,
  report,
}: {
  view: FlowView
  onToggle: (group: string) => void
  hits: FlowNodePayload[]
  onHit: (id: string) => void
  report: FlowGraphPayload | null
}) {
  return (
    <div className="w-52 shrink-0 overflow-auto border-r border-line p-2 text-[11px]" data-testid="flow-groups">
      {hits.length > 0 && (
        <div className="mb-3">
          <div className="mb-1 text-ink-3">Search hits (click: its neighbourhood)</div>
          {hits.map((n) => (
            <button
              key={n.id}
              type="button"
              className="block w-full truncate rounded-control px-1 py-0.5 text-left text-ink-1 hover:bg-surf-2"
              onClick={() => onHit(n.id)}
              data-testid={`flow-hit-${n.id}`}
            >
              {n.label}
            </button>
          ))}
        </div>
      )}
      <div className="mb-1 text-ink-3">Groups (click to expand / collapse)</div>
      {view.groups.map((g) => (
        <button
          key={g.group}
          type="button"
          className={cn('flex w-full items-center justify-between rounded-control px-1 py-0.5 text-left hover:bg-surf-2', g.expanded ? 'text-ink-1' : 'text-ink-3')}
          onClick={() => onToggle(g.group)}
          data-testid={`flow-group-toggle-${g.group}`}
        >
          <span className="truncate">{g.expanded ? '▾' : '▸'} {viewGroupLabel(g.group)}</span>{' '}
          <span className="tabular-nums" title={nodeCount(g.count)}>{g.count}</span>
        </button>
      ))}
      {view.aggregates.length > 0 && (
        <div className="mt-3 border-t border-line pt-2" data-testid="flow-aggregates">
          <div className="mb-1 text-ink-3">Aggregated relations</div>
          {[...view.aggregates]
            .sort((a, b) => b.count - a.count || a.id.localeCompare(b.id))
            .slice(0, 40)
            .map((a) => (
              <div key={a.id} className="mb-1 text-ink-2" title={a.bases.join(', ')}>
                <span className="text-ink-1">{label(a.source)}</span> → <span className="text-ink-1">{label(a.target)}</span>
                <div className="text-ink-3">{aggregateLine(a)}</div>
              </div>
            ))}
        </div>
      )}
      {report && (
        <div className="mt-3 border-t border-line pt-2">
          <ReportBox payload={report} />
        </div>
      )}
    </div>
  )
}
