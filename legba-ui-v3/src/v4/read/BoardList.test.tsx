/**
 * The Board's stage, rendered: eight ranked rows then "more"; Changed,
 * Checked, Gaps and Due as filters of the one list; a row opens its record;
 * no raw markup and no machine id on screen; no reading timer; the full daily
 * read one link away; nothing counted before it is read.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import BoardList from './BoardList'
import { useSelection } from '@/state/selection'
import { resetScope, useScope } from '@/state/scope'
import { scopeFromTarget } from '@/lib/scopeFromReport'
import { registerFramePort } from '@/lib/panelActions'

function sect<T>(items: T[]) {
  return { items, total: items.length, truncated: false }
}

const NOW = new Date().toISOString()

function findings(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    id: `f${i}`,
    analyst_id: 'military_posture',
    target_id: i % 2 ? 'country_g20_ru' : 'country_watch_bf',
    title: i === 0 ? '**BLUF:** Garrison moves north on military_posture [[ref:1]] [3]' : `Change number ${i}`,
    severity: i < 3 ? 'high' : 'low',
    confidence: 0.7,
    faithfulness_score: 0.9,
    effective_confidence: 0.62,
    produced_at: NOW,
  }))
}

interface Payloads {
  since?: unknown
  findings?: unknown
  hold?: boolean
}

function stub(p: Payloads = {}) {
  const fetchMock = vi.fn(async (url: string) => {
    if (p.hold) return new Promise(() => {})
    const u = String(url)
    const body = u.includes('/v3/since')
      ? (p.since ?? { cursor: NOW, server_now: NOW, counts: {}, new_findings: sect(findings(11)), superseded: sect([]), band_changes: sect([]), situations: sect([]), alerts: sect([]), forecasts_due: sect([]) })
      : u.includes('/v3/eval/country_scorecard')
        ? []
        : (p.findings ?? {
            data: [
              {
                id: 'f1', kind: 'finding', title: 'Change number 1', analyst_id: 'x', analyst_version: null, target_id: 'country_g20_ru', target_version: null,
                produced_at: NOW, severity: 'high', confidence: 0.7, schema_uri: 'x',
                verification: { unsupported_spans: [{ text: '> The claim [[ref:2]] **holds** not', reason: 'unsupported' }] },
                citations: [{ ordinal: 1, source: 's1', source_id: 's1', produced_at: null, single_source: false, wire_folded: false, marker_class: null }],
              },
            ],
          })
    return { ok: true, status: 200, json: async () => body, text: async () => '' } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  resetScope()
  useSelection.getState().clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the ranked list', () => {
  it('shows eight rows, then more; subjects by name; no raw markup, no timer', async () => {
    stub()
    render(wrap(<BoardList />))
    await waitFor(() => expect(screen.getAllByTestId('board-row')).toHaveLength(8))
    const list = screen.getByTestId('board-list')
    expect(list.textContent).not.toMatch(/\*\*|\[\[ref|\[\d+\]|BLUF|country_|military_posture/)
    expect(list.textContent).not.toMatch(/\d+:\d\d \/ 20:00/)
    expect(screen.queryByTestId('morning-read-timer')).toBeNull()
    for (const el of screen.getAllByTestId('board-row-subject')) expect(el.textContent).toMatch(/^[A-Z][^_]+$/)
    expect(screen.getByTestId('board-filter-count-changed').textContent).toBe('11')
    fireEvent.click(screen.getByTestId('board-list-more'))
    expect(screen.getAllByTestId('board-row')).toHaveLength(11)
  })

  it('a row opens its record in the sheet (a reader selection, not the stage)', async () => {
    stub()
    render(wrap(<BoardList />))
    const first = (await screen.findAllByTestId('board-row'))[0]
    fireEvent.click(first)
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f0', origin: 'board' })
  })

  it('Checked is a filter of the same list; its flagged claim is rendered, not raw', async () => {
    stub()
    render(wrap(<BoardList />))
    await screen.findAllByTestId('board-row')
    fireEvent.click(screen.getByTestId('board-filter-checked'))
    expect(screen.getByTestId('board-list')).toHaveAttribute('data-filter', 'checked')
    const rows = await screen.findAllByTestId('board-row')
    expect(rows).toHaveLength(1)
    const sentence = within(rows[0]).getByTestId('board-row-sentence')
    expect(sentence.textContent).toBe('The claim holds not')
    expect(sentence.querySelector('strong')?.textContent).toBe('holds')
    expect(within(rows[0]).getByTestId('board-row-trust').textContent).toBe('1 claim flaggedSingle source')
  })

  it('Gaps is not read, nor counted, until asked for', async () => {
    const f = stub()
    render(wrap(<BoardList />))
    await screen.findAllByTestId('board-row')
    expect(screen.queryByTestId('board-filter-count-gaps')).toBeNull()
    expect(f.mock.calls.some((c) => String(c[0]).includes('country_scorecard'))).toBe(false)
    fireEvent.click(screen.getByTestId('board-filter-gaps'))
    await waitFor(() => expect(f.mock.calls.some((c) => String(c[0]).includes('country_scorecard'))).toBe(true))
  })

  it('prints no zero before anything is read', async () => {
    stub({ hold: true })
    render(wrap(<BoardList />))
    expect(screen.getByTestId('board-list-loading')).toBeInTheDocument()
    expect(screen.getByTestId('board-filter-count-changed').textContent).toBe('—')
    expect(screen.getByTestId('board-list').textContent).not.toMatch(/\b0\b/)
  })

  it('a subject that is the point narrows the list, and can be cleared', async () => {
    stub()
    useScope.getState().setScope(scopeFromTarget('country_g20_ru', 'Russia', 'test'))
    render(wrap(<BoardList />))
    await waitFor(() => expect(screen.getAllByTestId('board-row')).toHaveLength(5))
    expect(screen.getByTestId('board-list-scoped').textContent).toContain('Russia')
    fireEvent.click(screen.getByTestId('board-list-scope-clear'))
    await waitFor(() => expect(screen.getAllByTestId('board-row')).toHaveLength(8))
  })

  it('the full daily read is one link away, opened on the stage', async () => {
    stub()
    const openView = vi.fn(() => true)
    const off = registerFramePort({ openView } as never)
    render(wrap(<BoardList />))
    fireEvent.click(screen.getByTestId('board-daily-read'))
    expect(openView).toHaveBeenCalledWith('v4.morning_read', { place: 'stage', origin: 'operator' })
    off()
  })
})
