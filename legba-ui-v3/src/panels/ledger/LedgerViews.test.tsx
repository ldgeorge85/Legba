/**
 * The Ledger page's views (U-11) rendered over invented rows: the forecasts
 * with their state, rule and checkable seal; the evaluation record's
 * designed empty state and a published run with its caveats and
 * deviations; the admin resolution queue with the cited evidence and no
 * write; and the Consult actions each view declares.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import LedgerForecasts from './LedgerForecasts'
import ProofRecord from './ProofRecord'
import ResolutionQueue from './ResolutionQueue'
import { resetLedgerView, useLedgerView, ledgerOnScreen } from './ledgerView'
import { ANCHORS, EVAL_RUNS, EVAL_RUNS_VERSIONED, forecast, forecasts, records, scorecard, stubFetch } from './__fixtures__/ledger'
import { applyPanelAction } from '@/lib/panelActions'
import { declaredContract } from '@/panel-registry/contracts'
import { dropCachedGets } from '@/lib/queryCache'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  resetLedgerView()
  dropCachedGets()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const RESOLVED = forecast(2, { status: 'resolved', resolution: { id: 'r2', outcome: 'no', resolved_at: '2026-03-21T00:00:00Z' } })
const VOIDED = forecast(3, { status: 'void', resolution: { id: 'r3', outcome: 'void', note: 'the vote was postponed' } })
const LATE = forecast(4, { sealed_at: '2026-03-01T00:00:00Z' })

function stubLedger(over: Record<string, unknown> = {}) {
  const sc = scorecard(0)
  sc.questions![0].proposal = { outcome: 'yes', recorded_at: '2099-03-22T00:00:00Z', evidence_count: 1 }
  const s = stubFetch({
    '/v3/ledger/forecasts': { body: forecasts([forecast(1), RESOLVED, VOIDED, LATE]) },
    '/v3/ledger/scorecard': { body: sc },
    '/v3/system/receipt-anchors': { body: ANCHORS },
    '/v3/ledger/resolution-records': { body: records(forecast(1).id) },
    ...(over as Record<string, never>),
  })
  vi.stubGlobal('fetch', s.fn)
  return s
}

describe('the forecasts (view:forecasts)', () => {
  it('every sealed forecast with its probability, dates, rule in plain words, state and seal', async () => {
    stubLedger()
    wrap(<LedgerForecasts />)
    await waitFor(() => expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(4))
    expect(screen.getByTestId('ledger-forecasts-counts').textContent).toBe('4 sealed · 2 open · 2 closed · next due 2026-03-20')
    const rows = screen.getAllByTestId('ledger-forecast-row')
    const first = rows.find((r) => r.getAttribute('data-slug') === 'FX1-invented')!
    await waitFor(() => expect(first.getAttribute('data-state')).toBe('awaiting_confirmation'))
    expect(within(first).getByTestId('ledger-forecast-state').textContent).toBe('Proposed: yes — awaiting confirmation')
    expect(within(first).getByTestId('ledger-forecast-numbers').textContent).toMatch(/Owner 70 %.*This system · no number stated.*Frontier model 55 %/)
    expect(within(first).getByTestId('ledger-forecast-rule').textContent).toMatch(
      /^Yes if a fictional ceasefire announcement is reported \(by AA and BB\) between 2026-03-01 and 2026-03-20\. Confirmed by an official statement of either government\./,
    )
    expect(first.textContent).toMatch(/sealed 2026-02-27 · due 2026-03-20/)
    await waitFor(() => expect(within(first).getByTestId('ledger-forecast-seal').textContent).toBe('seal: anchored'))
    // resolved and voided, the void with its reason
    expect(rows.find((r) => r.getAttribute('data-slug') === 'FX2-invented')!.getAttribute('data-state')).toBe('resolved_no')
    const v = rows.find((r) => r.getAttribute('data-slug') === 'FX3-invented')!
    expect(v.getAttribute('data-state')).toBe('void')
    expect(v.textContent).toMatch(/Voided: the vote was postponed/)
    // sealed after the newest anchor
    expect(within(rows.find((r) => r.getAttribute('data-slug') === 'FX4-invented')!).getByTestId('ledger-forecast-seal').textContent).toBe(
      'seal: not covered yet',
    )
    // No score on the forecasts page.
    expect(document.body.textContent).not.toMatch(/Brier|called it|\b0\.\d{3}\b/)
  })

  it('the seal opens as checkable: what it covers, the hash, the leaf, the anchor, never "verified"', async () => {
    stubLedger()
    wrap(<LedgerForecasts />)
    const row = (await screen.findAllByTestId('ledger-forecast-row'))[0]
    await waitFor(() => expect(within(row).getByTestId('ledger-forecast-seal').textContent).toBe('seal: anchored'))
    fireEvent.click(within(row).getByTestId('ledger-forecast-more'))
    const detail = within(row).getByTestId('forecast-seal-detail')
    expect(detail.textContent).toMatch(/The seal covers: the question as worded; its resolution rule/)
    expect(within(detail).getByTestId('forecast-seal-hash').textContent).toBe('1'.repeat(64))
    await waitFor(() => expect(within(detail).getByTestId('forecast-seal-leaf').textContent).toMatch(/^[0-9a-f]{64}$/))
    expect(within(detail).getByTestId('forecast-seal-check').getAttribute('data-state')).toBe('anchored')
    expect(detail.textContent).toMatch(/Covered by the daily anchor of 2026-02-28/)
    expect(detail.textContent).toMatch(/The timestamp itself is not checked in the console/)
    // No proof route in this stub: inclusion is not claimed.
    await waitFor(() => expect(within(detail).getByTestId('forecast-seal-inclusion').getAttribute('data-state')).toBe('unavailable'))
    expect(detail.textContent).not.toMatch(/Included in the anchor/)
    expect(row.textContent).toMatch(/Void if: Neither government says anything by due \+ grace\./)
  })

  it('an unreadable ledger is not an empty one; a missing route says so', async () => {
    stubLedger({ '/v3/ledger/forecasts': { body: { version: 't', available: false, forecasts: [] } } })
    wrap(<LedgerForecasts />)
    expect((await screen.findByTestId('ledger-forecasts-error')).textContent).toMatch(/not the same as having no forecasts/)
    expect(screen.getByTestId('ledger-forecasts-counts').textContent).toBe('—')
    cleanup()
    stubLedger({ '/v3/ledger/forecasts': { status: 404, body: {} } })
    wrap(<LedgerForecasts />)
    expect((await screen.findByTestId('ledger-forecasts-error')).textContent).toMatch(/does not serve the forecast ledger/)
  })

  it('Consult drives it through its contract: show_forecast and filter, and the on-screen report carries them', async () => {
    stubLedger()
    wrap(<LedgerForecasts />)
    await screen.findAllByTestId('ledger-forecast-row')
    expect(declaredContract('view:forecasts')!.actions.map((a) => a.id)).toEqual(['set_window', 'show_forecast', 'filter'])
    await act(async () => {
      expect(applyPanelAction('view:forecasts', 'filter', { show: 'resolved' }).state).toBe('executed')
    })
    expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(2)
    expect(applyPanelAction('view:forecasts', 'filter', { show: 'everything' }).state).not.toBe('executed')
    await act(async () => {
      expect(applyPanelAction('view:forecasts', 'show_forecast', { forecast: 'FX1-invented' }).state).toBe('executed')
    })
    expect(useLedgerView.getState()).toMatchObject({ forecast: 'FX1-invented', filter: 'all' })
    const sel = screen.getAllByTestId('ledger-forecast-row').find((r) => r.getAttribute('aria-current') === 'true')!
    expect(sel.getAttribute('data-slug')).toBe('FX1-invented')
    expect(sel.className).toMatch(/border-l-point/) // the reader's point, the one accent
    expect(ledgerOnScreen()).toEqual({ forecast: 'FX1-invented', filter: 'all', run: null })
    expect(applyPanelAction('view:forecasts', 'show_forecast', { forecast: 'nope' }).state).not.toBe('executed')
  })
})

describe('the evaluation record (view:proof)', () => {
  it('nothing published (200, runs: []) is the designed "no evaluation published yet"', async () => {
    const s = stubFetch({ '/v3/eval/runs': { body: { version: 'test', available: true, runs: [] } } })
    vi.stubGlobal('fetch', s.fn)
    wrap(<ProofRecord />)
    const none = await screen.findByTestId('proof-none')
    expect(screen.getByTestId('proof-record').getAttribute('data-state')).toBe('none_published')
    expect(none.textContent).toMatch(/^No evaluation published yet\./)
    expect(none.textContent).toMatch(/each score with its interval and the number of questions behind it/)
    expect(s.calls.some((c) => c.endsWith('/api/v1/v3/eval/runs'))).toBe(true)
    expect(document.body.textContent).not.toMatch(/\b0\.\d{3}\b/)
  })

  it('a 404 (a registry older than the route) is not "none published"', async () => {
    vi.stubGlobal('fetch', stubFetch({}).fn)
    wrap(<ProofRecord />)
    await waitFor(() => expect(screen.getByTestId('proof-record').getAttribute('data-state')).toBe('not_served'))
    expect(screen.queryByTestId('proof-none')).toBeNull()
    expect(screen.getByTestId('proof-record').textContent).toMatch(/does not serve the evaluation record yet\. That is not the same as none being published\./)
  })

  it('a published run: arms in plain words, score with interval and n, its caveats and deviations on screen', async () => {
    const s = stubFetch({ '/v3/eval/runs': { body: EVAL_RUNS } })
    vi.stubGlobal('fetch', s.fn)
    wrap(<ProofRecord />)
    const run = await screen.findByTestId('proof-run')
    expect(run.textContent).toMatch(/Answers from the raw feed/)
    expect(within(run).getByTestId('proof-results').textContent).toMatch(/0\.410.*0\.300 to 0\.520 \(95 %\).*n = 36/)
    expect(within(run).getAllByTestId('proof-by-class')[0].textContent).toBe('alpha 0.500 (n = 18) · beta 0.320 (n = 18)')
    expect(within(run).getByTestId('proof-caveats').textContent).toMatch(/drafted by a model and signed by one person/)
    expect(within(run).getByTestId('proof-deviations').textContent).toMatch(/Two questions were voided/)
    expect(within(run).getByTestId('proof-standing').textContent).toBe('Version 1, as first published.')
    const facts = within(run).getByTestId('proof-facts').textContent
    expect(facts).toMatch(/Answered as of 2026-03-05 · published 2026-03-20/)
    expect(facts).toMatch(/36 questions in two classes \(alpha 18, beta 18\)/)
    expect(facts).toMatch(/Graded by invented-grader · answering temperature 1/)
    expect(facts).toMatch(/Gold answers: gold v1/)
    // no verdict styling on any score: no accent, no good/bad colour
    const classes = [run, ...run.querySelectorAll('*')].map((e) => e.getAttribute('class') ?? '').join(' ')
    expect(classes).not.toMatch(/point|good|bad|danger|success|warn|green|red|accent/)
  })

  it('a correction, the version it supersedes, and a withdrawn run: all listed, each said in words', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/v3/eval/runs': { body: EVAL_RUNS_VERSIONED } }).fn)
    wrap(<ProofRecord />)
    await screen.findAllByTestId('proof-run')
    const runs = screen.getAllByTestId('proof-run')
    expect(runs.map((r) => r.getAttribute('data-status'))).toEqual(['current', 'superseded', 'withdrawn'])
    const standing = runs.map((r) => within(r).getByTestId('proof-standing').textContent)
    expect(standing[0]).toBe('Version 2, correcting version 1: arm B was transcribed from the wrong column.')
    expect(standing[1]).toBe('Version 1, superseded by version 2 on 2026-03-22. Kept as it was published.')
    expect(standing[2]).toMatch(/^Withdrawn on 2026-03-10: its gold set was found to be the practice set\. Its figures are kept below as published; they are not a current result\./)
    // the withdrawn run keeps its figures, caveats and deviations on screen
    expect(within(runs[2]).getByTestId('proof-results').textContent).toMatch(/0\.300.*no interval published.*n = 10/)
    expect(within(runs[2]).getByTestId('proof-results').textContent).toMatch(/Answers from the raw feed, again \(sample 2 of “Answers from the raw feed”\)/)
    expect(within(runs[2]).getByTestId('proof-caveats').textContent).toMatch(/drafted by a model/)
    expect(within(runs[0]).getByTestId('proof-results').textContent).toMatch(/0\.440/)
    expect(within(runs[1]).getByTestId('proof-results').textContent).toMatch(/0\.380/)
  })

  it('a run without caveats or deviations says so in words', async () => {
    const bare = { ...EVAL_RUNS, runs: [{ ...EVAL_RUNS.runs[0], caveats: null, deviations: [] }] }
    vi.stubGlobal('fetch', stubFetch({ '/v3/eval/runs': { body: bare } }).fn)
    wrap(<ProofRecord />)
    expect((await screen.findByTestId('proof-caveats')).textContent).toMatch(/No caveat was recorded with this run\./)
    expect(screen.getByTestId('proof-deviations').textContent).toMatch(/No departure from the registered design was recorded\./)
  })
})

describe('the resolution queue (view:resolutions, admin side)', () => {
  it('a proposal awaiting a person, its cited evidence, and no write anywhere', async () => {
    const s = stubLedger()
    const posts: string[] = []
    const wrapped = (url: string, init?: RequestInit) => {
      if (init?.method && init.method !== 'GET') posts.push(url)
      return s.fn(url)
    }
    vi.stubGlobal('fetch', wrapped)
    wrap(<ResolutionQueue />)
    const item = await screen.findByTestId('resolution-queue-item')
    expect(item.textContent).toMatch(/Proposed: yes/)
    fireEvent.click(within(item).getByRole('button'))
    const ev = await screen.findByTestId('resolution-evidence')
    expect(ev.textContent).toMatch(/Invented wire report: both sides announce a ceasefire/)
    expect(ev.textContent).toMatch(/example\.org · 2026-03-12 · clause event/)
    expect(within(ev).getByRole('link').getAttribute('href')).toBe('https://example.org/r/1')
    const gap = screen.getByTestId('resolution-confirm-gap')
    expect(gap.textContent).toMatch(/this registry serves no confirm route/)
    expect(gap.textContent).toMatch(/resolve_forecasts\.py confirm --record rec-invented-1/)
    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull()
    expect(posts).toEqual([])
    expect(s.calls.some((c) => c.includes(`/v3/ledger/resolution-records?question_id=${forecast(1).id}`))).toBe(true)
  })

  it('an empty queue says nothing is waiting', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/v3/ledger/scorecard': { body: scorecard(0) } }).fn)
    wrap(<ResolutionQueue />)
    expect((await screen.findByTestId('resolution-queue-empty')).textContent).toMatch(/Nothing is waiting/)
  })

  it('is an operator view with a declared contract, never on a reader page', () => {
    expect(declaredContract('view:resolutions')).toMatchObject({ side: 'operator', group: 'Operator' })
  })
})
