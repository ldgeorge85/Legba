/**
 * The World — shared state store (orchestrator-owned contract). The map
 * (Track A · agent A), the scrubber/layers (agent B), and the rails (agent C)
 * all read/write this, so they compose without importing each other.
 */
import { create } from 'zustand'
import type { Severity, WorldSignal, WorldFinding } from './types'

export type WorldLayer = 'signals' | 'findings' | 'situations' | 'entities'

export interface DrawerState {
  open: boolean
  title: string
  signals: WorldSignal[]
  findings: WorldFinding[]
}

/**
 * Plot filters — narrow what's drawn, on top of the per-layer on/off toggles.
 * `null` means "no constraint". `minSeverity` is a floor (plot points at or
 * above that severity rank); `source`/`country` are exact-match (sourceId /
 * ISO2 country code) and apply to whichever layers carry that dimension.
 */
export interface WorldFilters {
  minSeverity: Severity | null
  source: string | null
  country: string | null
}

export type WorldFilterKey = keyof WorldFilters

/**
 * Dropdown options the map publishes (derived from the windowed data) so the
 * LayerPanel can offer concrete source/country choices without importing the
 * data hooks — same decoupling pattern as `counts`.
 */
export interface FilterOptions {
  sources: string[]
  countries: string[]
}

/**
 * The read currently being LENSED (P1-T7). The temporal / geo / node-graph
 * lenses publish the selected country-or-finding read + its directly-cited
 * evidence signal ids here so the World map can brush the SAME read's evidence
 * (a subtle emphasis) without importing the lens. `null` = no active read.
 */
export interface ReadScope {
  /** The anchoring selection kind — 'target' (country) or 'finding'. */
  kind: string
  /** The read's substrate id (target descriptor id / finding id). */
  id: string
  /** The country the read assesses (signals/findings are fetched by it). */
  targetId: string | null
  label?: string
  /** Cited signal ids — the read's directly-cited evidence (emphasis set). */
  signalIds: string[]
}

const DAY_MS = 24 * 60 * 60 * 1000

interface WorldState {
  /** Time window (epoch ms). Default = last 24h; the scrubber drives it. */
  windowStartMs: number
  windowEndMs: number
  setWindow: (startMs: number, endMs: number) => void

  /** Playback for the scrubber. */
  playing: boolean
  speed: number
  setPlaying: (p: boolean) => void
  setSpeed: (s: number) => void

  /** Layer visibility (the Windy-style switcher). */
  layers: Record<WorldLayer, boolean>
  toggleLayer: (l: WorldLayer) => void

  /** Plot filters (severity floor + source/country) and their option lists. */
  filters: WorldFilters
  setFilter: <K extends WorldFilterKey>(k: K, v: WorldFilters[K]) => void
  clearFilters: () => void
  filterOptions: FilterOptions
  setFilterOptions: (o: FilterOptions) => void

  /**
   * Time-decay: when on, older points fade/shrink toward `windowEndMs` so the
   * map shows a decaying recent picture rather than an ever-growing pile.
   */
  decay: boolean
  toggleDecay: () => void

  /** Live counts per layer (badges); set by the map/rails. */
  counts: Partial<Record<WorldLayer, number>>
  setCount: (l: WorldLayer, n: number) => void

  /** Drill-down drawer — map cluster/hex click → event list. */
  drawer: DrawerState
  openDrawer: (d: Omit<DrawerState, 'open'>) => void
  closeDrawer: () => void

  /** The read currently being lensed (P1-T7) — set by the lenses, read by the
   *  World map so it brushes the same read's evidence. */
  readScope: ReadScope | null
  setReadScope: (s: ReadScope | null) => void
}

export const useWorldState = create<WorldState>((set) => ({
  windowStartMs: Date.now() - DAY_MS,
  windowEndMs: Date.now(),
  setWindow: (windowStartMs, windowEndMs) => set({ windowStartMs, windowEndMs }),

  playing: false,
  speed: 1,
  setPlaying: (playing) => set({ playing }),
  setSpeed: (speed) => set({ speed }),

  layers: { signals: true, findings: true, situations: true, entities: false },
  toggleLayer: (l) =>
    set((s) => ({ layers: { ...s.layers, [l]: !s.layers[l] } })),

  filters: { minSeverity: null, source: null, country: null },
  setFilter: (k, v) => set((s) => ({ filters: { ...s.filters, [k]: v } })),
  clearFilters: () =>
    set({ filters: { minSeverity: null, source: null, country: null } }),
  filterOptions: { sources: [], countries: [] },
  setFilterOptions: (filterOptions) => set({ filterOptions }),

  decay: true,
  toggleDecay: () => set((s) => ({ decay: !s.decay })),

  counts: {},
  setCount: (l, n) => set((s) => ({ counts: { ...s.counts, [l]: n } })),

  drawer: { open: false, title: '', signals: [], findings: [] },
  openDrawer: (d) => set({ drawer: { ...d, open: true } }),
  closeDrawer: () => set((s) => ({ drawer: { ...s.drawer, open: false } })),

  readScope: null,
  setReadScope: (readScope) => set({ readScope }),
}))
