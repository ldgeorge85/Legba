/**
 * applyMission — the imperative half of a mission (`lib/missions.ts` is the
 * pure half).
 *
 * Choosing a mission writes FOUR stores in a fixed order, and the order is the
 * contract:
 *
 *   1. **SCOPE first.** The Consult panel auto-pins the ambient scope on mount
 *      and on every scope change (`panels/system/Consult.tsx`,
 *      `SCOPE_PIN_ORIGIN`). Setting the scope before the tiles open means the
 *      Consult tile the mission opens comes up ALREADY pinned rather than
 *      pinning a frame later — which is the difference between "the mission
 *      restores its Consult tile pinned" and "the pin appears if you wait".
 *   2. **The temporal window**, on the world store — which is the map's window
 *      AND the scrubber's, because they read the same `spanMs`. This is the
 *      axis that made a stance switch feel like a tab switch: the tab strip
 *      moved and the clock did not.
 *   3. **The layers**, wholesale, and only when the mission actually claims
 *      some. A mission with no map makes no layer claim, and blanking the map
 *      would be a claim.
 *   4. **The tiles** the stance's seed does not carry, through the caller's own
 *      singleton opener — the same one the sidebar, ⌘K and the workspace seeds
 *      use, so a mode-gated tile is dropped here identically and a mission tile
 *      is indistinguishable from a hand-opened one.
 *
 * The stance itself is NOT switched here. The caller (`App.tsx`) owns
 * "switching never destroys" — serializing the outgoing stance before the
 * incoming one is restored — and a mission must not fork that path.
 *
 * Everything this module writes is a zustand module singleton, so it takes no
 * store arguments and is exercised against the real stores.
 */

import type { PanelKind } from '@/types'
import type { PresetPlacement } from '@/lib/layoutPresets'
import { useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import {
  resolveMissionScope,
  type MissionDef,
  type MissionScopeResolution,
} from '@/lib/missions'

/** What a mission actually did, for the bar's caption and the tests. */
export interface MissionApplied {
  /** The scope decision and the sentence explaining it. */
  scope: MissionScopeResolution
  /** The window the map and the scrubber were moved to. */
  window: { startMs: number; endMs: number; label: string }
  /** The layers turned on, or `null` when the mission made no layer claim. */
  layers: string[] | null
  /** Tiles the mission asked for that the opener actually opened. */
  opened: PanelKind[]
}

/**
 * The opener signature — the SAME one `lib/workspaces.ts` seeds through, so a
 * mission tile is mode-gated identically and is indistinguishable from a
 * hand-opened one. Returns falsy when the kind was skipped (unregistered, or
 * gated out of the active mode).
 *
 * The caller is responsible for dropping a `position` whose reference panel is
 * not in the dock — the same re-anchoring rule `seedWorkspace` keeps.
 */
export type MissionOpener = (
  kind: PanelKind,
  position?: PresetPlacement['position'],
) => unknown | undefined

export interface ApplyMissionOptions {
  /** Injected so the window a mission sets is deterministic under test. */
  now?: number
  /**
   * Leave the scope store alone.
   *
   * Set on the BOOT path when the link carried its own `#scope=`: an explicit
   * address the sender chose beats a mission's default aperture, and a Morning
   * Read link that also names a desk must land on that desk rather than
   * clearing it on the way in. The decision is still computed and reported, so
   * the bar can say the link's scope was kept.
   */
  skipScope?: boolean
}

/**
 * Apply a mission's three axes and open its extra tiles. See the module doc for
 * why the order is what it is.
 */
export function applyMission(
  m: MissionDef,
  open: MissionOpener,
  opts: ApplyMissionOptions = {},
): MissionApplied {
  const now = opts.now ?? Date.now()
  // 1 — scope, resolved against what the reader actually has, never invented.
  const resolved = resolveMissionScope(m, {
    scope: useScope.getState().scope,
    selection: useSelection.getState().selection,
  })
  const scope: MissionScopeResolution = opts.skipScope
    ? {
        action: 'keep',
        scope: null,
        note: (() => {
          const cur = useScope.getState().scope
          return cur
            ? `the link's own scope was kept — ${cur.label}`
            : "the link's own scope was kept"
        })(),
      }
    : resolved
  if (scope.action === 'clear') useScope.getState().setScope(null)
  else if (scope.action === 'set' && scope.scope) useScope.getState().setScope(scope.scope)

  // 2 — the temporal window: the map's and the scrubber's, in one write.
  useWorldState.getState().setSpan(m.timeWindow.spanMs, now)

  // 3 — the layers, wholesale, and only when claimed.
  if (m.layers.length > 0) useWorldState.getState().setLayers(m.layers)

  // 4 — the tiles the stance's seed does not carry.
  const opened: PanelKind[] = []
  for (const placement of m.panels) {
    if (open(placement.kind, placement.position)) opened.push(placement.kind)
  }

  return {
    scope,
    window: {
      startMs: now - m.timeWindow.spanMs,
      endMs: now,
      label: m.timeWindow.label,
    },
    layers: m.layers.length > 0 ? [...m.layers] : null,
    opened,
  }
}
