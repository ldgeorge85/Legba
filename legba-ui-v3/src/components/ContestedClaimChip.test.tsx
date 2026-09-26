/**
 * REAL-MOUNT tests for the CONTESTED-BY-RETRIEVAL chip (Program 7a).
 *
 * WHAT THIS CLOSES. Every other fold chip beside a claim describes how the
 * platform assembled that claim — the wire fold, the single-source mark, the
 * judge's own verdict. All of them are the tower checking itself. This one is
 * the only chip whose evidence is OUTSIDE the tower: the contrary-evidence pass
 * formulated the claim's counter-query, ran it on the free rung, fetched a page
 * through the auditor's fences, and that page states the opposite.
 *
 * So two properties matter more than the pixels, and both are asserted here:
 *
 *   * IT NEVER ADJUDICATES. The chip says a page we HOLD states the opposite.
 *     "The claim is false" is a verdict; this pass renders none, and a tooltip
 *     that implied one would undo the whole design.
 *   * IT DRILLS. A chip that named external evidence without offering it would
 *     be asking to be taken on trust — the exact posture the checking layer
 *     exists to replace. So it is a button and it selects the record, the same
 *     `selectRow` idiom the absence rows (k5b) use.
 *
 * `contentionsModel.test.ts` proves the derivation; the derivation can be right
 * while the sentence still renders a bare `[2]`, which is why these mount the
 * real components.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import CitedProse from './CitedProse'
import { extractCitations } from '@/lib/citationsModel'
import { contentionsByOrdinal, type ContentionRow } from '@/lib/contentionsModel'
import { useSelection } from '@/state/selection'

const REF_1 = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'
const REF_2 = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb'
const CLAIM_ID = 'c'.repeat(64)

const BODY = {
  body: 'Gulf shipping insurance rose [[ref:1]]. The strait remains shut [[ref:2]].',
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
      },
      {
        marker: '[[ref:2]]',
        ordinal: 2,
        ref_id: REF_2,
        ref_kind: 'finding',
        title: 'Chokepoint sub-claim',
        source: 'chokepoint_watch',
        produced_at: '2026-09-18T07:00:00+00:00',
      },
    ],
  },
}

function contention(over: Partial<ContentionRow> = {}): ContentionRow {
  return {
    claim_id: CLAIM_ID,
    claim_text: 'The strait remains shut.',
    finding_id: REF_1,
    origin_head_id: REF_2,
    block_ordinal: 2,
    span_role: 'bluf',
    target_id: 'country_watch_ir',
    desk_key: 'country_watch_ir',
    analyst_id: 'country_composition',
    query: 'hormuz reopened operating resumed',
    query_source: 'polarity',
    query_novel_tokens: 3,
    stance: 'contradicts',
    derivation: 'polarity',
    reason: '',
    statement: 'retrieved counter-evidence … Not adjudicated.',
    rung: 'searxng',
    refs: [
      {
        url: 'https://news.example/hormuz-reopens',
        sha256: 'ab'.repeat(32),
        chars: 900,
        published_at: '2026-09-20',
        extracted: true,
        fetched_at: '2026-09-25T05:37:00+00:00',
        status_code: 200,
        stance: 'contradicts',
        quote: 'Shipping sources said the strait reopened on Tuesday.',
      },
    ],
    linked_signals: 0,
    pipeline_version: '2026-09/7a.1',
    retrieved_at: '2026-09-25T05:37:00+00:00',
    as_of: '2026-09-25T05:37:00+00:00',
    expires_at: '2026-10-02T05:37:00+00:00',
    live: true,
    ...over,
  }
}

function mount(rows: ContentionRow[] | null) {
  render(
    <CitedProse
      text={BODY.body}
      citations={extractCitations(BODY)}
      claimTags
      contentions={rows ? contentionsByOrdinal(rows) : null}
    />,
  )
}

function chips(): HTMLElement[] {
  return screen
    .queryAllByTestId('claim-fold-chip')
    .filter((el) => el.getAttribute('data-chip-kind') === 'contested-by-retrieval')
}

describe('the contested-by-retrieval chip', () => {
  beforeEach(() => {
    useSelection.setState({ selection: null, history: [] })
  })

  it('appears beside the contended ordinal and nowhere else', () => {
    mount([contention()])
    const found = chips()
    expect(found).toHaveLength(1)
    expect(found[0].textContent).toBe('contested by retrieval')
  })

  it('states the retrieval and never the verdict', () => {
    mount([contention()])
    const title = chips()[0].getAttribute('title') ?? ''
    expect(title).toContain('a page this platform holds states the opposite')
    expect(title).toContain('news.example')
    expect(title).toContain('published 2026-09-20')
    expect(title).toContain('Not adjudicated')
    for (const banned of ['is false', 'is wrong', 'therefore', 'we conclude']) {
      expect(title.toLowerCase()).not.toContain(banned)
    }
  })

  it('DRILLS to the record — the k5b idiom, not a new panel', () => {
    mount([contention()])
    expect(chips()[0].getAttribute('data-drills')).toBe('true')
    fireEvent.click(chips()[0])
    const sel = useSelection.getState().selection
    expect(sel?.kind).toBe('contention')
    expect(sel?.id).toBe(CLAIM_ID)
    expect(sel?.origin).toBe('claim-fold-chip')
  })

  it('reads a qualification at a lower volume than a contradiction', () => {
    // A `qualifies` record NARROWS a claim; it does not pull against it, and
    // rendering the two at one volume would overstate every qualification the
    // pass finds — which are its commonest real result.
    mount([contention({ stance: 'qualifies' })])
    expect(chips()[0].textContent).toBe('narrowed by retrieval')
    expect(chips()[0].className).toContain('warning')
  })

  it('shows an expired record as expired rather than hiding it', () => {
    mount([contention({ live: false })])
    expect(chips()).toHaveLength(1)
    expect(chips()[0].getAttribute('title')).toContain('expired, not re-checked')
  })

  it('chips nothing when the retrieval settled nothing', () => {
    // `none_found` is the EXPECTED common answer of a contrary pass. It says
    // the web had no opposition to offer, which is not a weakness of the claim.
    mount([contention({ stance: 'none_found', refs: [] })])
    expect(chips()).toHaveLength(0)
  })

  it('renders exactly as before when no records are in hand', () => {
    // `null` is "not read yet" and an empty list is "read, nothing contended".
    // Neither may chip, and the claim must read as it did before 7a existed.
    mount(null)
    expect(chips()).toHaveLength(0)
    mount([])
    expect(chips()).toHaveLength(0)
    expect(screen.getAllByTestId('citation-chip').length).toBeGreaterThan(0)
  })
})
