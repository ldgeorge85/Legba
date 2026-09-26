/**
 * The Navigator — the landing's product rail.
 *
 * Two properties are load-bearing and both are asserted here:
 *
 *  1. It renders PRODUCTS, by tier, from the rows the reader already fetches —
 *     no new endpoint, and the tier of a row is read off its producer and its
 *     desk id rather than guessed.
 *  2. A click performs EXACTLY two writes — one `setScope`, one `select` — and
 *     nothing else. Everything downstream is subscription; if this file ever
 *     needs to grow a third write to make a panel follow, the scope/focus
 *     contract has been broken somewhere else.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import NavigatorPanel, { ageLabel } from './Navigator'
import worldRow from '@/v4/read/__fixtures__/assembly.world.json'
import type { PanelRegistration } from '@/types'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'

function reg(): PanelRegistration {
  return {
    id: 'nav1',
    panel_id: 'system_navigator',
    descriptor_id: 'system',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Navigator',
    mode: 'personal',
    layout_slot: 'system.navigator.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-09-06T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const REGION = {
  id: 'r-mena',
  analyst_id: 'region_composition',
  target_id: 'region_mena',
  title: 'MENA rollup · 5 Sep',
  severity: 'high',
  produced_at: '2026-09-05T06:00:00Z',
  data: {},
}
const COUNTRY = {
  id: 'c-br',
  analyst_id: 'country_composition',
  target_id: 'country_g20_br',
  title: 'Brazil composition',
  severity: 'medium',
  produced_at: '2026-09-05T05:00:00Z',
  data: {},
}
const THEMATIC = {
  id: 't-lane',
  analyst_id: 'country_composition',
  target_id: 'lane_hormuz',
  title: 'Hormuz lane',
  severity: 'low',
  produced_at: '2026-09-05T04:00:00Z',
  data: {},
}
const ASSESSMENT = {
  id: 'a-1',
  analyst_id: 'world_assessment',
  target_id: 'world',
  title: 'The Assessment voice',
  severity: 'high',
  produced_at: '2026-09-03T12:30:00Z',
  data: {},
}

/** One stub covering every producer the rail queries, plus the journal. */
function stubFetch() {
  const mock = vi.fn(async (url: RequestInfo | URL) => {
    const u = String(url)
    const body = (data: unknown) =>
      new Response(JSON.stringify(data), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    if (u.includes('analyst_id=world_assessor')) return body({ data: [worldRow] })
    if (u.includes('analyst_id=world_assessment')) return body({ data: [ASSESSMENT] })
    if (u.includes('analyst_id=region_composition')) return body({ data: [REGION] })
    if (u.includes('analyst_id=country_composition')) return body({ data: [COUNTRY, THEMATIC] })
    if (u.includes('/journal'))
      return body({
        consolidation: {
          id: 'j-cons',
          entry_kind: 'consolidation',
          title: 'Open consolidation',
          honesty_flags: [],
          period_start: '2026-09-01',
          period_end: '2026-09-07',
          produced_at: '2026-09-06T00:00:00Z',
          analyst_id: null,
          analyst_version: null,
          verify_score: null,
        },
        entries: [],
        next_cursor: null,
        calibration: { available: false },
      })
    return body({ data: [] })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

const panel = () => <NavigatorPanel registration={reg()} scope={{}} mode="personal" />

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  resetScope()
  useSelection.getState().clear()
})

describe('the rail renders today’s reads by tier', () => {
  it('pins the world read and its Assessment voice under Today', async () => {
    stubFetch()
    render(wrap(panel()))
    await waitFor(() => expect(screen.getByTestId('navigator-section-today')).toBeInTheDocument())
    const today = screen.getByTestId('navigator-section-today')
    await waitFor(() => expect(today.querySelectorAll('[data-testid="navigator-row"]').length).toBe(2))
    expect(today.textContent).toContain('World read')
    expect(today.textContent).toContain('The Assessment voice')
  })

  it('separates country desks from thematic lanes', async () => {
    stubFetch()
    render(wrap(panel()))
    const countries = await screen.findByTestId('navigator-section-countries')
    const thematic = await screen.findByTestId('navigator-section-thematic')
    // Both start collapsed — the rail opens on the day, not on the roster.
    fireEvent.click(countries.querySelector('button')!)
    fireEvent.click(thematic.querySelector('button')!)
    await waitFor(() => expect(countries.textContent).toContain('Brazil'))
    // `lane_*` is a supply-chain desk, not a country — the split is read off
    // the desk id, never guessed from the title.
    expect(thematic.textContent).toContain('Hormuz')
    expect(countries.textContent).not.toContain('Hormuz')
  })

  it('shows the desk count off `blocks.length` — a number the record carries', async () => {
    stubFetch()
    render(wrap(panel()))
    const today = await screen.findByTestId('navigator-section-today')
    await waitFor(() => expect(today.textContent).toContain('▸8'))
    // The Assessment row has no assembly block, so it shows NO count rather
    // than a zero — a zero here reads as "composed from nothing".
    const rows = today.querySelectorAll('[data-testid="navigator-row"]')
    expect(rows[1].textContent).not.toContain('▸0')
  })

  it('lists the open journal consolidation', async () => {
    stubFetch()
    render(wrap(panel()))
    await waitFor(() =>
      expect(screen.getByTestId('navigator-journal-row').textContent).toContain(
        'Open consolidation',
      ),
    )
  })
})

describe('one click = exactly one setScope + one select', () => {
  it('scopes the wall to the report AND focuses the record', async () => {
    stubFetch()
    const setScopeSpy = vi.spyOn(useScope.getState(), 'setScope')
    const selectSpy = vi.spyOn(useSelection.getState(), 'select')
    render(wrap(panel()))
    const today = await screen.findByTestId('navigator-section-today')
    await waitFor(() => expect(today.querySelectorAll('[data-testid="navigator-row"]').length).toBe(2))

    fireEvent.click(today.querySelectorAll('[data-testid="navigator-row"]')[0])

    expect(setScopeSpy).toHaveBeenCalledTimes(1)
    expect(selectSpy).toHaveBeenCalledTimes(1)

    const scope = useScope.getState().scope!
    expect(scope.kind).toBe('report')
    expect(scope.id).toBe(worldRow.id)
    expect(scope.origin).toBe('navigator')
    // The world it carries — projected from the payload already in hand.
    expect(scope.members.targetIds.length).toBeGreaterThan(1)
    expect(scope.members.analystIds).toEqual(['world_assessor'])

    const sel = useSelection.getState().selection!
    expect(sel.kind).toBe('report')
    expect(sel.id).toBe(worldRow.id)
    expect(sel.origin).toBe('navigator')
    // `ts` rides along so a timeline can center on the read without a fetch.
    expect(typeof sel.preview?.ts).toBe('number')
  })

  it('a region row scopes to that region, not to the world', async () => {
    stubFetch()
    render(wrap(panel()))
    const regions = await screen.findByTestId('navigator-section-regions')
    // The rail labels a desk by its own name, humanized from the target id —
    // never by the raw id, and never by the report's headline.
    await waitFor(() => expect(regions.textContent).toContain('Region Mena'))

    fireEvent.click(regions.querySelector('[data-testid="navigator-row"]')!)

    const scope = useScope.getState().scope!
    expect(scope.id).toBe('r-mena')
    expect(scope.members.targetIds).toEqual(['region_mena'])
  })

  it('a journal row scopes as a journal_entry — the kind the vocabulary lacked', async () => {
    stubFetch()
    render(wrap(panel()))
    const row = await screen.findByTestId('navigator-journal-row')
    fireEvent.click(row)
    expect(useScope.getState().scope?.kind).toBe('journal_entry')
    expect(useSelection.getState().selection?.kind).toBe('journal_entry')
  })

  it('marks the scoped row as current so the rail says where you are', async () => {
    stubFetch()
    render(wrap(panel()))
    const regions = await screen.findByTestId('navigator-section-regions')
    await waitFor(() => expect(regions.textContent).toContain('Region Mena'))
    const row = regions.querySelector('[data-testid="navigator-row"]')!
    expect(row).not.toHaveAttribute('aria-current')
    fireEvent.click(row)
    await waitFor(() =>
      expect(
        regions.querySelector('[data-testid="navigator-row"]'),
      ).toHaveAttribute('aria-current', 'true'),
    )
  })
})

describe('ageLabel', () => {
  const now = Date.parse('2026-09-06T12:00:00Z')
  it('is coarse on purpose — the rail answers "is today’s read here yet"', () => {
    expect(ageLabel('2026-09-06T11:40:00Z', now)).toBe('now')
    expect(ageLabel('2026-09-06T06:00:00Z', now)).toBe('6h')
    expect(ageLabel('2026-09-01T12:00:00Z', now)).toBe('5d')
    expect(ageLabel('2026-06-06T12:00:00Z', now)).toBe('3mo')
  })
  it('renders an absence as an absence', () => {
    expect(ageLabel(null, now)).toBe('—')
    expect(ageLabel('not a date', now)).toBe('—')
  })
})
