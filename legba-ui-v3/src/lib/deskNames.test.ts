/**
 * deskNames — a subject's name and roster leg come from what it DECLARES
 * (UI-B2; the geometry-contract rule: never a regex over the id).
 *
 * The test setup seeds the subject index with the repository's own target
 * descriptors (`test/subjectRoster.fixture.json`), so every assertion below
 * is over a real declaration; the last block swaps in another profile's
 * roster and proves no exemplar shape is assumed.
 */
import { afterEach, describe, it, expect } from 'vitest'
import {
  countryNameForTargetId,
  declaredSubject,
  humanizeId,
  rosterTagOf,
  subjectName,
  thematicDeskName,
} from './deskNames'
import { getSubjectIndex, setSubjectIndex, subjectIndexFrom } from './subjectIndex'
import { profileUiFrom } from './profileUi'
import noneFixture from '@/test/profileNone.fixture.json'

const seeded = getSubjectIndex()
afterEach(() => setSubjectIndex(seeded))

describe('a geo subject reads as the gazetteer name of the ONE code it declares', () => {
  it('every g20 desk, byte for byte with the names the id regex used to give', () => {
    expect(countryNameForTargetId('country_g20_br')).toBe('Brazil')
    expect(countryNameForTargetId('country_g20_us')).toBe('United States')
    expect(countryNameForTargetId('country_g20_tr')).toBe('Turkey')
    expect(countryNameForTargetId('country_g20_kr')).toBe('South Korea')
    expect(countryNameForTargetId('country_g20_gb')).toBe('United Kingdom')
    expect(countryNameForTargetId('country_g20_za')).toBe('South Africa')
  })

  it('every watch desk (incl. the Sahel/DRC/Myanmar/Haiti set)', () => {
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

  it('is null for a subject that is not geo, or not registered', () => {
    expect(countryNameForTargetId('situation_iran_war')).toBeNull()
    expect(countryNameForTargetId('lane_hormuz')).toBeNull()
    expect(countryNameForTargetId('country_g20_xx')).toBeNull()
  })

  it('reads the DECLARATION, not the id: a geo subject under any id names its code', () => {
    setSubjectIndex(
      subjectIndexFrom([
        { descriptor_id: 'desk_7', name: 'Desk seven', body: { scope: { domain: 'geo', geo: ['BR'], tags: [] } } },
        { descriptor_id: 'country_g20_br', name: 'Somewhere — Else', body: { scope: { domain: 'geo', geo: ['FR'], tags: [] } } },
      ]),
    )
    expect(subjectName('desk_7')).toBe('Brazil')
    expect(subjectName('country_g20_br')).toBe('France')
  })
})

describe('any other subject reads as its declared name', () => {
  it('the house form `<qualifier> — <name>` reads as the name', () => {
    expect(thematicDeskName('lane_hormuz')).toBe('Strait of Hormuz')
    expect(thematicDeskName('lane_red_sea')).toBe('Red Sea / Bab el-Mandeb / Suez')
    expect(thematicDeskName('flow_semiconductors')).toBe('Semiconductors')
    expect(subjectName('region_europe')).toBe('Europe')
    expect(subjectName('situation_iran_war')).toBe('US–Iran War')
  })

  it('a name with no qualifier is itself', () => {
    expect(subjectName('japan_news')).toBe('Japan News')
  })

  it('thematicDeskName is null for a non-thematic or unregistered id', () => {
    expect(thematicDeskName('country_g20_br')).toBeNull()
    expect(thematicDeskName('lane_unknown_future_lane')).toBeNull()
    expect(thematicDeskName(null)).toBeNull()
    expect(thematicDeskName(undefined)).toBeNull()
  })
})

describe('humanizeId', () => {
  it('a registered subject reads as its declared name', () => {
    expect(humanizeId('country_g20_br')).toBe('Brazil')
    expect(humanizeId('country_watch_cd')).toBe('DR Congo')
    expect(humanizeId('lane_hormuz')).toBe('Strait of Hormuz')
  })

  it('anything else is title-cased, a leading analyst_ dropped — no exemplar prefix is stripped', () => {
    expect(humanizeId('analyst_country_assessor')).toBe('Country Assessor')
    expect(humanizeId('economic_coercion')).toBe('Economic Coercion')
    expect(humanizeId('country_g20_xx')).toBe('Country G20 Xx')
  })

  it('the roster not read yet: generic, never a parse of the id', () => {
    setSubjectIndex(new Map())
    expect(humanizeId('country_g20_br')).toBe('Country G20 Br')
    expect(declaredSubject('country_g20_br')).toBeNull()
  })

  it('never throws and degrades to the raw id only for unsplittable input', () => {
    expect(humanizeId('---')).toBe('---')
  })
})

describe('the roster leg is the first profile tag the subject declares', () => {
  it('geopolitics: g20 / watch / supply_chain from scope.tags', () => {
    expect(rosterTagOf('country_g20_br')).toBe('g20')
    expect(rosterTagOf('country_watch_sd')).toBe('watch')
    expect(rosterTagOf('lane_hormuz')).toBe('supply_chain')
    expect(rosterTagOf('region_europe')).toBeNull()
    expect(rosterTagOf('nope')).toBeNull()
  })

  it('a second profile reads its OWN legs, and the exemplar ids land on none', () => {
    const sanctions = profileUiFrom(noneFixture)
    const idx = subjectIndexFrom([
      { descriptor_id: 'regime_ofac_russia', name: 'Regime — OFAC Russia', body: { scope: { domain: 'thematic', tags: ['regime'] } } },
      { descriptor_id: 'counterparty_acme', name: 'Counterparty — Acme Shipping', body: { scope: { tags: ['counterparty', 'regime'] } } },
    ])
    expect(rosterTagOf('regime_ofac_russia', idx, sanctions)).toBe('regime')
    expect(rosterTagOf('counterparty_acme', idx, sanctions)).toBe('regime') // profile order: regime first
    expect(rosterTagOf('country_g20_br', getSubjectIndex(), sanctions)).toBeNull()
    expect(subjectName('counterparty_acme', idx, sanctions)).toBe('Acme Shipping')
    // `map.geometry: none` — no gazetteer: a geo subject keeps its declared name
    const geo = subjectIndexFrom([{ descriptor_id: 'x', name: 'Jurisdiction — EU', body: { scope: { domain: 'geo', geo: ['BE'] } } }])
    expect(subjectName('x', geo, sanctions)).toBe('EU')
  })
})

const SOURCES = import.meta.glob(
  [
    '/src/lib/deskNames.ts',
    '/src/lib/subjectIndex.ts',
    '/src/components/ObjectRail.tsx',
    '/src/mobile/components/ReportsList.tsx',
    '/src/mobile/mobileModel.ts',
  ],
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>

/** The code, comments stripped (a comment may cite the old convention). */
function code(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

describe('no subject id is parsed anywhere the names and legs are read', () => {
  it('no exemplar id pattern, roster tag or producer id in the code', () => {
    expect(Object.keys(SOURCES)).toHaveLength(5)
    for (const [path, src] of Object.entries(SOURCES)) {
      expect(code(src), path).not.toMatch(
        /country_|g20|'watch'|watch_|lane_|flow_|supply_chain|_composition|world_assessor|world_assessment/,
      )
    }
  })
})
