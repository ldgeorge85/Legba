import type { Core } from 'cytoscape'

/**
 * Keep a cytoscape canvas sized + fitted to its container.
 *
 * #90 — the blank-graph bug: a cytoscape instance measures its container ONCE at
 * mount and never re-measures. In a Dockview tab the container is 0×0 at mount
 * (the tab isn't laid out yet), so the canvas renders blank and never recovers
 * when the tab finally sizes. A ResizeObserver on the container fixes it: on every
 * resize (including the first real one) we `resize()` the renderer and `fit()` the
 * graph into view. rAF-debounced so a burst of resizes coalesces.
 *
 * Returns a cleanup fn — call it before re-attaching (cy() can fire more than once).
 */
export function attachFitOnResize(cy: Core, padding = 30): () => void {
  const el = cy.container()
  if (!el || typeof ResizeObserver === 'undefined') return () => {}
  let raf = 0
  const refit = () => {
    cancelAnimationFrame(raf)
    raf = requestAnimationFrame(() => {
      // cy may be torn down between the rAF schedule and fire.
      if (typeof cy.destroyed === 'function' && cy.destroyed()) return
      cy.resize()
      if (cy.elements().length > 0) cy.fit(undefined, padding)
    })
  }
  const ro = new ResizeObserver(refit)
  ro.observe(el)
  refit() // initial — covers the case where the container is already sized
  return () => {
    ro.disconnect()
    cancelAnimationFrame(raf)
  }
}
