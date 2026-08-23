/**
 * Dockview RUNTIME contract test (GLASS-4 upgrade spike, 2026-08-21).
 *
 * WHY THIS EXISTS: every other Dockview-touching test in this repo drives a
 * hand-written FAKE api (see `layoutPresets.test.ts`'s `fakeApi`, which
 * implements exactly `clear`/`toJSON`/`fromJSON`). That is fine for testing our
 * sequencing logic, but it means the whole suite plus `tsc` can go green while
 * the real library has renamed, removed, or re-shaped every method the shell
 * calls — a fake and a typecheck cannot catch a RUNTIME break.
 *
 * The 4.3 → 7 upgrade spike needed proof beyond "it compiles", so this file
 * mounts the REAL `DockviewReact` in jsdom and exercises the exact API surface
 * `App.tsx`, `layoutPresets.ts`, `investigateLayout.ts` and
 * `TileWebGLOverlay.tsx` depend on. If a future major renames `addPanel`'s
 * `position.referencePanel`, drops `panel.api.setSize`, or changes the
 * serialized-layout shape, THIS is the test that goes red.
 *
 * It is deliberately library-contract-only: no Legba panels, no registry, no
 * network. Keep it that way — it should stay fast and should fail for exactly
 * one reason (Dockview changed).
 */

import { describe, it, expect } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/react'
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
  type IDockviewPanelHeaderProps,
  type SerializedDockview,
} from 'dockview-react'

/** Minimal panel body — mirrors how App.tsx reads `props.params`/`props.api`. */
function TestPanel(props: IDockviewPanelProps<{ label?: string }>) {
  return <div data-testid={`body-${props.api.id}`}>{props.params?.label ?? 'panel'}</div>
}

/** Mirrors App.tsx's `AnchorTab` — a custom tab reading `props.api.title`. */
function TestTab(props: IDockviewPanelHeaderProps) {
  return <div data-testid="anchor-tab">{props.api.title}</div>
}

const COMPONENTS = { default: TestPanel }
const TAB_COMPONENTS = { anchor: TestTab }

/** Mount a real Dockview and resolve once `onReady` has handed us the api. */
async function mountDock(): Promise<DockviewApi> {
  let api: DockviewApi | null = null
  const onReady = (ev: DockviewReadyEvent) => {
    api = ev.api
  }
  render(
    <div style={{ width: 1280, height: 800 }}>
      <DockviewReact
        components={COMPONENTS}
        tabComponents={TAB_COMPONENTS}
        onReady={onReady}
        className="dockview-theme-abyss h-full"
      />
    </div>,
  )
  await waitFor(() => expect(api).not.toBeNull())
  return api!
}

/** The App.tsx `addSingleton` call shape, reduced to the library contract. */
function addSingleton(
  api: DockviewApi,
  id: string,
  opts: {
    tabComponent?: string
    position?: { referencePanel: string; direction: 'right' | 'left' | 'above' | 'below' | 'within' }
  } = {},
) {
  return api.addPanel({
    id,
    component: 'default',
    title: `title-${id}`,
    ...(opts.tabComponent ? { tabComponent: opts.tabComponent } : {}),
    params: { label: id },
    ...(opts.position ? { position: opts.position } : {}),
  })
}

describe('Dockview runtime contract (the surface the Legba shell calls)', () => {
  it('mounts and hands back a DockviewApi via onReady', async () => {
    const api = await mountDock()
    expect(api).toBeTruthy()
    expect(typeof api.addPanel).toBe('function')
    expect(typeof api.getPanel).toBe('function')
    expect(typeof api.clear).toBe('function')
    expect(typeof api.toJSON).toBe('function')
    expect(typeof api.fromJSON).toBe('function')
    cleanup()
  })

  it('addPanel + getPanel round-trip on the panel id (singleton dedup path)', async () => {
    const api = await mountDock()
    const panel = addSingleton(api, 'system.findings')
    expect(panel).toBeTruthy()
    // App.tsx dedups sidebar opens by looking the id back up.
    const found = api.getPanel('system.findings')
    expect(found).toBeTruthy()
    expect(found!.id).toBe('system.findings')
    expect(api.getPanel('does.not.exist')).toBeUndefined()
    cleanup()
  })

  it('panel.api.setActive() exists and activates (the 4.x setActivePanel replacement)', async () => {
    const api = await mountDock()
    addSingleton(api, 'a')
    const b = addSingleton(api, 'b')
    const a = api.getPanel('a')!
    expect(typeof a.api.setActive).toBe('function')
    a.api.setActive()
    expect(a.api.isActive).toBe(true)
    expect(b.api.isActive).toBe(false)
    cleanup()
  })

  it('every split direction the presets use resolves against an earlier panel', async () => {
    // DEFAULT_BOOT_LAYOUT + the named presets + investigateLayout between them
    // use exactly these five directions, always referencing an earlier panel id.
    const api = await mountDock()
    addSingleton(api, 'anchor')
    const dirs = ['right', 'left', 'above', 'below', 'within'] as const
    for (const direction of dirs) {
      const id = `p-${direction}`
      const panel = addSingleton(api, id, {
        position: { referencePanel: 'anchor', direction },
      })
      expect(panel, `direction ${direction} failed to add`).toBeTruthy()
      expect(api.getPanel(id), `direction ${direction} not retrievable`).toBeTruthy()
    }
    cleanup()
  })

  it('panel.api.setSize accepts the width/height forms sizeMissionControl uses', async () => {
    const api = await mountDock()
    const kpi = addSingleton(api, 'v4.kpi')
    const report = addSingleton(api, 'v4.assessment', {
      position: { referencePanel: 'v4.kpi', direction: 'below' },
    })
    expect(typeof kpi.api.setSize).toBe('function')
    // App.tsx calls these inside a try/catch, but they must not THROW-by-signature.
    expect(() => kpi.api.setSize({ height: 100 })).not.toThrow()
    expect(() => report.api.setSize({ width: 384 })).not.toThrow()
    cleanup()
  })

  it('exposes api.width (sizeMissionControl reads it for the 30%/35% rails)', async () => {
    const api = await mountDock()
    expect(typeof api.width).toBe('number')
    cleanup()
  })

  it('a custom tabComponent renders (the ANCHOR_KINDS close-button-less tab)', async () => {
    const api = await mountDock()
    addSingleton(api, 'anchored', { tabComponent: 'anchor' })
    await waitFor(() => {
      expect(document.querySelector('[data-testid="anchor-tab"]')).toBeTruthy()
    })
    cleanup()
  })

  it('api.clear() empties the workspace (applyPreset / investigate grids)', async () => {
    const api = await mountDock()
    addSingleton(api, 'x')
    addSingleton(api, 'y', { position: { referencePanel: 'x', direction: 'right' } })
    expect(api.panels.length).toBe(2)
    api.clear()
    expect(api.panels.length).toBe(0)
    expect(api.getPanel('x')).toBeUndefined()
    cleanup()
  })

  it('toJSON → fromJSON round-trips a live layout (saveCustomLayout/loadCustomLayout)', async () => {
    const api = await mountDock()
    addSingleton(api, 'system.findings')
    addSingleton(api, 'system.inspector', {
      position: { referencePanel: 'system.findings', direction: 'right' },
    })
    const saved: SerializedDockview = api.toJSON()
    expect(saved.grid).toBeTruthy()
    expect(Object.keys(saved.panels)).toEqual(
      expect.arrayContaining(['system.findings', 'system.inspector']),
    )

    api.clear()
    expect(api.panels.length).toBe(0)

    api.fromJSON(saved)
    await waitFor(() => expect(api.panels.length).toBe(2))
    expect(api.getPanel('system.findings')).toBeTruthy()
    expect(api.getPanel('system.inspector')).toBeTruthy()
    cleanup()
  })

  it('loads a layout serialized by Dockview 4.3 (saved-layout forward compat)', async () => {
    // VERBATIM `api.toJSON()` output captured from dockview-core@4.3.0 — the
    // version this app shipped on — so an operator's localStorage layout
    // (`legba_layout_custom`) written before the upgrade still restores after
    // it. The inventory flagged this as the upgrade's headline risk.
    const v43Layout = {
      grid: {
        root: {
          type: 'branch',
          data: [
            {
              type: 'leaf',
              data: {
                views: ['system.findings'],
                activeView: 'system.findings',
                id: '1',
              },
              size: 640,
            },
            {
              type: 'leaf',
              data: {
                views: ['system.inspector'],
                activeView: 'system.inspector',
                id: '2',
              },
              size: 640,
            },
          ],
          size: 800,
        },
        width: 1280,
        height: 800,
        orientation: 'HORIZONTAL',
      },
      panels: {
        'system.findings': {
          id: 'system.findings',
          contentComponent: 'default',
          title: 'Live Feed',
          params: { label: 'system.findings' },
        },
        'system.inspector': {
          id: 'system.inspector',
          contentComponent: 'default',
          title: 'Inspector',
          params: { label: 'system.inspector' },
        },
      },
      activeGroup: '1',
    } as unknown as SerializedDockview

    const api = await mountDock()
    expect(() => api.fromJSON(v43Layout)).not.toThrow()
    await waitFor(() => expect(api.panels.length).toBe(2))
    expect(api.getPanel('system.findings')).toBeTruthy()
    expect(api.getPanel('system.inspector')).toBeTruthy()
    // The params the panel body reads must survive the restore.
    expect(api.getPanel('system.findings')!.params).toMatchObject({
      label: 'system.findings',
    })
    cleanup()
  })

  it('panel api exposes isVisible + onDidVisibilityChange (TileWebGLOverlay)', async () => {
    // The map's WebGL overlay portals its canvas to <body> and hides it when the
    // tile is tabbed behind; it reads exactly these two members off the panel api.
    const api = await mountDock()
    const panel = addSingleton(api, 'v4.map')
    expect(typeof panel.api.isVisible).toBe('boolean')
    expect(typeof panel.api.onDidVisibilityChange).toBe('function')
    const disposable = panel.api.onDidVisibilityChange(() => {})
    expect(typeof disposable.dispose).toBe('function')
    disposable.dispose()
    cleanup()
  })

  it('panel api exposes title (AnchorTab reads props.api.title)', async () => {
    const api = await mountDock()
    const panel = addSingleton(api, 'titled')
    expect(panel.api.title).toBe('title-titled')
    cleanup()
  })
})
