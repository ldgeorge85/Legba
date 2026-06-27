import { useCallback, useEffect, useRef, useState } from 'react'
import type { Core, Layouts, LayoutOptions } from 'cytoscape'

/**
 * Gate the mount of a cytoscape canvas on its container being on-screen + sized.
 *
 * #90 — react-cytoscapejs constructs `new Cytoscape({...})` WITHOUT a `layout`
 * option, so cytoscape's ready handler auto-runs its DEFAULT layout (grid) the
 * first time elements + style become ready. If that first mount happens while the
 * panel is a hidden/0×0 Dockview tab (a background tab brushed by a selection made
 * elsewhere), the default layout reads the detached container box → `boundingBox`
 * is undefined → `TypeError: ... reading 'h'` → the error boundary blanks the
 * panel. The fix is to NOT construct cytoscape until the wrapper is actually
 * visible: attach a ref to the wrapper, observe its size, and only render the
 * `<CytoscapeComponent>` once the box is non-zero. When the tab is later shown the
 * observer flips `visible` true and the canvas mounts cleanly into a real box.
 *
 * It tracks LIVE visibility (not latched): when the tab is hidden again `visible`
 * flips false so the caller UNMOUNTS the canvas. This is deliberate — cytoscape
 * re-runs its (default) layout on every element/style change, so patching new
 * elements into a hidden 0×0 instance (e.g. an entity-graph re-center on a
 * background tab) hits the same detached-box crash. Unmounting while hidden means
 * the re-query's new elements mount cleanly when the tab is next shown.
 *
 * IMPORTANT: `ref` is a CALLBACK ref, not an object ref. Callers often render a
 * loading/empty state (no canvas node) before the canvas div appears; a callback
 * ref re-binds the observers when the real node finally mounts (an object ref +
 * `useEffect([])` would observe `null` during the loading state and never re-run).
 *
 * Usage:
 *   const { ref, visible } = useVisibleSize<HTMLDivElement>()
 *   return <div ref={ref} className="relative ...">{visible && <CytoscapeComponent .../>}</div>
 */
export function useVisibleSize<T extends HTMLElement>(): {
  ref: (node: T | null) => void
  visible: boolean
} {
  const [visible, setVisible] = useState(false)
  const teardown = useRef<(() => void) | null>(null)

  const ref = useCallback((el: T | null) => {
    // Detach from the previous node (the rendered subtree can swap between a
    // loading/empty placeholder and the real canvas div).
    teardown.current?.()
    teardown.current = null
    if (!el) {
      setVisible(false)
      return
    }
    const isVis = () => {
      if (el.offsetParent === null) return false
      const b = el.getBoundingClientRect()
      return b.width > 0 && b.height > 0
    }
    const check = () => setVisible(isVis())
    check()
    const cleanups: Array<() => void> = []
    // A Dockview tab is shown/hidden by toggling `display` (or moving DOM) on an
    // ANCESTOR; ResizeObserver/IntersectionObserver on `el` don't reliably fire for
    // that ancestor-driven transition. A light interval re-check keeps `visible`
    // correct across show↔hide so the canvas mounts when the tab is brushed-then-
    // shown and unmounts when hidden again (live, not latched). 250ms is cheap.
    const poll = window.setInterval(check, 250)
    cleanups.push(() => clearInterval(poll))
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(check)
      ro.observe(el)
      cleanups.push(() => ro.disconnect())
    }
    teardown.current = () => cleanups.forEach((c) => c())
  }, [])

  // Disconnect when the host component unmounts.
  useEffect(() => () => teardown.current?.(), [])

  return { ref, visible }
}

/** Options for {@link attachFitOnResize}. */
export interface FitOnResizeOptions {
  /** Graph layout to run ONCE on the first non-zero-size tick. Omit to only
   *  resize+fit (callers that position elements themselves). */
  layout?: LayoutOptions
  /** Padding (px) passed to `cy.fit`. */
  padding?: number
}

/**
 * Keep a cytoscape canvas sized + fitted to its container, and run its layout
 * only once the container actually has a size.
 *
 * #90 — the blank-graph bug: a cytoscape instance measures its container ONCE at
 * mount and never re-measures. In a Dockview tab the container is 0×0 at mount
 * (the tab isn't laid out yet), so the canvas renders blank and never recovers
 * when the tab finally sizes. WORSE: running a measuring layout (`cose`,
 * `breadthfirst`, `concentric`) while the container is 0×0 dereferences the
 * (undefined) bounding box → `TypeError: Cannot read properties of undefined
 * (reading 'h')` → the error boundary blanks the panel.
 *
 * Fix: never run the layout at mount. A ResizeObserver on the container drives
 * everything — while the container is 0×0 we do nothing; on the FIRST tick where
 * it has a real size we `resize()`, run the (real) layout ONCE, then `fit()`; on
 * later ticks we only `resize()` + `fit()` (re-running the layout every resize
 * caused the Why-graph to jitter). rAF-debounced so a burst coalesces.
 *
 * Backward compatible: `attachFitOnResize(cy)` and `attachFitOnResize(cy, 30)`
 * (padding number) both still work.
 *
 * Returns a cleanup fn — call it before re-attaching (cy() can fire more than once).
 */
export function attachFitOnResize(
  cy: Core,
  opts: FitOnResizeOptions | number = {},
): () => void {
  const { layout, padding = 30 }: FitOnResizeOptions =
    typeof opts === 'number' ? { padding: opts } : opts

  const el = cy.container()
  if (!el || typeof ResizeObserver === 'undefined') return () => {}

  let raf = 0
  let layoutDone = false
  let running: Layouts | null = null

  // True only when the container is genuinely on-screen with a real box. A measuring
  // layout (cose/breadthfirst/concentric) reads this box as `boundingBox.h`; if the
  // box is degenerate it dereferences an undefined bbox → `reading 'h'` (#90).
  //   - `offsetParent === null` ⇒ a `display:none` ancestor (an inactive Dockview
  //     tab) → degenerate even when the stale `clientHeight` says otherwise.
  //   - getBoundingClientRect is the LIVE box.
  const isVisible = () => {
    const he = el as HTMLElement
    if (typeof he.offsetParent !== 'undefined' && he.offsetParent === null) return false
    const box = el.getBoundingClientRect()
    return box.width > 0 && box.height > 0
  }

  const refit = () => {
    cancelAnimationFrame(raf)
    raf = requestAnimationFrame(() => {
      // cy may be torn down between the rAF schedule and fire.
      if (typeof cy.destroyed === 'function' && cy.destroyed()) return
      if (!isVisible()) {
        // The tab went hidden — an iterative layout (cose) schedules its work in
        // deferred frames even with `animate:false`; if it ran while visible and
        // the tab then hides, that deferred frame reads the now-degenerate box and
        // crashes. Stop any in-flight run before it can touch the dead box (#90).
        if (running) { try { running.stop() } catch { /* already done */ } running = null }
        return
      }
      cy.resize()
      // Re-check cytoscape's OWN measured box AFTER resize(). Only run the layout
      // when the box is real AND there are elements to lay out.
      const sized = cy.width() > 0 && cy.height() > 0
      const hasEles = cy.elements().length > 0
      if (!sized || !hasEles) return
      // Belt-and-suspenders: a layout/fit must NEVER blank the panel.
      try {
        if (!layoutDone && layout) {
          // First real size: run the measuring layout exactly once.
          layoutDone = true
          running = cy.layout(layout)
          running.run()
        }
        cy.fit(undefined, padding)
      } catch {
        // Let a later non-zero tick retry the layout if the first attempt threw.
        layoutDone = false
      }
    })
  }
  const ro = new ResizeObserver(refit)
  ro.observe(el)
  refit() // initial — covers the case where the container is already sized
  return () => {
    ro.disconnect()
    cancelAnimationFrame(raf)
    // Stop any in-flight iterative layout (cose batches its work across frames
    // even with animate:false); a deferred frame firing after teardown / after
    // the tab hides reads a degenerate box → `reading 'h'` (#90).
    if (running) { try { running.stop() } catch { /* already done */ } running = null }
  }
}
