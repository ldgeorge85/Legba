/**
 * Component test for the UI-6 Tenant View panel.
 *
 * Tenancy keys on the descriptor **`owner`** (NO owner_tenant column). `fetch`
 * is stubbed for the per-family head-descriptor reads. Asserts: owners are
 * discovered across families, selecting one scopes the drill-in roster +
 * per-family roll-up, and selecting broadcasts `legba:set-tenant` with `owner`.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import TenantViewPanel from './TenantView'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_tenant_view', descriptor_id: 'tenant', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Tenant View', mode: 'cis',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function stubFetch() {
  const mock = vi.fn((url: string) => {
    const u = String(url)
    let body: unknown = []
    if (u.includes('family=target'))
      body = [
        { descriptor_id: 'country.br', name: 'Brazil', family: 'target', state: 'active', owner: 'acme' },
        { descriptor_id: 'country.ir', name: 'Iran', family: 'target', state: 'paused', owner: 'acme' },
        { descriptor_id: 'country.cn', name: 'China', family: 'target', state: 'active', owner: 'globex' },
      ]
    else if (u.includes('family=source'))
      body = [
        { descriptor_id: 'source.dw', name: 'DW', family: 'source', state: 'active', owner: 'acme' },
        // owner only present at body.identity.owner — must still resolve.
        { descriptor_id: 'source.bbc', name: 'BBC', family: 'source', state: 'active', body: { identity: { owner: 'globex' } } },
      ]
    else if (u.includes('family=analyst'))
      body = [{ descriptor_id: 'analyst.geo', name: 'Geo', family: 'analyst', state: 'active', owner: 'acme' }]
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('TenantViewPanel', () => {
  it('discovers owners across all families', async () => {
    stubFetch()
    render(wrap(<TenantViewPanel registration={reg()} scope={{}} mode="cis" />))
    await waitFor(() => expect(screen.getByTestId('tenant-pick-acme')).toBeInTheDocument())
    // globex owner is resolved from body.identity.owner on the BBC source + the CN target.
    expect(screen.getByTestId('tenant-pick-globex')).toBeInTheDocument()
  })

  it('selecting an owner scopes the roster and broadcasts owner', async () => {
    stubFetch()
    const listener = vi.fn()
    window.addEventListener('legba:set-tenant', listener as EventListener)

    render(wrap(<TenantViewPanel registration={reg()} scope={{}} mode="cis" />))
    await waitFor(() => expect(screen.getByTestId('tenant-pick-globex')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('tenant-pick-globex'))

    await waitFor(() => {
      expect(screen.getByTestId('tenant-descriptor-country.cn')).toBeInTheDocument()
    })
    // acme descriptors must be scoped out
    expect(screen.queryByTestId('tenant-descriptor-country.br')).not.toBeInTheDocument()
    expect(screen.getByTestId('tenant-descriptor-source.bbc')).toBeInTheDocument()
    // broadcast fired with the chosen owner
    expect(listener).toHaveBeenCalled()
    const ev = listener.mock.calls.at(-1)![0] as CustomEvent
    expect(ev.detail.owner).toBe('globex')

    window.removeEventListener('legba:set-tenant', listener as EventListener)
  })

  it('rolls up per family + by state for the active owner', async () => {
    stubFetch()
    render(wrap(<TenantViewPanel registration={reg()} scope={{}} mode="cis" />))
    await waitFor(() => expect(screen.getByTestId('tenant-pick-acme')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('tenant-pick-acme'))
    await waitFor(() => {
      // acme: 2 targets, 1 source, 1 analyst
      expect(screen.getByTestId('tenant-family-count-target')).toHaveTextContent('2')
      expect(screen.getByTestId('tenant-family-count-source')).toHaveTextContent('1')
      expect(screen.getByTestId('tenant-family-count-analyst')).toHaveTextContent('1')
      // target by-state: 1 active, 1 paused
      expect(screen.getByTestId('tenant-target-state-active')).toHaveTextContent('1')
      expect(screen.getByTestId('tenant-target-state-paused')).toHaveTextContent('1')
    })
  })
})
