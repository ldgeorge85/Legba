/**
 * Which globe rows are on (U-4) — one store for the Board globe and the
 * Subject inset, kept in step with the world layers Consult drives.
 *
 * The world store (`v4/world/worldState`) holds the five layers of the closed
 * drive vocabulary; a Consult `map` step sets them wholesale and a case's Undo
 * restores them. A globe row bound to one of them follows every change to it
 * ({@link globeLayersFromWorld}); a reader's toggle of a bound row writes the
 * world layer too, so the snapshot a case takes before its first step is the
 * globe the reader was looking at, and Undo returns to exactly that.
 *
 * On first use the world layers are aligned to the globe's rows at rest
 * (`alignWorldOnce`): a world default of "signals, situations, events on" would
 * otherwise light the incidents and events rows the first time anything
 * touched the world layers.
 */
import { create } from 'zustand'
import { useWorldState, type WorldLayer } from '@/v4/world/worldState'
import { GLOBE_LAYERS, defaultGlobeLayers, globeLayer, globeLayersFromWorld, type GlobeLayerId } from './catalog'

interface GlobeLayerState {
  on: Record<GlobeLayerId, boolean>
  set: (id: GlobeLayerId, value: boolean) => void
}

export const useGlobeLayers = create<GlobeLayerState>((set, get) => ({
  on: defaultGlobeLayers(),
  set: (id, value) => {
    const def = globeLayer(id)
    if (def.unavailable) return
    set({ on: { ...get().on, [id]: value } })
    const ws = useWorldState.getState()
    for (const w of def.world) if (ws.layers[w] !== value) useWorldState.getState().toggleLayer(w)
  },
}))

let aligned = false
let unsubscribe: (() => void) | null = null

/** Align the world layers to the rows at rest, once, and follow them after. */
export function alignWorldOnce(): void {
  if (!unsubscribe) {
    unsubscribe = useWorldState.subscribe((s, prev) => {
      if (s.layers === prev.layers) return
      const cur = useGlobeLayers.getState().on
      const next = globeLayersFromWorld(s.layers, cur)
      if (GLOBE_LAYERS.some((l) => next[l.id] !== cur[l.id])) useGlobeLayers.setState({ on: next })
    })
  }
  if (aligned) return
  aligned = true
  const on = useGlobeLayers.getState().on
  const want = new Set<WorldLayer>()
  for (const l of GLOBE_LAYERS) if (on[l.id]) for (const w of l.world) want.add(w)
  const ws = useWorldState.getState()
  for (const w of Object.keys(ws.layers) as WorldLayer[]) {
    if (w === 'entities') continue
    if (ws.layers[w] !== want.has(w)) useWorldState.getState().toggleLayer(w)
  }
}

/** Test helper. */
export function resetGlobeLayers(): void {
  aligned = false
  unsubscribe?.()
  unsubscribe = null
  useGlobeLayers.setState({ on: defaultGlobeLayers() })
}
