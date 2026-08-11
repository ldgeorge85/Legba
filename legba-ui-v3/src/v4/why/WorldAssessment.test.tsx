/**
 * Component test for the World Assessment panel.
 *
 * The regression this pins: the panel used to derive its scope from the global
 * selection, so with any desk selected — i.e. nearly always — the tab named
 * "World Assessment" silently swapped to that desk's `country_composition` and
 * the `world_assessor` read became unreachable. The panel is now PURELY the
 * world surface, presented like the Journal panel: the latest run in full at
 * the top (its produced_at named plainly, in UTC) with prior runs beneath as a
 * browsable history that swaps into the reading column.
 *
 * Asserts:
 *  - it queries `analyst_id=world_assessor` even with a target selected, and
 *    never asks for a `country_composition`
 *  - the header states the world read's date ("World read — 2026-08-04 12:00Z")
 *  - prior runs render as history, and clicking one swaps it into the column
 *    (badged superseded, with a one-click return to the latest)
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import WorldAssessment from './WorldAssessment'
import { useSelection } from '@/state/selection'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function run(id: string, title: string, producedAt: string) {
  return {
    id,
    title,
    body: `Body of ${title}.`,
    severity: 'elevated',
    confidence: 0.71,
    analyst_id: 'world_assessor',
    produced_at: producedAt,
    data: { title, summary: `Body of ${title}.` },
    verification: null,
  }
}

const RUNS = [
  run('w-3', 'World read — current', '2026-08-04T12:00:00Z'),
  run('w-2', 'World read — yesterday', '2026-08-03T12:00:00Z'),
  run('w-1', 'World read — two days ago', '2026-08-02T12:00:00Z'),
]

/** Records every requested URL; answers `/findings` with the run page and any
 *  other incidental request (citation/verification hydration) with an empty
 *  page, so an unrelated fetch can never fail the assertions. */
function stubFetch(rows = RUNS) {
  const fetchMock = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    json: async () => (url.includes('/findings') ? { data: rows } : { data: [] }),
    text: async () => '',
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function requestedUrls(fetchMock: ReturnType<typeof stubFetch>): string[] {
  return fetchMock.mock.calls.map((c) => String(c[0]))
}

beforeEach(() => {
  useSelection.getState().clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  useSelection.getState().clear()
})

describe('WorldAssessment — always the world', () => {
  it('queries world_assessor even when a country desk is selected', async () => {
    // The exact condition that used to hide the world read.
    useSelection.getState().select({ kind: 'target', id: 'target.geo.brazil', label: 'Brazil' })
    const fetchMock = stubFetch()

    render(wrap(<WorldAssessment />))
    await screen.findByTestId('world-assessment')

    const findingsUrls = requestedUrls(fetchMock).filter((u) => u.includes('/findings'))
    expect(findingsUrls.length).toBeGreaterThan(0)
    for (const url of findingsUrls) {
      expect(url).toContain('analyst_id=world_assessor')
    }
    // No desk arm survives: never a composition query, never a target filter.
    expect(requestedUrls(fetchMock).some((u) => u.includes('country_composition'))).toBe(false)
    expect(requestedUrls(fetchMock).some((u) => u.includes('target_id='))).toBe(false)
    // And the world read — not a desk card — is what rendered.
    expect(screen.queryByTestId('desk-intelligence-card')).toBeNull()
    expect(screen.getByText('World read — current')).toBeTruthy()
  })

  it('states the world read’s date in the header, in UTC', async () => {
    stubFetch()
    render(wrap(<WorldAssessment />))

    const stamp = await screen.findByTestId('world-assessment-stamp')
    expect(stamp.textContent).toBe('World read — 2026-08-04 12:00Z')
    // The latest run carries no superseded badge.
    expect(screen.queryByTestId('world-assessment-superseded')).toBeNull()
  })

  it('renders prior runs as history and swaps one into the reading column', async () => {
    stubFetch()
    render(wrap(<WorldAssessment />))
    await screen.findByTestId('world-assessment')

    // Newest is the read; the other two are its history.
    const rows = screen.getAllByTestId('world-assessment-history-row')
    expect(rows).toHaveLength(2)
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('World read — yesterday'),
      expect.stringContaining('World read — two days ago'),
    ])

    fireEvent.click(rows[0])

    await waitFor(() => {
      expect(screen.getByTestId('world-assessment-stamp').textContent).toBe(
        'World read — 2026-08-03 12:00Z',
      )
    })
    expect(screen.getByTestId('world-assessment-superseded')).toBeTruthy()

    // …and one click returns to the current read.
    fireEvent.click(screen.getByTestId('world-assessment-back-to-latest'))
    await waitFor(() => {
      expect(screen.getByTestId('world-assessment-stamp').textContent).toBe(
        'World read — 2026-08-04 12:00Z',
      )
    })
  })

  it('shows an honest empty state when the assessor has published nothing', async () => {
    stubFetch([])
    render(wrap(<WorldAssessment />))

    await screen.findByTestId('world-assessment-empty')
    expect(screen.queryByTestId('world-assessment-history')).toBeNull()
  })
})
