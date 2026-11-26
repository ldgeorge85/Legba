/**
 * surfaceDock — the Dockview half of the arrangement model: the PANEL SIDE of
 * the frame (U-6; the stage and the chat are the frame's own, not Dockview's).
 *
 * `lib/surfaces.ts` says WHAT a page's panel side seeds (its regions and their
 * views); `lib/frameGrid.ts` says how many columns the width affords; this
 * module lays it into a Dockview api and keeps it constrained:
 *
 *   - {@link seedSurface} clears the dock and lays the regions down by
 *     {@link placeRegions}: one region per column while columns last (the lead
 *     first, widths by weight), the rest stacked under the last column — each
 *     region one group, its views tabs in that group, the first in front;
 *   - {@link openInRegion} is "opening replaces content in its home region":
 *     the opened view joins the region's group as its ONE opened tab, closing
 *     the previous opened tab there; it never adds a column;
 *   - {@link dockedRegionCount} feeds the drop guard that refuses a split
 *     beyond {@link MAX_REGIONS} (App.tsx).
 *
 * The opener is App's `addSingleton`-shaped function, so a seeded view is
 * indistinguishable from an opened one (mode gating, alias resolution).
 */
import type { DockviewApi, IDockviewPanel } from 'dockview-react'
import { MAX_REGIONS, isSlotId, type RegionId, type SurfaceDef, type ViewId } from '@/lib/surfaces'
import { placeRegions, type RegionPlacement } from '@/lib/frameGrid'

/** Where a view is placed relative to one already in the dock. */
export type ViewPosition = { referencePanel: string; direction: 'left' | 'right' | 'above' | 'below' | 'within' }

/**
 * Add one view. `seeded` views get the close-less tab; the opener returns the
 * panel, or undefined when the view does not ship in this mode.
 */
export type ViewOpener = (
  view: ViewId,
  position: ViewPosition | undefined,
  opts: { seeded: boolean; inactive?: boolean },
) => IDockviewPanel | undefined

/** The params key that marks a view as the region's ONE opened (non-seeded) view. */
export const TRANSIENT_PARAM = 'transient'

/** The panel id each region's first live view has — the region's anchor. */
export function regionAnchors(api: Pick<DockviewApi, 'getPanel'>, def: SurfaceDef): Partial<Record<RegionId, string>> {
  const out: Partial<Record<RegionId, string>> = {}
  for (const r of def.regions) {
    const live = r.views.find((v) => api.getPanel(v))
    if (live) out[r.id] = live
  }
  return out
}

/** Where each region of `def` goes for `columns` columns. */
export function regionPlan(def: SurfaceDef, columns: number): RegionPlacement<RegionId>[] {
  return placeRegions(def.regions, columns)
}

/**
 * Lay `def`'s panel side into the dock for `columns` columns. Returns the view
 * ids actually opened, in order. A region whose views all fail (mode gating)
 * is skipped and a later region that hung off it hangs off the first one.
 */
export function seedSurface(api: DockviewApi, def: SurfaceDef, open: ViewOpener, columns = 1): ViewId[] {
  api.clear()
  const opened: ViewId[] = []
  const anchors: Partial<Record<RegionId, string>> = {}
  const plan = regionPlan(def, columns)
  /** The anchor of each column's top region, by column. */
  const columnTop: string[] = []
  for (const place of plan) {
    const region = def.regions.find((r) => r.id === place.id)!
    let anchor: string | undefined
    for (const view of region.views) {
      let position: ViewPosition | undefined
      if (anchor) {
        position = { referencePanel: anchor, direction: 'within' }
      } else if (place.row > 0 && columnTop[place.column]) {
        position = { referencePanel: columnTop[place.column], direction: 'below' }
      } else if (place.column > 0) {
        const ref = columnTop[place.column - 1] ?? opened[0]
        if (ref) position = { referencePanel: ref, direction: 'right' }
      } else if (opened[0]) {
        position = { referencePanel: opened[0], direction: 'right' }
      }
      const panel = open(view, position, { seeded: true, inactive: !!anchor })
      if (!panel) continue
      opened.push(view)
      if (!anchor) anchor = view
    }
    if (anchor) {
      anchors[region.id] = anchor
      if (place.row === 0) columnTop[place.column] = anchor
    }
  }
  // Each region's first view in front.
  for (const region of def.regions) {
    const front = anchors[region.id]
    if (front) api.getPanel(front)?.api.setActive()
  }
  return opened
}

/** Pin the column widths and row heights after a seed (best-effort; a resize must never break it). */
export function sizeSurface(api: DockviewApi, def: SurfaceDef, columns = 1): void {
  const anchors = regionAnchors(api, def)
  const width = api.width || 1200
  const height = api.height || 800
  for (const place of regionPlan(def, columns)) {
    const id = anchors[place.id]
    const panel = id ? api.getPanel(id) : undefined
    if (!panel) continue
    try {
      if (place.row === 0 && place.widthShare < 1) panel.api.setSize({ width: Math.round(width * place.widthShare) })
      if (place.heightShare < 1) panel.api.setSize({ height: Math.round(height * place.heightShare) })
    } catch {
      // sizing is best-effort
    }
  }
}

/** Grid regions in the dock (floating, popped-out groups are not regions). */
export function dockedRegionCount(api: Pick<DockviewApi, 'groups'>): number {
  return api.groups.filter((g) => g.api.location.type === 'grid').length
}

/**
 * Open `view` in `region` as the region's one OPENED view, replacing the
 * previous one there. Falls back to the active group, then any group, when
 * the region's anchor is gone (the reader moved things). Returns the panel.
 */
export function openInRegion(
  api: DockviewApi,
  def: SurfaceDef,
  region: RegionId,
  add: (position: ViewPosition | undefined) => IDockviewPanel | undefined,
  /** The id about to be opened: an already-open view with it is kept, not replaced. */
  keepId?: string,
): IDockviewPanel | undefined {
  const anchorId = regionAnchors(api, def)[region]
  const anchor = anchorId ? api.getPanel(anchorId) : api.activePanel ?? api.panels[0]
  if (anchor) {
    for (const p of [...anchor.group.panels]) {
      const params = p.params as Record<string, unknown> | undefined
      if (params?.[TRANSIENT_PARAM] && p.id !== keepId) p.api.close()
    }
  }
  const panel = add(anchor ? { referencePanel: anchor.id, direction: 'within' } : undefined)
  panel?.api.setActive()
  return panel
}

/** True when a drop at this place would add a region the surface may not have. */
export function dropWouldExceedRegions(
  api: Pick<DockviewApi, 'groups'>,
  kind: string,
  position: string,
): boolean {
  if (position === 'center') return false
  if (kind !== 'content' && kind !== 'edge') return false
  return dockedRegionCount(api) >= MAX_REGIONS
}

export { isSlotId }
