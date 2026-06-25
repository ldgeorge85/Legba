/**
 * Component test for the UI-5 Audit-Chain Browser.
 *
 * Asserts (acceptance: audit-chain shows verify status):
 *  - renders entries from the mocked `/registry/audit` page
 *  - each entry shows its inline Ed25519 verify status (verified/failed/unverifiable)
 *  - a FAILED entry flips the chain-health banner to "tamper detected"
 *  - family filter re-queries
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import AuditChainPanel from './AuditChain'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_audit',
    descriptor_id: 'audit.browser',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Audit-Chain Browser',
    mode: 'personal',
    layout_slot: 'system.audit.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const PAGE = [
  {
    id: 'a1',
    occurred_at: '2026-06-03T00:00:00Z',
    actor_id: 'op',
    actor_role: 'operator',
    namespace: 'target',
    descriptor_id: 'brazil',
    action: 'register',
    from_version: null,
    to_version: 'v1',
    change_summary: { note: 'init' },
    signer_did: 'did:key:z6Mk',
    signature_verified: true,
  },
  {
    id: 'a2',
    occurred_at: '2026-06-03T01:00:00Z',
    actor_id: 'op',
    actor_role: 'operator',
    namespace: 'target',
    descriptor_id: 'brazil',
    action: 'update',
    from_version: 'v1',
    to_version: 'v2',
    change_summary: {},
    signer_did: 'did:key:z6Mk',
    signature_verified: false,
  },
]

function stubFetch(rows: unknown = PAGE) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => rows })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('AuditChainPanel', () => {
  it('renders entries with their inline verify status', async () => {
    stubFetch()
    render(wrap(<AuditChainPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('audit-row-a1')).toBeInTheDocument())
    expect(screen.getByTestId('audit-verify-verified')).toBeInTheDocument()
    expect(screen.getByTestId('audit-verify-failed')).toBeInTheDocument()
  })

  it('a failed verify flips the chain-health banner to tamper detected', async () => {
    stubFetch()
    render(wrap(<AuditChainPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('audit-health')).toBeInTheDocument())
    expect(screen.getByTestId('audit-health-failed')).toHaveTextContent('1 failed')
    expect(screen.getByText(/chain tamper detected/)).toBeInTheDocument()
  })

  it('intact chain when all verified', async () => {
    stubFetch([PAGE[0]])
    render(wrap(<AuditChainPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByText(/chain intact/)).toBeInTheDocument())
  })

  it('family filter re-queries the endpoint', async () => {
    const fetchMock = stubFetch()
    render(wrap(<AuditChainPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('audit-row-a1')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('audit-family-filter'), { target: { value: 'analyst' } })
    await waitFor(() => {
      const calls = fetchMock.mock.calls.map((c) => String(c[0]))
      expect(calls.some((u) => u.includes('family=analyst'))).toBe(true)
    })
  })
})
