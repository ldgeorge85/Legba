import { describe, it, expect } from 'vitest'
import { resolveCountry, isCountry, COUNTRY_BY_ISO2 } from './countryGeo'

describe('resolveCountry', () => {
  it('resolves a canonical country name (case-insensitive)', () => {
    expect(resolveCountry('Brazil')).toMatchObject({ iso2: 'BR' })
    expect(resolveCountry('brazil')).toMatchObject({ iso2: 'BR' })
    expect(resolveCountry('  GERMANY ')).toMatchObject({ iso2: 'DE' })
  })

  it('resolves a bare ISO-2 code', () => {
    expect(resolveCountry('US')).toMatchObject({ iso2: 'US', name: 'United States' })
    expect(resolveCountry('br')).toMatchObject({ iso2: 'BR' })
  })

  it('resolves common aliases and short-forms', () => {
    expect(resolveCountry('USA')).toMatchObject({ iso2: 'US' })
    expect(resolveCountry('U.S.')).toMatchObject({ iso2: 'US' })
    expect(resolveCountry('UK')).toMatchObject({ iso2: 'GB' })
    expect(resolveCountry('Russian Federation')).toMatchObject({ iso2: 'RU' })
    expect(resolveCountry('Ivory Coast')).toMatchObject({ iso2: 'CI' })
    expect(resolveCountry('Türkiye')).toMatchObject({ iso2: 'TR' })
  })

  it('strips a leading "the"', () => {
    expect(resolveCountry('The Netherlands')).toMatchObject({ iso2: 'NL' })
  })

  it('carries a representative centroid', () => {
    const br = resolveCountry('Brazil')!
    expect(Number.isFinite(br.lat)).toBe(true)
    expect(Number.isFinite(br.lon)).toBe(true)
    // Brazil's centroid is in the southern hemisphere, western longitude.
    expect(br.lat).toBeLessThan(0)
    expect(br.lon).toBeLessThan(0)
  })

  it('returns null for non-countries and junk', () => {
    expect(resolveCountry('Acme Corp')).toBeNull()
    expect(resolveCountry('')).toBeNull()
    expect(resolveCountry(null)).toBeNull()
    expect(resolveCountry(undefined)).toBeNull()
    // a 2-char string that is not an ISO-2 code
    expect(resolveCountry('zz')).toBeNull()
  })
})

describe('isCountry', () => {
  it('is true for recognized countries, false otherwise', () => {
    expect(isCountry('France')).toBe(true)
    expect(isCountry('FR')).toBe(true)
    expect(isCountry('a random person')).toBe(false)
  })
})

describe('COUNTRY_BY_ISO2', () => {
  it('keys every entry on its own iso2 with finite centroids', () => {
    for (const [iso2, fix] of Object.entries(COUNTRY_BY_ISO2)) {
      expect(fix.iso2).toBe(iso2)
      expect(iso2).toMatch(/^[A-Z]{2}$/)
      expect(Number.isFinite(fix.lat)).toBe(true)
      expect(Number.isFinite(fix.lon)).toBe(true)
      expect(fix.lat).toBeGreaterThanOrEqual(-90)
      expect(fix.lat).toBeLessThanOrEqual(90)
      expect(fix.lon).toBeGreaterThanOrEqual(-180)
      expect(fix.lon).toBeLessThanOrEqual(180)
    }
  })
})
