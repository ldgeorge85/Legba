import { create } from 'zustand'

export type PanelType =
  | 'dashboard'
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

export interface PanelRequest {
  type: PanelType
  params?: Record<string, string>
}

interface WorkspaceState {
  sidebarCollapsed: boolean
  toggleSidebar: () => void

  // Panel open requests (consumed by Workspace component)
  pendingPanel: PanelRequest | null
  openPanel: (type: PanelType, params?: Record<string, string>) => void
  clearPending: () => void
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  sidebarCollapsed: false,
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  pendingPanel: null,
  openPanel: (type, params) => set({ pendingPanel: { type, params } }),
  clearPending: () => set({ pendingPanel: null }),
}))
