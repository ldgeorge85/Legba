/**
 * A HISTORY CITATION KEEPS ITS TENSE MARKER VERBATIM (UI-D1 Table A,
 * `system.timeline`; research note §4 piece B) — byte for byte.
 *
 * The producer is `analysts/history_grounding.stale_tense_marker`:
 *
 *     (historical: valid YYYY..YYYY, recorded YYYY-MM)
 *
 * with `????` / `????-??` in a slot the row does not carry. It is the one
 * string that carries a cited figure's valid time AND record time together,
 * so a surface that re-derived, re-spaced, smartened or "fixed" it (two dots
 * into an ellipsis, `????` into a guessed year) would lose the very thing the
 * marker exists for. This walks the real pipeline — the citation entry as the
 * producer writes it → `extractCitations` → the two renderers that print it
 * (`CitedProse`'s card, the desk brief's temporal notes) — and compares bytes.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import CitedProse from '@/components/CitedProse'
import { TemporalNotes } from '@/panels/target/DeskBriefSections'
import { extractCitations } from '@/lib/citationsModel'

/** The producer's grammar, exactly (`stale_tense_marker`, `_year`, `_year_month`). */
const PRODUCER_GRAMMAR = /^\(historical: valid (\d{4}|\?{4})\.\.(\d{4}|\?{4}), recorded (\d{4}-\d{2}|\?{4}-\?{2})\)$/

const MARKERS = [
  '(historical: valid 2025..2025, recorded 2026-07)',
  '(historical: valid 2016..2025, recorded 2026-09)',
  // A row missing its times: the producer prints the unknown slots, never a guess.
  '(historical: valid ????..2019, recorded ????-??)',
]

const bytes = (s: string) => Array.from(new TextEncoder().encode(s))

function citationWith(marker: string) {
  return extractCitations({
    body: 'The figure was 2.8 [12].',
    data: {
      citations: [
        {
          marker: '[12]',
          ordinal: 12,
          ref_kind: 'observation',
          ref_id: '11111111-1111-4111-8111-111111111111',
          grounding: 'history_series',
          marker_class: 'desk_grounding',
          resolves_against: 'data.citations',
          title: 'GDP growth (annual %) — world_bank',
          observation: {
            provider: 'world_bank',
            indicator_name: 'GDP growth (annual %)',
            value: '2.8',
            unit: 'pct_per_year',
            valid_from: '2025-01-01',
            valid_to: '2025-12-31',
            record_time: '2026-07-13T00:00:00+00:00',
            stale_tense: marker,
          },
        },
      ],
    },
  })
}

describe('the stale-tense marker, byte for byte', () => {
  it.each(MARKERS)('%s is the producer grammar', (marker) => {
    expect(marker).toMatch(PRODUCER_GRAMMAR)
  })

  it.each(MARKERS)('survives the citation parse unchanged: %s', (marker) => {
    const [c] = citationWith(marker)
    expect(bytes(c.observation?.staleTense ?? '')).toEqual(bytes(marker))
  })

  it.each(MARKERS)('CitedProse prints it verbatim in the card: %s', (marker) => {
    render(<CitedProse text="The figure was 2.8 [12]." citations={citationWith(marker)} />)
    const printed = screen.getByTestId('citation-stale-tense').textContent ?? ''
    expect(bytes(printed)).toEqual(bytes(marker))
  })

  it.each(MARKERS)('the desk brief\'s temporal notes print it verbatim beside both clocks: %s', (marker) => {
    render(<TemporalNotes citations={citationWith(marker)} />)
    const printed = screen.getByTestId('desk-brief-tense-marker').textContent ?? ''
    expect(bytes(printed)).toEqual(bytes(marker))
    // …and the valid window and the record time are each shown, never folded.
    const note = screen.getByTestId('desk-brief-temporal').textContent ?? ''
    expect(note).toContain('valid 2025-01-01 → 2025-12-31')
    expect(note).toContain('recorded 2026-07-13T00:00:00+00:00')
  })
})
