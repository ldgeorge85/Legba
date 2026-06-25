/**
 * Component test for the Backfill / Catch-up Replay panel.
 *
 * The backfill TRIGGER is intentionally disabled in this build: the
 * registry-side POST /targets/{id}/backfill is an honest 501 (the P-12
 * catch-up replay is a runtime-plane operation, not reachable through the
 * registry API). The panel surfaces a clear "backend not exposed" state rather
 * than a button that always errors (FEATURE_COMPLETE_PLAN api-ui-surface item
 * 5b — ship the disabled affordance, gate the cross-plane trigger). These
 * tests assert that contract: the run button stays disabled and never POSTs.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import BackfillPanel from './Backfill'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'b1',
    panel_id: 'system_backfill',
    descriptor_id: 'system.backfill',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Backfill Replay',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-06-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const TARGETS = [{ descriptor_id: 'target.brazil', name: 'Brazil', state: 'active' }]

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

function mockFetch() {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/registry/descriptors'))
      return Promise.resolve({ ok: true, json: async () => TARGETS })
    return Promise.resolve({ ok: true, json: async () => ({}) })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function pickTarget(value: string) {
  const select = screen.getByTestId('backfill-target')
  await waitFor(() => expect(select.querySelector(`option[value="${value}"]`)).toBeInTheDocument())
  fireEvent.change(select, { target: { value } })
}

describe('BackfillPanel', () => {
  it('renders the backend-not-exposed disabled state', () => {
    mockFetch()
    render(wrap(<BackfillPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('backfill-run')).toBeDisabled()
    expect(screen.getByTestId('backfill-disabled-note')).toBeInTheDocument()
  })

  it('keeps the run button disabled even after picking a target', async () => {
    mockFetch()
    render(wrap(<BackfillPanel registration={reg()} scope={{}} mode="personal" />))
    await pickTarget('target.brazil')
    // The trigger is gated off (501 backend); picking a target does not arm it.
    expect(screen.getByTestId('backfill-run')).toBeDisabled()
  })

  it('never POSTs to the backfill endpoint', async () => {
    const fetchMock = mockFetch()
    render(wrap(<BackfillPanel registration={reg()} scope={{}} mode="personal" />))
    await pickTarget('target.brazil')
    fireEvent.click(screen.getByTestId('backfill-run'))
    // Disabled button: the click is inert — no POST is issued.
    const posted = fetchMock.mock.calls.some(
      (c) => typeof c[0] === 'string' && c[0].includes('/backfill'),
    )
    expect(posted).toBe(false)
  })
})
