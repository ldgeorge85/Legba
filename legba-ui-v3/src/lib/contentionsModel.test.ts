/**
 * Unit tests for the contrary-evidence reading model (Program 7a).
 *
 * Three things are locked here, and the first is the one the whole lane rests
 * on: NOTHING ON THIS SURFACE MAY READ A STANCE AS A VERDICT. `contradicts`
 * means a page this platform fetched and holds states the opposite of a claim
 * we published; it does not mean the claim is false, and the sentence a chip
 * shows has to stop where the retrieval stopped.
 *
 * Then the join — block N IS citation `[[ref:N]]`, so no hashing happens in the
 * browser — and the honesty contract the rest of the reading kit keeps: a
 * retrieval that settled nothing chips nothing, and an expired record is
 * labelled rather than hidden.
 *
 * And, since 0222, THE FOUR FENCES: the readings that decide whether a page was
 * allowed to carry a stance at all. Two rules are locked over them and neither
 * is cosmetic — a reading nobody took prints as `not measured` and never as 0
 * (a row written before the fence pass shipped measured none of them), and the
 * F4 count prints against its bar, because "1" means nothing to a reader who
 * does not already know that a contradiction takes two independent outlets.
 */
import { describe, it, expect } from 'vitest'

import {
  contentionFences,
  contentionPages,
  contentionReason,
  contentionsByOrdinal,
  decisiveRef,
  independentPagesLine,
  refFencesRead,
  NOT_MEASURED,
  type ContentionRef,
  type ContentionRow,
} from './contentionsModel'

function row(over: Partial<ContentionRow> = {}): ContentionRow {
  return {
    claim_id: 'c'.repeat(64),
    claim_text: 'The Strait of Hormuz remains effectively shut.',
    finding_id: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa',
    origin_head_id: 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb',
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

describe('contentionsByOrdinal', () => {
  it('keys a record by the ordinal it was written against', () => {
    const map = contentionsByOrdinal([row()])
    expect([...map.keys()]).toEqual([2])
    expect(map.get(2)?.stance).toBe('contradicts')
  })

  it('skips a retrieval that settled nothing', () => {
    // `none_found` is the pass's EXPECTED common answer — the web had no
    // opposition to offer — and it is not a weakness of the claim. A chip for
    // it would be a claim the platform has not earned.
    const map = contentionsByOrdinal([
      row({ stance: 'none_found', refs: [] }),
      row({ stance: 'search_failed', block_ordinal: 3, refs: [] }),
    ])
    expect(map.size).toBe(0)
  })

  it('keeps the newest record when a claim was contended more than once', () => {
    // The route orders `as_of DESC`; an older retrieval is history, not a
    // second opinion, so the first row wins and nothing is merged.
    const map = contentionsByOrdinal([
      row({ query: 'newest' }),
      row({ query: 'older' }),
    ])
    expect(map.get(2)?.query).toBe('newest')
  })

  it('is empty for anything malformed, and never throws', () => {
    expect(contentionsByOrdinal(null).size).toBe(0)
    expect(contentionsByOrdinal(undefined).size).toBe(0)
    expect(contentionsByOrdinal([row({ block_ordinal: null })]).size).toBe(0)
  })
})

describe('decisiveRef', () => {
  it('prefers the page that carries the stance', () => {
    const r = decisiveRef(
      row({
        refs: [
          { ...row().refs[0], url: 'https://silent.example/a', stance: 'none_found' },
          { ...row().refs[0], url: 'https://loud.example/b', stance: 'contradicts' },
        ],
      }),
    )
    expect(r?.url).toBe('https://loud.example/b')
  })

  it('is null when nothing was fetched', () => {
    expect(decisiveRef(row({ refs: [] }))).toBeNull()
    expect(decisiveRef(null)).toBeNull()
  })
})

describe('contentionReason', () => {
  it('describes the RETRIEVAL and never adjudicates the claim', () => {
    const reason = contentionReason(row())
    expect(reason).toContain('a page this platform holds states the opposite')
    expect(reason).toContain('news.example')
    expect(reason).toContain('published 2026-09-20')
    expect(reason).toContain('Not adjudicated')
    // The four sentences a verdict would use, and none of them appears.
    for (const banned of ['is false', 'is wrong', 'therefore', 'we conclude']) {
      expect(reason.toLowerCase()).not.toContain(banned)
    }
  })

  it('says a qualification NARROWS rather than opposes', () => {
    expect(contentionReason(row({ stance: 'qualifies' }))).toContain('narrows this')
  })

  it('names the uncalibrated rule when the fallback produced the stance', () => {
    // The `negation` derivation has had no live sweep behind it; a reader is
    // told so, here, because this is the surface where the counter-ref is one
    // click away and a human can judge it.
    expect(contentionReason(row({ derivation: 'negation' }))).toContain(
      'uncalibrated negation rule',
    )
  })

  it('labels an expired record rather than hiding it', () => {
    // Hiding it would make a pass that STOPPED RUNNING look exactly like a week
    // with nothing to contend.
    expect(contentionReason(row({ live: false }))).toContain('expired, not re-checked')
  })

  it('renders a missing publication date as absence, never as a date', () => {
    const reason = contentionReason(
      row({ refs: [{ ...row().refs[0], published_at: null }] }),
    )
    expect(reason).not.toContain('published')
    expect(reason).toContain('news.example')
  })
})

// ---------------------------------------------------------------------------
// The four fences (migration 0222)
// ---------------------------------------------------------------------------

/** A page the fence pass READ — `host_class` is the marker, and its producer
 *  vocabulary is closed, so an empty one can only mean "never read". */
function page(over: Partial<ContentionRef> = {}): ContentionRef {
  return {
    url: 'https://news.example/hormuz-reopens',
    sha256: 'ab'.repeat(32),
    chars: 900,
    published_at: '2026-09-20',
    extracted: true,
    fetched_at: '2026-09-25T05:37:00+00:00',
    status_code: 200,
    stance: 'contradicts',
    quote: 'Shipping sources said the strait reopened on Tuesday.',
    host_class: 'reporting',
    page_published_at: '2026-09-20',
    subject_overlap: 4,
    fence: '',
    ...over,
  }
}

describe('refFencesRead', () => {
  it('reads an empty host class as NEVER MEASURED, not as an unknown host', () => {
    // `host_class_of` returns a member of a CLOSED vocabulary for every URL —
    // `unknown` for a host it does not recognise — so `''` can only be a row
    // written before the fence pass existed. Its `subject_overlap: 0` is a
    // serialisation default, and printing it as an overlap of zero would be
    // inventing a reading nobody took.
    expect(refFencesRead(page())).toBe(true)
    expect(refFencesRead(page({ host_class: 'unknown' }))).toBe(true)
    expect(refFencesRead(page({ host_class: '' }))).toBe(false)
    expect(refFencesRead(page({ host_class: undefined }))).toBe(false)
    expect(refFencesRead(null)).toBe(false)
  })
})

describe('independentPagesLine', () => {
  it('prints the count against the bar a contradiction needs', () => {
    expect(independentPagesLine(row({ independent_pages: 2 }))).toBe(
      '2 of 2 independent pages — the bar a contradiction needs',
    )
    expect(independentPagesLine(row({ independent_pages: 3 }))).toContain('3 of 2')
  })

  it('says what ONE admissible page was not enough for', () => {
    // F4's demotion, in words: the page passed F1–F3 and the record is still a
    // qualification, because two mastheads running one dispatch are one source
    // and a single outlet cannot clear the bar.
    expect(
      independentPagesLine(row({ stance: 'qualifies', independent_pages: 1 })),
    ).toBe('1 of 2 independent pages — one admissible page, not enough to contradict')
  })

  it('distinguishes "none contradicted" from "one was not enough"', () => {
    const none = independentPagesLine(row({ stance: 'qualifies', independent_pages: 0 }))
    expect(none).toContain('0 of 2 independent pages')
    expect(none).toContain('no admissible page contradicted')
    expect(none).not.toContain('one admissible page')
  })

  it('prints a fence nobody ran as NOT MEASURED and never as 0', () => {
    // A row written before 0222 measured none of the four. "Not measured" is
    // not "measured, and the answer was nothing", and a 0 here would read as
    // the pass having looked and found no independent page.
    expect(independentPagesLine(row({ independent_pages: null }))).toBe(NOT_MEASURED)
    expect(independentPagesLine(row({ independent_pages: undefined }))).toBe(NOT_MEASURED)
    expect(independentPagesLine(row())).toBe(NOT_MEASURED)
  })
})

describe('contentionFences', () => {
  it('reads the decisive page: host class, the date the gate parsed, the overlap', () => {
    const f = contentionFences(
      row({
        host_class: 'reporting',
        page_published_at: '2026-09-24',
        subject_overlap: 4,
        independent_pages: 2,
      }),
    )
    expect(f.host_class).toBe('reporting')
    expect(f.page_date).toBe('2026-09-24')
    expect(f.subject_overlap).toBe("4 of the claim's subject words in the matched sentence")
    expect(f.independent_pages).toContain('2 of 2 independent pages')
  })

  it('prints all four as absence on a row that measured none of them', () => {
    const f = contentionFences(
      row({
        host_class: null,
        page_published_at: null,
        subject_overlap: null,
        independent_pages: null,
      }),
    )
    expect(Object.values(f)).toEqual([
      NOT_MEASURED,
      NOT_MEASURED,
      NOT_MEASURED,
      NOT_MEASURED,
    ])
    // The one character that must never appear for an unmeasured fence.
    expect(JSON.stringify(f)).not.toContain('0')
  })
})

describe('contentionPages', () => {
  it('lists EVERY fetched page with the reading that admitted or refused it', () => {
    // A contradiction needs two independent outlets, so a drill that showed one
    // page would assert the bar and withhold the test of it.
    const pages = contentionPages(
      row({
        stance: 'contradicts',
        independent_pages: 2,
        refs: [
          page(),
          page({
            url: 'https://wire.example/strait-open',
            host_class: 'analysis',
            page_published_at: '2026-09-21',
            subject_overlap: 3,
          }),
        ],
      }),
    )
    expect(pages).toHaveLength(2)
    expect(pages.map((p) => p.url)).toEqual([
      'https://news.example/hormuz-reopens',
      'https://wire.example/strait-open',
    ])
    expect(pages.map((p) => p.host_class)).toEqual(['reporting', 'analysis'])
    expect(pages.map((p) => p.page_date)).toEqual(['2026-09-20', '2026-09-21'])
    expect(pages.map((p) => p.subject_overlap)).toEqual(['4', '3'])
    // An admissible page carries no fence, and the key is omitted rather than
    // rendered as an empty one.
    expect(pages.every((p) => !('fence' in p))).toBe(true)
  })

  it('names the fence that refused a page, and its OWN stance', () => {
    // F1 takes a reference host's page to `none_found` while the record keeps
    // the best page it read — so the page's stance is not the record's, and a
    // reader who can see that the encyclopedia entry settled nothing has learnt
    // why the count is what it is.
    const [p] = contentionPages(
      row({
        refs: [
          page({
            url: 'https://en.wikipedia.org/wiki/Strait_of_Hormuz',
            host_class: 'reference',
            stance: 'none_found',
            fence: 'host_class',
          }),
        ],
      }),
    )
    expect(p.host_class).toBe('reference')
    expect(p.stance).toBe('none_found')
    expect(p.fence).toBe('host_class')
  })

  it('separates "the gate read no date" from "the fences never ran"', () => {
    // Both are honest absences and they are NOT the same absence: the first is
    // the date gate reporting a page it could not date — which is exactly why
    // that page cannot contradict — and the second is a row from before 0222.
    const [gated, never] = contentionPages(
      row({
        refs: [
          page({ page_published_at: null }),
          page({ host_class: '', page_published_at: null, subject_overlap: 0 }),
        ],
      }),
    )
    expect(gated.page_date).toBe('no date the gate could read')
    expect(gated.subject_overlap).toBe('4')
    expect(never.page_date).toBe(NOT_MEASURED)
    expect(never.host_class).toBe(NOT_MEASURED)
    // The serialisation default, refused: 0 here would be an overlap nobody
    // measured, printed as one that was.
    expect(never.subject_overlap).toBe(NOT_MEASURED)
  })

  it('is empty for a record that fetched nothing, and never throws', () => {
    expect(contentionPages(row({ refs: [] }))).toEqual([])
    expect(contentionPages(null)).toEqual([])
    expect(contentionPages(undefined)).toEqual([])
  })
})
