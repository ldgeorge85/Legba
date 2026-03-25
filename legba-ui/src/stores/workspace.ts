import { create } from 'zustand'

export type PanelType =
  | 'dashboard'
  | 'signals'
  | 'events'
  | 'event-detail'
  | 'entities'
  | 'entity-detail'
  | 'sources'
  | 'goals'
  | 'graph'
  | 'map'
  | 'timeline'
  | 'event-stream'
  | 'consult'
  | 'situations'
  | 'watchlist'
  | 'analytics'
  | 'cycle-monitor'
  | 'journal'
  | 'facts'
  | 'reports'
  | 'scorecard'
  | 'proposed-edges'
  | 'hypotheses'
  | 'briefs'
  | 'inbox'
  | 'config'
  | 'users'

export interface PanelRequest {
  type: PanelType
  params?: Record<string, string>
}

export interface PresetRequest {
  panels: PanelType[]
}

export interface CustomLayoutRequest {
  layoutJSON: string
}

interface WorkspaceState {
  sidebarCollapsed: boolean
  toggleSidebar: () => void

  // Panel open requests (consumed by Workspace component)
  pendingPanel: PanelRequest | null
  openPanel: (type: PanelType, params?: Record<string, string>) => void
  clearPending: () => void

  // Layout presets
  pendingPreset: PresetRequest | null
  applyPreset: (panels: PanelType[]) => void
  clearPreset: () => void

  // Custom layout restore
  pendingCustomLayout: CustomLayoutRequest | null
  restoreCustomLayout: (layoutJSON: string) => void
  clearCustomLayout: () => void

  // Save current layout (returns JSON string or null)
  dockviewApiRef: { getLayoutJSON: () => string | null } | null
  setDockviewApiRef: (ref: { getLayoutJSON: () => string | null } | null) => void
  saveCurrentLayout: () => string | null
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  sidebarCollapsed: false,
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  pendingPanel: null,
  openPanel: (type, params) => set({ pendingPanel: { type, params } }),
  clearPending: () => set({ pendingPanel: null }),

  pendingPreset: null,
  applyPreset: (panels) => set({ pendingPreset: { panels } }),
  clearPreset: () => set({ pendingPreset: null }),

  pendingCustomLayout: null,
  restoreCustomLayout: (layoutJSON) => set({ pendingCustomLayout: { layoutJSON } }),
  clearCustomLayout: () => set({ pendingCustomLayout: null }),

  dockviewApiRef: null,
  setDockviewApiRef: (ref) => set({ dockviewApiRef: ref }),
  saveCurrentLayout: () => {
    const ref = get().dockviewApiRef
    if (ref) return ref.getLayoutJSON()
    return null
  },
}))
