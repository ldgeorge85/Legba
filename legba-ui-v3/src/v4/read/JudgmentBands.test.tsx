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
import { resetScope, useScope } from '@/state/scope'
import { scopeFromTarget } from '@/lib/scopeFromReport'
import { fireEvent } from '@testing-library/react'

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
    // X4 READPATH — the unchecked finding is split by the registry's state (a
    // null block from an older registry reads `not judged`) and LISTED with
    // its mark, never left as a plain count.
    expect(desk.textContent).toContain('unverified: 0 judge pending, 1 not judged')
    const unverified = within(desk).getAllByTestId('morning-read-checked-unverified-row')
    expect(unverified).toHaveLength(1)
    expect(unverified[0].textContent).toContain('Unverified note')
    expect(within(unverified[0]).getByTestId('unverified-state').textContent).toBe(
      'unverified · not judged',
    )
  })

  it('X4 READPATH — a just-written finding reads `judge pending`, a lost one `not judged`; never plain, never 0', async () => {
    const base = {
      kind: 'finding',
      analyst_version: null,
      target_id: 'country_g20_jp',
      target_version: null,
      confidence: 0.87,
      severity: 'elevated',
      schema_uri: 'iglu:legba/finding/jsonschema/1-0-0',
      citations: [],
    }
    await renderBands({
      findings: {
        data: [
          {
            ...base,
            id: 'f-pending',
            title: 'Japan escalation read',
            analyst_id: 'escalation',
            produced_at: '2026-09-24T08:58:00Z',
            verification: {
              score_state: 'pending',
              since: '2026-09-24T08:58:00+00:00',
              judge_window_s: 900,
              reason: 'awaiting_verify',
            },
          },
          {
            ...base,
            id: 'f-lost',
            title: 'Japan posture read',
            analyst_id: 'military_posture',
            produced_at: '2026-09-24T04:41:26Z',
            verification: {
              score_state: 'absent',
              since: '2026-09-24T04:41:26+00:00',
              judge_window_s: 900,
              reason: 'no_verdict',
            },
          },
        ],
      },
    })
    const checked = screen.getByTestId('morning-read-checked')
    // Nothing verified, nothing flagged — and the band does not call that a pass.
    expect(screen.getByTestId('morning-read-checked-note').textContent).toBe(
      '0 verified · 0 claims flagged · unverified: 1 judge pending, 1 not judged',
    )
    const desk = within(checked).getByTestId('morning-read-checked-desk')
    expect(desk.textContent).not.toContain('Nothing flagged on this desk.')
    expect(desk.textContent).toContain('Nothing on this desk carries a verify verdict, so nothing is claimed as held.')
    const marks = within(desk).getAllByTestId('unverified-state')
    expect(marks.map((m) => [m.getAttribute('data-state'), m.textContent])).toEqual([
      ['pending', 'unverified · judge pending'],
      ['absent', 'unverified · not judged'],
    ])
    // The mark explains itself, with its clock.
    expect(marks[0].getAttribute('title')).toContain('within 15 minutes')
    expect(marks[1].getAttribute('title')).toContain('Nothing will grade this finding')
    for (const m of marks) expect(m.textContent).not.toMatch(/\b0(\.0+)?\b/)
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

// ---------------------------------------------------------------------------
// X1/D1 — nothing prints before it is read
// ---------------------------------------------------------------------------

describe('a count before its band has been read (X1/D1)', () => {
  it('prints — on every band head and the rail while the routes are in flight, never 0', async () => {
    // Every route hangs: the landing's first seconds, frozen.
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})))
    render(wrap(<JudgmentBands />))
    await screen.findByTestId('morning-read-judgment')

    for (const id of ['changed', 'checked', 'gaps', 'due'] as const) {
      const band = screen.getByTestId(bandTestId(id))
      expect(band.getAttribute('data-count')).toBeNull()
      expect(band.getAttribute('data-count-state')).toBe('pending')
      const note = screen.getByTestId(`${bandTestId(id)}-note`)
      expect(note.textContent).toBe('—')
      expect(note.textContent).not.toMatch(/\b0\b/)
      expect(screen.getByTestId(`morning-read-rail-count-${id}`).textContent).toBe('—')
    }
    // The band bodies say what they are doing; no head claims a number.
    expect(screen.getByTestId('morning-read-changed-loading')).toBeTruthy()
    expect(screen.getByTestId('morning-read-checked-loading')).toBeTruthy()
  })

  it('prints "unavailable", not 0, for a band whose route failed', async () => {
    await renderBands({ fail: ['since'] })
    for (const id of ['changed', 'due'] as const) {
      expect(screen.getByTestId(bandTestId(id)).getAttribute('data-count-state')).toBe('unavailable')
      expect(screen.getByTestId(`${bandTestId(id)}-note`).textContent).toBe('unavailable')
      expect(screen.getByTestId(`morning-read-rail-count-${id}`).textContent).toBe('unavailable')
    }
    // The band that answered prints its measured zero.
    expect(screen.getByTestId('morning-read-rail-count-checked').textContent).toBe('0')
  })
})

// ---------------------------------------------------------------------------
// X3/D3 — a SUBJECT scope narrows every band (review D §C5: a rail click on
// Brazil scoped the feed while the bands kept listing Ukraine and Iran)
// ---------------------------------------------------------------------------

describe('a subject scope (X3/D3)', () => {
  afterEach(() => resetScope())

  const twoDesks = () => ({
    since: {
      ...emptySince(),
      new_findings: sect([
        { id: 'f-br', analyst_id: 'security_desk', target_id: 'country_g20_br', title: 'Border incident', severity: 'high', confidence: 0.8, faithfulness_score: 0.9, effective_confidence: 0.8, produced_at: '2026-09-24T07:00:00Z' },
        { id: 'f-sd', analyst_id: 'security_desk', target_id: 'country_watch_sd', title: 'Khartoum shelling', severity: 'high', confidence: 0.8, faithfulness_score: 0.9, effective_confidence: 0.8, produced_at: '2026-09-24T07:30:00Z' },
      ]),
    },
  })

  it('narrows CHANGED (and its rail count) to the scoped subject, says so, and clears back to every subject', async () => {
    useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil', 'operator'))
    await renderBands(twoDesks())
    const changed = screen.getByTestId('morning-read-changed')
    await waitFor(() => expect(changed.getAttribute('data-count')).toBe('1'))
    expect(changed.textContent).toContain('Border incident')
    expect(changed.textContent).not.toContain('Khartoum shelling')
    expect(screen.getByTestId('morning-read-rail-changed').textContent).toBe('1. Changed1')
    expect(screen.getByTestId('morning-read-scoped').textContent).toContain('Brazil')

    fireEvent.click(screen.getByTestId('morning-read-scope-clear'))
    await waitFor(() => expect(screen.getByTestId('morning-read-changed').getAttribute('data-count')).toBe('2'))
    expect(screen.queryByTestId('morning-read-scoped')).toBeNull()
  })

  it('with no subject scope, every subject is listed (unchanged)', async () => {
    await renderBands(twoDesks())
    await waitFor(() => expect(screen.getByTestId('morning-read-changed').getAttribute('data-count')).toBe('2'))
    expect(screen.queryByTestId('morning-read-scoped')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// UI 2026-11-13 — "changed" de-duplicated and ranked: an alert collapses into
// the item it alerts on, `x -> x` is not a change, and the list is ranked
// worst-first across desks instead of grouped by desk.
// ---------------------------------------------------------------------------

describe('CHANGED — de-duplicated and ranked', () => {
  const fnd = (id: string, target_id: string, title: string, severity: string, produced_at: string) => ({
    id,
    analyst_id: 'security_desk',
    target_id,
    title,
    severity,
    confidence: 0.8,
    faithfulness_score: 0.9,
    effective_confidence: 0.8,
    produced_at,
  })

  it('one row per item, the alert as a mark on it, no x -> x row, worst first across desks', async () => {
    await renderBands({
      since: {
        ...emptySince(),
        new_findings: sect([
          fnd('f-lo', 'desk_alpha', 'Minor note', 'low', '2026-09-24T08:50:00Z'),
          fnd('f-hi', 'desk_beta', 'Bridge down', 'medium', '2026-09-24T07:00:00Z'),
          fnd('f-md', 'desk_alpha', 'Road shut', 'medium', '2026-09-24T08:00:00Z'),
        ]),
        alerts: sect([
          { id: 'al-1', severity: 'high', channel: 'verified_finding', summary: 'Verified high-severity finding: Bridge down', target_id: 'desk_beta', produced_at: '2026-09-24T07:05:00Z', finding_id: 'f-hi' },
          { id: 'al-2', severity: 'medium', channel: 'situation_escalation', summary: 'Situation escalated: Strikes (escalating -> escalating)', target_id: 'desk_alpha', produced_at: '2026-09-24T08:40:00Z' },
        ]),
      },
    })
    const changed = screen.getByTestId('morning-read-changed')
    await waitFor(() => expect(changed.getAttribute('data-count')).toBe('3'))
    const rows = within(changed).getAllByTestId('morning-read-changed-row')
    expect(rows).toHaveLength(3)
    // Ranked: the alerted item first (raised to high), then medium, then low.
    expect(rows[0].textContent).toContain('Bridge down')
    expect(rows[1].textContent).toContain('Road shut')
    expect(rows[2].textContent).toContain('Minor note')
    // The alert is a mark on its item, never a row of its own.
    expect(within(rows[0]).getByTestId('morning-read-changed-alert-mark').textContent).toBe('alerted: verified')
    expect(changed.textContent).not.toContain('Verified high-severity finding')
    // A state to the same state is not a change.
    expect(changed.textContent).not.toContain('escalating -> escalating')
    // No per-desk grouping heads: the desk is named on each row.
    expect(within(rows[0]).getByTestId('morning-read-changed-desk').textContent).toBe('Desk Beta')
    expect(screen.getByTestId('morning-read-rail-changed').textContent).toBe('1. Changed3')
  })
})
