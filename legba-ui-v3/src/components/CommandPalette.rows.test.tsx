/**
 * ⌘K's ROW MODEL (X3/D3; review D §C4: "⌘K opens on 465 rows sorted
 * alphabetically, starting with '38 North', 'ABC News Australia' and paused
 * sources"; "⌘K tags Brazil TARGET").
 *
 *  - the empty palette opens on SUBJECTS, ordered by recent activity (the
 *    newest read on record), then panels, … then sources, paused sources last
 *    and marked;
 *  - "38 North" is a source, never a subject;
 *  - a subject is called by the profile's noun, never "target", and its Enter
 *    opens its read (the same three actions as a rail click).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { CommandPalette, orderPaletteEntries, recordEntries, type PaletteEntry } from './CommandPalette'
import { PortalHost } from '@/ui/portal'
import profileUiJson from '@/lib/profile_ui.json'
import { subjectWordsIn } from '@/lib/subjectNoun'
import { fromFullRow } from '@/lib/subjectReads'

const NOUN = profileUiJson.subject.singular.toLowerCase()

const TARGETS = [
  { descriptor_id: 'country_g20_br', name: 'G20 — Brazil', state: 'active', body: { scope: { domain: 'geo', geo: ['BR'], tags: ['g20'] } } },
  { descriptor_id: 'country_watch_sd', name: 'Watch — Sudan', state: 'active', body: { scope: { domain: 'geo', geo: ['SD'], tags: ['watch'] } } },
  { descriptor_id: 'country_g20_us', name: 'G20 — United States', state: 'active', body: { scope: { domain: 'geo', geo: ['US'], tags: ['g20'] } } },
  { descriptor_id: 'situation_iran_war', name: 'Situation — US–Iran War', state: 'active', body: { scope: { domain: 'thematic', geo: [], tags: ['situation'] } } },
  { descriptor_id: 'tpl_country', name: 'Country Geopolitical Template (pivot)', state: 'draft', body: { scope: { domain: 'geo', geo: [], tags: [] } } },
]
const SOURCES = [
  { descriptor_id: 'source.38north.analysis', name: '38 North — Korea Analysis', state: 'active' },
  { descriptor_id: 'source.abc_au.justin', name: 'ABC News Australia — Just In', state: 'active' },
  { descriptor_id: 'source.actuniger.politique', name: 'ActuNiger — Politique', state: 'paused' },
]
const ANALYSTS = [{ descriptor_id: 'alert_trigger_scan', name: 'Alert Trigger Scan', state: 'active' }]
// Activity: Sudan read most recently, then Brazil; the US, the situation and the draft have no read on record.
const COUNTRY_READS = {
  data: [
    { id: 'c1', analyst_id: 'country_composition', target_id: 'country_watch_sd', produced_at: '2026-10-08T18:00:00Z', confidence: 0.7, title: 'Sudan', data: {} },
    { id: 'c2', analyst_id: 'country_composition', target_id: 'country_g20_br', produced_at: '2026-10-08T09:00:00Z', confidence: 0.7, title: 'Brazil', data: {} },
  ],
  next_cursor: null,
}

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      let body: unknown = []
      if (url.includes('family=target')) body = TARGETS
      else if (url.includes('family=source')) body = SOURCES
      else if (url.includes('family=analyst')) body = ANALYSTS
      // U-4: verdicts read the verdict-weight route
      else if (url.includes('/v3/map/subject_reads?analyst_id=country_composition')) body = { available: true, data: COUNTRY_READS.data.map(fromFullRow) }
      else if (url.includes('/findings?analyst_id=country_composition')) body = COUNTRY_READS
      else if (url.includes('/findings')) body = { data: [], next_cursor: null }
      return { ok: true, status: 200, json: async () => body }
    }),
  )
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PortalHost>{ui}</PortalHost>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  localStorage.clear()
  stubFetch()
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const keys = () =>
  [...screen.getByTestId('command-palette-list').querySelectorAll('[data-testid^="command-palette-item-"]')].map(
    (el) => (el.getAttribute('data-testid') ?? '').replace('command-palette-item-', ''),
  )

describe('⌘K opens on subjects, by activity — never an alphabetical wall', () => {
  it('the first rows are subjects ordered by their newest read; paused sources are last and marked', async () => {
    render(wrap(<CommandPalette open mode="personal" onClose={() => {}} onDrive={vi.fn()} />))
    await waitFor(() => expect(keys()).toContain('record:target:country_g20_br'))
    await waitFor(() => expect(keys()[0]).toBe('record:target:country_watch_sd'))
    const k = keys()
    // subjects first: by activity (Sudan 18:00Z, Brazil 09:00Z), then the ones with no read on record
    expect(k.slice(0, 4)).toEqual([
      'record:target:country_watch_sd',
      'record:target:country_g20_br',
      // no read on record — after every subject that has one, by name
      'record:target:country_g20_us',
      'record:target:situation_iran_war',
    ])
    // "38 North" is a source, not a subject — and not the first row
    const north = screen.getByTestId('command-palette-item-record:source:source.38north.analysis')
    expect(north).toHaveAttribute('data-family', 'source')
    expect(k.indexOf('record:source:source.38north.analysis')).toBeGreaterThan(k.indexOf('record:target:situation_iran_war'))
    // panels come before any source
    const firstPanel = k.findIndex((x) => x.startsWith('panel:'))
    expect(firstPanel).toBeGreaterThan(3)
    expect(firstPanel).toBeLessThan(k.indexOf('record:source:source.38north.analysis'))
    // the paused source is LAST, and says so
    expect(k[k.length - 1]).toBe('record:source:source.actuniger.politique')
    const paused = screen.getByTestId('command-palette-item-record:source:source.actuniger.politique')
    expect(paused).toHaveAttribute('data-family', 'source_paused')
    expect(paused.textContent).toMatch(/paused/i)
    // the draft subject sinks below the active sources, above the paused one
    expect(k.indexOf('record:target:tpl_country')).toBeGreaterThan(k.indexOf('record:source:source.abc_au.justin'))
  })

  it("a subject row is called by the profile's noun — never 'target' — and carries its last read", async () => {
    render(wrap(<CommandPalette open mode="personal" onClose={() => {}} onDrive={vi.fn()} />))
    const br = await screen.findByTestId('command-palette-item-record:target:country_g20_br')
    expect(br.textContent).toContain('Brazil')
    expect(br.textContent?.toLowerCase()).toContain(NOUN)
    expect(br.textContent).not.toMatch(/target/i)
    const us = screen.getByTestId('command-palette-item-record:target:country_g20_us')
    expect(us.textContent).toContain('no read on record')
    // no palette row mixes two of the collapsed nouns
    for (const el of screen.getByTestId('command-palette-list').querySelectorAll('[data-entry-kind="record"]')) {
      if (el.getAttribute('data-family') === 'inactive') continue // a draft's DECLARED name is data, not our word
      expect(subjectWordsIn(el.textContent ?? '').length).toBeLessThanOrEqual(1)
      expect(el.textContent ?? '').not.toMatch(/\btarget\b/i)
    }
  })
})

describe('the row model, pure', () => {
  const run = vi.fn()
  it("a subject's Enter opens its READ — set_scope, focus, open_panel subject.page — and ⌘Enter only focuses", () => {
    const [row] = recordEntries(
      [{ recordKind: 'target', id: 'country_g20_br', label: 'G20 — Brazil', state: 'active' }],
      run,
      new Map([['country_g20_br', Date.parse('2026-10-08T09:00:00Z')]]),
    )
    expect(row.family).toBe('subject')
    row.open()
    const actions = run.mock.calls.at(-1)![0] as { action: string; args: Record<string, unknown> }[]
    expect(actions.map((a) => a.action)).toEqual(['set_scope', 'focus', 'open_panel'])
    expect(actions[0].args).toMatchObject({ kind: 'target', id: 'country_g20_br' })
    expect(actions[2].args).toMatchObject({ kind: 'subject.page', params: { tab: 'brief' } })
    row.alt!()
    expect((run.mock.calls.at(-1)![0] as { action: string }[]).map((a) => a.action)).toEqual(['focus'])
  })

  it('a query still sinks a paused source below every other match', () => {
    const mk = (key: string, family: PaletteEntry['family'], label: string): PaletteEntry => ({
      key,
      family,
      label,
      hint: '',
      entryKind: 'record',
      search: label,
      open: () => {},
    })
    const out = orderPaletteEntries(
      [mk('p', 'source_paused', 'Niger news'), mk('s', 'source', 'Niger wire'), mk('d', 'subject', 'Niger')],
      'niger',
    )
    expect(out.map((e) => e.key)).toEqual(['d', 's', 'p'])
  })
})
