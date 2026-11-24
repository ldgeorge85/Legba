/**
 * TrackRecord — the owner's rule on screen: below five resolutions the
 * region shows the count toward five and what will appear, and NOTHING that
 * is or looks like a score (no Brier figure, no called/missed, no
 * good/warning/critical colour). At five the scores appear, every resolved
 * forecast listed, misses beside hits. Invented rows only.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TrackRecord from './TrackRecord'
import { scorecard, stubFetch } from './__fixtures__/ledger'
import type { ScorecardResponse } from '@/lib/scorecardModel'
import { dropCachedGets } from '@/lib/queryCache'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mount(body: ScorecardResponse | 'pending' | { status: number }) {
  const route = body === 'pending' ? 'pending' : 'status' in body && !('available' in body) ? { status: body.status, body: {} } : { body }
  const { fn } = stubFetch({ '/v3/ledger/scorecard': route as never })
  vi.stubGlobal('fetch', fn)
  dropCachedGets() // an in-flight GET is shared across mounts (lib/queryCache)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TrackRecord />
    </QueryClientProvider>,
  )
}

/** Any good / warning / critical colour, or the accent, anywhere in the region. */
const VERDICT_COLOUR = /accent-(ok|warning|critical)|severity-|--good|--warn|--critical|text-point|border-point|bg-point/

function assertNoScore(region: HTMLElement) {
  const text = region.textContent ?? ''
  expect(text).not.toMatch(/\b\d\.\d{3}\b/) // a Brier figure
  expect(text).not.toMatch(/called it|missed|better than|worse than|level with/)
  expect(region.querySelector('[data-testid="track-record-score"]')).toBeNull()
  expect(region.querySelector('[data-testid="track-record-calibration"]')).toBeNull()
  expect(region.querySelector('[data-testid="track-record-resolved"]')).toBeNull()
  expect(region.querySelector('[role="progressbar"], progress, meter')).toBeNull()
  for (const el of [region, ...region.querySelectorAll('*')]) {
    expect(el.getAttribute('class') ?? '', el.outerHTML.slice(0, 80)).not.toMatch(VERDICT_COLOUR)
    expect(el.getAttribute('style') ?? '').not.toMatch(/color/)
  }
}

describe('TrackRecord — no score below five', () => {
  for (const n of [0, 3, 4]) {
    it(`${n} resolved: the count toward five, what will appear, no score and no verdict colour`, async () => {
      mount(scorecard(n))
      const region = await screen.findByTestId('track-record')
      await waitFor(() => expect(region.getAttribute('data-state')).toBe('withheld'))
      expect(region.getAttribute('data-scored')).toBe('0')
      expect(screen.getByTestId('track-record-line').textContent).toBe(`${n} of the 5 resolutions needed before any score is shown.`)
      expect(screen.getByTestId('track-record-count').textContent).toBe(`${n} / 5 resolved`)
      expect(within(screen.getByTestId('track-record-will-appear')).getAllByRole('listitem')).toHaveLength(3)
      assertNoScore(region)
    })
  }

  it('4 with a route that says "not withheld": still no score', async () => {
    mount(scorecard(4, { withheld: false }))
    const region = await screen.findByTestId('track-record')
    await waitFor(() => expect(region.getAttribute('data-state')).toBe('withheld'))
    assertNoScore(region)
  })

  it('while reading, and when the route is missing or unreadable: no figure at all', async () => {
    mount('pending')
    const region = await screen.findByTestId('track-record')
    expect(region.getAttribute('data-state')).toBe('reading')
    assertNoScore(region)
    cleanup()
    mount({ status: 404 })
    await waitFor(() => expect(screen.getByTestId('track-record').getAttribute('data-state')).toBe('not_deployed'))
    assertNoScore(screen.getByTestId('track-record'))
    cleanup()
    mount({ available: false })
    await waitFor(() => expect(screen.getByTestId('track-record').getAttribute('data-state')).toBe('unreadable'))
    assertNoScore(screen.getByTestId('track-record'))
  })
})

describe('TrackRecord — at five', () => {
  it('4 → 5: the scores appear, with the baseline and n, and every resolved forecast — misses as plainly as hits', async () => {
    mount(scorecard(4))
    await waitFor(() => expect(screen.getByTestId('track-record').getAttribute('data-state')).toBe('withheld'))
    cleanup()
    mount(scorecard(5))
    const region = await screen.findByTestId('track-record')
    await waitFor(() => expect(region.getAttribute('data-state')).toBe('scored'))
    expect(region.getAttribute('data-scored')).toBe('1')
    const table = screen.getByTestId('track-record-score')
    expect(table.textContent).toMatch(/Owner/)
    expect(table.textContent).toMatch(/0\.\d{3}/)
    expect(table.textContent).toMatch(/0\.250/) // the baseline beside it
    const rows = screen.getAllByTestId('track-record-resolved-row')
    expect(rows).toHaveLength(5)
    const verdicts = rows.map((r) => r.getAttribute('data-verdict'))
    expect(verdicts).toContain('missed')
    expect(verdicts).toContain('called')
    // A miss is a row like a hit: same element, same classes on its verdict.
    const missEl = rows.find((r) => r.getAttribute('data-verdict') === 'missed')!
    const hitEl = rows.find((r) => r.getAttribute('data-verdict') === 'called')!
    expect(missEl.className).toBe(hitEl.className)
    expect(within(missEl).getByTestId('track-record-verdict').className).toBe(within(hitEl).getByTestId('track-record-verdict').className)
    expect(within(missEl).getByTestId('track-record-verdict').textContent).toBe('missed')
    // Never folded: no "show more" control.
    expect(region.textContent).not.toMatch(/show (more|all)|more\b.*hidden/i)
    expect(screen.getAllByTestId('track-record-band')).toHaveLength(10)
  })
})
