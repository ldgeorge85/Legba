/**
 * subject.page — the wrapper's contract, with each tab's original component
 * stubbed (UI-B3). What is asserted is the WRAPPER: which component each tab
 * mounts and on which subject, the alias tab a page opens on, an open page
 * following a later tab request, the following (stance-seeded) page, the
 * mode gate, and the pin per tab and per drill (UI-B6's hook).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelProps, PanelRegistration } from '@/types'

function stub(name: string) {
  return {
    default: (p: PanelProps) => (
      <div data-testid={`stub-${name}`} data-subject={p.scope.target_id ?? ''} data-reg={p.registration.descriptor_id} />
    ),
  }
}
vi.mock('@/panels/target/Findings', () => stub('findings'))
vi.mock('@/panels/target/Claims', () => stub('claims'))
vi.mock('@/panels/target/Signals', () => stub('signals'))
vi.mock('@/panels/target/Situations', () => stub('situations'))
vi.mock('@/panels/target/Sources', () => stub('sources'))
vi.mock('@/panels/target/Graph', () => stub('graph'))
vi.mock('@/panels/target/Overview', () => stub('overview'))
// The brief stub drills: a button that claims the unit read's pin the way the
// real brief's unit block does (usePinScope, surface `subject brief`).
vi.mock('@/panels/target/DeskBriefPage', async () => {
  const { useState } = await import('react')
  const { usePinScope } = await import('@/lib/consultContext')
  return {
    default: (p: PanelProps) => {
      const [drilled, setDrilled] = useState(false)
      usePinScope(drilled ? { kind: 'finding', id: 'unit-read-1', label: 'Unit read' } : null, 'subject brief', drilled ? 1 : 0)
      return (
        <div data-testid="stub-brief" data-subject={p.scope.target_id ?? ''}>
          <button type="button" data-testid="stub-brief-drill" onClick={() => setDrilled(true)}>
            drill
          </button>
        </div>
      )
    },
  }
})

import SubjectPage from './SubjectPage'
import { SUBJECT_TABS } from '@/lib/subjectPage'
import { requestPanelTab, resetPanelParams } from '@/state/panelParams'
import { resetAmbientPins, resolveAmbientPin, useAmbientPins } from '@/lib/consultContext'
import { useSelection } from '@/state/selection'
import { resetScope } from '@/state/scope'

function registration(subject: string | null): PanelRegistration {
  return {
    id: subject ? `subject.page:${subject}` : 'singleton:subject.page',
    panel_id: 'subject_page',
    descriptor_id: subject ?? '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Desk',
    mode: 'personal',
    layout_slot: 'subject.page',
    data_query: {},
    binding: subject ? { target_id: subject } : {},
    retired: false,
    created_at: new Date().toISOString(),
    retired_at: null,
  } as unknown as PanelRegistration
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function renderBound(subject: string, opts: { tab?: string; mode?: 'personal' | 'cis' } = {}) {
  return render(
    wrap(
      <SubjectPage
        registration={registration(subject)}
        scope={{ target_id: subject }}
        mode={opts.mode ?? 'personal'}
        initialTab={opts.tab}
      />,
    ),
  )
}

const pinNow = () => resolveAmbientPin(useAmbientPins.getState().claims, null)

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => [] })))
  resetPanelParams()
  resetAmbientPins()
  resetScope()
  useSelection.setState({ selection: null, history: [], future: [] })
})

const MOUNTS: Record<string, string> = {
  brief: 'stub-brief',
  evidence: 'stub-findings',
  claims: 'stub-claims',
  signals: 'stub-signals',
  situations: 'stub-situations',
  sources: 'stub-sources',
  graph: 'stub-graph',
  runtime: 'stub-overview',
}

describe('subject.page — the tabs', () => {
  it('names Table A\'s eight tabs, in order', () => {
    expect(SUBJECT_TABS.map((t) => t.label)).toEqual([
      'Brief', 'Evidence', 'Findings', 'Signals', 'Situations', 'Sources', 'Graph', 'Runtime',
    ])
  })

  it('each tab mounts its ORIGINAL component, bound to the page\'s subject', async () => {
    renderBound('country_g20_br')
    for (const t of SUBJECT_TABS) {
      fireEvent.mouseDown(screen.getByTestId(`subject-tab-${t.id}`))
      fireEvent.click(screen.getByTestId(`subject-tab-${t.id}`))
      const mounted = await screen.findByTestId(MOUNTS[t.id])
      expect(mounted, t.id).toHaveAttribute('data-subject', 'country_g20_br')
    }
  })

  it('opens on the tab an alias asked for (target.claims → #claims), the Brief otherwise', async () => {
    renderBound('lane_hormuz', { tab: 'claims' })
    expect(await screen.findByTestId('stub-claims')).toHaveAttribute('data-subject', 'lane_hormuz')
    expect(screen.getByTestId('subject-tab-claims')).toHaveAttribute('aria-selected', 'true')
  })

  it('a stale tab name falls back to the Brief, never an empty surface', async () => {
    renderBound('lane_hormuz', { tab: 'lineage' })
    expect(await screen.findByTestId('stub-brief')).toBeInTheDocument()
  })

  it('an OPEN page follows a later tab request on its own key; another subject\'s page does not move', async () => {
    renderBound('country_g20_br')
    await screen.findByTestId('stub-brief')
    act(() => requestPanelTab('subject.page:lane_hormuz', 'graph'))
    expect(screen.queryByTestId('stub-graph')).toBeNull()
    act(() => requestPanelTab('subject.page:country_g20_br', 'situations'))
    expect(await screen.findByTestId('stub-situations')).toBeInTheDocument()
  })

  it('cis mode does not offer the personal-only tabs (Signals, Sources) — the retired kinds\' own modes', async () => {
    renderBound('country_g20_br', { mode: 'cis', tab: 'signals' })
    expect(screen.queryByTestId('subject-tab-signals')).toBeNull()
    expect(screen.queryByTestId('subject-tab-sources')).toBeNull()
    // The request for an unshipped tab lands on the Brief.
    expect(await screen.findByTestId('stub-brief')).toBeInTheDocument()
  })
})

describe('subject.page — the pin (UI-B6)', () => {
  it('pins the subject; a drill re-pins to the unit read; a tab change pins the subject again', async () => {
    renderBound('country_g20_br')
    await screen.findByTestId('stub-brief')
    await waitFor(() => expect(pinNow()).toMatchObject({ surface: 'subject page', pin: { kind: 'target', id: 'country_g20_br' } }))
    // The drill on the Brief tab is the newer claim.
    fireEvent.click(screen.getByTestId('stub-brief-drill'))
    await waitFor(() => expect(pinNow()).toMatchObject({ surface: 'subject brief', pin: { kind: 'finding', id: 'unit-read-1' } }))
    // Leaving the tab releases the drill and re-asserts the subject.
    fireEvent.mouseDown(screen.getByTestId('subject-tab-claims'))
    fireEvent.click(screen.getByTestId('subject-tab-claims'))
    await screen.findByTestId('stub-claims')
    await waitFor(() => expect(pinNow()).toMatchObject({ surface: 'subject page', pin: { kind: 'target', id: 'country_g20_br' } }))
  })

  it('the pin carries the subject\'s DECLARED name (no id parsing)', async () => {
    renderBound('lane_hormuz')
    await waitFor(() => expect(pinNow()?.pin.label).toBe('Strait of Hormuz'))
    expect(screen.getByTestId('subject-page-name')).toHaveTextContent('Strait of Hormuz')
    expect(screen.getByTestId('subject-page-name')).toHaveTextContent('lane_hormuz')
  })
})

describe('subject.page — unbound, the Subject stance\'s seed: it FOLLOWS the selected subject', () => {
  function renderFollowing() {
    return render(wrap(<SubjectPage registration={registration(null)} scope={{}} mode="personal" />))
  }

  it('with nothing selected it states its absence — never a blank page, never a guessed subject', () => {
    renderFollowing()
    expect(screen.getByTestId('subject-page-no-subject')).toHaveTextContent('No desk is selected')
    expect(screen.queryByTestId('stub-brief')).toBeNull()
    expect(pinNow()).toBeNull()
  })

  it('follows a selected subject, and keeps it when the reader drills off it', async () => {
    renderFollowing()
    act(() => useSelection.getState().select({ kind: 'target', id: 'lane_hormuz', origin: 'test' }))
    expect(await screen.findByTestId('stub-brief')).toHaveAttribute('data-subject', 'lane_hormuz')
    expect(screen.getByTestId('subject-page-following')).toBeInTheDocument()
    act(() => useSelection.getState().select({ kind: 'finding', id: 'f-1', origin: 'test' }))
    expect(screen.getByTestId('stub-brief')).toHaveAttribute('data-subject', 'lane_hormuz')
    act(() => useSelection.getState().select({ kind: 'target', id: 'country_g20_br', origin: 'test' }))
    await waitFor(() => expect(screen.getByTestId('stub-brief')).toHaveAttribute('data-subject', 'country_g20_br'))
  })
})
