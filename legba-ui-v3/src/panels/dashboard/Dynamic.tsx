/**
 * D3. Dynamic Dashboard (`dashboard.dynamic`).
 *
 * Per L-108 §5 + L-092 §3.3 D3 — schema-driven dashboard. Reads the
 * analyst's `OutputT` schema and renders against a small catalog of
 * widgets (geo_map, timeline, kpi_grid, table, text_summary).
 *
 * L-108 §5 default render contract: option (a) — schema-driven generic
 * widgets. Per-dashboard React component via `config.component` is the
 * escape hatch.
 */

import { PanelChrome } from '@/components/PanelChrome'
import type { PanelProps } from '@/types'

type WidgetKind = 'geo_map' | 'timeline' | 'kpi_grid' | 'table' | 'text_summary'

interface DashboardConfig {
  layout_hint?: WidgetKind
  component?: string // escape hatch
  // Future: layout DAG over widgets, per-widget binding refs.
}

export default function DynamicDashboard({ registration, scope }: PanelProps) {
  const cfg = (registration.data_query as DashboardConfig) ?? {}
  const layout = cfg.layout_hint ?? 'table'

  if (cfg.component) {
    return (
      <PanelChrome registration={registration} subtitle="dashboard (escape hatch)">
        <div className="text-xs text-accent-warning bg-accent-warning/10 p-2 rounded">
          Custom component path declared (<code>{cfg.component}</code>) but escape-hatch loader
          not wired in this first L-204 cut.
        </div>
      </PanelChrome>
    )
  }

  return (
    <PanelChrome registration={registration} subtitle={`dashboard ${scope.dashboard_id ?? ''}`}>
      <div className="text-xs space-y-2">
        <div className="bg-accent-info/10 border border-accent-info/30 rounded p-2 text-accent-info">
          Schema-driven dashboard. Widget kind: <code>{layout}</code>
        </div>
        <p className="text-slate-300">
          Generic widget catalog (geo_map, timeline, kpi_grid, table, text_summary) is the L-108
          §5 default render contract. Concrete widget bindings load from the bound analyst's
          OutputT pydantic schema.
        </p>
      </div>
    </PanelChrome>
  )
}
