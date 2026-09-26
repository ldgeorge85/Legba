/**
 * Component test for TargetOverviewPanel — source-first version.
 *
 * The panel reads the live runtime endpoints (GET /targets/{id}/runtime +
 * /signals + /findings); the old /rollup contract was retired in the pivot.
 * These tests cover the 404-soft-fail path and the descriptor-render path.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetOverviewPanel from './Overview'
import type { PanelRegistration } from '@/types'
import { PRINT_FRAME_ATTR } from '@/lib/printDocument'
import { useExportBasket } from '@/state/exportBasket'

function reg(overrides: Partial<PanelRegistration> = {}): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_overview',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Brazil Overview',
    mode: 'personal',
    layout_slot: 'dashboard.brazil.overview',
    data_query: {},
    binding: { target_id: 'brazil' },
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
    ...overrides,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

/** Route a stubbed fetch by URL fragment; unmatched → 404. */
function routedFetch(routes: Record<string, unknown>) {
  return vi.fn((url: string) => {
    const u = String(url)
    for (const [frag, body] of Object.entries(routes)) {
      if (u.includes(frag)) {
        return Promise.resolve({ ok: true, status: 200, json: async () => body })
      }
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useExportBasket.getState().clear()
})

describe('TargetOverviewPanel', () => {
  it('renders gracefully when the runtime endpoints 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }),
    )
    render(
      wrap(<TargetOverviewPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />),
    )
    await waitFor(() => {
      expect(screen.getByText(/no descriptor row/i)).toBeInTheDocument()
    })
  })

  it('renders the descriptor when /targets/{id}/runtime returns data', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/targets/brazil/runtime': {
          descriptor_id: 'brazil',
          active_descriptor: {
            descriptor_id: 'brazil',
            version: 'a'.repeat(64),
            schema_uri: 'iglu:legba/target/jsonschema/3-0-0',
            state: 'active',
            name: 'Brazil',
            abstraction_level: 'L1',
            source_count: 3,
          },
          actors: [],
        },
        '/signals': { data: [] },
        '/findings': { data: [] },
      }),
    )
    render(
      wrap(<TargetOverviewPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />),
    )
    await waitFor(() => {
      expect(screen.getByText('Brazil')).toBeInTheDocument()
    })
    expect(screen.getByText('active')).toBeInTheDocument()
  })

  it('"desk brief": fills the basket with the composition, exports, and downloads one file', async () => {
    const compositionFinding = {
      id: 'comp-1',
      title: 'Brazil composition',
      produced_at: '2026-09-01T00:00:00Z',
      analyst_id: 'country_composition',
      target_id: 'brazil',
      data: {
        data: {
          assembly: {
            schema: 'assembly.v1',
            regime: 'assembly',
            tier: 'country',
            as_of: '2026-09-01T00:00:00Z',
            blocks: [],
            coverage: [],
          },
        },
      },
    }
    const createUrl = vi.fn(() => 'blob:mock')
    vi.stubGlobal('URL', { ...URL, createObjectURL: createUrl, revokeObjectURL: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    const fetchMock = vi.fn((url: string) => {
      const u = String(url)
      if (u.includes('/v3/export')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers({
            'Content-Type': 'text/markdown; charset=utf-8',
            'Content-Disposition': 'attachment; filename="legba-export-20260901.md"',
          }),
          text: async () => '# Desk brief — Brazil\n\nbody\n',
        })
      }
      if (u.includes('/findings') && u.includes('analyst_id=country_composition')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ data: [compositionFinding] }) })
      }
      if (u.includes('/situations')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => ({ data: [], next_cursor: null }) })
      }
      // /targets/{id}/runtime, /signals, the panel's own /findings — all 404
      // (the empty-state branches this file's first test already covers).
      return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(
      wrap(<TargetOverviewPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />),
    )

    const button = await screen.findByTestId('desk-brief-button')
    expect(button).not.toBeDisabled()
    fireEvent.click(button)

    await waitFor(() => expect(createUrl).toHaveBeenCalled())

    // The basket now carries the composition, and the export POST sent
    // exactly that item.
    expect(useExportBasket.getState().items.map((i) => i.id)).toEqual(['comp-1'])
    const exportCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/v3/export'))
    expect(exportCall).toBeTruthy()
    const sent = JSON.parse(String((exportCall as unknown as [string, RequestInit])[1].body))
    expect(sent.items).toEqual([{ kind: 'finding', id: 'comp-1' }])
    expect(sent.title).toBe('Desk brief — brazil')

    // 7b-iii — the brief just composed is HELD, so it can be printed without
    // a second compose. It prints in its own frame, never through the shell.
    const shellPrint = vi.fn()
    vi.stubGlobal('print', shellPrint)
    const printButton = screen.getByTestId('desk-brief-print')
    await waitFor(() => expect(printButton).toBeEnabled())
    fireEvent.click(printButton)

    const frame = document.querySelector<HTMLIFrameElement>(`iframe[${PRINT_FRAME_ATTR}]`)
    expect(frame).not.toBeNull()
    expect(frame!.contentDocument?.title).toBe('Desk brief — Brazil')
    expect(shellPrint).not.toHaveBeenCalled()
    // Exactly one export POST — printing re-used the composed bytes.
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/v3/export'))).toHaveLength(1)
  })

  it('"print / save as PDF" is disabled until a brief has been composed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }),
    )
    render(
      wrap(<TargetOverviewPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />),
    )
    expect(await screen.findByTestId('desk-brief-print')).toBeDisabled()
  })
})
