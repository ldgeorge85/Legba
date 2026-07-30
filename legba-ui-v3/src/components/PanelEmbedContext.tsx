/**
 * Panel-embed context — the "double chrome" fix.
 *
 * A `panels/merged/*.tsx` wrapper hoists ONE header (title + tab strip) above
 * several original, unmodified sub-panels (see e.g. `merged/AlertsWatches.tsx`).
 * Each sub-panel is ALSO a standalone Dockview panel in its own right (kept
 * registered-but-hidden so old saved layouts keep resolving), so it still
 * renders its own `PanelChrome` header/border internally. Mounted inside a
 * merged wrapper, that second header stacks underneath the wrapper's own —
 * doubled framing inside what Dockview already frames as one panel.
 *
 * A merged wrapper renders its embedded children inside `PanelEmbedProvider`;
 * `PanelChrome` reads it via `usePanelEmbedded()` and skips its own
 * header/border, rendering just the body — so the chrome renders exactly
 * once. A panel mounted directly by Dockview (the common case, and the
 * hidden-but-still-real standalone kind) sees the default `false` and keeps
 * its normal chrome.
 */

import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'

const PanelEmbedContext = createContext(false)

export function PanelEmbedProvider({ children }: { children: ReactNode }) {
  return <PanelEmbedContext.Provider value={true}>{children}</PanelEmbedContext.Provider>
}

export function usePanelEmbedded(): boolean {
  return useContext(PanelEmbedContext)
}
