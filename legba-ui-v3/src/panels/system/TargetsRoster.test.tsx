/**
 * Component test for the TargetsRosterPanel landing surface.
 *
 * Asserts the registry list endpoint is called, rows render, and the
 * client-side filter narrows the row set.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetsRosterPanel from './TargetsRoster'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'd1',
    panel_id: 'system_targets_roster',
    descriptor_id: '(singleton)',
    descriptor_version: 'v0',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Roster',
    mode: 'personal',
    layout_slot: 'system.targets.roster',
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

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('TargetsRosterPanel', () => {
  it('renders rows from the registry list endpoint and supports filtering', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => [
          {
            descriptor_id: 'brazil',
            version: 'v' + 'a'.repeat(63),
            state: 'active',
            owner: 'lewis',
            name: 'Brazil',
            family: 'target',
            abstraction_level: null,
            kind: null,
            body: {},
          },
          {
            descriptor_id: 'nigeria',
            version: 'v' + 'b'.repeat(63),
            state: 'paused',
            owner: 'lewis',
            name: 'Nigeria',
            family: 'target',
            abstraction_level: null,
            kind: null,
            body: {},
          },
        ],
      }),
    )
    render(wrap(<TargetsRosterPanel registration={reg()} scope={{}} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByText('brazil')).toBeInTheDocument()
      expect(screen.getByText('nigeria')).toBeInTheDocument()
    })

    // Filter by typing in the query box.
    fireEvent.change(screen.getByTestId('roster-query'), { target: { value: 'bra' } })
    const rows = screen.getByTestId('roster-rows')
    await waitFor(() => {
      expect(within(rows).queryByText('nigeria')).toBeNull()
    })
    expect(within(rows).getByText('brazil')).toBeInTheDocument()
  })
})
