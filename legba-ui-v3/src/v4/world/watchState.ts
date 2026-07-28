/**
 * Watch-locations store (P4-3, feature 5) — a thin zustand wrapper over the
 * pure `@/lib/watchLocations` model. Holds the operator's "watch here" points,
 * hydrated from + persisted to localStorage on every mutation. All geometry /
 * proximity math stays in the pure lib (unit-tested); this store only owns the
 * live list + the persistence side-effect.
 */
import { create } from 'zustand'
import {
  addWatch,
  loadWatchLocations,
  makeWatch,
  persistWatchLocations,
  removeWatch,
  setWatchRadius,
  type WatchLocation,
} from '@/lib/watchLocations'

interface WatchState {
  watches: WatchLocation[]
  /** When true, the next map click drops a watch (the map arms this). */
  placing: boolean
  setPlacing: (placing: boolean) => void
  add: (label: string, lat: number, lon: number, radiusKm?: number) => void
  remove: (id: string) => void
  setRadius: (id: string, radiusKm: number) => void
}

export const useWatchState = create<WatchState>((set) => ({
  watches: loadWatchLocations(),
  placing: false,
  setPlacing: (placing) => set({ placing }),
  add: (label, lat, lon, radiusKm) =>
    set((s) => {
      const watches = addWatch(s.watches, makeWatch(label, lat, lon, radiusKm))
      persistWatchLocations(watches)
      return { watches, placing: false }
    }),
  remove: (id) =>
    set((s) => {
      const watches = removeWatch(s.watches, id)
      persistWatchLocations(watches)
      return { watches }
    }),
  setRadius: (id, radiusKm) =>
    set((s) => {
      const watches = setWatchRadius(s.watches, id, radiusKm)
      persistWatchLocations(watches)
      return { watches }
    }),
}))
