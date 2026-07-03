/**
 * Unit tests for the panel-loader dispatch table (L-204 Step 6).
 *
 * Asserts:
 *  - Known panel_id dispatches to the registered kind + component.
 *  - Unknown panel_id falls back to the unbound-placeholder marker.
 *  - The `panels.` prefix is stripped defensively (mirrors the backend).
 *  - `extractScope` reads target/analyst/dashboard ids verbatim.
 *  - `instanceId` builds stable `<kind>:<scope>` identifiers.
 */

import { describe, it, expect } from 'vitest'
import {
  resolvePanel,
  extractScope,
  instanceId,
} from './loader'
import type { PanelRegistration } from '@/types'

function makeReg(overrides: Partial<PanelRegistration> = {}): PanelRegistration {
  return {
    id: 'r1',
    panel_id: 'target_overview',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
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

describe('resolvePanel', () => {
  it('dispatches a known panel_id to its kind + component', () => {
    const reg = makeReg({ panel_id: 'target_overview' })
    const result = resolvePanel(reg)
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.kind).toBe('target.overview')
      expect(result.definition.scopeKey).toBe('target_id')
      expect(typeof result.Component).toBe('object') // React.lazy is an object
    }
  })

  it('falls back to unbound marker for unknown panel_id', () => {
    const reg = makeReg({ panel_id: 'does_not_exist' })
    const result = resolvePanel(reg)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.reason).toBe('unknown_panel_id')
      expect(result.panel_id).toBe('does_not_exist')
    }
  })

  it('strips a leading "panels." prefix defensively', () => {
    const reg = makeReg({ panel_id: 'panels.target_overview' })
    const result = resolvePanel(reg)
    expect(result.ok).toBe(true)
  })

  it('dispatches every panel kind without throwing', () => {
    // Smoke test — every kind in the registry must be reachable.
    const all = [
      'target_overview',
      'target_signals',
      'target_findings',
      'target_situations',
      'target_sources',
      'target_map',
      'target_graph',
      'target_timeline',
      'target_claims',
      'analyst_runs',
      'analyst_outputs',
      'analyst_cross_target',
      'analyst_critiques',
      'registry_targets',
      'registry_analysts',
      'registry_stack',
      'system_lineage',
      'system_budget',
      'system_optimizer',
      'system_dead_letter',
      'system_consult',
    ]
    for (const panel_id of all) {
      const result = resolvePanel(makeReg({ panel_id }))
      expect(result.ok, `panel_id ${panel_id} did not resolve`).toBe(true)
    }
  })
})

describe('extractScope', () => {
  it('reads target_id from the binding', () => {
    const reg = makeReg({ binding: { target_id: 'brazil' } })
    expect(extractScope(reg)).toEqual({ target_id: 'brazil' })
  })

  it('reads analyst_id from the binding', () => {
    const reg = makeReg({ binding: { analyst_id: 'predictor_lat_am' } })
    expect(extractScope(reg)).toEqual({ analyst_id: 'predictor_lat_am' })
  })

  it('reads dashboard_id from the binding', () => {
    const reg = makeReg({ binding: { dashboard_id: 'energy_pulse' } })
    expect(extractScope(reg)).toEqual({ dashboard_id: 'energy_pulse' })
  })

  it('returns empty object for singleton (no binding)', () => {
    const reg = makeReg({ binding: {} })
    expect(extractScope(reg)).toEqual({})
  })

  it('ignores non-string values', () => {
    const reg = makeReg({ binding: { target_id: 42 as unknown as string } })
    expect(extractScope(reg)).toEqual({})
  })
})

describe('instanceId', () => {
  it('uses kind + scope value when bound', () => {
    expect(instanceId('target.overview', { target_id: 'brazil' })).toBe(
      'target.overview:brazil',
    )
  })
  it('uses kind alone when unbound (singleton)', () => {
    expect(instanceId('system.consult', {})).toBe('system.consult')
  })
  it('picks first non-null scope value when multiple set', () => {
    expect(
      instanceId('analyst.runs', { target_id: 'x', analyst_id: 'y' }),
    ).toBe('analyst.runs:x')
  })
})
