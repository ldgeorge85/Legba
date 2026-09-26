/**
 * REAL-MOUNT tests for the cited source + fold chips beside a claim (7b-i).
 *
 * THE READING PROBLEM this closes: a composed read's sentence ends in an
 * ordinal, and everything the platform already checked about that claim — the
 * outlet count after the wire fold, the single-source mark, the judge's
 * per-claim verdict — lived a hover or a drill away. A reader could not see,
 * without a click, why a claim was weaker than it looked.
 *
 * These mount the real components, not the model: `claimFold.test.ts` proves
 * the derivation, and the derivation can be right while the sentence still
 * renders a bare `[2]`.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import CitedProse from './CitedProse'
import CitedAssessment from './inspector/CitedAssessment'
import { extractCitations } from '@/lib/citationsModel'
import { corroborationByOrdinal } from '@/lib/claimFold'

const REF_1 = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'
const REF_2 = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb'

/**
 * A country assembly's merged body: two claims, the second single-sourced and
 * wire-folded, with the record's own `blocks[].corroboration` carrying the
 * fold COUNT beside the citation's boolean.
 */
const ASSEMBLY_BODY = {
  body: 'Gulf shipping insurance rose [[ref:1]]. Enrichment capacity doubled [[ref:2]].',
  data: {
    citations: [
      {
        marker: '[[ref:1]]',
        ordinal: 1,
        ref_id: REF_1,
        ref_kind: 'finding',
        title: 'Shipping insurance sub-claim',
        source: 'economic_coercion',
        produced_at: '2026-09-18T06:30:00+00:00',
        derived_from: [REF_2],
      },
      {
        marker: '[[ref:2]]',
        ordinal: 2,
        ref_id: REF_2,
        ref_kind: 'finding',
        title: 'Enrichment sub-claim',
        source: 'proliferation_watch',
        produced_at: '2026-09-17T22:05:00+00:00',
        single_source: true,
        wire_folded: true,
        derived_from: [REF_1],
      },
    ],
    assembly: {
      blocks: [
        { ordinal: 1, corroboration: { n_sources: 4, n_desks_sharing: 2 } },
        {
          ordinal: 2,
          corroboration: {
            n_sources: 3,
            n_desks_sharing: 1,
            n_independent_sources: 1,
            wire_folded: 2,
            single_source: true,
          },
        },
      ],
    },
  },
  verification: {
    judge_status: 'llm',
    checkable_claims: 4,
    supported_claims: 3,
    unsupported_spans: [
      {
        text: 'Enrichment capacity doubled.',
        reason: 'judge_unsupported',
        markers: [2],
      },
    ],
  },
}

const citations = () => extractCitations(ASSEMBLY_BODY)
const corroboration = () => corroborationByOrdinal(ASSEMBLY_BODY)

/** The chip elements rendered beside one ordinal. */
function tagFor(marker: string): HTMLElement | null {
  const chip = screen
    .getAllByTestId('citation-chip')
    .find((el) => el.getAttribute('data-marker') === marker)!
  return chip.parentElement!.querySelector<HTMLElement>('[data-testid="claim-tag"]')
}

describe('CitedProse — the source tag beside a claim', () => {
  it('renders masthead · date beside every claim when the surface asks for it', () => {
    render(
      <CitedProse
        text={ASSEMBLY_BODY.body}
        citations={citations()}
        verification={ASSEMBLY_BODY.verification}
        claimTags
        corroboration={corroboration()}
      />,
    )
    const tags = screen.getAllByTestId('claim-source-tag')
    expect(tags).toHaveLength(2)
    expect(tags[0].getAttribute('data-masthead')).toBe('Economic coercion')
    expect(tags[0].getAttribute('data-date')).toBe('2026-09-18')
    expect(tags[0].textContent).toContain('Economic coercion')
    expect(tags[0].textContent).toContain('2026-09-18')
  })

  it('renders the fold chips for [[ref:2]] — the verdict, the fold, the count', () => {
    render(
      <CitedProse
        text={ASSEMBLY_BODY.body}
        citations={citations()}
        verification={ASSEMBLY_BODY.verification}
        claimTags
        corroboration={corroboration()}
      />,
    )
    const tag = tagFor('[[ref:2]]')!
    const chips = [...tag.querySelectorAll('[data-testid="claim-fold-chip"]')]
    expect(chips.map((c) => c.getAttribute('data-chip-kind'))).toEqual([
      'unsupported',
      'single-source',
      'wire-folded',
    ])
    expect(chips.map((c) => c.textContent)).toEqual([
      'unsupported',
      'single-source',
      '2 wire-folded',
    ])
    // Each chip states its reason in ONE sentence, taken from the data.
    expect(chips[0].getAttribute('title')).toBe(
      'verdict: unsupported by the verify judge, confidence lowered',
    )
    expect(chips[1].getAttribute('title')).toBe('cited signals fold to one outlet')
    expect(chips[2].getAttribute('title')).toBe('2 wire copies folded into one outlet')
  })

  it('a claim with no fold reason gets a source tag and NO chips', () => {
    render(
      <CitedProse
        text={ASSEMBLY_BODY.body}
        citations={citations()}
        verification={ASSEMBLY_BODY.verification}
        claimTags
        corroboration={corroboration()}
      />,
    )
    const tag = tagFor('[[ref:1]]')!
    expect(tag.querySelectorAll('[data-testid="claim-fold-chip"]')).toHaveLength(0)
    expect(tag.querySelector('[data-testid="claim-source-tag"]')).not.toBeNull()
  })

  it('keeps the existing ordinal drill-down untouched', () => {
    render(
      <CitedProse
        text={ASSEMBLY_BODY.body}
        citations={citations()}
        verification={ASSEMBLY_BODY.verification}
        claimTags
        corroboration={corroboration()}
      />,
    )
    const chips = screen.getAllByTestId('citation-chip')
    expect(chips).toHaveLength(2)
    for (const chip of chips) {
      expect(chip.tagName).toBe('BUTTON')
      expect(chip.getAttribute('data-drills')).toBe('true')
      expect(chip.getAttribute('data-cite-kind')).toBe('finding')
    }
    // …and the hover card still carries the per-claim verdict.
    expect(screen.getAllByTestId('citation-card')).toHaveLength(2)
  })

  it('renders NOTHING extra by default — the scan surfaces are untouched', () => {
    render(
      <CitedProse
        text={ASSEMBLY_BODY.body}
        citations={citations()}
        verification={ASSEMBLY_BODY.verification}
      />,
    )
    expect(screen.queryAllByTestId('claim-tag')).toHaveLength(0)
    expect(screen.queryAllByTestId('claim-fold-chip')).toHaveLength(0)
    expect(screen.getAllByTestId('citation-chip')).toHaveLength(2)
  })

  it('a read with no source, no date and no fold keys renders exactly as today', () => {
    const plain = {
      body: 'A claim resting on a signal [1].',
      data: {
        citations: [
          { marker: '[1]', signal_id: 'ffffffff-9999-4999-8999-ffffffffffff', title: 'A wire item' },
        ],
      },
    }
    render(<CitedProse text={plain.body} citations={extractCitations(plain)} claimTags />)
    expect(screen.queryAllByTestId('claim-tag')).toHaveLength(0)
    expect(screen.getAllByTestId('citation-chip')).toHaveLength(1)
  })

  it('falls back to the citation boolean when the record block is not in hand', () => {
    // A country composition carries citations but no assembly payload: the
    // chip must still fire, stating no number it does not have.
    render(
      <CitedProse
        text={ASSEMBLY_BODY.body}
        citations={citations()}
        verification={ASSEMBLY_BODY.verification}
        claimTags
      />,
    )
    const chip = tagFor('[[ref:2]]')!.querySelector(
      '[data-testid="claim-fold-chip"][data-chip-kind="wire-folded"]',
    )!
    expect(chip.textContent).toBe('wire-folded')
    expect(chip.getAttribute('title')).toBe('wire copies folded into one outlet')
  })
})

describe("CitedAssessment — the Inspector's read card turns the tags on", () => {
  it('renders the source tag and the chips without the caller asking', () => {
    render(
      <CitedAssessment
        text={ASSEMBLY_BODY.body}
        citations={citations()}
        verification={ASSEMBLY_BODY.verification}
        corroboration={corroboration()}
      />,
    )
    expect(screen.getAllByTestId('claim-source-tag')).toHaveLength(2)
    const kinds = screen
      .getAllByTestId('claim-fold-chip')
      .map((el) => el.getAttribute('data-chip-kind'))
    expect(kinds).toEqual(['unsupported', 'single-source', 'wire-folded'])
    // The evidence panel below is unchanged — one row per citation.
    expect(screen.getAllByTestId('evidence-row')).toHaveLength(2)
  })

  it('an uncited legacy read still degrades to prose with no tags', () => {
    render(<CitedAssessment text="A legacy read with no citations." citations={[]} />)
    expect(screen.queryAllByTestId('claim-tag')).toHaveLength(0)
    expect(screen.getByTestId('uncited-marker')).toBeTruthy()
  })
})
