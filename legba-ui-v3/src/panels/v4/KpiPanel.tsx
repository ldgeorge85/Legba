/**
 * v4 panel — the mission-control KPI strip (S7-T2).
 *
 * The glance-state stat tiles (signals / findings / active situations / active
 * sources) with band-change deltas, lifted OUT of the map into their own thin
 * strip panel per the UI direction ("stat tiles OUT of the map into the KPI
 * strip"). Shipped as the top strip of the default mission-control layout.
 */
import { PanelBoundary } from './PanelBoundary'
import KpiStrip from '@/v4/world/KpiStrip'

export default function KpiPanel() {
  return (
    <PanelBoundary>
      <div className="h-full w-full overflow-hidden bg-surface-300">
        <KpiStrip />
      </div>
    </PanelBoundary>
  )
}
