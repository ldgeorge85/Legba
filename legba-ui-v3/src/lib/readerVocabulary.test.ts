import { describe, expect, it } from 'vitest'
import {
  ENGINE_TERMS,
  engineWordsIn,
  producerLabel,
  readableDay,
  subjectDayTitle,
  subjectKindNoun,
  utcClock,
  utcStamp,
} from '@/lib/readerVocabulary'
import { PROFILE_UI } from '@/lib/profileUi'
import { subjectIndexFrom } from '@/lib/subjectIndex'
import { apertureLines, plainReads } from '@/lib/layerCatalog'

describe('the reader vocabulary', () => {
  it('every engine term carries the reader words that replace it', () => {
    expect(ENGINE_TERMS.length).toBeGreaterThan(20)
    for (const t of ENGINE_TERMS) expect(t.reader.trim().length, t.term).toBeGreaterThan(0)
  })

  it('finds the engine words the audit saw, and not ordinary prose or the reader words', () => {
    expect(engineWordsIn('judge 0.97 mean faithfulness · 14d')).toEqual(expect.arrayContaining(['judge', 'faithfulness']))
    expect(engineWordsIn('UNITS CARRIED 3 of 8')).toEqual(['units carried'])
    expect(engineWordsIn('Reads: GET /v3/since?channel=geo_convergence')).toContain('API route')
    for (const plain of ['A judge in Lagos ruled', 'Checked', 'Single source', '2 claims flagged', 'Burkina Faso · 24 Nov']) {
      expect(engineWordsIn(plain), plain).toEqual([])
    }
  })

  it("a subject's page title is its name and a readable day, never a machine id", () => {
    expect(subjectDayTitle('Burkina Faso', '2026-11-24T11:30:02Z')).toBe('Burkina Faso · 24 Nov')
    expect(subjectDayTitle('Burkina Faso', null)).toBe('Burkina Faso')
    expect(readableDay('not a date')).toBeNull()
  })

  it('one clock: UTC with a Z', () => {
    expect(utcClock(new Date('2026-11-24T18:22:28Z'))).toBe('18:22Z')
    expect(utcStamp('2026-11-24T18:22:28Z')).toBe('2026-11-24 18:22Z')
  })

  it("a producer id is said as the profile names it", () => {
    const frame = PROFILE_UI.frames.find((f) => f.producer)
    if (frame?.producer) expect(producerLabel(frame.producer)).toBe(frame.label)
    expect(producerLabel('some_new_unit')).toBe('Some new unit')
    expect(producerLabel(null)).toBeNull()
  })

  it("a subject's kind noun comes from the profile: the subject noun on a scored leg, the leg's label otherwise", () => {
    const scored = PROFILE_UI.rosters.scored_subject_tags[0]
    const other = PROFILE_UI.rosters.subject_tags.find((t) => !PROFILE_UI.rosters.scored_subject_tags.includes(t))
    const index = subjectIndexFrom([
      { descriptor_id: 's1', body: { scope: { tags: scored ? [scored] : [] } } },
      { descriptor_id: 's2', body: { scope: { tags: other ? [other] : [] } } },
    ])
    expect(subjectKindNoun('s1', index)).toBe(PROFILE_UI.subject.singular.toLowerCase())
    if (other) expect(subjectKindNoun('s2', index)).not.toBe(PROFILE_UI.subject.singular.toLowerCase())
  })

  it("a layer's aperture tells a reader what it shows, never the route", () => {
    expect(plainReads('GET /signals — the newest 3,000 ingested signals')).toBe('the newest 3,000 ingested signals')
    expect(plainReads('GET /v3/since?channel=x')).toBe('what this layer draws')
    const layer = { label: 'Signals', aperture: { reads: 'GET /signals — the newest rows', sourceLayers: 'a', window: 'b', subjects: 'c', placement: 'd' } }
    const lines = apertureLines(layer as never, false)
    expect(lines[0]).toEqual({ label: 'Shows', text: 'the newest rows' })
    expect(lines.map((l) => l.text).join(' ')).not.toMatch(/GET|\/v3\//)
  })
})
