import { describe, it, expect } from 'vitest'
import { humanizeAnalystId } from './analystNames'

describe('humanizeAnalystId', () => {
  it('turns the canonical example into sentence case', () => {
    expect(humanizeAnalystId('economic_coercion')).toBe('Economic coercion')
  })

  it('handles a mixed-case / multi-word unit id', () => {
    expect(humanizeAnalystId('Energy_security')).toBe('Energy security')
  })

  it('strips a leading analyst_ plumbing prefix', () => {
    expect(humanizeAnalystId('analyst_country_assessor')).toBe('Country assessor')
  })

  it('handles dotted ids (engine-room legacy shape)', () => {
    expect(humanizeAnalystId('cred.analyst')).toBe('Cred analyst')
  })

  it('handles a single-word id', () => {
    expect(humanizeAnalystId('world_assessor')).toBe('World assessor')
  })

  it('never fabricates a name for null/undefined/blank — honest fallback', () => {
    expect(humanizeAnalystId(null)).toBe('unknown analyst')
    expect(humanizeAnalystId(undefined)).toBe('unknown analyst')
    expect(humanizeAnalystId('   ')).toBe('unknown analyst')
  })

  it('accepts a caller-supplied fallback', () => {
    expect(humanizeAnalystId(null, '(no analyst)')).toBe('(no analyst)')
  })

  it('is idempotent on an already-humanized string', () => {
    expect(humanizeAnalystId('Economic coercion')).toBe('Economic coercion')
  })
})
