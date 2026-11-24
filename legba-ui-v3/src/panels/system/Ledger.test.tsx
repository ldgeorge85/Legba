/**
 * Component test for the forecast Ledger (ST0-C): rows from the mocked route,
 * open first by due date, an absent arm shown as `absent`, the Brier
 * placeholder column, and the unreadable-ledger notice.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import LedgerPanel from './Ledger'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_ledgers',
    descriptor_id: 'ledgers',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Ledgers',
    mode: 'personal',
    layout_slot: 'system.ledgers.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-11-10T00:00:00Z',
    retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const BODY = {
  version: '2026-11/st0',
  available: true,
  forecasts: [
    {
      id: '2',
      slug: 'FX2-late',
      question: 'Fictional late question',
      resolver_kind: 'series_threshold',
      machine_resolvable: true,
      due_at: '2027-01-03T23:59:59+00:00',
      status: 'open',
      numbers: [],
    },
    {
      id: '1',
      slug: 'FX1-soon',
      question: 'Fictional soon question',
      resolver_kind: 'official_result',
      due_at: '2026-11-29T23:59:59+00:00',
      status: 'open',
      sealed_at: '2026-11-11T00:00:00+00:00',
      numbers: [
        { issuer_kind: 'operator', issuer_ref: 'operator', p: 0.3, sealed: true },
        { issuer_kind: 'tower', issuer_ref: 'tower', p: null, absent_reason: 'no analyst number', sealed: true },
      ],
    },
  ],
}

function stubFetch(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('LedgerPanel', () => {
  it('renders the ledger by what resolves next with every arm', async () => {
    const fetchMock = stubFetch(BODY)
    render(wrap(<LedgerPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('ledger-row-FX1-soon')).toBeInTheDocument())
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v3/ledger/forecasts')
    const rows = within(screen.getByTestId('ledger-table')).getAllByTestId(/^ledger-row-/)
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual(['ledger-row-FX1-soon', 'ledger-row-FX2-late'])
    expect(screen.getByTestId('ledger-arm-FX1-soon-operator')).toHaveTextContent('0.30')
    expect(screen.getByTestId('ledger-arm-FX1-soon-tower')).toHaveTextContent('absent')
    expect(screen.getByTestId('ledger-arm-FX1-soon-tower')).toHaveAttribute('title', 'no analyst number')
    expect(screen.getByTestId('ledger-state-FX1-soon')).toHaveTextContent('sealed')
    expect(screen.getByText('Brier (from resolution)')).toBeInTheDocument()
  })

  it('an unreadable ledger is said, not shown as empty', async () => {
    stubFetch({ version: '2026-11/st0', available: false, forecasts: [] })
    render(wrap(<LedgerPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('ledger-unavailable')).toBeInTheDocument())
    expect(screen.queryByTestId('ledger-empty')).toBeNull()
  })
})
