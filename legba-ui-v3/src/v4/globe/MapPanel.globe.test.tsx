/**
 * U-4 — the map panel under the globe flag: without WebGL2 (jsdom has none)
 * the drawn map renders with the globe's own chrome, a layer that has not
 * been read says so, and the flag can turn the globe off.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

vi.mock('@/v4/globe/GlobeMap', () => ({ default: () => <div data-testid="stub-globe" /> }))
vi.mock('@/v4/world/MapLibreWorldMap', () => ({ default: () => <div data-testid="stub-flat-map" /> }))
vi.mock('@/components/TileWebGLOverlay', () => ({
  TileWebGLOverlay: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}))

import MapPanel from '@/panels/v4/MapPanel'
import { globeEnabled } from '@/lib/globeFlag'
import { resetGlobeLayers } from './layerState'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  resetGlobeLayers()
  // every read held: nothing has answered
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.removeItem('legba_globe')
})

describe('the map panel under the globe flag', () => {
  it('is on by default and can be turned off by storage', () => {
    expect(globeEnabled()).toBe(true)
    localStorage.setItem('legba_globe', '0')
    expect(globeEnabled()).toBe(false)
  })

  it('draws the SVG map with the globe chrome when WebGL2 is missing, and says no read yet', async () => {
    render(wrap(<MapPanel />))
    const map = await screen.findByTestId('globe-map')
    expect(map.dataset.renderer).toBe('svg')
    expect(screen.getByTestId('globe-fallback-note').textContent).toMatch(/WebGL2/)
    // the legend rests folded and opens on its button
    expect(screen.queryByTestId('globe-legend')).toBeNull()
    fireEvent.click(screen.getByTestId('globe-legend-button'))
    expect(screen.getByTestId('globe-legend').textContent).toMatch(/no read yet/)
    // with the layer card folded the legend has the map's height to scroll in
    expect(screen.getByTestId('globe-legend')).toHaveAttribute('data-room', 'alone')
    expect(screen.getByTestId('globe-legend').className).toMatch(/overflow-y-auto/)
    // the Board's map carries its coverage strip, which waits for its reads
    expect(screen.getByTestId('coverage-strip')).toHaveAttribute('data-state', 'unread')
    // never a measured zero before anything answered
    expect(screen.getByTestId('globe-legend').textContent).not.toMatch(/\b0 (subjects|places|rings)\b/)
    expect(screen.queryByTestId('stub-flat-map')).toBeNull()
  })

  it('with the flag off and no WebGL2, the flat map yields to the drawn one (Leaflet is retired)', async () => {
    localStorage.setItem('legba_globe', '0')
    render(wrap(<MapPanel />))
    await waitFor(() => expect(screen.getByTestId('globe-map').dataset.renderer).toBe('svg'))
  })
})
