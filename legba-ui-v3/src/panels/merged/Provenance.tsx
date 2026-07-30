/**
 * Provenance (`system.provenance`) — U-3 merge set 2.
 *
 * Folds three surfaces that were all answering "where did this come from /
 * how does it connect" into one sidebar row with tabs:
 *   - Why      → the selection-driven lineage/ego-graph room (`v4.why`)
 *   - Lineage  → the `derived_from` DAG walker (`system.lineage`)
 *   - Flow     → the live registry canvas (`v4.flow`)
 *
 * All three children render UNMODIFIED — this wrapper only adds the tab strip
 * above them, mounting them inside `PanelEmbedProvider` so `LineagePanel`'s own
 * (otherwise-standalone) `PanelChrome` header/border stays suppressed — this
 * wrapper's header is the ONLY chrome that renders (the "double chrome" fix;
 * `WhyPanel` / `FlowPanel` don't use `PanelChrome` so are unaffected either
 * way). `v4.why` / `system.lineage` / `v4.flow` stay registered (hidden from
 * the sidebar) pointing at their original components — see panel-registry/
 * registry.ts HIDDEN_KINDS — so a saved layout referencing any of the old ids
 * keeps resolving exactly as before.
 */
import { useState } from 'react'
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import WhyPanel from '@/panels/v4/WhyPanel'
import LineagePanel from '@/panels/system/Lineage'
import FlowPanel from '@/panels/v4/FlowPanel'

type Tab = 'why' | 'lineage' | 'flow'

const TABS: readonly PanelTabDef[] = [
  { id: 'why', label: 'Why' },
  { id: 'lineage', label: 'Lineage' },
  { id: 'flow', label: 'Flow' },
]

export default function ProvenanceMerged(props: PanelProps) {
  const [tab, setTab] = useState<Tab>('why')
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Provenance</span>
        <PanelTabStrip
          tabs={TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="Provenance surface"
          testIdPrefix="provenance-tab"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'why' && <WhyPanel />}
          {tab === 'lineage' && <LineagePanel {...props} />}
          {tab === 'flow' && <FlowPanel />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
