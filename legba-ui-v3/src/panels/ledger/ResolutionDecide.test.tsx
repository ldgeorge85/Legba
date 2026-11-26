/**
 * F-1 — deciding a forecast's outcome on the console, over invented rows:
 *
 *   * the queue opens on the evidence, then the rule in plain words, then the
 *     decision; Confirm asks first (in the page — no browser dialog), then
 *     records, then says what happened;
 *   * a reason is required to reject, to void and for a judgement — nothing
 *     is sent without one;
 *   * a contributor is told how many questions are held back and why; a
 *     reader is told their role cannot decide; a refusal is shown in the
 *     route's own words;
 *   * THE FIFTH RESOLUTION, end to end: four resolved, the track record shows
 *     the count and no score; confirming the fifth in the queue re-reads the
 *     scorecard and the scores appear;
 *   * the reader's Ledger says "included in the anchor of <day>" only when
 *     the path checks in the browser, and says a tampered proof plainly.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import LedgerForecasts from './LedgerForecasts'
import ResolutionQueue from './ResolutionQueue'
import TrackRecord from './TrackRecord'
import { resetLedgerView } from './ledgerView'
import {
  PROOF_ANCHORS,
  QUESTION_PROOF,
  SEAL_PROOF,
  forecast,
  forecasts,
  queue,
  queueEntry,
  scorecard,
} from './__fixtures__/ledger'
import { dropCachedGets } from '@/lib/queryCache'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

type Answer = { status?: number; body?: unknown }
type Post = { url: string; body: Record<string, unknown> }

function respond(a: Answer): Response {
  const status = a.status ?? 200
  return {
    ok: status < 400,
    status,
    json: async () => a.body,
    text: async () => JSON.stringify(a.body ?? {}),
    headers: new Headers({ 'content-type': 'application/json' }),
    clone() {
      return this
    },
  } as unknown as Response
}

/** GETs answered by route (a function sees the POSTs so far); POSTs recorded and answered. */
function stub(gets: Record<string, Answer | ((posts: Post[]) => Answer)>, post: (p: Post) => Answer = () => ({ status: 500, body: {} })) {
  const posts: Post[] = []
  const fn = (url: string, init?: RequestInit) => {
    const u = String(url)
    if (init?.method === 'POST') {
      const p = { url: u, body: JSON.parse(String(init.body ?? '{}')) }
      posts.push(p)
      return Promise.resolve(respond(post(p)))
    }
    const key = Object.keys(gets).find((k) => u.includes(k))
    const r = key ? gets[key] : { status: 404, body: { detail: 'Not Found' } }
    return Promise.resolve(respond(typeof r === 'function' ? r(posts) : r))
  }
  vi.stubGlobal('fetch', fn)
  return posts
}

beforeEach(() => {
  resetLedgerView()
  dropCachedGets()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const done = (status: string, outcome: string, kind = 'confirmation') => ({
  status: 201,
  body: { version: 'test', status, record_id: 'rec-new', record_kind: kind, outcome, question_id: forecast(1).id, slug: 'FX1-invented', actor_role: 'admin' },
})

async function openFirst() {
  const item = await screen.findByTestId('resolution-queue-item')
  fireEvent.click(within(item).getAllByRole('button')[0])
  return item
}

describe('the queue decides (view:resolutions)', () => {
  it('evidence first, then the rule, then the decision; confirm asks, records, and says what happened', async () => {
    const posts = stub({ '/v3/ledger/resolution-queue': { body: queue([queueEntry(1, 'yes')]) } }, () => done('confirmed', 'yes'))
    wrap(<ResolutionQueue />)
    await openFirst()
    const ev = await screen.findByTestId('resolution-evidence')
    const rule = screen.getByTestId('resolution-rule')
    const decide = screen.getByTestId('resolution-decide')
    expect(ev.compareDocumentPosition(rule) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(rule.compareDocumentPosition(decide) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(ev.textContent).toMatch(/Invented wire report: both sides announce a ceasefire/)
    expect(rule.textContent).toMatch(/^The rule it was sealed withYes if a fictional ceasefire announcement is reported/)
    expect(rule.textContent).toMatch(/Void if: Neither government says anything by due \+ grace\./)
    expect(screen.getByTestId('resolution-queue').textContent).toMatch(/You decide as an admin\./)

    fireEvent.click(screen.getByTestId('resolution-confirm'))
    const sure = await screen.findByTestId('resolution-sure')
    expect(sure.textContent).toMatch(/Confirm the proposal: the forecast resolved "yes"\./)
    expect(sure.textContent).toMatch(/cannot be edited later/)
    expect(posts).toEqual([])
    // Going back sends nothing.
    fireEvent.click(screen.getByTestId('resolution-back'))
    expect(posts).toEqual([])
    fireEvent.click(screen.getByTestId('resolution-confirm'))
    fireEvent.click(await screen.findByTestId('resolution-record'))
    const said = await screen.findByTestId('resolution-done')
    expect(said.textContent).toBe('Confirmed as an admin: the forecast resolved "yes". It now counts in the track record.')
    expect(posts).toEqual([{ url: '/api/v1/v3/ledger/resolutions/rec-invented-1/confirm', body: { outcome: 'yes' } }])
  })

  it('reject and void need a reason; nothing is sent without one', async () => {
    const posts = stub({ '/v3/ledger/resolution-queue': { body: queue([queueEntry(1, 'void')]) } }, (p) =>
      p.url.endsWith('/reject') ? done('rejected', 'void', 'rejection') : done('confirmed', 'void'),
    )
    wrap(<ResolutionQueue />)
    await openFirst()
    fireEvent.click(await screen.findByTestId('resolution-reject'))
    expect(screen.getByTestId('resolution-problem').textContent).toMatch(/a rejection needs its reason/)
    fireEvent.click(screen.getByTestId('resolution-confirm'))
    expect(screen.getByTestId('resolution-problem').textContent).toMatch(/a void needs its reason/)
    expect(screen.queryByTestId('resolution-sure')).toBeNull()
    expect(posts).toEqual([])
    fireEvent.change(screen.getByTestId('resolution-reason'), { target: { value: 'the report is about a different exchange' } })
    fireEvent.click(screen.getByTestId('resolution-reject'))
    expect((await screen.findByTestId('resolution-sure')).textContent).toMatch(/Your reason: the report is about a different exchange/)
    fireEvent.click(screen.getByTestId('resolution-record'))
    expect((await screen.findByTestId('resolution-done')).textContent).toMatch(/^Rejected as an admin\. Nothing closed: the forecast stays open/)
    expect(posts).toEqual([{ url: '/api/v1/v3/ledger/resolutions/rec-invented-1/reject', body: { reason: 'the report is about a different exchange' } }])
  })

  it('a judgement is offered only where the rule cannot decide', async () => {
    const posts = stub({ '/v3/ledger/resolution-queue': { body: queue([queueEntry(1, 'needs_judgement')]) } }, () => done('decided', 'no', 'override'))
    wrap(<ResolutionQueue />)
    await openFirst()
    expect(await screen.findByTestId('resolution-judge-no')).toBeTruthy()
    expect(screen.queryByTestId('resolution-confirm')).toBeNull()
    expect(screen.getByTestId('resolution-proposal').textContent).toMatch(/Is the announcement attributed to both governments\?/)
    fireEvent.change(screen.getByTestId('resolution-reason'), { target: { value: 'only one government said it' } })
    fireEvent.click(screen.getByTestId('resolution-judge-no'))
    fireEvent.click(await screen.findByTestId('resolution-record'))
    await screen.findByTestId('resolution-done')
    expect(posts[0]).toEqual({ url: '/api/v1/v3/ledger/resolutions/rec-invented-1/judgement', body: { outcome: 'no', reason: 'only one government said it' } })
  })

  it('a refusal is shown in the route’s own words, and nothing is claimed', async () => {
    stub({ '/v3/ledger/resolution-queue': { body: queue([queueEntry(1, 'yes')]) } }, () => ({
      status: 409,
      body: { detail: { error: 'stale_proposal', message: 'the proposal reads \'no\', not \'yes\'; read it again' } },
    }))
    wrap(<ResolutionQueue />)
    await openFirst()
    fireEvent.click(await screen.findByTestId('resolution-confirm'))
    fireEvent.click(await screen.findByTestId('resolution-record'))
    expect((await screen.findByTestId('resolution-refused')).textContent).toBe("the proposal reads 'no', not 'yes'; read it again")
    expect(screen.queryByTestId('resolution-done')).toBeNull()
  })

  it('a contributor is told what is held back and why; a reader that their role cannot decide', async () => {
    stub({
      '/v3/ledger/resolution-queue': {
        body: queue([queueEntry(1, 'yes')], {
          role: 'contributor',
          may_correct: false,
          hidden: 2,
          hidden_reason: 'questions where you entered a number: an admin or the owner decides those',
        }),
      },
    })
    wrap(<ResolutionQueue />)
    expect((await screen.findByTestId('resolution-queue-hidden')).textContent).toBe(
      '2 more are not shown: questions where you entered a number: an admin or the owner decides those.',
    )
    expect(screen.getByTestId('resolution-queue').textContent).toMatch(/You decide as a contributor\./)
    cleanup()
    dropCachedGets()
    stub({ '/v3/ledger/resolution-queue': { status: 403, body: { detail: { error: 'role_forbidden', required_role: 'contributor' } } } })
    wrap(<ResolutionQueue />)
    expect((await screen.findByTestId('resolution-queue-forbidden')).textContent).toMatch(/needs the contributor role or above/)
  })
})

describe('the fifth resolution opens the track record', () => {
  it('four resolved: no score; the fifth confirmed in the queue: the scores appear', async () => {
    const fifth = queueEntry(5, 'no')
    const posts = stub(
      {
        '/v3/ledger/resolution-queue': (p) => ({ body: queue(p.length ? [] : [fifth]) }),
        '/v3/ledger/scorecard': (p) => ({ body: scorecard(p.length ? 5 : 4) }),
      },
      () => done('confirmed', 'no'),
    )
    wrap(
      <>
        <TrackRecord />
        <ResolutionQueue />
      </>,
    )
    const region = await screen.findByTestId('track-record')
    await waitFor(() => expect(region.getAttribute('data-state')).toBe('withheld'))
    expect(screen.getByTestId('track-record-line').textContent).toBe('4 of the 5 resolutions needed before any score is shown.')
    expect(region.textContent).not.toMatch(/\b\d\.\d{3}\b/)

    await openFirst()
    fireEvent.click(await screen.findByTestId('resolution-confirm'))
    fireEvent.click(await screen.findByTestId('resolution-record'))
    await screen.findByTestId('resolution-done')
    expect(posts).toHaveLength(1)
    await waitFor(() => expect(screen.getByTestId('track-record').getAttribute('data-state')).toBe('scored'))
    expect(screen.getByTestId('track-record-line').textContent).toMatch(/^Scored over 5 resolved forecasts\./)
    expect(screen.getByTestId('resolution-queue-empty')).toBeTruthy()
  })
})

describe('the reader’s seal (view:forecasts)', () => {
  function ledger(proof: unknown) {
    stub({
      '/v3/ledger/forecasts': { body: forecasts([forecast(1)]) },
      '/v3/ledger/scorecard': { body: scorecard(0) },
      '/v3/system/receipt-anchors': { body: PROOF_ANCHORS },
      '/v3/ledger/seal-proof': { body: proof },
    })
    wrap(<LedgerForecasts />)
  }

  it('"included in the anchor of <day>" only when the path checks here', async () => {
    ledger(SEAL_PROOF)
    const row = (await screen.findAllByTestId('ledger-forecast-row'))[0]
    fireEvent.click(within(row).getByTestId('ledger-forecast-select'))
    const inc = await screen.findByTestId('forecast-seal-inclusion')
    await waitFor(() => expect(inc.getAttribute('data-state')).toBe('included'))
    expect(inc.textContent).toMatch(/^Included in the anchor of 2026-03-02: the path from this seal to that day's root checks in your browser\./)
    expect(inc.textContent).toMatch(/the timestamp proof is checked offline with the OpenTimestamps client/)
  })

  it('a tampered proof is said plainly', async () => {
    ledger({ ...SEAL_PROOF, question: { ...QUESTION_PROOF, path: QUESTION_PROOF.path!.slice(1) } })
    const row = (await screen.findAllByTestId('ledger-forecast-row'))[0]
    fireEvent.click(within(row).getByTestId('ledger-forecast-select'))
    const inc = await screen.findByTestId('forecast-seal-inclusion')
    await waitFor(() => expect(inc.getAttribute('data-state')).toBe('failed'))
    expect(inc.textContent).toMatch(/^The inclusion path did not check/)
    expect(screen.getByTestId('ledger-forecast-sheet').textContent).not.toMatch(/Included in the anchor/)
  })
})
