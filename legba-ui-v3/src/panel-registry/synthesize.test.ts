/**
 * Unit tests for bound-panel synthesis (P0-2f reachability fix).
 *
 * The live `ui_panel_registrations` surface is empty, so the sidebar's
 * Targets/Analysts groups are synthesized from descriptor heads. Asserts:
 *  - the bound-kind sets are derived from the bundle registry (not a frozen list)
 *  - per-record synthesis emits the full per-target / per-analyst panel set
 *  - retired records and mode-gated kinds are skipped
 *  - real registry rows stay authoritative in the merge (dedupe by instance)
 *  - live-scale synthesis (124 targets / 64 analysts) stays fast + unique
 */

import { describe, it, expect } from 'vitest'
import type { PanelRegistration } from '@/types'
import { PANEL_REGISTRY } from './registry'
import {
  ANALYST_BOUND_KINDS,
  TARGET_BOUND_KINDS,
  mergeRegistrations,
  synthesizeBoundRegistrations,
  type RecordDescriptor,
} from './synthesize'

function rec(id: string, state = 'active'): RecordDescriptor {
  return { descriptor_id: id, name: id, state }
}

function realReg(overrides: Partial<PanelRegistration> = {}): PanelRegistration {
  return {
    id: 'row-1',
    panel_id: 'target_overview',
    descriptor_id: 'brazil',
    descriptor_version: 'a'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Brazil Overview',
    mode: 'personal',
    layout_slot: 'dashboard.brazil.overview',
    data_query: {},
    binding: { target_id: 'brazil' },
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
    ...overrides,
  }
}

describe('bound-kind sets', () => {
  it('derive from the bundle registry: binding-required, category-matched, not hidden', () => {
    for (const kind of TARGET_BOUND_KINDS) {
      const def = PANEL_REGISTRY[kind].definition
      expect(def.requiresBinding).toBe(true)
      expect(def.category).toBe('target')
      expect(def.hidden).not.toBe(true)
    }
    for (const kind of ANALYST_BOUND_KINDS) {
      const def = PANEL_REGISTRY[kind].definition
      expect(def.requiresBinding).toBe(true)
      expect(def.category).toBe('analyst')
      expect(def.hidden).not.toBe(true)
    }
    // The shipped sets (T1–T10 / A1–A5): drift here means the registry changed.
    expect(TARGET_BOUND_KINDS).toContain('target.map')
    expect(TARGET_BOUND_KINDS).toContain('target.timeline')
    expect(TARGET_BOUND_KINDS.length).toBe(9)
    expect(ANALYST_BOUND_KINDS).toContain('analyst.outputs')
    expect(ANALYST_BOUND_KINDS.length).toBe(4)
  })
})

describe('synthesizeBoundRegistrations', () => {
  it('emits the full panel set per record with the addBound binding shape', () => {
    const rows = synthesizeBoundRegistrations([rec('brazil')], [rec('country_assessor')], 'personal')
    expect(rows.length).toBe(TARGET_BOUND_KINDS.length + ANALYST_BOUND_KINDS.length)

    const map = rows.find((r) => r.panel_id === 'target_map')
    expect(map).toBeDefined()
    expect(map!.binding).toEqual({ target_id: 'brazil' })
    expect(map!.descriptor_family).toBe('target')
    expect(map!.analyst_id).toBeNull()
    expect(map!.retired).toBe(false)

    const outputs = rows.find((r) => r.panel_id === 'analyst_outputs')
    expect(outputs).toBeDefined()
    expect(outputs!.binding).toEqual({ analyst_id: 'country_assessor' })
    expect(outputs!.analyst_id).toBe('country_assessor')
  })

  it('skips retired records and records without an id', () => {
    const rows = synthesizeBoundRegistrations(
      [rec('brazil'), rec('gone', 'retired'), { descriptor_id: '', state: 'active' }],
      [],
      'personal',
    )
    expect(rows.length).toBe(TARGET_BOUND_KINDS.length)
    expect(rows.every((r) => r.descriptor_id === 'brazil')).toBe(true)
  })

  it('gates kinds by mode (cis drops the personal-only panels)', () => {
    const personal = synthesizeBoundRegistrations([rec('brazil')], [rec('a1')], 'personal')
    const cis = synthesizeBoundRegistrations([rec('brazil')], [rec('a1')], 'cis')
    expect(cis.length).toBeLessThan(personal.length)
    // target.signals ships personal-only per the registry table.
    expect(cis.find((r) => r.panel_id === 'target_signals')).toBeUndefined()
    expect(cis.find((r) => r.panel_id === 'target_map')).toBeDefined()
  })

  it('mints unique ids across records and kinds', () => {
    const rows = synthesizeBoundRegistrations([rec('a'), rec('b')], [rec('c')], 'personal')
    const ids = new Set(rows.map((r) => r.id))
    expect(ids.size).toBe(rows.length)
  })
})

describe('mergeRegistrations', () => {
  it('keeps real rows and drops the synthetic duplicate of the same instance', () => {
    const synthetic = synthesizeBoundRegistrations([rec('brazil')], [], 'personal')
    const real = [realReg()] // target.overview:brazil
    const merged = mergeRegistrations(real, synthetic)
    expect(merged.length).toBe(synthetic.length) // one synthetic replaced by one real
    expect(merged[0]).toBe(real[0]) // real first + authoritative
    const overviews = merged.filter((r) => r.panel_id === 'target_overview')
    expect(overviews.length).toBe(1)
    expect(overviews[0].id).toBe('row-1')
  })

  it('a RETIRED real row does not shadow the synthetic instance', () => {
    const synthetic = synthesizeBoundRegistrations([rec('brazil')], [], 'personal')
    const merged = mergeRegistrations([realReg({ retired: true })], synthetic)
    const overviews = merged.filter((r) => r.panel_id === 'target_overview')
    // retired real row + live synthetic row both present; App filters retired.
    expect(overviews.length).toBe(2)
    expect(overviews.some((r) => !r.retired)).toBe(true)
  })

  it('passes real rows with unknown panel kinds through untouched', () => {
    const unknown = realReg({ id: 'row-x', panel_id: 'not_a_panel' })
    const merged = mergeRegistrations([unknown], [])
    expect(merged).toEqual([unknown])
  })

  it('handles the empty-registry live case: merge(∅, synthetic) = synthetic', () => {
    const synthetic = synthesizeBoundRegistrations([rec('brazil')], [rec('a1')], 'personal')
    expect(mergeRegistrations([], synthetic)).toEqual(synthetic)
  })
})

describe('live-scale synthesis (124 targets / 64 analysts)', () => {
  it('produces the expected row count with unique instance ids, quickly', () => {
    const targets = Array.from({ length: 124 }, (_, i) => rec(`target_${i}`))
    const analysts = Array.from({ length: 64 }, (_, i) => rec(`analyst_${i}`))
    const t0 = performance.now()
    const rows = synthesizeBoundRegistrations(targets, analysts, 'personal')
    const merged = mergeRegistrations([], rows)
    const elapsed = performance.now() - t0
    expect(rows.length).toBe(124 * TARGET_BOUND_KINDS.length + 64 * ANALYST_BOUND_KINDS.length)
    expect(new Set(merged.map((r) => r.id)).size).toBe(rows.length)
    expect(elapsed).toBeLessThan(250) // pure O(records × kinds); no quadratic scan
  })
})
