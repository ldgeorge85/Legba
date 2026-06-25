/**
 * Component test for the UI-6 Alert Center panel.
 *
 * The center now drives off the FINDINGS POLL (not a WS tail). `fetch` is
 * stubbed; the canned `/findings` page is mutable so the test can simulate a
 * second poll that introduces a NEW finding. Asserts:
 *   - a subscription can be added + listed + persisted, and deleted,
 *   - the FIRST poll seeds without firing (no alerts for pre-existing findings),
 *   - a NEW finding on a later poll fires + is flagged matched when it satisfies
 *     a subscription, and unmatched-but-new findings still appear (dimmed).
 *
 * Polls are advanced deterministically via the chrome refresh button.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import AlertCenterPanel from './AlertCenter'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_alert_center', descriptor_id: 'alerts', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Alert Center', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

interface F {
  id: string
  title: string
  severity: string | null
  target_id: string | null
  analyst_id?: string | null
  produced_at: string
}

// Mutable findings page — tests push new rows then refetch to simulate a poll.
let findingsPage: F[] = []

// Registered scopes the ScopePicker offers — the operator now PICKS a scope from
// the live registry rather than free-typing it, so these ids must exist as
// options for the select to accept them.
const SCOPE_ROWS = [
  { descriptor_id: 'brazil', name: 'Brazil', state: 'active' },
  { descriptor_id: 'iran', name: 'Iran', state: 'active' },
  { descriptor_id: 'x', name: 'X', state: 'active' },
]

function stubFetch() {
  const mock = vi.fn((url: string) => {
    const u = String(url)
    let body: unknown = []
    if (u.includes('/findings')) body = { data: findingsPage }
    else if (u.includes('/registry/descriptors')) body = SCOPE_ROWS
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

async function repoll() {
  // The chrome refresh button calls the query's refetch (a fresh poll).
  fireEvent.click(screen.getByTestId('panel-refresh'))
}

/** Pick a scope from the (async-loaded) ScopePicker select by its descriptor id.
 *  Waits for the registry options to land before selecting, then asserts the
 *  controlled value took. */
async function pickScope(value: string) {
  const sel = (await screen.findByTestId('alert-scope-id')) as HTMLSelectElement
  await waitFor(() =>
    expect([...sel.options].some((o) => o.value === value)).toBe(true),
  )
  fireEvent.change(sel, { target: { value } })
  await waitFor(() => expect(sel.value).toBe(value))
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  findingsPage = []
})

describe('AlertCenterPanel', () => {
  it('adds and persists a subscription', async () => {
    findingsPage = []
    stubFetch()
    render(wrap(<AlertCenterPanel registration={reg()} scope={{}} mode="personal" />))
    await pickScope('brazil')
    fireEvent.click(screen.getByTestId('alert-add-sub'))

    await waitFor(() => expect(screen.getByTestId('alert-sub-target:brazil')).toBeInTheDocument())
    const stored = JSON.parse(localStorage.getItem('legba.alerts.subscriptions') ?? '[]')
    expect(stored[0].scope_id).toBe('brazil')
  })

  it('first poll seeds without firing; a new matching finding fires + is flagged', async () => {
    // Pre-existing finding present on the first (seed) poll.
    findingsPage = [
      { id: 'pre1', title: 'Old finding', severity: 'high', target_id: 'brazil', produced_at: '2026-06-03T00:00:00Z' },
    ]
    stubFetch()
    render(wrap(<AlertCenterPanel registration={reg()} scope={{}} mode="personal" />))

    // subscribe to brazil at floor 'any' so unscored/scored alike match.
    await pickScope('brazil')
    fireEvent.click(screen.getByTestId('alert-add-sub'))
    await waitFor(() => expect(screen.getByTestId('alert-sub-target:brazil')).toBeInTheDocument())

    // Seed pass must not fire on the pre-existing finding.
    await waitFor(() => expect(screen.getByTestId('alert-fired')).toBeInTheDocument())
    expect(screen.queryByTestId('alert-fired-pre1')).not.toBeInTheDocument()

    // A NEW finding appears on the next poll → fires and is flagged matched.
    findingsPage = [
      { id: 'new1', title: 'Coup fired', severity: 'critical', target_id: 'brazil', produced_at: '2026-06-03T01:00:00Z' },
      ...findingsPage,
    ]
    await repoll()

    await waitFor(() => expect(screen.getByTestId('alert-fired-new1')).toBeInTheDocument())
    expect(screen.getByTestId('alert-fired-matched-new1')).toBeInTheDocument()
    // The seed finding never fires.
    expect(screen.queryByTestId('alert-fired-pre1')).not.toBeInTheDocument()
  })

  it('an unmatched new finding still appears (no match flag)', async () => {
    // Mirror the matched test's structure (a subscription + seed pass) so the
    // seed poll settles before the next poll — but subscribe to 'brazil' while
    // the new finding targets 'iran', so it fires UNMATCHED.
    findingsPage = [
      { id: 'seed0', title: 'pre-existing', severity: 'low', target_id: 'x', produced_at: '2026-06-03T00:00:00Z' },
    ]
    stubFetch()
    render(wrap(<AlertCenterPanel registration={reg()} scope={{}} mode="personal" />))
    await pickScope('brazil')
    fireEvent.click(screen.getByTestId('alert-add-sub'))
    await waitFor(() => expect(screen.getByTestId('alert-sub-target:brazil')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('alert-fired')).toBeInTheDocument())
    expect(screen.queryByTestId('alert-fired-seed0')).not.toBeInTheDocument()

    findingsPage = [
      { id: 'iranf', title: 'Iran finding', severity: 'high', target_id: 'iran', produced_at: '2026-06-03T02:00:00Z' },
      ...findingsPage,
    ]
    await repoll()

    await waitFor(() => expect(screen.getByTestId('alert-fired-iranf')).toBeInTheDocument())
    expect(screen.queryByTestId('alert-fired-matched-iranf')).not.toBeInTheDocument()
  })

  it('delete removes the subscription', async () => {
    findingsPage = []
    stubFetch()
    render(wrap(<AlertCenterPanel registration={reg()} scope={{}} mode="personal" />))
    await pickScope('brazil')
    fireEvent.click(screen.getByTestId('alert-add-sub'))
    await waitFor(() => expect(screen.getByTestId('alert-sub-target:brazil')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('alert-sub-del-target:brazil'))
    await waitFor(() => expect(screen.queryByTestId('alert-sub-target:brazil')).not.toBeInTheDocument())
  })
})
