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
 * …plus the three CONTINUITY guarantees the panel exists to hold (see the
 * `Live Feed — continuity` block at the bottom):
 *
 *  1. a background refetch preserves the scroll offset, every filter chip, and
 *     the very DOM nodes of the rows already on screen — new arrivals queue
 *     behind the "N new" pill instead of reflowing the list under the reader;
 *  2. every facet is operator-settable from the filter bar itself (desk,
 *     producer, output kind, effective-confidence floor) and they AND together;
 *  3. a sidebar desk selection SEEDS a removable desk chip, and clicking a feed
 *     row moves only the global selection — never the feed's filters or scroll.
 *
 * `@/lib/ws` is mocked so the test drives the live-tail callback; `fetch` is
 * stubbed at the HTTP boundary. Small pages render as a plain (non-virtualized)
 * list, so every row is queryable in jsdom.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import type { RegistryEvent } from '@/lib/ws'
import { selectRow, useSelection } from '@/state/selection'
import { resetFeedView, useFeedView } from '@/state/feedView'

/**
 * jsdom implements no layout, so `scrollTop` is a permanent 0 and the feed's
 * whole "did the ground move under the reader" behaviour would be untestable.
 * Back it with real per-element storage — the ONLY fake here, and it fakes the
 * browser, never the component.
 */
const SCROLL_TOPS = new WeakMap<HTMLElement, number>()
Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
  configurable: true,
  get(this: HTMLElement) {
    return SCROLL_TOPS.get(this) ?? 0
  },
  set(this: HTMLElement, v: number) {
    SCROLL_TOPS.set(this, Number(v) || 0)
  },
})

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

/** The QueryClient the most recent `wrap()` built — lets a test drive a
 *  BACKGROUND refetch (`invalidateQueries`) the way the 30s poll does, without
 *  going through the panel's explicit refresh button (which resets by design). */
let currentQc: QueryClient

function wrap(ui: ReactElement) {
  currentQc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={currentQc}>{ui}</QueryClientProvider>
}

function feed() {
  return <FindingsFeedPanel registration={reg()} scope={{}} mode="personal" />
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
  // The feed's view state is a module-scoped, session-persisted store (that is
  // the point — it outlives the panel's unmount). Reset it, and the selection
  // it now takes desk seeds from, so tests don't inherit each other's posture.
  sessionStorage.clear()
  resetFeedView()
  useSelection.getState().clear()
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

// ---------------------------------------------------------------------------
// The three continuity guarantees (the operator-reported defects)
// ---------------------------------------------------------------------------

/** A second page identical to PAGE plus one newly-produced finding on top. */
const PAGE_PLUS_NEW = {
  data: [
    {
      id: 'f-new',
      kind: 'finding',
      title: 'Freshly produced',
      body: '',
      confidence: 0.8,
      severity: 'high',
      target_id: 'brazil',
      analyst_id: 'cred.analyst',
      analyst_version: 'v1',
      produced_at: '2026-06-03T11:00:00Z',
      derived_from: [],
      schema_uri: 'x',
      data: {},
    },
    ...PAGE.data,
  ],
  next_cursor: null,
}

/**
 * A `/findings` stub that serves a DIFFERENT page on each of the FEED's own
 * reads, so a refetch genuinely delivers new rows (react-query's structural
 * sharing would otherwise hand back the identical object and nothing would move
 * at all).
 *
 * Only the feed's page reads advance the sequence: the desk-roster hooks the
 * filter bar shares with the Sidebar (`analyst_id=country_composition`,
 * `analyst_id=disruption_status`) also hit `/findings`, and letting them eat
 * sequence slots would non-deterministically hand the feed page 2 on first
 * paint. The feed is the only caller that pages at `limit=50`.
 */
function stubFetchSequence(pages: unknown[]) {
  let call = 0
  const isFeedPage = (url: string) => url.includes('/findings') && url.includes('limit=50')
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    let body: unknown
    if (url.includes('/signals')) body = { data: [], next_cursor: null }
    else if (isFeedPage(url)) body = pages[Math.min(call++, pages.length - 1)]
    else body = pages[0]
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Scroll the feed list away from the top (the "I am mid-read" state). */
function scrollFeedTo(px: number) {
  const list = screen.getByTestId('feed-list')
  fireEvent.scroll(list, { target: { scrollTop: px } })
  return list
}

/** Force the SAME background refetch the 30s poll performs. */
async function backgroundRefetch() {
  await act(async () => {
    await currentQc.invalidateQueries({ queryKey: ['feed-findings'] })
  })
}

describe('Live Feed — continuity across refetches (defect 1)', () => {
  it('a background refetch keeps the scroll offset, the filters and the very row nodes', async () => {
    stubFetchSequence([PAGE, PAGE_PLUS_NEW])
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    // Operator posture: a severity facet set by hand, scrolled down mid-read.
    fireEvent.change(screen.getByTestId('feed-facet-severity'), { target: { value: 'critical' } })
    await waitFor(() => expect(screen.getByTestId('feed-chip-severity-critical')).toBeInTheDocument())
    const list = scrollFeedTo(420)
    const critNodeBefore = screen.getByTestId('finding-f-crit')

    await backgroundRefetch()

    // Byte-for-byte: same chip, same scroll offset, and the SAME DOM node for a
    // row that was already on screen (stable keys ⇒ no remount).
    expect(screen.getByTestId('feed-chip-severity-critical')).toBeInTheDocument()
    expect(screen.getByTestId('feed-facet-severity')).toHaveValue('critical')
    expect(list.scrollTop).toBe(420)
    expect(screen.getByTestId('finding-f-crit')).toBe(critNodeBefore)
  })

  it('holds new arrivals behind an "N new" pill while scrolled, and merges on click', async () => {
    stubFetchSequence([PAGE, PAGE_PLUS_NEW])
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    const list = scrollFeedTo(300)
    await backgroundRefetch()

    // The new row is counted, NOT inserted — the list under the reader is still
    // exactly what it was.
    const pill = await screen.findByTestId('feed-resume-live')
    expect(pill).toHaveTextContent('1 new finding')
    expect(screen.queryByTestId('finding-f-new')).not.toBeInTheDocument()
    expect(list.scrollTop).toBe(300)

    fireEvent.click(pill)

    await waitFor(() => expect(screen.getByTestId('finding-f-new')).toBeInTheDocument())
    expect(screen.queryByTestId('feed-resume-live')).not.toBeInTheDocument()
    // Merging is also the "back to the top" gesture.
    expect(list.scrollTop).toBe(0)
  })

  it('merges straight in when the operator is at the top (nothing to protect)', async () => {
    stubFetchSequence([PAGE, PAGE_PLUS_NEW])
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    await backgroundRefetch()

    await waitFor(() => expect(screen.getByTestId('finding-f-new')).toBeInTheDocument())
    expect(screen.queryByTestId('feed-resume-live')).not.toBeInTheDocument()
  })

  it('scrolling back to the top drains the held rows without a click', async () => {
    stubFetchSequence([PAGE, PAGE_PLUS_NEW])
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    scrollFeedTo(300)
    await backgroundRefetch()
    await screen.findByTestId('feed-resume-live')

    scrollFeedTo(0)

    await waitFor(() => expect(screen.getByTestId('finding-f-new')).toBeInTheDocument())
    expect(screen.queryByTestId('feed-resume-live')).not.toBeInTheDocument()
  })

  it('remembers the scroll offset across an unmount (a Dockview tab switch)', async () => {
    stubFetch()
    const first = render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())
    scrollFeedTo(260)
    first.unmount()

    expect(useFeedView.getState().scrollTop).toBe(260)

    stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('feed-list').scrollTop).toBe(260))
  })
})

describe('Live Feed — operator-owned filters (defect 2)', () => {
  it('the desk facet is settable by hand from the filter bar', async () => {
    stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    // `brazil` is offered because the loaded page produced rows for it.
    const desk = screen.getByTestId('feed-facet-desk')
    await waitFor(() => expect(within(desk).getByRole('option', { name: 'Brazil' })).toBeInTheDocument())

    fireEvent.change(desk, { target: { value: 'brazil' } })

    await waitFor(() => expect(screen.getByTestId('feed-chip-target-brazil')).toBeInTheDocument())
    expect(desk).toHaveValue('brazil')
  })

  it('the producer facet writes an analyst chip and pushes it server-side', async () => {
    const fetchMock = stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    const producer = screen.getByTestId('feed-facet-producer')
    // The canonical unit roster is always offered, even before a unit row lands.
    expect(within(producer).getByRole('option', { name: 'Energy security' })).toBeInTheDocument()
    // …and a producer seen in the page joins it.
    await waitFor(() =>
      expect(within(producer).getByRole('option', { name: 'Cred analyst' })).toBeInTheDocument(),
    )

    fireEvent.change(producer, { target: { value: 'cred.analyst' } })

    await waitFor(() => expect(screen.getByTestId('feed-chip-analyst-cred.analyst')).toBeInTheDocument())
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).includes('analyst_id=cred.analyst')),
      ).toBe(true),
    )
  })

  it('the verification facet writes a chip and pushes it server-side (GLASS-1)', async () => {
    const fetchMock = stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('feed-facet-verify'), { target: { value: 'verified' } })

    await waitFor(() => expect(screen.getByTestId('feed-chip-verified-true')).toBeInTheDocument())
    // The facet reaches the whole corpus: the page is re-asked WITH the param,
    // not sieved client-side after the fetch.
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => String(call[0]).includes('verified=true'))).toBe(
        true,
      ),
    )

    // The judge picks swap in (the single-pick select clears its sibling chip)
    // — the J2 unsampled stratum is ONE first-class query param.
    fireEvent.change(screen.getByTestId('feed-facet-verify'), {
      target: { value: 'judge-unsampled' },
    })

    await waitFor(() => expect(screen.getByTestId('feed-chip-judge-unsampled')).toBeInTheDocument())
    expect(screen.queryByTestId('feed-chip-verified-true')).not.toBeInTheDocument()
    expect(screen.getByTestId('feed-facet-verify')).toHaveValue('judge-unsampled')
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).includes('judge_status=unsampled')),
      ).toBe(true),
    )
  })

  it('the effective-confidence floor drops below-floor rows, and composes (AND) with severity', async () => {
    stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-low')).toBeInTheDocument())

    // f-low is confidence 0.4 (below the 0.50 floor); f-crit is 0.9.
    fireEvent.change(screen.getByTestId('feed-facet-band'), { target: { value: '0.5' } })

    await waitFor(() => expect(screen.queryByTestId('finding-f-low')).not.toBeInTheDocument())
    expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument()
    expect(screen.getByTestId('feed-chip-minconf-0.5')).toBeInTheDocument()

    // AND a second facet that the remaining row FAILS ⇒ empty, not "last wins".
    fireEvent.change(screen.getByTestId('feed-facet-severity'), { target: { value: 'low' } })
    await waitFor(() => expect(screen.queryByTestId('finding-f-crit')).not.toBeInTheDocument())
    expect(screen.getByTestId('feed-chip-severity-low')).toBeInTheDocument()
    expect(screen.getByTestId('feed-chip-minconf-0.5')).toBeInTheDocument()
  })

  it('every active facet renders as a removable chip and survives an unmount', async () => {
    stubFetch()
    const first = render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('feed-facet-severity'), { target: { value: 'high' } })
    await waitFor(() => expect(screen.getByTestId('feed-chip-severity-high')).toBeInTheDocument())
    first.unmount()

    stubFetch()
    render(wrap(feed()))
    const chip = await screen.findByTestId('feed-chip-severity-high')
    expect(chip).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('feed-chip-remove-severity-high'))
    await waitFor(() => expect(screen.queryByTestId('feed-chip-severity-high')).not.toBeInTheDocument())
  })
})

describe('Live Feed — selecting is not filtering (defect 3)', () => {
  it('a sidebar desk click seeds a visible, removable desk chip', async () => {
    stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())
    expect(screen.queryByTestId('feed-chip-target-brazil')).not.toBeInTheDocument()

    // EXACTLY what components/Sidebar.tsx fires for a desk row.
    act(() => selectRow('target', 'brazil', 'Brazil', { origin: 'desks' }))

    await waitFor(() => expect(screen.getByTestId('feed-chip-target-brazil')).toBeInTheDocument())
    expect(screen.getByTestId('feed-facet-desk')).toHaveValue('brazil')

    // …and it is the operator's to remove — nothing about a seeded chip is special.
    fireEvent.click(screen.getByTestId('feed-chip-remove-target-brazil'))
    await waitFor(() => expect(screen.queryByTestId('feed-chip-target-brazil')).not.toBeInTheDocument())
  })

  it('clicking a row moves the selection but never the desk filter, the rows, or the scroll', async () => {
    const fetchMock = stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    act(() => selectRow('target', 'brazil', 'Brazil', { origin: 'desks' }))
    await waitFor(() => expect(screen.getByTestId('feed-chip-target-brazil')).toBeInTheDocument())
    // Let the seeded desk settle into the server-side query before we measure.
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => String(call[0]).includes('target_id=brazil'))).toBe(
        true,
      ),
    )
    const list = scrollFeedTo(180)
    const callsBefore = fetchMock.mock.calls.length
    const critNodeBefore = screen.getByTestId('finding-f-crit')

    fireEvent.click(screen.getByTestId('finding-f-crit'))

    // The Inspector gets the row…
    await waitFor(() => expect(useSelection.getState().selection?.id).toBe('f-crit'))
    expect(useSelection.getState().selection?.kind).toBe('finding')
    // …and the feed keeps its desk filter, its place, its rows, and issues no
    // refetch. This is the regression the operator reported.
    expect(screen.getByTestId('feed-chip-target-brazil')).toBeInTheDocument()
    expect(list.scrollTop).toBe(180)
    expect(screen.getByTestId('finding-f-crit')).toBe(critNodeBefore)
    expect(fetchMock.mock.calls.length).toBe(callsBefore)
  })

  it('highlights the inspected row in place', async () => {
    stubFetch()
    render(wrap(feed()))
    await waitFor(() => expect(screen.getByTestId('finding-f-crit')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('finding-f-crit'))

    await waitFor(() =>
      expect(screen.getByTestId('finding-f-crit')).toHaveAttribute('data-active', 'true'),
    )
    expect(screen.getByTestId('finding-f-low')).not.toHaveAttribute('data-active')
  })
})
