/**
 * Unit tests for the feed's producer taxonomy.
 *
 * Locks: the unit/composition/other classification, the grouped option build
 * (canonical rosters always offered, other producers only on evidence), the
 * exact-id set that gates the server-side `analyst_id=` push, and the LOCKSTEP
 * between this module's unit roster and the canonical one the country desk
 * renders.
 */
import { describe, it, expect } from 'vitest'
import {
  FEED_COMPOSITION_IDS,
  FEED_UNIT_IDS,
  buildProducerOptions,
  classifyProducer,
  exactProducerIds,
} from './feedProducers'
import { UNITS } from '@/v4/why/CountryUnitsAssessment'

describe('the unit roster stays in lockstep with the country desk', () => {
  it('mirrors v4/why/CountryUnitsAssessment.UNITS exactly (add a unit in BOTH places)', () => {
    expect([...FEED_UNIT_IDS].sort()).toEqual(UNITS.map((u) => u.id).sort())
  })
})

describe('classifyProducer', () => {
  it('names the bounded units', () => {
    expect(classifyProducer('energy_security')).toBe('unit')
    expect(classifyProducer('proliferation_watch')).toBe('unit')
  })

  it('names the compositions — but not a sweep that merely mentions them', () => {
    expect(classifyProducer('country_composition')).toBe('composition')
    expect(classifyProducer('region_composition')).toBe('composition')
    // A real analyst_id in the substrate; it is a sweep, not a composition.
    expect(classifyProducer('composition_lineage_sweep')).toBe('other')
  })

  it('is case/whitespace tolerant and never promotes an absent id', () => {
    expect(classifyProducer('  Energy_Security ')).toBe('unit')
    expect(classifyProducer(null)).toBe('other')
    expect(classifyProducer('')).toBe('other')
    expect(classifyProducer('finding_supersession')).toBe('other')
  })
})

describe('buildProducerOptions', () => {
  it('always offers the canonical unit + composition rosters, in order', () => {
    const opts = buildProducerOptions([])
    expect(opts.filter((o) => o.group === 'unit').map((o) => o.id)).toEqual([...FEED_UNIT_IDS])
    expect(opts.filter((o) => o.group === 'composition').map((o) => o.id)).toEqual([
      ...FEED_COMPOSITION_IDS,
    ])
    // Nothing was in view, so nothing is marked present…
    expect(opts.every((o) => !o.present)).toBe(true)
    // …and no "other producer" is invented out of thin air.
    expect(opts.filter((o) => o.group === 'other')).toEqual([])
  })

  it('adds other producers only on EVIDENCE, label-sorted, and marks what is in view', () => {
    const opts = buildProducerOptions(['finding_supersession', 'energy_security', null, '  ', 'alert_trigger_scan'])
    const others = opts.filter((o) => o.group === 'other')
    expect(others.map((o) => o.id)).toEqual(['alert_trigger_scan', 'finding_supersession'])
    expect(others.every((o) => o.present)).toBe(true)
    expect(opts.find((o) => o.id === 'energy_security')?.present).toBe(true)
    expect(opts.find((o) => o.id === 'military_posture')?.present).toBe(false)
  })

  it('humanizes the label and never duplicates an id', () => {
    const opts = buildProducerOptions(['energy_security', 'energy_security', 'cred.analyst'])
    expect(opts.find((o) => o.id === 'energy_security')?.label).toBe('Energy security')
    expect(opts.find((o) => o.id === 'cred.analyst')?.label).toBe('Cred analyst')
    expect(new Set(opts.map((o) => o.id)).size).toBe(opts.length)
  })
})

describe('exactProducerIds — what may be pushed server-side', () => {
  it('covers the canonical rosters plus everything seen', () => {
    const exact = exactProducerIds(['finding_supersession'])
    expect(exact.has('energy_security')).toBe(true)
    expect(exact.has('country_composition')).toBe(true)
    expect(exact.has('finding_supersession')).toBe(true)
  })

  it('excludes a hand-typed partial, so it stays a client-side substring match', () => {
    expect(exactProducerIds([]).has('energy')).toBe(false)
  })
})
