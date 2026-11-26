/**
 * MobileApp — the four promises this surface makes, held as tests.
 *
 *   1. the navigator lists the latest reads, by tier            — the landing
 *   2. tapping a report sets the URL context and every section
 *      below it follows the selection                            — the Crossroads
 *   3. (M-3g) the Consult moved to the phone shell's Consult tab — not tested here
 *   4. a dead network still renders yesterday's read, labelled    — the train
 *
 * Rendered against the LIVE read fixtures, through a stubbed `fetch`, so the
 * assertions are about real payload shapes rather than hand-written doubles.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import MobileApp from './MobileApp'
import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import countryRow from '@/v4/read/__fixtures__/assembly.country.json'
import thematicRow from '@/v4/read/__fixtures__/assembly.thematic.json'
import assessmentRow from '@/v4/read/__fixtures__/assessment.world.json'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'
import { __resetReadTelemetry, __pendingReadEvents } from '@/lib/readTelemetry'
import { consultActions } from '@/state/consultSession'
import { __LEGACY_KEYS, hasStoredReads, loadedAtLabel, purgeStoredReads } from './mobileCache'
import { MOBILE_PANEL_ID } from './mobileConsult'
import { dropCachedGets } from '@/lib/queryCache'

type Row = Record<string, unknown>

const WORLD = worldRow as unknown as Row
const COUNTRY = countryRow as unknown as Row
const THEMATIC = thematicRow as unknown as Row
const ASSESSMENT = assessmentRow as unknown as Row

/** A region read — the one tier the fixture set has no row for. */
const REGION: Row = {
  ...COUNTRY,
  id: 'region-0001-0000-0000-000000000001',
  analyst_id: 'region_composition',
  title: 'Region read · 3 Sep — Europe',
}

const ALL_READS = [WORLD, ASSESSMENT, REGION, COUNTRY, THEMATIC]

const CONSOLIDATION = {
  id: 'journal-consol-1',
  entry_kind: 'consolidation',
  title: 'What the week actually established',
  honesty_flags: [],
  period_start: '2026-08-28T00:00:00Z',
  period_end: '2026-09-03T00:00:00Z',
  produced_at: '2026-09-03T18:00:00Z',
  analyst_id: 'journal_consolidator',
  analyst_version: '1',
  verify_score: 0.82,
}

interface StubOptions {
  reads?: Row[] | 'fail'
  /** Fail every `/findings` read after the first N answered (a connection that drops). */
  failAfter?: number
  onPost?: (url: string, body: unknown) => void
  answer?: Record<string, unknown>
}

/** The `fields=summary` projection of a row: its scalars, no `data`, no `body`. */
function summaryOf(r: Row): Row {
  const { id, analyst_id, target_id, kind, title, produced_at, severity, confidence } = r
  return { id, analyst_id, target_id, kind, title, produced_at, severity, confidence }
}

/** The urls the stub answered, in order (the request pattern under test). */
const findingsUrls: string[] = []

/**
 * The `/findings` route as the registry answers it: `fields=summary` → the
 * summary projection; otherwise the full rows that match `analyst_id`,
 * `target_id` / `target_id_null` and `since`, newest first, cut to `limit`.
 */
function answerFindings(u: string, reads: Row[]): Row[] {
  const q = new URL(u, 'http://x').searchParams
  let rows = [...reads]
  const inList = q.get('analyst_id_in')?.split(',')
  if (inList) rows = rows.filter((r) => inList.includes(String(r.analyst_id)))
  if (q.get('analyst_id')) rows = rows.filter((r) => r.analyst_id === q.get('analyst_id'))
  if (q.get('target_id')) rows = rows.filter((r) => r.target_id === q.get('target_id'))
  if (q.get('target_id_null') === 'true') rows = rows.filter((r) => r.target_id == null)
  const t = (s: unknown) => Date.parse(String(s).replace(' ', 'T').replace(/([+-]\d{2})$/, '$1:00'))
  if (q.get('since')) rows = rows.filter((r) => t(r.produced_at) >= t(q.get('since')))
  rows.sort((a, b) => t(b.produced_at) - t(a.produced_at))
  rows = rows.slice(0, Number(q.get('limit') ?? 50))
  return q.get('fields') === 'summary' ? rows.map(summaryOf) : rows
}

function stubFetch(opts: StubOptions = {}) {
  const reads = opts.reads ?? ALL_READS
  findingsUrls.length = 0
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    if (init?.method === 'POST') {
      opts.onPost?.(u, init.body ? JSON.parse(String(init.body)) : null)
      if (u.includes('/consult')) {
        return ok(opts.answer ?? { answer: 'Because of the Hormuz attacks.', finding_id: null, session_id: 's1' })
      }
      return ok({})
    }
    if (u.includes('/findings')) {
      findingsUrls.push(u)
      if (reads === 'fail') throw new TypeError('network down')
      if (opts.failAfter != null && findingsUrls.length > opts.failAfter) throw new TypeError('network down')
      // GET /findings/{id} answers ONE full row
      const byId = /\/findings\/([^/?]+)/.exec(new URL(u, 'http://x').pathname)
      if (byId) {
        const row = reads.find((r) => String(r.id) === decodeURIComponent(byId[1]))
        return row ? ok(row) : notFound()
      }
      return ok({ data: answerFindings(u, reads) })
    }
    if (u.includes('/journal')) {
      return ok({ consolidation: CONSOLIDATION, entries: [], next_cursor: null, calibration: {} })
    }
    return ok({ data: [] })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

function notFound(): Response {
  return {
    ok: false,
    status: 404,
    json: async () => ({ detail: 'no such finding' }),
    text: async () => '{"detail":"no such finding"}',
  } as unknown as Response
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  localStorage.clear()
  purgeStoredReads()
  __resetReadTelemetry()
  consultActions().reset(MOBILE_PANEL_ID)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ── 1 · the navigator ────────────────────────────────────────────────────────

describe('the Reports navigator', () => {
  it('renders the latest reads grouped by tier', async () => {
    stubFetch()
    render(wrap(<MobileApp />))

    await screen.findByTestId('reports-list')
    // Every tier present in the fixture set gets its own group.
    for (const tier of ['world', 'assessment', 'region', 'country', 'thematic']) {
      expect(await screen.findByTestId(`tier-group-${tier}`)).toBeInTheDocument()
    }
    expect(screen.getAllByTestId('report-row').length).toBe(ALL_READS.length)
  })

  it('shows the lead on a row, and no unexplained verify pill', async () => {
    stubFetch()
    render(wrap(<MobileApp />))

    const world = await screen.findByTestId('tier-group-world')
    const row = within(world).getAllByTestId('report-row')[0]
    expect(row.textContent).toContain('World')
    // The summary row's lead is its title's headline.
    expect(row.textContent).toContain('Saudi Arabia remains under high energy')
    expect(row.querySelector('.m-chip')).toBeNull()
    expect(row.textContent).not.toMatch(/\d\.\d\d/)
  })

  it('The navigator reads the summary projection, never the full rows', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    await screen.findByTestId('tier-group-world')
    expect(findingsUrls).toHaveLength(1)
    const q = new URL(findingsUrls[0], 'http://x').searchParams
    expect(q.get('fields')).toBe('summary')
    expect(q.get('limit')).toBe('80')
    expect(q.get('analyst_id_in')).toContain('world_assessor')
  })

  it('The World read is titled for the world, not for its first block\'s country', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    const world = await screen.findByTestId('tier-group-world')
    const title = within(world).getAllByTestId('report-row')[0].querySelector('.m-row-target')!.textContent
    const firstBlock = projectAssembly(WORLD as unknown as ReadFindingRow)!.blocks.find((b) => b.target_name)!
    expect(title).not.toBe(firstBlock.target_name)
    expect(title).toMatch(/world/i)
  })

  it('The journal row is plain words, no raw markdown', async () => {
    stubFetch()
    const consol = { ...CONSOLIDATION, title: '**Anchor held.** The week in brief' }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url)
        if (u.includes('/journal')) return ok({ consolidation: consol, entries: [], next_cursor: null, calibration: {} })
        if (u.includes('/findings')) return ok({ data: answerFindings(u, ALL_READS) })
        return ok({ data: [] })
      }),
    )
    render(wrap(<MobileApp />))
    const group = await screen.findByTestId('journal-group')
    expect(group.textContent).toContain('Anchor held. The week in brief')
    expect(group.textContent).not.toContain('**')
  })

  it('lists the journal consolidation as its own row', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    const group = await screen.findByTestId('journal-group')
    expect(group.textContent).toContain('What the week actually established')
  })

  it('emits workspace_open under the mobile workspace, not the workstation one', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    await screen.findByTestId('reports-list')
    await waitFor(() => {
      const opens = __pendingReadEvents().filter((e) => e.event_kind === 'workspace_open')
      expect(opens.length).toBeGreaterThan(0)
      expect(opens[0].workspace).toBe('mobile')
    })
  })
})

// ── 2 · the Crossroads rule ──────────────────────────────────────────────────

describe('selecting a report', () => {
  async function openWorldReport() {
    stubFetch()
    render(wrap(<MobileApp />))
    const world = await screen.findByTestId('tier-group-world')
    const row = within(world).getAllByTestId('report-row')[0]
    fireEvent.click(row)
    return await screen.findByTestId('report-view')
  }

  it('sets the report as the URL context', async () => {
    await openWorldReport()
    expect(new URLSearchParams(window.location.search).get('report')).toBe(WORLD.id)
  })

  it('emits brief_read for the report actually opened', async () => {
    await openWorldReport()
    await waitFor(() => {
      const briefs = __pendingReadEvents().filter((e) => e.event_kind === 'brief_read')
      expect(briefs.length).toBeGreaterThan(0)
      expect(briefs[0].subject_id).toBe(WORLD.id)
      expect(briefs[0].workspace).toBe('mobile')
    })
  })

  it('renders the record, without the engine line', async () => {
    const view = await openWorldReport()
    expect(within(view).queryByTestId('verify-line')).toBeNull()
    expect(within(view).queryByTestId('drop-ledger')).toBeNull()
    expect(within(view).getAllByTestId('record-block').length).toBeGreaterThan(0)
  })

  it('Opening a report fetches that one read at full weight, once', async () => {
    await openWorldReport()
    expect(findingsUrls).toHaveLength(2)
    // by id, exact — GET /findings/{id}, no list filters
    const u = new URL(findingsUrls[1], 'http://x')
    expect(u.pathname).toBe(`/api/v1/findings/${encodeURIComponent(String(WORLD.id))}`)
    expect([...u.searchParams.keys()]).toEqual([])
  })

  it('An Assessment fetches its underlying world read beside it when it is in the window', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    const group = await screen.findByTestId('tier-group-assessment')
    fireEvent.click(within(group).getAllByTestId('report-row')[0])
    await screen.findByTestId('report-view')
    // the list, the assessment, and (only if its spine is in the loaded page) the spine
    expect(findingsUrls.length).toBeGreaterThanOrEqual(2)
    expect(findingsUrls.length).toBeLessThanOrEqual(3)
    const assessment = ALL_READS.find((r) => r.analyst_id === 'world_assessment')!
    expect(new URL(findingsUrls[1], 'http://x').pathname).toBe(`/api/v1/findings/${encodeURIComponent(String(assessment.id))}`)
  })

  it('fills Findings, Targets and Signals from the report itself', async () => {
    const view = await openWorldReport()
    const assembly = projectAssembly(WORLD as unknown as ReadFindingRow)!

    fireEvent.click(within(view).getByTestId('section-toggle-findings'))
    expect(within(view).getAllByTestId('finding-item').length).toBe(assembly.blocks.length)

    fireEvent.click(within(view).getByTestId('section-toggle-signals'))
    expect(within(view).getAllByTestId('signal-item').length).toBeGreaterThan(0)

    fireEvent.click(within(view).getByTestId('section-toggle-entities'))
    expect(within(view).getAllByTestId('entity-chip').length).toBeGreaterThan(0)
  })

  it('narrows every section when an entity is tapped, and restores on clear', async () => {
    const view = await openWorldReport()
    fireEvent.click(within(view).getByTestId('section-toggle-findings'))
    const before = within(view).getAllByTestId('finding-item').length

    fireEvent.click(within(view).getByTestId('section-toggle-entities'))
    const chip = within(view).getAllByTestId('entity-chip')[0]
    const entityId = chip.getAttribute('data-entity-id')
    fireEvent.click(chip)

    // The selection is in the URL, and the findings list has shrunk with it.
    await waitFor(() => {
      expect(new URLSearchParams(window.location.search).get('entity')).toBe(entityId)
    })
    const after = within(view).getAllByTestId('finding-item').length
    expect(after).toBeLessThan(before)
    expect(screen.getByTestId('entity-filter-bar')).toBeInTheDocument()

    fireEvent.click(within(screen.getByTestId('entity-filter-bar')).getByText('Clear'))
    await waitFor(() => {
      expect(new URLSearchParams(window.location.search).get('entity')).toBeNull()
    })
    expect(within(screen.getByTestId('report-view')).getAllByTestId('finding-item').length).toBe(
      before,
    )
  })

  it('returns to the navigator on Back', async () => {
    const view = await openWorldReport()
    fireEvent.click(within(view).getByTestId('report-back'))
    await screen.findByTestId('reports-list')
    expect(new URLSearchParams(window.location.search).get('report')).toBeNull()
  })
})

// ── 3 · the Consult (M-3g: no longer here — Consult is the phone shell's tab,
//        `screens/consult/ConsultScreen.test.tsx`) ───────────────────────────

// ── 4 · offline ──────────────────────────────────────────────────────────────

describe('offline (nothing of the record is stored on the device)', () => {
  it('deletes the list and the report bodies an earlier build stored', async () => {
    localStorage.setItem(__LEGACY_KEYS.list, JSON.stringify({ v: 1, savedAt: 'x', rows: [] }))
    localStorage.setItem(`${__LEGACY_KEYS.reportPrefix}abc`, JSON.stringify({ v: 1, savedAt: 'x', row: WORLD }))
    localStorage.setItem('legba_consult_model', 'core') // a preference, kept
    expect(hasStoredReads()).toBe(true)
    stubFetch()
    render(wrap(<MobileApp />))
    await screen.findByTestId('reports-list')
    expect(hasStoredReads()).toBe(false)
    expect(localStorage.getItem('legba_consult_model')).toBe('core')
  })

  it('writes no read to localStorage while reading, opening a report included', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    const world = await screen.findByTestId('tier-group-world')
    fireEvent.click(within(world).getAllByTestId('report-row')[0])
    await screen.findByTestId('report-view')
    for (let i = 0; i < localStorage.length; i += 1) {
      const v = localStorage.getItem(localStorage.key(i)!) ?? ''
      expect(v).not.toContain('Saudi Arabia remains under high energy')
    }
    expect(hasStoredReads()).toBe(false)
  })

  it('a refresh that fails keeps what was loaded on screen, with the time it was loaded', async () => {
    stubFetch({ failAfter: 1 })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MobileApp />
      </QueryClientProvider>,
    )
    await screen.findByTestId('tier-group-world')
    expect(screen.queryByTestId('offline-banner')).toBeNull()
    dropCachedGets() // past the shared GET cache's 30 s reuse: the next read goes to the network
    await qc.refetchQueries({ queryKey: ['mobile-reads'] }).catch(() => {})
    const banner = await screen.findByTestId('offline-banner')
    expect(banner.textContent).toMatch(/^No connection\. Showing what was loaded at \d{2}:\d{2} UTC\.$/)
    expect(screen.getAllByTestId('report-row').length).toBe(ALL_READS.length)
  })

  it('with no connection and nothing loaded, says so — no stale list from the device', async () => {
    stubFetch({ reads: 'fail' })
    render(wrap(<MobileApp />))
    const msg = await screen.findByTestId('reports-offline')
    expect(msg.textContent).toMatch(/No connection\. Nothing has been loaded yet\./)
    expect(screen.queryAllByTestId('report-row')).toHaveLength(0)
  })

  it('a read not loaded before the connection dropped says so', async () => {
    stubFetch({ failAfter: 1 }) // the list answers; the full read does not
    window.history.replaceState(null, '', `/?report=${WORLD.id}`)
    render(wrap(<MobileApp />))
    const msg = await screen.findByTestId('report-missing')
    expect(msg.textContent).toMatch(/No connection\. This read was not loaded/)
  })

  it('says so plainly when a report is not in the loaded window', async () => {
    stubFetch({ reads: [] })
    window.history.replaceState(null, '', '/?report=does-not-exist')
    render(wrap(<MobileApp />))
    expect(await screen.findByTestId('report-missing')).toHaveTextContent('not in the loaded window')
  })
})

describe('the offline line', () => {
  it('names the time what is on screen was loaded, in UTC', () => {
    expect(loadedAtLabel(Date.UTC(2026, 10, 14, 7, 41))).toBe('No connection. Showing what was loaded at 07:41 UTC.')
    expect(loadedAtLabel(null)).toBe('No connection.')
  })
})
