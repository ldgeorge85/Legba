/**
 * SPIKE PROBE (GLASS-4, 2026-08-21) — when does Dockview UNMOUNT a panel's
 * React tree?
 *
 * This is the load-bearing question for the "Consult panel with partial-turn
 * persistence" design: the Consult panel holds its whole transcript, its
 * in-flight `EventSource`, and its `sessionId` in plain `useState`, with NO
 * unmount cleanup. Whatever unmounts that component silently drops an
 * in-flight consult turn and leaks the stream.
 *
 * `TileWebGLOverlay.tsx` asserts in prose that "Dockview keeps inactive tab
 * content mounted". Dockview 7's `defaultRenderer` defaults to
 * `'onlyWhenVisible'`, which suggests the opposite. This probe settles it by
 * counting real mount/unmount events for the three lifecycle events that
 * matter to Consult:
 *   1. tab-switch away (another panel in the same group becomes active)
 *   2. `api.clear()` (what every layout preset + Investigate grid calls)
 *   3. explicit `renderer: 'always'` opt-in
 */

import { describe, it, expect } from 'vitest'
import { useEffect } from 'react'
import { render, waitFor, act } from '@testing-library/react'
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
} from 'dockview-react'

const mounts: Record<string, number> = {}
const unmounts: Record<string, number> = {}

function Tracked(props: IDockviewPanelProps) {
  const id = props.api.id
  useEffect(() => {
    mounts[id] = (mounts[id] ?? 0) + 1
    return () => {
      unmounts[id] = (unmounts[id] ?? 0) + 1
    }
  }, [id])
  return <div data-testid={`content-${id}`}>content</div>
}

const COMPONENTS = { default: Tracked }

async function mountDock(): Promise<DockviewApi> {
  let api: DockviewApi | null = null
  render(
    <div style={{ width: 1280, height: 800 }}>
      <DockviewReact
        components={COMPONENTS}
        onReady={(ev: DockviewReadyEvent) => {
          api = ev.api
        }}
        className="dockview-theme-abyss"
      />
    </div>,
  )
  await waitFor(() => expect(api).not.toBeNull())
  return api!
}

describe('Dockview panel mount lifecycle (Consult partial-turn persistence)', () => {
  it('tab-switch: does the backgrounded panel stay mounted?', async () => {
    for (const k of Object.keys(mounts)) delete mounts[k]
    for (const k of Object.keys(unmounts)) delete unmounts[k]

    const api = await mountDock()
    // Two panels in the SAME group => real tabs.
    api.addPanel({ id: 'consult', component: 'default', title: 'Consult' })
    api.addPanel({
      id: 'other',
      component: 'default',
      title: 'Other',
      position: { referencePanel: 'consult', direction: 'within' },
    })
    await waitFor(() => expect(mounts['consult']).toBe(1))

    // Background the consult tab.
    await act(async () => {
      api.getPanel('other')!.api.setActive()
    })
    await waitFor(() => expect(api.getPanel('other')!.api.isActive).toBe(true))

    // eslint-disable-next-line no-console
    console.log(
      `\n=== TAB SWITCH: consult mounts=${mounts['consult']} unmounts=${
        unmounts['consult'] ?? 0
      } (default renderer)\n`,
    )
    // Recorded as the OBSERVED behaviour; see the report for interpretation.
    expect(mounts['consult']).toBe(1)
  })

  it('api.clear(): the panel is destroyed and its React state is gone', async () => {
    for (const k of Object.keys(mounts)) delete mounts[k]
    for (const k of Object.keys(unmounts)) delete unmounts[k]

    const api = await mountDock()
    api.addPanel({ id: 'consult', component: 'default', title: 'Consult' })
    await waitFor(() => expect(mounts['consult']).toBe(1))

    await act(async () => {
      api.clear()
    })
    await waitFor(() => expect(unmounts['consult']).toBe(1))

    // eslint-disable-next-line no-console
    console.log(
      `\n=== api.clear(): consult mounts=${mounts['consult']} unmounts=${unmounts['consult']}\n`,
    )
    // This is the one that silently drops an in-flight consult turn: every
    // layout preset and both Investigate grids call api.clear().
    expect(unmounts['consult']).toBe(1)
    expect(api.getPanel('consult')).toBeUndefined()
  })

  it("renderer:'always' is accepted per-panel (the opt-in keep-mounted knob)", async () => {
    for (const k of Object.keys(mounts)) delete mounts[k]
    for (const k of Object.keys(unmounts)) delete unmounts[k]

    const api = await mountDock()
    api.addPanel({
      id: 'consult',
      component: 'default',
      title: 'Consult',
      renderer: 'always',
    })
    api.addPanel({
      id: 'other',
      component: 'default',
      title: 'Other',
      position: { referencePanel: 'consult', direction: 'within' },
    })
    await waitFor(() => expect(mounts['consult']).toBe(1))

    await act(async () => {
      api.getPanel('other')!.api.setActive()
    })
    await waitFor(() => expect(api.getPanel('other')!.api.isActive).toBe(true))

    // eslint-disable-next-line no-console
    console.log(
      `\n=== renderer:'always' TAB SWITCH: consult mounts=${mounts['consult']} unmounts=${
        unmounts['consult'] ?? 0
      }\n`,
    )
    expect(unmounts['consult'] ?? 0).toBe(0)
  })
})
