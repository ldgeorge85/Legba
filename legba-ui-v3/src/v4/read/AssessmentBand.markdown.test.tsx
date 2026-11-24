/**
 * X1/D1 — the Assessment's markdown path keeps every unsupported mark inline.
 *
 * With `citations` the band renders its body through the workstation's one
 * prose renderer (markdown + `[[ref:N]]` chips) instead of as raw text, so the
 * marks can no longer be `<sup>` elements spliced in by offset. They become
 * letter glyphs inserted into the source right after each flagged span — and
 * never inside a citation marker, which would break the chip.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import AssessmentBand, { markBodyInline } from './AssessmentBand'
import type { AssessmentPayload, ReadFindingRow, UnsupportedMark } from '@/lib/assemblyModel'
import { projectAssessment } from '@/lib/assemblyModel'
import { extractCitations } from '@/lib/citationsModel'
import fixture from './__fixtures__/assessment.country.json'

function mark(start: number, end: number, cls = 'rank'): UnsupportedMark {
  return { sentence_index: 0, char_start: start, char_end: end, class: cls, detector: 'deterministic', note: 'n', text: '' }
}

describe('markBodyInline', () => {
  it('puts the mark’s letter right after its span, in the notes’ order', () => {
    const body = 'alpha beta gamma'
    expect(markBodyInline(body, [mark(6, 10), mark(0, 5)])).toBe('alphaᵇ betaᵃ gamma')
  })

  it('never splits a [[ref:N]] marker — the glyph moves to the marker’s end', () => {
    const body = 'risk [[ref:12]] holds'
    // A span that ends INSIDE the marker.
    expect(markBodyInline(body, [mark(0, 9)])).toBe('risk [[ref:12]]ᵃ holds')
  })

  it('drops an out-of-range or overlapping mark rather than guessing', () => {
    const body = 'one two'
    expect(markBodyInline(body, [mark(0, 3), mark(1, 4), mark(5, 99)])).toBe('oneᵃ two')
  })
})

describe('AssessmentBand with citations', () => {
  it('renders markdown and chips, not the raw source', () => {
    const row = fixture as unknown as ReadFindingRow
    const payload = projectAssessment(row) as AssessmentPayload
    render(
      <AssessmentBand
        body={fixture.body}
        title={fixture.title}
        payload={payload}
        spineOrdinals={[1, 2, 3]}
        spineMismatch={false}
        citations={extractCitations(fixture.data as Record<string, unknown>)}
      />,
    )
    const body = screen.getByTestId('read-assessment-body')
    expect(body.textContent).not.toContain('**BLUF:**')
    expect(body.textContent).not.toContain('[[ref:')
    expect(body.querySelectorAll('[data-testid="citation-chip"]').length).toBeGreaterThan(0)
  })
})
