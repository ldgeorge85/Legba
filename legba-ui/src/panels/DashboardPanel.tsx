import { useDashboard, useEventTimeseries, useEvents } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { cn, categoryColor } from '@/lib/utils'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Badge } from '@/components/common/Badge'
import {
  Newspaper,
  Users,
  Globe,
  Target,
  AlertTriangle,
  Eye,
  Network,
  FileText,
  Download,
  FlaskConical,
  BookOpen,
} from 'lucide-react'

const SEVERITY_COLORS: Record<string, string> = {
  critical: 'bg-red-500/20 text-red-400 border-red-500/30',
  high: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  medium: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  low: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  info: 'bg-slate-500/20 text-slate-400 border-slate-500/30',
}

interface StatCardProps {
  label: string
  value: number | string
  icon: React.ReactNode
  onClick?: () => void
}

function StatCard({ label, value, icon, onClick }: StatCardProps) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-3 p-3 rounded-lg bg-card border border-border hover:border-primary/30 transition-colors text-left"
    >
      <div className="p-2 rounded-md bg-primary/10 text-primary">{icon}</div>
      <div>
        <p className="text-2xl font-semibold">{value}</p>
        <p className="text-xs text-muted-foreground">{label}</p>
      </div>
    </button>
  )
}

export function DashboardPanel() {
  const { data, isLoading } = useDashboard()
  const { data: timeseries } = useEventTimeseries(7)
  const { data: recentEvents } = useEvents({ limit: 5 })
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  if (isLoading) {
    return (
      <div className="p-6 space-y-4">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="h-20 bg-card border border-border rounded-lg animate-pulse" />
        ))}
      </div>
    )
  }

  if (!data) {
    return <div className="p-6 text-muted-foreground">Failed to load dashboard data</div>
  }

  return (
    <div className="p-4 space-y-4 max-w-5xl">
      {/* KPI Grid */}
      <div className="grid grid-cols-4 gap-3">
        <StatCard label="Signals" value={data.signals} icon={<Newspaper size={18} />} onClick={() => openPanel('signals')} />
        <StatCard label="Events" value={data.events} icon={<Newspaper size={18} />} onClick={() => openPanel('events')} />
        <StatCard label="Entities" value={data.entities} icon={<Users size={18} />} onClick={() => openPanel('entities')} />
        <StatCard label="Sources" value={data.sources} icon={<Globe size={18} />} onClick={() => openPanel('sources')} />
        <StatCard label="Goals" value={data.goals} icon={<Target size={18} />} onClick={() => openPanel('goals')} />
        <StatCard label="Situations" value={data.situations} icon={<AlertTriangle size={18} />} onClick={() => openPanel('situations')} />
        <StatCard label="Watchlist" value={data.watchlist} icon={<Eye size={18} />} onClick={() => openPanel('watchlist')} />
        <StatCard label="Relationships" value={data.relationships} icon={<Network size={18} />} onClick={() => openPanel('graph')} />
        <StatCard label="Facts" value={data.facts} icon={<FileText size={18} />} />
        <StatCard label="Hypotheses" value={data.hypotheses ?? 0} icon={<FlaskConical size={18} />} onClick={() => openPanel('hypotheses')} />
        <StatCard label="Briefs" value={data.briefs ?? 0} icon={<BookOpen size={18} />} onClick={() => openPanel('briefs')} />
      </div>

      {/* Ingestion Service Status */}
      {data.ingestion && (
        <div className="bg-card border border-border rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Download size={14} className="text-muted-foreground" />
            <h3 className="text-sm font-medium">Ingestion Service</h3>
            <span className={cn(
              'inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full',
              data.ingestion.active
                ? 'bg-emerald-500/10 text-emerald-400'
                : 'bg-zinc-500/10 text-zinc-400'
            )}>
              <span className={cn(
                'w-1.5 h-1.5 rounded-full',
                data.ingestion.active ? 'bg-emerald-400 animate-pulse' : 'bg-zinc-500'
              )} />
              {data.ingestion.active ? 'Running' : 'Offline'}
            </span>
          </div>
          <div className="grid grid-cols-3 gap-3 text-center">
            <div>
              <p className="text-lg font-semibold">{data.ingestion.signals_1h}</p>
              <p className="text-[10px] text-muted-foreground">Signals / 1h</p>
            </div>
            <div>
              <p className="text-lg font-semibold">{data.ingestion.signals_24h}</p>
              <p className="text-[10px] text-muted-foreground">Signals / 24h</p>
            </div>
            <div>
              <p className={cn('text-lg font-semibold', data.ingestion.errors_1h > 0 && 'text-amber-400')}>
                {data.ingestion.errors_1h}
              </p>
              <p className="text-[10px] text-muted-foreground">Errors / 1h</p>
            </div>
          </div>
        </div>
      )}

      {/* Recent Events */}
      <div className="bg-card border border-border rounded-lg p-3">
        <h3 className="text-sm font-medium mb-2">Recent Events</h3>
        {(!recentEvents || recentEvents.items.length === 0) ? (
          <p className="text-sm text-muted-foreground">No derived events yet</p>
        ) : (
          <div className="space-y-1">
            {recentEvents.items.map((ev) => (
              <div
                key={ev.event_id}
                className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-secondary cursor-pointer text-sm"
                onClick={() => openPanel('event-detail', { id: ev.event_id })}
              >
                {ev.severity ? (
                  <span className={cn(
                    'inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded border',
                    SEVERITY_COLORS[ev.severity.toLowerCase()] ?? SEVERITY_COLORS.info
                  )}>
                    {ev.severity}
                  </span>
                ) : (
                  <span className="inline-flex px-1.5 py-0.5 text-[10px] text-muted-foreground">--</span>
                )}
                <span className="flex-1 truncate">{ev.title}</span>
                <span className="text-[10px] text-muted-foreground shrink-0">{ev.signal_count} signals</span>
                <Badge className={cn('text-[10px]', categoryColor(ev.category))}>
                  {ev.category}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Recent Signals */}
      <div className="bg-card border border-border rounded-lg p-3">
        <h3 className="text-sm font-medium mb-2">Recent Signals</h3>
        <div className="space-y-1">
          {(data.recent_signals ?? []).slice(0, 10).map((event) => (
            <div
              key={event.event_id}
              className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-secondary cursor-pointer text-sm"
              onClick={() => select({ type: 'signal', id: event.event_id, name: event.title })}
            >
              <Badge className={cn('text-[10px]', categoryColor(event.category))}>
                {event.category}
              </Badge>
              <span className="flex-1 truncate">{event.title}</span>
              <TimeAgo date={event.timestamp} className="text-xs text-muted-foreground shrink-0" />
            </div>
          ))}
        </div>
      </div>

      {/* Active Situations */}
      {(data.active_situations ?? []).length > 0 && (
        <div className="bg-card border border-border rounded-lg p-3">
          <h3 className="text-sm font-medium mb-2">Active Situations</h3>
          <div className="space-y-1">
            {data.active_situations.map((sit) => (
              <div
                key={sit.situation_id}
                className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-secondary cursor-pointer text-sm"
                onClick={() => openPanel('situations')}
              >
                <AlertTriangle size={14} className="text-amber-400 shrink-0" />
                <span className="flex-1 truncate">{sit.title}</span>
                <span className="text-xs text-muted-foreground">{sit.event_count} events</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Mini sparkline area for event volume */}
      {timeseries && timeseries.length > 0 && (
        <div className="bg-card border border-border rounded-lg p-3">
          <h3 className="text-sm font-medium mb-2">Event Volume (7d)</h3>
          <div className="flex items-end gap-1 h-16">
            {timeseries.map((day, i) => {
              const max = Math.max(...timeseries.map((d) => d.total), 1)
              const height = (day.total / max) * 100
              return (
                <div
                  key={i}
                  className="flex-1 bg-primary/30 rounded-t hover:bg-primary/50 transition-colors"
                  style={{ height: `${height}%` }}
                  title={`${day.date}: ${day.total} events`}
                />
              )
            })}
          </div>
          <div className="flex justify-between text-[10px] text-muted-foreground mt-1">
            <span>{timeseries[0]?.date}</span>
            <span>{timeseries[timeseries.length - 1]?.date}</span>
          </div>
        </div>
      )}
    </div>
  )
}
