/**
 * A `map.geometry: none` profile lands on a usable surface, and the map is
 * refused BY NAME (UI-B4/map; UI-D1 §2.4 and its §8 W8 gate).
 *
 * The fixture is PROFILE-1's real export of the sanctions skeleton
 * (`scripts/gen_profile_ui.py --profile profiles/sanctions_export_control`),
 * mocked in as the build's `profile_ui.json`, and the REAL shell boots over it:
 *
 *  - the landing is the Read stance, seeded whole with no map tile;
 *  - the `＋` catalog and a subject's drills do not list the map;
 *  - `open_panel(map)` and `map(...)` are refused `no_map_surface`, for a case
 *    and for the operator — the reason the Consult tile's case rail prints;
 *  - missions open no map and make no layer claim;
 *  - the layer switcher renders nothing to switch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
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
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { act as action, applyAction, drive, getShell } from '@/lib/applyAction'
import { PROFILE_UI } from '@/lib/profileUi'
import { SURFACES, LANDING_SURFACE, findSurface, surfaceViews } from '@/lib/surfaces'
import { resetFrameState } from '@/state/frame'
import { catalogKinds, drillsOf } from '@/panel-registry/kindPlan'
import { NO_MAP_SURFACE } from '@/lib/subjectGeometry'
import { resetCasePlayer } from '@/state/casePlayer'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import LayerPanel from '@/v4/world/LayerPanel'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

// U-6: the panel side's ids and the stage's view.
const panels = () => [
  ...Object.keys((getShell()!.serializeLayout() as { panels?: Record<string, unknown> })?.panels ?? {}),
  ...[document.querySelector('[data-testid="stage"]')?.getAttribute('data-stage-view') ?? ''].filter(Boolean),
]

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  resetCasePlayer()
  resetFrameState()
  useSelection.getState().clear()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const body = String(url).includes('/registry/') ? [] : { data: [], items: [], next_cursor: null }
      return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  resetCasePlayer()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the fixture is the none profile', () => {
  it('declares map.geometry none', () => {
    expect(PROFILE_UI.profile).toBe('sanctions_export_control')
    expect(PROFILE_UI.map.geometry).toBe('none')
  })
})

describe('a geometry: none profile lands on a usable surface', () => {
  it('no surface seeds the map; a region it leaves empty takes the subject field, never a blank', () => {
    for (const sf of SURFACES) expect(surfaceViews(sf)).not.toContain('v4.map')
    const board = findSurface(LANDING_SURFACE)!
    expect(LANDING_SURFACE).toBe('board')
    // The Board's map region holds the subject field's slot; the read is on the stage.
    expect(board.regions[0].views).toEqual(['slot:subject_field'])
    expect(board.stage).toBe('view:board_changed')
    // The Subject surface's one region takes the subject field where the map was.
    expect(findSurface('subject')!.regions.map((r) => r.views)).toEqual([['slot:subject_field']])
  })

  it('the shell boots on the Board with no map tile', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(getShell()).not.toBeNull())
    await waitFor(() => expect(panels()).toContain('view:board_changed'))
    expect(getShell()!.currentStance()).toBe('board')
    expect(panels()).toContain('slot:subject_field')
    expect(panels()).not.toContain('v4.map')
  })

  it('the catalog and a subject\'s drills do not list the map', () => {
    expect(catalogKinds()).not.toContain('v4.map')
    expect(drillsOf('subject')).not.toContain('target.map')
  })

  it('the layer switcher has nothing to switch', () => {
    render(wrap(<LayerPanel />))
    expect(screen.queryByLabelText('Expand layers panel')).toBeNull()
    expect(screen.queryByTestId('layer-catalog')).toBeNull()
  })
})

describe('the map is refused by name: no_map_surface', () => {
  it('a case: open_panel(map), open_panel(target.map) and map(...) are refused', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(getShell()).not.toBeNull())
    const open = applyAction(action('open_panel', { kind: 'v4.map', params: {} }), getShell(), { driver: 'case' })
    expect(open.state).toBe('refused')
    expect(open).toMatchObject({ reason: expect.stringMatching(new RegExp(`^${NO_MAP_SURFACE}`)) })
    const bound = applyAction(
      action('open_panel', { kind: 'target.map', params: { subject: { kind: 'target', id: 'regime_ofac_russia' } } }),
      getShell(),
      { driver: 'case' },
    )
    expect(bound).toMatchObject({ state: 'refused', reason: expect.stringMatching(/^no_map_surface/) })
    for (const view of ['reset', 'fit_ref']) {
      const out = applyAction(
        action('map', { view, ref: { kind: 'target', id: 'regime_ofac_russia' }, layers: ['signals'] }),
        getShell(),
        { driver: 'case' },
      )
      expect(out).toMatchObject({ state: 'refused', reason: expect.stringMatching(/^no_map_surface/) })
    }
    expect(panels()).not.toContain('v4.map')
  })

  it('the operator is refused the same way', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(getShell()).not.toBeNull())
    const [out] = drive(action('open_panel', { kind: 'v4.map', params: {} }))
    expect(out).toMatchObject({ state: 'refused', reason: expect.stringMatching(/^no_map_surface/) })
    expect(panels()).not.toContain('v4.map')
  })
})
