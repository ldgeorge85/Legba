/**
 * "A surface without an absence contract does not register" (UI-D1 §6.5).
 *
 * Walks the live `PANEL_REGISTRY`: every registered kind must resolve to a
 * UI-D1 Table A survivor that carries a contract. A new kind added without a
 * `KIND_SURVIVOR` row fails here (and `tsc` refuses it first, because the map
 * is a `Record<PanelKind, …>`).
 */
import { describe, it, expect } from 'vitest'
import { PANEL_REGISTRY } from './registry'
import { KIND_ALIASES, resolveKind } from './aliases'
import {
  ABSENCE_CONTRACTS,
  KIND_SURVIVOR,
  PALETTE_SEARCH,
  absenceContractFor,
  absenceContractForId,
} from './absenceContracts'
import type { PanelKind } from '@/types'

const REGISTERED = Object.keys(PANEL_REGISTRY) as PanelKind[]

describe('absence contracts — the per-kind absence column', () => {
  it('every registered kind resolves to a survivor that carries a contract', () => {
    const missing = REGISTERED.filter((k) => {
      const survivor = KIND_SURVIVOR[k]
      return !survivor || typeof ABSENCE_CONTRACTS[survivor] !== 'string' || ABSENCE_CONTRACTS[survivor] === ''
    })
    expect(missing).toEqual([])
    for (const k of REGISTERED) expect(absenceContractFor(k).length).toBeGreaterThan(0)
  })

  it('the survivor table is UI-D1 Table A: 31 kinds, + N-J\'s system.flow (+ the palette `/` family)', () => {
    const survivors = Object.keys(ABSENCE_CONTRACTS).filter((k) => k !== PALETTE_SEARCH)
    expect(survivors).toHaveLength(32)
    expect(survivors).toContain('system.flow')
  })

  it('maps no kind that is not registered (no stale rows)', () => {
    const stale = Object.keys(KIND_SURVIVOR).filter((k) => !(k in PANEL_REGISTRY))
    expect(stale).toEqual([])
  })

  it('a kind that survives as itself is registered under its own id', () => {
    for (const [kind, survivor] of Object.entries(KIND_SURVIVOR)) {
      if (kind === survivor) expect(PANEL_REGISTRY[kind as PanelKind]).toBeDefined()
    }
  })

  it('agrees with the alias table: every registered kind resolves to itself', () => {
    // The alias table names RETIRED ids only, so a registered kind resolves
    // to itself — its contract is keyed by the id the registry actually holds.
    for (const k of REGISTERED) expect(resolveKind(k)?.kind).toBe(k)
  })

  it('every RETIRED id answers to its survivor\'s contract through the alias table', () => {
    for (const [retired, alias] of Object.entries(KIND_ALIASES)) {
      expect(KIND_SURVIVOR[retired as PanelKind], `${retired} still keyed`).toBeUndefined()
      expect(absenceContractForId(retired), retired).toBe(absenceContractFor(alias.kind))
    }
    // The ledgers' contract reaches the three ids it absorbed.
    expect(absenceContractForId('system.audit')).toMatch(/never a token/)
    expect(absenceContractForId('system.pulse')).toBeUndefined()
  })

  it('the three survivors UI-B1/merges built answer to their own Table A contract', () => {
    for (const k of ['system.registry', 'system.ledgers', 'system.register'] as PanelKind[]) {
      expect(KIND_SURVIVOR[k]).toBe(k)
    }
    expect(absenceContractFor('system.register')).toMatch(/stated scale/)
  })

  it('contracts speak platform nouns, never the exemplar', () => {
    for (const text of Object.values(ABSENCE_CONTRACTS)) {
      expect(text).not.toMatch(/\b(desk|country|countries|G20|region)\b/)
    }
  })
})
