/**
 * Tests for the alias table (UI_HOLISTIC_DESIGN_2026-08-24 §4.4 / §6.4).
 *
 * The alias table is the load-bearing part of every merge train: it is the only
 * reason a retired panel id may disappear from the registry without breaking a
 * saved layout, a ⌘K deep-link, a `#sel=` share hash, or a runtime
 * `ui_panel_registrations` row minted years earlier. So the bar is exhaustive:
 *
 *  1. EVERY id from the 67-kind era resolves — the twelve retired here, and
 *     every kind still registered.
 *  2. No alias points at another alias, and every `tab` names a real tab of the
 *     survivor it points at (a stale tab name would silently render the wrong
 *     surface).
 *  3. The kind table and the panel_id table stay 1:1 — a half-declared
 *     retirement resolves in the sidebar and breaks in the loader.
 *  4. The `fromJSON` pre-pass rewrites stale ids, COLLAPSES DUPLICATES, drops
 *     the unresolvable, and never hands Dockview a dangling view reference.
 */

import { describe, it, expect } from 'vitest'
import type { SerializedDockview } from 'dockview-react'
import {
  KIND_ALIASES,
  PANEL_ID_ALIASES,
  resolveKind,
  resolveRetiredPanelId,
  rewriteSerializedLayout,
} from './aliases'
import { PANEL_REGISTRY } from './registry'
import type { PanelKind } from '@/types'

/**
 * The tab ids each tabbed survivor actually renders. Mirrors the `TABS`
 * constants in the merged wrappers (and Entities); an alias naming a tab that
 * is not in this map is a defect, not a matter of taste.
 */
const SURVIVOR_TABS: Partial<Record<PanelKind, readonly string[]>> = {
  'system.timeline': ['events', 'validity'],
  'system.provenance': ['why', 'lineage', 'flow', 'trajectory', 'narratives'],
  'system.alerts_watches': ['watches', 'triggers', 'deliveries'],
  'system.consult': ['chat', 'deep'],
  'system.entities': ['list', 'graph', 'structure'],
}

describe('the alias table', () => {
  it('retires exactly the twelve U-3/GLASS-2 merge originals', () => {
    expect(Object.keys(KIND_ALIASES).sort()).toEqual(
      [
        'system.alert_center',
        'system.deep_consult',
        'system.entity_graph',
        'system.escalations',
        'system.lineage',
        'system.narratives',
        'system.notable_structure',
        'system.situations',
        'system.watchlist',
        'v4.flow',
        'v4.timeline',
        'v4.why',
      ].sort(),
    )
  })

  it('every alias points at a LIVE registered kind', () => {
    for (const [retired, alias] of Object.entries(KIND_ALIASES)) {
      expect(PANEL_REGISTRY[alias.kind], `${retired} → ${alias.kind}`).toBeDefined()
    }
  })

  it('no alias points at another alias (one hop, always)', () => {
    for (const [retired, alias] of Object.entries(KIND_ALIASES)) {
      expect(KIND_ALIASES[alias.kind], `${retired} → ${alias.kind} is itself retired`).toBeUndefined()
    }
  })

  it('no retired kind is still registered (an alias and a row would disagree)', () => {
    for (const retired of Object.keys(KIND_ALIASES)) {
      expect(PANEL_REGISTRY[retired as PanelKind], retired).toBeUndefined()
    }
  })

  it('every tab names a real tab of the survivor', () => {
    for (const [retired, alias] of Object.entries(KIND_ALIASES)) {
      if (!alias.tab) continue
      const tabs = SURVIVOR_TABS[alias.kind]
      expect(tabs, `${alias.kind} has no known tab set`).toBeDefined()
      expect(tabs, `${retired} → ${alias.kind}#${alias.tab}`).toContain(alias.tab)
    }
  })

  it('every retired surface claims a DISTINCT tab of its survivor (no two ids land on the same read)', () => {
    const seen = new Set<string>()
    for (const [retired, alias] of Object.entries(KIND_ALIASES)) {
      const slot = `${alias.kind}#${alias.tab ?? ''}`
      expect(seen.has(slot), `${retired} duplicates ${slot}`).toBe(false)
      seen.add(slot)
    }
  })

  it('keeps the kind table and the panel_id table 1:1', () => {
    // The descriptor-facing id is the kind with dots→underscores, which is how
    // every `def()` row spells it. Derive the expected table from the kind
    // table so a retirement declared in one place and not the other fails here
    // rather than in the loader at runtime.
    const expected = Object.fromEntries(
      Object.entries(KIND_ALIASES).map(([kind, alias]) => [kind.replace(/[.]/g, '_'), alias]),
    )
    expect(PANEL_ID_ALIASES).toEqual(expected)
  })
})

describe('resolveKind — every id from the 67-kind era still resolves', () => {
  it('resolves a live kind to itself, with no tab', () => {
    expect(resolveKind('system.findings')).toEqual({ kind: 'system.findings' })
    expect(resolveKind('system.wall')).toEqual({ kind: 'system.wall' })
  })

  it('resolves every registered kind (the resolver is total over the catalog)', () => {
    for (const kind of Object.keys(PANEL_REGISTRY)) {
      expect(resolveKind(kind), kind).toEqual({ kind })
    }
  })

  it('resolves every retired kind onto its survivor + tab', () => {
    expect(resolveKind('system.watchlist')).toEqual({
      kind: 'system.alerts_watches',
      tab: 'watches',
    })
    expect(resolveKind('v4.why')).toEqual({ kind: 'system.provenance', tab: 'why' })
    expect(resolveKind('system.deep_consult')).toEqual({ kind: 'system.consult', tab: 'deep' })
  })

  it('returns undefined for an id that is neither (the caller decides: drop, or placeholder)', () => {
    // Kinds DELETED outright in the S7-T2 consolidation — never aliased,
    // because nothing shipped renders them any more.
    expect(resolveKind('system.pulse')).toBeUndefined()
    expect(resolveKind('dashboard.dynamic')).toBeUndefined()
    expect(resolveKind('')).toBeUndefined()
  })

  it('resolves a retired descriptor-facing panel_id', () => {
    expect(resolveRetiredPanelId('system_entity_graph')).toEqual({
      kind: 'system.entities',
      tab: 'graph',
    })
    // A LIVE panel_id is not this function's job (PANEL_ID_TO_KIND owns it).
    expect(resolveRetiredPanelId('system_findings')).toBeUndefined()
  })
})

// ---------------------------------------------------------------------------
// The fromJSON pre-pass.
// ---------------------------------------------------------------------------

/** Build a minimal but REAL-shaped serialized layout: one group, N views. */
function layoutOf(views: string[], extra?: Partial<SerializedDockview>): SerializedDockview {
  const panels: Record<string, unknown> = {}
  for (const id of views) {
    panels[id] = {
      id,
      contentComponent: 'default',
      title: id,
      params: { registration: null, singletonKind: id.split(':')[0], mode: 'personal' },
    }
  }
  return {
    grid: {
      root: {
        type: 'leaf',
        data: { id: 'group-1', views, activeView: views[0] },
      },
      width: 1600,
      height: 900,
      orientation: 'HORIZONTAL',
    },
    panels,
    ...extra,
  } as unknown as SerializedDockview
}

function viewsOf(layout: SerializedDockview): string[] {
  const root = (layout as unknown as { grid: { root: { data: { views?: string[] } } } }).grid.root
  return root.data.views ?? []
}

describe('rewriteSerializedLayout — the fromJSON pre-pass', () => {
  it('leaves a layout of live kinds byte-identical', () => {
    const layout = layoutOf(['system.findings', 'system.inspector'])
    const out = rewriteSerializedLayout(layout)
    expect(out.layout).toEqual(layout)
    expect(out.rewritten).toEqual([])
    expect(out.collapsed).toEqual([])
    expect(out.dropped).toEqual([])
    expect(out.empty).toBe(false)
  })

  it('never mutates its input', () => {
    const layout = layoutOf(['v4.why'])
    const before = JSON.stringify(layout)
    rewriteSerializedLayout(layout)
    expect(JSON.stringify(layout)).toBe(before)
  })

  it('rewrites a retired id onto its survivor, on the aliased tab', () => {
    const out = rewriteSerializedLayout(layoutOf(['system.watchlist']))
    expect(viewsOf(out.layout)).toEqual(['system.alerts_watches'])
    expect(out.rewritten).toEqual(['system.watchlist'])
    const panels = (out.layout as unknown as { panels: Record<string, { params: { singletonKind: string; tab?: string } }> }).panels
    expect(panels['system.alerts_watches'].params.singletonKind).toBe('system.alerts_watches')
    expect(panels['system.alerts_watches'].params.tab).toBe('watches')
    expect(panels['system.watchlist']).toBeUndefined()
  })

  it('re-titles a rewritten tile with the survivor\'s name (a tab is not the old panel\'s title)', () => {
    const out = rewriteSerializedLayout(layoutOf(['system.lineage']))
    const panels = (out.layout as unknown as { panels: Record<string, { title: string }> }).panels
    expect(panels['system.provenance'].title).toBe(
      PANEL_REGISTRY['system.provenance'].definition.defaultTitle,
    )
  })

  it('COLLAPSES DUPLICATES — v4.why + system.lineage + v4.flow become ONE Provenance tile', () => {
    // The design's worked example (§6.2 step 2): three retired ids that all
    // resolve to the same survivor must not produce three identical tiles.
    const out = rewriteSerializedLayout(layoutOf(['v4.why', 'system.lineage', 'v4.flow']))
    expect(viewsOf(out.layout)).toEqual(['system.provenance'])
    expect(out.rewritten).toEqual(['v4.why'])
    expect(out.collapsed).toEqual(['system.lineage', 'v4.flow'])
    expect(Object.keys((out.layout as unknown as { panels: object }).panels)).toEqual([
      'system.provenance',
    ])
  })

  it('collapses a retired id onto a survivor tile the layout ALREADY holds', () => {
    const out = rewriteSerializedLayout(layoutOf(['system.provenance', 'v4.why']))
    expect(viewsOf(out.layout)).toEqual(['system.provenance'])
    expect(out.collapsed).toEqual(['v4.why'])
  })

  it('drops an id with no alias and no registry entry, loudly (never handed to Dockview)', () => {
    const out = rewriteSerializedLayout(layoutOf(['system.findings', 'system.pulse']))
    expect(viewsOf(out.layout)).toEqual(['system.findings'])
    expect(out.dropped).toEqual(['system.pulse'])
    expect(out.empty).toBe(false)
  })

  it('reports empty when NOTHING survives, so the caller seeds instead of showing a void', () => {
    const out = rewriteSerializedLayout(layoutOf(['system.pulse', 'dashboard.dynamic']))
    expect(out.empty).toBe(true)
    expect(out.dropped).toEqual(['system.pulse', 'dashboard.dynamic'])
  })

  it('repoints activeView when the active tile was rewritten or dropped', () => {
    const rewritten = rewriteSerializedLayout(layoutOf(['v4.why', 'system.findings']))
    const root = (rewritten.layout as unknown as { grid: { root: { data: { activeView: string } } } }).grid.root
    expect(root.data.activeView).toBe('system.provenance')

    const dropped = rewriteSerializedLayout(layoutOf(['system.pulse', 'system.findings']))
    const root2 = (dropped.layout as unknown as { grid: { root: { data: { activeView: string } } } }).grid.root
    expect(root2.data.activeView).toBe('system.findings')
  })

  it('prunes a branch child that lost every view, and keeps the siblings', () => {
    const layout = {
      grid: {
        root: {
          type: 'branch',
          data: [
            { type: 'leaf', data: { id: 'g1', views: ['system.pulse'], activeView: 'system.pulse' } },
            {
              type: 'leaf',
              data: { id: 'g2', views: ['system.findings'], activeView: 'system.findings' },
            },
          ],
        },
        width: 1600,
        height: 900,
      },
      panels: {
        'system.pulse': { id: 'system.pulse', params: { singletonKind: 'system.pulse' } },
        'system.findings': { id: 'system.findings', params: { singletonKind: 'system.findings' } },
      },
      activeGroup: 'g1',
    } as unknown as SerializedDockview
    const out = rewriteSerializedLayout(layout)
    const children = (out.layout as unknown as { grid: { root: { data: Array<{ data: { id: string } }> } } }).grid.root.data
    expect(children).toHaveLength(1)
    expect(children[0].data.id).toBe('g2')
    // The activeGroup pointer must not survive its group.
    expect((out.layout as unknown as { activeGroup?: string }).activeGroup).toBeUndefined()
    expect(out.empty).toBe(false)
  })

  it('collapses duplicates ACROSS groups — one panel id may live in exactly one group', () => {
    // Found by `aliasesRuntime.test.tsx`, not by inspection: when two retired
    // tiles sit in DIFFERENT groups and resolve to the same survivor, mapping
    // the ids per-group leaves both groups referencing one panel id — and the
    // real Dockview then mounts it twice. First group wins.
    const layout = {
      grid: {
        root: {
          type: 'branch',
          data: [
            { type: 'leaf', data: { id: 'g1', views: ['v4.why'], activeView: 'v4.why' } },
            {
              type: 'leaf',
              data: { id: 'g2', views: ['system.lineage'], activeView: 'system.lineage' },
            },
          ],
        },
      },
      panels: {
        'v4.why': { id: 'v4.why', params: { singletonKind: 'v4.why' } },
        'system.lineage': { id: 'system.lineage', params: { singletonKind: 'system.lineage' } },
      },
    } as unknown as SerializedDockview
    const out = rewriteSerializedLayout(layout)
    const children = (out.layout as unknown as { grid: { root: { data: Array<{ data: { views: string[] } }> } } }).grid.root.data
    expect(children).toHaveLength(1)
    expect(children[0].data.views).toEqual(['system.provenance'])
  })

  it('keeps a BOUND panel id (<kind>:<record>) intact', () => {
    const out = rewriteSerializedLayout(layoutOf(['target.findings:country_g20_ua']))
    expect(viewsOf(out.layout)).toEqual(['target.findings:country_g20_ua'])
    expect(out.dropped).toEqual([])
  })

  it('tolerates a layout with no panels map at all (passes it straight through)', () => {
    const weird = { grid: { root: { type: 'leaf', data: { views: [] } } } } as unknown as SerializedDockview
    const out = rewriteSerializedLayout(weird)
    expect(out.layout).toEqual(weird)
    expect(out.empty).toBe(false)
  })
})
