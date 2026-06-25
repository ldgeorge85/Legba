/**
 * Unit tests for the command-palette fuzzy matcher (DOM-free).
 */

import { describe, it, expect } from 'vitest'
import { fuzzyMatch, RECORD_PRIMARY_PANEL } from './CommandPalette'
import { PANEL_REGISTRY } from '@/panel-registry/registry'

describe('RECORD_PRIMARY_PANEL', () => {
  it('maps every record family to a real, bound primary panel', () => {
    for (const [recordKind, panelKind] of Object.entries(RECORD_PRIMARY_PANEL)) {
      const entry = PANEL_REGISTRY[panelKind]
      expect(entry, `${recordKind} → ${panelKind} must exist in the registry`).toBeDefined()
    }
  })

  it('routes targets and analysts to binding-scoped panels', () => {
    // The record-jump default opens a panel bound to the record's id, so the
    // target/analyst primaries must be requiresBinding panels (P-B1).
    expect(PANEL_REGISTRY[RECORD_PRIMARY_PANEL.target].definition.requiresBinding).toBe(true)
    expect(PANEL_REGISTRY[RECORD_PRIMARY_PANEL.analyst].definition.requiresBinding).toBe(true)
  })
})

describe('fuzzyMatch', () => {
  it('matches an empty query against anything', () => {
    expect(fuzzyMatch('', 'Target Overview')).toBe(true)
  })

  it('matches a contiguous substring', () => {
    expect(fuzzyMatch('find', 'Findings Feed')).toBe(true)
  })

  it('matches a non-contiguous subsequence', () => {
    expect(fuzzyMatch('fnf', 'Findings Feed')).toBe(true)
  })

  it('is case-insensitive', () => {
    expect(fuzzyMatch('LINE', 'Provenance Lineage')).toBe(true)
  })

  it('rejects when chars are out of order', () => {
    expect(fuzzyMatch('zzz', 'Findings Feed')).toBe(false)
    expect(fuzzyMatch('feedf', 'Findings Feed')).toBe(false)
  })
})
