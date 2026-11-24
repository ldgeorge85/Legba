/**
 * useTileVisible — true while this panel's Dockview tile is actually in view.
 *
 * Dockview keeps an inactive tab's React tree MOUNTED, so a panel that fetches
 * or computes on mount does that work for every saved layout that merely
 * contains its tab. A heavy panel gates its work on this hook instead (N-Q:
 * the Flow panel is inert until it is shown). Outside a tile (unit tests, a
 * standalone render) there is no api and the hook answers true.
 */
import { useEffect, useState } from 'react'
import { useDockviewPanelApi } from '@/components/DockviewPanelApiContext'

export function useTileVisible(): boolean {
  const api = useDockviewPanelApi()
  const [visible, setVisible] = useState<boolean>(() => (api ? api.isVisible : true))
  useEffect(() => {
    if (!api) return
    setVisible(api.isVisible)
    const sub = api.onDidVisibilityChange((e: { isVisible: boolean }) => setVisible(e.isVisible))
    return () => sub.dispose()
  }, [api])
  return visible
}

/**
 * Latches: false until the tile has STAYED visible for 50 ms, then true for
 * good. The guard ignores the flash a seed walk produces when it adds a
 * tab (active for a moment) and then re-activates the stance's default tab.
 */
export function useSeenOnce(visible: boolean): boolean {
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    if (!visible || seen) return
    const t = setTimeout(() => setSeen(true), 50)
    return () => clearTimeout(t)
  }, [visible, seen])
  return seen
}
