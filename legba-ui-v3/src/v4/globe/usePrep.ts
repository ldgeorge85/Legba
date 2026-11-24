/**
 * usePrep — the globe's data, prepared OFF the main thread (U-4).
 *
 * One worker per map instance (terminated with it). Each change of input
 * posts one message; an answer to an older message is dropped. Where no
 * Worker exists (a unit test under jsdom), the same pure `prepare` runs here
 * with the same result — the worker is a placement, not a second code path.
 */
import { useEffect, useRef, useState } from 'react'
import { WORLD_GEOJSON_PATH } from '@/lib/basemap'
import { prepare, slimWorld, svgCountryPaths, type PrepInput, type PrepOutput, type World } from './prep'

type Paths = Array<{ iso: string; name: string; d: string }>

let mainWorld: Promise<World> | null = null
function worldOnMain(): Promise<World> {
  mainWorld ??= fetch(WORLD_GEOJSON_PATH)
    .then((r) => (r.ok ? r.json() : null))
    .then(slimWorld)
    .catch(() => ({ type: 'FeatureCollection', features: [] }) as World)
  return mainWorld
}

function makeWorker(): Worker | null {
  if (typeof Worker === 'undefined') return null
  try {
    return new Worker(new URL('./prep.worker.ts', import.meta.url), { type: 'module' })
  } catch {
    return null
  }
}

export interface PrepState {
  out: PrepOutput | null
  /** Milliseconds the last preparation took in the worker (for the walk). */
  ms: number | null
}

export function usePrep(input: PrepInput | null, svg: { width: number; height: number } | null): PrepState & { paths: Paths | null } {
  const workerRef = useRef<Worker | null>(null)
  const seqRef = useRef(0)
  const svgSeqRef = useRef(0)
  const [state, setState] = useState<PrepState>({ out: null, ms: null })
  const [paths, setPaths] = useState<Paths | null>(null)

  useEffect(() => {
    const w = makeWorker()
    workerRef.current = w
    if (w) {
      w.onmessage = (e: MessageEvent<{ kind: 'prep' | 'svg'; seq: number; out?: PrepOutput; paths?: Paths; ms?: number; error?: string }>) => {
        const m = e.data
        if (m.error) return
        if (m.kind === 'svg') {
          if (m.seq === svgSeqRef.current && m.paths) setPaths(m.paths)
          return
        }
        if (m.seq === seqRef.current && m.out) setState({ out: m.out, ms: m.ms ?? null })
      }
    }
    return () => {
      w?.terminate()
      workerRef.current = null
    }
  }, [])

  useEffect(() => {
    if (!input) return
    const seq = ++seqRef.current
    const w = workerRef.current
    if (w) {
      w.postMessage({ kind: 'prep', seq, worldUrl: new URL(WORLD_GEOJSON_PATH, window.location.origin).href, input })
      return
    }
    let live = true
    void worldOnMain().then((world) => {
      if (live && seq === seqRef.current) setState({ out: prepare(input, world), ms: null })
    })
    return () => {
      live = false
    }
  }, [input])

  const sw = svg?.width ?? 0
  const sh = svg?.height ?? 0
  useEffect(() => {
    if (!sw || !sh) return
    const seq = ++svgSeqRef.current
    const w = workerRef.current
    if (w) {
      w.postMessage({ kind: 'svg', seq, worldUrl: new URL(WORLD_GEOJSON_PATH, window.location.origin).href, width: sw, height: sh })
      return
    }
    let live = true
    void worldOnMain().then((world) => {
      if (live && seq === svgSeqRef.current) setPaths(svgCountryPaths(world, sw, sh))
    })
    return () => {
      live = false
    }
  }, [sw, sh])

  return { ...state, paths }
}
