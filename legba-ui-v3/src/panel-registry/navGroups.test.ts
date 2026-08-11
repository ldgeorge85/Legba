/**
 * Tests for sidebar nav grouping (TASK D5; U-3 task-ordered nav + Engine Room
 * + the ≤22-visible-row acceptance criterion).
 *
 * Asserts the grouping policy is total (every singleton panel reaches a
 * group), stable/ordered (task order where U-3 §3 pins one, alphabetical
 * otherwise), auto-slots new kinds via prefix fallback, and that the
 * structural sidebar-row count (group headers + the singleton rows they
 * contain — NOT the dynamic per-desk/per-target/per-analyst instance rows,
 * which nest inside Desks / Engine Room and don't inflate this count) stays
 * within the COHERENCE_WAVES_PLAN_2026-07-28 §U-3 target of ≤ 22.
 */

import { describe, it, expect } from 'vitest'
import type { PanelKind } from '@/types'
import { SINGLETON_PANELS, PANEL_REGISTRY } from './registry'
import {
  NAV_GROUP_DEFS,
  buildNavGroups,
  groupForKind,
  kindPrefix,
  type NavGroupId,
} from './navGroups'

describe('groupForKind', () => {
  it('assigns a known group to every singleton panel kind (no "more" leakage)', () => {
    for (const kind of SINGLETON_PANELS) {
      const gid = groupForKind(kind)
      expect(NAV_GROUP_DEFS.some((d) => d.id === gid), `${kind} → ${gid}`).toBe(true)
      expect(gid, `${kind} should not fall into catch-all`).not.toBe('more')
    }
  })

  it('routes registry.* and source.* panels into Engine Room (id: operations)', () => {
    expect(groupForKind('registry.targets')).toBe('operations')
    expect(groupForKind('registry.sources')).toBe('operations')
    expect(groupForKind('source.detail')).toBe('operations')
  })

  it('routes the system.* / v4.* families across the five groups', () => {
    // Awareness — the live surfaces + detail rail. Includes the U-3 merged
    // Timeline (Events/Validity) and Alerts & Watches (Watches/Triggers/
    // Deliveries) surfaces.
    expect(groupForKind('system.findings')).toBe('awareness')
    expect(groupForKind('system.inspector')).toBe('awareness')
    expect(groupForKind('v4.map')).toBe('awareness')
    expect(groupForKind('system.timeline')).toBe('awareness')
    expect(groupForKind('system.alerts_watches')).toBe('awareness')
    // Investigation — dig into the why. Includes the U-3 merged Provenance
    // surface (Why/Lineage/Flow tabs).
    expect(groupForKind('system.search')).toBe('investigation')
    expect(groupForKind('system.entities')).toBe('investigation')
    expect(groupForKind('system.provenance')).toBe('investigation')
    // Analysis — reason over the substrate.
    expect(groupForKind('system.consult')).toBe('analysis')
    expect(groupForKind('system.optimizer')).toBe('analysis')
    expect(groupForKind('system.eval_scorecard')).toBe('analysis')
    // Products — the finished intelligence.
    expect(groupForKind('v4.assessment')).toBe('products')
    expect(groupForKind('system.journal')).toBe('products')
    // Engine Room (id: operations) — the plumbing catch-all.
    expect(groupForKind('system.budget')).toBe('operations')
    expect(groupForKind('system.governor')).toBe('operations')
    expect(groupForKind('system.actor_health')).toBe('operations')
    expect(groupForKind('system.audit')).toBe('operations')
  })

  it('auto-slots an unknown kind via prefix fallback', () => {
    expect(groupForKind('registry.brand_new' as PanelKind)).toBe('operations')
    expect(groupForKind('system.brand_new' as PanelKind)).toBe('operations')
    expect(groupForKind('source.brand_new' as PanelKind)).toBe('operations')
  })

  it('falls back to Engine Room for an unrecognized prefix', () => {
    expect(groupForKind('weird.panel' as PanelKind)).toBe('operations')
  })
})

describe('kindPrefix', () => {
  it('returns the leading dotted segment', () => {
    expect(kindPrefix('system.optimizer.diff')).toBe('system')
    expect(kindPrefix('registry.targets')).toBe('registry')
  })

  it('returns the whole string when there is no dot', () => {
    expect(kindPrefix('lonely' as PanelKind)).toBe('lonely')
  })
})

describe('NAV_GROUP_DEFS — Engine Room (U-3 §2)', () => {
  it('labels the operations group "Engine Room" while keeping its id stable', () => {
    const ops = NAV_GROUP_DEFS.find((d) => d.id === 'operations')
    expect(ops?.label).toBe('Engine Room')
  })
})

describe('buildNavGroups', () => {
  it('keeps every singleton panel reachable exactly once', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    const seen = groups.flatMap((g) => g.kinds)
    expect(new Set(seen).size).toBe(seen.length) // no dupes
    expect(seen.slice().sort()).toEqual(SINGLETON_PANELS.slice().sort())
  })

  it('omits empty groups', () => {
    const groups = buildNavGroups(['registry.targets', 'registry.stack'])
    expect(groups.map((g) => g.id)).toEqual(['operations'])
    expect(groups[0].kinds).toContain('registry.targets')
  })

  it('renders groups in NAV_GROUP_DEFS order', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    const orderIndex = (id: NavGroupId) => NAV_GROUP_DEFS.findIndex((d) => d.id === id)
    const indices = groups.map((g) => orderIndex(g.id))
    expect(indices).toEqual([...indices].sort((a, b) => a - b))
  })

  it('Awareness reads Wall → Live Feed → World Map → Timeline → Alerts & Watches → Inspector → the rest (U-3 §3 task order, NOT alphabetical)', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    const awareness = groups.find((g) => g.id === 'awareness')!
    expect(awareness.kinds).toEqual([
      'system.wall',
      'system.findings',
      'v4.map',
      'system.timeline',
      'system.alerts_watches',
      'system.inspector',
      'v4.kpi', // "At a Glance" — no task-order override, so it's "the rest"
    ])
  })

  it('a group with no task-order overrides stays purely alphabetical by title', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    const products = groups.find((g) => g.id === 'products')!
    const titles = products.kinds.map((k) => PANEL_REGISTRY[k].definition.defaultTitle)
    expect(titles).toEqual([...titles].sort((a, b) => a.localeCompare(b)))
  })
})

describe('U-3 acceptance — ≤ 22 visible sidebar rows', () => {
  // "Visible sidebar rows" = the STRUCTURAL rows: the 6 fixed section headers
  // (Desks + the 5 verb groups, one of which is Engine Room) plus every
  // singleton panel row that lives directly under Awareness / Investigation /
  // Analysis / Products (Engine Room's own 14 rows, and the dynamic per-desk /
  // per-target / per-analyst instance rows nested inside Desks / Engine Room,
  // are each one collapsed structural row regardless of how many records
  // exist behind them — see Sidebar.tsx). This is what COHERENCE_WAVES_PLAN
  // §U-3's "≤ 22 visible sidebar rows" acceptance criterion measures.
  const DESKS_HEADER = 1
  const ENGINE_ROOM_HEADER = 1

  // RAISED 22 -> 23 on 2026-08-03 by K-G4, deliberately and for exactly one
  // row: `system.graph_walk` (Investigation). U-3's budget exists to stop
  // panel SPRAWL — a sidebar re-accumulating the 62 rows the consolidation
  // removed — and it did its job: the count sat at exactly 22, and every
  // graph surface added since (system.entity_graph, system.notable_structure)
  // was folded into a tab of `system.entities` rather than spending a row.
  //
  // The walk is not that kind of addition. It is an interactive VERB over the
  // reified `entity_edges` store — anchor, expand a hop per click, inspect an
  // edge's evidence — and it is the surface under the operator's stated
  // platform vision ("walking the world graph, asking multi-hop questions
  // interactively IS basically the entire vision"). Folding the entire vision
  // into a tab of an Entities panel, or hiding it behind ⌘K the way
  // `system.wall_movers` is hidden, would satisfy the number and defeat its
  // purpose.
  //
  // So the number moves by one, visibly, in a reviewable line — and the
  // ratchet closes again at 23. The next panel that wants a visible row is
  // back to the same argument: earn it, fold into a tab, or hide.
  const BUDGET = 23

  it('stays at or under the target', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    const nonEngineRoomGroups = groups.filter((g) => g.id !== 'operations')
    const headerCount = nonEngineRoomGroups.length + DESKS_HEADER + ENGINE_ROOM_HEADER
    const leafCount = nonEngineRoomGroups.reduce((n, g) => n + g.kinds.length, 0)
    const total = headerCount + leafCount
    expect(total).toBeLessThanOrEqual(BUDGET)
  })

  it('spends the raised row on the graph walk and nothing else', () => {
    // The ratchet: if the count reaches 23 WITHOUT system.graph_walk being the
    // reason, something else quietly took the row and the budget must be
    // re-argued rather than inherited.
    const groups = buildNavGroups(SINGLETON_PANELS)
    const nonEngineRoomGroups = groups.filter((g) => g.id !== 'operations')
    const total =
      nonEngineRoomGroups.length +
      DESKS_HEADER +
      ENGINE_ROOM_HEADER +
      nonEngineRoomGroups.reduce((n, g) => n + g.kinds.length, 0)
    if (total === BUDGET) {
      const visible = nonEngineRoomGroups.flatMap((g) => g.kinds)
      expect(visible).toContain('system.graph_walk')
    }
  })
})
