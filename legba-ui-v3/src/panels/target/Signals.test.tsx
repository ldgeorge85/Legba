/**
 * Component test for the UI-3 Target Signals panel (rebuilt against the
 * frozen `/signals` page shape — target-agnostic rows scoped by geo).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetSignalsPanel from './Signals'
import type { PanelRegistration } from '@/types'
import { mockErrorResponse } from '@/test/apiMocks'

function reg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_signals',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Signals',
    mode: 'personal',
    layout_slot: 'target.signals.main',
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

function signal(over: Record<string, unknown> = {}) {
  return {
    id: 'sig1',
    title: 'Saudi energy minister visits Russia',
    source_id: 'source.aljazeera.world',
    source_url: 'https://aljazeera.com/x',
    language: 'en',
    produced_at: '2026-06-04T18:30:00Z',
    geo: ['RU'],
    tags: ['news'],
    entity_classes: ['person', 'location'],
    derived_from: [],
    data: { geo: { lat: 64.6, lon: 97.7, country: 'Russia', country_iso2: 'RU' } },
    ...over,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('TargetSignalsPanel', () => {
  it('shows the empty state when there are no signals', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [], next_cursor: null }) })),
    )
    render(wrap(<TargetSignalsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-signals-empty')).toBeInTheDocument()
    })
  })

  it('renders a signal row with the geocoded country and expands to entity/tag chips + source link', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [signal()], next_cursor: null }) })),
    )
    render(wrap(<TargetSignalsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByText('Saudi energy minister visits Russia')).toBeInTheDocument()
    })
    // Geocoded country chip from data.geo.country.
    expect(screen.getByText('Russia')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('target-signal-row-sig1'))
    await waitFor(() => {
      expect(screen.getByTestId('target-signal-out-sig1')).toBeInTheDocument()
    })
    expect(screen.getByText('person')).toBeInTheDocument()
    expect(screen.getByText('news')).toBeInTheDocument()
  })

  it('applies the client-side free-text filter', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          data: [signal(), signal({ id: 'sig2', title: 'BBC reports on trade deal' })],
          next_cursor: null,
        }),
      })),
    )
    render(wrap(<TargetSignalsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByText('Saudi energy minister visits Russia')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByTestId('target-signals-text'), { target: { value: 'trade' } })
    await waitFor(() => {
      expect(screen.queryByText('Saudi energy minister visits Russia')).not.toBeInTheDocument()
    })
    expect(screen.getByText('BBC reports on trade deal')).toBeInTheDocument()
  })

  it('degrades to the empty state on a 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockErrorResponse(404, { detail: 'nope' })),
    )
    render(wrap(<TargetSignalsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-signals-empty')).toBeInTheDocument()
    })
  })
})
