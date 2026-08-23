/**
 * Tests for the bundle-time registry table — every spec'd panel kind is
 * declared, every panel_id is unique, every singleton has scopeKey=null.
 */

import { describe, it, expect } from 'vitest'
import {
  PANEL_REGISTRY,
  PANEL_ID_TO_KIND,
  SINGLETON_PANELS,
} from './registry'
import type { PanelKind } from '@/types'

describe('PANEL_REGISTRY', () => {
  it('declares every L-092 panel kind', () => {
    // Per legba_ui_panels_v2.md §3 and legba_panel_registration.md §2,
    // minus the retired /predictions-backed panels (Target Hypotheses,
    // Predictor Forecasts) dropped with the /predictions read endpoint.
    const kinds = Object.keys(PANEL_REGISTRY)
    expect(kinds.length).toBeGreaterThanOrEqual(30)
  })

  it('has unique panel_ids', () => {
    const seen = new Set<string>()
    for (const entry of Object.values(PANEL_REGISTRY)) {
      expect(seen.has(entry.definition.panelId)).toBe(false)
      seen.add(entry.definition.panelId)
    }
  })

  it('maps panel_id back to its kind', () => {
    for (const [kind, entry] of Object.entries(PANEL_REGISTRY)) {
      expect(PANEL_ID_TO_KIND[entry.definition.panelId]).toBe(kind)
    }
  })

  it('singletons have scopeKey=null and requiresBinding=false', () => {
    for (const kind of SINGLETON_PANELS) {
      const def = PANEL_REGISTRY[kind].definition
      expect(def.scopeKey).toBe(null)
      expect(def.requiresBinding).toBe(false)
    }
  })

  it('per-target panels are scoped on target_id', () => {
    const targets = Object.entries(PANEL_REGISTRY).filter(
      ([, e]) => e.definition.category === 'target',
    )
    // Was 10 (T1-T10); Target Hypotheses retired with /predictions.
    expect(targets.length).toBeGreaterThanOrEqual(9)
    for (const [, entry] of targets) {
      expect(entry.definition.scopeKey).toBe('target_id')
      expect(entry.definition.requiresBinding).toBe(true)
    }
  })

  it('per-analyst panels are scoped on analyst_id', () => {
    const analysts = Object.entries(PANEL_REGISTRY).filter(
      ([, e]) => e.definition.category === 'analyst',
    )
    // Was 5 (A1-A5); Predictor Forecasts retired with /predictions.
    expect(analysts.length).toBe(4)
    for (const [, entry] of analysts) {
      expect(entry.definition.scopeKey).toBe('analyst_id')
    }
  })

  it('every panel declares at least one mode', () => {
    for (const entry of Object.values(PANEL_REGISTRY)) {
      expect(entry.definition.modes.length).toBeGreaterThan(0)
    }
  })

  it('operator panels are personal-only per L-108 §6', () => {
    const operator = Object.values(PANEL_REGISTRY).filter(
      (e) => e.definition.category === 'operator',
    )
    for (const entry of operator) {
      expect(entry.definition.modes).toEqual(['personal'])
    }
  })
})

/**
 * Layout-compat (COHERENCE_WAVES_PLAN_2026-07-28 §1.9 / §U-3 acceptance):
 * merging panels must keep every OLD panel id resolving — a saved Dockview
 * layout (or the `legba_nav_collapsed` sidebar state) that references a
 * pre-merge kind must still render something real, not an
 * UnboundPanelPlaceholder / `unknown_panel_id`.
 *
 * The alias mechanism is the SAME `HIDDEN_KINDS` set the codebase already
 * used for the S7-T2 consolidation (registry.ts): a merged-away kind stays a
 * full row in `PANEL_REGISTRY` — same panelId, same Component (its ORIGINAL,
 * unmodified one) — with only `definition.hidden = true` flipped, so it drops
 * out of the sidebar (`SINGLETON_PANELS`) but `PANEL_ID_TO_KIND` and
 * `resolvePanel` (panel-registry/loader.ts) still resolve it exactly as
 * before. ⌘K also still lists it (CommandPalette's `panelEntries` iterates
 * `PANEL_REGISTRY`, not `SINGLETON_PANELS`).
 */
describe('U-3 layout-compat — old panel ids still resolve after the merges', () => {
  // Every kind U-3 folded away behind a merged/tabbed survivor, per
  // COHERENCE_WAVES_PLAN_2026-07-28 §U-3's five merge sets.
  const MERGED_AWAY_KINDS: PanelKind[] = [
    'v4.timeline', // → Timeline's "Events" mode (survivor: system.timeline)
    'v4.why', // → Provenance's "Why" tab (survivor: system.provenance)
    'system.lineage', // → Provenance's "Lineage" tab
    'v4.flow', // → Provenance's "Flow" tab
    'system.alert_center', // → Alerts & Watches' "Triggers" tab (survivor: system.alerts_watches)
    'system.watchlist', // → Alerts & Watches' "Watches" tab
    'system.escalations', // → Alerts & Watches' "Deliveries" tab
    'system.deep_consult', // → Consult's depth toggle (survivor: system.consult, unchanged id)
    'system.entity_graph', // → Entities' "Graph" tab (survivor: system.entities, unchanged id)
    'system.notable_structure', // → Entities' "Structure" tab
  ]

  it('every merged-away kind is still a full registry row — hidden, not deleted', () => {
    for (const kind of MERGED_AWAY_KINDS) {
      const entry = PANEL_REGISTRY[kind]
      expect(entry, `${kind} must still exist in PANEL_REGISTRY`).toBeDefined()
      expect(entry.definition.hidden, `${kind} must be hidden from the sidebar`).toBe(true)
      expect(entry.Component, `${kind} must still have a real Component`).toBeDefined()
      // Hidden ≠ gone: it must NOT be in SINGLETON_PANELS (the sidebar's list)…
      expect(SINGLETON_PANELS).not.toContain(kind)
    }
  })

  it("every merged-away kind's panel_id still round-trips through PANEL_ID_TO_KIND (what a saved layout / registration actually persists)", () => {
    for (const kind of MERGED_AWAY_KINDS) {
      const panelId = PANEL_REGISTRY[kind].definition.panelId
      expect(PANEL_ID_TO_KIND[panelId], `panel_id "${panelId}" → ${kind}`).toBe(kind)
    }
  })

  it('the five new merged/survivor kinds exist, are visible, and are non-binding singletons', () => {
    const survivors: PanelKind[] = [
      'system.timeline', // Timeline (unchanged id — now the merged wrapper)
      'system.provenance', // Provenance (new kind)
      'system.alerts_watches', // Alerts & Watches (new kind)
      'system.consult', // Consult (unchanged id — now with the depth toggle)
      'system.entities', // Entities (unchanged id — now with Graph/Structure tabs)
    ]
    for (const kind of survivors) {
      const entry = PANEL_REGISTRY[kind]
      expect(entry, `${kind} must exist`).toBeDefined()
      expect(entry.definition.requiresBinding).toBe(false)
      // system.entities is 'operator' category (personal-only per L-108 §6) —
      // still visible, just not hidden.
      expect(entry.definition.hidden).not.toBe(true)
    }
  })

  it('simulates resolving a saved-layout singleton panel by its OLD kind (mirrors App.tsx LegbaPanelComponent\'s singletonKind lookup)', () => {
    // A saved custom layout persists Dockview's own serialized panel params,
    // which for a singleton carry `{ singletonKind: <PanelKind> }` verbatim
    // (see App.tsx addSingleton / LegbaPanelComponent). Restoring it just
    // looks the kind up in PANEL_REGISTRY — reproduce that lookup here for
    // every merged-away kind.
    for (const kind of MERGED_AWAY_KINDS) {
      const entry = PANEL_REGISTRY[kind]
      expect(entry, `singletonKind "${kind}" from an old saved layout must still resolve`).toBeDefined()
    }
  })
})

/**
 * U-4 (COHERENCE_WAVES_PLAN_2026-07-28 §U-4) — the boot-grid "what changed"
 * tile is registered + hidden for a DIFFERENT reason than the U-3 merge
 * aliases above: it's a brand-new capability (not a folded-away original),
 * hidden purely so it doesn't spend the U-3 ≤22-visible-row sidebar budget
 * on a tile that's already on-screen at cold boot with no user action.
 */
describe('U-4 — system.wall_movers is registered, hidden, and still fully reachable', () => {
  it('is a real, non-binding singleton with its own Component', () => {
    const entry = PANEL_REGISTRY['system.wall_movers']
    expect(entry).toBeDefined()
    expect(entry.definition.requiresBinding).toBe(false)
    expect(entry.definition.scopeKey).toBe(null)
    expect(entry.Component).toBeDefined()
  })

  it('is hidden from the sidebar (does not spend the ≤22-row budget)', () => {
    expect(PANEL_REGISTRY['system.wall_movers'].definition.hidden).toBe(true)
    expect(SINGLETON_PANELS).not.toContain('system.wall_movers')
  })

  it('has a unique panel_id that round-trips through PANEL_ID_TO_KIND (so ⌘K / a saved layout referencing it resolves)', () => {
    const panelId = PANEL_REGISTRY['system.wall_movers'].definition.panelId
    expect(PANEL_ID_TO_KIND[panelId]).toBe('system.wall_movers')
  })

  it('ships in both personal and cis modes, same as the full Wall it complements', () => {
    expect(PANEL_REGISTRY['system.wall_movers'].definition.modes).toEqual(
      PANEL_REGISTRY['system.wall'].definition.modes,
    )
  })

  it('adding this kind did not flip system.wall (the full panel) to hidden or binding', () => {
    const wall = PANEL_REGISTRY['system.wall'].definition
    expect(wall.hidden).not.toBe(true)
    expect(wall.requiresBinding).toBe(false)
  })
})

/**
 * GLASS-2 — the three API surfaces that shipped with no consumer now have one.
 *
 * Two of the three land as TABS of the merged Provenance panel rather than as
 * sidebar rows (the ≤23-visible-row budget in navGroups.test.ts is spent to the
 * last row, and that test's own terms are "earn it, fold into a tab, or hide").
 * They use the same registered-but-hidden alias mechanism as the U-3 merges, so
 * ⌘K and any saved layout still resolve them standalone.
 */
describe('GLASS-2 — the unconsumed-API consumers', () => {
  const TAB_MOUNTED: PanelKind[] = ['system.situations', 'system.narratives']

  it('the journal gate is a real, VISIBLE, non-binding singleton', () => {
    const entry = PANEL_REGISTRY['system.journal_gate']
    expect(entry).toBeDefined()
    expect(entry.Component).toBeDefined()
    expect(entry.definition.requiresBinding).toBe(false)
    expect(entry.definition.scopeKey).toBe(null)
    expect(entry.definition.hidden).not.toBe(true)
    expect(SINGLETON_PANELS).toContain('system.journal_gate')
  })

  it('the journal gate is personal-only — it applies registry writes', () => {
    expect(PANEL_REGISTRY['system.journal_gate'].definition.modes).toEqual(['personal'])
  })

  it('the two tab-mounted surfaces are registered, hidden, and keep a real Component', () => {
    for (const kind of TAB_MOUNTED) {
      const entry = PANEL_REGISTRY[kind]
      expect(entry, `${kind} must exist in PANEL_REGISTRY`).toBeDefined()
      expect(entry.Component, `${kind} must have a real Component`).toBeDefined()
      expect(entry.definition.hidden, `${kind} is mounted as a Provenance tab`).toBe(true)
      expect(SINGLETON_PANELS).not.toContain(kind)
    }
  })

  it('every GLASS-2 panel_id round-trips (⌘K + saved layouts resolve them)', () => {
    for (const kind of [...TAB_MOUNTED, 'system.journal_gate' as PanelKind]) {
      const panelId = PANEL_REGISTRY[kind].definition.panelId
      expect(PANEL_ID_TO_KIND[panelId], `panel_id "${panelId}" → ${kind}`).toBe(kind)
    }
  })

  it('folding the two tabs in did not hide their host (system.provenance)', () => {
    const host = PANEL_REGISTRY['system.provenance'].definition
    expect(host.hidden).not.toBe(true)
    expect(host.requiresBinding).toBe(false)
  })
})
