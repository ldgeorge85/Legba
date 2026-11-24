/**
 * Provenance (`system.provenance`) — UI-D1 Table A "Evidence: Why · Lineage ·
 * Fan-out" (U-3 merge set 2, re-cut by UI-B1/merges).
 *
 * The surfaces that answer "where did this come from / how did it get here":
 *   - Why      → the selection-driven lineage/ego-graph room (`v4.why`)
 *   - Lineage  → the `derived_from` DAG walker (`system.lineage`)
 *   - Fan-out  → source → signal → finding (`source.fanout`, UI-B1/merges)
 *
 * What LEFT in UI-B1/merges (Table A: "loses Flow, Trajectory and
 * Narratives"): the registry canvas is `system.registry`'s Canvas tab, and the
 * situation register and narratives are `system.register`'s two tabs. Their
 * retired ids (`v4.flow`, `system.situations`, `system.narratives`) were
 * re-pointed in `panel-registry/aliases.ts`; a saved tab param naming one of
 * the old tabs falls back to Why (`initialTab`), never to an empty surface.
 *
 * All children render UNMODIFIED — this wrapper only adds the tab strip above
 * them, mounting them inside `PanelEmbedProvider` so a child's own
 * `PanelChrome` header stays suppressed and this wrapper's header is the ONLY
 * chrome (`WhyPanel` does not use `PanelChrome`).
 */
import { useEffect } from 'react'
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import WhyPanel from '@/panels/v4/WhyPanel'
import LineagePanel from '@/panels/system/Lineage'
import FanoutExplorerPanel from '@/panels/source/FanoutExplorer'
import { emitRead } from '@/lib/readTelemetry'
import { useMergedTab } from './useMergedTab'

type Tab = 'why' | 'lineage' | 'fanout'

export const PROVENANCE_TABS: readonly PanelTabDef[] = [
  { id: 'why', label: 'Why' },
  { id: 'lineage', label: 'Lineage' },
  { id: 'fanout', label: 'Fan-out' },
]

export default function ProvenanceMerged(props: PanelProps) {
  // The retired kinds (v4.why / system.lineage / source.fanout) resolve here
  // through panel-registry/aliases.ts, each naming the tab that IS its
  // surface; an open tile follows a later alias open (useMergedTab).
  const [tab, setTab] = useMergedTab<Tab>('system.provenance', props.initialTab, PROVENANCE_TABS, 'why')

  // READ TELEMETRY (D2e) — the lineage walk itself.
  //
  // PREMISE_REASON_TO_EXIST §2.2: "Receipt chains that are never walked...
  // 59,771 preserved artifacts backing a trust operation nobody performs."
  // That claim rested on Caddy logs, which cannot see a tab change inside a
  // mounted panel. This can.
  //
  // Only the WALK surfaces count: Why (the derivation graph) and Lineage (the
  // derived_from tracer) here, and the production-path canvas from its new
  // home (`system.registry`'s Canvas tab). Fan-out joined this wrapper in
  // UI-B1/merges and is NOT counted: widening the event's definition would
  // move the one number §2.2 is graded on without a decision to do so.
  useEffect(() => {
    if (tab === 'why' || tab === 'lineage') {
      emitRead('lineage_walk', { subjectKind: 'provenance_tab', subjectId: tab })
    }
  }, [tab])

  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Provenance</span>
        <PanelTabStrip
          tabs={PROVENANCE_TABS}
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
          {tab === 'fanout' && <FanoutExplorerPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
