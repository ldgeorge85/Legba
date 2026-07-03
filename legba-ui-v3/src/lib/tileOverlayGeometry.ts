/**
 * Pure geometry / visibility helpers for the TileWebGLOverlay harness (S7-T5).
 *
 * THE PROBLEM (S7-T2 spike): a WebGL canvas painted inside a Dockview tile stays
 * invisible. Dockview positions tiles with CSS `transform`, and Chrome refuses
 * to composite a WebGL layer that has a transformed ancestor — the canvas paints
 * but never shows (the "black tile" the spike hit on BOTH dockview v4 and v7).
 * This kills maplibre-gl (map) and sigma.js (graph); Leaflet survives only
 * because it is DOM/SVG, not WebGL.
 *
 * THE UNLOCK: render the WebGL canvas in a `position: fixed` overlay portalled
 * to `document.body` — OUTSIDE every tile transform — and continuously mirror
 * the tile's on-screen rect onto it. These pure helpers make the per-frame
 * "should the overlay show, and where?" decision, so it is unit-tested without a
 * DOM (the imperative rAF loop lives in TileWebGLOverlay).
 */

export interface TileRect {
  left: number
  top: number
  width: number
  height: number
}

export interface TileVisibilityInput {
  /** The anchor element is still attached to the document. */
  connected: boolean
  /**
   * `offsetParent !== null`. It is null when the anchor (or any ancestor) is
   * `display:none` — which is how Dockview hides an inactive tab's content, so
   * a tabbed-behind tile reports 0×0 and no offset parent. (Also null for a
   * `position:fixed` anchor, but the anchor here is always in normal flow.)
   */
  hasOffsetParent: boolean
  /** The anchor's measured bounding rect. */
  rect: TileRect
  /**
   * Dockview's own panel-visibility signal (`api.isVisible`) when the panel api
   * is threaded through. `undefined` = no dockview api in context → ignored, and
   * the rect/offsetParent tests carry the decision on their own.
   */
  dockviewVisible?: boolean
  /** Panel-level force-hide (an explicit `hidden` prop on the overlay). */
  forceHidden?: boolean
}

/** Below this many px in either axis, a tile rect reads as collapsed/hidden
 *  rather than genuinely laid out. */
export const MIN_TILE_PX = 2

/**
 * Decide whether the portalled overlay should be shown this frame. Any single
 * "hidden" signal wins (fail-closed): a hidden overlay is a harmless empty div,
 * but a stale-visible one would float its WebGL canvas over the wrong tile.
 */
export function isTileVisible(input: TileVisibilityInput): boolean {
  if (input.forceHidden) return false
  if (!input.connected) return false
  if (!input.hasOffsetParent) return false
  if (input.dockviewVisible === false) return false
  return input.rect.width >= MIN_TILE_PX && input.rect.height >= MIN_TILE_PX
}

/**
 * Whether the mirrored geometry moved enough to warrant a DOM write. Skipping
 * sub-pixel no-ops keeps the rAF loop from thrashing layout every frame when the
 * tile is stationary.
 */
export function rectChanged(a: TileRect, b: TileRect, epsilon = 0.5): boolean {
  return (
    Math.abs(a.left - b.left) > epsilon ||
    Math.abs(a.top - b.top) > epsilon ||
    Math.abs(a.width - b.width) > epsilon ||
    Math.abs(a.height - b.height) > epsilon
  )
}
