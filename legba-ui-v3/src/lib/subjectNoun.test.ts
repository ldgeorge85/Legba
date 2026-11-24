/**
 * X3/D3 — ONE subject noun on screen (review D §C4). `subject` / `desk` /
 * `target` / `country` collapse to the profile's noun; the others are aliases
 * in code only.
 */
import { describe, expect, it } from 'vitest'
import profileUiJson from '@/lib/profile_ui.json'
import { nounTitle, selectionKindLabel, SUBJECT_NOUN, subjectWordsIn } from './subjectNoun'

const one = profileUiJson.subject.singular
const many = profileUiJson.subject.plural

describe('the one subject noun', () => {
  it("is the profile's noun, as the profile spells it", () => {
    expect(SUBJECT_NOUN).toBe(one)
  })

  it('rewrites every alias into it, keeping case and number', () => {
    expect(nounTitle('Target Map')).toBe(`${one} Map`)
    expect(nounTitle('Target Timeline')).toBe(`${one} Timeline`)
    expect(nounTitle('Cross-target Analyst')).toBe(`Cross-${one.toLowerCase()} Analyst`)
    expect(nounTitle('Country')).toBe(one)
    expect(nounTitle('all countries')).toBe(`all ${many.toLowerCase()}`)
    expect(nounTitle('TARGET')).toBe(one.toUpperCase())
    expect(nounTitle('World')).toBe('World')
  })

  it('finds the collapsed nouns in a string (the screen test)', () => {
    expect(subjectWordsIn('Desk page · target map · Countries')).toEqual(['desk', 'target', 'country'])
    expect(subjectWordsIn('Brazil · 3h ago')).toEqual([])
    expect(subjectWordsIn(nounTitle('Desk page · target map · Countries')).length).toBe(1)
  })

  it("a `target` selection kind is called by the noun; every other kind keeps its word", () => {
    expect(selectionKindLabel('target')).toBe(one.toLowerCase())
    expect(selectionKindLabel('finding')).toBe('finding')
  })
})
