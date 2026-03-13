import { useEventTimeseries, useEntityDistribution, useSourceHealth, useCyclePerformance } from '@/api/hooks'

// Recharts integration — Phase 5
// Placeholder showing data availability

export function AnalyticsPanel() {
  const { data: timeseries } = useEventTimeseries(30)
  const { data: entities } = useEntityDistribution()
  const { data: sources } = useSourceHealth()
  const { data: cycles } = useCyclePerformance(50)

  return (
    <div className="p-4 space-y-4">
      <h3 className="text-sm font-medium">Analytics Dashboard</h3>
      <div className="grid grid-cols-2 gap-3">
        <div className="bg-card border border-border rounded-lg p-3">
          <p className="text-xs text-muted-foreground mb-1">Event Timeseries</p>
          <p className="text-lg font-semibold">{timeseries?.length ?? 0} days loaded</p>
          <p className="text-xs text-muted-foreground">Recharts line chart — Phase 5</p>
        </div>
        <div className="bg-card border border-border rounded-lg p-3">
          <p className="text-xs text-muted-foreground mb-1">Entity Distribution</p>
          <p className="text-lg font-semibold">{entities?.length ?? 0} types</p>
          <p className="text-xs text-muted-foreground">Recharts pie chart — Phase 5</p>
        </div>
        <div className="bg-card border border-border rounded-lg p-3">
          <p className="text-xs text-muted-foreground mb-1">Source Health</p>
          <p className="text-lg font-semibold">{sources?.length ?? 0} sources</p>
          <p className="text-xs text-muted-foreground">Recharts bar chart — Phase 5</p>
        </div>
        <div className="bg-card border border-border rounded-lg p-3">
          <p className="text-xs text-muted-foreground mb-1">Cycle Performance</p>
          <p className="text-lg font-semibold">{cycles?.length ?? 0} cycles</p>
          <p className="text-xs text-muted-foreground">Recharts area chart — Phase 5</p>
        </div>
      </div>
    </div>
  )
}
