/**
 * The four missions, round-tripped through the URL hash against the REAL shell.
 *
 * A mission is only addressable if the whole loop closes: choosing one writes
 * `#mission=`, and a link carrying `#mission=` restores the stance AND the
 * three axes a stance never carried — the target scope, the temporal window and
 * the map's layer selection. Testing the pure half proves the object; only a
 * mounted `App` proves the loop, which is why this file mounts one.
 *
 * Also pinned here:
 *  - `#mission=` alone names a stance (the mission's own), so a link does not
 *    have to spell out the arrangement as well as the job;
 *  - a link that ALSO carries `#scope=` keeps that scope — an address the
 *    sender chose deliberately beats a mission's default aperture;
 *  - walking off a mission's stance drops the mission's NAME (it is no longer
 *    true of the wall) without rolling back the posture it set.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { MISSIONS, findMission } from '@/lib/missions'
import { SCOPE_PIN_ORIGIN } from '@/lib/consultContext'
import { consultActions } from '@/state/consultSession'
import { emptyMembers, resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'

// Two missions mount the World Map, whose renderers statically pull `leaflet`
// and `maplibre-gl`. Both touch `window` at module scope and resolve through a
// lazy import that can land AFTER jsdom is torn down, which surfaces as an
// unhandled rejection with nothing to do with missions. The map's own
// behaviour is not what this file is about, so the tile is stubbed.
vi.mock('@/panels/v4/MapPanel', () => ({
  default: () => <div data-testid="stub-world-map" />,
}))

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

/** The `mission` param currently on the hash, or null. */
function missionParam(): string | null {
  return new URLSearchParams(window.location.hash.replace(/^#/, '')).get('mission')
}

function wsParam(): string | null {
  return new URLSearchParams(window.location.hash.replace(/^#/, '')).get('ws')
}

async function chooseMission(id: string) {
  fireEvent.click(await screen.findByTestId('mission-trigger'))
  fireEvent.click(screen.getByTestId(`mission-option-${id}`))
}

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  useSelection.getState().clear()
  useWorldState.setState({
    layers: { signals: true, findings: true, situations: true, entities: true, events: true },
  })
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const body = String(url).includes('/registry/')
        ? []
        : { data: [], items: [], next_cursor: null }
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
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('missions round-trip through the hash', () => {
  for (const m of MISSIONS) {
    it(`${m.label} — chosen from the bar, it lands on the hash with its stance`, async () => {
      render(wrap(<App />))
      await chooseMission(m.id)
      await waitFor(() => expect(missionParam()).toBe(m.id))
      expect(wsParam()).toBe(m.workspace)
      expect(screen.getByTestId('mission-active-chip')).toHaveTextContent(m.label)
      // The window moved with it — the axis a tab switch never touched.
      expect(useWorldState.getState().spanMs).toBe(m.timeWindow.spanMs)
    })

    it(`${m.label} — restored from a link, it re-applies its own aperture`, async () => {
      window.location.hash = `#mission=${m.id}`
      render(wrap(<App />))
      await waitFor(() =>
        expect(useWorldState.getState().spanMs).toBe(m.timeWindow.spanMs),
      )
      // `#mission=` alone named the stance.
      expect(screen.getByTestId(`workspace-tab-${m.workspace}`)).toHaveAttribute(
        'aria-selected',
        'true',
      )
      expect(missionParam()).toBe(m.id)
      if (m.layers.length > 0) {
        const layers = useWorldState.getState().layers
        for (const [key, on] of Object.entries(layers)) {
          expect(on, `${m.id} layer ${key}`).toBe(
            (m.layers as readonly string[]).includes(key),
          )
        }
      }
    })
  }

  it('an unknown mission id on the hash is ignored, not applied', async () => {
    window.location.hash = '#mission=not_a_mission'
    render(wrap(<App />))
    await waitFor(() => expect(screen.getByTestId('workspace-bar')).toBeInTheDocument())
    expect(screen.queryByTestId('mission-active-chip')).toBeNull()
  })

  it("a link carrying its own #scope= keeps it — an address beats a default aperture", async () => {
    window.location.hash = '#scope=target%3Acountry_g20_ar&mission=morning_read'
    render(wrap(<App />))
    // Morning Read's declaration is "the whole roster", which would normally
    // CLEAR the scope. The link's own scope wins, and the bar says it did.
    await waitFor(() => expect(useScope.getState().scope?.id).toBe('country_g20_ar'))
    await waitFor(() =>
      expect(screen.getByTestId('workspace-question')).toHaveTextContent(
        "link's own scope was kept",
      ),
    )
  })

  it('Desk Watch with a desk selected scopes the wall to it', async () => {
    render(wrap(<App />))
    useSelection.getState().select({ kind: 'target', id: 'country_watch_il', label: 'Israel' })
    await chooseMission('desk_watch')
    await waitFor(() => expect(useScope.getState().scope?.id).toBe('country_watch_il'))
    expect(screen.getByTestId('workspace-question')).toHaveTextContent('Israel')
  })

  it('Desk Watch with NO desk selected says so on the bar and invents nothing', async () => {
    render(wrap(<App />))
    await chooseMission('desk_watch')
    await waitFor(() =>
      expect(screen.getByTestId('workspace-question')).toHaveTextContent('no desk selected'),
    )
    expect(useScope.getState().scope).toBeNull()
  })

  it('walking off a mission\'s stance drops the NAME, not the posture', async () => {
    render(wrap(<App />))
    await chooseMission('crisis')
    await waitFor(() => expect(missionParam()).toBe('crisis'))
    const span = useWorldState.getState().spanMs

    fireEvent.click(screen.getByTestId('workspace-tab-engine'))
    await waitFor(() => expect(missionParam()).toBeNull())
    expect(screen.queryByTestId('mission-active-chip')).toBeNull()
    // The window it set is the reader's posture now; rolling it back would be
    // the surprise, not the honesty.
    expect(useWorldState.getState().spanMs).toBe(span)
  })

  it('returning to the mission\'s own stance keeps the mission', async () => {
    render(wrap(<App />))
    await chooseMission('release')
    await waitFor(() => expect(missionParam()).toBe('release'))
    // `trust` IS Release's stance — switching to it is a no-op switch.
    fireEvent.click(screen.getByTestId('workspace-tab-trust'))
    expect(missionParam()).toBe('release')
  })

  it('the mission survives a scope the reader sets afterwards', async () => {
    render(wrap(<App />))
    await chooseMission('morning_read')
    await waitFor(() => expect(missionParam()).toBe('morning_read'))
    useScope.getState().setScope({
      kind: 'target',
      id: 'country_g20_cn',
      label: 'China',
      members: emptyMembers(),
      origin: 'navigator',
    })
    await waitFor(() => expect(useScope.getState().scope?.label).toBe('China'))
    expect(missionParam()).toBe('morning_read')
  })

  it("the mission's Consult tile comes up already pinned to the new scope", async () => {
    render(wrap(<App />))
    useSelection.getState().select({ kind: 'target', id: 'country_watch_il', label: 'Israel' })
    await chooseMission('desk_watch')
    // The Consult panel auto-pins the AMBIENT scope; applying the mission sets
    // that scope before the tile opens, so the pin is there on first paint
    // rather than a navigation later.
    await waitFor(
      () => {
        const pins = consultActions().panel('singleton:system.consult').pins
        const scopePin = pins.find((p) => p.origin === SCOPE_PIN_ORIGIN)
        expect(scopePin?.id).toBe('country_watch_il')
      },
      // The tile is lazy-loaded; the pin lands on its first mount, not before.
      { timeout: 8000 },
    )
  }, 15000)

  it('every mission names a stance the bar actually renders', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(screen.getByTestId('workspace-bar')).toBeInTheDocument())
    for (const m of MISSIONS) {
      expect(screen.getByTestId(`workspace-tab-${findMission(m.id)!.workspace}`)).toBeInTheDocument()
    }
  })
})
