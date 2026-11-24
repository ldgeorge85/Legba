/**
 * frameGrid — THE FRAME'S GRID, as numbers (U-6; 02_UI_TARGET_BRIEF §12).
 *
 *  ┌──────────────────────────── top bar ─────────────────────────────────────┐
 *  │ rail │      CENTRE (a reading measure)       │  PANELS (the rest)        │
 *  │      │  ┌─────────── STAGE ───────────────┐  │  1 column  < 2 × min      │
 *  │ subj │  │ today's read / what was pulled  │  │  2 columns ≥ 2 × min      │
 *  │ hist │  ├══════════ divider ══════════════┤  │  3 columns ≥ 3 × min      │
 *  │ view │  │ CHAT — Consult, ask box at foot │  │  (never more columns than │
 *  │      │  └─────────────────────────────────┘  │   the page seeds regions) │
 *  ├──────┴────────────── time band ─────────────┴───────────────────────────┤
 *  └──────────────────────────── status line ────────────────────────────────┘
 *
 * Everything is in REM so one root size scales the whole console: the root is
 * 16 px below 2200 px of width, 18 px from 2200 and 24 px from 3000 (the CSS
 * in `globals.css` steps `html { font-size }` at the same widths). So text at
 * 3840 is 1.5× its 1920 size instead of the same 12–14 px the owner found
 * tiny, and every measure below grows with it.
 *
 *   | width | root | rail (open / shut) | centre measure | min panel column | columns |
 *   |-------|------|--------------------|----------------|------------------|---------|
 *   | 1366  | 16   | 240 / 48 (shut)    | 576            | 480              | 1       |
 *   | 1920  | 16   | 240 (open)         | 768            | 480              | 1       |
 *   | 2560  | 18   | 270 (open)         | 864            | 540              | 2       |
 *   | 3840  | 24   | 360 (open)         | 1152           | 720              | 3       |
 *
 * Centre measure = clamp(36rem, 40vw, 48rem) — a chat reading width that
 * grows modestly with the screen and never runs unbounded. The panels take the
 * whole remaining width, so there is no dead band and nothing stretches: a
 * column is never narrower than 30rem, and a page that seeds two regions uses
 * two columns even where three would fit (the extra width goes to the lead).
 *
 * Pure: no DOM, no React. `App.tsx` and `lib/surfaceDock.ts` read it; the
 * walk's budgets (`tools/walk/budgets.mjs`) restate the same numbers.
 */

/** The widths at which the root type size steps up (CSS px of the viewport). */
export const TYPE_STEPS: readonly { minWidth: number; rootPx: number }[] = [
  { minWidth: 0, rootPx: 16 },
  { minWidth: 2200, rootPx: 18 },
  { minWidth: 3000, rootPx: 24 },
]

/** The root font size (px) at a viewport width. */
export function rootPxFor(viewportWidth: number): number {
  let px = TYPE_STEPS[0].rootPx
  for (const s of TYPE_STEPS) if (viewportWidth >= s.minWidth) px = s.rootPx
  return px
}

/** The rail, open and shut (rem). */
export const RAIL_OPEN_REM = 15
export const RAIL_SHUT_REM = 3
/** Below this width the rail starts shut (it can still be opened). */
export const RAIL_OPEN_FROM = 1600

/** The centre's reading measure: clamp(min, vw share, max), in rem / fraction. */
export const CENTRE_MIN_REM = 36
export const CENTRE_VW = 0.4
export const CENTRE_MAX_REM = 48

/** A panel column is never narrower than this (rem). */
export const PANEL_COLUMN_MIN_REM = 30
/** At most this many panel columns. */
export const MAX_PANEL_COLUMNS = 3

/** The chat's share of the centre's height at rest, and its range (fractions). */
export const CHAT_DEFAULT_SHARE = 0.5
export const CHAT_MAX_SHARE = 0.92
/** The ask bar alone (rem): what the chat collapses to. */
export const ASK_BAR_REM = 9
/** A side column of Consult (moved left or right), default width (rem). */
export const CONSULT_SIDE_REM = 30

export interface FrameGrid {
  rootPx: number
  railPx: number
  centrePx: number
  panelsPx: number
  /** Panel columns this width affords (before the page's region count caps it). */
  columns: number
  minColumnPx: number
}

/** The centre measure at a width (px). */
export function centreMeasurePx(viewportWidth: number): number {
  const root = rootPxFor(viewportWidth)
  return Math.round(Math.min(CENTRE_MAX_REM * root, Math.max(CENTRE_MIN_REM * root, CENTRE_VW * viewportWidth)))
}

/** How many panel columns `panelsPx` affords at `rootPx`. */
export function columnsFor(panelsPx: number, rootPx: number): number {
  const min = PANEL_COLUMN_MIN_REM * rootPx
  return Math.max(1, Math.min(MAX_PANEL_COLUMNS, Math.floor(panelsPx / min)))
}

/** The grid at a viewport width, rail open or shut (default: by width). */
export function frameGrid(viewportWidth: number, railOpen = viewportWidth >= RAIL_OPEN_FROM): FrameGrid {
  const rootPx = rootPxFor(viewportWidth)
  const railPx = (railOpen ? RAIL_OPEN_REM : RAIL_SHUT_REM) * rootPx
  const centrePx = centreMeasurePx(viewportWidth)
  const panelsPx = Math.max(0, viewportWidth - railPx - centrePx)
  return { rootPx, railPx, centrePx, panelsPx, columns: columnsFor(panelsPx, rootPx), minColumnPx: PANEL_COLUMN_MIN_REM * rootPx }
}

/** A region the page seeds on the panel side: its views and its width weight. */
export interface PlacedRegion<R extends string = string> {
  id: R
  weight: number
}

/** Where a region goes: a column, and (when a column holds two) its row. */
export interface RegionPlacement<R extends string = string> {
  id: R
  column: number
  /** 0 = the column's top region; 1 = under it. */
  row: number
  /** Share of the panel side's width for its column (0–1). */
  widthShare: number
  /** Share of its column's height (0–1). */
  heightShare: number
}

/**
 * Lay `regions` (in order, the lead first) into at most `columns` columns.
 *
 *   - as many columns as there are regions, capped by `columns`;
 *   - one region per column while they fit, widths by weight;
 *   - regions past the last column stack UNDER the last column's region, the
 *     lead keeping the larger share of the height (60 %).
 */
export function placeRegions<R extends string>(regions: readonly PlacedRegion<R>[], columns: number): RegionPlacement<R>[] {
  if (regions.length === 0) return []
  const cols = Math.max(1, Math.min(columns, regions.length))
  const top = regions.slice(0, cols)
  const total = top.reduce((n, r) => n + r.weight, 0) || 1
  const out: RegionPlacement<R>[] = top.map((r, i) => ({
    id: r.id,
    column: i,
    row: 0,
    widthShare: r.weight / total,
    heightShare: 1,
  }))
  const rest = regions.slice(cols)
  if (rest.length > 0) {
    const last = out[out.length - 1]
    last.heightShare = 0.6
    const each = 0.4 / rest.length
    rest.forEach((r, i) =>
      out.push({ id: r.id, column: last.column, row: i + 1, widthShare: last.widthShare, heightShare: each }),
    )
  }
  return out
}

/** Clamp a chat share to its range (the ask bar's floor is in px, so it needs the height). */
export function clampChatShare(share: number, centreHeightPx: number, rootPx = 16): number {
  const floor = centreHeightPx > 0 ? Math.min(0.5, (ASK_BAR_REM * rootPx) / centreHeightPx) : 0.1
  if (!Number.isFinite(share)) return CHAT_DEFAULT_SHARE
  return Math.min(CHAT_MAX_SHARE, Math.max(floor, share))
}
