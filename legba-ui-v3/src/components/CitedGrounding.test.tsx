/**
 * REAL-MOUNT tests for the 2026-08-30 grounding-citation repair.
 *
 * THE DEFECT. A unit finding's `data.citations` carries five DESK GROUNDING
 * block kinds alongside its signal refs. `citationsModel` special-cased
 * exactly ONE of them (`situation_register`); the other four carry no `ref_id`
 * at all, hit the `if (!marker || !refId) continue` skip, never reached
 * `byMarker`, and so `tokenizeProse` emitted an `unresolved` token — rendering
 * an amber "Unresolved citation — this marker has no backing evidence in the
 * record" chip OVER evidence the verify plane scored SUPPORTED. `prior_read`
 * DOES carry a `ref_id` (an `analyst_outputs` uuid) and so fell through to the
 * `refKind: 'signal'` default: a chip captioned "signal" that drills to a
 * signal row which has never existed.
 *
 * These mount the real components, not the model — the model can be right
 * while the surface still renders an amber chip, and that gap is the whole
 * reported defect.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import CitedProse from './CitedProse'
import CitedAssessment from './inspector/CitedAssessment'
import { extractCitations } from '@/lib/citationsModel'

const PRIOR_ID = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'

/** A real unit read's merged body: one signal ref + all five grounding
 *  blocks, in the shape `unit_grounding.citation_for_block` writes (including
 *  the `marker_class` / `resolves_against` structural marks). */
const UNIT_BODY = {
  body:
    'Volume is up [1]. Flat against the prior read [2]. The fortnight record [3] ' +
    'holds, the register [4] is unchanged, the baseline [5] is exceeded, and the ' +
    'standing question [6] is still open.',
  data: {
    citations: [
      { marker: '[1]', signal_id: 'ffffffff-9999-4999-8999-ffffffffffff', title: 'Route 9 column' },
      {
        marker: '[2]',
        ref_kind: 'prior_read',
        ref_id: PRIOR_ID,
        grounding: 'prior_read',
        title: 'Ruritania — morning read, 18 July',
        evidence_text: 'BLUF: tension flat versus the prior sweep.',
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[3]',
        ref_kind: 'window_ledger',
        grounding: 'window_ledger',
        title: "Window ledger (this unit's trailing 14-day record)",
        evidence_text: '16 July 2026 — elevated — dated fortnight record',
        ledger_finding_ids: ['bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb'],
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[4]',
        ref_kind: 'situation_register',
        grounding: 'situation_register',
        title: 'Open-situation register',
        evidence_text: 'Ruritania — open frame — active',
        situation_ids: ['cccccccc-3333-4333-8333-cccccccccccc'],
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[5]',
        ref_kind: 'desk_baseline',
        grounding: 'desk_baseline',
        title: 'Desk baseline',
        evidence_text: 'signal_volume_24h expected 41.2, current 118.0',
        baseline_keys: ['country_g20_RR:signal_volume_24h'],
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
      {
        marker: '[6]',
        ref_kind: 'open_questions',
        grounding: 'open_questions',
        title: 'Standing open questions',
        evidence_text: 'Who controls the Route 9 corridor?',
        question_ids: ['dddddddd-4444-4444-8444-dddddddddddd'],
        marker_class: 'desk_grounding',
        resolves_against: 'data.citations',
      },
    ],
  },
}

/** The same five blocks WITHOUT the structural marks — every row written
 *  before the `2026-08-30/1` stamp. The repair must read these too, off the
 *  registered `ref_kind` vocabulary, or it fixes only rows that do not exist
 *  yet. */
const PRE_STAMP_BODY = {
  body: UNIT_BODY.body,
  data: {
    citations: UNIT_BODY.data.citations.map((c) => {
      const { marker_class: _mc, resolves_against: _ra, ...rest } = c as Record<string, unknown>
      return rest
    }),
  },
}

describe('CitedProse — every grounding kind renders as an honest chip', () => {
  it('renders no "Unresolved citation" chip for any of the five blocks', () => {
    const citations = extractCitations(UNIT_BODY)
    render(<CitedProse text={UNIT_BODY.body} citations={citations} />)
    expect(screen.queryAllByTestId('citation-unresolved')).toHaveLength(0)
    expect(screen.getAllByTestId('citation-chip')).toHaveLength(6)
  })

  it('every chip is kind-labeled, and only the four synthetic blocks refuse to drill', () => {
    render(<CitedProse text={UNIT_BODY.body} citations={extractCitations(UNIT_BODY)} />)
    const byMarker = new Map(
      screen.getAllByTestId('citation-chip').map((el) => [el.getAttribute('data-marker'), el]),
    )
    expect(byMarker.get('[1]')?.getAttribute('data-cite-kind')).toBe('signal')
    expect(byMarker.get('[2]')?.getAttribute('data-cite-kind')).toBe('prior_read')
    expect(byMarker.get('[3]')?.getAttribute('data-cite-kind')).toBe('window_ledger')
    expect(byMarker.get('[4]')?.getAttribute('data-cite-kind')).toBe('situation_register')
    expect(byMarker.get('[5]')?.getAttribute('data-cite-kind')).toBe('desk_baseline')
    expect(byMarker.get('[6]')?.getAttribute('data-cite-kind')).toBe('open_questions')
    // The prior read has a real record behind it; the four synthetic blocks
    // do not, and no anchor is minted for them.
    expect(byMarker.get('[2]')?.getAttribute('data-drills')).toBe('true')
    for (const m of ['[3]', '[4]', '[5]', '[6]']) {
      expect(byMarker.get(m)?.getAttribute('data-drills')).toBe('false')
      expect(byMarker.get(m)?.getAttribute('data-grounding')).toBe('true')
    }
    // …and the signal ref is untouched by all of this.
    expect(byMarker.get('[1]')?.getAttribute('data-grounding')).toBeNull()
    expect(byMarker.get('[1]')?.getAttribute('data-drills')).toBe('true')
  })

  it('the prior read is captioned "prior read", never "signal"', () => {
    render(<CitedProse text={UNIT_BODY.body} citations={extractCitations(UNIT_BODY)} />)
    const chip = screen
      .getAllByTestId('citation-chip')
      .find((el) => el.getAttribute('data-marker') === '[2]')!
    expect(chip.getAttribute('title')).toContain('prior read')
    expect(chip.getAttribute('title')).not.toContain('signal')
    // The hover card's kind caption follows the same one definition.
    const card = chip.parentElement!.querySelector('[data-testid="citation-card"]')!
    expect(card.textContent).toContain('prior read')
  })

  it('a PRE-STAMP row (no marker_class) resolves identically off ref_kind', () => {
    render(<CitedProse text={PRE_STAMP_BODY.body} citations={extractCitations(PRE_STAMP_BODY)} />)
    expect(screen.queryAllByTestId('citation-unresolved')).toHaveLength(0)
    expect(screen.getAllByTestId('citation-chip')).toHaveLength(6)
  })

  it('a genuinely dangling marker is STILL shown as unresolved (the honesty contract holds)', () => {
    render(<CitedProse text="A claim with no backing [99]." citations={[]} />)
    expect(screen.getAllByTestId('citation-unresolved')).toHaveLength(1)
  })
})

describe('CitedAssessment — the evidence panel drills honestly', () => {
  it('lists one row per citation, with the id-less blocks as labeled non-links', () => {
    render(
      <CitedAssessment text={UNIT_BODY.body} citations={extractCitations(UNIT_BODY)} />,
    )
    const rows = screen.getAllByTestId('evidence-row')
    expect(rows).toHaveLength(6)
    // Four synthetic blocks → a kind-labeled non-link, not a dead RecordLink.
    const nonLinks = screen.getAllByTestId('evidence-nonlink')
    expect(nonLinks).toHaveLength(4)
    expect(nonLinks.map((n) => n.textContent).join(' ')).toContain('window ledger')
    expect(nonLinks.map((n) => n.textContent).join(' ')).toContain('open questions')
  })

  it('the prior read drills to a FINDING, not to a signal that cannot exist', () => {
    render(
      <CitedAssessment text={UNIT_BODY.body} citations={extractCitations(UNIT_BODY)} />,
    )
    const priorRow = screen
      .getAllByTestId('evidence-row')
      .find((el) => el.getAttribute('data-cite-kind') === 'prior_read')!
    const link = within(priorRow).getByTestId('record-link')
    expect(link.getAttribute('data-kind')).toBe('finding')
    expect(link.getAttribute('data-id')).toBe(PRIOR_ID)
  })

  it('each grounding row states the set it resolves against', () => {
    render(
      <CitedAssessment text={UNIT_BODY.body} citations={extractCitations(UNIT_BODY)} />,
    )
    const ledgerRow = screen
      .getAllByTestId('evidence-row')
      .find((el) => el.getAttribute('data-cite-kind') === 'window_ledger')!
    expect(ledgerRow.textContent).toContain('desk grounding')
    expect(ledgerRow.textContent).toContain('resolves against data.citations')
  })

  it('the anchor ids are distinct across id-less blocks (they used to collide)', () => {
    render(
      <CitedAssessment text={UNIT_BODY.body} citations={extractCitations(UNIT_BODY)} />,
    )
    const ids = screen.getAllByTestId('evidence-row').map((el) => el.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})
