/**
 * Component test for the UI-3 Target Claims (ex-Facts, rebuilt against the
 * frozen `/findings` page shape with corroboration fields in `data`).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetClaimsPanel from './Claims'
import type { PanelRegistration } from '@/types'
import { mockErrorResponse } from '@/test/apiMocks'

function reg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_claims',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Claims',
    mode: 'personal',
    layout_slot: 'target.claims.main',
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

function finding(over: Record<string, unknown> = {}) {
  return {
    id: 'f1',
    kind: 'finding',
    title: 'Troops massing near the border',
    body: 'Multiple independent sources report movement.',
    confidence: 0.72,
    severity: 'high',
    data: { corroboration_score: 0.6, corroboration_sources: 3 },
    target_id: 'brazil',
    analyst_id: 'inline.brazil',
    produced_at: '2026-06-02T00:00:00Z',
    derived_from: ['s1', 's2'],
    ...over,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('TargetClaimsPanel', () => {
  it('shows the empty state when there are no claims', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [], next_cursor: null }) })),
    )
    render(wrap(<TargetClaimsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-claims-empty')).toBeInTheDocument()
    })
  })

  it('renders a claim with confidence + corroboration and expands to the evidence chain', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [finding()], next_cursor: null }) })),
    )
    render(wrap(<TargetClaimsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByText('Troops massing near the border')).toBeInTheDocument()
    })
    // Corroboration source count surfaced in the row.
    expect(screen.getByTestId('target-claim-corrob-f1')).toHaveTextContent('×3')

    fireEvent.click(screen.getByText('Troops massing near the border'))
    await waitFor(() => {
      expect(screen.getByText(/evidence chain \(2\)/)).toBeInTheDocument()
    })
    // The corroboration confidence bar renders (score was 0.6).
    expect(screen.getByTestId('target-claim-corrob-bar-f1')).toBeInTheDocument()
  })

  it('facets by topic tag (dropping bookkeeping tags) and flags uncorroborated claims', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          data: [
            finding({
              id: 'f1',
              title: 'Troops massing near the border',
              data: {
                corroboration_sources: 3,
                tags: ['conflict', 'border', 'target:brazil', 'analyst:x'],
              },
            }),
            finding({
              id: 'f2',
              title: 'Single-source rumor of a coup',
              data: { corroboration_sources: 1, tags: ['politics'] },
            }),
          ],
          next_cursor: null,
        }),
      })),
    )
    render(wrap(<TargetClaimsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByTestId('target-claims-tag-chips')).toBeInTheDocument()
    })
    // Topic tags surfaced as facet chips; bookkeeping tags dropped.
    expect(screen.getByTestId('target-claims-tag-conflict')).toBeInTheDocument()
    expect(screen.queryByTestId('target-claims-tag-target:brazil')).not.toBeInTheDocument()

    // The single-source claim is flagged uncorroborated; the 3-source one isn't.
    expect(screen.getByTestId('target-claim-uncorrob-f2')).toBeInTheDocument()
    expect(screen.queryByTestId('target-claim-uncorrob-f1')).not.toBeInTheDocument()

    // Clicking a tag chip filters the list to matching claims.
    fireEvent.click(screen.getByTestId('target-claims-tag-politics'))
    await waitFor(() => {
      expect(screen.queryByText('Troops massing near the border')).not.toBeInTheDocument()
    })
    expect(screen.getByText('Single-source rumor of a coup')).toBeInTheDocument()
  })

  it('degrades to the empty state on a 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockErrorResponse(404, { detail: 'nope' })),
    )
    render(wrap(<TargetClaimsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-claims-empty')).toBeInTheDocument()
    })
  })
})
