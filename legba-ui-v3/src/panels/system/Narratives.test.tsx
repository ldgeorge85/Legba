/**
 * Component test for the `system.narratives` panel.
 *
 * Mocks the two list routes:
 *   GET /api/v1/v3/narratives       → contested-claim families
 *   GET /api/v1/v3/narratives/echo  → the directed co-carriage edges
 *
 * The tests pin the honesty behaviour: the server's `honesty_note` reaches the
 * screen verbatim in both modes, the carriage list is labeled as publication
 * order rather than influence, and the empty state does not claim to tell
 * "nothing detected" apart from "migration 0102 isn't applied" — because the
 * route answers the same empty envelope for both.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import NarrativesPanel from './Narratives'
import type { Narrative, NarrativeEchoEdge } from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'n1',
    panel_id: 'system_narratives',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Narratives',
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

const SERVER_NOTE =
  'Narratives are DETECT-ONLY reifications of contested-claim families and never ' +
  'mutate facts. Echo-lead is DESCRIPTIVE co-carriage timing — NOT a causal or ' +
  'coordination claim.'

const FAMILY: Narrative = {
  contention_id: 'c-1',
  subject_key: 'Strait of Hormuz',
  predicate_key: 'transit_status',
  status: 'contested',
  surfaced_value: null,
  variant_count: 2,
  carrier_source_count: 4,
  publish_dated_source_count: 2,
  signal_count: 9,
  fact_count: 1,
  first_seen_at: '2026-08-01T00:00:00Z',
  last_seen_at: '2026-08-03T00:00:00Z',
  span_hours: 48,
  lead_source_id: 'src.alpha',
  lead_first_seen_at: '2026-08-01T00:00:00Z',
  max_echo_lag_hours: 6,
  carriers: [
    { source_id: 'src.alpha', first_seen_at: '2026-08-01T00:00:00Z', lag_hours: 0, signal_count: 3 },
    { source_id: 'src.beta', first_seen_at: '2026-08-01T04:00:00Z', lag_hours: 4, signal_count: 2 },
  ],
  variants: [
    { value: 'closed', count: 5 },
    { value: 'restricted', count: 4 },
  ],
  opened_at: null,
  contention_surfaced_at: null,
  computed_at: null,
}

const EDGE: NarrativeEchoEdge = {
  leader_source_id: 'src.alpha',
  follower_source_id: 'src.beta',
  co_carried: 10,
  lead_count: 9,
  follow_within_count: 8,
  echo_ratio: 0.9,
  median_lag_hours: 3,
  mean_lag_hours: 3.4,
  min_lag_hours: 1,
  max_lag_hours: 9,
  echo_window_hours: 24,
  systematic: true,
  computed_at: null,
}

let families: Narrative[] = [FAMILY]
let edges: NarrativeEchoEdge[] = [EDGE]

function mockFetch() {
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url)
    if (u.includes('/narratives/echo')) {
      return {
        ok: true,
        json: async () => ({ edges, count: edges.length, honesty_note: SERVER_NOTE }),
      } as unknown as Response
    }
    if (u.includes('/narratives')) {
      return {
        ok: true,
        json: async () => ({
          narratives: families,
          count: families.length,
          honesty_note: SERVER_NOTE,
        }),
      } as unknown as Response
    }
    return { ok: true, json: async () => ({}) } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  families = [FAMILY]
  edges = [EDGE]
})

function renderPanel() {
  return render(wrap(<NarrativesPanel registration={reg()} scope={{}} mode="personal" />))
}

describe('NarrativesPanel — families', () => {
  it("renders the server's honesty note verbatim rather than paraphrasing it", async () => {
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('narratives-honesty-note')).toHaveTextContent(
      /NOT a causal or coordination claim/,
    )
  })

  it('lists contested-claim families as subject · predicate', async () => {
    mockFetch()
    renderPanel()
    const row = await screen.findByTestId('narrative-row-c-1')
    expect(row.textContent).toContain('Strait of Hormuz · transit_status')
    expect(row.textContent).toContain('contested')
  })

  it('expanding states the publish-dated denominator the echo timing rests on', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('narrative-row-c-1')
    fireEvent.click(screen.getByTestId('narrative-toggle-c-1'))
    expect(await screen.findByTestId('narrative-coverage-c-1')).toHaveTextContent(
      /2 of 4 carriers are publish-dated/,
    )
  })

  it('labels the carriage list as publication order, never as influence', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('narrative-row-c-1')
    fireEvent.click(screen.getByTestId('narrative-toggle-c-1'))
    const carriers = await screen.findByTestId('narrative-carriers-c-1')
    expect(carriers.textContent).toContain('src.alpha')
    expect(carriers.textContent).toContain('src.beta')
    expect(screen.getByTestId('narrative-row-c-1').textContent).toContain(
      'publication order — not influence',
    )
  })

  it('refuses to distinguish "nothing detected" from "no tables yet" on an empty list', async () => {
    families = []
    mockFetch()
    renderPanel()
    const empty = await screen.findByTestId('narratives-families-empty')
    expect(empty).toHaveTextContent(/migration 0102/)
    expect(empty).toHaveTextContent(/will not claim to tell them apart/)
  })

  it('passes the status filter to the route', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('narrative-row-c-1')
    fireEvent.change(screen.getByTestId('narratives-status-filter'), {
      target: { value: 'surfaced' },
    })
    await waitFor(() => {
      expect(f.mock.calls.some((c) => String(c[0]).includes('status=surfaced'))).toBe(true)
    })
  })
})

describe('NarrativesPanel — echo graph', () => {
  it('switches to the echo mode and renders a directed edge in publication language', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('narrative-row-c-1')
    fireEvent.click(screen.getByTestId('narratives-tab-echo'))

    const edge = await screen.findByTestId('echo-row-src.alpha-src.beta')
    expect(edge.textContent).toContain('published before')
    expect(edge.textContent).toContain('8/10 co-carried within')
    expect(edge.textContent).toContain('median lag 3.0h')
  })

  it("reports the server's systematic flag rather than re-deriving one", async () => {
    edges = [{ ...EDGE, systematic: false, echo_ratio: 0.99 }]
    mockFetch()
    renderPanel()
    fireEvent.click(await screen.findByTestId('narratives-tab-echo'))
    const edge = await screen.findByTestId('echo-row-src.alpha-src.beta')
    expect(edge.textContent).toContain('consistent')
    expect(edge.textContent).not.toContain('systematic')
  })

  it('the honesty note rides the echo mode too', async () => {
    mockFetch()
    renderPanel()
    fireEvent.click(await screen.findByTestId('narratives-tab-echo'))
    await screen.findByTestId('echo-row-src.alpha-src.beta')
    expect(screen.getByTestId('narratives-honesty-note')).toHaveTextContent(
      /NOT a causal or coordination claim/,
    )
  })

  it('toggles the systematic-only filter through to the route', async () => {
    const f = mockFetch()
    renderPanel()
    fireEvent.click(await screen.findByTestId('narratives-tab-echo'))
    await screen.findByTestId('echo-row-src.alpha-src.beta')
    fireEvent.click(screen.getByTestId('narratives-systematic-toggle'))
    await waitFor(() => {
      expect(f.mock.calls.some((c) => String(c[0]).includes('systematic_only=true'))).toBe(true)
    })
  })

  it('states an empty echo graph honestly', async () => {
    edges = []
    mockFetch()
    renderPanel()
    fireEvent.click(await screen.findByTestId('narratives-tab-echo'))
    expect(await screen.findByTestId('narratives-echo-empty')).toHaveTextContent(
      /migration 0102/,
    )
  })
})
