/**
 * The Morning Read × the REAL Dockview — the binding-path test.
 *
 * `MorningRead.test.tsx` proves the surface renders what the payload says. It
 * proves nothing about whether the workstation can actually mount it, and that
 * is the half that breaks: a panel reaches an operator through a `PanelKind`
 * union, a lazy registry row, `App.tsx`'s `LegbaPanelComponent`, a Suspense
 * boundary and a serialized-layout round trip — five seams, none of which a
 * direct `render()` exercises.
 *
 * So this file mounts the REAL `DockviewReact` with `App.tsx`'s own exported
 * `COMPONENTS` map (the `aliasesRuntime.test.tsx` / `readTelemetryRuntime`
 * idiom), adds the panel exactly the way `App.tsx`'s `addSingleton` does, and
 * asserts the reading surface appears — and survives `toJSON` → `clear` →
 * `fromJSON`, which is what an operator's saved layout does on every reload.
 *
 * It also pins the two chokepoints the wager is graded on through the real
 * mount: `panel_open` for the tile, `brief_read` for the brief. An instrument
 * that only fires in a unit test reports zero in production and looks like
 * evidence (D2e).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
  type SerializedDockview,
} from 'dockview-react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { COMPONENTS } from '@/App'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { WORKSPACES, LANDING_WORKSPACE } from '@/lib/workspaces'
import { __pendingReadEvents, __resetReadTelemetry } from '@/lib/readTelemetry'
import { __resetReadLayout } from '@/lib/readLayout'
import worldRow from './__fixtures__/assembly.world.json'

const KIND = 'v4.morning_read'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function kinds(): string[] {
  return __pendingReadEvents().map((e) => e.event_kind)
}

beforeEach(() => {
  __resetReadTelemetry()
  __resetReadLayout()
  sessionStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const analyst = /analyst_id=([^&]+)/.exec(String(url))?.[1] ?? ''
      const data = analyst === 'world_assessor' ? [worldRow] : []
      return {
        ok: true,
        status: 200,
        json: async () => ({ data }),
        text: async () => '',
      } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function mountDock(): Promise<DockviewApi> {
  let api: DockviewApi | null = null
  render(
    wrap(
      <div style={{ width: 1280, height: 800 }}>
        <DockviewReact
          components={COMPONENTS}
          onReady={(ev: DockviewReadyEvent) => {
            api = ev.api
          }}
          className="dockview-theme-abyss h-full"
        />
      </div>,
    ),
  )
  await waitFor(() => expect(api).not.toBeNull())
  return api!
}

/** Mirrors `App.tsx`'s `addSingleton` params shape exactly. */
function addSingleton(api: DockviewApi, kind: string) {
  return api.addPanel({
    id: kind,
    component: 'default',
    title: kind,
    params: { registration: null, singletonKind: kind, mode: 'personal' },
  })
}

describe('the Morning Read mounts through the real workstation', () => {
  it('is a registered singleton kind with a definition the shell can use', () => {
    const entry = PANEL_REGISTRY[KIND]
    expect(entry).toBeTruthy()
    expect(entry.definition.requiresBinding).toBe(false)
    expect(entry.definition.modes).toContain('personal')
    expect(entry.definition.defaultTitle).toBe('Morning Read')
    expect(entry.definition.tier).toBe('live')
  })

  it('renders the reading surface inside a real Dockview panel', async () => {
    const api = await mountDock()
    addSingleton(api, KIND)
    // Lazy import + Suspense + the panel boundary all have to resolve for this.
    await screen.findByTestId('morning-read', {}, { timeout: 5000 })
    expect(screen.getByTestId('read-record')).toBeTruthy()
    expect(api.panels.length).toBe(1)
  })

  it('survives the saved-layout round trip an operator performs on every reload', async () => {
    const api = await mountDock()
    addSingleton(api, KIND)
    await screen.findByTestId('morning-read', {}, { timeout: 5000 })

    const saved: SerializedDockview = api.toJSON()
    api.clear()
    await waitFor(() => expect(api.panels.length).toBe(0))

    expect(() => api.fromJSON(saved)).not.toThrow()
    await waitFor(() => expect(api.panels.length).toBe(1))
    expect(api.getPanel(KIND)).toBeTruthy()
    await screen.findByTestId('morning-read', {}, { timeout: 5000 })
  })

  it('emits panel_open for the tile and brief_read for the brief', async () => {
    const api = await mountDock()
    addSingleton(api, KIND)
    await waitFor(() => expect(kinds()).toContain('panel_open'))
    await screen.findByTestId('morning-read', {}, { timeout: 5000 })
    await waitFor(() => expect(kinds()).toContain('brief_read'))
    // One brief rendered, one brief_read. The count is what G5 is graded on and
    // it must never read high (readTelemetry's rule 3).
    expect(kinds().filter((k) => k === 'brief_read')).toHaveLength(1)
  })

  it('is the tab the landing stance opens on', () => {
    const landing = WORKSPACES.find((w) => w.id === LANDING_WORKSPACE)!
    expect(landing.seed.map((p) => p.kind)).toContain(KIND)
    expect(landing.active).toContain(KIND)
    // The legacy prose one-pager stays seeded beside it for the whole A/B.
    expect(landing.seed.map((p) => p.kind)).toContain('v4.assessment')
  })
})
