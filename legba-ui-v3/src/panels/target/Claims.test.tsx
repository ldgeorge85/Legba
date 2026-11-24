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

  it('hides engine tag facets, labels topics in words and offers one severity scale (U-1)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          data: [
            finding({
              id: 'f1',
              severity: 'medium',
              data: {
                corroboration_sources: 2,
                tags: ['severity_delta:steady', 'severity:moderate', 'meta', 'spine:x_composition', 'topic:trade_pressure'],
              },
            }),
            finding({ id: 'f2', title: 'Second claim', severity: 'moderate', data: { tags: ['topic:trade_pressure'] } }),
          ],
          next_cursor: null,
        }),
      })),
    )
    render(wrap(<TargetClaimsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-claims-tag-chips')).toBeInTheDocument()
    })
    const chips = screen.getByTestId('target-claims-tag-chips').textContent ?? ''
    expect(chips).toMatch(/Trade pressure/)
    expect(chips).not.toMatch(/severity_delta|severity:|spine|meta|topic:/)
    // The facet list is folded behind a disclosure, closed until a topic is picked.
    expect(screen.getByTestId('target-claims-topics').hasAttribute('open')).toBe(false)
    // Per-claim tag chips (on the expanded claim) speak the same words.
    fireEvent.click(screen.getByText('Troops massing near the border'))
    expect(screen.getByTestId('target-claim-tags-f1').textContent).toBe('Trade pressure')

    const opts = Array.from(
      (screen.getByTestId('target-claims-severity') as HTMLSelectElement).options,
    ).map((o) => o.value)
    expect(opts).toContain('moderate')
    expect(opts).not.toContain('medium')
    expect(opts.filter((o) => o === 'moderate')).toHaveLength(1)
    // `medium` and `moderate` are one rung: filtering on it keeps both claims.
    fireEvent.change(screen.getByTestId('target-claims-severity'), { target: { value: 'moderate' } })
    await waitFor(() => {
      expect(screen.getByText('Second claim')).toBeInTheDocument()
      expect(screen.getByText('Troops massing near the border')).toBeInTheDocument()
    })
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
