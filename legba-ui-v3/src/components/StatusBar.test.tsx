/**
 * The honesty footer, mounted.
 *
 * `lib/coverageFooter.test.ts` argues with the arithmetic; this file pins the
 * three states the bar itself has to survive, because a footer is on screen for
 * every minute of every morning and a footer that lies or throws is worse than
 * no footer at all:
 *
 *   1. **all present** — four figures, each with its unit and its own as-of;
 *   2. **one route failed** — that figure alone reads `unmeasured`, the other
 *      three are untouched, and nothing renders a zero;
 *   3. **a scope with no absence read** — the world scope, where
 *      `/v3/absence` is never even asked (it answers one desk at a time) and
 *      the figure says why rather than showing `0`.
 *
 * Plus the drill: one click on a figure opens the panel that owns it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { StatusBar } from './StatusBar'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { COVERAGE_FIGURE_PANEL } from '@/lib/coverageFooter'
import { emptyMembers, resetScope, useScope } from '@/state/scope'

const FIRING = [
  {
    source_id: 'source.a',
    state: 'active',
    signals_24h: 10,
    signals_7d: 70,
    last_seen_at: '2026-09-26T16:10:00.000Z',
    age_seconds: 600,
    last_poll_outcome: 'success',
    recent_error_count: 0,
    status: 'firing',
    freshness_grade: 'ok',
    budget_minutes: 120,
  },
  {
    source_id: 'source.b',
    state: 'active',
    signals_24h: 0,
    signals_7d: 3,
    last_seen_at: '2026-09-25T16:10:00.000Z',
    age_seconds: 90_000,
    last_poll_outcome: 'success',
    recent_error_count: 0,
    status: 'silent',
    freshness_grade: 'stale',
    budget_minutes: 120,
  },
]

const JUDGE = {
  generated_at: '2026-09-26T16:13:00.000Z',
  window_days: 7,
  measured: true,
  pools_across_pipeline_versions: false,
  totals: {
    critiques: 4497,
    by_status: {},
    attributed: 2973,
    unattributed: 1524,
    providers: 3,
    adjudicated_n: 4497,
    adjudicated_share: 0.98,
    faithfulness_n: 4497,
    faithfulness_mean: 0.8298,
    judge_calls: 100,
    judge_call_errors: 0,
  },
  providers: [],
  pipeline_versions: [],
  cells: [],
  sentinels: {},
  judge_statuses: [],
}

const ROSTER = {
  available: true,
  nights: 7,
  grain: 'desk',
  as_of: '2026-09-26T09:20:00.000Z',
  desks: [],
  roster: {
    desks_graded: 44,
    desks_unmeasured: 143,
    correctness_mean_of_desks: 0.46,
    coverage_mean_of_desks: 0.08,
    claims_pooled: {
      n_claims: 940,
      n_decided: 80,
      n_contains: 40,
      correctness_pooled: 0.5,
      coverage_pooled: 0.085,
    },
  },
  honesty_note: '',
}

const ABSENCE = {
  version: '2026-09/k5',
  scope: 'country_g20_ar',
  read_at: '2026-09-26T16:19:00.000Z',
  kinds: {},
  absences: [{ kind: 'not_collected', stale: false }],
  not_measured: [],
}

/** Which routes answer, and which blow up. */
type Failures = { sources?: boolean; judge?: boolean; grader?: boolean; absence?: boolean }

function stubFetch(fail: Failures = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const u = String(url)
      const answer = (body: unknown) =>
        ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }) as unknown as Response
      const fault = () =>
        ({ ok: false, status: 500, json: async () => ({}), text: async () => 'boom' }) as unknown as Response
      if (u.includes('/v3/system/source-firing')) return fail.sources ? fault() : answer(FIRING)
      if (u.includes('/v3/system/judge-stats')) return fail.judge ? fault() : answer(JUDGE)
      if (u.includes('/v3/eval/grader_roster')) return fail.grader ? fault() : answer(ROSTER)
      if (u.includes('/v3/absence')) return fail.absence ? fault() : answer(ABSENCE)
      return answer({})
    }),
  )
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

function bar(props: Partial<Parameters<typeof StatusBar>[0]> = {}) {
  return wrap(
    <StatusBar
      mode="personal"
      authenticated
      lastRefresh={null}
      {...props}
    />,
  )
}

/** Scope the wall to one desk, which is the only case `/v3/absence` can answer. */
function scopeToDesk() {
  useScope.getState().setScope({
    kind: 'target',
    id: 'country_g20_ar',
    label: 'Argentina',
    members: emptyMembers(),
    origin: 'test',
  })
}

beforeEach(() => {
  resetScope()
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the honesty footer — all four routes answering', () => {
  it('states four figures, each with its unit and its own as-of', async () => {
    stubFetch()
    scopeToDesk()
    render(bar())

    await waitFor(() => expect(screen.getByTestId('coverage-sources')).toHaveTextContent('/'))
    expect(screen.getByTestId('coverage-sources')).toHaveTextContent('1/2 firing')
    await waitFor(() =>
      expect(screen.getByTestId('coverage-judge')).toHaveTextContent('faithfulness'),
    )
    expect(screen.getByTestId('coverage-grader')).toHaveTextContent('44/187 desks graded')
    await waitFor(() => expect(screen.getByTestId('coverage-absence')).toHaveTextContent('typed'))
    expect(screen.getByTestId('coverage-absence')).toHaveTextContent('1 typed')

    // Every figure carries an as-of beside the value, not only in the tooltip.
    for (const id of ['sources', 'judge', 'grader', 'absence']) {
      expect(screen.getByTestId(`coverage-${id}`).textContent).toMatch(/·/)
      expect(screen.getByTestId(`coverage-${id}`)).toHaveAttribute('data-unmeasured', 'false')
    }
  })

  it('the registered-panel count is gone from the bar', () => {
    stubFetch()
    render(bar())
    expect(screen.queryByText(/panels registered/)).toBeNull()
  })

  it('one click on a figure opens the panel that owns it', async () => {
    stubFetch()
    const onOpenPanel = vi.fn()
    render(bar({ onOpenPanel }))
    fireEvent.click(screen.getByTestId('coverage-grader'))
    expect(onOpenPanel).toHaveBeenCalledWith(COVERAGE_FIGURE_PANEL.grader)
    fireEvent.click(screen.getByTestId('coverage-sources'))
    expect(onOpenPanel).toHaveBeenCalledWith(COVERAGE_FIGURE_PANEL.sources)
  })
})

describe('the honesty footer — one route failed', () => {
  it('prints unmeasured for that figure alone, and never a zero', async () => {
    stubFetch({ grader: true })
    render(bar())

    await waitFor(() =>
      expect(screen.getByTestId('coverage-grader')).toHaveAttribute('data-unmeasured', 'true'),
    )
    const grader = screen.getByTestId('coverage-grader')
    expect(grader).toHaveTextContent('unmeasured')
    expect(grader.textContent).not.toMatch(/\b0\b/)
    expect(grader.getAttribute('title')).toContain('/v3/eval/grader_roster')

    // The other three are untouched: one route failing is not four.
    await waitFor(() =>
      expect(screen.getByTestId('coverage-sources')).toHaveAttribute('data-unmeasured', 'false'),
    )
    await waitFor(() =>
      expect(screen.getByTestId('coverage-judge')).toHaveAttribute('data-unmeasured', 'false'),
    )
  })
})

describe('the honesty footer — a scope with no absence read', () => {
  it('says why there is no gap count instead of showing 0', async () => {
    stubFetch()
    render(bar())

    const gaps = screen.getByTestId('coverage-absence')
    expect(gaps).toHaveTextContent('unmeasured')
    expect(gaps).toHaveAttribute('data-unmeasured', 'true')
    expect(gaps.getAttribute('title')).toContain('one desk at a time')

    // And it was never asked: `/v3/absence` answers one desk, so a world scope
    // must not fire a read it cannot interpret.
    await waitFor(() => expect(screen.getByTestId('coverage-sources')).toHaveTextContent('firing'))
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
    expect(calls.some((c) => String(c[0]).includes('/v3/absence'))).toBe(false)
  })

  it('fills in the moment a desk IS scoped', async () => {
    stubFetch()
    render(bar())
    expect(screen.getByTestId('coverage-absence')).toHaveAttribute('data-unmeasured', 'true')
    act(() => scopeToDesk())
    await waitFor(() =>
      expect(screen.getByTestId('coverage-absence')).toHaveAttribute('data-unmeasured', 'false'),
    )
    expect(screen.getByTestId('coverage-absence')).toHaveTextContent('1 typed')
  })
})
