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
import { resolveKind, rewriteSerializedLayout } from '@/panel-registry/aliases'

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
        // U-3 merge — alert_center is now the "Triggers" tab of the merged
        // Alerts & Watches surface; point the preset at the visible survivor,
        // not the hidden original (both still render, but this is the one
        // the sidebar actually shows).
        kind: 'system.alerts_watches',
        position: { referencePanel: 'system.inspector', direction: 'below' },
      },
    ],
  },
  {
    id: 'workspace',
    label: 'Workspace',
    description: 'The combined intel desk — live feed, Inspector detail, Provenance, and Consult.',
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
        // U-3 merge — v4.why is now the "Why" tab of the merged Provenance
        // surface; point the preset at the visible survivor.
        kind: 'system.provenance',
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
    description: 'Optimizer, eval scorecard, and the consult workbench (chat + deep-analysis toggle).',
    panels: [
      { kind: 'system.optimizer' },
      {
        kind: 'system.eval_scorecard',
        position: { referencePanel: 'system.optimizer', direction: 'below' },
      },
      {
        // U-3 merge — Deep Consult is now a depth toggle ON this same panel,
        // so the preset no longer opens a separate system.deep_consult tile.
        kind: 'system.consult',
        position: { referencePanel: 'system.optimizer', direction: 'right' },
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

/**
 * THE COLD-BOOT SEED MOVED (UI_HOLISTIC_DESIGN_2026-08-24 §3.1).
 *
 * `DEFAULT_BOOT_LAYOUT` — the S7-T2 mission-control grid plus U-4's movers
 * band — is superseded by the MORNING READ workspace (`lib/workspaces.ts`),
 * which the shell seeds on first ready. The stance model made "the layout you
 * land in" a property of a named workspace rather than a constant beside the
 * preset list, and the Wall (whose own quadrant IS movers-since-last-visit)
 * replaced the standalone `system.wall_movers` tile the old grid mounted
 * beside its own parent.
 *
 * Everything below this line — the named presets, and the single-slot
 * Save/Restore the sidebar's Layouts menu drives — is UNCHANGED and still
 * live. Workspaces are additive: they own `legba_ws`, this owns
 * `legba_layout_custom`, and neither writes the other's key.
 */

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
    // Skip kinds that resolve to nothing, defensively — a preset must never
    // crash the shell if a kind is later renamed/removed. A RETIRED kind still
    // resolves (through the alias table), so a preset written before a merge
    // train keeps opening the survivor.
    if (!resolveKind(placement.kind)) continue
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

/**
 * Restore the saved layout for `mode`; returns true if one was applied.
 *
 * The saved blob may predate a retirement, so it goes through the alias
 * pre-pass first (design §4.4 call site 3 / §6.2): stale ids resolve onto their
 * survivor, duplicates collapse onto one tile, unresolvable ids are dropped
 * before Dockview — which would throw on an unknown component — ever sees
 * them. A layout that holds nothing renderable returns false rather than
 * replacing the operator's live dock with an empty one.
 */
export function loadCustomLayout(api: DockviewApi, mode: string): boolean {
  try {
    const all = readCustomStore()
    const data = all[mode]
    if (!data) return false
    const rewrite = rewriteSerializedLayout(data)
    if (rewrite.empty) return false
    api.fromJSON(rewrite.layout)
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
