/**
 * Layout presets — restore v2's named Dockview arrangements.
 *
 * A preset is an ordered list of singleton panel placements: the first
 * entry is the anchor; later entries split relative to an earlier panel's
 * id (its `PanelKind`, which doubles as the Dockview panel id for
 * singletons — see App.tsx `addSingleton`).
 *
 * Applying a preset clears the workspace and re-seeds it through the same
 * singleton-opener App.tsx uses for its boot grid, so preset panels are
 * indistinguishable from hand-opened ones. Panels whose `modes` exclude
 * the active mode are skipped silently (the opener already enforces this).
 *
 * Custom layouts (whatever the operator has dragged into place) round-trip
 * through Dockview's own `toJSON()`/`fromJSON()` serializer and persist to
 * localStorage — see `saveCustomLayout` / `loadCustomLayout`.
 */

import type { DockviewApi, SerializedDockview } from 'dockview-react'
import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'

/** Relative placement for a non-anchor preset panel. */
export type PresetDirection = 'right' | 'left' | 'above' | 'below' | 'within'

export interface PresetPlacement {
  kind: PanelKind
  /** Omitted for the anchor panel; required otherwise. */
  position?: { referencePanel: PanelKind; direction: PresetDirection }
}

export interface LayoutPreset {
  id: string
  label: string
  description: string
  panels: PresetPlacement[]
}

/**
 * The v2 named presets. Each uses only non-binding singleton kinds so it
 * can be seeded without a descriptor binding. The opener skips any kind
 * whose `modes` don't include the active mode, so a preset is a superset
 * and degrades gracefully per mode (e.g. cis hides operator-only panels).
 */
export const LAYOUT_PRESETS: LayoutPreset[] = [
  {
    id: 'wall',
    label: 'Wall',
    description:
      'The mission-control anchor — the Wall tile (world at a glance + movers since last visit) with the Inspector beside it.',
    // P1-7: OPTIONAL preset only — the default boot grid is unchanged; the
    // operator opts in. The Inspector rides right so the Wall's finding /
    // desk / situation rows have somewhere to land.
    panels: [
      { kind: 'system.wall' },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.wall', direction: 'right' },
      },
    ],
  },
  {
    id: 'monitoring',
    label: 'Monitoring',
    description: 'Live findings, the Inspector, and the target registry — the daily-driver grid.',
    panels: [
      { kind: 'system.findings' },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.findings', direction: 'right' },
      },
      {
        // Roster collapsed into the Target Registry in Wave A (#90) — point the
        // preset at the canonical Registry panel, not the hidden roster dupe.
        kind: 'registry.targets',
        position: { referencePanel: 'system.findings', direction: 'below' },
      },
      {
        kind: 'system.alert_center',
        position: { referencePanel: 'system.inspector', direction: 'below' },
      },
    ],
  },
  {
    id: 'workspace',
    label: 'Workspace',
    description: 'The combined intel desk — live feed, Inspector detail, the Why graph, and Consult.',
    // Operator's "pretty awesome" view (#90): scan the feed (anchor), drill the
    // selection in the Inspector, see its provenance in the Why graph, and ask
    // the substrate in Consult — one 2×2 desk, all brushed by the shared selection.
    panels: [
      { kind: 'system.findings' },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.findings', direction: 'right' },
      },
      {
        kind: 'system.consult',
        position: { referencePanel: 'system.findings', direction: 'below' },
      },
      {
        kind: 'v4.why',
        position: { referencePanel: 'system.inspector', direction: 'below' },
      },
    ],
  },
  {
    id: 'investigation',
    label: 'Investigation',
    description: 'Findings + the Inspector (detail/drill) + entities and search.',
    panels: [
      { kind: 'system.findings' },
      {
        kind: 'system.inspector',
        position: { referencePanel: 'system.findings', direction: 'right' },
      },
      {
        kind: 'system.entities',
        position: { referencePanel: 'system.findings', direction: 'below' },
      },
      {
        kind: 'system.search',
        position: { referencePanel: 'system.entities', direction: 'within' },
      },
    ],
  },
  {
    id: 'analysis',
    label: 'Analysis',
    description: 'Optimizer, eval scorecard, deep + interactive consult workbench.',
    panels: [
      { kind: 'system.optimizer' },
      {
        kind: 'system.eval_scorecard',
        position: { referencePanel: 'system.optimizer', direction: 'below' },
      },
      {
        kind: 'system.consult',
        position: { referencePanel: 'system.optimizer', direction: 'right' },
      },
      {
        kind: 'system.deep_consult',
        position: { referencePanel: 'system.consult', direction: 'below' },
      },
    ],
  },
  {
    id: 'operations',
    label: 'Operations',
    description: 'Runtime health, dead-letter, stream lag, and governor events.',
    panels: [
      { kind: 'system.actor_health' },
      {
        kind: 'system.dead_letter',
        position: { referencePanel: 'system.actor_health', direction: 'right' },
      },
      {
        kind: 'system.stream_lag',
        position: { referencePanel: 'system.actor_health', direction: 'below' },
      },
      {
        kind: 'system.governor',
        position: { referencePanel: 'system.dead_letter', direction: 'below' },
      },
    ],
  },
  {
    id: 'focus',
    label: 'Focus (Zen)',
    description: 'Single-record deep read — just the Inspector, full canvas. Centered.',
    // Move 6 Zen/centered focus mode: the Inspector alone, no surrounding tiles,
    // for an undistracted single-record read. The current selection drives it;
    // every referenced id is still a RecordLink, so drilling continues from here.
    panels: [{ kind: 'system.inspector' }],
  },
]

/** Lookup by id; undefined for an unknown id. */
export function findPreset(id: string): LayoutPreset | undefined {
  return LAYOUT_PRESETS.find((p) => p.id === id)
}

/**
 * Opener signature shared with App.tsx's `addSingleton`. Returning the
 * applier from here keeps App as the single owner of how a singleton is
 * mounted (params shape, mode gating) — we only sequence the calls.
 */
export type SingletonOpener = (
  kind: PanelKind,
  position?: { referencePanel: PanelKind; direction: PresetDirection },
) => void

/**
 * Apply a preset: clear the current layout, then seed each placement via
 * the supplied opener. Unknown ids are a no-op. The anchor (no position)
 * goes first so later splits have something to reference.
 */
export function applyPreset(api: DockviewApi, preset: LayoutPreset, open: SingletonOpener): void {
  api.clear()
  for (const placement of preset.panels) {
    // Skip kinds not in the registry defensively — a preset must never
    // crash the shell if a kind is later renamed/removed.
    if (!PANEL_REGISTRY[placement.kind]) continue
    open(placement.kind, placement.position)
  }
}

// ---------------------------------------------------------------------------
// Custom layout persistence — Dockview's own serializer + localStorage.
// ---------------------------------------------------------------------------

const CUSTOM_LAYOUT_KEY = 'legba_layout_custom'

/** Serialize the live Dockview layout and stash it under `mode`. */
export function saveCustomLayout(api: DockviewApi, mode: string): void {
  try {
    const all = readCustomStore()
    all[mode] = api.toJSON()
    localStorage.setItem(CUSTOM_LAYOUT_KEY, JSON.stringify(all))
  } catch {
    // localStorage may be unavailable (private mode / quota) — saving a
    // layout is best-effort and must not surface as an error.
  }
}

/** Restore the saved layout for `mode`; returns true if one was applied. */
export function loadCustomLayout(api: DockviewApi, mode: string): boolean {
  try {
    const all = readCustomStore()
    const data = all[mode]
    if (!data) return false
    api.fromJSON(data)
    return true
  } catch {
    return false
  }
}

/** Whether a saved custom layout exists for `mode`. */
export function hasCustomLayout(mode: string): boolean {
  return readCustomStore()[mode] != null
}

function readCustomStore(): Record<string, SerializedDockview> {
  try {
    const raw = localStorage.getItem(CUSTOM_LAYOUT_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, SerializedDockview>
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}
