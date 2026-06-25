/**
 * The Flow — custom ReactFlow node (F.B).
 *
 * A compact dark card for one registry descriptor (source / target / analyst /
 * action_pack). The left border encodes the node KIND, a status dot encodes the
 * lifecycle STATE, and the footer paints LIVE telemetry pulled from the shared
 * flowState store keyed by descriptorId (sources show rate, analysts show fire
 * count, errors go red, a stalled source gets a ring + badge).
 *
 * Pure presentation: it reads the shared selection-free telemetry slice and the
 * node data handed to it by the canvas. The canvas owns selection/focus wiring.
 */

import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { AlertTriangle, TriangleAlert } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useFlowState } from './flowState'
import type { FlowNode as FlowNodeType, FlowNodeKind, LifecycleState } from './types'

/** Left-border accent per node kind. */
const KIND_BORDER: Record<FlowNodeKind, string> = {
  source: '#3b82f6', // accent.info
  target: '#10b981', // accent.ok
  analyst: '#a78bfa', // violet (per spec)
  pack: '#f59e0b', // accent.warning
}

/** Human label per kind for the card chrome. */
const KIND_LABEL: Record<FlowNodeKind, string> = {
  source: 'source',
  target: 'target',
  analyst: 'analyst',
  pack: 'pack',
}

/** Status-dot color per lifecycle state. */
const STATE_DOT: Record<LifecycleState, string> = {
  active: '#10b981', // accent.ok
  configured: '#3b82f6', // accent.info
  draft: '#64748b', // slate-500
  paused: '#f59e0b', // accent.warning
  retired: '#334155', // slate-700
}

function FlowNodeImpl({ data, selected }: NodeProps<FlowNodeType>) {
  const { kind, label, state, subkind, descriptorId } = data
  const telemetry = useFlowState((s) => s.telemetry[descriptorId])

  const stalled = telemetry?.stalled === true
  const errors = telemetry?.errors ?? 0
  const hasErrors = errors > 0

  return (
    <div
      className={cn(
        'relative w-[190px] h-[64px] rounded-md bg-surface-50 border border-slate-800',
        'px-3 py-2 flex flex-col justify-between shadow-md transition-shadow',
        'border-l-4',
        selected && 'ring-2 ring-accent-info/70',
        stalled && 'ring-2 ring-accent-critical',
      )}
      style={{ borderLeftColor: KIND_BORDER[kind] }}
      data-testid={`flow-node-${descriptorId}`}
      title={`${KIND_LABEL[kind]} · ${descriptorId}`}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!h-2 !w-2 !bg-slate-600 !border-slate-900"
      />

      {/* Header — status dot + label */}
      <div className="flex items-center gap-2 min-w-0">
        <span
          className="h-2 w-2 rounded-full shrink-0"
          style={{ backgroundColor: STATE_DOT[state] }}
          title={state}
        />
        <span className="text-[12px] leading-tight text-slate-200 truncate font-medium">
          {label}
        </span>
      </div>

      {/* Footer — subkind + live telemetry */}
      <div className="flex items-center justify-between gap-2 min-w-0">
        <span className="text-[10px] text-slate-500 truncate">
          {subkind ?? KIND_LABEL[kind]}
        </span>
        <span className="flex items-center gap-1.5 shrink-0 text-[10px] tabular-nums">
          {kind === 'source' && telemetry?.ratePerSec != null && (
            <span className="text-slate-400">{formatRate(telemetry.ratePerSec)}/s</span>
          )}
          {kind === 'analyst' && telemetry?.fires != null && (
            <span className="text-violet-300">▲{telemetry.fires}</span>
          )}
          {hasErrors && (
            <span className="flex items-center gap-0.5 text-accent-critical font-medium">
              <TriangleAlert className="h-2.5 w-2.5" aria-hidden />
              {errors}
            </span>
          )}
        </span>
      </div>

      {stalled && (
        <span
          className="absolute -top-2 -right-1 flex items-center gap-0.5 rounded-sm bg-accent-critical/90 px-1 py-px text-[9px] font-semibold uppercase tracking-wide text-white"
          data-testid={`flow-node-stalled-${descriptorId}`}
        >
          <AlertTriangle className="h-2.5 w-2.5" aria-hidden />
          stalled
        </span>
      )}

      <Handle
        type="source"
        position={Position.Right}
        className="!h-2 !w-2 !bg-slate-600 !border-slate-900"
      />
    </div>
  )
}

/** Compact rate: integers stay whole, sub-1 rates keep one decimal. */
function formatRate(rate: number): string {
  if (!Number.isFinite(rate)) return '0'
  if (rate >= 10) return String(Math.round(rate))
  if (rate >= 1) return rate.toFixed(1).replace(/\.0$/, '')
  return rate.toFixed(2).replace(/0$/, '')
}

export const FlowNode = memo(FlowNodeImpl)
export default FlowNode
