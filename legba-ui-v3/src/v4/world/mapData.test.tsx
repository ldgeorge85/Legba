/**
 * The map's data hooks against the routes' REAL shapes (UI-B4/map):
 *
 *  - `/situations` serves a `{data, next_cursor}` page with `name` / `status`
 *    (the layer used to call `.map` on the envelope and draw nothing);
 *  - a signal's window time is its VALID time, `data.published_at` — every
 *    other instant on `/signals` is the fetch time, and it is never borrowed;
 *  - a record with no coordinate places through its SUBJECT'S declared
 *    geometry, never an ISO code parsed from the subject's id.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { placeFinding, useWorldEvents, useWorldSignals, useWorldSituations } from './mapData'
import { geometryIndexFrom } from '@/lib/subjectGeometry'
import type { WorldFinding } from './types'

const TARGETS = [
  { descriptor_id: 'desk_alpha', body: { scope: { domain: 'geo', geo: ['FR'] } } },
  { descriptor_id: 'theme_beta', body: { scope: { domain: 'thematic', geo: ['FR', 'DE'] } } },
]

const SITUATIONS = {
  data: [
    {
      id: 'sit-1',
      name: 'Port strike widens',
      status: 'closed',
      target_id: 'desk_alpha',
      last_event_at: '2026-10-03T12:00:00Z',
      created_at: '2026-10-03T12:05:00Z',
    },
    { id: 'sit-2', name: 'Unplaced', status: 'dormant', target_id: null, last_event_at: null, created_at: '2026-10-03T00:00:00Z' },
  ],
  next_cursor: null,
}

const SIGNALS = {
  data: [
    {
      id: 'sig-1',
      source_id: 'src',
      severity: 'high',
      geo: ['FR'],
      data: { published_at: '2026-10-01T08:00:00Z', geo: { lat: 48.8, lon: 2.3 } },
      event_timestamp: '2026-10-04T10:00:00Z',
      produced_at: '2026-10-04T10:00:00Z',
      created_at: '2026-10-04T10:00:00Z',
    },
    {
      id: 'sig-2',
      source_id: 'src',
      severity: 'low',
      geo: ['FR'],
      data: {},
      event_timestamp: '2026-10-04T10:00:00Z',
      produced_at: '2026-10-04T10:00:00Z',
      created_at: '2026-10-04T10:00:00Z',
    },
  ],
  next_cursor: null,
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const u = String(url)
      const body = (u.includes('/registry/descriptors') || u.includes('/v3/roster'))
        ? TARGETS
        : u.includes('/situations')
          ? SITUATIONS
          : u.includes('/signals')
            ? SIGNALS
            : { data: [], next_cursor: null }
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('situations — the route serves a page of name/status rows', () => {
  it('reads the envelope, the name, the status and the valid time; places through the subject', async () => {
    const { result } = renderHook(() => useWorldSituations(), { wrapper })
    await waitFor(() => expect(result.current.read).toBe(true))
    await waitFor(() => expect(result.current.situations[0]?.lat).not.toBeNull())
    const [a, b] = result.current.situations
    expect(a).toMatchObject({ id: 'sit-1', title: 'Port strike widens', lifecycle: 'resolved', countries: ['FR'] })
    expect(a.ts).toBe(Date.parse('2026-10-03T12:00:00Z'))
    expect(b).toMatchObject({ title: 'Unplaced', lat: null, lon: null })
    expect(Number.isNaN(b.ts)).toBe(true)
  })
})

describe("signals — the window time is the publisher's, not the fetch", () => {
  it('ts is data.published_at; a row without one has no valid time (never the fetch time)', async () => {
    const { result } = renderHook(() => useWorldSignals(), { wrapper })
    await waitFor(() => expect(result.current.read).toBe(true))
    const [one, two] = result.current.signals
    expect(one.ts).toBe(Date.parse('2026-10-01T08:00:00Z'))
    expect(Number.isNaN(two.ts)).toBe(true)
  })

  it('reads the light weight by name and places a row at its data.geo point', async () => {
    const { result } = renderHook(() => useWorldSignals(), { wrapper })
    await waitFor(() => expect(result.current.read).toBe(true))
    const urls = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]))
    const sig = urls.filter((u) => u.includes('/signals'))
    expect(sig.length).toBeGreaterThan(0)
    for (const u of sig) expect(u).toContain('fields=summary')
    expect(result.current.signals[0]).toMatchObject({ lat: 48.8, lon: 2.3 })
  })
})

describe('events — the light row by name (the Watch page over its 2 MB budget)', () => {
  it('asks fields=summary and places an event from the light row alone', async () => {
    // exactly the light row's columns (registry/event_list_weight.py EventSummaryRow)
    const light = {
      id: 'ev-1', title: 'Port strike', severity: 'high', lifecycle_state: 'developing', category: 'labour',
      target_id: 'desk_alpha', geo: ['FR'], geo_lat: 48.8, geo_lon: 2.3,
      time_start: '2026-10-03T06:00:00Z', time_end: null, valid_from: '2026-10-03T06:00:00Z', produced_at: '2026-10-03T07:00:00Z',
    }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => {
      const body = { data: [light, { ...light, id: 'ev-2', geo_lat: null, geo_lon: null }], next_cursor: null }
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
    }))
    const { result } = renderHook(() => useWorldEvents(), { wrapper })
    await waitFor(() => expect(result.current.read).toBe(true))
    const urls = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]))
    expect(urls).toEqual(['/api/v1/v3/events?limit=500&fields=summary'])
    expect(result.current.events).toHaveLength(1)
    expect(result.current.events[0]).toMatchObject({
      id: 'ev-1', lat: 48.8, lon: 2.3, countries: ['FR'], severity: 'high', lifecycle: 'developing', targetId: 'desk_alpha',
      ts: Date.parse('2026-10-03T06:00:00Z'), title: 'Port strike',
    })
  })
})

describe('placeFinding — through the subject geometry contract', () => {
  const index = geometryIndexFrom(TARGETS)
  const bare: WorldFinding = {
    id: 'f',
    lat: null,
    lon: null,
    countries: [],
    severity: 'info',
    targetId: 'desk_alpha',
    analystId: 'u',
    ts: 0,
    title: 't',
  }

  it('a subject that declares ONE place lends it', () => {
    expect(placeFinding(bare, index)).toMatchObject({ countries: ['FR'], lat: expect.any(Number) })
  })

  it('a region lends its codes but no point', () => {
    expect(placeFinding({ ...bare, targetId: 'theme_beta' }, index)).toMatchObject({ countries: ['FR', 'DE'], lat: null })
  })

  it('a country-shaped id with no descriptor is NOT parsed into a place', () => {
    expect(placeFinding({ ...bare, targetId: 'country_g20_us' }, index)).toMatchObject({ countries: [], lat: null })
  })
})
