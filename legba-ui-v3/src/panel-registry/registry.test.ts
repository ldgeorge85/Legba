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
import { resolveKind, resolveRetiredPanelId } from './aliases'
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
 * Layout-compat (COHERENCE_WAVES_PLAN_2026-07-28 §1.9 / §U-3 acceptance,
 * re-expressed for the ALIAS mechanism — UI_HOLISTIC_DESIGN_2026-08-24 §4.4).
 *
 * The requirement is unchanged and permanent: a saved Dockview layout, a ⌘K
 * deep-link or a `ui_panel_registrations` row that names a PRE-MERGE kind must
 * still render something real, never an `unknown_panel_id` placeholder.
 *
 * What changed is the price. Until this train the mechanism was `HIDDEN_KINDS`:
 * the merged-away kind stayed a FULL registry row — component import and all —
 * with `hidden = true`. Twelve of those rows existed only to be invisible.
 * They are now twelve lines in `panel-registry/aliases.ts`, and the assertions
 * below moved with them: the kind must be GONE from the registry, and it must
 * RESOLVE through the alias table onto the survivor that renders it, on the
 * tab that IS the retired surface. See `aliases.test.ts` for the exhaustive
 * table-level bar (every retired id resolves, no alias points at another
 * alias, every tab names a real tab, the fromJSON pre-pass collapses
 * duplicates).
 */
describe('U-3 layout-compat — old panel ids resolve through the alias table', () => {
  // Every kind U-3/GLASS-2 folded away behind a merged/tabbed survivor.
  const MERGED_AWAY_KINDS = [
    'v4.timeline', // → Timeline's "Events" mode (survivor: system.timeline)
    'v4.why', // → Provenance's "Why" tab (survivor: system.provenance)
    'system.lineage', // → Provenance's "Lineage" tab
    'v4.flow', // → Provenance's "Flow" tab
    'system.situations', // → Provenance's "Trajectory" tab
    'system.narratives', // → Provenance's "Narratives" tab
    'system.alert_center', // → Alerts & Watches' "Triggers" tab
    'system.watchlist', // → Alerts & Watches' "Watches" tab
    'system.escalations', // → Alerts & Watches' "Deliveries" tab
    'system.deep_consult', // → Consult's "Deep" depth
    'system.entity_graph', // → Entities' "Graph" tab
    'system.notable_structure', // → Entities' "Structure" tab
  ]

  it('every merged-away kind is GONE from the registry — retired, not hidden', () => {
    for (const kind of MERGED_AWAY_KINDS) {
      expect(
        PANEL_REGISTRY[kind as PanelKind],
        `${kind} should no longer cost a registry row`,
      ).toBeUndefined()
      expect(SINGLETON_PANELS as string[]).not.toContain(kind)
    }
  })

  it('every merged-away kind still RESOLVES, onto a live survivor', () => {
    for (const kind of MERGED_AWAY_KINDS) {
      const alias = resolveKind(kind)
      expect(alias, `${kind} must resolve`).toBeDefined()
      expect(PANEL_REGISTRY[alias!.kind], `${kind} → ${alias!.kind}`).toBeDefined()
    }
  })

  it("every merged-away kind's panel_id still resolves (what a registration row actually persists)", () => {
    // `<kind>` → `<panel_id>` is the historical snake_case form; the loader
    // resolves it through PANEL_ID_ALIASES now that PANEL_ID_TO_KIND cannot.
    for (const kind of MERGED_AWAY_KINDS) {
      const panelId = kind.replace(/[.]/g, '_')
      expect(PANEL_ID_TO_KIND[panelId], `${panelId} must not be a live kind`).toBeUndefined()
      const alias = resolveRetiredPanelId(panelId)
      expect(alias, `panel_id "${panelId}" must resolve`).toBeDefined()
      expect(PANEL_REGISTRY[alias!.kind]).toBeDefined()
    }
  })

  it('the five merged/survivor kinds exist, are visible, and are non-binding singletons', () => {
    const survivors: PanelKind[] = [
      'system.timeline', // Timeline (Events / Validity)
      'system.provenance', // Provenance (Why / Lineage / Flow / Trajectory / Narratives)
      'system.alerts_watches', // Alerts & Watches (Watches / Triggers / Deliveries)
      'system.consult', // Consult (Chat / Deep)
      'system.entities', // Entities (List / Graph / Structure)
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

  it('no survivor was itself retired (an alias must never point at an alias)', () => {
    for (const kind of MERGED_AWAY_KINDS) {
      const alias = resolveKind(kind)!
      expect(resolveKind(alias.kind)!.kind).toBe(alias.kind)
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
 * Two of the three (`system.situations`, `system.narratives`) landed as TABS of
 * the merged Provenance panel. They used to be registered-but-hidden rows for
 * exactly the reason the design diagnosed — the ≤23-visible-row budget was
 * spent, and "fold into a tab or hide" was the documented escape hatch. They
 * are now alias rows: the surfaces still render (Provenance's Trajectory and
 * Narratives tabs, unmodified), and a deep-link to either id still lands on its
 * tab, without either costing a catalog row.
 */
describe('GLASS-2 — the unconsumed-API consumers', () => {
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

  it('the two tab-mounted surfaces resolve onto Provenance, on their own tabs', () => {
    expect(resolveKind('system.situations')).toEqual({
      kind: 'system.provenance',
      tab: 'trajectory',
    })
    expect(resolveKind('system.narratives')).toEqual({
      kind: 'system.provenance',
      tab: 'narratives',
    })
  })

  it('folding the two tabs in did not hide their host (system.provenance)', () => {
    const host = PANEL_REGISTRY['system.provenance'].definition
    expect(host.hidden).not.toBe(true)
    expect(host.requiresBinding).toBe(false)
  })
})

/**
 * The catalog after the retirement (UI_HOLISTIC_DESIGN_2026-08-24 §4.3).
 *
 * A ratchet in the direction the design pushes: the registry may not grow back
 * the rows the alias table just removed. It is deliberately a CEILING, not an
 * equality — the merge trains that follow shrink it further, and each one is
 * expected to lower this number, never raise it.
 */
describe('registry size ratchet', () => {
  it('stays at or under the post-alias count', () => {
    // 55 → 56: ONE documented exception, D2e (the read-telemetry train).
    //
    // The ratchet exists because the operator's verdict on the panel program
    // was "why are we just continuing to add more damn panels", and
    // PREMISE_REASON_TO_EXIST §4 puts "further Engine-Room observability
    // panels for an engine room with no visitor" on the kill-list. A new kind
    // therefore has to answer for itself, loudly, here.
    //
    // `system.read_scoreboard` answers: it is the ONLY panel that measures
    // whether the other 55 are read at all. It is the instrument of the
    // 90-day oracle wager (§5 Option 1), which is the thing that will decide
    // whether the panel program continues or is cut — so it is the one
    // addition whose purpose is to make deletions defensible rather than to
    // postpone them. Folding it into an existing engine panel was considered
    // and rejected: burying the wager's scoreboard inside a surface nobody
    // visits reproduces the exact failure mode under study.
    //
    // The ratchet is re-armed at 56 and the same rule applies to the next
    // one. If the wager returns a negative verdict at day 90, this row goes
    // with the rest of the deck.
    expect(Object.keys(PANEL_REGISTRY).length).toBeLessThanOrEqual(56)
  })

  it('hidden-but-registered rows are the exception, not the mechanism', () => {
    // Eighteen kinds were hidden before this train — 27% of the catalog. What
    // remains is the set with no survivor to alias onto yet (each named, with
    // its reason, in registry.ts). If this number grows, a retirement was
    // hidden instead of aliased.
    const hidden = Object.values(PANEL_REGISTRY).filter((e) => e.definition.hidden === true)
    expect(hidden.length).toBeLessThanOrEqual(6)
  })
})
