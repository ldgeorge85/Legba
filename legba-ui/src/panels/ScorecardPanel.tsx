import { useScorecard } from '@/api/hooks'
import { cn } from '@/lib/utils'
import { TimeAgo } from '@/components/common/TimeAgo'
import { EntityLink } from '@/components/EntityLink'

function MetricCard({
  label,
  value,
  subtitle,
  color,
}: {
  label: string
  value: string | number
  subtitle?: string
  color?: 'green' | 'yellow' | 'red' | 'default'
}) {
  const colorClass =
    color === 'green'
      ? 'text-emerald-400'
      : color === 'yellow'
        ? 'text-amber-400'
        : color === 'red'
          ? 'text-red-400'
          : 'text-gray-100'

  return (
    <div className="bg-card border border-border rounded-lg p-3">
      <p className="text-xs text-muted-foreground mb-1">{label}</p>
      <p className={cn('text-2xl font-semibold', colorClass)}>{value}</p>
      {subtitle && <p className="text-xs text-muted-foreground mt-0.5">{subtitle}</p>}
    </div>
  )
}

function BarList({ title, data }: { title: string; data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1])
  const max = entries.length > 0 ? entries[0][1] : 1

  return (
    <div className="bg-card border border-border rounded-lg p-3">
      <h3 className="text-sm font-medium mb-2">{title}</h3>
      <div className="space-y-1.5">
        {entries.map(([name, count]) => (
          <div key={name} className="flex items-center gap-2 text-sm">
            <span className="w-28 shrink-0 truncate text-muted-foreground" title={name}>
              {name.replace(/_/g, ' ')}
            </span>
            <div className="flex-1 h-4 bg-secondary rounded overflow-hidden">
              <div
                className="h-full bg-primary/40 rounded"
                style={{ width: `${(count / max) * 100}%` }}
              />
            </div>
            <span className="w-12 text-right text-xs text-muted-foreground tabular-nums">{count}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function rateColor(value: number, greenThreshold: number, yellowThreshold: number): 'green' | 'yellow' | 'red' {
  if (value >= greenThreshold) return 'green'
  if (value >= yellowThreshold) return 'yellow'
  return 'red'
}

export function ScorecardPanel() {
  const { data, isLoading } = useScorecard()

  if (isLoading) {
    return (
      <div className="p-6 space-y-4">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="h-20 bg-card border border-border rounded-lg animate-pulse" />
        ))}
      </div>
    )
  }

  if (!data) {
    return <div className="p-6 text-muted-foreground">Failed to load scorecard data</div>
  }

  const d = data.data
  const sourceTotal = Object.values(d.source_health).reduce((a, b) => a + b, 0)
  const topEntities = Object.entries(d.top_entities)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)

  const healthColors: Record<string, string> = {
    active: 'bg-emerald-500',
    error: 'bg-red-500',
    retired: 'bg-gray-500',
    paused: 'bg-amber-500',
  }

  return (
    <div className="p-4 space-y-4 max-w-5xl overflow-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">System Scorecard</h2>
        <span className="text-xs text-muted-foreground">
          Last updated: cycle {data.cycle},{' '}
          <TimeAgo date={d.timestamp} className="text-xs text-muted-foreground" />
        </span>
      </div>

      {/* Row 1: Key Metrics */}
      <div className="grid grid-cols-4 gap-3">
        <MetricCard
          label="Entity Link Rate"
          value={`${d.entity_link_rate.toFixed(1)}%`}
          color={rateColor(d.entity_link_rate, 30, 15)}
        />
        <MetricCard
          label="Fact Freshness"
          value={`${d.fact_freshness_pct.toFixed(1)}%`}
          subtitle={`${d.stale_facts} stale facts`}
          color={rateColor(d.fact_freshness_pct, 80, 50)}
        />
        <MetricCard
          label="Knowledge Graph"
          value={`${d.graph.nodes.toLocaleString()}`}
          subtitle={`${d.graph.edges.toLocaleString()} edges`}
        />
        <MetricCard
          label="Sources"
          value={`${d.source_health.active ?? 0}`}
          subtitle={`active / ${sourceTotal} total`}
        />
      </div>

      {/* Row 2: Coverage */}
      <div className="grid grid-cols-2 gap-3">
        <BarList title="Coverage by Category" data={d.coverage_by_category} />
        <BarList title="Coverage by Region" data={d.coverage_by_region} />
      </div>

      {/* Row 3: Source Health */}
      <div className="bg-card border border-border rounded-lg p-3">
        <h3 className="text-sm font-medium mb-2">Source Health</h3>
        {/* Stacked bar */}
        <div className="flex h-6 rounded overflow-hidden mb-2">
          {Object.entries(d.source_health).map(([status, count]) =>
            count > 0 ? (
              <div
                key={status}
                className={cn('h-full', healthColors[status] ?? 'bg-gray-600')}
                style={{ width: `${(count / sourceTotal) * 100}%` }}
                title={`${status}: ${count}`}
              />
            ) : null,
          )}
        </div>
        {/* Legend chips */}
        <div className="flex gap-4 flex-wrap">
          {Object.entries(d.source_health).map(([status, count]) => (
            <div key={status} className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className={cn('w-2.5 h-2.5 rounded-sm', healthColors[status] ?? 'bg-gray-600')} />
              <span>
                {status}: {count}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* Row 4: Top Entities */}
      <div className="bg-card border border-border rounded-lg p-3">
        <h3 className="text-sm font-medium mb-2">Top Entities</h3>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-muted-foreground border-b border-border">
              <th className="text-left py-1 font-medium">Entity</th>
              <th className="text-right py-1 font-medium">Events</th>
            </tr>
          </thead>
          <tbody>
            {topEntities.map(([name, count]) => (
              <tr key={name} className="border-b border-border/50 hover:bg-secondary/50">
                <td className="py-1"><EntityLink name={name} /></td>
                <td className="py-1 text-right tabular-nums text-muted-foreground">{count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
