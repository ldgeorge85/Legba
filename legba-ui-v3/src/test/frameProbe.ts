/**
 * frameProbe — what the mounted frame shows, for the App-level tests (U-6):
 * the panel side's Dockview ids (from the shell port's snapshot, whose own
 * keys are the dock's serialized arrangement) and the stage's current view.
 */
import { getShell } from '@/lib/applyAction'

export interface DockLayout {
  grid?: { root?: unknown }
  panels?: Record<string, { params?: Record<string, unknown> }>
}

export const dockLayout = (): DockLayout => (getShell()?.serializeLayout() as DockLayout) ?? {}

/** The panel side's view ids. */
export const dockIds = (): string[] => Object.keys(dockLayout().panels ?? {})

/** The stage's current view (instance id), or null. */
export const stageView = (): string | null =>
  document.querySelector('[data-testid="stage"]')?.getAttribute('data-stage-view') || null

/** Everything on screen: the stage's view, then the panel side's. */
export const screenIds = (): string[] => {
  const s = stageView()
  return [...(s ? [s] : []), ...dockIds()]
}

/** The panel side's regions (each a list of view ids), left to right. */
export const dockRegions = (): string[][] => {
  const leaves = (node: unknown): string[][] => {
    if (!node || typeof node !== 'object') return []
    const n = node as { type?: string; data?: unknown }
    if (n.type === 'leaf') return [((n.data as { views?: string[] })?.views ?? []).slice()]
    return Array.isArray(n.data) ? n.data.flatMap(leaves) : []
  }
  return leaves(dockLayout().grid?.root)
}
