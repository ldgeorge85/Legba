/**
 * The globe's actions (U-4) — what a frame, a panel contract or a test can ask
 * of the map without holding the map. Every one goes through a store the map
 * already follows, so a request made while the map is hidden or not yet
 * mounted is applied when it is shown:
 *
 *   setLayer(id, on)        the globe's row store (`layerState.ts`), which
 *                           also writes the world layer a Consult step drives
 *   flyTo(target)           a camera request the mounted map executes once
 *                           (a place, a bounding box, or a subject's declared area)
 *   setWindow(start, end)   THE ONE CLOCK (`useWorldState.setWindow`)
 *   select(ref)             THE ONE POINT (`useSelection.select`)
 *   home()                  back to the home view
 */
import { create } from 'zustand'
import { useSelection, type SelectionKind } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { useGlobeLayers } from './layerState'
import type { GlobeLayerId } from './catalog'

export type CameraRequest =
  | { kind: 'place'; lon: number; lat: number; zoom?: number }
  | { kind: 'bounds'; west: number; south: number; east: number; north: number }
  | { kind: 'subject'; id: string }
  | { kind: 'home' }

/** Where the camera rests (written by the mounted map when a move ends). */
export interface CameraView {
  lon: number
  lat: number
  zoom: number
  projection: 'globe' | 'flat'
}

interface CameraState {
  request: CameraRequest | null
  nonce: number
  /** The last settled view of the Board map (null until it has drawn once). */
  view: CameraView | null
  ask: (r: CameraRequest) => void
  settle: (v: CameraView) => void
}

export const useGlobeCamera = create<CameraState>((set) => ({
  request: null,
  nonce: 0,
  view: null,
  ask: (request) => set((s) => ({ request, nonce: s.nonce + 1 })),
  settle: (view) => set({ view }),
}))

export const globeActions = {
  setLayer(id: GlobeLayerId, on: boolean): void {
    useGlobeLayers.getState().set(id, on)
  },
  flyTo(target: CameraRequest): void {
    useGlobeCamera.getState().ask(target)
  },
  home(): void {
    useGlobeCamera.getState().ask({ kind: 'home' })
  },
  setWindow(startMs: number, endMs: number): void {
    if (Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs) useWorldState.getState().setWindow(startMs, endMs)
  },
  select(ref: { kind: SelectionKind; id: string; label?: string }): void {
    useSelection.getState().select({ ...ref, origin: 'map-action' })
  },
}
