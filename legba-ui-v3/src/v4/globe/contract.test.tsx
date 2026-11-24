/**
 * U-7 — THE ONE MAP CONTRACT, against the real map component.
 *
 * The frame's router (`applyPanelAction('v4.map', …)`) reaches the globe only
 * through `globeActions` (`contract.ts`): a layer set by its real id moves the
 * mounted map's layer, a fly-to moves its camera, an Undo puts both back, a
 * request made while no map is mounted is applied when one is, and a series
 * place is the shared point. maplibre-gl is replaced by a recorder (jsdom has
 * no WebGL); everything above it — GlobeMap, its stores, the router — is real.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

type Handler = (...a: unknown[]) => void

const { FakeMap, FakeMarker, maps } = vi.hoisted(() => {
  const maps: FakeMap[] = []

  class FakeMap {
    center = { lng: 30, lat: 20 }
    zoom = 1.35
    handlers = new Map<string, Handler[]>()
    layout = new Map<string, string>()
    calls: { fn: string; args: unknown }[] = []
    touchZoomRotate = { disableRotation: () => {} }
    constructor() {
      maps.push(this)
      setTimeout(() => this.fire('load'), 0)
    }
    on(ev: string, fn: Handler) {
      this.handlers.set(ev, [...(this.handlers.get(ev) ?? []), fn])
      return this
    }
    off() {
      return this
    }
    once(_ev: string, fn: Handler) {
      setTimeout(() => fn(), 0)
      return this
    }
    fire(ev: string) {
      for (const h of this.handlers.get(ev) ?? []) h({})
    }
    private move(fn: string, args: unknown, center?: [number, number], zoom?: number) {
      this.calls.push({ fn, args })
      if (center) this.center = { lng: center[0], lat: center[1] }
      if (zoom != null) this.zoom = zoom
      this.fire('moveend')
    }
    easeTo(o: { center?: [number, number]; zoom?: number }) {
      this.move('easeTo', o, o.center, o.zoom)
    }
    jumpTo(o: { center?: [number, number]; zoom?: number }) {
      this.move('jumpTo', o, o.center, o.zoom)
    }
    fitBounds(b: [[number, number], [number, number]], o: unknown) {
      this.move('fitBounds', { b, o }, [(b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2], 3)
    }
    getCenter() {
      return this.center
    }
    getZoom() {
      return this.zoom
    }
    hasImage() {
      return true
    }
    addImage() {}
    addSource() {}
    getSource() {
      return { setData: () => {}, getClusterExpansionZoom: async () => 3 }
    }
    addLayer() {}
    getLayer() {
      return true
    }
    setLayoutProperty(id: string, k: string, v: string) {
      if (k === 'visibility') this.layout.set(id, v)
    }
    getLayoutProperty(id: string) {
      return this.layout.get(id)
    }
    setPaintProperty() {}
    setFeatureState() {}
    setFilter() {}
    getFilter() {
      return null
    }
    queryRenderedFeatures() {
      return []
    }
    getCanvas() {
      return { style: {} }
    }
    resize() {}
    remove() {}
    setProjection() {}
    zoomIn() {}
    zoomOut() {}
    project() {
      return { x: 0, y: 0 }
    }
  }

  class FakeMarker {
    el: HTMLElement
    constructor(o: { element: HTMLElement }) {
      this.el = o.element
    }
    setLngLat() {
      return this
    }
    addTo() {
      return this
    }
    remove() {}
    getElement() {
      return this.el
    }
  }
  return { FakeMap, FakeMarker, maps }
})
type FakeMap = InstanceType<typeof FakeMap>

vi.mock('maplibre-gl', () => ({ default: { Map: FakeMap, Marker: FakeMarker }, Map: FakeMap, Marker: FakeMarker }))
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}))
vi.mock('./style', async (orig) => ({ ...(await orig<typeof import('./style')>()), ensureMapFonts: async () => {}, markImages: () => [] }))

import GlobeMap, { homeCenter, homeZoom } from './GlobeMap'
import { chromeFolds } from './chrome'
import { applyPanelAction, clearViewUndo, undoView } from '@/lib/panelActions'
import { contractOf } from '@/panel-registry/contractOf'
import { MAP_LAYER_IDS } from '@/panel-registry/contracts'
import { useSelection } from '@/state/selection'
import { useGlobeCamera } from './actions'
import { mapOnScreen } from './contract'
import { resetGlobeLayers, useGlobeLayers } from './layerState'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const lastMove = () => maps[maps.length - 1].calls[maps[maps.length - 1].calls.length - 1]

beforeEach(() => {
  maps.length = 0
  resetGlobeLayers()
  clearViewUndo()
  useSelection.getState().clear()
  useGlobeCamera.setState({ request: null, nonce: 0, view: null })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ data: [], items: [] }), text: async () => '{"data":[]}' }) as unknown as Response),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function mount() {
  render(wrap(<GlobeMap preset="board" active onFallback={() => {}} />))
  await waitFor(() => expect(maps.length).toBe(1))
  // loaded: the size and sources effects have run
  await waitFor(() => expect(maps[0].handlers.get('moveend')?.length).toBeGreaterThan(0))
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20))
  })
}

describe('the map contract is the globe', () => {
  it("lists the globe's real layer ids and the five map actions", () => {
    expect(MAP_LAYER_IDS).toEqual(['change', 'blanks', 'series', 'forecasts', 'events', 'at_sea'])
    const c = contractOf('v4.map')!
    expect(c.actions.map((a) => a.id)).toEqual(['set_layer', 'fly_to', 'home', 'set_window', 'select'])
    // the row with no route is named, not offered
    expect(c.actions[0].args[0].describe).toMatch(/drift — not available/)
  })

  it('set_layer and fly_to drive the mounted map, and Undo takes both back', async () => {
    await mount()
    expect(useGlobeLayers.getState().on.events).toBe(false)
    const before = { ...maps[0].center, zoom: maps[0].zoom }

    act(() => {
      expect(applyPanelAction('v4.map', 'set_layer', { layer: 'events', on: true })).toEqual({ state: 'executed' })
    })
    expect(useGlobeLayers.getState().on.events).toBe(true)
    expect(screen.getByTestId('globe-map').dataset.marks).toBeDefined()

    act(() => {
      expect(applyPanelAction('v4.map', 'fly_to', { lon: 43.4, lat: 12.6, zoom: 4 })).toEqual({ state: 'executed' })
    })
    await waitFor(() => expect(lastMove()).toMatchObject({ fn: 'easeTo', args: { center: [43.4, 12.6], zoom: 4 } }))
    expect(mapOnScreen().centre).toMatch(/^12.6,43.4 z4.0 (globe|flat)$/)

    // Undo the fly-to: the camera goes back to where it rested
    act(() => {
      undoView()
    })
    await waitFor(() => expect(lastMove()).toMatchObject({ fn: 'easeTo', args: { center: [before.lng, before.lat] } }))
    expect(useGlobeLayers.getState().on.events).toBe(true)
    // Undo the layer
    act(() => {
      undoView()
    })
    expect(useGlobeLayers.getState().on.events).toBe(false)
  })

  it('refuses what the map cannot do, by name', () => {
    expect(applyPanelAction('v4.map', 'set_layer', { layer: 'drift', on: true })).toMatchObject({ state: 'refused' })
    expect(applyPanelAction('v4.map', 'fly_to', {})).toMatchObject({ state: 'skipped', reason: expect.stringMatching(/fly_to needs/) })
    expect(applyPanelAction('v4.map', 'fly_to', { lon: 'x', lat: 1 })).toMatchObject({ state: 'refused' })
  })

  it('a request made while no map is mounted is applied when it is', async () => {
    act(() => {
      applyPanelAction('v4.map', 'set_layer', { layer: 'at_sea', on: true })
      applyPanelAction('v4.map', 'fly_to', { west: 30, south: 10, east: 60, north: 30 })
    })
    await mount()
    expect(useGlobeLayers.getState().on.at_sea).toBe(true)
    await waitFor(() => expect(maps[0].calls.some((c) => c.fn === 'fitBounds' && JSON.stringify(c.args).includes('[[30,10],[60,30]]'))).toBe(true))
  })

  it('a series place is the shared point: select and fly_to by its place id', async () => {
    await mount()
    act(() => {
      expect(applyPanelAction('v4.map', 'fly_to', { ref: { kind: 'place', id: 'portwatch:chokepoint4' } })).toEqual({ state: 'executed' })
    })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'place', id: 'portwatch:chokepoint4', label: 'Bab el-Mandeb Strait' })
    await waitFor(() => expect(maps[0].calls.some((c) => c.fn === 'easeTo' && JSON.stringify(c.args).includes('[43.4,12.6]'))).toBe(true))
    expect(applyPanelAction('v4.map', 'select', { ref: { kind: 'place', id: 'portwatch:nowhere' } })).toEqual({ state: 'executed' })
    expect(applyPanelAction('v4.map', 'fly_to', { ref: { kind: 'place', id: 'portwatch:nowhere' } })).toMatchObject({ state: 'skipped' })
  })
})

describe('the map in the frame (U-7): home and chrome by the room it is given', () => {
  it('flat, the home fits the inhabited world in both sides; the globe fits the shorter side', () => {
    for (const [w, h] of [[576, 271], [740, 337], [910, 524], [854, 1230], [1393, 1879]]) {
      const z = homeZoom(w, h, 'flat')
      expect(512 * 2 ** z).toBeLessThanOrEqual(w + 1)
      expect(0.56 * 512 * 2 ** z).toBeLessThanOrEqual(h + 1)
      const g = homeZoom(w, h, 'globe')
      expect((2 * 512 * 2 ** g) / (2 * Math.PI)).toBeLessThanOrEqual(Math.min(w, h))
    }
  })

  it('flat, home centres on the whole world when it fits the width, on the subjects in a tall column', () => {
    expect(homeCenter([60, 20], 'flat', 910, 524)).toEqual([0, 27])
    expect(homeCenter([60, 20], 'flat', 854, 1230)).toEqual([60, 27])
    expect(homeCenter([60, 20], 'globe', 854, 1230)).toEqual([60, 20])
  })

  it('folds the layer card under 40 × 30 rem and the legend under 30 rem tall', () => {
    expect(chromeFolds(740, 337, false)).toEqual({ layers: true, legend: true })
    expect(chromeFolds(480, 900, false)).toEqual({ layers: true, legend: false })
    expect(chromeFolds(910, 524, false)).toEqual({ layers: false, legend: false })
    expect(chromeFolds(1393, 1879, true)).toEqual({ layers: true, legend: true })
    // not measured yet: nothing folded that cannot be judged
    expect(chromeFolds(0, 0, false)).toEqual({ layers: false, legend: false })
  })
})
