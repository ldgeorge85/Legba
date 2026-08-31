/**
 * Alias pre-pass × the REAL Dockview (the binding-path test).
 *
 * `aliases.test.ts` proves the rewrite produces the ids we expect. That is not
 * the risky half. The risky half is that `rewriteSerializedLayout` performs
 * TREE SURGERY on a serialized grid — dropping views, collapsing duplicates,
 * pruning branch children that lost every view, repointing `activeView` and
 * `activeGroup` — and then hands the result to a library that has **no
 * per-panel fallback for an unknown component** and no layout versioning of its
 * own. A fake api cannot catch a shape Dockview refuses; only Dockview can.
 *
 * So this file mounts the REAL `DockviewReact` in jsdom (same idiom as
 * `lib/dockviewRuntime.test.tsx`), captures a genuine `toJSON()` containing
 * PRE-MERGE panel ids, runs the pre-pass, and asserts `fromJSON` accepts it and
 * mounts the survivors. If a future Dockview tightens its deserializer, this is
 * where it goes red — before an operator's saved layout does.
 */

import { describe, it, expect } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/react'
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
  type SerializedDockview,
} from 'dockview-react'
import { rewriteSerializedLayout } from './aliases'

function TestPanel(props: IDockviewPanelProps<{ singletonKind?: string }>) {
  return <div data-testid={`body-${props.api.id}`}>{props.params?.singletonKind ?? 'panel'}</div>
}

const COMPONENTS = { default: TestPanel }

async function mountDock(): Promise<DockviewApi> {
  let api: DockviewApi | null = null
  render(
    <div style={{ width: 1280, height: 800 }}>
      <DockviewReact
        components={COMPONENTS}
        onReady={(ev: DockviewReadyEvent) => {
          api = ev.api
        }}
        className="dockview-theme-abyss h-full"
      />
    </div>,
  )
  await waitFor(() => expect(api).not.toBeNull())
  return api!
}

/** Mirrors App.tsx's `addSingleton` params shape (what a saved layout carries). */
function addSingleton(
  api: DockviewApi,
  kind: string,
  position?: { referencePanel: string; direction: 'right' | 'below' | 'within' },
) {
  return api.addPanel({
    id: kind,
    component: 'default',
    title: kind,
    params: { registration: null, singletonKind: kind, mode: 'personal' },
    ...(position ? { position } : {}),
  })
}

describe('the alias pre-pass produces a layout the real Dockview accepts', () => {
  it('restores a PRE-MERGE saved layout onto the survivors', async () => {
    const api = await mountDock()
    // A layout saved before the merges: three retired ids and one live one.
    addSingleton(api, 'v4.why')
    addSingleton(api, 'system.findings', {
      referencePanel: 'v4.why',
      direction: 'right',
    })
    addSingleton(api, 'system.watchlist', {
      referencePanel: 'system.findings',
      direction: 'below',
    })
    const saved: SerializedDockview = api.toJSON()
    api.clear()
    expect(api.panels.length).toBe(0)

    const rewrite = rewriteSerializedLayout(saved)
    expect(rewrite.empty).toBe(false)
    expect(() => api.fromJSON(rewrite.layout)).not.toThrow()

    await waitFor(() => expect(api.panels.length).toBe(3))
    expect(api.getPanel('system.provenance'), 'v4.why → Provenance').toBeTruthy()
    expect(api.getPanel('system.alerts_watches'), 'watchlist → Alerts & Watches').toBeTruthy()
    expect(api.getPanel('system.findings')).toBeTruthy()
    // The retired ids are gone, not duplicated alongside their survivors.
    expect(api.getPanel('v4.why')).toBeUndefined()
    expect(api.getPanel('system.watchlist')).toBeUndefined()
    cleanup()
  })

  it('collapses three retired tiles that share one survivor into ONE mounted tile', async () => {
    const api = await mountDock()
    addSingleton(api, 'v4.why')
    addSingleton(api, 'system.lineage', { referencePanel: 'v4.why', direction: 'right' })
    addSingleton(api, 'v4.flow', { referencePanel: 'system.lineage', direction: 'below' })
    const saved = api.toJSON()
    api.clear()

    const rewrite = rewriteSerializedLayout(saved)
    expect(rewrite.collapsed).toEqual(['system.lineage', 'v4.flow'])
    api.fromJSON(rewrite.layout)

    await waitFor(() => expect(api.panels.length).toBe(1))
    expect(api.getPanel('system.provenance')).toBeTruthy()
    cleanup()
  })

  it('loads a layout whose pruned group left a branch with one child', async () => {
    const api = await mountDock()
    // `system.pulse` was DELETED outright in S7-T2 — no kind, no alias. It must
    // be removed from the grid, and the surviving split must still deserialize.
    addSingleton(api, 'system.pulse')
    addSingleton(api, 'system.findings', {
      referencePanel: 'system.pulse',
      direction: 'right',
    })
    const saved = api.toJSON()
    api.clear()

    const rewrite = rewriteSerializedLayout(saved)
    expect(rewrite.dropped).toEqual(['system.pulse'])
    expect(() => api.fromJSON(rewrite.layout)).not.toThrow()

    await waitFor(() => expect(api.panels.length).toBe(1))
    expect(api.getPanel('system.findings')).toBeTruthy()
    cleanup()
  })

  it('passes the aliased TAB through to the mounted panel params', async () => {
    const api = await mountDock()
    addSingleton(api, 'system.escalations')
    const saved = api.toJSON()
    api.clear()

    api.fromJSON(rewriteSerializedLayout(saved).layout)
    await waitFor(() => expect(api.panels.length).toBe(1))
    const panel = api.getPanel('system.alerts_watches')!
    // App.tsx reads `params.tab` and hands it to the panel as `initialTab`, so
    // a deep-link to the retired kind lands on ITS tab, not the default one.
    expect((panel.params as { tab?: string }).tab).toBe('deliveries')
    cleanup()
  })
})
