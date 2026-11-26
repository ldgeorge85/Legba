/**
 * The OBJECT RAIL (UI-B1/shell; UI-D1 §2.2) — what replaced the panel tree.
 *
 *  - six sections, in the fixed order, labelled from `profile_ui.json`;
 *  - ZERO panel rows (the ratchet that replaced the tree's 26-row budget):
 *    every row is an object — a read, a subject, a unit, a source,
 *    a case or a queue — and the catalog is the `＋` popover;
 *  - subjects grouped by the profile's subject roster, human names, the
 *    confidence band from the same verdict read the Wall uses, and no
 *    fabricated band on a subject that has no composition tier;
 *  - every click compiles to drive actions and runs through the ONE executor
 *    (a fake shell port records what reached the dock);
 *  - absence: a count not read yet is "—", never 0.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { fromFullRow } from '@/lib/subjectReads'
import { CatalogPopover } from '@/components/CatalogPopover'
import { ObjectRail, RAIL_SECTIONS, SubjectRoster } from './ObjectRail'
import { registerShell, type ShellPort } from '@/lib/applyAction'
import profileUiJson from '@/lib/profile_ui.json'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { catalogKinds } from '@/panel-registry/kindPlan'
import { useSelection } from '@/state/selection'
import { mockErrorResponse } from '@/test/apiMocks'
import { resetScope, useScope } from '@/state/scope'
import { subjectReadActions } from '@/lib/subjectRead'
import { subjectWordsIn } from '@/lib/subjectNoun'

const COUNTRY_READS = {
  data: [
    {
      id: 'f1',
      analyst_id: 'country_composition',
      target_id: 'country_g20_br',
      produced_at: '2026-07-20T00:00:00Z',
      confidence: 0.85,
      effective_confidence: 0.85,
      verification: { faithfulness_score: 0.9, judge_status: 'llm' },
      title: 'Brazil — country composition',
      data: { citations: [1, 2] },
    },
    {
      id: 'f2',
      analyst_id: 'country_composition',
      target_id: 'country_watch_sd',
      produced_at: '2026-07-19T00:00:00Z',
      confidence: 0.4,
      effective_confidence: 0.4,
      verification: null,
      title: 'Sudan — country composition',
      data: { citations: [] },
    },
  ],
  next_cursor: null,
}

const WORLD_READS = {
  data: [
    {
      id: 'w1',
      analyst_id: 'world_assessor',
      target_id: null,
      produced_at: '2026-07-21T06:00:00Z',
      title: 'The world read',
      data: {},
    },
  ],
  next_cursor: null,
}

// The route's shape: the declared name and scope (UI-B2 reads a subject's name
// and roster leg off these, never off its id).
const TARGET_DESCRIPTORS = [
  {
    descriptor_id: 'lane_hormuz',
    name: 'Lane — Strait of Hormuz',
    state: 'active',
    body: { scope: { domain: 'thematic', geo: ['IR', 'OM'], tags: ['supply_chain', 'lane'] } },
  },
  {
    descriptor_id: 'lane_black_sea',
    name: 'Lane — Black Sea & Turkish Straits',
    state: 'draft',
    body: { scope: { domain: 'thematic', geo: ['TR'], tags: ['supply_chain', 'lane'] } },
  },
  {
    descriptor_id: 'country_g20_br',
    name: 'G20 — Brazil',
    state: 'active',
    body: { scope: { domain: 'geo', geo: ['BR'], tags: ['geopolitical', 'g20', 'region_americas'] } },
  },
  {
    descriptor_id: 'country_watch_sd',
    name: 'Watch — Sudan',
    state: 'active',
    body: { scope: { domain: 'geo', geo: ['SD'], tags: ['geopolitical', 'watch', 'region_africa'] } },
  },
]

function stubFetch(overrides: Record<string, unknown> = {}) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    let body: unknown = { data: [], next_cursor: null }
    for (const [needle, b] of Object.entries(overrides)) if (url.includes(needle)) return Promise.resolve({ ok: true, json: async () => b })
    // The live plane today: built never — the route refuses BY NAME.
    if (url.includes('/registry/projection/')) {
      return Promise.resolve(
        mockErrorResponse(409, { detail: { state: 'projection_empty', reason: 'the plane has never been built', plane: 'geopolitics' } }),
      )
    }
    // U-4: the rail's verdicts read the verdict-weight route
    if (url.includes('/v3/map/subject_reads?analyst_id=country_composition')) body = { available: true, data: (COUNTRY_READS as { data: Parameters<typeof fromFullRow>[0][] }).data.map(fromFullRow) }
    else if (url.includes('/findings?analyst_id=country_composition')) body = COUNTRY_READS
    else if (url.includes('/findings?analyst_id=world_assessor')) body = WORLD_READS
    else if ((url.includes('/registry/descriptors') || url.includes('/v3/roster')) && url.includes('family=target')) body = TARGET_DESCRIPTORS
    else if ((url.includes('/registry/descriptors') || url.includes('/v3/roster'))) body = []
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function fakeShell() {
  const calls: string[] = []
  const shell = {
    mode: 'personal',
    openSingleton: vi.fn((kind: string) => {
      calls.push(`open:${kind}`)
      return true
    }),
    openBound: vi.fn((kind: string, id: string) => {
      calls.push(`bound:${kind}:${id}`)
      return true
    }),
    switchStance: vi.fn((id: string) => calls.push(`stance:${id}`)),
    applyPreset: vi.fn((id: string) => calls.push(`preset:${id}`)),
    runMission: vi.fn((id: string) => calls.push(`mission:${id}`)),
    reopenConsult: vi.fn(),
    currentStance: () => 'morning_read',
    serializeLayout: () => null,
    restoreLayout: vi.fn(),
  } satisfies ShellPort
  return { shell, calls }
}

let unregister: () => void = () => {}
let shell: ReturnType<typeof fakeShell>

beforeEach(() => {
  stubFetch()
  localStorage.clear()
  useSelection.getState().clear()
  resetScope()
  shell = fakeShell()
  unregister = registerShell(shell.shell)
})

afterEach(() => {
  unregister()
  vi.unstubAllGlobals()
})

const renderRail = () =>
  render(wrap(<ObjectRail mode="personal" />))

describe('the six sections', () => {
  it('render in the fixed order: Reads, Subjects, Units, Sources, Cases, Queues', () => {
    renderRail()
    const order = [...document.querySelectorAll('[data-rail-section]')].map((el) =>
      el.getAttribute('data-rail-section'),
    )
    expect(order).toEqual(['reads', 'subjects', 'units', 'sources', 'cases', 'queues'])
    expect(RAIL_SECTIONS.map((s) => s.id)).toEqual(order)
  })

  it('take the subject and unit nouns from profile_ui.json, verbatim', () => {
    renderRail()
    expect(screen.getByTestId('rail-section-title-subjects').textContent).toBe(profileUiJson.subject.plural)
    expect(screen.getByTestId('rail-section-title-units').textContent).toBe(profileUiJson.unit_noun.plural)
    expect(screen.getByTestId('rail-section-title-reads').textContent).toBe('Reads')
    expect(screen.getByTestId('rail-section-title-cases').textContent).toBe('Cases')
    expect(screen.getByTestId('rail-section-title-queues').textContent).toBe('Queues')
  })

  it('the two long rosters start folded, the four short sections open', () => {
    renderRail()
    const expanded = (id: string) =>
      screen.getByTestId(`rail-section-toggle-${id}`).getAttribute('aria-expanded')
    expect(expanded('units')).toBe('false')
    expect(expanded('sources')).toBe('false')
    for (const id of ['reads', 'subjects', 'cases', 'queues']) expect(expanded(id)).toBe('true')
  })
})

describe('RATCHET — panel rows in the rail: 0 (was 26)', () => {
  it('every row is an object; none is a panel, and no catalog title is a rail row', async () => {
    localStorage.setItem('legba_rail_collapsed', JSON.stringify([]))
    renderRail()
    await screen.findByText('Brazil')
    const rows = [...document.querySelectorAll('[data-rail-row]')]
    expect(rows.length).toBeGreaterThan(0)
    const objects = new Set(rows.map((r) => r.getAttribute('data-rail-row')))
    for (const o of objects) {
      expect(['read', 'subject', 'unit', 'source', 'case', 'queue']).toContain(o)
    }
    const titles = new Set(catalogKinds().map((k) => PANEL_REGISTRY[k].definition.defaultTitle))
    const panelRows = rows.filter((r) => titles.has((r.textContent ?? '').trim()))
    expect(panelRows.map((r) => r.textContent)).toEqual([])
  })

  it('the catalog is the top bar ＋ popover, and it lists no drill', () => {
    let closed = false
    render(wrap(<CatalogPopover mode="personal" side="operator" onClose={() => (closed = true)} />))
    // The rail no longer carries its own search / catalog row.
    const pop = screen.getByTestId('catalog-popover')
    expect(within(pop).queryByTestId('catalog-item-analysis.cross_framing')).toBeNull()
    expect(within(pop).queryByTestId('catalog-item-target.findings')).toBeNull()
    fireEvent.click(within(pop).getByTestId('catalog-item-system.journal'))
    // `open_panel` through the one executor reached the shell's opener.
    expect(shell.calls).toEqual(['open:system.journal'])
    expect(closed).toBe(true)
  })
})

describe('Subjects — grouped by the profile roster', () => {
  it('human names, grouped G20 / Watch list / supply_chain, a draft lane absent', async () => {
    renderRail()
    await screen.findByText('Brazil')
    expect(within(screen.getByTestId('rail-frame-g20')).getByText('Brazil')).toBeInTheDocument()
    expect(within(screen.getByTestId('rail-frame-watch')).getByText('Sudan')).toBeInTheDocument()
    expect(screen.getByTestId('rail-frame-g20').textContent).toContain(profileUiJson.labels.roster.g20)
    expect(screen.getByTestId('rail-frame-watch').textContent).toContain(profileUiJson.labels.roster.watch)
    await waitFor(() => expect(screen.getByTestId('rail-subject-lane_hormuz')).toBeInTheDocument())
    expect(screen.queryByTestId('rail-subject-lane_black_sea')).toBeNull()
    // The group order is the profile's roster order.
    const frames = [...document.querySelectorAll('[data-testid^="rail-frame-"]')].map((e) =>
      e.getAttribute('data-testid'),
    )
    expect(frames).toEqual(['rail-frame-g20', 'rail-frame-watch', 'rail-frame-supply_chain'])
  })

  it('the plane refusing: the roster grouping stands and the refusal is NAMED (never a blank)', async () => {
    renderRail()
    await screen.findByText('Brazil')
    const note = await screen.findByTestId('rail-projection-subject_groups')
    await waitFor(() => expect(note).toHaveAttribute('data-projection-state', 'projection_empty'))
    expect(note.textContent).toContain('projection_empty')
    expect(note).toHaveAttribute('title', 'projection_empty — the plane has never been built')
    // the Reads section names the tiers' source the same way
    await waitFor(() =>
      expect(screen.getByTestId('rail-projection-tiers')).toHaveAttribute('data-projection-state', 'projection_empty'),
    )
  })

  it('the plane answering: subjects group by the FRAME FAMILY, under the frame label', async () => {
    stubFetch({
      '/registry/projection/geopolitics/subject_groups': {
        state: 'ok',
        plane: 'geopolitics',
        projected_at: '2026-10-04T10:27:00Z',
        section: 'subject_groups',
        rows: [
          // the live shape (VIEW-1): the kind row resolves its WHOLE subtree and carries no rule of its own
          { key: 'region', ord: 0, value: { kind: 'region', label: 'Region frames', parent: null, member_rule: [], members: ['country_g20_br', 'country_g20_us', 'country_watch_sd'] } },
          { key: 'region_africa', ord: 1, value: { kind: 'region', label: 'Region — Africa', parent: 'region', member_rule: ['tag:region_africa'], members: ['country_watch_sd'] } },
          { key: 'region_americas', ord: 2, value: { kind: 'region', label: 'Region — Americas', parent: 'region', member_rule: ['tag:region_americas'], members: ['country_g20_br', 'country_g20_us'] } },
          { key: 'escalation_composition', ord: 3, value: { kind: 'thematic', label: 'escalation_composition (cross-region thematic)', parent: 'thematic', member_rule: ['analyst:escalation_composition'], members: [] } },
        ],
        unmeasured: null,
      },
    })
    renderRail()
    await screen.findByText('Brazil')
    await waitFor(() => expect(screen.getByTestId('rail-frame-region_americas')).toBeInTheDocument())
    expect(within(screen.getByTestId('rail-frame-region_americas')).getByText('Brazil')).toBeInTheDocument()
    expect(screen.getByTestId('rail-frame-region_americas').textContent).toContain('Region — Americas')
    expect(within(screen.getByTestId('rail-frame-region_africa')).getByText('Sudan')).toBeInTheDocument()
    // the roster leg rides the tooltip under a frame group
    expect(screen.getByTestId('rail-subject-country_g20_br').getAttribute('title') ?? '').toContain(
      profileUiJson.labels.roster.g20,
    )
    // a frame that resolves no member heads nothing; an ungrouped subject keeps its roster leg
    expect(screen.queryByTestId('rail-frame-region')).toBeNull()
    expect(screen.queryByTestId('rail-frame-escalation_composition')).toBeNull()
    await waitFor(() => expect(screen.getByTestId('rail-subject-lane_hormuz')).toBeInTheDocument())
    expect(within(screen.getByTestId('rail-frame-supply_chain')).getByText('Strait of Hormuz')).toBeInTheDocument()
    expect(screen.queryByTestId('rail-frame-g20')).toBeNull()
    expect(screen.queryByTestId('rail-projection-subject_groups')).toBeNull()
  })

  it('a band chip only where a composition tier exists — never fabricated on a lane', async () => {
    renderRail()
    const br = await screen.findByTestId('rail-subject-country_g20_br')
    expect(br.textContent).toMatch(/High|Moderate|Low|Unverified/)
    const lane = await screen.findByTestId('rail-subject-lane_hormuz')
    expect(lane.textContent).not.toMatch(/High|Moderate|Low|Unverified/)
  })

  it('a subject click makes it the point, focuses it, and goes to its home — the subject tab', async () => {
    renderRail()
    fireEvent.click(await screen.findByTestId('rail-subject-country_g20_br'))
    // (1) the wall's scope — the feed and the Morning Read filter by it
    expect(useScope.getState().scope).toMatchObject({ kind: 'target', id: 'country_g20_br', label: 'Brazil' })
    // (2) the focus — the Inspector (read state first) and Consult's pin follow it
    expect(useSelection.getState().selection).toMatchObject({ kind: 'target', id: 'country_g20_br', label: 'Brazil' })
    // (3) its home: the subject tab, whose page follows the point — never a
    // bound copy inside the tab the reader was on
    expect(shell.calls).toEqual(['stance:subject'])
  })

  it('the click compiles to set_scope → focus → arrange subject, in that order', () => {
    expect(subjectReadActions('country_g20_br', 'Brazil')).toEqual([
      { action: 'set_scope', args: { kind: 'target', id: 'country_g20_br', label: 'Brazil' } },
      { action: 'focus', args: { ref: { kind: 'target', id: 'country_g20_br' }, label: 'Brazil' } },
      { action: 'arrange', args: { layout: 'subject', layout_type: 'stance' } },
    ])
  })

  it('ONE subject noun on the rail: no rendered string carries two of subject / desk / target / country', async () => {
    localStorage.setItem('legba_rail_collapsed', JSON.stringify([]))
    renderRail()
    await screen.findByText('Brazil')
    fireEvent.click(screen.getByTestId('rail-drills-country_g20_br'))
    const rail = screen.getByTestId('object-rail')
    const words = subjectWordsIn(rail.textContent ?? '')
    expect(words.length).toBeLessThanOrEqual(1)
    if (words.length === 1) expect(words[0]).toBe(profileUiJson.subject.singular.toLowerCase())
    // the drill menu names the subject's reads in the profile's noun
    const menu = screen.getByTestId('rail-drill-menu-country_g20_br').textContent ?? ''
    expect(menu).not.toMatch(/target/i)
    expect(menu).toContain(`${profileUiJson.subject.singular} page`)
  })

  it("a subject's drills open BOUND to it from its own menu (the drill's parent)", async () => {
    renderRail()
    await screen.findByTestId('rail-subject-country_g20_br')
    fireEvent.click(screen.getByTestId('rail-drills-country_g20_br'))
    // UI-B3: a subject's per-subject reads are ONE drill, its page.
    fireEvent.click(screen.getByTestId('rail-drill-subject.page'))
    expect(shell.calls).toEqual(['bound:subject.page:country_g20_br'])
  })
})

describe('Reads, Units, Cases, Queues', () => {
  it('the top floor lists its newest read; a click SCOPES the wall to it and focuses it', async () => {
    renderRail()
    const row = await screen.findByTestId('rail-read-w1')
    fireEvent.click(row)
    expect(useScope.getState().scope).toMatchObject({ kind: 'report', id: 'w1' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'report', id: 'w1' })
  })

  it("units are the profile's roster", () => {
    localStorage.setItem('legba_rail_collapsed', JSON.stringify(['sources']))
    renderRail()
    const roster = screen.getByTestId('rail-unit-roster')
    expect(within(roster).getAllByRole('button', { name: /./ }).length).toBeGreaterThanOrEqual(
      profileUiJson.rosters.units.length,
    )
    fireEvent.click(screen.getByTestId('rail-unit-energy_security'))
    expect(useSelection.getState().selection).toMatchObject({ kind: 'analyst', id: 'energy_security' })
  })

  it('no mission rows and no layouts block: the arrangement machinery is gone', () => {
    renderRail()
    expect(document.querySelector('[data-testid^="rail-mission-"]')).toBeNull()
    expect(screen.queryByTestId('layout-preset-select')).toBeNull()
    expect(screen.queryByTestId('layout-save')).toBeNull()
    expect(screen.queryByTestId('rail-saved-missions')).toBeNull()
  })

  it('with no recorded case, Cases says so rather than showing nothing', () => {
    renderRail()
    expect(screen.getByTestId('rail-cases-empty')).toBeInTheDocument()
  })

  it('queue counts render "—" before the read lands, never 0, and a queue opens its surface', async () => {
    stubFetch({ '/journal_proposals': new Promise(() => {}) })
    renderRail()
    const q = screen.getByTestId('rail-queue-system.journal_gate')
    expect(q.textContent).toContain('—')
    expect(q.textContent).not.toMatch(/\b0 pending/)
    fireEvent.click(q)
    expect(shell.calls).toEqual(['open:system.journal_gate'])
  })

  it('a malformed queue body is "unavailable", never a count', async () => {
    stubFetch({ '/v3/optimizer/candidates': { not: 'a list' } })
    renderRail()
    await waitFor(() =>
      expect(screen.getByTestId('rail-queue-system.optimizer').textContent).toContain('unavailable'),
    )
  })

  it('in the cis mode the personal-only queues are not offered', () => {
    render(wrap(<ObjectRail mode="cis" />))
    expect(screen.getByTestId('rail-queues-none')).toBeInTheDocument()
  })
})

describe("the reader's rail: subjects ranked by what changed", () => {
  it('most changes first with the count, then "No change" by name; no band word', async () => {
    const changes = new Map([
      ['lane_hormuz', 2],
      ['country_watch_sd', 5],
    ])
    render(wrap(<SubjectRoster changes={changes} />))
    await waitFor(() => expect(screen.getByTestId('rail-subject-country_g20_br')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('rail-subject-lane_hormuz')).toBeInTheDocument())
    const moved = [...screen.getByTestId('rail-subjects-changed').querySelectorAll('[data-rail-row="subject"]')]
    expect(moved.map((b) => b.getAttribute('data-testid'))).toEqual(['rail-subject-country_watch_sd', 'rail-subject-lane_hormuz'])
    expect(moved[0].textContent).toBe('Sudan5')
    expect(screen.getByTestId('rail-subjects-still-head').textContent).toBe('No change')
    expect(within(screen.getByTestId('rail-subjects-still')).getByTestId('rail-subject-country_g20_br').textContent).toBe('Brazil')
    expect(screen.getByTestId('rail-subjects-ranked').textContent).not.toMatch(/High|Moderate|Low|Unverified/)
  })

  it('before the counts are read: the roster by name, with no count and no "No change" head', async () => {
    render(wrap(<SubjectRoster changes={null} />))
    await waitFor(() => expect(screen.getByTestId('rail-subject-country_g20_br')).toBeInTheDocument())
    expect(screen.getByTestId('rail-subjects-ranked')).toHaveAttribute('data-read', '0')
    expect(screen.queryByTestId('rail-subjects-still-head')).toBeNull()
    expect(screen.getByTestId('rail-subject-country_g20_br').textContent).toBe('Brazil')
  })
})
