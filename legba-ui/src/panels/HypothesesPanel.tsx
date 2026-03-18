import { useState } from 'react'
import { useHypotheses } from '@/api/hooks'
import type { HypothesisSummary } from '@/api/types'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { FlaskConical, Check, Circle } from 'lucide-react'

const STATUS_OPTIONS = ['all', 'active', 'confirmed', 'refuted', 'stale'] as const
type StatusFilter = (typeof STATUS_OPTIONS)[number]

const STATUS_COLORS: Record<string, string> = {
  active: 'bg-green-500/20 text-green-400 border-green-500/30',
  confirmed: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  refuted: 'bg-red-500/20 text-red-400 border-red-500/30',
  stale: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
}

export function HypothesesPanel() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('active')
  const queryStatus = statusFilter === 'all' ? '' : statusFilter
  const { data, isLoading } = useHypotheses(queryStatus)

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <FlaskConical size={14} className="text-muted-foreground" />
        <span className="text-xs font-medium text-muted-foreground">ACH</span>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
          className="ml-auto text-xs bg-secondary border border-border rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary"
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s === 'all' ? 'All statuses' : s.charAt(0).toUpperCase() + s.slice(1)}
            </option>
          ))}
        </select>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto p-2 space-y-2">
        {isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">Loading...</div>
        ) : !data?.length ? (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3">
            <FlaskConical size={32} className="opacity-30" />
            <p className="text-sm text-center max-w-[280px]">
              No hypotheses yet. SYNTHESIZE cycles create competing hypothesis pairs.
            </p>
          </div>
        ) : (
          data.map((h) => <HypothesisCard key={h.id} hypothesis={h} />)
        )}
      </div>

      {/* Footer */}
      <div className="flex items-center px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
        <span>{data ? `${data.length} hypothesis${data.length !== 1 ? ' pairs' : ''}` : '--'}</span>
      </div>
    </div>
  )
}

function HypothesisCard({ hypothesis: h }: { hypothesis: HypothesisSummary }) {
  const support = h.support_count ?? 0
  const refute = h.refute_count ?? 0
  const total = support + refute
  const supportPct = total > 0 ? (support / total) * 100 : 50

  return (
    <div className="rounded-lg border border-border bg-card p-3 space-y-2.5">
      {/* Header row: situation + status */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground truncate">
          {h.situation_name ?? 'Unlinked'}
        </span>
        <StatusBadge status={h.status} />
      </div>

      {/* Thesis */}
      <div className="space-y-1">
        <div className="flex items-start gap-1.5">
          <span className="text-[10px] font-semibold text-green-400 mt-0.5 shrink-0">H1</span>
          <p className="text-sm text-foreground leading-snug">{h.thesis}</p>
        </div>
        <div className="flex items-start gap-1.5">
          <span className="text-[10px] font-semibold text-red-400 mt-0.5 shrink-0">H2</span>
          <p className="text-sm text-muted-foreground leading-snug">{h.counter_thesis}</p>
        </div>
      </div>

      {/* Evidence balance bar */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-[10px] text-muted-foreground">
          <span>
            <span className="text-green-400 font-medium">{support}</span> supporting
          </span>
          <span>
            refuting <span className="text-red-400 font-medium">{refute}</span>
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-secondary overflow-hidden flex">
          {total > 0 ? (
            <>
              <div
                className="bg-green-500/70 transition-all duration-300"
                style={{ width: `${supportPct}%` }}
              />
              <div
                className="bg-red-500/70 transition-all duration-300"
                style={{ width: `${100 - supportPct}%` }}
              />
            </>
          ) : (
            <div className="w-full bg-secondary" />
          )}
        </div>
      </div>

      {/* Diagnostic evidence checklist */}
      {h.diagnostic_evidence && h.diagnostic_evidence.length > 0 && (
        <div className="space-y-1 pt-0.5">
          <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">
            Diagnostics
          </span>
          <ul className="space-y-0.5">
            {h.diagnostic_evidence.map((de, i) => (
              <li key={i} className="flex items-start gap-1.5 text-xs">
                {de.observed ? (
                  <Check size={12} className="text-green-400 mt-0.5 shrink-0" />
                ) : (
                  <Circle size={12} className="text-muted-foreground/50 mt-0.5 shrink-0" />
                )}
                <span
                  className={cn(
                    'leading-snug',
                    de.observed ? 'text-foreground' : 'text-muted-foreground'
                  )}
                >
                  {de.description}
                  <span className="text-[10px] text-muted-foreground ml-1">
                    (proves {de.proves})
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Cycle info */}
      <div className="text-[10px] text-muted-foreground">
        Created cycle {h.created_cycle}, last evaluated cycle {h.last_evaluated_cycle}
      </div>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const color = STATUS_COLORS[status.toLowerCase()] ?? STATUS_COLORS.stale
  return (
    <Badge className={cn('text-[10px]', color)}>
      {status}
    </Badge>
  )
}
