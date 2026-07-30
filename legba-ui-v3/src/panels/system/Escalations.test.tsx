/**
 * Component test for the Escalation Deliveries panel (`system.escalations`) —
 * the human-visible alert edge (audit finding C3 / decision D1).
 *
 * `fetch` is stubbed to serve a canned `GET /api/v1/v3/system/escalations`
 * payload. Asserts the whole point of the panel:
 *   - a non-delivery window fires the LOUD red banner + surfaces the
 *     per-(sink,status) breakdown, and a `failed` row shows its delivery error,
 *   - a clean/empty window renders the quiet clear banner + an HONEST empty
 *     state (no fabricated rows).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import type { EscalationDeliveriesResponse } from '@/lib/api'
import EscalationsPanel from './Escalations'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_escalations', descriptor_id: 'alerts', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Escalation Deliveries', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

// Mutable canned response the fetch stub returns for /v3/system/escalations.
let escalationsBody: EscalationDeliveriesResponse

function stubFetch() {
  const mock = vi.fn((url: string) => {
    const u = String(url)
    const body: unknown = u.includes('/v3/system/escalations') ? escalationsBody : []
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

const EMPTY: EscalationDeliveriesResponse = {
  summary: {
    window_hours: 24, total: 0, delivered: 0, failed: 0, logged_only: 0,
    retrying: 0, other: 0, non_delivery: 0, by_sink_status: [],
  },
  rows: [],
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  escalationsBody = EMPTY
})

describe('EscalationsPanel', () => {
  it('fires the LOUD banner + shows the delivery error on a non-delivery window', async () => {
    escalationsBody = {
      summary: {
        window_hours: 24, total: 553, delivered: 1, failed: 552, logged_only: 0,
        retrying: 0, other: 0, non_delivery: 552,
        by_sink_status: [
          { sink_kind: 'pushover', status: 'failed', n: 552, sample_error: 'pushover 552: monthly limit' },
        ],
      },
      rows: [
        {
          id: 'd1', alert_row_id: 'f1', channel_name: 'pushover', sink_kind: 'alert',
          sink_target: 'channels.escalations', target_id: 'us', severity: 'high',
          effective_confidence: 0.91, status: 'failed',
          error_message: 'pushover 552: monthly limit reached',
          attempt_number: 1, attempted_at: '2026-07-08T00:00:00Z', delivered_at: null,
          payload_summary: { title: 'Coup-risk spike', action: 'escalate' },
        },
      ],
    }
    stubFetch()
    render(wrap(<EscalationsPanel registration={reg()} scope={{}} mode="personal" />))

    // The LOUD red banner + its per-(sink,status) breakdown chip.
    await waitFor(() => expect(screen.getByTestId('escalations-banner-alarm')).toBeInTheDocument())
    expect(screen.getByTestId('escalations-nd-pushover-failed')).toBeInTheDocument()

    // The failed delivery row surfaces its error (this is the whole point).
    expect(screen.getByTestId('escalations-row-d1')).toBeInTheDocument()
    const err = screen.getByTestId('escalations-row-error-d1')
    expect(err.textContent).toContain('pushover 552')
  })

  it('renders the clear banner + an HONEST empty state when nothing escalated', async () => {
    escalationsBody = EMPTY
    stubFetch()
    render(wrap(<EscalationsPanel registration={reg()} scope={{}} mode="personal" />))

    // The health banner falls back to a local EMPTY summary before the query
    // resolves, so it reads "clear" on the very first (pre-fetch) render too
    // — asserting on it first made `waitFor` return trivially without ever
    // waiting on the actual fetch, so the very next (synchronous) assertion
    // below raced the still-pending query and failed intermittently. Wait on
    // the empty-state row instead: it only appears once `isLoading` flips
    // false, so it's the one honest post-load signal.
    await waitFor(() => expect(screen.getByTestId('escalations-empty')).toBeInTheDocument())
    expect(screen.getByTestId('escalations-banner-clear')).toBeInTheDocument()
    // No fabricated rows / no alarm.
    expect(screen.queryByTestId('escalations-banner-alarm')).not.toBeInTheDocument()
  })
})
