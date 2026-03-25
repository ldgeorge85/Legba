import { useState } from 'react'
import { useSituations, useSituation } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { AlertTriangle, ChevronDown, ChevronRight, ExternalLink } from 'lucide-react'

const STATUSES = ['active', 'resolved', 'escalating']

function severityColor(severity: string) {
  switch (severity) {
    case 'critical': return 'bg-red-500/20 text-red-400'
    case 'high': return 'bg-orange-500/20 text-orange-400'
    case 'medium': return 'bg-amber-500/20 text-amber-400'
    case 'low': return 'bg-green-500/20 text-green-400'
    default: return 'bg-secondary text-muted-foreground'
  }
}

/** Inline detail section shown when a situation card is expanded */
function SituationDetail({ situationId }: { situationId: string }) {
  const { data, isLoading } = useSituation(situationId)
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  if (isLoading) return <div className="px-3 py-2 text-xs text-muted-foreground">Loading details...</div>
  if (!data) return <div className="px-3 py-2 text-xs text-muted-foreground">Failed to load details</div>

  return (
    <div className="px-3 py-2 space-y-2 border-t border-border/30 bg-secondary/30">
      {data.description && (
        <p className="text-xs text-muted-foreground whitespace-pre-wrap">{data.description}</p>
      )}
      {data.events?.length > 0 && (
        <div>
          <p className="text-[10px] font-medium text-muted-foreground mb-1">Linked Events ({data.events.length})</p>
          <div className="space-y-1">
            {data.events.map((evt) => (
              <button
                key={evt.event_id}
                className="flex items-center gap-1.5 w-full text-left text-xs px-2 py-1 rounded hover:bg-secondary transition-colors"
                onClick={(e) => {
                  e.stopPropagation()
                  openPanel('event-detail', { id: evt.event_id })
                }}
              >
                <ExternalLink size={10} className="text-muted-foreground shrink-0" />
                <span className="truncate">{evt.title}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export function SituationsPanel() {
  const [statusFilter, setStatusFilter] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const { data, isLoading } = useSituations({ status: statusFilter || undefined })
  const select = useSelectionStore((s) => s.select)

  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>

  return (
    <div className="flex flex-col h-full">
      {/* Status filter */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="text-sm bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
        >
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      {/* List */}
      <div className="flex-1 overflow-auto">
        {!data?.length ? (
          <div className="p-4 text-sm text-muted-foreground">No situations tracked</div>
        ) : (
          <div className="space-y-1 p-2">
            {data.map((sit) => {
              const isExpanded = expandedId === sit.situation_id
              return (
                <div
                  key={sit.situation_id}
                  className="rounded border border-border/50 overflow-hidden"
                >
                  <div
                    className="flex items-center gap-2 px-3 py-2 hover:bg-secondary cursor-pointer"
                    onClick={() => {
                      select({ type: 'situation', id: sit.situation_id, name: sit.title })
                      setExpandedId(isExpanded ? null : sit.situation_id)
                    }}
                  >
                    {isExpanded
                      ? <ChevronDown size={14} className="text-muted-foreground shrink-0" />
                      : <ChevronRight size={14} className="text-muted-foreground shrink-0" />
                    }
                    <AlertTriangle size={14} className="text-amber-400 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate">{sit.title}</p>
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <Badge className={`text-[10px] ${severityColor(sit.severity)}`}>{sit.severity}</Badge>
                        <span>{sit.event_count} events</span>
                        <TimeAgo date={sit.updated_at} />
                      </div>
                    </div>
                    <Badge className={sit.status === 'active' ? 'bg-blue-500/20 text-blue-400 text-[10px]' : 'bg-secondary text-muted-foreground text-[10px]'}>
                      {sit.status}
                    </Badge>
                  </div>
                  {isExpanded && <SituationDetail situationId={sit.situation_id} />}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
