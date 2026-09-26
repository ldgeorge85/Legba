/**
 * JudgmentBands — the four bands, rendered.
 *
 * `morningReadBands.test.ts` argues with each derivation. This file proves the
 * surface a reader actually meets:
 *
 *   * the four band containers exist, in the reading order, with their counts
 *   * an empty desk states the absence instead of rendering blank
 *   * a desk with two unsupported claims shows both SENTENCES and their source
 *   * an expired forecast shows its FROZEN resolution test
 *   * the rail and the read timer are present, and the timer emits nothing
 *   * a failed route says so rather than reading as "nothing happened"
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import JudgmentBands, { CHECKED_LIMIT } from './JudgmentBands'
import { __pendingReadEvents, __resetReadTelemetry } from '@/lib/readTelemetry'
import { bandTestId } from '@/lib/morningReadBands'

// ---------------------------------------------------------------------------
// A URL-routed stub over the three routes the surface reads.
// ---------------------------------------------------------------------------

type Route = 'since' | 'findings' | 'scorecards' | 'absence'

interface Payloads {
  since?: unknown
  findings?: unknown
  scorecards?: unknown
  /** k5b — the GAPS band reads `/v3/absence?scope=` once per desk it shows;
   *  keyed by desk so a test can give two desks different answers. */
  absence?: Record<string, unknown>
  /** Routes listed here answer 500 — the honest-failure cases. */
  fail?: Array<Route>
}

/** An empty but WELL-FORMED absence answer: read cleanly, nothing absent. */
export function emptyAbsence(scope: string) {
  return {
    version: '2026-09/k5',
    scope,
    read_at: '2026-09-24T09:00:00Z',
    kinds: {},
    absences: [],
    not_measured: [],
  }
}

function stub(p: Payloads = {}) {
  const fail = new Set(p.fail ?? [])
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url)
    const which: Route = u.includes('/v3/absence')
      ? 'absence'
      : u.includes('/v3/since')
        ? 'since'
        : u.includes('/v3/eval/country_scorecard')
          ? 'scorecards'
          : 'findings'
    if (fail.has(which)) {
      return {
        ok: false,
        status: 500,
        json: async () => ({}),
        text: async () => 'boom',
      } as unknown as Response
    }
    const scope = /scope=([^&]+)/.exec(u)?.[1] ?? ''
    const body =
      which === 'absence'
        ? (p.absence?.[decodeURIComponent(scope)] ?? emptyAbsence(decodeURIComponent(scope)))
        : which === 'since'
          ? (p.since ?? emptySince())
          : which === 'scorecards'
            ? (p.scorecards ?? [])
            : (p.findings ?? { data: [] })
    return {
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => '',
    } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function sect<T>(items: T[]) {
  return { items, total: items.length, truncated: false }
}

function emptySince() {
  return {
    cursor: '2026-09-23T06:00:00Z',
    server_now: '2026-09-24T09:00:00Z',
    counts: {},
    new_findings: sect([]),
    superseded: sect([]),
    band_changes: sect([]),
    situations: sect([]),
    alerts: sect([]),
    forecasts_due: sect([]),
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  __resetReadTelemetry()
  localStorage.clear()
  sessionStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function renderBands(p: Payloads = {}) {
  stub(p)
  render(wrap(<JudgmentBands />))
  await screen.findByTestId('morning-read-judgment')
  // Every band paints from a query; wait for the last one to settle.
  await waitFor(() =>
    expect(screen.queryByTestId('morning-read-gaps-loading')).toBeNull(),
  )
}

// ---------------------------------------------------------------------------
// The four bands, in order
// ---------------------------------------------------------------------------

describe('the surface', () => {
  it('renders the four band containers in the reading order', async () => {
    await renderBands()
    for (const id of ['changed', 'checked', 'gaps', 'due'] as const) {
      expect(screen.getByTestId(bandTestId(id))).toBeTruthy()
    }
    const surface = screen.getByTestId('morning-read-judgment')
    const order = [...surface.querySelectorAll('[data-band]')].map((el) =>
      el.getAttribute('data-band'),
    )
    expect(order).toEqual(['changed', 'checked', 'gaps', 'due'])
  })

  it('carries the reading-order rail and the read timer, and emits nothing', async () => {
    await renderBands()
    const rail = screen.getByTestId('morning-read-rail')
    expect(within(rail).getByTestId('morning-read-rail-changed')).toBeTruthy()
    expect(within(rail).getByTestId('morning-read-rail-due')).toBeTruthy()
    expect(within(rail).getByTestId('morning-read-timer').textContent).toContain('/ 20:00')
    // A first visit says which window it is showing.
    expect(screen.getByTestId('morning-read-first-visit')).toBeTruthy()
    // Rule 4: the timer never inflates the wager's own metric.
    expect(__pendingReadEvents()).toHaveLength(0)
  })

  it('states each absence instead of rendering four blank bands', async () => {
    await renderBands()
    expect(screen.getByTestId('morning-read-changed-empty').textContent).toContain(
      'Nothing landed since',
    )
    expect(screen.getByTestId('morning-read-checked-empty')).toBeTruthy()
    expect(screen.getByTestId('morning-read-gaps-empty')).toBeTruthy()
    expect(screen.getByTestId('morning-read-due-empty')).toBeTruthy()
    for (const id of ['changed', 'checked', 'gaps', 'due'] as const) {
      expect(screen.getByTestId(bandTestId(id)).getAttribute('data-count')).toBe('0')
    }
  })

  it('says a route FAILED rather than reading as "nothing happened"', async () => {
    await renderBands({ fail: ['since', 'scorecards'] })
    expect(screen.getByTestId('morning-read-changed-error')).toBeTruthy()
    expect(screen.getByTestId('morning-read-due-error')).toBeTruthy()
    expect(screen.getByTestId('morning-read-gaps-error')).toBeTruthy()
    // The band that DID answer is still empty-but-honest, not an error.
    expect(screen.getByTestId('morning-read-checked-empty')).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// Counts on the band heads and the rail
// ---------------------------------------------------------------------------

describe('the counts', () => {
  it('puts each band’s count on its container AND on the rail', async () => {
    await renderBands({
      since: {
        ...emptySince(),
        new_findings: sect([
          {
            id: 'f1',
            analyst_id: 'security_desk',
            target_id: 'country_g20_br',
            title: 'Border incident',
            severity: 'high',
            confidence: 0.8,
            faithfulness_score: 0.9,
            effective_confidence: 0.8,
            produced_at: '2026-09-24T07:00:00Z',
          },
        ]),
        forecasts_due: sect([
          {
            id: 'due1',
            region: 'country_g20_br',
            event_class: 'hazard_severe',
            window_start: '2026-09-14T00:00:00Z',
            window_end: '2026-09-21T00:00:00Z',
            p: 0.31,
            p_base: 0.22,
            method: 'recent_rate_poisson',
            method_version: 'forecast_acute/2026-09.1',
            resolution_test: 'retro: class=hazard_severe; o=1 iff >=1 signal; grace=1d',
            resolved_by: 'unresolved:expired',
            issued_at: '2026-09-14T00:00:00Z',
            days_overdue: 9.2,
            mark: 'expired',
          },
        ]),
      },
      scorecards: [
        {
          target_id: 'country_g20_in',
          id: 'card-in',
          produced_at: '2026-09-24T05:00:00Z',
          generated_at: '2026-09-24T05:00:00Z',
          floors: {},
          dimensions: {
            economy: {
              band: 'insufficient-evidence',
              basis: [],
              severity_tag: null,
              effective_confidence: null,
              confidence: null,
              critic_score: null,
              damped: false,
              reason: 'no-finding',
              produced_at: null,
              eval: {},
            },
          },
          composition: { present: true, basis: ['x'] },
        },
      ],
    })

    expect(screen.getByTestId('morning-read-changed').getAttribute('data-count')).toBe('1')
    expect(screen.getByTestId('morning-read-gaps').getAttribute('data-count')).toBe('1')
    expect(screen.getByTestId('morning-read-due').getAttribute('data-count')).toBe('1')

    const rail = screen.getByTestId('morning-read-rail')
    expect(within(rail).getByTestId('morning-read-rail-changed').textContent).toBe('1. Changed1')
    expect(within(rail).getByTestId('morning-read-rail-due').textContent).toBe('4. Due1')

    // CHANGED cites the row, humanized — never raw snake_case.
    const changed = screen.getByTestId('morning-read-changed')
    expect(within(changed).getByTestId('morning-read-changed-row').textContent).toContain(
      'Border incident',
    )
    expect(within(changed).getByText('Brazil')).toBeTruthy()

    // GAPS names the dimension and the card's own reason.
    const gaps = screen.getByTestId('morning-read-gaps')
    const gapRow = within(gaps).getByTestId('morning-read-gaps-row')
    expect(gapRow.textContent).toContain('economy')
    expect(gapRow.textContent).toContain('no unit finding yet')
  })
})

// ---------------------------------------------------------------------------
// CHECKED — two unsupported claims on one desk
// ---------------------------------------------------------------------------

describe('CHECKED', () => {
  it('quotes both flagged sentences with the finding they came from', async () => {
    await renderBands({
      findings: {
        data: [
          {
            id: 'f-bad',
            kind: 'finding',
            title: 'Security read for Brazil',
            analyst_id: 'security_desk',
            analyst_version: null,
            target_id: 'country_g20_br',
            target_version: null,
            confidence: 0.8,
            severity: 'high',
            schema_uri: 'iglu:legba/finding/jsonschema/1-0-0',
            produced_at: '2026-09-24T07:00:00Z',
            citations: [],
            verification: {
              faithfulness_score: 0.62,
              judge_status: 'llm',
              checkable_claims: 5,
              supported_claims: 3,
              unsupported_spans: [
                { text: 'Troop numbers doubled overnight.', reason: 'no_citation', markers: [] },
                { text: 'The port remains closed.', reason: 'judge_contradicted', markers: [3] },
              ],
            },
          },
          {
            id: 'f-ok',
            kind: 'finding',
            title: 'Economy read for Brazil',
            analyst_id: 'economy_desk',
            analyst_version: null,
            target_id: 'country_g20_br',
            target_version: null,
            confidence: 0.9,
            severity: 'low',
            schema_uri: 'iglu:legba/finding/jsonschema/1-0-0',
            produced_at: '2026-09-24T07:30:00Z',
            citations: [],
            verification: {
              faithfulness_score: 0.95,
              judge_status: 'llm',
              checkable_claims: 4,
              supported_claims: 4,
              unsupported_spans: [],
            },
          },
          {
            id: 'f-none',
            kind: 'finding',
            title: 'Unverified note',
            analyst_id: 'notes',
            analyst_version: null,
            target_id: 'country_g20_br',
            target_version: null,
            confidence: 0.5,
            severity: 'low',
            schema_uri: 'iglu:legba/finding/jsonschema/1-0-0',
            produced_at: '2026-09-24T07:45:00Z',
            citations: [],
            verification: null,
          },
        ],
      },
    })

    const checked = screen.getByTestId('morning-read-checked')
    expect(checked.getAttribute('data-count')).toBe('2')

    const claims = within(checked).getAllByTestId('morning-read-checked-claim')
    expect(claims).toHaveLength(2)
    expect(claims[0].textContent).toContain('Troop numbers doubled overnight.')
    expect(claims[0].textContent).toContain('no citation')
    // The SOURCE travels with the sentence.
    expect(claims[0].textContent).toContain('security_desk')
    expect(claims[0].textContent).toContain('Security read for Brazil')
    expect(claims[1].textContent).toContain('contradicted by the verify judge')
    expect(claims[1].textContent).toContain('cited [3]')

    // The desk line separates verified from flagged from UNCHECKED (rule 2).
    const desk = within(checked).getByTestId('morning-read-checked-desk')
    expect(desk.textContent).toContain('1 verified')
    expect(desk.textContent).toContain('1 with a flagged claim')
    expect(desk.textContent).toContain('1 unchecked')
  })

  it('says when the window held MORE findings than the page it read', async () => {
    await renderBands({
      findings: {
        data: Array.from({ length: CHECKED_LIMIT }, (_, i) => ({
          id: `f${i}`,
          kind: 'finding',
          title: `finding ${i}`,
          analyst_id: 'security_desk',
          analyst_version: null,
          target_id: 'country_g20_br',
          target_version: null,
          confidence: 0.8,
          severity: 'low',
          schema_uri: 'iglu:legba/finding/jsonschema/1-0-0',
          produced_at: '2026-09-24T07:00:00Z',
          citations: [],
          verification: { faithfulness_score: 0.9, unsupported_spans: [] },
        })),
      },
    })
    expect(screen.getByTestId('morning-read-checked-truncated').textContent).toContain(
      `newest ${CHECKED_LIMIT} findings`,
    )
  })
})

// ---------------------------------------------------------------------------
// DUE — the frozen resolution test
// ---------------------------------------------------------------------------

describe('DUE', () => {
  it('shows an expired call with its FROZEN resolution test, retro prefix intact', async () => {
    await renderBands({
      since: {
        ...emptySince(),
        forecasts_due: sect([
          {
            id: 'due1',
            region: 'country_g20_br',
            event_class: 'hazard_severe',
            window_start: '2026-09-07T00:00:00Z',
            window_end: '2026-09-14T00:00:00Z',
            p: 0.31,
            p_base: 0.22,
            method: 'recent_rate_poisson',
            method_version: 'forecast_acute/2026-09.1',
            resolution_test:
              'retro: class=hazard_severe; o=1 iff >=1 signal geo-overlapping the region; grace=1d',
            resolved_by: 'unresolved:expired',
            issued_at: '2026-09-07T00:00:00Z',
            days_overdue: 9.2,
            mark: 'expired',
          },
        ]),
      },
    })

    const due = screen.getByTestId('morning-read-due')
    const row = within(due).getByTestId('morning-read-due-row')
    expect(row.textContent).toContain('hazard_severe')
    expect(row.textContent).toContain('9.2d past its window')
    expect(row.textContent).toContain('the resolver ran and could not grade it')

    const test = within(due).getByTestId('morning-read-due-test')
    expect(test.textContent?.startsWith('retro: ')).toBe(true)
    expect(test.textContent).toContain('o=1 iff >=1 signal')
  })
})
