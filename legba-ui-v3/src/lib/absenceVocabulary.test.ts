/**
 * The absence vocabulary is CLOSED and every phrase carries its meaning
 * (UI-D1 §6.3). The id list is pinned here on purpose: adding a phrase, or
 * re-wording one, is a design decision and a byte-identity change on every
 * surface that prints it — it should show up as a diff to this test.
 */
import { describe, it, expect } from 'vitest'
import {
  ABSENCE_PHRASE_IDS,
  ABSENCE_VOCABULARY,
  absenceMeaning,
  absencePhrase,
} from './absenceVocabulary'
import { UNMEASURED } from './coverageFooter'
import { CARRIED_UNGRADED } from './evalOps'
import { EVIDENCE_STATE_LABEL } from './deskBriefPage'
import { truthLine } from './consultWrites'

describe('absenceVocabulary', () => {
  it('is the closed list of UI-D1 §6.3 — exactly these ids, in this order', () => {
    expect(ABSENCE_PHRASE_IDS).toEqual([
      'not_measured',
      'unmeasured',
      'carried_ungraded',
      'floor_honoured',
      'kept_by_floor',
      'last_known_absence',
      'no_typed_absence',
      'absent_not_contradicted',
      'no_read_yet',
      'dash',
      'writes_none',
    ])
  })

  it('pins the exact words (a re-wording is a byte-identity change)', () => {
    expect(Object.fromEntries(ABSENCE_PHRASE_IDS.map((id) => [id, absencePhrase(id)]))).toEqual({
      not_measured: 'not measured',
      unmeasured: 'unmeasured',
      carried_ungraded: 'carried, ungraded',
      floor_honoured: 'floor honoured',
      kept_by_floor: 'kept by the floor',
      last_known_absence: 'last known absence, not re-checked',
      no_typed_absence: 'no typed absence is recorded',
      absent_not_contradicted: 'absent, not contradicted',
      no_read_yet: 'no read yet',
      dash: '—',
      writes_none: 'writes: none',
    })
  })

  it('every phrase has a meaning and a source, and no two phrases are the same words', () => {
    const phrases = new Set<string>()
    for (const id of ABSENCE_PHRASE_IDS) {
      const entry = ABSENCE_VOCABULARY[id]
      expect(entry.phrase.trim().length, id).toBeGreaterThan(0)
      expect(absenceMeaning(id).length, `${id} meaning`).toBeGreaterThan(40)
      expect(entry.source.length, `${id} source`).toBeGreaterThan(0)
      expect(phrases.has(entry.phrase), `${id} duplicate`).toBe(false)
      phrases.add(entry.phrase)
    }
  })

  it('no phrase is a number — absence is never printed as a digit', () => {
    for (const id of ABSENCE_PHRASE_IDS) expect(absencePhrase(id)).not.toMatch(/\d/)
  })

  it('the phrases are the words the tree already prints (one vocabulary, not a fifth way)', () => {
    expect(absencePhrase('unmeasured')).toBe(UNMEASURED)
    expect(absencePhrase('carried_ungraded')).toBe(CARRIED_UNGRADED.replace('_', ', '))
    expect(absencePhrase('no_read_yet')).toBe(EVIDENCE_STATE_LABEL.no_read)
    expect(absencePhrase('writes_none')).toBe(truthLine([]))
  })

  it('names platform objects, never the exemplar (no desk / country / region in a phrase)', () => {
    for (const id of ABSENCE_PHRASE_IDS) {
      expect(absencePhrase(id)).not.toMatch(/\b(desk|country|countries|region|world)\b/i)
    }
  })
})
