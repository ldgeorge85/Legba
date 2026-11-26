/**
 * Claim text on the Board's list: what each marker becomes. Bold and italic
 * are rendered; citation markers, quote and list leaders and a BLUF label
 * are stripped; identifiers are said in words. No raw marker reaches the
 * screen.
 */
import { describe, expect, it, afterEach } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { ClaimText, claimPlain, claimSegments, claimSource, oneSentence } from './claimText'

afterEach(() => cleanup())

const RAW = [
  '**BLUF:** 2 reads lead this cycle, weighted and uncrowned: [[ref:1]], [[ref:3]].',
  '> The driving vector, direction and severity are unchanged from the 23 November prior read [62], and this window adds an eleventh consecutive high-severity day to this desk\'s record [63] [[ref:4]].',
  '> New in this window, Eisenkot is using his informal bloc [15] [[ref:7]].',
  '- no conflicting pair was detected among the 2 shown blocks (1 pairs examined; BLUF-grain).',
  'Escalation on military_posture: **sharp** rise, *not* yet sustained 【2】.',
]

describe('the markers', () => {
  it('strips citation markers, quote and list leaders and the BLUF label', () => {
    expect(claimSource(RAW[0])).toBe('2 reads lead this cycle, weighted and uncrowned.')
    expect(claimSource(RAW[2])).toBe('New in this window, Eisenkot is using his informal bloc.')
    expect(claimSource(RAW[3]).startsWith('no conflicting pair')).toBe(true)
    expect(claimSource('[link text](https://example.org) and `code`')).toBe('link text and code')
  })

  it('renders bold and italic, and says identifiers in words', () => {
    const segs = claimSegments(claimSource(RAW[4]))
    expect(segs.find((s) => s.strong)?.text).toBe('sharp')
    expect(segs.find((s) => s.em)?.text).toBe('not')
    expect(segs.map((s) => s.text).join('')).toBe('Escalation on military posture: sharp rise, not yet sustained.')
  })

  it('a stray mark is never left on screen', () => {
    expect(claimPlain('an **unclosed mark and a __ stray')).not.toMatch(/\*\*|__/)
  })

  it('no raw marker reaches the screen, whatever the text', () => {
    const { container } = render(
      <div>
        {RAW.map((t, i) => (
          <p key={i}>
            <ClaimText text={t} />
          </p>
        ))}
      </div>,
    )
    const text = container.textContent ?? ''
    expect(text).not.toMatch(/\*\*|\[\[ref|\[\d+\]|【|^>|\n>|BLUF:|military_posture/)
    expect(container.querySelector('strong')?.textContent).toBe('sharp')
  })
})

describe('one sentence', () => {
  it('keeps a short text whole, takes the first sentence of a long one, else cuts at a word', () => {
    expect(oneSentence('Short.')).toBe('Short.')
    const long = `First sentence that runs long enough to count as one. ${'More words follow here. '.repeat(12)}`
    expect(oneSentence(long)).toBe('First sentence that runs long enough to count as one.')
    const run = 'word '.repeat(80).trim()
    const cut = oneSentence(run)
    expect(cut.endsWith('…')).toBe(true)
    expect(cut.length).toBeLessThanOrEqual(201)
  })
})
