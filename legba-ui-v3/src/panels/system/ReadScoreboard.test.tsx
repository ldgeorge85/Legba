/**
 * Component test for `system.read_scoreboard` — the wager's scoreboard.
 *
 * Mocks the registry at the HTTP boundary. What these hold:
 *   * AN EMPTY LOG RENDERS AS A FINDING, not as a spinner or a blank. This is
 *     the one that matters most: the premise review PREDICTS zero, and a
 *     scoreboard that looks broken when the score is zero would let the whole
 *     result be dismissed as a bug.
 *   * the headline is a ratio with its denominator printed — "12 of 30 days",
 *     never a bare count, because twelve refreshes on one morning and twelve
 *     separate mornings are opposite findings;
 *   * drills are counted apart from opens (§2.2 is specifically about the
 *     trust operations, and folding them into a total would hide the thing
 *     under test);
 *   * the day strip is DENSE — a silent day draws as an empty cell.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import ReadScoreboardPanel from './ReadScoreboard'
import type { ReadRollupResponse } from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'rs1',
    panel_id: 'system_read_scoreboard',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Read Scoreboard',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-08-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function payload(over: Partial<ReadRollupResponse> = {}): ReadRollupResponse {
  return {
    since: '2026-07-31',
    totals: {
      reads_today: 0,
      reads_this_week: 0,
      brief_reads_today: 0,
      brief_reads_this_week: 0,
      brief_read_days: 0,
      active_days: 0,
      sessions_this_week: 0,
      window_days: 30,
    },
    days: [],
    ...over,
  }
}

function serve(body: ReadRollupResponse) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response),
  )
}

beforeEach(() => {
  vi.stubGlobal('localStorage', localStorage)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the empty log', () => {
  it('says nothing was read, in words — never a spinner or a blank', async () => {
    serve(payload())
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    const empty = await screen.findByTestId('read-scoreboard-empty')
    expect(empty.textContent).toContain('Nothing read in the last 30 days')
    // The distinction that keeps a real result from being read as an outage.
    expect(empty.textContent).toContain('measurement, not a failure to load')
    expect(screen.queryByTestId('read-scoreboard-loading')).toBeNull()
  })

  it('still renders the tiles at zero rather than hiding them', async () => {
    serve(payload())
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    const tiles = await screen.findByTestId('read-scoreboard-tiles')
    expect(tiles.textContent).toContain('reads today')
    expect(screen.getByTestId('tile-brief').textContent).toContain(
      'opened on 0 of 30 days',
    )
  })
})

describe('a read week', () => {
  const populated = payload({
    totals: {
      reads_today: 14,
      reads_this_week: 61,
      brief_reads_today: 1,
      brief_reads_this_week: 4,
      brief_read_days: 12,
      active_days: 18,
      sessions_this_week: 5,
      window_days: 30,
    },
    days: [
      { day: '2026-08-29', event_kind: 'brief_read', events: 1, sessions: 1 },
      { day: '2026-08-29', event_kind: 'panel_open', events: 9, sessions: 1 },
      { day: '2026-08-29', event_kind: 'finding_open', events: 3, sessions: 1 },
      { day: '2026-08-29', event_kind: 'citation_drill', events: 2, sessions: 1 },
      { day: '2026-08-27', event_kind: 'lineage_walk', events: 4, sessions: 2 },
      { day: '2026-08-27', event_kind: 'panel_open', events: 6, sessions: 2 },
    ],
  })

  it('prints the headline as a ratio with its denominator', async () => {
    serve(populated)
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    const brief = await screen.findByTestId('tile-brief')
    expect(brief.textContent).toContain('opened on 12 of 30 days')
  })

  it('counts drills apart from opens — §2.2 is about the trust operations', async () => {
    serve(populated)
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    const drills = await screen.findByTestId('tile-drills')
    // 2 citation drills + 4 lineage walks; the 15 panel opens are NOT drills.
    expect(drills.textContent).toContain('6')
    expect(drills.textContent).toContain('lineage walks + citation drills')
  })

  it('breaks the log down by kind with both events and days-seen', async () => {
    serve(populated)
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('read-scoreboard-kinds')
    const rows = screen.getAllByTestId('read-kind-row').map((r) => r.textContent)
    // Busiest first: panel_open 15 over 2 days.
    expect(rows[0]).toContain('panel_open')
    expect(rows[0]).toContain('15')
    expect(rows.some((r) => r?.includes('brief_read'))).toBe(true)
  })

  it('draws a DENSE 14-day strip — a silent day is an empty cell, not a gap', async () => {
    serve(populated)
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('read-scoreboard-grid')
    const cells = screen.getAllByTestId('read-day-cell')
    expect(cells).toHaveLength(14)
    // Exactly one day had a morning read; one other had activity without one.
    expect(cells.filter((c) => c.dataset.briefRead === 'true')).toHaveLength(1)
    expect(
      cells.filter((c) => c.dataset.briefRead === 'false' && Number(c.dataset.events) > 0),
    ).toHaveLength(1)
    expect(cells.filter((c) => Number(c.dataset.events) === 0)).toHaveLength(12)
  })

  it('discloses that "reads" includes panel opens, this panel included', async () => {
    serve(populated)
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('read-scoreboard-tiles')
    expect(document.body.textContent).toContain('including panel opens')
  })

  it('re-queries when the operator changes the window', async () => {
    serve(populated)
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('read-scoreboard-tiles')
    const calls = (fetch as ReturnType<typeof vi.fn>).mock.calls.length
    fireEvent.click(screen.getByText('7d'))
    await waitFor(() =>
      expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(calls),
    )
    const last = (fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0] as string
    expect(last).toContain('/read-events/rollup?days=7')
  })
})

describe('a failed read', () => {
  it('says it could not read the log — never renders a silent zero', async () => {
    // The difference between "the operator read nothing" and "we could not
    // look" is the whole finding; a panel that renders them the same way
    // would turn an outage into evidence against the wager.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('registry down')))
    render(wrap(<ReadScoreboardPanel registration={reg()} scope={{}} mode="personal" />))
    const err = await screen.findByTestId('read-scoreboard-error')
    expect(err.textContent).toContain('Could not read the read log')
    expect(screen.queryByTestId('read-scoreboard-empty')).toBeNull()
  })
})
