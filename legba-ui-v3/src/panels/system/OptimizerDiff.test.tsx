/**
 * Component test for the UI-5 Optimizer Prompt-Module Diff.
 *
 * Asserts:
 *  - empty state when no candidate is selected
 *  - an `open_panel` carrying `candidate_id` (the retired event's successor)
 *    loads + renders a diff, whether it lands before or after the mount
 *  - added / deleted lines are marked, and the +/- stat shows in the subtitle
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import OptimizerDiffPanel from './OptimizerDiff'
import OptimizerMerged from '@/panels/merged/Optimizer'
import { drive } from '@/lib/applyAction'
import { bridgeActions } from '@/lib/commands'
import { resetPanelParams } from '@/state/panelParams'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_optimizer_diff',
    descriptor_id: 'optimizer.diff',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Prompt-Module Diff',
    mode: 'personal',
    layout_slot: 'system.optimizer.diff.main',
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

const DIFF = {
  candidate_id: 'cand-1',
  analyst_id: 'cred.analyst',
  current_module_path: 'modules/cred/v1.txt',
  candidate_module_path: 'modules/cred/cand-1.txt',
  current_text: 'line one\nshared\nold tail',
  candidate_text: 'line ONE\nshared\nnew tail\nextra',
  eval_score: 0.82,
  eval_score_delta: 0.05,
}

function stubFetch() {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => DIFF })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  resetPanelParams()
})

describe('OptimizerDiffPanel', () => {
  it('shows the empty state with no candidate selected', () => {
    stubFetch()
    render(wrap(<OptimizerDiffPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('optdiff-empty')).toBeInTheDocument()
  })

  // UI-B1/merges: the diff is the Optimizer's Diff tab. `open_panel` on the
  // retired id resolves onto `system.optimizer`, so the candidate reaches this
  // (unmodified) component THROUGH the wrapper's relay — the binding path these
  // three tests now traverse.
  it('loads + renders a diff from an open_panel while mounted', async () => {
    stubFetch()
    render(wrap(<OptimizerMerged registration={reg()} scope={{}} mode="personal" initialTab="diff" />))
    expect(screen.getByTestId('optdiff-empty')).toBeInTheDocument()
    act(() => {
      drive(bridgeActions.optimizerDiff('cand-1'))
    })
    await waitFor(() => expect(screen.getByTestId('optdiff-body')).toBeInTheDocument())
    // 'old tail' deleted, 'new tail' + 'extra' added, 'line one'→'line ONE' (del+add)
    expect(screen.getAllByTestId('optdiff-line-add').length).toBeGreaterThan(0)
    expect(screen.getAllByTestId('optdiff-line-del').length).toBeGreaterThan(0)
  })

  it('loads the candidate an open_panel carried BEFORE the tile mounted (first open)', async () => {
    stubFetch()
    drive(bridgeActions.optimizerDiff('cand-1'))
    render(wrap(<OptimizerMerged registration={reg()} scope={{}} mode="personal" initialTab="diff" />))
    await waitFor(() => expect(screen.getByTestId('optdiff-body')).toBeInTheDocument())
  })

  it('a remount does not replay a request it already consumed', () => {
    stubFetch()
    drive(bridgeActions.optimizerDiff('cand-1'))
    const first = render(wrap(<OptimizerMerged registration={reg()} scope={{}} mode="personal" initialTab="diff" />))
    first.unmount()
    render(wrap(<OptimizerMerged registration={reg()} scope={{}} mode="personal" initialTab="diff" />))
    expect(screen.getByTestId('optdiff-empty')).toBeInTheDocument()
  })

  it('loads via data_query.candidate_id', async () => {
    stubFetch()
    const r = { ...reg(), data_query: { candidate_id: 'cand-1' } }
    render(wrap(<OptimizerDiffPanel registration={r} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('optdiff-body')).toBeInTheDocument())
  })
})
