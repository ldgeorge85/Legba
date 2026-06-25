/**
 * Component test for the UI-6 Report Export panel.
 *
 * `fetch` is stubbed for the live reads (/findings + /situations). Asserts:
 * rows render; selecting + Generate produces a STIX preview (now with both a
 * report SDO AND an indicator SDO); raw-JSON + markdown formats preview; Download
 * triggers a Blob object-URL + anchor click; Print calls window.print.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import ReportExportPanel from './ReportExport'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_report_export', descriptor_id: 'report', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Report Export', mode: 'personal',
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
    if (u.includes('/findings'))
      body = { data: [{ id: 'f1', title: 'Coup risk', body: 'army movement', severity: 'high', target_id: 'brazil', produced_at: '2026-06-03T00:00:00Z', derived_from: ['s1'] }] }
    else if (u.includes('/situations'))
      body = [{ id: 'sit1', summary: 'Escalating', severity: 'critical', target_id: 'brazil', opened_at: '2026-06-02T00:00:00Z', contributing_finding_ids: ['f1'] }]
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('ReportExportPanel', () => {
  it('renders selectable findings + situations', async () => {
    stubFetch()
    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('report-item-finding-f1')).toBeInTheDocument())
    expect(screen.getByTestId('report-item-situation-sit1')).toBeInTheDocument()
  })

  it('generate produces a STIX bundle with report AND indicator SDOs', async () => {
    stubFetch()
    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('report-check-finding-f1')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('report-check-finding-f1'))
    fireEvent.click(screen.getByTestId('report-generate'))

    await waitFor(() => expect(screen.getByTestId('report-preview')).toBeInTheDocument())
    const preview = screen.getByTestId('report-preview').textContent ?? ''
    const parsed = JSON.parse(preview)
    expect(parsed.type).toBe('bundle')
    const objs = parsed.objects as any[]
    expect(objs.some((o) => o.type === 'report' && o.name === 'Coup risk')).toBe(true)
    expect(objs.some((o) => o.type === 'indicator' && o.name === 'Coup risk')).toBe(true)
  })

  it('raw-JSON format previews the selection verbatim', async () => {
    stubFetch()
    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('report-check-finding-f1')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('report-format'), { target: { value: 'json' } })
    fireEvent.click(screen.getByTestId('report-check-finding-f1'))
    fireEvent.click(screen.getByTestId('report-generate'))

    await waitFor(() => expect(screen.getByTestId('report-preview')).toBeInTheDocument())
    const parsed = JSON.parse(screen.getByTestId('report-preview').textContent ?? '')
    expect(parsed.count).toBe(1)
    expect(parsed.items[0].id).toBe('f1')
  })

  it('markdown format previews a markdown report', async () => {
    stubFetch()
    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('report-check-finding-f1')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('report-format'), { target: { value: 'markdown' } })
    fireEvent.click(screen.getByTestId('report-select-all'))
    fireEvent.click(screen.getByTestId('report-generate'))

    await waitFor(() => {
      expect(screen.getByTestId('report-preview').textContent).toContain('# Legba Intelligence Report')
    })
  })

  it('download builds a Blob and clicks an anchor', async () => {
    stubFetch()
    const createUrl = vi.fn(() => 'blob:mock')
    const revokeUrl = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL: createUrl, revokeObjectURL: revokeUrl })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('report-check-finding-f1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('report-check-finding-f1'))
    fireEvent.click(screen.getByTestId('report-download'))

    expect(createUrl).toHaveBeenCalled()
    expect(clickSpy).toHaveBeenCalled()
    clickSpy.mockRestore()
  })

  it('print → PDF calls window.print', async () => {
    stubFetch()
    const printSpy = vi.fn()
    vi.stubGlobal('print', printSpy)

    render(wrap(<ReportExportPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('report-check-finding-f1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('report-check-finding-f1'))
    fireEvent.click(screen.getByTestId('report-print'))

    expect(printSpy).toHaveBeenCalled()
  })
})
