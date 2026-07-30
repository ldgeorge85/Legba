/**
 * Component test for the Timeline panel (`system.timeline`) — pins the
 * FIRST-MOUNT rendering path (gallery-2 audit fix).
 *
 * The regression: the panel renders its loading empty-state first, so the
 * plot div (and the width measurement it feeds) mounts only when data lands.
 * The old []-deps ResizeObserver hook never observed that late element →
 * width stayed 0 → the SVG never opened on a fresh mount (close+reopen was
 * the only way to see bars). This test drives exactly that sequence: fetch
 * resolves AFTER the first paint, the fake ResizeObserver reports a size when
 * (and only when) the plot div is actually observed, and the bars MUST appear
 * without any remount.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import type { TimelineResponse } from '@/lib/timelineWindows'
import { DockviewPanelApiProvider } from '@/components/DockviewPanelApiContext'
import type { TilePanelApi } from '@/components/DockviewPanelApiContext'
import TimelinePanel from './Timeline'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_timeline', descriptor_id: 'timeline', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Timeline', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

// A ResizeObserver fake that reports a real width as soon as an element is
// observed — the browser's initial-delivery behaviour, minus the layout pass.
class MeasuringResizeObserver {
  constructor(private cb: ResizeObserverCallback) {}
  observe() {
    this.cb(
      [{ contentRect: { width: 800 } } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    )
  }
  unobserve() {}
  disconnect() {}
}

const NOW = new Date().toISOString()
const HOUR = 3_600_000
const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString()

const TIMELINE: TimelineResponse = {
  days: 30,
  server_now: NOW,
  target_id: null,
  items: [
    { id: 's1', kind: 'situation', label: 'Border clashes', start: iso(72 * HOUR), end: null, status: 'active' },
    { id: 'f1', kind: 'finding', label: 'Verified finding', start: iso(48 * HOUR), end: iso(6 * HOUR), severity: 'high' },
    { id: 'a1', kind: 'fact', label: 'Validity band', start: iso(24 * HOUR), end: null },
  ],
  counts: { situation: 1, finding: 1, fact: 1 },
  truncated: {},
}

const realRO = globalThis.ResizeObserver

beforeEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = MeasuringResizeObserver
})

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = realRO
  vi.restoreAllMocks()
})

describe('TimelinePanel', () => {
  it('renders bars on FIRST data arrival (no close+reopen required)', async () => {
    // Fetch resolves on a later tick, guaranteeing the loading empty-state
    // paints first — the exact sequence the []-deps hook could not survive.
    let resolveFetch: (v: unknown) => void = () => {}
    const gate = new Promise((r) => {
      resolveFetch = r
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('/v3/timeline')) {
          await gate
          return { ok: true, json: async () => TIMELINE }
        }
        return { ok: true, json: async () => ({}) }
      }),
    )

    render(wrap(<TimelinePanel registration={reg()} scope={{}} mode="personal" />))

    // First paint: the honest loading empty-state, no plot yet.
    expect(screen.getByTestId('timeline-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('timeline-plot')).not.toBeInTheDocument()

    resolveFetch(undefined)

    // Data lands → the plot mounts, gets measured, and the bars draw — all
    // three lanes, on the very first mount.
    await waitFor(() => expect(screen.getByTestId('timeline-plot')).toBeInTheDocument())
    await waitFor(() => {
      expect(screen.getByTestId('timeline-bar-situation')).toBeInTheDocument()
      expect(screen.getByTestId('timeline-bar-finding')).toBeInTheDocument()
      expect(screen.getByTestId('timeline-bar-fact')).toBeInTheDocument()
    })

    // The per-kind tally reflects the same rows (honest counts, no cap hit).
    expect(screen.getByTestId('timeline-tally-situation').textContent).toContain('1')
  })

  it('recovers plot width when a hidden-mounted Dockview tile becomes visible', async () => {
    // The SECOND blank-canvas class (the callback-ref fix above doesn't
    // cover it): a panel mounted into a Dockview tile that starts hidden (a
    // background tab, or a tile Dockview hasn't laid out yet). A fake tile
    // api that starts NOT visible, with the visibility-change subscription
    // `useDockviewTileRedraw` registers.
    let tileVisible = false
    const visHandlers: Array<(e: { isVisible: boolean }) => void> = []
    const fakeApi = {
      get isVisible() {
        return tileVisible
      },
      onDidVisibilityChange(cb: (e: { isVisible: boolean }) => void) {
        visHandlers.push(cb)
        return { dispose: () => {} }
      },
      onDidDimensionsChange() {
        return { dispose: () => {} }
      },
    } as unknown as TilePanelApi

    // A ResizeObserver that reports the tile's REAL box on `observe()` (0
    // while hidden, 800 once visible) but never fires again on its own once
    // observed — exactly the "misses the hidden→visible transition" failure
    // useTileRedraw.ts documents. Only a FRESH observe() call (the
    // callback-ref detach/reattach a `key` remount forces) picks up the
    // tile's current size.
    class TileAwareResizeObserver {
      constructor(private cb: ResizeObserverCallback) {}
      observe() {
        const width = tileVisible ? 800 : 0
        this.cb(
          [{ contentRect: { width } } as unknown as ResizeObserverEntry],
          this as unknown as ResizeObserver,
        )
      }
      unobserve() {}
      disconnect() {}
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).ResizeObserver = TileAwareResizeObserver

    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('/v3/timeline')) return { ok: true, json: async () => TIMELINE }
        return { ok: true, json: async () => ({}) }
      }),
    )

    render(
      wrap(
        <DockviewPanelApiProvider value={fakeApi}>
          <TimelinePanel registration={reg()} scope={{}} mode="personal" />
        </DockviewPanelApiProvider>,
      ),
    )

    // Data lands and the plot div mounts, but the tile is still hidden — the
    // subtitle honestly reports the item count (shaped from the fetch,
    // independent of measurement) while the canvas stays blank. This is the
    // exact gallery-2 "278 ranged items" + empty-plot symptom.
    await waitFor(() => expect(screen.getByTestId('timeline-plot')).toBeInTheDocument())
    expect(
      screen.queryByRole('img', { name: 'validity-window timeline' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByTestId('timeline-bar-situation')).not.toBeInTheDocument()

    // The Dockview tile activates (tab switch / layout settles).
    tileVisible = true
    visHandlers.forEach((h) => h({ isVisible: true }))

    // The bars must draw WITHOUT any remount or manual interaction from here.
    await waitFor(() => {
      expect(screen.getByTestId('timeline-bar-situation')).toBeInTheDocument()
      expect(screen.getByTestId('timeline-bar-finding')).toBeInTheDocument()
      expect(screen.getByTestId('timeline-bar-fact')).toBeInTheDocument()
    })
  })
})
