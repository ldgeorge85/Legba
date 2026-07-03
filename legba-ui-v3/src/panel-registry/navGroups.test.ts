/**
 * Tests for sidebar nav grouping (TASK D5).
 *
 * Asserts the grouping policy is total (every singleton panel reaches a
 * group), stable/ordered, and auto-slots new kinds via prefix fallback.
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

  it('routes registry.* and source.* panels into Operations', () => {
    expect(groupForKind('registry.targets')).toBe('operations')
    expect(groupForKind('registry.sources')).toBe('operations')
    expect(groupForKind('source.detail')).toBe('operations')
  })

  it('routes the system.* / v4.* families across the five groups', () => {
    // Awareness — the live surfaces + detail rail.
    expect(groupForKind('system.findings')).toBe('awareness')
    expect(groupForKind('system.alert_center')).toBe('awareness')
    expect(groupForKind('system.inspector')).toBe('awareness')
    expect(groupForKind('v4.map')).toBe('awareness')
    // Investigation — dig into the why.
    expect(groupForKind('system.lineage')).toBe('investigation')
    expect(groupForKind('system.search')).toBe('investigation')
    expect(groupForKind('system.entities')).toBe('investigation')
    expect(groupForKind('v4.why')).toBe('investigation')
    // Analysis — reason over the substrate.
    expect(groupForKind('system.consult')).toBe('analysis')
    expect(groupForKind('system.deep_consult')).toBe('analysis')
    expect(groupForKind('system.optimizer')).toBe('analysis')
    expect(groupForKind('system.eval_scorecard')).toBe('analysis')
    // Products — the finished intelligence.
    expect(groupForKind('v4.assessment')).toBe('products')
    expect(groupForKind('system.journal')).toBe('products')
    // Operations — the plumbing catch-all.
    expect(groupForKind('system.budget')).toBe('operations')
    expect(groupForKind('system.governor')).toBe('operations')
    expect(groupForKind('system.actor_health')).toBe('operations')
    expect(groupForKind('system.audit')).toBe('operations')
  })

  it('auto-slots an unknown kind via prefix fallback', () => {
    // A hypothetical new registry.* / system.* panel with no explicit
    // override still lands in the right group purely from its prefix.
    expect(groupForKind('registry.brand_new' as PanelKind)).toBe('operations')
    expect(groupForKind('system.brand_new' as PanelKind)).toBe('operations')
    expect(groupForKind('source.brand_new' as PanelKind)).toBe('operations')
  })

  it('falls back to Operations for an unrecognized prefix', () => {
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

describe('buildNavGroups', () => {
  it('keeps every singleton panel reachable exactly once', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    const seen = groups.flatMap((g) => g.kinds)
    expect(new Set(seen).size).toBe(seen.length) // no dupes
    expect(seen.slice().sort()).toEqual(SINGLETON_PANELS.slice().sort())
  })

  it('omits empty groups', () => {
    // Only registry kinds → only the Operations group appears.
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

  it('sorts kinds within a group alphabetically by default title', () => {
    const groups = buildNavGroups(SINGLETON_PANELS)
    for (const group of groups) {
      const titles = group.kinds.map((k) => PANEL_REGISTRY[k].definition.defaultTitle)
      expect(titles).toEqual([...titles].sort((a, b) => a.localeCompare(b)))
    }
  })
})
