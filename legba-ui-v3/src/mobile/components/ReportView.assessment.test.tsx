/**
 * ReportView (W4) — an Assessment row's panel, rendered against the sanitised
 * real 64f74201 / e30e67d3 payloads. Before: Findings / Targets / Signals /
 * Where all read 0, every [[ref:N]] rendered unresolved, and "verify —".
 */
import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import ReportView from './ReportView'
import assessmentFixture from '../__fixtures__/world_assessment.64f74201.json'
import spineFixture from '../__fixtures__/world_spine.e30e67d3.json'
import type { ReadFindingRow } from '@/lib/assemblyModel'

const ASSESSMENT = assessmentFixture as unknown as ReadFindingRow
const SPINE = spineFixture as unknown as ReadFindingRow

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function view(row: ReadFindingRow, spineRow: ReadFindingRow | null = null) {
  render(
    wrap(
      <ReportView
        row={row}
        entityId={null}
        onSelectEntity={() => {}}
        onBack={() => {}}
        offlineNote={null}
        spineRow={spineRow}
      />,
    ),
  )
}

function counts(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const id of ['findings', 'entities', 'signals', 'where']) {
    out[id] = within(screen.getByTestId(`section-${id}`)).getByTestId(`section-toggle-${id}`)
      .querySelector('.m-count')!.textContent!
  }
  return out
}

describe('an Assessment row, alone', () => {
  it('counts what the read quotes, and shows signals as unavailable (a dash), not 0', () => {
    view(ASSESSMENT)
    expect(counts()).toEqual({ findings: '16', entities: '15', signals: '—', where: '15' })
  })

  it('resolves the body references: no unresolved chip', () => {
    view(ASSESSMENT)
    expect(screen.queryAllByTestId('citation-unresolved')).toHaveLength(0)
    expect(screen.getAllByTestId('citation-chip').length).toBeGreaterThanOrEqual(16)
  })

  it('M-0a: the header carries no engine figure (no verify score, gate, block count or drop ledger)', () => {
    view(ASSESSMENT)
    const header = screen.getByTestId('report-view').querySelector('header')!.textContent ?? ''
    expect(header).not.toMatch(/verify|gate|blocks|below the line|below floor|aperture|floor honoured/i)
    expect(screen.queryByTestId('verify-line')).toBeNull()
    expect(screen.queryByTestId('drop-ledger')).toBeNull()
  })

  it('explains the missing signals once the section is opened', () => {
    view(ASSESSMENT)
    fireEvent.click(screen.getByTestId('section-toggle-signals'))
    expect(screen.getByTestId('signals-unavailable')).toBeInTheDocument()
    expect(screen.queryByText('No signals under this filter.')).toBeNull()
  })
})

describe('an Assessment row with its spine', () => {
  it('fills the signals from the cited blocks', () => {
    view(ASSESSMENT, SPINE)
    const c = counts()
    expect(c.findings).toBe('16')
    expect(Number(c.signals)).toBeGreaterThan(0)
    fireEvent.click(screen.getByTestId('section-toggle-signals'))
    expect(screen.getAllByTestId('signal-item').length).toBe(Number(c.signals))
  })
})

describe('the spine row itself', () => {
  it('M-0a: no "verify 1.00" on the header or the cards; a card says how old its evidence is in words', () => {
    view(SPINE)
    const page = screen.getByTestId('report-view').textContent ?? ''
    expect(page).not.toMatch(/verify \d|gate (passed|failed)|\d+ blocks/)
    const metas = [...document.querySelectorAll('.m-block-meta')].map((e) => e.textContent ?? '')
    expect(metas.length).toBeGreaterThan(0)
    for (const m of metas) expect(m).not.toMatch(/verify|\d\.\d\d(?!\d)/)
    expect(metas.some((m) => /evidence .+ old/.test(m))).toBe(true)
  })
})

describe('reader words in the report header and body (U-1)', () => {
  it('names the subject instead of its machine id, and renders markdown', () => {
    // Synthetic title/body over the fixture's shape.
    view({
      ...ASSESSMENT,
      target_id: 'country_zz_qq',
      title: 'country_zz_qq, 2026-01-02 — 2 desks: unit_alpha, unit_beta',
      body: '**BLUF:** Ports reopen.\n\n## The reading\n\nTraffic resumed.',
    })
    const title = screen.getByTestId('report-title').textContent ?? ''
    expect(title).not.toMatch(/country_zz_qq|unit_alpha/)
    expect(title).toMatch(/unit alpha, unit beta/)
    const page = screen.getByTestId('report-view')
    expect(page.textContent).not.toMatch(/\*\*BLUF|## The reading/)
    expect(page.querySelector('strong')?.textContent).toMatch(/BLUF/)
  })
})
