/**
 * The World — shared state store (orchestrator-owned contract). The map
 * (Track A · agent A), the scrubber/layers (agent B), and the rails (agent C)
 * all read/write this, so they compose without importing each other.
 */
import { create } from 'zustand'
import type { Severity, WorldSignal, WorldFinding } from './types'

export type WorldLayer = 'signals' | 'findings' | 'situations' | 'entities' | 'events'

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
 * The read currently being LENSED. The temporal / geo / node-graph
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
  /**
   * THE ONE CLOCK (UI-D1 D6) — the shared time window (epoch ms). Default =
   * the last 24h. `setWindow` is the ONLY writer of the two instants: the
   * scrubber's thumbs and its playback, a span preset (through `setSpan`
   * below), a mission, a stance seed's `timeWindow` and a case's `timeline`
   * step all land here. The Timeline's zoom and pan are a view lens and never
   * call it.
   */
  windowStartMs: number
  windowEndMs: number
  setWindow: (startMs: number, endMs: number) => void

  /**
   * The scrubber's OUTER span (ms) — the total range its two thumbs sweep.
   * `setSpan` re-anchors the window to the last `spanMs` ending at `now`
   * (a span preset click), so widening to 7d/30d actually reaches back in the
   * data rather than staying pinned to a 24h slice. It records the span and
   * then writes the window THROUGH `setWindow`, so the one writer stays one.
   */
  spanMs: number
  setSpan: (spanMs: number, now?: number) => void

  /**
   * AS RECORDED — the instant whose record the console shows (epoch ms), or
   * null for now. Set by the time band's marker and by `?at=` in the address
   * (`lib/asOf.ts`); a region honours it only when its reads can.
   */
  asOfMs: number | null
  setAsOf: (ms: number | null) => void

  /** Playback: the time band steps the window a day at a time, to now. */
  playing: boolean
  setPlaying: (p: boolean) => void

  /** Layer visibility (the Windy-style switcher). */
  layers: Record<WorldLayer, boolean>
  toggleLayer: (l: WorldLayer) => void
  /**
   * Set the visible layers WHOLESALE — every layer named is on, every other is
   * off. The mission switcher's half of the layer contract
   * (`lib/applyMission.ts`): a mission declares the layers its job needs, and
   * toggling them one at a time would leave the previous mission's extras lit.
   *
   * An EMPTY list is refused rather than honoured: `layers: []` on a mission
   * means "this mission makes no layer claim", and blanking the map is not the
   * same statement. The caller checks too; this is the second guard.
   */
  setLayers: (on: readonly WorldLayer[]) => void

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

  /**
   * Live counts per layer (badges); set by the map/rails. `null` (or never
   * set) = the layer's read has not answered — *no read yet*, never 0; a
   * number is a measured count in the window (UI-B4/map).
   */
  counts: Partial<Record<WorldLayer, number | null>>
  setCount: (l: WorldLayer, n: number | null) => void

  /** Drill-down drawer — map cluster/hex click → event list. */
  drawer: DrawerState
  openDrawer: (d: Omit<DrawerState, 'open'>) => void
  closeDrawer: () => void

  /** The read currently being lensed (P1-T7) — set by the lenses, read by the
   *  World map so it brushes the same read's evidence. */
  readScope: ReadScope | null
  setReadScope: (s: ReadScope | null) => void
}

export const useWorldState = create<WorldState>((set, get) => ({
  windowStartMs: Date.now() - DAY_MS,
  windowEndMs: Date.now(),
  setWindow: (windowStartMs, windowEndMs) => set({ windowStartMs, windowEndMs }),

  spanMs: DAY_MS,
  setSpan: (spanMs, now = Date.now()) => {
    set({ spanMs })
    get().setWindow(now - spanMs, now)
  },

  asOfMs: null,
  setAsOf: (asOfMs) => set({ asOfMs }),

  playing: false,
  setPlaying: (playing) => set({ playing }),

  layers: { signals: true, findings: true, situations: true, entities: false, events: true },
  toggleLayer: (l) =>
    set((s) => ({ layers: { ...s.layers, [l]: !s.layers[l] } })),
  setLayers: (on) =>
    set((s) => {
      if (on.length === 0) return s
      const next = {} as Record<WorldLayer, boolean>
      for (const key of Object.keys(s.layers) as WorldLayer[]) {
        next[key] = on.includes(key)
      }
      return { layers: next }
    }),

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
