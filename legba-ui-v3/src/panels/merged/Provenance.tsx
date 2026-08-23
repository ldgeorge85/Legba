/**
 * Provenance (`system.provenance`) — U-3 merge set 2, extended by GLASS-2.
 *
 * Folds the surfaces that all answer "where did this come from / how did it get
 * here" into one sidebar row with tabs:
 *   - Why        → the selection-driven lineage/ego-graph room (`v4.why`)
 *   - Lineage    → the `derived_from` DAG walker (`system.lineage`)
 *   - Flow       → the live registry canvas (`v4.flow`)
 *   - Trajectory → the situation register's frames + each frame's append-only
 *                  trajectory ledger (`system.situations`, GLASS-2)
 *   - Narratives → reified contested-claim families + the source-echo graph
 *                  (`system.narratives`, GLASS-2)
 *
 * The last two joined here rather than taking sidebar rows of their own: the
 * U-3 ≤23-visible-row budget (panel-registry/navGroups.test.ts) is spent to the
 * last row, and that test's stated options for the next surface are "earn it,
 * fold into a tab, or hide". They fold — and the fold is honest rather than
 * merely convenient, because both are provenance reads: a trajectory row is the
 * provenance of a situation's STATE (which findings moved it, dated by their
 * evidence), and a narrative is the provenance of a contested CLAIM's carriage
 * (who published it first, who followed).
 *
 * All five children render UNMODIFIED — this wrapper only adds the tab strip
 * above them, mounting them inside `PanelEmbedProvider` so a child's own
 * (otherwise-standalone) `PanelChrome` header/border stays suppressed and this
 * wrapper's header is the ONLY chrome that renders (the "double chrome" fix;
 * `WhyPanel` / `FlowPanel` don't use `PanelChrome` so are unaffected either
 * way). Every folded kind stays registered (hidden from the sidebar) pointing
 * at its original component — see panel-registry/registry.ts HIDDEN_KINDS — so
 * a saved layout or ⌘K deep-link referencing any of the ids keeps resolving.
 */
import { useState } from 'react'
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import WhyPanel from '@/panels/v4/WhyPanel'
import LineagePanel from '@/panels/system/Lineage'
import FlowPanel from '@/panels/v4/FlowPanel'
import SituationTrajectoryPanel from '@/panels/system/SituationTrajectory'
import NarrativesPanel from '@/panels/system/Narratives'

type Tab = 'why' | 'lineage' | 'flow' | 'trajectory' | 'narratives'

const TABS: readonly PanelTabDef[] = [
  { id: 'why', label: 'Why' },
  { id: 'lineage', label: 'Lineage' },
  { id: 'flow', label: 'Flow' },
  { id: 'trajectory', label: 'Trajectory' },
  { id: 'narratives', label: 'Narratives' },
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
          {tab === 'trajectory' && <SituationTrajectoryPanel {...props} />}
          {tab === 'narratives' && <NarrativesPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
