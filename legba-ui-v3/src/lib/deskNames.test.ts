import { describe, it, expect } from 'vitest'
import {
  countryNameForTargetId,
  humanizeId,
  iso2FromTargetId,
  isDeskTargetId,
} from './deskNames'

describe('iso2FromTargetId', () => {
  it('extracts the iso2 from a g20 desk id', () => {
    expect(iso2FromTargetId('country_g20_br')).toBe('br')
    expect(iso2FromTargetId('country_g20_US')).toBe('us')
  })

  it('extracts the iso2 from a watch desk id', () => {
    expect(iso2FromTargetId('country_watch_sd')).toBe('sd')
  })

  it('returns null for non-desk ids', () => {
    expect(iso2FromTargetId('japan_news')).toBeNull()
    expect(iso2FromTargetId('analyst_country_assessor')).toBeNull()
    expect(iso2FromTargetId(null)).toBeNull()
    expect(iso2FromTargetId(undefined)).toBeNull()
    expect(iso2FromTargetId('')).toBeNull()
  })

  it('does not match a partial/extra-segment id', () => {
    expect(iso2FromTargetId('country_g20_br_extra')).toBeNull()
    expect(iso2FromTargetId('prefix_country_g20_br')).toBeNull()
  })
})

describe('isDeskTargetId', () => {
  it('is true for g20 + watch ids, false otherwise', () => {
    expect(isDeskTargetId('country_g20_de')).toBe(true)
    expect(isDeskTargetId('country_watch_ht')).toBe(true)
    expect(isDeskTargetId('nigeria_news')).toBe(false)
  })
})

describe('countryNameForTargetId', () => {
  it('resolves every g20 code to a human name', () => {
    expect(countryNameForTargetId('country_g20_br')).toBe('Brazil')
    expect(countryNameForTargetId('country_g20_us')).toBe('United States')
    expect(countryNameForTargetId('country_g20_tr')).toBe('Turkey')
    expect(countryNameForTargetId('country_g20_kr')).toBe('South Korea')
    expect(countryNameForTargetId('country_g20_gb')).toBe('United Kingdom')
    expect(countryNameForTargetId('country_g20_za')).toBe('South Africa')
  })

  it('resolves every watch code to a human name (incl. the Sahel/DRC/Myanmar/Haiti set)', () => {
    expect(countryNameForTargetId('country_watch_il')).toBe('Israel')
    expect(countryNameForTargetId('country_watch_ir')).toBe('Iran')
    expect(countryNameForTargetId('country_watch_ua')).toBe('Ukraine')
    expect(countryNameForTargetId('country_watch_tw')).toBe('Taiwan')
    expect(countryNameForTargetId('country_watch_kp')).toBe('North Korea')
    expect(countryNameForTargetId('country_watch_pk')).toBe('Pakistan')
    expect(countryNameForTargetId('country_watch_sd')).toBe('Sudan')
    expect(countryNameForTargetId('country_watch_ml')).toBe('Mali')
    expect(countryNameForTargetId('country_watch_bf')).toBe('Burkina Faso')
    expect(countryNameForTargetId('country_watch_ne')).toBe('Niger')
    expect(countryNameForTargetId('country_watch_cd')).toBe('DR Congo')
    expect(countryNameForTargetId('country_watch_mm')).toBe('Myanmar')
    expect(countryNameForTargetId('country_watch_ht')).toBe('Haiti')
  })

  it('returns null for a legacy/non-desk target id', () => {
    expect(countryNameForTargetId('japan_news')).toBeNull()
    expect(countryNameForTargetId('situation_iran_war')).toBeNull()
  })
})

describe('humanizeId', () => {
  it('humanizes a desk id to the country name', () => {
    expect(humanizeId('country_g20_br')).toBe('Brazil')
    expect(humanizeId('country_watch_cd')).toBe('DR Congo')
  })

  it('title-cases a non-desk id, dropping plumbing prefixes', () => {
    expect(humanizeId('analyst_country_assessor')).toBe('Country Assessor')
    expect(humanizeId('economic_coercion')).toBe('Economic Coercion')
    expect(humanizeId('leadership_transition')).toBe('Leadership Transition')
  })

  it('humanizes legacy single-country targets generically (no crash, no raw snake_case)', () => {
    expect(humanizeId('japan_news')).toBe('Japan News')
    expect(humanizeId('nigeria_news')).toBe('Nigeria News')
  })

  it('never throws and degrades to the raw id only for unsplittable input', () => {
    expect(humanizeId('---')).toBe('---')
  })
})
