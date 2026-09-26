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
    // Consult moved analysis → AWARENESS (WORKSTATION_V2_FLOW_DESIGN decision
    // 3). It is the landing's docked-right centre — the standing conversation
    // beside the read, not a tool the operator goes and finds.
    expect(groupForKind('system.consult')).toBe('awareness')
    // Analysis — reason over the substrate.
    expect(groupForKind('system.optimizer')).toBe('analysis')
    expect(groupForKind('system.eval_scorecard')).toBe('analysis')
    // Products — the finished intelligence, and the rail that indexes it.
    expect(groupForKind('system.navigator')).toBe('products')
    expect(groupForKind('v4.assessment')).toBe('products')
    // `v4.morning_read` had no override and fell to Investigation through the
    // `v4 →` prefix fallback — the landing's headline product filed under
    // "dig into the why" (design §1). It is pinned where it belongs now.
    expect(groupForKind('v4.morning_read')).toBe('products')
    expect(groupForKind('system.journal')).toBe('products')
    // Engine Room (id: operations) — the plumbing catch-all.
    expect(groupForKind('system.budget')).toBe('operations')
    expect(groupForKind('system.governor')).toBe('operations')
    expect(groupForKind('system.actor_health')).toBe('operations')
    expect(groupForKind('system.audit')).toBe('operations')
  })

  it('pins the Journal Gate to Engine Room beside Weekly Grading (both are operator duties over a queue)', () => {
    expect(groupForKind('system.journal_gate')).toBe('operations')
    expect(groupForKind('system.goldset')).toBe('operations')
  })

  it('files the Morning Read under Products (the read is a product, beside the navigator) rather than the v4 prefix fallback', () => {
    // The landing surface — the first thing the operator reads each day — had
    // no KIND_GROUP row at all, so it fell through to
    // `PREFIX_GROUP['v4'] = 'investigation'` and the headline product filed
    // itself under Investigation. Nothing chose that; the fallback answered a
    // question nobody had asked. The row is explicit now so the placement
    // survives any future change to the fallback.
    expect(groupForKind('v4.morning_read')).toBe('products')
    // The fallback it used to take, still in force for an unmapped v4.* kind:
    expect(groupForKind('v4.brand_new' as PanelKind)).toBe('investigation')

    // …and it lands in the rendered Products group, not merely in the map.
    const products = buildNavGroups(SINGLETON_PANELS).find((g) => g.id === 'products')
    expect(products?.kinds).toContain('v4.morning_read')
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
      // Decision 3 — Consult joins Awareness at the END of the workflow order:
      // the conversation you turn to after reading, not before.
      'system.consult',
      // "the rest" — no task-order override, so alphabetical by title:
      'v4.kpi', // "At a Glance"
    ])
  })

  it('Products reads Navigator → the read → the surface it retires → the rest', () => {
    // Products USED to have no task-order overrides and fell back to
    // alphabetical. It has a workflow now — you pick a product on the rail, you
    // read it, and (for the duration of the demotion A/B) you can compare it
    // against the prose one-pager it retires — so it gets the same treatment
    // Awareness has. Everything without an override still sorts alphabetically
    // after the ordered ones.
    const groups = buildNavGroups(SINGLETON_PANELS)
    const products = groups.find((g) => g.id === 'products')!
    expect(products.kinds.slice(0, 3)).toEqual([
      'system.navigator',
      'v4.morning_read',
      'v4.assessment',
    ])
    const rest = products.kinds.slice(3)
    const titles = rest.map((k) => PANEL_REGISTRY[k].definition.defaultTitle)
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
  //
  // RAISED 23 -> 24 on 2026-09-04 by D-4, deliberately, TEMPORARILY, and for
  // exactly one row: `v4.morning_read` (Products).
  //
  // The three options the ratchet offers are earn / fold / hide, and the honest
  // answer here is that the Morning Read has already EARNED the row by taking
  // it from `v4.assessment` — it is the same producer, the same query and the
  // same daily read, rendered from `assembly.v1` instead of from composed
  // prose. It is not an addition to the deck; it is the deck's product surface
  // changing shape under the composition demotion.
  //
  // What it is NOT allowed to be is a permanent second row for one read. The
  // reason both rows exist is spec §5.2: `LEGBA_COMPOSITION_ASSEMBLY` is a
  // runtime flag, the old prose path must stay runnable for the whole program,
  // and the A/B is graded by reading the two side by side. The moment that A/B
  // closes, `v4.assessment` retires the way every other merged surface retired
  // — as a row in `panel-registry/aliases.ts` pointing at `v4.morning_read` —
  // and this budget returns to 23 without a further argument.
  //
  // So the ratchet below is armed at 24 AND at the pair: reaching 24 is only
  // legal while BOTH the raised rows are the named ones.
  //
  // RAISED 24 -> 25 on 2026-09-06 by workstation T-1, for exactly one row:
  // `system.navigator` (Products).
  //
  // The three options the ratchet offers are earn / fold / hide, and this row
  // is the first one whose purpose is to make the ratchet itself unnecessary.
  // The diagnosis behind the budget was that "nobody owned the composed
  // experience, so the registry became the product, and the front door is a
  // list of it" — the sidebar catalog IS the disease the row budget was
  // treating the symptoms of. The Navigator is the front door that is not a
  // panel list: it navigates PRODUCTS (the world read, the regional rollups,
  // the country desks, the thematic lanes, the journal) and none of its rows
  // is a panel. Folding it into a tab would put the front door behind another
  // surface; hiding it behind ⌘K would mean the landing has no way to change
  // which report it is showing.
  //
  // It is also the row that pays the others back. §7 of the same design retires
  // twelve kinds and merges six trains to reach 24 REGISTERED kinds, and §10
  // decision 10 deletes the 38-row sidebar tree outright once the catalog lives
  // on ＋/⌘K. When that lands, this count is not 25 minus one — it is the whole
  // measurement changing shape, because the tree it counts is gone.
  //
  // RAISED 25 -> 26 on 2026-09-24 by 7b-v, for exactly one row:
  // `system.layer_divergence` (Analysis).
  //
  // The budget offers earn / fold / hide, and the row's placement is the whole
  // argument. Left to the `system.*` prefix fallback it would land in Engine
  // Room and cost NO visible row at all — which is precisely why it is pinned
  // to Analysis instead: a country's layer-to-layer gap moving is a reading
  // about the world, and filing it with the plumbing would be buying a cheap
  // budget number by mis-describing what the surface is. Folding it into the
  // Eval Scorecard was considered and rejected on the same ground: that panel
  // answers "is this ANALYST getting better" against our own grades, and this
  // one answers "did this COUNTRY's information environment move" against the
  // country's own baseline. Hiding it behind ⌘K would leave the differentiator
  // the 2026-09-23 capture put on the reader surface reachable only by someone
  // who already knew it existed.
  //
  // It expires the way the others do: §7's merge trains still owe this count a
  // reduction, and nothing here argues otherwise.
  //
  // NOT RAISED on 2026-09-26 by wave P lane B, and that is the point worth
  // recording. `analysis.cross_framing` is the 60th registered kind and it took
  // the ratchet's third option — hide — rather than asking for a 27th row: it
  // is a drill scoped to a CLAIM, opened from the claim it is about, and a
  // sidebar row would have opened it with nothing in hand. The registry size
  // ratchet moved; this one did not. If cross-framing ever wants a visible row,
  // the argument belongs HERE and has not been made.
  const BUDGET = 26

  it('stays at or under the target', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    const nonEngineRoomGroups = groups.filter((g) => g.id !== 'operations')
    const headerCount = nonEngineRoomGroups.length + DESKS_HEADER + ENGINE_ROOM_HEADER
    const leafCount = nonEngineRoomGroups.reduce((n, g) => n + g.kinds.length, 0)
    const total = headerCount + leafCount
    expect(total).toBeLessThanOrEqual(BUDGET)
  })

  it('spends the four raised rows on the graph walk, the Morning Read, the Navigator and the divergence map, and nothing else', () => {
    // The ratchet: if the count reaches the budget WITHOUT all four named
    // surfaces being the reason, something else quietly took a row and the
    // budget must be re-argued rather than inherited.
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
      expect(visible).toContain('v4.morning_read')
      expect(visible).toContain('system.navigator')
      expect(visible).toContain('system.layer_divergence')
    }
  })

  it('cross-framing spends no sidebar row, and is pinned to Analysis for when it might', () => {
    // Two halves, both load-bearing. It must not appear in the rendered tree
    // (it is hidden, so `SINGLETON_PANELS` never carries it) — that is what
    // keeps the budget at 26 while the registry grew to 60. And its group
    // assignment must already be right, so the day the `hidden` flag comes off
    // the placement is a decision someone made rather than a prefix accident.
    const visible = buildNavGroups(SINGLETON_PANELS).flatMap((g) => g.kinds)
    expect(visible).not.toContain('analysis.cross_framing')
    expect(groupForKind('analysis.cross_framing')).toBe('analysis')
    // The new `analysis.*` family resolves on its own prefix, not the Engine
    // Room catch-all — so a second kind in it lands where its name says.
    expect(groupForKind('analysis.brand_new' as PanelKind)).toBe('analysis')
  })

  it('the divergence map is the row that must NOT be in Engine Room', () => {
    // The 26th row was bought by a placement decision (see the BUDGET banner):
    // the `system.*` prefix fallback would have filed it under Engine Room for
    // free, and taking that would have described a reading about the world as a
    // plumbing check. If this ever moves back, the budget should come down with
    // it rather than the row being kept AND the placement changed.
    const groups = buildNavGroups(SINGLETON_PANELS)
    const analysis = groups.find((g) => g.id === 'analysis')
    expect(analysis?.kinds).toContain('system.layer_divergence')
    const engineRoom = groups.find((g) => g.id === 'operations')
    expect(engineRoom?.kinds ?? []).not.toContain('system.layer_divergence')
  })

  it('the Morning Read row is the one that retires the World Assessment row', () => {
    // The pairing that makes the 24th row temporary rather than permanent: both
    // surfaces render `world_assessor`, so when the demotion A/B closes the
    // prose one-pager aliases onto the reader and the budget returns to 23. If
    // a future train ever leaves only ONE of them visible, this assertion is
    // the record of which one was supposed to survive.
    const visible = buildNavGroups(SINGLETON_PANELS)
      .filter((g) => g.id !== 'operations')
      .flatMap((g) => g.kinds)
    if (visible.includes('v4.assessment')) {
      expect(visible).toContain('v4.morning_read')
    }
  })
})
