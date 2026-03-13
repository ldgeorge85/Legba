import { create } from 'zustand'

export interface Selection {
  type: 'entity' | 'event' | 'source' | 'situation'
  id: string
  name: string
}

interface SelectionState {
  selected: Selection | null
  history: Selection[]
  historyIndex: number

  select: (sel: Selection) => void
  deselect: () => void
  goBack: () => void
  goForward: () => void
}

export const useSelectionStore = create<SelectionState>((set, get) => ({
  selected: null,
  history: [],
  historyIndex: -1,

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
}))
