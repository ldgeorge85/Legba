/**
 * Component test for the UI-1 daily-driver Findings feed.
 *
 * Asserts:
 *  - renders rows from the mocked `/findings` page
 *  - severity sort reorders rows
 *  - the live-tail (mocked WS) appends a new finding and badges it
 *  - the group-by-situation toggle exists (clustering seam)
 *  - saved views persist to localStorage
 *
 * `@/lib/ws` is mocked so the test can drive the live-tail callback
 * deterministically. `fetch` is stubbed at the HTTP boundary.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import type { RegistryEvent } from '@/lib/ws'

// --- mock the WS multiplexer; capture the onEvent callback ---
// The unified feed opens TWO subscriptions (findings tail `analyst.*.finding`
// + signals tail `legba.signals.>`); keep `capturedOnEvent` bound to the
// FINDINGS tail so the finding-tail tests target the right handler.
let capturedOnEvent: ((ev: RegistryEvent) => void) | null = null
const closeSpy = vi.fn()
vi.mock('@/lib/ws', () => ({
  subscribeRegistryEvents: (
    filter: string,
    onEvent: (ev: RegistryEvent) => void,
  ) => {
    // Ignore the signals tail here; bind the findings tail to capturedOnEvent.
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
    title: 'Findings Feed',
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

// The unified feed fetches BOTH /findings and /signals (source='all' default).
// Stub /signals empty by default so the findings assertions stay deterministic.
function stubFindingsFetch(signals: unknown = { data: [], next_cursor: null }) {
  const fetchMock = vi.fn().mockImplementation((url: string) =>
    Promise.resolve({
      ok: true,
      json: async () => (url.includes('/signals') ? signals : PAGE),
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// A page of THREE near-dup findings for one situation + a P-FS summary
// finding naming the latest/superseded split.
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

function mkFinding(
  id: string,
  data: Record<string, unknown>,
  produced_at: string,
  title: string,
) {
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

function stubClusteredFetch(signals: unknown = { data: [], next_cursor: null }) {
  const fetchMock = vi.fn().mockImplementation((url: string) =>
    Promise.resolve({
      ok: true,
      json: async () => (url.includes('/signals') ? signals : CLUSTERED_PAGE),
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  capturedOnEvent = null
  closeSpy.mockClear()
})

/** Minimal `/signals` REST row (SignalRestRow shape). */
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

describe('FindingsFeedPanel', () => {
  it('renders findings from the mocked page', async () => {
    stubFindingsFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument()
    })
    expect(screen.getByTestId('finding-f-low')).toBeInTheDocument()
  })

  it('severity sort puts critical above low', async () => {
    stubFindingsFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('findings-sort'), { target: { value: 'severity' } })

    await waitFor(() => {
      const list = screen.getByTestId('findings-list')
      const cards = within(list).getAllByTestId(/^finding-f-/)
      expect(cards[0]).toHaveAttribute('data-testid', 'finding-f-crit')
    })
  })

  it('appends a live finding from the mocked WS tail and badges it', async () => {
    stubFindingsFetch()
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

    await waitFor(() => {
      expect(screen.getByTestId('finding-f-live')).toBeInTheDocument()
    })
    expect(screen.getByTestId('finding-live-f-live')).toBeInTheDocument()
  })

  it('respects the active severity filter on the live tail', async () => {
    stubFindingsFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    // Filter to critical only.
    fireEvent.change(screen.getByTestId('findings-severity-filter'), {
      target: { value: 'critical' },
    })

    // A low-severity live finding should be dropped by the tail filter.
    capturedOnEvent!({
      type: 'event',
      subject: 'analyst.x.finding',
      payload: { id: 'f-low-live', title: 'low live', severity: 'low' },
      ts: '2026-06-03T12:00:00Z',
    })

    // Give react a tick; the row must NOT appear.
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByTestId('finding-f-low-live')).not.toBeInTheDocument()
  })

  it('exposes the group-by-situation clustering toggle (flat today)', async () => {
    stubFindingsFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())
    const toggle = screen.getByTestId('findings-group-toggle') as HTMLInputElement
    expect(toggle).toBeInTheDocument()
    expect(toggle.checked).toBe(true)
    // No clustering data → a single flat cluster wraps the rows.
    expect(screen.getByTestId('findings-cluster-__flat__')).toBeInTheDocument()
  })

  it('saves a view to localStorage', async () => {
    stubFindingsFetch()
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('my-view')
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('findings-save-view'))

    await waitFor(() => {
      expect(screen.getByTestId('findings-view-my-view')).toBeInTheDocument()
    })
    const stored = JSON.parse(localStorage.getItem('legba.findings.views') ?? '[]')
    expect(stored[0].name).toBe('my-view')
    promptSpy.mockRestore()
  })

  // --- situation clustering (P-FS, UI-1 finish) ---

  it('renders near-dup findings for one situation as ONE cluster (latest shown)', async () => {
    stubClusteredFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))

    // The situation cluster block renders (keyed by the signature).
    await waitFor(() =>
      expect(screen.getByTestId('findings-cluster-sit:brazil-coup')).toBeInTheDocument(),
    )
    // Only the latest (dup-v3) is shown up-front; the superseded near-dups
    // are collapsed (not in the DOM until the history expander is opened).
    expect(screen.getByTestId('finding-dup-v3')).toBeInTheDocument()
    expect(screen.queryByTestId('finding-dup-v2')).not.toBeInTheDocument()
    expect(screen.queryByTestId('finding-dup-v1')).not.toBeInTheDocument()
    // P-FS confirmed the split → confirmed badge present.
    expect(screen.getByTestId('findings-cluster-confirmed-sit:brazil-coup')).toBeInTheDocument()
  })

  it('expands per-cluster supersession history on demand', async () => {
    stubClusteredFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('findings-cluster-sit:brazil-coup')).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId('findings-cluster-history-toggle-sit:brazil-coup'))

    await waitFor(() => {
      expect(screen.getByTestId('finding-dup-v2')).toBeInTheDocument()
    })
    expect(screen.getByTestId('finding-dup-v1')).toBeInTheDocument()
    // history rows carry the superseded badge
    expect(screen.getByTestId('finding-superseded-dup-v2')).toBeInTheDocument()
  })

  it('flat/clustered toggle flips between grouped and ungrouped views', async () => {
    stubClusteredFetch()
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('findings-cluster-sit:brazil-coup')).toBeInTheDocument(),
    )

    // Switch to flat: every finding shown, no situation grouping.
    fireEvent.click(screen.getByTestId('findings-mode-flat'))
    await waitFor(() => {
      expect(screen.getByTestId('findings-cluster-__flat__')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('findings-cluster-sit:brazil-coup')).not.toBeInTheDocument()
    // all three dups now visible flat
    expect(screen.getByTestId('finding-dup-v1')).toBeInTheDocument()
    expect(screen.getByTestId('finding-dup-v2')).toBeInTheDocument()
    expect(screen.getByTestId('finding-dup-v3')).toBeInTheDocument()

    // Back to clustered.
    fireEvent.click(screen.getByTestId('findings-mode-clustered'))
    await waitFor(() =>
      expect(screen.getByTestId('findings-cluster-sit:brazil-coup')).toBeInTheDocument(),
    )
  })

  // --- #90 unified feed: signals as first-class rows ---

  it('renders a signal row in the unified feed (source=all)', async () => {
    stubFindingsFetch({ data: [mkSignal('sig-1', ['brazil'])], next_cursor: null })
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('signal-sig-1')).toBeInTheDocument()
    })
    // signal carries its SIGNAL badge and renders in the dedicated signals block
    expect(screen.getByTestId('signal-badge-sig-1')).toBeInTheDocument()
    expect(screen.getByTestId('findings-signals-flat')).toBeInTheDocument()
  })

  it('keeps signals flat — a signal sharing a finding geo does NOT join its cluster', async () => {
    // The signal's data.geo == 'brazil-coup' matches the finding cluster's key
    // tokens; the clusterKeyOf source-guard must keep it out of the cluster.
    stubClusteredFetch({ data: [mkSignal('sig-geo', ['brazil-coup'])], next_cursor: null })
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('findings-cluster-sit:brazil-coup')).toBeInTheDocument(),
    )
    await waitFor(() => expect(screen.getByTestId('findings-signals-flat')).toBeInTheDocument())
    // the signal is in the signals block, NOT inside the finding cluster
    const cluster = screen.getByTestId('findings-cluster-sit:brazil-coup')
    expect(within(cluster).queryByTestId('signal-sig-geo')).not.toBeInTheDocument()
    expect(
      within(screen.getByTestId('findings-signals-flat')).getByTestId('signal-sig-geo'),
    ).toBeInTheDocument()
  })

  it('disables severity + analyst filters under source=signals', async () => {
    stubFindingsFetch({ data: [mkSignal('sig-2')], next_cursor: null })
    render(wrap(<FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('findings-source-signals'))
    expect(screen.getByTestId('findings-severity-filter')).toBeDisabled()
    expect(screen.getByTestId('findings-analyst-filter')).toBeDisabled()
    // findings drop out of the source=signals view; the signal shows
    await waitFor(() => expect(screen.queryByTestId('finding-f-crit')).not.toBeInTheDocument())
    expect(screen.getByTestId('signal-sig-2')).toBeInTheDocument()
  })

  it('Live OFF clears the live-tail rows', async () => {
    stubFindingsFetch()
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

    // Toggle Live OFF → the live-only row is cleared; the REST rows remain.
    fireEvent.click(screen.getByTestId('findings-tail-toggle'))
    await waitFor(() => expect(screen.queryByTestId('finding-f-live2')).not.toBeInTheDocument())
    expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument()
  })
})
