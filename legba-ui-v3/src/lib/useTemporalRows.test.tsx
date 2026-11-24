/**
 * U-5 — the time band reads findings at the summary weight: a finding's lane
 * needs only its id, title, severity and two clocks, every one of which
 * `fields=summary` carries (the full 200-row page was ~12 MB live).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useTemporalRows } from './useTemporalRows'

function Probe() {
  const { rows } = useTemporalRows()
  const f = rows.find((r) => r.source === 'findings')
  return <div data-testid="probe">{f ? `${f.title}|${f.severity}|${Number.isFinite(f.validMs)}` : 'none'}</div>
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('useTemporalRows', () => {
  it('asks /findings for the summary projection and draws a lane row from it', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const u = String(url)
      const data = u.includes('/findings')
        ? [{ id: 'f1', title: 'A finding', severity: 'high', produced_at: '2026-11-13T06:00:00Z', created_at: '2026-11-13T06:01:00Z' }]
        : []
      return { ok: true, status: 200, json: async () => ({ data }), text: async () => '' } as unknown as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Probe />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('A finding|high|true'))
    const findings = fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/findings'))
    expect(findings).toEqual(['/api/v1/findings?limit=200&fields=summary'])
  })

  it('asks /v3/events for the light row — the same read as the map events layer (roll 87: 0.42 MB whole, the Watch page over budget)', async () => {
    const { WORLD_EVENTS_PATH } = await import('./queryKeys')
    expect(WORLD_EVENTS_PATH).toBe('/v3/events?limit=500&fields=summary')
    const fetchMock = vi.fn(async (url: string) => {
      const u = String(url)
      // a light row: exactly what the lane reads (event_list_weight.EventSummaryRow)
      const data = u.includes('/v3/events')
        ? [{ id: 'e1', title: 'An event', severity: 'high', lifecycle_state: 'active', valid_from: '2026-11-13T06:00:00Z', time_start: '2026-11-13T06:00:00Z', produced_at: '2026-11-13T07:00:00Z' }]
        : []
      return { ok: true, status: 200, json: async () => ({ data }), text: async () => '' } as unknown as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    function EventProbe() {
      const { rows } = useTemporalRows()
      const e = rows.find((r) => r.source === 'events')
      return <div data-testid="probe">{e ? `${e.title}|${e.severity}|${e.subtitle}|${Number.isFinite(e.validMs)}|${Number.isFinite(e.recordMs)}` : 'none'}</div>
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <EventProbe />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('An event|high|active|true|true'))
    const events = fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/v3/events'))
    expect(events).toEqual([`/api/v1${WORLD_EVENTS_PATH}`])
  })
})
