/**
 * ONE POINT, region by region: every region either follows the point or says
 * "(not filtered)". The Ledger's forecasts and the Watch log narrow to the
 * point's subject with a visible "Show all"; Consult asks about the point,
 * never about the whole roster or another subject beside it; a region that
 * does not follow it carries the label.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { RegionPoint } from './PointFilter'
import { ConsultPointLine } from './ConsultPointLine'
import { followsPoint } from '@/lib/point'
import { scopeFromTarget } from '@/lib/scopeFromReport'
import { clearPoint, pointSubjectOf, resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { ROSTER_PIN, resetAmbientPins, resolveAmbientPin, useAmbientPins } from '@/lib/consultContext'
import LedgerForecasts from '@/panels/ledger/LedgerForecasts'
import { resetLedgerView } from '@/panels/ledger/ledgerView'
import { ANCHORS, forecast, forecasts, scorecard, stubFetch } from '@/panels/ledger/__fixtures__/ledger'
import AlertLog from '@/v4/watch/AlertLog'
import { resetWatch } from '@/v4/watch/state'
import { useWorldState } from '@/v4/world/worldState'
import { dropCachedGets } from '@/lib/queryCache'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const point = (id: string, label: string) => act(() => useScope.getState().setScope(scopeFromTarget(id, label)))

beforeEach(() => {
  resetScope()
  resetAmbientPins()
  resetLedgerView()
  resetWatch()
  dropCachedGets()
  useSelection.getState().clear()
  useWorldState.getState().setSpan(24 * 3_600_000)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the point store', () => {
  it('a subject scope is the point; clearPoint clears the scope and the selection', () => {
    useSelection.getState().select({ kind: 'target', id: 'country_g20_br', label: 'Brazil' })
    useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil'))
    expect(pointSubjectOf(useScope.getState().scope)).toEqual({ id: 'country_g20_br', label: 'Brazil' })
    clearPoint()
    expect(useScope.getState().scope).toBeNull()
    expect(useSelection.getState().selection).toBeNull()
    expect(pointSubjectOf(null)).toBeNull()
  })
})

describe('Consult asks about the point', () => {
  it('a claim on the whole roster or on another subject never takes the pin from the point', () => {
    const scope = scopeFromTarget('country_watch_bf', 'Burkina Faso')
    const claims = [
      { key: 'wall-scope', surface: 'scope', object: { kind: 'report' as const, id: scope.id, label: scope.label }, seq: 1 },
      { key: 'accuracy#1', surface: 'accuracy', object: ROSTER_PIN, seq: 2 },
      { key: 'tile#1', surface: 'tile', object: { kind: 'target' as const, id: 'country_g20_ru', label: 'Russia' }, seq: 3 },
    ]
    expect(resolveAmbientPin(claims, scope)?.pin.id).toBe('country_watch_bf')
    // a record is narrower focus and still wins
    const withRecord = [...claims, { key: 'record#1', surface: 'record', object: { kind: 'finding' as const, id: 'f1', label: 'a finding' }, seq: 4 }]
    expect(resolveAmbientPin(withRecord, scope)?.pin.id).toBe('f1')
    // with no point the newest claim is the pin, as before
    expect(resolveAmbientPin(claims.slice(1), null)?.pin.id).toBe('country_g20_ru')
  })

  it('the point line names the point, not the roster the Accuracy page claims', () => {
    render(<ConsultPointLine pinned={0} />)
    act(() => useAmbientPins.getState().claim('accuracy#1', 'accuracy', ROSTER_PIN))
    expect(screen.getByTestId('consult-point-name').textContent).toBe('the whole roster')
    point('country_watch_bf', 'Burkina Faso')
    act(() => useAmbientPins.getState().claim('accuracy#1', 'accuracy', ROSTER_PIN))
    expect(screen.getByTestId('consult-point-name').textContent).toBe('Burkina Faso')
    act(() => clearPoint())
    expect(screen.getByTestId('consult-point-name').textContent).toBe('the whole roster')
  })

  it("shows the time band's window, read-only", () => {
    const end = Date.parse('2026-11-14T08:00:00Z')
    useWorldState.getState().setWindow(end - 24 * 3_600_000, end)
    render(<ConsultPointLine pinned={0} />)
    expect(screen.getByTestId('consult-point-window').textContent).toMatch(/window 24h/)
    act(() => useWorldState.getState().setWindow(end - 30 * 86_400_000, end))
    expect(screen.getByTestId('consult-point-window').textContent).toMatch(/window 30d/)
  })
})

describe('a region that does not follow the point says so', () => {
  it('"(not filtered)" over an unfiltered region while a point is set; nothing over one that follows', () => {
    render(
      <>
        <RegionPoint viewId="view:track_record">
          <p>track record</p>
        </RegionPoint>
        <RegionPoint viewId="view:forecasts">
          <p>forecasts</p>
        </RegionPoint>
      </>,
    )
    expect(screen.queryByTestId('region-not-filtered')).toBeNull()
    point('country_watch_bf', 'Burkina Faso')
    expect(screen.getAllByTestId('region-not-filtered')).toHaveLength(1)
    expect(screen.getByTestId('region-not-filtered').closest('[data-point-region]')).toHaveAttribute('data-point-region', 'view:track_record')
    act(() => clearPoint())
    expect(screen.queryByTestId('region-not-filtered')).toBeNull()
  })

  it('knows which regions follow', () => {
    for (const v of ['view:board_changed', 'v4.map', 'subject.page', 'view:forecasts', 'view:watch_alerts', 'subject.page:country_g20_br']) {
      expect(followsPoint(v)).toBe(true)
    }
    for (const v of ['view:track_record', 'view:proof', 'view:watch_silences', 'system.eval_scorecard']) expect(followsPoint(v)).toBe(false)
  })
})

describe('the Ledger follows the point', () => {
  const ON_BF = forecast(1, { subject_keys: ['country_watch_bf'] })
  const ON_RU = forecast(2, { subject_keys: ['country_g20_ru'] })
  const NONE = forecast(3)
  function stubLedger() {
    const s = stubFetch({
      '/v3/ledger/forecasts': { body: forecasts([ON_BF, ON_RU, NONE]) },
      '/v3/ledger/scorecard': { body: scorecard(0) },
      '/v3/system/receipt-anchors': { body: ANCHORS },
    })
    vi.stubGlobal('fetch', s.fn)
    return s
  }

  it('narrows the forecasts table to the point, with Show all to undo it', async () => {
    stubLedger()
    wrap(<LedgerForecasts />)
    await waitFor(() => expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(3))
    expect(screen.queryByTestId('ledger-forecasts-point')).toBeNull()
    point('country_watch_bf', 'Burkina Faso')
    await waitFor(() => expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(1))
    expect(screen.getByTestId('ledger-forecast-row')).toHaveAttribute('data-slug', ON_BF.slug)
    expect(screen.getByTestId('ledger-forecasts-point').textContent).toMatch(/Only Burkina Faso/)
    fireEvent.click(screen.getByTestId('ledger-forecasts-point-show-all'))
    expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(3)
    expect(screen.getByTestId('ledger-forecasts-point-not-filtered').textContent).toBe('(not filtered)')
    fireEvent.click(screen.getByTestId('ledger-forecasts-point-only'))
    expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(1)
    // a new point narrows again; clearing it shows everything with no line
    point('country_g20_ru', 'Russia')
    await waitFor(() => expect(screen.getByTestId('ledger-forecast-row')).toHaveAttribute('data-slug', ON_RU.slug))
    act(() => clearPoint())
    expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(3)
    expect(screen.queryByTestId('ledger-forecasts-point')).toBeNull()
  })

  it('says so when no forecast is about the point', async () => {
    stubLedger()
    point('country_g20_br', 'Brazil')
    wrap(<LedgerForecasts />)
    await waitFor(() => expect(screen.getByTestId('ledger-forecasts-none-on-point').textContent).toMatch(/No sealed forecast is about Brazil/))
  })

  it('reads the ledger as recorded when an instant is set, and keys the read on it', async () => {
    const s = stubLedger()
    const at = Date.parse('2026-03-10T00:00:00Z')
    act(() => useWorldState.getState().setAsOf(at))
    try {
      wrap(<LedgerForecasts />)
      await waitFor(() => expect(screen.getAllByTestId('ledger-forecast-row')).toHaveLength(3))
      expect(s.calls.some((u) => u.includes('/v3/ledger/forecasts') && u.includes(`at=${encodeURIComponent(new Date(at).toISOString())}`))).toBe(true)
    } finally {
      act(() => useWorldState.getState().setAsOf(null))
    }
  })
})

describe('the Watch log follows the point', () => {
  const alert = (id: string, subject_kind: string, subject_id: string) => ({
    id, kind: 'baseline_deviation', group: 'baseline', subject_kind, subject_id,
    severity: 'medium', max_severity: 'medium', summary: 'Example raised line', opened_at: '2026-11-14T11:00:00Z', last_raised_at: '2026-11-14T11:30:00Z',
    occurrences: 1, paged_count: 0,
  })
  const LOG = {
    measured: true, window_hours: 24, as_of: '2026-11-14T11:30:00Z', rule_version: 'x',
    kinds: [{ kind: 'baseline_deviation', group: 'baseline', pages_when: [] }],
    open: [alert('a1', 'target', 'country_watch_bf'), alert('a2', 'target', 'country_g20_ru'), alert('a3', 'source', 'source.example.a')],
    closed: [], paged: [],
  }
  function stubLog() {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const u = String(url)
      if (u.includes('/alerts')) return { ok: true, status: 200, json: async () => LOG } as unknown as Response
      return { ok: false, status: 404, json: async () => ({}), text: async () => '{}' } as unknown as Response
    }))
  }

  it('narrows the alert log to the point, with Show all to undo it', async () => {
    stubLog()
    wrap(<AlertLog />)
    await waitFor(() => expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(3))
    point('country_watch_bf', 'Burkina Faso')
    await waitFor(() => expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(1))
    expect(screen.getByTestId('watch-alert-point').textContent).toMatch(/Only Burkina Faso/)
    fireEvent.click(screen.getByTestId('watch-alert-point-show-all'))
    expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(3)
    expect(screen.getByTestId('watch-alert-point-not-filtered')).toBeInTheDocument()
  })

  it('says so when nothing fired about the point', async () => {
    stubLog()
    point('country_g20_br', 'Brazil')
    wrap(<AlertLog />)
    await waitFor(() => expect(screen.getByTestId('watch-alert-none-on-point').textContent).toMatch(/Nothing fired about Brazil/))
  })
})
