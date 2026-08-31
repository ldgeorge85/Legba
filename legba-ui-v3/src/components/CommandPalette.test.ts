/**
 * Unit tests for the command-palette fuzzy matcher (DOM-free).
 */

import { describe, it, expect } from 'vitest'
import { fuzzyMatch, parsePalettePrefix, RECORD_PRIMARY_PANEL } from './CommandPalette'
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

describe('parsePalettePrefix — namespaced families (design §3.3)', () => {
  it('splits the workspace sigil off the query', () => {
    expect(parsePalettePrefix('#')).toEqual({ prefix: '#', rest: '' })
    expect(parsePalettePrefix('#trust')).toEqual({ prefix: '#', rest: 'trust' })
    // VS Code's "prefix + space" form narrows the same way.
    expect(parsePalettePrefix('# morning')).toEqual({ prefix: '#', rest: 'morning' })
  })

  it('splits the panel/layout sigil', () => {
    expect(parsePalettePrefix('>wall')).toEqual({ prefix: '>', rest: 'wall' })
  })

  it('leaves an unprefixed query completely alone (the default index is unchanged)', () => {
    expect(parsePalettePrefix('ukraine')).toEqual({ prefix: null, rest: 'ukraine' })
    expect(parsePalettePrefix('')).toEqual({ prefix: null, rest: '' })
  })

  it('does not claim sigils it has no family for (they stay part of the query)', () => {
    // `/` (substrate) and `@` (entities) are named by the design but wait on
    // the search fan-out moving under the palette — until then they must NOT
    // silently swallow a character.
    expect(parsePalettePrefix('/hormuz')).toEqual({ prefix: null, rest: '/hormuz' })
    expect(parsePalettePrefix('@wagner')).toEqual({ prefix: null, rest: '@wagner' })
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
