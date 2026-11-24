/**
 * lib/subjectPage — the subject page's pure model (UI-B3): the tabs, a
 * subject's OWN roster from what its descriptor declares, the composing
 * frames, the absence line. No id is parsed anywhere below.
 */
import { describe, expect, it } from 'vitest'
import {
  SUBJECT_TABS,
  cadenceFromProjection,
  compositionProducers,
  noCompositionLines,
  subjectPageKey,
  subjectRoster,
  subjectTabsFor,
} from './subjectPage'
import { PROFILE_UI, profileUiFrom, subjectUnits } from './profileUi'
import { subjectIndexFrom } from './subjectIndex'
import { UNIT_CADENCE_HOURS } from './gapStripModel'

describe('the tabs', () => {
  it('Table A\'s eight, Brief first, Runtime last; the mode gate keeps the retired kinds\' modes', () => {
    expect(SUBJECT_TABS.map((t) => t.id)).toEqual([
      'brief', 'evidence', 'claims', 'signals', 'situations', 'sources', 'graph', 'runtime',
    ])
    expect(subjectTabsFor('personal')).toHaveLength(8)
    expect(subjectTabsFor('cis').map((t) => t.id)).toEqual([
      'brief', 'evidence', 'claims', 'situations', 'graph', 'runtime',
    ])
  })

  it('a bound page\'s tab requests ride its own tile id; the following page rides the kind', () => {
    expect(subjectPageKey('lane_hormuz')).toBe('subject.page:lane_hormuz')
    expect(subjectPageKey(null)).toBe('subject.page')
  })
})

describe('subjectRoster — the subject\'s OWN units (the geopolitics exemplar)', () => {
  it('a scored desk carries the scored subjects\' units (the Python twin: scored_subject_units)', () => {
    const r = subjectRoster('country_g20_br')
    expect(r.basis).toBe('scored')
    expect(r.units.map((u) => u.id)).toEqual(subjectUnits().map((u) => u.id))
    expect(r.units.map((u) => u.id)).not.toContain('disruption_status')
  })

  it('a supply-chain lane carries the units scoped to a tag it declares — the lane unit alone', () => {
    const r = subjectRoster('lane_hormuz')
    expect(r.basis).toBe('leg')
    expect(r.units.map((u) => u.id)).toEqual(['disruption_status'])
  })

  it('a region no unit fans out over has an empty roster, and says why by its declared tags', () => {
    const r = subjectRoster('region_africa')
    expect(r.basis).toBe('leg')
    expect(r.units).toEqual([])
    expect(r.reason).toMatch(/^no unit fans out over this desk's tags \(/)
  })

  it('a subject the registry has not served has NO roster — none is assumed', () => {
    const r = subjectRoster('country_g20_br', { index: new Map() })
    expect(r.basis).toBe('unresolved')
    expect(r.units).toEqual([])
  })

  it('cadence: the projection\'s declared hours where the plane answered, else the units\' shared cadence', () => {
    const cadence = cadenceFromProjection([
      { key: 'disruption_status', ord: 8, value: { cadence_hours: 6 } },
      { key: 'escalation', ord: 2, value: { cadence_hours: 'x' } },
    ])
    expect(subjectRoster('lane_hormuz', { cadence }).units[0].cadenceHours).toBe(6)
    const desk = subjectRoster('country_g20_br', { cadence })
    expect(desk.units.find((u) => u.id === 'escalation')!.cadenceHours).toBe(UNIT_CADENCE_HOURS)
  })
})

describe('subjectRoster — a profile that sets no leg apart', () => {
  const ui = profileUiFrom({
    subject: { singular: 'Regime', plural: 'Regimes' },
    unit_noun: { singular: 'question', plural: 'questions' },
    rosters: {
      subject_tags: ['regime', 'counterparty'],
      units: [
        { id: 'designation_change', question: 'What changed?' },
        { id: 'counterparty_exposure', scope_tag: 'counterparty', question: 'Is it named?' },
      ],
    },
  })
  const index = subjectIndexFrom([
    { descriptor_id: 'regime_x', name: 'Regime — X', body: { scope: { tags: ['regime'] } } },
    { descriptor_id: 'cp_y', name: 'Counterparty — Y', body: { scope: { tags: ['counterparty'] } } },
  ])

  it('fans every unscoped unit over every subject, plus those scoped to a tag the subject declares', () => {
    expect(subjectRoster('regime_x', { ui, index }).units.map((u) => u.id)).toEqual(['designation_change'])
    const cp = subjectRoster('cp_y', { ui, index })
    expect(cp.basis).toBe('every')
    expect(cp.units.map((u) => [u.id, u.question])).toEqual([
      ['designation_change', 'What changed?'],
      ['counterparty_exposure', 'Is it named?'],
    ])
  })

  it('a profile with no composing frame asks no producer, and the absence line says so in its words', () => {
    expect(compositionProducers(ui)).toEqual([])
    const lines = noCompositionLines('regime_x', [], ui)
    expect(lines.headline).toBe('No composition on record for this regime.')
    expect(lines.detail).toContain('declares no composition producer')
    expect(lines.detail).toContain('questions')
  })
})

describe('compositionProducers — the exemplar', () => {
  it('every frame that declares a producer, the country composition among them', () => {
    const producers = compositionProducers(PROFILE_UI)
    expect(producers).toContain('country_composition')
    expect(producers).toContain('region_composition')
    expect(new Set(producers).size).toBe(producers.length)
  })
})
