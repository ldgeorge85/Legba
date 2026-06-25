/**
 * Panel-tier context.
 *
 * The App renders each panel inside a `PanelTierProvider` carrying the tier
 * from the panel kind's registry definition (`PanelKindDefinition.tier`). A
 * panel's `PanelChrome` reads it via `usePanelTier()` and shows a "Preview"
 * badge when the tier is `'preview'` — without every panel having to thread the
 * tier through itself. A panel can still override by passing `tier=` to
 * `PanelChrome` explicitly.
 *
 * Default is `'live'` (the common case + the value for panels rendered outside
 * a provider, e.g. unit tests mounting a panel directly).
 */

import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'

export type PanelTier = 'preview' | 'live'

const PanelTierContext = createContext<PanelTier>('live')

export function PanelTierProvider({ tier, children }: { tier: PanelTier; children: ReactNode }) {
  return <PanelTierContext.Provider value={tier}>{children}</PanelTierContext.Provider>
}

export function usePanelTier(): PanelTier {
  return useContext(PanelTierContext)
}
