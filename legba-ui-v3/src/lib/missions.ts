/**
 * Missions — a stance, plus the three axes a stance never carried.
 *
 * ## What a mission is
 *
 * `lib/workspaces.ts` seeds PANELS. That is half of what the operator means
 * when they say "put me in crisis mode": the other half is WHAT THE WALL IS
 * ABOUT (the target scope), HOW FAR BACK IT LOOKS (the temporal window) and
 * WHICH LAYERS ARE DRAWN (the map's layer selection). Those three lived in
 * three separate stores that nothing set together, so switching stances moved
 * the tab strip and left the map pointed at whatever the last click pointed it
 * at.
 *
 * A mission is therefore ONE object:
 *
 *     panel set  +  targetScope  +  timeWindow  +  layers[]  +  the Consult tile
 *
 * and it is named for the reader's JOB rather than for the arrangement —
 * Morning Read, Desk Watch, Crisis, Release. Choosing one moves the map's scope
 * and the scrubber's window, not just the tab strip, and leaves the Consult
 * tile open with the ambient scope pin already set
 * (`lib/consultContext.ts::SCOPE_PIN_ORIGIN`), so switching to Crisis does not
 * orphan the morning's session.
 *
 * ## What a mission is NOT
 *
 *  * **Not a seventh stance.** The six stances and their `Alt+1…6` keys are
 *    untouched; a mission NAMES one of them and adds the tiles that stance's
 *    seed does not carry. Its layout persists in that stance's own slot, so a
 *    mission never gets a storage key of its own to drift out of date.
 *  * **Not a route.** It rides the existing hash as `#mission=<id>`, beside
 *    `#sel=`, `#scope=` and `#ws=`.
 *  * **Not a scope INVENTOR.** `targetScope` is a DECLARATION of what the
 *    mission wants to be about, resolved at switch time against what the reader
 *    has actually selected ({@link resolveMissionScope}). Desk Watch with no
 *    desk selected leaves the scope exactly as it was and SAYS SO; it does not
 *    pick a desk, and it does not scope to nothing and call that a desk.
 *
 * Everything here is pure — no store reads, no fetch, no React — so the four
 * missions, their resolution rules and their derived description lines are
 * unit-tested without a DOM. The imperative half (which stores get written, in
 * which order) is `lib/applyMission.ts`.
 */

import type { PanelKind } from '@/types'
import type { PresetPlacement } from '@/lib/layoutPresets'
import type { WorldLayer } from '@/v4/world/worldState'
import type { Scope } from '@/state/scope'
import type { Selection } from '@/state/selection'
import { emptyMembers } from '@/state/scope'
import { findWorkspace, type WorkspaceId } from '@/lib/workspaces'

/** Stable ids — they key the `#mission=` hash param. */
export type MissionId = 'morning_read' | 'desk_watch' | 'crisis' | 'release'

/** `Scope.origin` stamped on a scope a mission set. */
export const MISSION_SCOPE_ORIGIN = 'mission'

/**
 * What the mission wants the wall to be about.
 *
 *  * `world` — the whole roster. Clears the scope, which is not the same as
 *    "filter to nothing": an absent scope filters nothing anywhere.
 *  * `selected_target` — the ONE desk the reader has chosen. Resolved, never
 *    invented.
 *  * `selected_situation` — the situation the reader has chosen, and through it
 *    the desks it touches.
 *  * `unscoped` — this mission is about the machine, not about a place, so it
 *    leaves whatever the wall was about alone rather than clearing it.
 */
export type MissionScope =
  | { kind: 'world' }
  | { kind: 'selected_target' }
  | { kind: 'selected_situation' }
  | { kind: 'unscoped' }

/** The temporal aperture a mission sets on the map and the scrubber. */
export interface MissionWindow {
  /** Outer span in ms — `useWorldState.setSpan` re-anchors [now−span, now]. */
  spanMs: number
  /** The span with its unit, rendered verbatim. */
  label: string
}

export interface MissionDef {
  id: MissionId
  /** The mission's name, in the chooser and on the bar's active chip. */
  label: string
  /** The one-line description in the chooser — the reader's job, not the layout. */
  description: string
  /** The stance whose panel seed this mission mounts. */
  workspace: WorkspaceId
  /**
   * Tiles the mission needs that its stance's seed does NOT carry, as the same
   * ordered `{kind, position}` placements a stance seed uses.
   *
   * Opened through the same singleton opener as everything else, so mode gating
   * drops one identically (`system.eval_scorecard` is `personal`-only, and
   * Release in `cis` mode simply shows one tile fewer), a kind the stance
   * ALREADY has is focused rather than duplicated, and a placement whose
   * reference panel is not in the dock is re-anchored rather than handed to
   * Dockview as a position naming a tile it never saw.
   *
   * The positions matter: `system.consult` opened with no position joins the
   * ACTIVE group as a tab, which would put the conversation on top of the very
   * feed the mission exists to read.
   */
  panels: PresetPlacement[]
  targetScope: MissionScope
  timeWindow: MissionWindow
  /**
   * The map layers this mission draws; every other layer goes off. An EMPTY
   * list means the mission makes no layer claim and the layer selection is left
   * exactly as the reader left it — it does not mean "all layers off".
   */
  layers: WorldLayer[]
}

const HOUR = 3_600_000
const DAY = 24 * HOUR

/**
 * The four missions.
 *
 * Each one's window and layer set is the aperture the job actually needs: a
 * crisis is read at six hours because that is the pace a situation moves at,
 * and a desk watch at seven days because a desk's findings arrive nightly.
 */
export const MISSIONS: readonly MissionDef[] = [
  {
    id: 'morning_read',
    label: 'Morning Read',
    description: "The day's read — what moved, what it means, and where the evidence ran out.",
    workspace: 'morning_read',
    // The Morning Read stance already seeds the read, the map, the timeline and
    // Consult — but a saved arrangement may have closed the conversation, and
    // every mission owes the reader its Consult tile. Listing it here focuses
    // the one that is there and re-opens the one that is not.
    panels: [{ kind: 'system.consult' }],
    targetScope: { kind: 'world' },
    timeWindow: { spanMs: DAY, label: '24h' },
    layers: ['signals', 'findings', 'situations', 'events'],
  },
  {
    id: 'desk_watch',
    label: 'Desk Watch',
    description: 'One desk, end to end — its findings, its timeline, its provenance.',
    workspace: 'desk',
    // The Desk stance is feed · inspector · timeline · provenance. The mission
    // adds the map beside the timeline and docks the conversation to the right
    // of the detail rail rather than tabbing it over the feed.
    panels: [
      { kind: 'v4.map', position: { referencePanel: 'system.timeline', direction: 'right' } },
      {
        kind: 'system.consult',
        position: { referencePanel: 'system.inspector', direction: 'right' },
      },
    ],
    targetScope: { kind: 'selected_target' },
    timeWindow: { spanMs: 7 * DAY, label: '7d' },
    layers: ['signals', 'findings', 'events'],
  },
  {
    id: 'crisis',
    label: 'Crisis',
    description: 'A situation and the desks it touches, at the pace it is moving.',
    workspace: 'investigate',
    // The Investigate stance tabs Consult BEHIND `system.entities`, where it
    // opens hidden (see the note in lib/workspaces.ts). Listing it here brings
    // it to the front of that group, which is the whole reason a crisis mission
    // has a Consult clause at all.
    panels: [
      { kind: 'v4.map', position: { referencePanel: 'system.entities', direction: 'right' } },
      { kind: 'system.timeline', position: { referencePanel: 'v4.map', direction: 'within' } },
      {
        kind: 'system.consult',
        position: { referencePanel: 'system.entities', direction: 'within' },
      },
    ],
    targetScope: { kind: 'selected_situation' },
    timeWindow: { spanMs: 6 * HOUR, label: '6h' },
    layers: ['signals', 'situations', 'events'],
  },
  {
    id: 'release',
    label: 'Release',
    description: 'Is what it produced any good — the scorecard, the judge and the grader roster.',
    workspace: 'trust',
    // The Trust stance carries the gauge, the judge, source health and the eval
    // boards; the scorecard — band calibration and the grader roster — is the
    // one the release question is actually asked of.
    panels: [
      {
        kind: 'system.eval_scorecard',
        position: { referencePanel: 'system.eval_boards', direction: 'within' },
      },
      {
        kind: 'system.consult',
        position: { referencePanel: 'system.source_health', direction: 'within' },
      },
    ],
    // The eval set is over the whole roster and is never filtered to one desk,
    // so this mission leaves the wall's scope alone instead of clearing it: the
    // reader keeps the desk they were on when they came to check the release.
    targetScope: { kind: 'unscoped' },
    timeWindow: { spanMs: 30 * DAY, label: '30d' },
    // No map in this stance — the mission makes no layer claim rather than
    // silently switching every layer off.
    layers: [],
  },
]

/** Lookup by id; undefined for an unknown id. */
export function findMission(id: string): MissionDef | undefined {
  return MISSIONS.find((m) => m.id === id)
}

/** Whether a string is one of the four mission ids (hash / storage validation). */
export function isMissionId(id: string): id is MissionId {
  return MISSIONS.some((m) => m.id === id)
}

/** The mission whose stance is `ws`, if any — the bar's active-chip lookup. */
export function missionForWorkspace(ws: WorkspaceId): MissionDef | undefined {
  return MISSIONS.find((m) => m.workspace === ws)
}

/**
 * The mission's whole panel set: its stance's seed, plus the tiles it adds.
 *
 * Derived rather than restated, so a change to a stance's seed cannot leave a
 * mission describing a layout it no longer mounts. Order is seed-first, and a
 * kind the stance already seeds is never listed twice.
 */
export function missionPanels(m: MissionDef): PanelKind[] {
  const seeded = findWorkspace(m.workspace)?.seed.map((p) => p.kind) ?? []
  const out = [...seeded]
  for (const placement of m.panels) {
    if (!out.includes(placement.kind)) out.push(placement.kind)
  }
  return out
}

/** The scope declaration in words — the chooser's, and the resolution's, noun. */
export function missionScopeText(s: MissionScope): string {
  switch (s.kind) {
    case 'world':
      return 'the whole roster'
    case 'selected_target':
      return 'the desk you have selected'
    case 'selected_situation':
      return 'the situation you have selected'
    case 'unscoped':
      return 'whatever the wall is already about'
  }
}

/**
 * The "what this mission shows" line.
 *
 * Composed from the object's own fields — the tile count, the scope
 * declaration, the window with its unit, the layer list — so it cannot drift
 * from what choosing the mission actually does. An empty layer list prints as
 * the claim it is (none), never as "no layers".
 */
export function missionShows(m: MissionDef): string {
  const tiles = missionPanels(m).length
  return [
    `${tiles} tile${tiles === 1 ? '' : 's'}`,
    `scoped to ${missionScopeText(m.targetScope)}`,
    `${m.timeWindow.label} window`,
    m.layers.length > 0
      ? `map layers: ${m.layers.join(', ')}`
      : 'map layers: left as you set them',
    'Consult pinned to the scope',
  ].join(' · ')
}

// ---------------------------------------------------------------------------
// Scope resolution — declaration → what actually happens, with the reason.
// ---------------------------------------------------------------------------

/** What applying the mission does to the scope store. */
export type MissionScopeAction = 'clear' | 'set' | 'keep'

export interface MissionScopeResolution {
  action: MissionScopeAction
  /** The scope to set. Non-null exactly when `action === 'set'`. */
  scope: Scope | null
  /** Why this happened, in one clause. Rendered — never swallowed. */
  note: string
}

/** The live posture a resolution is decided against. */
export interface MissionScopeInputs {
  /** What the wall is currently about. */
  scope: Scope | null
  /** What the reader currently has selected (FOCUS). */
  selection: Selection | null
}

/**
 * Turn a mission's scope DECLARATION into what will actually happen.
 *
 * Two rules, and they are the whole point of the type:
 *
 *   1. **Nothing is invented.** A mission that wants a desk and finds none
 *      KEEPS the current scope and says the reader has not picked one. It does
 *      not guess a desk, and it does not clear the scope and let an empty wall
 *      read as "this desk has nothing".
 *   2. **A resolved scope is DEGRADED, deliberately.** `members` are projected
 *      from a payload (`lib/scopeFromReport`), which is a fetch this pure
 *      module will not make. An empty member set filters nothing (`scopeParams`
 *      pushes no parameter, `inScope` matches everything), so the reader lands
 *      on the right thing with nothing hidden and the Navigator re-projects the
 *      full world when its rows arrive — exactly the contract the `#scope=`
 *      share-link restore already keeps.
 */
export function resolveMissionScope(
  m: MissionDef,
  inputs: MissionScopeInputs,
): MissionScopeResolution {
  const { scope, selection } = inputs
  switch (m.targetScope.kind) {
    case 'world':
      return {
        action: 'clear',
        scope: null,
        note: 'scoped to the whole roster — no desk filter',
      }
    case 'unscoped':
      return {
        action: 'keep',
        scope: null,
        note: scope
          ? `left scoped to ${scope.label} — this mission is about the machine, not a desk`
          : 'unscoped — this mission is about the machine, not a desk',
      }
    case 'selected_target':
      return resolveFrom(m, scope, selection, 'target', 'desk')
    case 'selected_situation':
      return resolveFrom(m, scope, selection, 'situation', 'situation')
  }
}

function resolveFrom(
  m: MissionDef,
  scope: Scope | null,
  selection: Selection | null,
  kind: 'target' | 'situation',
  noun: string,
): MissionScopeResolution {
  if (scope && scope.kind === kind) {
    return {
      action: 'keep',
      scope: null,
      note: `already scoped to ${noun} ${scope.label}`,
    }
  }
  if (selection && selection.kind === kind) {
    const label = selection.label ?? selection.id
    return {
      action: 'set',
      scope: {
        kind,
        id: selection.id,
        label,
        members: emptyMembers(),
        origin: MISSION_SCOPE_ORIGIN,
      },
      note: `scoped to the ${noun} you had selected — ${label}`,
    }
  }
  return {
    action: 'keep',
    scope: null,
    note: `no ${noun} selected — ${m.label} left the wall's scope as it was`,
  }
}
