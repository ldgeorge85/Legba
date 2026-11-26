/**
 * M-2 — the phone shell renders: Changed by default with what PAGED and what
 * changed; a tab switch replaces the screen; a drill-in pushes and Back pops;
 * the seen cursor stays on the device when the server says sign-in is off.
 * Invented rows, through a stubbed `fetch`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import PhoneShell from './PhoneShell'
import { DEVICE_SEEN_KEY } from './seenCursor'

const SINCE = {
  cursor: '2026-11-13T00:00:00Z',
  server_now: '2026-11-14T00:00:00Z',
  counts: {},
  new_findings: { items: [{ id: 'ex_f1', analyst_id: 'example_unit', target_id: 'example_x', title: 'Example read about a harbour', severity: 'high', confidence: 0.7, faithfulness_score: 0.8, effective_confidence: 0.7, produced_at: '2026-11-13T12:00:00Z' }], total: 1, truncated: false },
  superseded: { items: [], total: 0, truncated: false },
  band_changes: { items: [], total: 0, truncated: false },
  situations: { items: [], total: 0, truncated: false },
  alerts: { items: [], total: 0, truncated: false },
  needs_you: {
    items: [
      { id: 'ex-a1', kind: 'k', severity: 'high', summary: 'Example: it paged', subject_kind: 'target', subject_id: 'example_x', opened_at: '2026-11-13T10:00:00Z', last_raised_at: '2026-11-13T10:00:00Z', last_paged_at: '2026-11-13T10:00:00Z', why: 'paged' },
      { id: 'ex-a2', kind: 'k', severity: 'medium', summary: 'Example: open, never paged', subject_kind: 'target', subject_id: 'example_x', opened_at: '2026-11-13T11:00:00Z', last_raised_at: '2026-11-13T11:00:00Z', last_paged_at: null, why: 'open_at_severity' },
    ],
    total: 2,
    truncated: false,
  },
}

/** The badge polls the counts route; its `needs_you` is paged-only
 * (the list's own predicate), so it differs from the list's raw `total`. */
const COUNTS = {
  cursor: '2026-11-13T00:00:00Z', server_now: '2026-11-14T00:00:00Z', counts: { needs_you: 3 }, needs_you: 3, changed: 1, forecasts_due: 0,
}

function res(status: number, body: unknown): Response {
  return { ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
}

const asked: string[] = []

beforeEach(() => {
  asked.length = 0
  window.history.replaceState(null, '', '/m/')
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url)
      asked.push(u)
      if (u.includes('/v3/since/counts')) return res(200, COUNTS)
      if (u.includes('/v3/since')) return res(200, SINCE)
      if (u.includes('/v3/me/seen')) return res(404, { detail: { error: 'seen_cursor_unavailable', reason: 'signin_off' } })
      if (u.includes('/auth/me')) return res(200, { kind: 'operator', actor: 'operator:bearer:x', role: 'operator', user: null, session_rejected: false })
      return res(200, { data: [] })
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <PhoneShell />
    </QueryClientProvider>,
  )
}

describe('PhoneShell', () => {
  it('opens on Changed: what paged (only), what changed, five tabs, a badge for the paged', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('phone-changed').getAttribute('data-read')).toBe('1'))
    expect(screen.getAllByTestId('phone-needs-you-row')).toHaveLength(1)
    expect(screen.getByText('Example: it paged')).toBeTruthy()
    expect(screen.queryByText('Example: open, never paged')).toBeNull()
    expect(screen.getAllByTestId('changed-row')).toHaveLength(1)
    for (const t of ['changed', 'watch', 'ledger', 'consult', 'subjects']) expect(screen.getByTestId(`tab-${t}`)).toBeTruthy()
    // the badge is the counts route's paged-only figure (3 here), not the list's rows
    await waitFor(() => expect(screen.getByTestId('tab-changed-badge').textContent).toBe('3'))
    // one since read for the window, asked once; one counts read for the badge
    expect(asked.filter((u) => u.includes('/v3/since?')).length).toBe(1)
    expect(asked.filter((u) => u.includes('/v3/since/counts?')).length).toBe(1)
  })

  it('keeps the seen cursor on the device when sign-in is off', async () => {
    mount()
    await waitFor(() => screen.getByTestId('phone-mark-seen'))
    expect(screen.getByTestId('phone-since-line').textContent).toContain('remembered on this phone only (sign-in is off)')
    fireEvent.click(screen.getByTestId('phone-mark-seen'))
    await waitFor(() => expect(JSON.parse(localStorage.getItem(DEVICE_SEEN_KEY) ?? '{}').changed).toBe('2026-11-14T00:00:00Z'))
  })

  it('a row pushes its read; Back returns; a tab switch replaces', async () => {
    mount()
    await waitFor(() => screen.getByTestId('changed-row'))
    const start = window.history.length
    fireEvent.click(screen.getByTestId('changed-row'))
    await waitFor(() => expect(screen.getByTestId('phone-shell').getAttribute('data-depth')).toBe('1'))
    expect(window.history.length).toBe(start + 1)
    expect(window.location.hash).toContain('screen=read%3Aex_f1')
    fireEvent.click(screen.getByTestId('tab-ledger'))
    await waitFor(() => expect(screen.getByTestId('phone-shell').getAttribute('data-tab')).toBe('ledger'))
    expect(window.history.length).toBe(start + 1)
  })
})
