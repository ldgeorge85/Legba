/**
 * TileWebGLOverlay (S7-T5) — the position-sync overlay harness that lets a WebGL
 * canvas render inside a Dockview tile.
 *
 * WHY (S7-T2 spike): Dockview lays tiles out with CSS `transform`, and Chrome
 * will not composite a WebGL layer that has a transformed ancestor — the canvas
 * paints but stays BLACK (reproduced on dockview v4 AND v7). Leaflet works
 * in-tile because it is DOM/SVG; maplibre-gl and sigma.js do not.
 *
 * HOW: this renders TWO things:
 *   1. an in-tile ANCHOR div (normal flow, reserves the tile's space and is what
 *      we measure), and
 *   2. the real `children` (the WebGL canvas + its chrome) inside a
 *      `position: fixed` div PORTALLED to `document.body` — outside every tile
 *      transform, so WebGL composites normally.
 * A rAF loop mirrors the anchor's `getBoundingClientRect()` onto the portalled
 * overlay every frame (writing style directly — no React re-render), and hides
 * the overlay when the tile is tabbed-behind / collapsed / detached. This
 * transparently handles panel drag, split resize, window resize, and tab
 * switching (see `isTileVisible` for the visibility decision).
 *
 * Reusable: pass any WebGL renderer as `children` (used by the World map's
 * maplibre-gl canvas; a sigma.js graph could reuse it identically).
 *
 * Z-ORDER: the overlay defaults to `zIndex: 30` — above tiles, BELOW the command
 * palette (z-50) and other modal chrome, which live in the same top-level
 * stacking context (`#root` creates none) and therefore paint over it. The map's
 * own controls (layers/legend/drawer) are rendered INSIDE `children`, so they
 * sit above the canvas rather than under the body overlay.
 *
 * LIMITATION (documented, not a regression): the overlay draws above ALL in-tile
 * Dockview content, so a FLOATING/popout panel dragged over this tile would be
 * occluded by the overlay. The default mission-control layout is fully tiled, so
 * this does not arise there; a production hardening would add an
 * `elementFromPoint` occlusion test.
 */
import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/cn'
import { useDockviewPanelApi } from './DockviewPanelApiContext'
import { isTileVisible, rectChanged, type TileRect } from '@/lib/tileOverlayGeometry'

export interface TileWebGLOverlayProps {
  children: ReactNode
  /** z-index of the portalled overlay. Default 30 (above tiles, below modals). */
  zIndex?: number
  /** Extra classes on the portalled overlay container. */
  className?: string
  /** Force-hide the overlay regardless of geometry (panel-level override). */
  hidden?: boolean
  /**
   * Fired when visibility flips or the mirrored size changes — e.g. so a WebGL
   * renderer can `map.resize()`. Stored in a ref, so its identity need not be
   * stable.
   */
  onGeometry?: (g: { width: number; height: number; visible: boolean }) => void
}

export function TileWebGLOverlay({
  children,
  zIndex = 30,
  className,
  hidden,
  onGeometry,
}: TileWebGLOverlayProps) {
  const anchorRef = useRef<HTMLDivElement>(null)
  const overlayRef = useRef<HTMLDivElement>(null)
  const api = useDockviewPanelApi()

  const geomCb = useRef(onGeometry)
  geomCb.current = onGeometry

  // Latest Dockview visibility, read by the rAF loop without re-subscribing.
  const apiVisibleRef = useRef<boolean | undefined>(api?.isVisible)
  useLayoutEffect(() => {
    apiVisibleRef.current = api?.isVisible
    if (!api) return
    const disposable = api.onDidVisibilityChange((e: { isVisible: boolean }) => {
      apiVisibleRef.current = e.isVisible
    })
    return () => disposable.dispose()
  }, [api])

  useLayoutEffect(() => {
    let raf = 0
    let shown: boolean | null = null
    const last: TileRect = { left: -1, top: -1, width: -1, height: -1 }

    const tick = () => {
      raf = requestAnimationFrame(tick)
      const anchor = anchorRef.current
      const overlay = overlayRef.current
      if (!anchor || !overlay) return

      const r = anchor.getBoundingClientRect()
      const rect: TileRect = { left: r.left, top: r.top, width: r.width, height: r.height }
      const visible = isTileVisible({
        connected: anchor.isConnected,
        hasOffsetParent: anchor.offsetParent !== null,
        rect,
        dockviewVisible: apiVisibleRef.current,
        forceHidden: hidden,
      })

      if (visible !== shown) {
        shown = visible
        overlay.style.display = visible ? 'block' : 'none'
        geomCb.current?.({ width: rect.width, height: rect.height, visible })
      }
      if (!visible) return

      if (rectChanged(rect, last)) {
        overlay.style.left = `${rect.left}px`
        overlay.style.top = `${rect.top}px`
        overlay.style.width = `${rect.width}px`
        overlay.style.height = `${rect.height}px`
        last.left = rect.left
        last.top = rect.top
        last.width = rect.width
        last.height = rect.height
        geomCb.current?.({ width: rect.width, height: rect.height, visible: true })
      }
    }

    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [hidden])

  return (
    <>
      {/* In-tile anchor: reserves the tile space + is what the loop measures.
          Empty + aria-hidden — the real UI lives in the portalled overlay. */}
      <div ref={anchorRef} className="h-full w-full" data-testid="tile-webgl-anchor" aria-hidden />
      {createPortal(
        <div
          ref={overlayRef}
          className={cn('fixed', className)}
          // Starts hidden at 0×0; the rAF loop shows + positions it on the first
          // frame once the anchor has a real rect.
          style={{ left: 0, top: 0, width: 0, height: 0, zIndex, display: 'none' }}
          data-testid="tile-webgl-overlay"
        >
          {children}
        </div>,
        document.body,
      )}
    </>
  )
}
