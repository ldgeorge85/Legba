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
