/**
 * Component test for the reformed unified Live Feed (S7-T4).
 *
 * Asserts:
 *  - renders finding rows from the mocked `/findings` page (intelligence stream)
 *  - the sort control reorders rows (severity → critical first)
 *  - the live-tail (mocked WS) appends a new finding and badges it
 *  - a typed severity facet drops a non-matching live finding
 *  - the two streams are HARD-separated (switch to Signals → findings gone)
 *  - superseded near-dups are hidden by default and revealable
 *  - saved views persist to localStorage
 *  - Live OFF clears the live-tail rows
 *
 * `@/lib/ws` is mocked so the test drives the live-tail callback; `fetch` is
 * stubbed at the HTTP boundary. Small pages render as a plain (non-virtualized)
 * list, so every row is queryable in jsdom.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import type { RegistryEvent } from '@/lib/ws'

// --- mock the WS multiplexer; capture the FINDINGS tail's onEvent ---
let capturedOnEvent: ((ev: RegistryEvent) => void) | null = null
const closeSpy = vi.fn()
vi.mock('@/lib/ws', () => ({
  subscribeRegistryEvents: (filter: string, onEvent: (ev: RegistryEvent) => void) => {
    if (filter !== 'legba.signals.>') capturedOnEvent = onEvent
    return { close: closeSpy }
  },
}))

import FindingsFeedPanel from './Findings'

function reg(): PanelRegistration {
  return {
    id: 'p1',
    panel_id: 'system_findings',
    descriptor_id: 'findings.feed',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Live Feed',
    mode: 'personal',
    layout_slot: 'system.findings.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const PAGE = {
  data: [
    {
      id: 'f-low',
      kind: 'finding',
      title: 'Minor anomaly',
      body: '',
      confidence: 0.4,
      severity: 'low',
      target_id: 'brazil',
      analyst_id: 'cred.analyst',
      analyst_version: 'v1',
      produced_at: '2026-06-03T10:00:00Z',
      derived_from: [],
      schema_uri: 'x',
      data: {},
    },
    {
      id: 'f-crit',
      kind: 'finding',
      title: 'Critical coup signal',
      body: 'body',
      confidence: 0.9,
      severity: 'critical',
      target_id: 'brazil',
      analyst_id: 'cred.analyst',
      analyst_version: 'v1',
      produced_at: '2026-06-03T09:00:00Z',
      derived_from: ['s1'],
      schema_uri: 'x',
      data: {},
    },
  ],
  next_cursor: null,
}

function stubFetch(signals: unknown = { data: [], next_cursor: null }, findings: unknown = PAGE) {
  const fetchMock = vi.fn().mockImplementation((url: string) =>
    Promise.resolve({
      ok: true,
      json: async () => (url.includes('/signals') ? signals : findings),
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function mkFinding(id: string, data: Record<string, unknown>, produced_at: string, title: string) {
  return {
    id,
    kind: 'finding',
    title,
    body: '',
    confidence: 0.8,
    severity: 'high',
    target_id: 'brazil',
    analyst_id: 'coup.analyst',
    analyst_version: 'v1',
    produced_at,
    derived_from: [],
    schema_uri: 'x',
    data,
  }
}

// Three near-dup findings for one situation + a P-FS summary naming the split.
const CLUSTERED_PAGE = {
  data: [
    mkFinding('dup-v3', { situation_id: 'brazil-coup' }, '2026-06-03T00:00:00Z', 'Coup risk v3 (latest)'),
    mkFinding('dup-v2', { situation_id: 'brazil-coup' }, '2026-06-02T00:00:00Z', 'Coup risk v2'),
    mkFinding('dup-v1', { situation_id: 'brazil-coup' }, '2026-06-01T00:00:00Z', 'Coup risk v1'),
    mkFinding(
      'pfs-summary',
      {
        sub_handler: 'finding_supersession',
        clusters: [
          {
            situation_signature: 'sit:brazil-coup',
            latest_finding_id: 'dup-v3',
            superseded_finding_ids: ['dup-v2', 'dup-v1'],
            reason: 'situation_id',
            score: 1.0,
          },
        ],
      },
      '2026-06-03T01:00:00Z',
      'Finding supersession summary',
    ),
  ],
  next_cursor: null,
}

function mkSignal(id: string, geo: string[] = [], title = 'Quake near border') {
  return {
    id,
    title,
    source_id: 'usgs.quakes',
    event_timestamp: '2026-06-03T08:00:00Z',
    produced_at: '2026-06-03T08:00:00Z',
    confidence: 0.0,
    target_id: 'brazil',
    data: { geo },
    geo,
    tags: ['seismic'],
    derived_from: [],
    schema_uri: 'x',
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  window.location.hash = ''
  capturedOnEvent = null
  closeSpy.mockClear()
})

describe('Live Feed (reformed, S7-T4)', () => {
  it('renders findings from the mocked page (intelligence stream)', async () => {
    stubFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())
    expect(screen.getByTestId('finding-f-low')).toBeInTheDocument()
  })

  it('sort:severity puts critical above low', async () => {
    stubFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('feed-sort'), { target: { value: 'severity' } })

    await waitFor(() => {
      const list = screen.getByTestId('feed-list')
      const cards = within(list).getAllByTestId(/^finding-f-/)
      expect(cards[0]).toHaveAttribute('data-testid', 'finding-f-crit')
    })
  })

  it('appends a live finding from the mocked WS tail and badges it', async () => {
    stubFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())
    expect(capturedOnEvent).toBeTypeOf('function')

    capturedOnEvent!({
      type: 'event',
      subject: 'analyst.cred.analyst.finding',
      payload: {
        id: 'f-live',
        title: 'Live incoming',
        severity: 'high',
        target_id: 'brazil',
        analyst_id: 'cred.analyst',
        produced_at: '2026-06-03T12:00:00Z',
      },
      ts: '2026-06-03T12:00:00Z',
    })

    await waitFor(() => expect(screen.getByTestId('finding-f-live')).toBeInTheDocument())
    expect(screen.getByTestId('finding-live-f-live')).toBeInTheDocument()
  })

  it('a severity facet drops a non-matching live finding', async () => {
    stubFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    // Facet: severity:critical (a chip) — client + live-tail both honor it.
    fireEvent.change(screen.getByTestId('feed-facet-severity'), { target: { value: 'critical' } })
    await waitFor(() => expect(screen.queryByTestId('finding-f-low')).not.toBeInTheDocument())

    capturedOnEvent!({
      type: 'event',
      subject: 'analyst.x.finding',
      payload: { id: 'f-low-live', title: 'low live', severity: 'low' },
      ts: '2026-06-03T12:00:00Z',
    })

    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByTestId('finding-f-low-live')).not.toBeInTheDocument()
    expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument()
  })

  it('HARD-separates streams: switching to Signals drops findings and shows the signal', async () => {
    stubFetch({ data: [mkSignal('sig-1', ['brazil'])], next_cursor: null })
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('feed-stream-signals'))

    await waitFor(() => expect(screen.getByTestId('signal-sig-1')).toBeInTheDocument())
    expect(screen.getByTestId('signal-badge-sig-1')).toBeInTheDocument()
    // findings never interleave into the signals stream
    expect(screen.queryByTestId('finding-f-crit')).not.toBeInTheDocument()
  })

  it('hides superseded near-dups by default and reveals them on toggle', async () => {
    stubFetch({ data: [], next_cursor: null }, CLUSTERED_PAGE)
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))

    // Latest shown; the two superseded dups hidden.
    await waitFor(() => expect(screen.getByTestId('finding-dup-v3')).toBeInTheDocument())
    expect(screen.queryByTestId('finding-dup-v2')).not.toBeInTheDocument()
    expect(screen.queryByTestId('finding-dup-v1')).not.toBeInTheDocument()

    // Reveal superseded.
    fireEvent.click(screen.getByTestId('feed-superseded-toggle'))
    await waitFor(() => expect(screen.getByTestId('finding-dup-v2')).toBeInTheDocument())
    expect(screen.getByTestId('finding-dup-v1')).toBeInTheDocument()
  })

  it('saves a view to localStorage', async () => {
    stubFetch()
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('my-view')
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('feed-save-view'))

    await waitFor(() => expect(screen.getByTestId('feed-view-my-view')).toBeInTheDocument())
    const stored = JSON.parse(localStorage.getItem('legba.feed.views') ?? '[]')
    expect(stored[0].name).toBe('my-view')
    promptSpy.mockRestore()
  })

  it('Live OFF clears the live-tail rows', async () => {
    stubFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    capturedOnEvent!({
      type: 'event',
      subject: 'analyst.cred.analyst.finding',
      payload: {
        id: 'f-live2',
        title: 'Live incoming 2',
        severity: 'high',
        target_id: 'brazil',
        analyst_id: 'cred.analyst',
        produced_at: '2026-06-03T12:00:00Z',
      },
      ts: '2026-06-03T12:00:00Z',
    })
    await waitFor(() => expect(screen.getByTestId('finding-f-live2')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('feed-live-toggle'))
    await waitFor(() => expect(screen.queryByTestId('finding-f-live2')).not.toBeInTheDocument())
    expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument()
  })
})
