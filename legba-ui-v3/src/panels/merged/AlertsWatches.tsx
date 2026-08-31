/**
 * Alerts & Watches (`system.alerts_watches`) — U-3 merge set 3.
 *
 * Folds three alert-adjacent surfaces into one sidebar row with tabs:
 *   - Watches    → SERVER-side standing watches (`system.watchlist`, the live
 *                  entity/topic/place watches `alert_trigger_scan` evaluates)
 *   - Triggers   → the client-only findings-feed subscription tail
 *                  (`system.alert_center`) — a guarded PREVIEW surface (no
 *                  dedicated backend route yet), labeled as such on its tab.
 *   - Deliveries → did an escalation actually LAND? (`system.escalations`,
 *                  `GET /api/v1/v3/system/escalations`)
 *
 * All three children render UNMODIFIED, but embedded inside
 * `PanelEmbedProvider` so their own (otherwise-standalone) `PanelChrome`
 * header/border stays suppressed — this wrapper's own header above is the
 * ONLY chrome that renders (the "double chrome" fix). `system.watchlist` /
 * `system.alert_center` / `system.escalations` stay registered (hidden from
 * the sidebar) pointing at their original components — see panel-registry/
 * registry.ts HIDDEN_KINDS — so a saved layout referencing any of the old ids
 * keeps resolving exactly as before.
 */
import { useState } from 'react'
import type { PanelProps } from '@/types'
import { initialTab, PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelTierProvider } from '@/components/PanelTierContext'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import WatchlistPanel from '@/panels/system/Watchlist'
import AlertCenterPanel from '@/panels/system/AlertCenter'
import EscalationsPanel from '@/panels/system/Escalations'

type Tab = 'watches' | 'triggers' | 'deliveries'

const TABS: readonly PanelTabDef[] = [
  { id: 'watches', label: 'Watches' },
  { id: 'triggers', label: 'Triggers', badge: 'preview' },
  { id: 'deliveries', label: 'Deliveries' },
]

export default function AlertsWatchesMerged(props: PanelProps) {
  // system.watchlist / system.alert_center / system.escalations retired onto
  // the watches / triggers / deliveries tabs (aliases.ts).
  const [tab, setTab] = useState<Tab>(() => initialTab(props.initialTab, TABS, 'watches') as Tab)
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Alerts &amp; Watches</span>
        <PanelTabStrip
          tabs={TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="Alerts and watches surface"
          testIdPrefix="alerts-watches-tab"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'watches' && <WatchlistPanel {...props} />}
          {tab === 'triggers' && (
            // The alert_center read is still the guarded preview surface (no
            // dedicated backend route) — the "preview" badge already shows on
            // the tab itself (see TABS above); re-scoping the tier context
            // here no longer needs to reach a nested PanelChrome (suppressed
            // by PanelEmbedProvider), just keeps the value correct in case
            // AlertCenterPanel reads it for anything else.
            <PanelTierProvider tier="preview">
              <AlertCenterPanel {...props} />
            </PanelTierProvider>
          )}
          {tab === 'deliveries' && <EscalationsPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
