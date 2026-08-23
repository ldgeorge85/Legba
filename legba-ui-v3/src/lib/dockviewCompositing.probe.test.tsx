/**
 * SPIKE PROBE (GLASS-4, 2026-08-21) — is a WebGL canvas inside a Dockview tile
 * actually sitting under a transformed / compositing-triggering ancestor?
 *
 * This exists to test the claim recorded in `TileWebGLOverlay.tsx`:
 *   "Dockview lays tiles out with CSS `transform`, and Chrome will not
 *    composite a WebGL layer that has a transformed ancestor"
 * which is the stated justification for the dual Leaflet/MapLibre map stack.
 *
 * It walks the real ancestor chain from a mounted panel body up to the Dockview
 * root and reports every inline style + class that could create a compositing
 * layer or a containing block (transform / will-change / filter / perspective /
 * contain / opacity / isolation).
 *
 * NOTE: jsdom has no layout engine and no compositor, so this CANNOT prove a
 * canvas paints. What it CAN prove is the DOM/style FACT the claim rests on —
 * whether Dockview writes a transform onto a panel-content ancestor at all.
 */

import { describe, it, expect } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import {
  DockviewReact,
  type DockviewApi,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
} from 'dockview-react'

function Body(props: IDockviewPanelProps) {
  return <div data-testid={`content-${props.api.id}`}>content</div>
}

const COMPONENTS = { default: Body }

const COMPOSITING_PROPS = [
  'transform',
  'willChange',
  'filter',
  'backdropFilter',
  'perspective',
  'contain',
  'isolation',
  'opacity',
] as const

describe('Dockview tile compositing probe', () => {
  it('reports the ancestor style chain above panel content', async () => {
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
    const dock = api!

    // A realistic split: the map tile lives to the right of the feed.
    dock.addPanel({ id: 'system.findings', component: 'default', title: 'Feed' })
    dock.addPanel({
      id: 'v4.map',
      component: 'default',
      title: 'Map',
      position: { referencePanel: 'system.findings', direction: 'right' },
    })

    const content = await waitFor(() => {
      const el = document.querySelector('[data-testid="content-v4.map"]')
      expect(el).toBeTruthy()
      return el as HTMLElement
    })

    const chain: string[] = []
    let node: HTMLElement | null = content
    let transformedAncestors = 0
    while (node && node !== document.body) {
      const s = node.style
      const flagged = COMPOSITING_PROPS.map((p) => {
        const v = (s as unknown as Record<string, string>)[p]
        return v ? `${p}=${v}` : null
      }).filter(Boolean)
      if (flagged.some((f) => f!.startsWith('transform='))) transformedAncestors++
      chain.push(
        `  <${node.tagName.toLowerCase()} class="${node.className}"` +
          ` style-pos="${s.position || '-'}" left="${s.left || '-'}" top="${s.top || '-'}"` +
          ` w="${s.width || '-'}" h="${s.height || '-'}"` +
          (flagged.length ? ` COMPOSITING[${flagged.join(', ')}]` : '') +
          '>',
      )
      node = node.parentElement
    }

    // eslint-disable-next-line no-console
    console.log(
      '\n=== ancestor chain, panel content -> root (dockview ' +
        `${(globalThis as unknown as { __DV_VERSION__?: string }).__DV_VERSION__ ?? 'n/a'}):\n` +
        chain.join('\n') +
        `\n=== ancestors carrying an inline transform: ${transformedAncestors}\n`,
    )

    // The claim under test: Dockview positions tiles with `transform`.
    // Assert the OPPOSITE of the docstring so this test goes red the day it
    // becomes true again (i.e. if a future Dockview switches to transforms,
    // the dual-map justification comes back and we want to be told).
    expect(transformedAncestors).toBe(0)

    // And assert what it DOES use: absolute left/top/width/height offsets.
    const usesOffsets = chain.some((l) => /left="\d+px"/.test(l) || /top="\d+px"/.test(l))
    expect(usesOffsets).toBe(true)
  })
})
