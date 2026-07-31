/**
 * Component tests for the Sidebar (U-2: countries become first-class; U-3:
 * Targets/Analysts nest inside Engine Room; supply-chain follow-up: thematic
 * lane/flow desks).
 *
 * Covers:
 *  - the new "Desks" group renders at the TOP (before Awareness), showing
 *    human country names + a confidence band chip, sourced from the same
 *    `/findings?analyst_id=country_composition` read the Wall uses;
 *  - clicking a desk fires the SAME keystone action a Wall band-grid chip
 *    does (`selectRow('target', …)`) — no new selection flow;
 *  - the raw Targets/Analysts instance groups now nest INSIDE Engine Room
 *    (the operations group, U-3 §2) rather than as separate top-level
 *    sections — invisible while it's collapsed, revealed (themselves still
 *    collapsed) once it's expanded;
 *  - the `legba_nav_collapsed` persistence key survives: a pre-existing
 *    saved collapse-list (minted before Desks existed) still leaves Desks
 *    expanded, and toggling Desks persists under the SAME key;
 *  - a "Supply chain" subsection nests under Desks, sourced from
 *    `/registry/descriptors?family=target` (the same descriptor-heads read
 *    the Engine Room's Targets section uses), filtered to active + tagged
 *    `supply_chain` — draft-state and untagged targets are proven absent;
 *    rows carry no confidence chip (no composition tier) but do show a cheap
 *    recency stamp when a `disruption_status` finding exists; clicking one
 *    fires the identical `selectRow('target', …)`.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { Sidebar } from './Sidebar'
import type { PanelRegistration } from '@/types'
import { useSelection } from '@/state/selection'

const COLLAPSE_KEY = 'legba_nav_collapsed'

const FINDINGS_PAGE = {
  data: [
    {
      id: 'f1',
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

/** Registry target descriptor heads (`/registry/descriptors?family=target`) —
 *  the same descriptor-heads read the Engine Room's Targets section and the
 *  bound-panel synthesizer use. Proves the Supply chain subsection filters on
 *  BOTH state === 'active' AND scope.tags containing 'supply_chain': a draft
 *  lane and an active-but-untagged country target must NOT surface. */
const TARGET_DESCRIPTORS = [
  {
    descriptor_id: 'lane_hormuz',
    state: 'active',
    body: { scope: { tags: ['supply_chain', 'disruption', 'lane'] } },
  },
  {
    descriptor_id: 'lane_red_sea',
    state: 'active',
    body: { scope: { tags: ['supply_chain', 'disruption', 'lane'] } },
  },
  {
    descriptor_id: 'lane_malacca_south_china_sea',
    state: 'active',
    body: { scope: { tags: ['supply_chain', 'disruption', 'lane'] } },
  },
  {
    // Registered but still draft — must NOT appear (the desk set grows only
    // as lanes are deliberately activated).
    descriptor_id: 'lane_black_sea',
    state: 'draft',
    body: { scope: { tags: ['supply_chain', 'disruption', 'lane'] } },
  },
  {
    // Active, but no supply_chain tag — must NOT appear in the subsection.
    descriptor_id: 'country_g20_br',
    state: 'active',
    body: { scope: { tags: ['g20'] } },
  },
]

/** `disruption_status` findings — the cheap recency stamp source. Only two of
 *  the three active lanes have one, proving the third renders name-only. */
const DISRUPTION_FINDINGS_PAGE = {
  data: [
    { target_id: 'lane_hormuz', produced_at: '2026-07-29T12:00:00Z' },
    { target_id: 'lane_red_sea', produced_at: '2026-07-28T00:00:00Z' },
  ],
  next_cursor: null,
}

function stubFetch() {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    let body: unknown = { data: [], next_cursor: null }
    if (url.includes('/findings?analyst_id=country_composition')) body = FINDINGS_PAGE
    else if (url.includes('/findings?analyst_id=disruption_status')) body = DISRUPTION_FINDINGS_PAGE
    else if (url.includes('/registry/descriptors') && url.includes('family=target')) {
      body = TARGET_DESCRIPTORS
    }
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function targetReg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_overview',
    descriptor_id: 'country_g20_ar',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Overview',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: { target_id: 'country_g20_ar' },
    retired: false,
    created_at: '2026-06-01T00:00:00Z',
    retired_at: null,
  }
}

function analystReg(): PanelRegistration {
  return {
    id: 'a1',
    panel_id: 'analyst_outputs',
    descriptor_id: 'country_assessor',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'analyst',
    analyst_id: 'country_assessor',
    title: 'Analyst Outputs',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: { analyst_id: 'country_assessor' },
    retired: false,
    created_at: '2026-06-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const noop = () => {}

function renderSidebar(registrations: PanelRegistration[] = [targetReg(), analystReg()]) {
  return render(
    wrap(
      <Sidebar
        registrations={registrations}
        onOpen={noop}
        onApplyPreset={noop}
        onOpenPalette={noop}
        onSaveLayout={noop}
        onRestoreLayout={noop}
        canRestoreLayout={false}
      />,
    ),
  )
}

beforeEach(() => {
  stubFetch()
  localStorage.clear()
  useSelection.getState().clear()
})

describe('Sidebar — Desks group', () => {
  it('renders human country names, not raw target ids', async () => {
    renderSidebar()
    expect(await screen.findByTestId('nav-desk-BR')).toHaveTextContent('Brazil')
    expect(screen.getByTestId('nav-desk-SD')).toHaveTextContent('Sudan')
    // No raw snake_case desk id anywhere in the rendered chrome.
    expect(screen.queryByText('country_g20_br')).not.toBeInTheDocument()
    expect(screen.queryByText('country_watch_sd')).not.toBeInTheDocument()
  })

  it('shows a confidence band chip per desk', async () => {
    renderSidebar()
    const brazil = await screen.findByTestId('nav-desk-BR')
    expect(within(brazil).getByText('High')).toBeInTheDocument()
    const sudan = screen.getByTestId('nav-desk-SD')
    expect(within(sudan).getByText('Unverified')).toBeInTheDocument()
  })

  it('the Desks group is the FIRST nav group in the sidebar', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-BR')
    const groupHeaders = screen
      .getAllByRole('button')
      .filter((b) => b.dataset.testid?.startsWith('nav-group-'))
    expect(groupHeaders[0]).toHaveAttribute('data-testid', 'nav-group-desks')
  })

  it('clicking a desk fires the same keystone action as a Wall chip (selectRow → target)', async () => {
    renderSidebar()
    const brazil = await screen.findByTestId('nav-desk-BR')
    fireEvent.click(brazil)
    const sel = useSelection.getState().selection
    expect(sel).toMatchObject({ kind: 'target', id: 'country_g20_br', label: 'Brazil' })
  })
})

describe('Sidebar — Desks group — Supply chain subsection', () => {
  it('renders the active + supply_chain-tagged lanes with their human names', async () => {
    renderSidebar()
    expect(await screen.findByTestId('nav-desk-lane_hormuz')).toHaveTextContent('Strait of Hormuz')
    expect(screen.getByTestId('nav-desk-lane_red_sea')).toHaveTextContent('Red Sea / Bab el-Mandeb')
    expect(screen.getByTestId('nav-desk-lane_malacca_south_china_sea')).toHaveTextContent(
      'Malacca / South China Sea',
    )
  })

  it('excludes a registered-but-draft lane (state filter)', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-lane_hormuz')
    expect(screen.queryByTestId('nav-desk-lane_black_sea')).not.toBeInTheDocument()
    expect(screen.queryByText('Black Sea')).not.toBeInTheDocument()
  })

  it('excludes an active target that does not carry the supply_chain tag', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-lane_hormuz')
    // country_g20_br is active but untagged — it must render only once, as the
    // country desk, never a second time under Supply chain.
    expect(screen.getAllByText('Brazil')).toHaveLength(1)
  })

  it('renders a "Supply chain" divider under the country desks, inside the same Desks group', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-lane_hormuz')
    // Exactly one Desks group header — no new top-level nav group was added.
    expect(screen.getAllByTestId(/^nav-group-desks$/)).toHaveLength(1)
    expect(screen.getByTestId('nav-desks-supply-chain-header')).toHaveTextContent('Supply chain')
  })

  it('never shows a fabricated confidence band chip on a supply-chain desk', async () => {
    renderSidebar()
    const hormuz = await screen.findByTestId('nav-desk-lane_hormuz')
    for (const label of ['High', 'Moderate', 'Low', 'Unverified']) {
      expect(within(hormuz).queryByText(label)).not.toBeInTheDocument()
    }
  })

  it('shows a cheap recency stamp when a disruption_status finding exists, name-only otherwise', async () => {
    renderSidebar()
    const hormuz = await screen.findByTestId('nav-desk-lane_hormuz')
    // 2026-07-29T12:00:00Z relative to the frozen test clock below.
    expect(within(hormuz).getByText(/ago$/)).toBeInTheDocument()
    // Malacca has no disruption_status finding in the fixture — name-only.
    const malacca = screen.getByTestId('nav-desk-lane_malacca_south_china_sea')
    expect(within(malacca).queryByText(/ago$/)).not.toBeInTheDocument()
  })

  it('clicking a supply-chain desk fires the identical selectRow a country desk does', async () => {
    renderSidebar()
    const hormuz = await screen.findByTestId('nav-desk-lane_hormuz')
    fireEvent.click(hormuz)
    const sel = useSelection.getState().selection
    expect(sel).toMatchObject({ kind: 'target', id: 'lane_hormuz', label: 'Strait of Hormuz' })
  })
})

describe('Sidebar — raw Targets/Analysts nest inside Engine Room (U-3 §2)', () => {
  it('Targets/Analysts are not even in the DOM while Engine Room (operations) is collapsed (the default)', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-BR')
    expect(screen.getByTestId('nav-group-operations')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('nav-group-targets')).not.toBeInTheDocument()
    expect(screen.queryByTestId('nav-group-analysts')).not.toBeInTheDocument()
  })

  it('expanding Engine Room reveals Targets/Analysts nested inside it, themselves collapsed by default', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-BR')
    fireEvent.click(screen.getByTestId('nav-group-operations'))
    expect(await screen.findByTestId('nav-group-targets')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('nav-group-analysts')).toHaveAttribute('aria-expanded', 'false')
  })

  it('Targets renders after Engine Room’s own singleton rows once expanded (nested, not a sibling section)', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-BR')
    fireEvent.click(screen.getByTestId('nav-group-operations'))
    const targetsHeader = await screen.findByTestId('nav-group-targets')
    const operationsHeader = screen.getByTestId('nav-group-operations')
    // eslint-disable-next-line no-bitwise
    expect(
      operationsHeader.compareDocumentPosition(targetsHeader) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })
})

describe('Sidebar — legba_nav_collapsed persistence survives the new group', () => {
  it('a fresh install (no saved state) boots with Desks expanded', async () => {
    renderSidebar()
    await screen.findByTestId('nav-desk-BR')
    expect(screen.getByTestId('nav-group-desks')).toHaveAttribute('aria-expanded', 'true')
  })

  it('a PRE-EXISTING saved collapse list (minted before Desks existed) still leaves Desks expanded', async () => {
    // Simulate a user's saved layout from before this wave: the array has no
    // 'desks' entry at all.
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify(['operations', 'targets', 'analysts']))
    renderSidebar()
    await screen.findByTestId('nav-desk-BR')
    expect(screen.getByTestId('nav-group-desks')).toHaveAttribute('aria-expanded', 'true')
    // The old collapse choices for the five-group tree are honored unchanged.
    expect(screen.getByTestId('nav-group-operations')).toHaveAttribute('aria-expanded', 'false')
  })

  it('collapsing Desks persists under the SAME legba_nav_collapsed key', async () => {
    renderSidebar()
    const header = await screen.findByTestId('nav-group-desks')
    fireEvent.click(header)
    const saved = JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? '[]')
    expect(saved).toContain('desks')
  })
})
