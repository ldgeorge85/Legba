/**
 * activeSurface — which surface the frame is on, for the few views that
 * dress differently by surface (U-4: the one map component is a globe on the
 * Board and a flat inset on Subject). `App.tsx` is the only writer.
 */
import { create } from 'zustand'
import type { SurfaceId } from '@/lib/surfaces'

interface ActiveSurfaceState {
  surface: SurfaceId
  setSurface: (s: SurfaceId) => void
}

export const useActiveSurface = create<ActiveSurfaceState>((set) => ({
  surface: 'board',
  setSurface: (surface) => set({ surface }),
}))
