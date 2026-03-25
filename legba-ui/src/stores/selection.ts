import { create } from 'zustand'

export interface Selection {
  type: 'entity' | 'event' | 'signal' | 'source' | 'situation'
  id: string
  name: string
}

interface SelectionState {
  // Current focus (existing)
  selected: Selection | null
  history: Selection[]
  historyIndex: number

  // 4D navigation (new)
  focusEntity: string | null
  focusSituation: string | null
  timeWindow: [string, string] | null

  // Actions
  select: (sel: Selection) => void
  deselect: () => void
  goBack: () => void
  goForward: () => void
  setFocusEntity: (id: string | null) => void
  setFocusSituation: (id: string | null) => void
  setTimeWindow: (range: [string, string] | null) => void
}

export const useSelectionStore = create<SelectionState>((set, get) => ({
  selected: null,
  history: [],
  historyIndex: -1,

  // 4D navigation defaults
  focusEntity: null,
  focusSituation: null,
  timeWindow: null,

  select: (sel) => {
    const { history, historyIndex } = get()
    // Trim forward history and append
    const trimmed = history.slice(0, historyIndex + 1)
    trimmed.push(sel)
    // Cap history at 50
    if (trimmed.length > 50) trimmed.shift()
    set({
      selected: sel,
      history: trimmed,
      historyIndex: trimmed.length - 1,
    })
  },

  deselect: () => set({ selected: null }),

  goBack: () => {
    const { history, historyIndex } = get()
    if (historyIndex > 0) {
      const newIndex = historyIndex - 1
      set({
        selected: history[newIndex],
        historyIndex: newIndex,
      })
    }
  },

  goForward: () => {
    const { history, historyIndex } = get()
    if (historyIndex < history.length - 1) {
      const newIndex = historyIndex + 1
      set({
        selected: history[newIndex],
        historyIndex: newIndex,
      })
    }
  },

  setFocusEntity: (id) => set({ focusEntity: id }),
  setFocusSituation: (id) => set({ focusSituation: id }),
  setTimeWindow: (range) => set({ timeWindow: range }),
}))
