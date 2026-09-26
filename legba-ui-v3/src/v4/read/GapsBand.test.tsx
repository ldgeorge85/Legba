/**
 * GapsBand — the Morning Read's typed-absence band, rendered (k5b).
 *
 * `morningReadBands.test.ts` argues with the derivation. This file proves the
 * surface a reader meets, and every case names the failure it forbids:
 *
 *   * the seven kinds are all reachable, each with the ROUTE's own meaning
 *   * a stale item says LAST KNOWN, NOT RE-CHECKED — never passes for current
 *   * a kind the route could not read says "not measured", never renders blank
 *   * a desk read cleanly says so — "nothing absent" is an answer
 *   * a unit named by both a kind and the card appears ONCE, and the drop is
 *     counted rather than silently swallowed
 *   * the proof's `ref_kind` is a link only where it resolves
 *   * one glossary affordance, carrying the shared definition
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import GapsBand, { useGapViews } from './GapsBand'
import { TYPED_ABSENCE_GLOSSARY_NOTE, type AbsenceItem } from '@/lib/absenceModel'
import type { CountryScorecard } from '@/lib/evalOps'

const READ_AT = '2026-09-24T09:00:00Z'

function item(over: Partial<AbsenceItem> = {}): AbsenceItem {
  return {
    kind: 'collected_but_silent',
    subject: 'source.example.headlines',
    since: '2026-09-20T10:00:00Z',
    window: null,
    reason: 'this source is healthy and carried nothing for this desk',
    as_of: '2026-09-20T10:00:00Z',
    as_of_basis: "this source's most recent signal in this desk's scope",
    expires_at: '2026-09-20T14:00:00Z',
    review: null,
    stale: true,
    proof: {
      what_was_checked: 'signals whose geo overlaps [BR] in the last 30d',
      checked_at: '2026-09-20T10:00:00Z',
      ref: 'source.example.headlines',
      ref_kind: 'source',
    },
    ...over,
  }
}

const KINDS: Record<string, string> = {
  not_collected: 'nothing covers this subject for this desk',
  collected_but_silent: 'a covering source exists and is healthy',
  source_stale: 'the producer is past its own cadence budget',
  searched_found_nothing: 'the external audit searched and nothing decided the claim',
  search_failed: 'the search itself did not answer',
  below_floor: 'the banded scorecard refused to band it',
  layer_declared_absent: 'an operator declared this layer absent',
  // 7g-2 — the eighth kind. Present here for the same reason the other seven
  // are: the band renders the vocabulary the ROUTE publishes, and this test
  // fails if it starts rendering a subset of it.
  history_gap: 'a curated collection declares this and the observations table lacks it',
}

function answer(scope: string, over: Record<string, unknown> = {}) {
  return {
    version: '2026-09/k5',
    scope,
    read_at: READ_AT,
    kinds: KINDS,
    absences: [],
    not_measured: [],
    ...over,
  }
}

function card(target: string, dims: Record<string, unknown> = {}): CountryScorecard {
  return {
    id: `card-${target}`,
    target_id: target,
    produced_at: '2026-09-24T06:00:00Z',
    dimensions: dims,
  } as unknown as CountryScorecard
}

/** A `fetch` stub that answers `/v3/absence?scope=` per desk. */
function stubAbsence(byDesk: Record<string, unknown>, fail: string[] = []) {
  const mock = vi.fn(async (url: string) => {
    const u = String(url)
    const scope = decodeURIComponent(/scope=([^&]+)/.exec(u)?.[1] ?? '')
    if (fail.includes(scope)) {
      return {
        ok: false,
        status: 500,
        json: async () => ({}),
        text: async () => 'boom',
      } as unknown as Response
    }
    const body = byDesk[scope] ?? answer(scope)
    return {
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => '',
    } as unknown as Response
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

/** The band with its own hook, rendered the way `JudgmentBands` renders it. */
function Harness({ cards }: { cards: CountryScorecard[] }) {
  const views = useGapViews(cards)
  return <GapsBand {...views} state="ready" />
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

async function renderBand(cards: CountryScorecard[]) {
  render(wrap(<Harness cards={cards} />))
  await waitFor(() =>
    expect(screen.queryByTestId('morning-read-gaps-desk-loading')).toBeNull(),
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the eight kinds', () => {
  it('renders every kind that has items, with the route’s own meaning on hover', async () => {
    const all = Object.keys(KINDS).map((kind) =>
      item({ kind: kind as AbsenceItem['kind'], subject: `subject_${kind}` }),
    )
    stubAbsence({ country_g20_br: answer('country_g20_br', { absences: all }) })
    await renderBand([card('country_g20_br')])

    const groups = screen.getAllByTestId('morning-read-gaps-kind')
    expect(groups.map((g) => g.getAttribute('data-kind'))).toEqual(Object.keys(KINDS))
    for (const g of groups) {
      const kind = g.getAttribute('data-kind') as string
      // The meaning shown is the ROUTE's, not a label this surface invented.
      expect(within(g).getAllByTitle(KINDS[kind]).length).toBeGreaterThan(0)
    }
  })

  it('reads each desk exactly once', async () => {
    const mock = stubAbsence({})
    await renderBand([card('country_g20_br'), card('country_g20_ar')])
    const scopes = mock.mock.calls
      .map((c) => String(c[0]))
      .filter((u) => u.includes('/v3/absence'))
    expect(scopes).toHaveLength(2)
    expect(new Set(scopes).size).toBe(2)
  })
})

describe('the clock', () => {
  it('states a stale absence as LAST KNOWN, NOT RE-CHECKED', async () => {
    stubAbsence({
      country_g20_br: answer('country_g20_br', { absences: [item({ stale: true })] }),
    })
    await renderBand([card('country_g20_br')])
    const stamp = screen.getByTestId('morning-read-gaps-stamp')
    expect(stamp.textContent).toContain('last known absence, not re-checked')
    expect(stamp.textContent).toContain('measured')
    expect(
      screen.getByTestId('morning-read-gaps-absence').getAttribute('data-stale'),
    ).toBe('true')
  })

  it('states a current absence as current until its own expiry', async () => {
    stubAbsence({
      country_g20_br: answer('country_g20_br', {
        absences: [item({ stale: false, expires_at: '2026-12-01T00:00:00Z' })],
      }),
    })
    await renderBand([card('country_g20_br')])
    const stamp = screen.getByTestId('morning-read-gaps-stamp')
    expect(stamp.textContent).toContain('current until')
    expect(stamp.textContent).not.toContain('last known')
  })
})

describe('the three answers are never collapsed', () => {
  it('says NOT MEASURED, in the route’s own words, rather than rendering blank', async () => {
    stubAbsence({
      country_g20_br: answer('country_g20_br', {
        absences: [item()],
        not_measured: [
          'searched_found_nothing / search_failed: external_grades could not be read (UndefinedTableError)',
        ],
      }),
    })
    await renderBand([card('country_g20_br')])
    const lines = screen.getAllByTestId('morning-read-gaps-not-measured')
    // BOTH kinds the one entry names, not just the first.
    expect(lines.map((l) => l.getAttribute('data-kind')).sort()).toEqual([
      'search_failed',
      'searched_found_nothing',
    ])
    expect(lines[0].textContent).toContain('not measured for this desk')
    expect(lines[0].textContent).toContain('UndefinedTableError')
  })

  it('a desk read cleanly is NAMED as read, never silently dropped', async () => {
    stubAbsence({ country_g20_br: answer('country_g20_br') })
    await renderBand([card('country_g20_br')])
    expect(screen.getByTestId('morning-read-gaps-all-clear').textContent).toContain(
      'No typed absence is recorded on any desk read here',
    )
    // The desk is still on the page: "we looked here and found nothing" is an
    // answer, and a band that hid it would imply it never looked.
    expect(screen.getByTestId('morning-read-gaps-clean').textContent).toContain(
      'Brazil',
    )
  })

  it('a desk with only a card gap states the absence read as empty, not missing', async () => {
    stubAbsence({ country_g20_br: answer('country_g20_br') })
    await renderBand([
      card('country_g20_br', {
        energy_security: { band: 'insufficient-evidence', reason: 'below-floor' },
      }),
    ])
    expect(screen.getByTestId('morning-read-gaps-desk-empty').textContent).toContain(
      'No typed absence is recorded for this desk',
    )
    // And the seven kinds it DID read are named as read.
    expect(screen.getByTestId('morning-read-gaps-clear-kinds').textContent).toContain(
      'Read for this desk, nothing absent',
    )
    expect(screen.getAllByTestId('morning-read-gaps-row')).toHaveLength(1)
  })

  it('a desk whose read failed says so instead of reading as covered', async () => {
    stubAbsence({}, ['country_g20_br'])
    await renderBand([card('country_g20_br')])
    expect(screen.getByTestId('morning-read-gaps-desk-error').textContent).toContain(
      'did not answer',
    )
    expect(screen.queryByTestId('morning-read-gaps-desk-empty')).toBeNull()
  })

  it('an empty band says nothing was READ, not that nothing is missing', async () => {
    stubAbsence({})
    await renderBand([])
    expect(screen.getByTestId('morning-read-gaps-empty').textContent).toContain(
      'because nothing was read',
    )
  })
})

describe('the card rows', () => {
  it('shows a card gap no kind names, and never repeats one a kind does', async () => {
    stubAbsence({
      country_g20_br: answer('country_g20_br', {
        absences: [item({ kind: 'source_stale', subject: 'energy_security' })],
      }),
    })
    await renderBand([
      card('country_g20_br', {
        energy_security: { band: 'insufficient-evidence', reason: 'below-floor' },
        military_posture: { band: 'insufficient-evidence', reason: 'below-floor' },
      }),
    ])
    const rows = screen.getAllByTestId('morning-read-gaps-row')
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain('military_posture')
    // The dropped row is COUNTED — a de-dupe that hides its own arithmetic is
    // indistinguishable from a row that was lost.
    expect(screen.getByTestId('morning-read-gaps-deduped').textContent).toContain(
      '1 card row not repeated here',
    )
  })
})

describe('the proof', () => {
  it('offers the ref as a control only where its kind resolves', async () => {
    stubAbsence({
      country_g20_br: answer('country_g20_br', {
        absences: [
          item({ subject: 'a', proof: { ...item().proof, ref_kind: 'source' } }),
          item({
            kind: 'below_floor',
            subject: 'b',
            proof: { ...item().proof, ref: 'card-1', ref_kind: 'scorecard' },
          }),
        ],
      }),
    })
    await renderBand([card('country_g20_br')])
    // `source` resolves to an Inspector selection; `scorecard` does not and is
    // shown as text rather than a click that would 404.
    expect(screen.getByTestId('morning-read-gaps-proof-ref').textContent).toBe('source')
    expect(screen.getByTestId('morning-read-gaps-proof-text').textContent).toBe(
      'scorecard',
    )
  })
})

describe('the glossary link', () => {
  it('carries the shared typed-absence definition, once', async () => {
    stubAbsence({})
    await renderBand([card('country_g20_br')])
    const link = screen.getByTestId('morning-read-gaps-glossary')
    expect(link.getAttribute('title')).toBe(TYPED_ABSENCE_GLOSSARY_NOTE)
    expect(screen.getAllByTestId('morning-read-gaps-glossary')).toHaveLength(1)
  })
})
