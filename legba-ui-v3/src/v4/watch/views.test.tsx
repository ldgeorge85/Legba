/**
 * The Watch views (U-9), rendered over invented example answers: what each
 * region reads (and does NOT read), the designed empty and not-yet states,
 * the point a row click makes, and the Consult actions through the contract.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { useSelection } from '@/state/selection'
import { applyPanelAction, clearViewUndo, undoView } from '@/lib/panelActions'
import { useWorldState } from '@/v4/world/worldState'
import AlertLog from './AlertLog'
import Silences from './Silences'
import SourceEdits from './SourceEdits'
import { resetWatch, useWatch, watchOnScreen } from './state'
import './register'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const LOG = {
  measured: true,
  window_hours: 24,
  as_of: '2026-11-14T11:30:00Z',
  rule_version: '2026-11-14/1',
  kinds: [
    { kind: 'source_cadence_stall', group: 'source_health', pages_when: ['source_cadence_stall >= high'] },
    { kind: 'baseline_deviation', group: 'baseline', pages_when: [] },
    { kind: 'host_disk', group: 'host', pages_when: ['host_disk >= low'] },
  ],
  open: [
    {
      id: 'a1', kind: 'source_cadence_stall', group: 'source_health', subject_kind: 'source', subject_id: 'source.example.harbour_notices',
      severity: 'high', max_severity: 'high', summary: 'Example raised line', opened_at: '2026-11-14T10:00:00Z', last_raised_at: '2026-11-14T11:00:00Z',
      occurrences: 2, paged_count: 1, paged_severity: 'high',
    },
    {
      id: 'a2', kind: 'baseline_deviation', group: 'baseline', subject_kind: 'target', subject_id: 'example_land',
      severity: 'medium', max_severity: 'medium', summary: 'Example raised line', opened_at: '2026-11-14T11:00:00Z', last_raised_at: '2026-11-14T11:30:00Z',
      occurrences: 1, paged_count: 0,
    },
    {
      id: 'a3', kind: 'host_disk', group: 'host', subject_kind: 'host', subject_id: 'host',
      severity: 'low', max_severity: 'low', summary: 'Example raised line', opened_at: '2026-11-14T09:00:00Z', last_raised_at: '2026-11-14T09:00:00Z',
      occurrences: 1, paged_count: 1, paged_severity: 'low',
    },
  ],
  closed: [],
  paged: [{ event_id: 'e1', alert_id: 'a1', raised_at: '2026-11-14T10:00:00Z', kind: 'source_cadence_stall', severity: 'high', rule_entry: 'source_cadence_stall >= high', outcomes: ['delivered'], delivered: true }],
}

const FIRING = [
  { source_id: 'source.example.a', state: 'active', status: 'firing', last_seen_at: '2026-11-14T11:00:00Z' },
  { source_id: 'source.example.quiet_feed', state: 'active', status: 'silent', last_seen_at: '2026-11-11T11:00:00Z', quiet_class: 'published_nothing' },
  { source_id: 'source.example.broken_feed', state: 'active', status: 'error', last_seen_at: '2026-11-10T11:00:00Z', quiet_class: 'polling_fails' },
]

const DRIFT_EMPTY = {
  available: true, window_hours: 168, classified: 0, open_flags_total: 0, history_rewrites: 0,
  auto_closed: { window: 0, total: 0, reopened: 0 }, would_auto_close: { window: 0, total: 0, rule_version: null, switch_on: false },
}
const DRIFT_SOME = { ...DRIFT_EMPTY, classified: 4, open_flags_total: 1, would_auto_close: { window: 1, total: 1, rule_version: 'drift-autoclose/1', switch_on: false } }
const FLAGS = {
  available: true,
  flags: [
    {
      id: 7, address: 'https://example.org/news/item-1', source_id: 'source.example.harbour_notices', drift_class: 'substantive_edit',
      change_type: 'fact_corrected', acknowledged: 'silent', claim_effect: 'cited_text_changed', dependant_kind: 'finding', dependant_id: 'f-1',
      changed: [{ earlier: 'Twelve ships waited.', later: 'Nine ships waited.' }], removed: [], would_auto_close: true,
    },
  ],
}
const HISTORY = { available: true, address: 'https://example.org/news/item-1', versions: [{ id: 1, first_seen_at: '2026-11-12T09:10:00Z', last_seen_at: '2026-11-12T09:10:00Z', fetch_count: 1 }, { id: 2, first_seen_at: '2026-11-13T07:02:00Z', last_seen_at: '2026-11-13T07:02:00Z', fetch_count: 2 }] }
const EDITORS = {
  available: true,
  sources: [
    { subject: 'source.zeta.wire', edit_record: { '30d': { pages_rechecked: 40, assessed: 9, acknowledged: { silent: 5 } } } },
    { subject: 'source.alpha.daily', edit_record: { '30d': { pages_rechecked: 12, assessed: 1 } } },
  ],
}

let answers: Record<string, unknown> = {}
let fetchMock: ReturnType<typeof vi.fn>

function stub(map: Record<string, unknown>) {
  answers = map
  fetchMock = vi.fn(async (url: string) => {
    const path = String(url).replace(/^.*\/api\/v1/, '')
    const key = Object.keys(answers).find((k) => path.startsWith(k))
    if (!key) return { ok: false, status: 404, json: async () => ({}), text: async () => '{}' } as unknown as Response
    return { ok: true, status: 200, json: async () => answers[key] } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
}
const asked = () => fetchMock.mock.calls.map((c) => String(c[0]).replace(/^.*\/api\/v1/, '')).filter((u) => u !== '/auth/me')

beforeEach(() => {
  resetWatch()
  clearViewUndo()
  useSelection.getState().clear()
  useWorldState.getState().setSpan(24 * 3_600_000)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the alert log', () => {
  it('reads the one log over the clock window and shows reader kinds with the page decision', async () => {
    stub({ '/v3/system/alerts/log': LOG })
    render(wrap(<AlertLog />))
    await waitFor(() => expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(2))
    expect(asked()).toEqual(['/v3/system/alerts/log?hours=24'])
    const [first, second] = screen.getAllByTestId('watch-alert-row')
    expect(first).toHaveAttribute('data-group', 'fired')
    expect(first).toHaveTextContent('Logged only · the page rule has no entry for this kind')
    expect(second).toHaveTextContent('Paged at high · the page rule pages this kind at high or above · delivered')
    expect(second).toHaveTextContent('raised 2 times')
    expect(screen.getByTestId('watch-alert-open')).toHaveTextContent('3')
    // the system's own kinds are one chip away, never mixed in by default
    fireEvent.click(screen.getByTestId('watch-filter-group-all'))
    expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(3)
    fireEvent.click(screen.getByTestId('watch-filter-paged-paged'))
    expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(2)
    // a filter is never another request
    expect(asked()).toHaveLength(1)
  })

  it('says what a filter selected beyond the rows drawn (roll 84: 40 → 40 with 63 paged)', async () => {
    const base = LOG.open[0]
    const many = Array.from({ length: 70 }, (_, i) => ({
      ...base,
      id: `m${i}`,
      subject_id: `source.example.feed_${i}`,
      // 50 paged, 20 logged only: the paged set alone overfills one page of 40
      paged_count: i < 50 ? 1 : 0,
      ...(i < 50 ? {} : { paged_severity: undefined }),
    }))
    stub({ '/v3/system/alerts/log': { ...LOG, open: many, paged: [] } })
    render(wrap(<AlertLog />))
    await waitFor(() => expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(40))
    const log = screen.getByTestId('watch-alert-log')
    expect(log).toHaveAttribute('data-shown', '70')
    expect(log).toHaveAttribute('data-shown-paged', '50')
    fireEvent.click(screen.getByTestId('watch-filter-paged-paged'))
    // the drawn rows stay at the page size; the selection is what narrowed
    expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(40)
    expect(log).toHaveAttribute('data-shown', '50')
    expect(log).toHaveAttribute('data-shown-paged', '50')
    expect(screen.getAllByTestId('watch-alert-row').every((r) => r.getAttribute('data-paged') === '1')).toBe(true)
    expect(asked()).toHaveLength(1)
  })

  it('prints "–" until the log answered, and says when it could not be read', async () => {
    stub({ '/v3/system/alerts/log': { measured: false, window_hours: 24, not_measured_reason: 'the alert log could not be read (Example)' } })
    render(wrap(<AlertLog />))
    expect(screen.getByTestId('watch-alert-open')).toHaveTextContent('–')
    await waitFor(() => expect(screen.getByTestId('watch-alert-unmeasured')).toBeInTheDocument())
    expect(screen.getByTestId('watch-alert-open')).toHaveTextContent('–')
  })

  it("a row's subject becomes the point, and Consult's select_alert does the same", async () => {
    stub({ '/v3/system/alerts/log': LOG })
    render(wrap(<AlertLog />))
    await waitFor(() => expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(2))
    fireEvent.click(screen.getAllByTestId('watch-subject-link')[1])
    expect(useSelection.getState().selection).toMatchObject({ kind: 'source', id: 'source.example.harbour_notices' })
    await waitFor(() => expect(screen.getAllByTestId('watch-subject-link')[1]).toHaveAttribute('data-point', '1'))
    act(() => {
      expect(applyPanelAction('view:watch_alerts', 'select_alert', { alert: 'a2' }, { origin: 'case' }).state).toBe('executed')
    })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'target', id: 'example_land' })
    expect(useWatch.getState().focusedAlert).toBe('a2')
    expect(screen.getByTestId('watch-alert-detail')).toHaveTextContent('As raised: Example raised line')
  })

  it('Consult filters it through the contract, refuses what the contract does not take, and Undo puts it back', async () => {
    stub({ '/v3/system/alerts/log': LOG })
    render(wrap(<AlertLog />))
    await waitFor(() => expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(2))
    act(() => {
      expect(applyPanelAction('view:watch_alerts', 'filter_alerts', { severity: 'high' }).state).toBe('executed')
    })
    expect(screen.getAllByTestId('watch-alert-row')).toHaveLength(1)
    expect(watchOnScreen()).toMatchObject({ alerts: 1, filter: expect.stringContaining('severity=high') })
    expect(applyPanelAction('view:watch_alerts', 'filter_alerts', { severity: 'loud' }).state).toBe('refused')
    expect(applyPanelAction('view:watch_alerts', 'set_layer', { layer: 'x', on: true }).state).toBe('refused')
    act(() => {
      undoView()
    })
    expect(useWatch.getState().alertFilter.severity).toBe('any')
  })
})

describe('silences', () => {
  it('shares the health line read and says which side each quiet source is on', async () => {
    stub({ '/v3/system/source-firing': FIRING, '/v3/system/alerts/log': LOG })
    render(wrap(<Silences />))
    await waitFor(() => expect(screen.getAllByTestId('watch-silence-row')).toHaveLength(2))
    const rows = screen.getAllByTestId('watch-silence-row')
    expect(rows[0]).toHaveAttribute('data-side', 'could_not_look')
    expect(rows[0]).toHaveTextContent('Polling fails')
    expect(rows[1]).toHaveTextContent('Nothing new published')
    expect(screen.getByTestId('watch-silences-quiet')).toHaveTextContent('2')
    act(() => {
      applyPanelAction('view:watch_silences', 'filter_silences', { side: 'looked' })
    })
    expect(screen.getAllByTestId('watch-silence-row')).toHaveLength(1)
  })
})

describe('changed after read', () => {
  it('when nothing has been compared yet, it says what will appear and why it is empty — and reads no list', async () => {
    stub({ '/v3/evidence/drift': DRIFT_EMPTY })
    render(wrap(<SourceEdits />))
    await waitFor(() => expect(screen.getByTestId('watch-edits-empty')).toBeInTheDocument())
    expect(screen.getByTestId('watch-edits-empty')).toHaveTextContent(/after the first re-fetch of a cited page/)
    // roll 84: no hard-coded date — the server exposes no re-check schedule
    expect(screen.getByTestId('watch-edits-empty').textContent).not.toMatch(/November|\b20\b/)
    expect(screen.getByTestId('watch-edits-empty')).toHaveTextContent(/never “nothing changed”/)
    expect(screen.getByTestId('watch-edits-open')).toHaveTextContent('0')
    expect(asked()).toEqual(['/v3/evidence/drift?window_hours=168'])
  })

  it('a server without the record says so — not a zero', async () => {
    stub({ '/v3/evidence/drift': { available: false } })
    render(wrap(<SourceEdits />))
    await waitFor(() => expect(screen.getByTestId('watch-edits-unavailable')).toBeInTheDocument())
    expect(screen.getByTestId('watch-edits-open')).toHaveTextContent('–')
  })

  it('lists open flags with both versions and the shadow marked as a shadow', async () => {
    stub({ '/v3/evidence/drift/flags': FLAGS, '/v3/evidence/drift/history': HISTORY, '/v3/evidence/drift': DRIFT_SOME })
    render(wrap(<SourceEdits />))
    await waitFor(() => expect(screen.getAllByTestId('watch-edit-flag')).toHaveLength(1))
    const flag = screen.getByTestId('watch-edit-flag')
    expect(flag).toHaveTextContent('a fact corrected, without saying so')
    expect(flag).toHaveTextContent('the passage relied on changed')
    expect(screen.getByTestId('watch-edit-shadow')).toHaveTextContent('shadow: not closed')
    expect(screen.getByTestId('watch-edits-shadow-count')).toHaveTextContent('the switch is off')
    act(() => {
      applyPanelAction('view:watch_edits', 'open_flag', { flag: '7' })
    })
    await waitFor(() => expect(screen.getByTestId('watch-edit-versions')).toBeInTheDocument())
    expect(screen.getByTestId('watch-edit-passages')).toHaveTextContent('Twelve ships waited.')
    expect(screen.getByTestId('watch-edit-passages')).toHaveTextContent('Nine ships waited.')
    expect(screen.getAllByTestId('watch-edit-versions')[0].querySelectorAll('li')).toHaveLength(2)
    expect(asked()).toEqual([
      '/v3/evidence/drift?window_hours=168',
      '/v3/evidence/drift/flags?state=open&limit=25',
      '/v3/evidence/drift/history?address=https%3A%2F%2Fexample.org%2Fnews%2Fitem-1',
    ])
  })

  it('the edit record is a measure listed by name, read only when its tab is shown', async () => {
    stub({ '/v3/evidence/drift/editors': EDITORS, '/v3/evidence/drift': DRIFT_SOME, '/v3/evidence/drift/flags': FLAGS })
    useWatch.setState({ editsTab: 'record' })
    render(wrap(<SourceEdits />))
    await waitFor(() => expect(screen.getAllByTestId('watch-edit-record-row')).toHaveLength(2))
    const names = screen.getAllByTestId('watch-edit-record-row').map((r) => r.textContent ?? '')
    expect(names[0]).toMatch(/^alpha · daily/)
    expect(screen.getByTestId('watch-edit-record-caption')).toHaveTextContent('not a judgement of the source')
    expect(asked()).not.toContain('/v3/evidence/drift/flags?state=open&limit=25')
  })
})
