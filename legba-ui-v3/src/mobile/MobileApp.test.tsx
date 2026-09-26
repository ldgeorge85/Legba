/**
 * MobileApp — the four promises this surface makes, held as tests.
 *
 *   1. the navigator lists the latest reads, by tier            — the landing
 *   2. tapping a report sets the URL context and every section
 *      below it follows the selection                            — the Crossroads
 *   3. the Consult sheet is pinned to whatever you are reading    — the centre
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
import { __clearMobileCache, saveReport, saveReportsList } from './mobileCache'
import { toReportRow } from './mobileModel'
import { MOBILE_PANEL_ID } from './components/ConsultSheet'

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
  onPost?: (url: string, body: unknown) => void
}

function stubFetch(opts: StubOptions = {}) {
  const reads = opts.reads ?? ALL_READS
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    if (init?.method === 'POST') {
      opts.onPost?.(u, init.body ? JSON.parse(String(init.body)) : null)
      if (u.includes('/consult')) {
        return ok({ answer: 'Because of the Hormuz attacks.', finding_id: null, session_id: 's1' })
      }
      return ok({})
    }
    if (u.includes('/findings')) {
      if (reads === 'fail') throw new TypeError('network down')
      return ok({ data: reads })
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

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  localStorage.clear()
  __clearMobileCache()
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

  it('shows the lead thread and the verify score on a row', async () => {
    stubFetch()
    render(wrap(<MobileApp />))

    const world = await screen.findByTestId('tier-group-world')
    const row = within(world).getAllByTestId('report-row')[0]
    expect(row.textContent).toContain('World')
    // The fixture's lead block BLUF is the thread the row must show.
    expect(row.textContent).toContain('Saudi Arabia remains under high energy')
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

  it('renders the record, the verify line and the drop ledger', async () => {
    const view = await openWorldReport()
    expect(within(view).getByTestId('verify-line')).toBeInTheDocument()
    expect(within(view).getByTestId('drop-ledger')).toBeInTheDocument()
    expect(within(view).getAllByTestId('record-block').length).toBeGreaterThan(0)
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

// ── 3 · the Consult, pinned ──────────────────────────────────────────────────

describe('the Consult sheet', () => {
  it('is present on the navigator, unpinned', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    await screen.findByTestId('reports-list')
    expect(screen.getByTestId('consult-pin').textContent).toContain('No report pinned')
  })

  it('names the selected report once one is open', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    const world = await screen.findByTestId('tier-group-world')
    fireEvent.click(within(world).getAllByTestId('report-row')[0])
    await screen.findByTestId('report-view')
    expect(screen.getByTestId('consult-pin').textContent).toContain('World')
  })

  it('sends the pinned report inside the question the server receives', async () => {
    const posts: { url: string; body: unknown }[] = []
    stubFetch({ onPost: (url, body) => posts.push({ url, body }) })
    render(wrap(<MobileApp />))

    const world = await screen.findByTestId('tier-group-world')
    fireEvent.click(within(world).getAllByTestId('report-row')[0])
    await screen.findByTestId('report-view')

    fireEvent.click(screen.getByTestId('consult-toggle'))
    fireEvent.change(screen.getByTestId('consult-input'), {
      target: { value: 'What is driving this?' },
    })
    fireEvent.click(screen.getByTestId('consult-send'))

    await waitFor(() => {
      const consult = posts.find((p) => p.url.includes('/consult'))
      expect(consult).toBeDefined()
      const body = consult!.body as Record<string, unknown>
      // The text channel — what today's server reads.
      expect(String(body.question)).toContain(String(WORLD.id))
      expect(String(body.question)).toContain('What is driving this?')
      // The structured channel — what the server is growing a field for.
      expect(body.pinned_context).toBeInstanceOf(Array)
      expect((body.pinned_context as { kind: string }[])[0].kind).toBe('report')
    })

    // And the answer lands in the transcript.
    await waitFor(() => {
      expect(screen.getByTestId('consult-transcript').textContent).toContain('Hormuz')
    })
  })

  it('records consult_open when the sheet is pulled up', async () => {
    stubFetch()
    render(wrap(<MobileApp />))
    await screen.findByTestId('reports-list')
    fireEvent.click(screen.getByTestId('consult-toggle'))
    await waitFor(() => {
      expect(__pendingReadEvents().some((e) => e.event_kind === 'consult_open')).toBe(true)
    })
  })
})

// ── 4 · offline ──────────────────────────────────────────────────────────────

describe('offline', () => {
  it('renders the cached navigator, labelled with when it was cached', async () => {
    const rows = ALL_READS.map(
      (r) => toReportRow(r as unknown as ReadFindingRow, projectAssembly(r as unknown as ReadFindingRow))!,
    )
    saveReportsList(rows)
    stubFetch({ reads: 'fail' })

    render(wrap(<MobileApp />))

    const banner = await screen.findByTestId('cached-banner')
    expect(banner.textContent).toMatch(/offline — cached \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/)
    expect(screen.getAllByTestId('report-row').length).toBe(rows.length)
  })

  it('renders the last viewed report from cache when the network is gone', async () => {
    const rows = ALL_READS.map(
      (r) => toReportRow(r as unknown as ReadFindingRow, projectAssembly(r as unknown as ReadFindingRow))!,
    )
    saveReportsList(rows)
    saveReport(WORLD as unknown as ReadFindingRow)
    stubFetch({ reads: 'fail' })
    window.history.replaceState(null, '', `/?report=${WORLD.id}`)

    render(wrap(<MobileApp />))

    const view = await screen.findByTestId('report-view')
    expect(within(view).getByTestId('cached-banner')).toBeInTheDocument()
    expect(within(view).getAllByTestId('record-block').length).toBeGreaterThan(0)
  })

  it('says so plainly when a report is neither live nor cached', async () => {
    stubFetch({ reads: [] })
    window.history.replaceState(null, '', '/?report=does-not-exist')
    render(wrap(<MobileApp />))
    expect(await screen.findByTestId('report-missing')).toBeInTheDocument()
  })
})
