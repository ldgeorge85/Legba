/**
 * useDockviewTileRedraw (P0-2f) — re-measure canvas/chart surfaces when their
 * Dockview tile becomes visible.
 *
 * THE background-tab blank-surface class: Dockview keeps inactive tab content
 * MOUNTED but hidden, so a WebGL map (maplibre) or a measured chart (recharts
 * `ResponsiveContainer`) that mounts into a hidden tile initializes against a
 * zero-size box and stays blank when the tab is activated — a ResizeObserver
 * alone can miss the transition. This hook subscribes to the tile's own panel
 * api (`onDidVisibilityChange` / `onDidDimensionsChange`, threaded through
 * `DockviewPanelApiContext`) and:
 *
 *   - calls `onRedraw` one frame after the tile becomes visible (and on size
 *     changes while visible) — wire `map.resize()` / `fitView()` here;
 *   - returns a monotonically-increasing tick that bumps on each
 *     hidden→visible transition — use it as a `key` to remount components
 *     that only measure on mount (recharts `ResponsiveContainer`).
 *
 * Outside a Dockview tile (unit tests, standalone render) the api context is
 * null and the hook is inert: tick stays 0 and `onRedraw` is never called.
 */
import { useEffect, useRef, useState } from 'react'
import { useDockviewPanelApi } from '@/components/DockviewPanelApiContext'

export function useDockviewTileRedraw(onRedraw?: () => void): number {
  const api = useDockviewPanelApi()
  const [tick, setTick] = useState(0)

  // Latest callback in a ref so an unstable identity never re-subscribes.
  const cbRef = useRef(onRedraw)
  cbRef.current = onRedraw

  useEffect(() => {
    if (!api) return
    let visRaf = 0
    let dimRaf = 0

    const visibility = api.onDidVisibilityChange((e: { isVisible: boolean }) => {
      if (!e.isVisible) return
      cancelAnimationFrame(visRaf)
      // Defer a frame so Dockview has laid the tile out at its real size
      // before consumers measure. Redraw FIRST (e.g. map.resize()), then bump
      // the tick so key-remounted charts re-measure the corrected box.
      visRaf = requestAnimationFrame(() => {
        cbRef.current?.()
        setTick((t) => t + 1)
      })
    })

    const dimensions = api.onDidDimensionsChange(() => {
      if (!api.isVisible) return
      cancelAnimationFrame(dimRaf)
      // Size-only change on a live tile: redraw, no remount tick (per-frame
      // remounts during a drag-resize would thrash).
      dimRaf = requestAnimationFrame(() => {
        cbRef.current?.()
      })
    })

    return () => {
      cancelAnimationFrame(visRaf)
      cancelAnimationFrame(dimRaf)
      visibility.dispose()
      dimensions.dispose()
    }
  }, [api])

  return tick
}
