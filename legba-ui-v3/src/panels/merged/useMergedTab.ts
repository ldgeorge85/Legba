/**
 * useMergedTab — the tab state every merged wrapper of UI-B1/merges shares.
 *
 * Two ways a tab is asked for, and the wrapper honours both:
 *
 *   1. at MOUNT, from `PanelProps.initialTab` — a retired id resolved onto this
 *      survivor through `panel-registry/aliases.ts` (opening `system.audit`
 *      lands on Ledgers' Audit tab), or a saved layout's `tab` param;
 *   2. while MOUNTED, from a tab request the shell's singleton opener posts
 *      when the survivor is already on the wall (`state/panelParams.ts::
 *      requestPanelTab`) — without it, opening a retired id whose survivor is
 *      open would only re-focus the tile on whatever tab it was showing.
 *
 * An unknown tab is ignored in both cases (`initialTab` falls back to the
 * wrapper's default): a stale tab name must never render an empty surface.
 */
import { useState } from 'react'
import { initialTab, type PanelTabDef } from '@/components/PanelTabs'
import { tabRequestKey, usePanelOpenParams } from '@/state/panelParams'

export function useMergedTab<T extends string>(
  kind: string,
  requested: string | undefined,
  tabs: readonly PanelTabDef[],
  fallback: T,
): [T, (tab: T) => void] {
  const [tab, setTab] = useState<T>(() => initialTab(requested, tabs, fallback) as T)
  usePanelOpenParams(tabRequestKey(kind), (params) => {
    const asked = params.tab
    if (typeof asked === 'string' && tabs.some((t) => t.id === asked)) setTab(asked as T)
  })
  return [tab, setTab]
}
