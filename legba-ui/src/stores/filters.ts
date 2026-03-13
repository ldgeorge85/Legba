import { create } from 'zustand'

interface FiltersState {
  dateRange: { start: Date | null; end: Date | null }
  searchTerm: string
  categories: string[]
  entityTypes: string[]

  setDateRange: (start: Date | null, end: Date | null) => void
  setSearch: (term: string) => void
  toggleCategory: (cat: string) => void
  toggleEntityType: (type: string) => void
  clearFilters: () => void
}

export const useFiltersStore = create<FiltersState>((set) => ({
  dateRange: { start: null, end: null },
  searchTerm: '',
  categories: [],
  entityTypes: [],

  setDateRange: (start, end) => set({ dateRange: { start, end } }),
  setSearch: (term) => set({ searchTerm: term }),

  toggleCategory: (cat) =>
    set((s) => ({
      categories: s.categories.includes(cat)
        ? s.categories.filter((c) => c !== cat)
        : [...s.categories, cat],
    })),

  toggleEntityType: (type) =>
    set((s) => ({
      entityTypes: s.entityTypes.includes(type)
        ? s.entityTypes.filter((t) => t !== type)
        : [...s.entityTypes, type],
    })),

  clearFilters: () =>
    set({
      dateRange: { start: null, end: null },
      searchTerm: '',
      categories: [],
      entityTypes: [],
    }),
}))
