import { useState } from 'react'
import { useSituations } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { AlertTriangle } from 'lucide-react'

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

export function SituationsPanel() {
  const [statusFilter, setStatusFilter] = useState('')
  const { data, isLoading } = useSituations({ status: statusFilter || undefined })
  const openPanel = useWorkspaceStore((s) => s.openPanel)
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
            {data.map((sit) => (
              <div
                key={sit.situation_id}
                className="flex items-center gap-2 px-3 py-2 rounded hover:bg-secondary cursor-pointer border border-border/50"
                onClick={() => {
                  select({ type: 'situation', id: sit.situation_id, name: sit.title })
                  openPanel('situations', { id: sit.situation_id })
                }}
              >
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
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
