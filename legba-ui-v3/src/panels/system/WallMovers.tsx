/**
 * WallMovers (`system.wall_movers`) — U-4 (COHERENCE_WAVES_PLAN_2026-07-28):
 * "boot into what changed."
 *
 * A hostile UX review found the cold-boot grid answered "what's happening
 * now" (KPI strip, live feed, world map, world assessment, timeline) but
 * never "what moved while I was away" — the Wall's own movers-since-last-
 * visit quadrant existed and worked, but was reachable only as an opt-in
 * preset or a sidebar row, never on the first screenful.
 *
 * This panel is a standalone mount of JUST that quadrant (not the whole
 * 2×2 Wall — the Wall's other three quadrants: world-at-a-glance band grid,
 * newest-verified, health corner — already have close analogues on the
 * default boot grid in the World Map, the Live Feed, and the KPI strip
 * respectively, so mounting the full Wall there would be redundant AND, at
 * 1920×1080 with the map/feed/report columns already filling the screen,
 * would force a cramped extra split). Movers-since-last-visit has no such
 * analogue anywhere else on boot — this tile is the one genuinely missing
 * surface, sized to slot in as a slim full-width band under the KPI strip
 * (see `App.tsx`'s boot effect + `lib/layoutPresets.ts`'s
 * `DEFAULT_BOOT_LAYOUT`).
 *
 * Shares `useSince` + `MoversContent` with the full Wall (`Wall.tsx`) so
 * both read/advance the SAME `legba_wall_cursor` — opening this tile and
 * later opening the full Wall never disagree about "since when."
 */
import { PanelChrome } from '@/components/PanelChrome'
import { MoversContent, useSince } from './Wall'
import { relTime } from '@/lib/evalOps'
import type { PanelProps } from '@/types'

export default function WallMoversPanel({ registration }: PanelProps) {
  const { visit, data, isLoading, error, refetch } = useSince()
  const sinceLabel = visit.firstVisit ? 'first visit — last 24h' : `since ${relTime(visit.cursor)}`
  return (
    <PanelChrome
      registration={registration}
      subtitle={`what changed ${sinceLabel}`}
      onRefresh={() => refetch()}
    >
      <div className="h-full min-h-[120px] overflow-y-auto p-2" data-testid="wall-movers-strip">
        <MoversContent
          since={data}
          cursor={visit.cursor}
          firstVisit={visit.firstVisit}
          loading={isLoading}
          error={error}
        />
      </div>
    </PanelChrome>
  )
}
