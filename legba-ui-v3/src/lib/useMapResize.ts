/**
 * useMapResize (v4 + v3 fix) — FROZEN surface.
 *
 * THE blank-map root cause: MapLibre initialized inside a flex / Dockview tile
 * that starts at 0 height renders nothing, forever, with no error. This observes
 * the container and calls `map.resize()` on every size change (and once on the
 * next frame after mount), so the canvas fills the moment the tile gets a size.
 */
import { useEffect, type RefObject } from 'react'

export function useMapResize(
  containerRef: RefObject<HTMLElement | null>,
  getMap: () => { resize: () => void } | null | undefined,
): void {
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      getMap()?.resize()
    })
    ro.observe(el)
    const raf = requestAnimationFrame(() => getMap()?.resize())
    return () => {
      ro.disconnect()
      cancelAnimationFrame(raf)
    }
  }, [containerRef, getMap])
}
