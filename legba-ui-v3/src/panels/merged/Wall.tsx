/**
 * The Wall (`system.wall`) — UI-B1/merges, UI-D1 Table A "absorbs `v4.kpi`
 * (header band), `system.wall_movers` (tab Movers)".
 *
 *   - header band → the At-a-Glance counts strip (`v4.kpi`), always shown: it
 *                   is read AT THE SAME TIME as whichever tab is up, so it is a
 *                   band over the tabs rather than a tab of its own;
 *   - Overview    → the four-quadrant Wall (the original `system.wall`);
 *   - Movers      → the movers-since-last-visit quadrant mounted alone
 *                   (`system.wall_movers`, U-4).
 *
 * `v4.kpi` resolves here with no tab (the band is on every tab);
 * `system.wall_movers` resolves onto Movers. Every child renders UNMODIFIED;
 * the Wall's own `PanelChrome` keeps its refresh control in the embedded bar.
 */
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import WallPanel from '@/panels/system/Wall'
import WallMoversPanel from '@/panels/system/WallMovers'
import KpiPanel from '@/panels/v4/KpiPanel'
import { useMergedTab } from './useMergedTab'

type Tab = 'overview' | 'movers'

export const WALL_TABS: readonly PanelTabDef[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'movers', label: 'Movers' },
]

export default function WallMerged(props: PanelProps) {
  const [tab, setTab] = useMergedTab<Tab>('system.wall', props.initialTab, WALL_TABS, 'overview')
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">The Wall</span>
        <PanelTabStrip
          tabs={WALL_TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="Wall surface"
          testIdPrefix="wall-tab"
        />
      </div>
      <div className="shrink-0 border-b border-line" data-testid="wall-kpi-band">
        <KpiPanel />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'overview' && <WallPanel {...props} />}
          {tab === 'movers' && <WallMoversPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
