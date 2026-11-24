/**
 * Flow (N-J v0) — node anatomy. Fixed width; the left accent and the shape
 * encode the KIND (tokens only), a paused node is dimmed, a node shown only as
 * scope context is faded, a search hit is ringed, and an active run overlay
 * paints a status ring. Read-only: no handles are connectable.
 */
import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { cn } from '@/lib/cn'
import type { FlowCanvasNode, FlowCardData, FlowGroupData } from './flowProjection'
import { CARD_H, CARD_W, GROUP_H, GROUP_W } from './flowLayout'
import { nodeCount } from './flowView'

/** Accent + shape per kind. Unknown kinds fall back to the generic box. */
export const KIND_STYLE: Record<string, { accent: string; shape: string; tag: string }> = {
  source: { accent: 'border-l-accent-info', shape: 'rounded-control', tag: 'source' },
  target: { accent: 'border-l-accent-ok', shape: 'rounded-control', tag: 'target' },
  analyst: { accent: 'border-l-accent-emphasis', shape: 'rounded-panel', tag: 'analyst' },
  pack: { accent: 'border-l-accent-warning', shape: 'rounded-none', tag: 'tools' },
  collection: { accent: 'border-l-severity-low', shape: 'rounded-control', tag: 'collection' },
  store: { accent: 'border-l-confidence-high', shape: 'rounded-full', tag: 'store' },
  sink: { accent: 'border-l-severity-high', shape: 'rounded-none', tag: 'sink' },
  unresolved: { accent: 'border-l-accent-critical', shape: 'rounded-control border-dashed', tag: 'unresolved' },
}
const GENERIC = { accent: 'border-l-line-strong', shape: 'rounded-control', tag: 'unknown' }

export function kindStyle(kind: string) {
  return KIND_STYLE[kind] ?? GENERIC
}

const RUN_RING: Record<string, string> = {
  success: 'ring-2 ring-accent-ok',
  failed: 'ring-2 ring-accent-critical',
  error: 'ring-2 ring-accent-critical',
}

const handle = '!h-1.5 !w-1.5 !min-w-0 !border-0 !bg-line-strong'

function FlowCardImpl({ data, selected }: NodeProps<Extract<FlowCanvasNode, { type: 'flowCard' }>>) {
  const d = data as FlowCardData
  const st = kindStyle(d.kind)
  return (
    <div
      className={cn(
        'relative border border-line bg-surf-2 px-2.5 py-1.5 border-l-4 flex flex-col justify-center gap-0.5',
        st.accent, st.shape,
        d.paused && 'opacity-50',
        d.context && 'opacity-60',
        d.matched && 'ring-2 ring-accent-warning',
        d.runStatus && (RUN_RING[d.runStatus] ?? 'ring-2 ring-accent-warning'),
        selected && 'ring-2 ring-accent-info',
      )}
      style={{ width: CARD_W, height: CARD_H }}
      data-testid={`flow-card-${d.nodeId}`}
      data-paused={d.paused ? 'true' : 'false'}
      title={`${st.tag} · ${d.nodeId}${d.paused ? ' · paused' : ''}`}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className={handle} />
      <div className="flex items-center gap-1.5 min-w-0">
        <span className="text-[10px] uppercase tracking-wide text-ink-3 shrink-0">{st.tag}</span>
        <span className="text-label text-ink-1 truncate font-medium">{d.label}</span>
      </div>
      <div className="text-[11px] text-ink-2 truncate">{d.summary}</div>
      <div className="flex items-center gap-1 text-[10px] text-ink-3">
        {d.state ? <span>{d.state}</span> : null}
        {d.runStatus ? <span className="text-ink-1">run: {d.runStatus}</span> : null}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} className={handle} />
    </div>
  )
}

function FlowGroupImpl({ data, selected }: NodeProps<Extract<FlowCanvasNode, { type: 'flowGroup' }>>) {
  const d = data as FlowGroupData
  return (
    <div
      className={cn(
        'relative rounded-panel border-2 border-dashed border-line-strong bg-surf-1 px-3 py-2',
        'flex flex-col justify-center',
        d.matched && 'ring-2 ring-accent-warning',
        selected && 'ring-2 ring-accent-info',
      )}
      style={{ width: GROUP_W, height: GROUP_H }}
      data-testid={`flow-group-${d.group}`}
      title={`${d.label}: ${nodeCount(d.count)}, collapsed — expand from the group list`}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className={handle} />
      <span className="text-[10px] uppercase tracking-wide text-ink-3">group</span>
      <span className="text-label text-ink-1 font-medium truncate">{d.label}</span>{' '}
      <span className="text-[11px] text-ink-2" data-testid="flow-group-count">
        {nodeCount(d.count)}{d.paused ? ` · ${d.paused} paused` : ''}
      </span>
      <Handle type="source" position={Position.Right} isConnectable={false} className={handle} />
    </div>
  )
}

export const FlowCard = memo(FlowCardImpl)
export const FlowGroup = memo(FlowGroupImpl)
export const FLOW_NODE_TYPES = { flowCard: FlowCard, flowGroup: FlowGroup }
