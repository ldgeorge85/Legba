/**
 * Component test for the A10 Report Export panel (collection-basket export).
 *
 * The panel reads the persistent export basket (`@/state/exportBasket`) and
 * POSTs it to `/api/v1/v3/export`; `fetch` is stubbed to return a markdown /
 * JSON document. Asserts: basket rows render + are removable; export POSTs the
 * basket and previews the returned document (markdown rendered, JSON raw);
 * export triggers a download (Blob object-URL + anchor click); the empty-basket
 * state disables export.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import ReportExportPanel from './ReportExport'
import { useExportBasket } from '@/state/exportBasket'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_report_export', descriptor_id: 'report', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Report Export', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  return ui
}

const MD_DOC = '# Legba export\n\n> machine-generated export\n\n## 1. Coup risk\n\nbody.\n'
const JSON_DOC = JSON.stringify({ title: 'Legba export', item_count: 1, items: [{ id: 'f1' }] })

function stubFetch(format: 'markdown' | 'json' = 'markdown') {
  const isMd = format === 'markdown'
  const mock = vi.fn(() =>
    Promise.resolve({
      ok: true,
      headers: new Headers({
        'Content-Type': isMd ? 'text/markdown; charset=utf-8' : 'application/json',
        'Content-Disposition': `attachment; filename="legba-export-20260724.${isMd ? 'md' : 'json'}"`,
      }),
      text: async () => (isMd ? MD_DOC : JSON_DOC),
    }),
  )
  vi.stubGlobal('fetch', mock)
  return mock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useExportBasket.getState().clear()
})

describe('ReportExportPanel', () => {
  it('renders basket items and the empty state', () => {
    const { rerender } = render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('report-basket-empty')).toBeInTheDocument()

    useExportBasket.getState().add({ kind: 'finding', id: 'f1', label: 'Coup risk' })
    rerender(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('report-basket-item-finding-f1')).toBeInTheDocument()
    expect(screen.queryByTestId('report-basket-empty')).not.toBeInTheDocument()
  })

  it('removes an item from the basket', () => {
    useExportBasket.getState().add({ kind: 'finding', id: 'f1', label: 'Coup risk' })
    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.click(screen.getByTestId('report-basket-remove-finding-f1'))
    expect(useExportBasket.getState().items).toHaveLength(0)
  })

  it('export POSTs the basket and renders the markdown preview', async () => {
    const mock = stubFetch('markdown')
    useExportBasket.getState().add({ kind: 'finding', id: 'f1', label: 'Coup risk' })
    const createUrl = vi.fn(() => 'blob:mock')
    vi.stubGlobal('URL', { ...URL, createObjectURL: createUrl, revokeObjectURL: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.click(screen.getByTestId('report-export'))

    await waitFor(() => expect(screen.getByTestId('report-preview')).toBeInTheDocument())
    // POSTed to the export route with the basket items + markdown format.
    const [url, init] = mock.mock.calls[0] as unknown as [string, RequestInit]
    expect(String(url)).toContain('/v3/export')
    const sent = JSON.parse(String(init.body))
    expect(sent.format).toBe('markdown')
    expect(sent.items).toEqual([{ kind: 'finding', id: 'f1' }])
    // Markdown rendered (heading text present, not raw '#').
    expect(screen.getByTestId('report-preview').textContent).toContain('Legba export')
    // Download fired.
    expect(createUrl).toHaveBeenCalled()
  })

  it('json format previews the raw document', async () => {
    stubFetch('json')
    useExportBasket.getState().add({ kind: 'journal_entry', id: 'j1', label: 'lens' })
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:mock'), revokeObjectURL: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    fireEvent.click(screen.getByTestId('report-format-json'))
    fireEvent.click(screen.getByTestId('report-export'))

    await waitFor(() => expect(screen.getByTestId('report-preview')).toBeInTheDocument())
    expect(screen.getByTestId('report-preview').textContent).toContain('"item_count": 1')
  })

  it('disables export on an empty basket', () => {
    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('report-export')).toBeDisabled()
  })
})
