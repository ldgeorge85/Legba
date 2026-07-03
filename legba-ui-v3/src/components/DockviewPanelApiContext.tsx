/**
 * DockviewPanelApiContext (S7-T5) — exposes a panel's own Dockview panel api to
 * the component it renders.
 *
 * The App-level `LegbaPanelComponent` receives `props.api` (the per-tile
 * `DockviewPanelApi`) from Dockview but historically dropped it — panels only
 * got `{ registration, scope, mode }`. The TileWebGLOverlay harness wants the
 * authoritative `api.isVisible` / `api.onDidVisibilityChange` signal so a
 * body-portalled WebGL overlay hides the instant its tile is tabbed behind
 * (Dockview keeps inactive tab content mounted). This context threads that api
 * down without widening the panel component contract.
 *
 * It is OPTIONAL: `useDockviewPanelApi()` returns null outside a provider (e.g.
 * in unit tests or a standalone render), and the overlay falls back to its
 * rect/offsetParent visibility tests.
 */
import { createContext, useContext } from 'react'
import type { IDockviewPanelProps } from 'dockview-react'

/** The per-tile panel api Dockview hands to a panel component. */
export type TilePanelApi = IDockviewPanelProps['api']

const DockviewPanelApiContext = createContext<TilePanelApi | null>(null)

export const DockviewPanelApiProvider = DockviewPanelApiContext.Provider

/** The current tile's Dockview panel api, or null when rendered outside a tile. */
export function useDockviewPanelApi(): TilePanelApi | null {
  return useContext(DockviewPanelApiContext)
}
