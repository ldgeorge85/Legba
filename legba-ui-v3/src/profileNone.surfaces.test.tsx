/**
 * UI-B2 — a SECOND profile renders its own words on every surface this lane
 * touched (UI-D1 §8 W10: "a second profile renders its own").
 *
 * The fixture is PROFILE-1's real export of the sanctions skeleton
 * (`scripts/gen_profile_ui.py --profile profiles/sanctions_export_control`),
 * mocked in as the build's `profile_ui.json`. Every surface below is the real
 * component over it; the last block boots the REAL shell on its landing and
 * greps the rendered DOM for the exemplar's words.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

vi.mock('@/lib/profile_ui.json', async () => ({
  default: (await import('@/test/profileNone.fixture.json')).default,
}))
vi.mock('@/panels/v4/MapPanel', () => ({
  default: () => <div data-testid="stub-world-map" />,
}))

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { SurfaceBar } from '@/components/SurfaceBar'
import { ObjectRail } from '@/components/ObjectRail'
import WallPanel from '@/panels/system/Wall'
import ReportsList from '@/mobile/components/ReportsList'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { PLATFORM_NOUNS, PROFILE_UI } from '@/lib/profileUi'
import { findSurface } from '@/lib/surfaces'
import { UNSCOPED_DESK_LABEL } from '@/lib/morningReadBands'
import { PROFILE_TIERS, READ_ANALYST_CSV, groupByTier, toReportRow } from '@/mobile/mobileModel'
import { setSubjectIndex, subjectIndexFrom } from '@/lib/subjectIndex'
import { humanizeId, rosterTagOf } from '@/lib/deskNames'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { getShell } from '@/lib/applyAction'
import { resetCasePlayer } from '@/state/casePlayer'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import type { PanelRegistration } from '@/types'
import fixture from '@/test/profileNone.fixture.json'

/** The exemplar's words on a surface this lane moved — none may render under another profile. */
const EXEMPLAR = /\b(G20|Watch list|Regions?|Countr(y|ies)|World Map|World Assessment|World at a glance|No desk)\b/i
/**
 * The landing grep (UI-D1 §8 W10; W8's note: the Navigator's REGIONS / COUNTRIES
 * sections and the rail's "G20 (19)"): the exemplar's roster and frame headings
 * and the labels this lane moved. The singular producer names a Live Feed
 * filter still lists (`lib/feedProducers.ts`, "Country composition") are a
 * found residue, not this lane's surface — see the lane report.
 */
const LANDING_EXEMPLAR = /\b(G20|Watch list|Regions|Countries|World Map|World Assessment|World at a glance|No desk)\b/i

const SANCTIONS_ROSTER = subjectIndexFrom([
  { descriptor_id: 'regime_ofac_russia', name: 'Regime — OFAC Russia', state: 'active', body: { scope: { domain: 'thematic', tags: ['regime'] } } },
  { descriptor_id: 'counterparty_acme', name: 'Counterparty — Acme Shipping', state: 'active', body: { scope: { tags: ['counterparty'] } } },
])

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  resetCasePlayer()
  useSelection.getState().clear()
  setSubjectIndex(SANCTIONS_ROSTER)
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      if (String(url).includes('/registry/projection/')) {
        const raw = JSON.stringify({ detail: { state: 'projection_disabled', reason: 'the profile declares no projection' } })
        return { ok: false, status: 409, json: async () => JSON.parse(raw), text: async () => raw } as unknown as Response
      }
      const body = String(url).includes('/registry/') ? [] : { data: [], items: [], next_cursor: null }
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  resetCasePlayer()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the fixture is the sanctions profile', () => {
  it('its own plane, its own nouns', () => {
    expect(PROFILE_UI.profile).toBe('sanctions_export_control')
    expect(PROFILE_UI.plane).toBe(fixture.plane)
    expect(PROFILE_UI.subject).toEqual({ singular: 'Regime', plural: 'Regimes' })
  })
})

describe('surfaces: the Subject surface takes the profile\'s noun', () => {
  it('the Subject surface reads "Regime"; the others keep the shell\'s words', () => {
    expect(findSurface('subject')!.label).toBe('Regime')
    expect(findSurface('board')!.label).toBe('Board')
    expect(findSurface('ledger')!.label).toBe('Ledger')
  })

  it('the top bar shows the profile\'s noun', () => {
    render(
      wrap(
        <SurfaceBar
          mode="personal"
          active="board"
          onSwitch={() => {}}
          onReset={() => {}}
          onCopyLink={() => {}}
          onOpenPalette={() => {}}
        />,
      ),
    )
    expect(screen.getByTestId('surface-tab-subject')).toHaveTextContent('Regime')
    expect(screen.queryByText('Desk')).toBeNull()
  })
})

describe('titles and labels fall to the profile, then the platform noun', () => {
  it('the map and read titles, the unscoped bucket', () => {
    expect(PANEL_REGISTRY['v4.map'].definition.defaultTitle).toBe(PLATFORM_NOUNS.map)
    expect(PANEL_REGISTRY['v4.assessment'].definition.defaultTitle).toBe(PLATFORM_NOUNS.read)
    expect(UNSCOPED_DESK_LABEL).toBe('No regime or counterparty (unscoped)')
  })

  it('the Wall titles itself with the profile\'s overview title and its subject noun', async () => {
    const reg = {
      id: 'singleton:system.wall',
      panel_id: 'system_wall',
      descriptor_id: '(singleton)',
      descriptor_version: '0',
      descriptor_family: 'target',
      analyst_id: null,
      title: 'The Wall',
      mode: 'personal',
      layout_slot: 'system.wall',
      data_query: {},
      binding: {},
    } as unknown as PanelRegistration
    render(wrap(<WallPanel registration={reg} scope={{}} mode="personal" />))
    expect((await screen.findAllByText(/Roster at a glance/i)).length).toBeGreaterThan(0)
    expect(document.body.textContent).not.toMatch(EXEMPLAR)
  })
})

describe('the read tiers: the sanctions skeleton declares no producer', () => {
  it('no tier, no CSV, no group — and no exemplar producer is asked for', () => {
    expect(PROFILE_TIERS).toEqual([])
    expect(READ_ANALYST_CSV).toBe('')
    expect(
      toReportRow({ id: 'x', analyst_id: 'country_composition', produced_at: '2026-10-04T00:00:00Z' } as never),
    ).toBeNull()
    expect(groupByTier([])).toEqual([])
  })

  it('the mobile list renders no tier and no exemplar word', () => {
    render(
      <ReportsList
        rows={[]}
        day={null}
        days={[]}
        onOpen={() => {}}
        onDay={() => {}}
        consolidation={null}
        onOpenConsolidation={() => {}}
        offlineNote={null}
      />,
    )
    expect(screen.getByTestId('reports-empty')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(EXEMPLAR)
  })
})

describe('subjects: names and legs from the declaration, in the profile\'s words', () => {
  it('a regime and a counterparty name themselves and sit on their own legs', () => {
    expect(humanizeId('regime_ofac_russia')).toBe('OFAC Russia')
    expect(rosterTagOf('regime_ofac_russia')).toBe('regime')
    expect(rosterTagOf('counterparty_acme')).toBe('counterparty')
  })

  it('the rail: section nouns are the profile\'s, the projection refusal is named, no exemplar word', async () => {
    render(wrap(<ObjectRail mode="personal" onOpenPalette={() => {}} />))
    expect(screen.getByTestId('rail-section-title-subjects').textContent).toBe('Regimes')
    expect(screen.getByTestId('rail-section-title-units').textContent).toBe('questions')
    expect(await screen.findByTestId('rail-reads-no-top-floor')).toHaveTextContent(PLATFORM_NOUNS.noTopFloor)
    await waitFor(() =>
      expect(screen.getByTestId('rail-projection-tiers')).toHaveAttribute('data-projection-state', 'projection_disabled'),
    )
    expect(document.body.textContent).not.toMatch(EXEMPLAR)
  })
})

describe('THE NONE LANDING — the rendered DOM carries no exemplar word', () => {
  it('the shell boots on its landing; REGIONS / COUNTRIES / G20 appear nowhere', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(getShell()).not.toBeNull())
    await waitFor(() => expect(document.querySelector('[data-testid="board-list"][data-read="1"]')).not.toBeNull(), {
      timeout: 5000,
    })
    const text = document.body.textContent ?? ''
    // The reader side has no rail (U-2); the profile's noun names the Subject surface.
    expect(text).toContain('Regime')
    const hits = [...text.matchAll(new RegExp(LANDING_EXEMPLAR.source, 'gi'))].map((m) =>
      text.slice(Math.max(0, m.index! - 60), m.index! + 40),
    )
    expect(hits).toEqual([])
    // the full daily read, one link away: its bands' source is named on its
    // rail (the plane refused), and it carries no exemplar word either
    fireEvent.click(document.querySelector('[data-testid="board-daily-read"]')!)
    await waitFor(() => expect(document.querySelector('[data-testid="morning-read-rail"]')).not.toBeNull(), {
      timeout: 5000,
    })
    await waitFor(() =>
      expect(document.querySelector('[data-testid="morning-read-rail"]')).toHaveAttribute(
        'data-bands-source',
        'projection_disabled',
      ),
    )
    expect(document.body.textContent ?? '').not.toMatch(LANDING_EXEMPLAR)
  })
})
