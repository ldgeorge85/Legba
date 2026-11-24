/**
 * System Status (`system.status`) — UI-B1/merges, UI-D1 Table A "Engine:
 * Status · Production · Failures · Actors · Queues".
 *
 * Five engine reads that are looked at ONE INSTEAD OF THE OTHER (holistic §4.1's
 * merge test), so they are tabs of one kind:
 *   - Status     → the per-layer health page (the original `system.status`)
 *   - Production → the whole-engine production gauge (`system.production_gauge`)
 *   - Failures   → the dead-letter inspector (`system.dead_letter`)
 *   - Actors     → the actor roster and its last errors (`system.actor_health`)
 *   - Queues     → per-consumer JetStream lag (`system.stream_lag`)
 *
 * The four retired ids resolve here through `panel-registry/aliases.ts`, each
 * onto the tab that IS its surface. Every child renders UNMODIFIED inside
 * `PanelEmbedProvider`, so this wrapper's header is the only chrome (the
 * children keep their functional controls — refresh, filters — per
 * `PanelChrome`'s embedded branch).
 */
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import SystemStatusPanel from '@/panels/system/SystemStatus'
import ProductionGaugePanel from '@/panels/system/ProductionGauge'
import DeadLetterPanel from '@/panels/system/DeadLetter'
import ActorHealthPanel from '@/panels/system/ActorHealth'
import StreamLagPanel from '@/panels/system/StreamLag'
import { useMergedTab } from './useMergedTab'

type Tab = 'status' | 'production' | 'failures' | 'actors' | 'queues'

export const STATUS_TABS: readonly PanelTabDef[] = [
  { id: 'status', label: 'Status' },
  { id: 'production', label: 'Production' },
  { id: 'failures', label: 'Failures' },
  { id: 'actors', label: 'Actors' },
  { id: 'queues', label: 'Queues' },
]

export default function StatusMerged(props: PanelProps) {
  const [tab, setTab] = useMergedTab<Tab>('system.status', props.initialTab, STATUS_TABS, 'status')
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">System Status</span>
        <PanelTabStrip
          tabs={STATUS_TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="System status surface"
          testIdPrefix="status-tab"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'status' && <SystemStatusPanel {...props} />}
          {tab === 'production' && <ProductionGaugePanel {...props} />}
          {tab === 'failures' && <DeadLetterPanel {...props} />}
          {tab === 'actors' && <ActorHealthPanel {...props} />}
          {tab === 'queues' && <StreamLagPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
