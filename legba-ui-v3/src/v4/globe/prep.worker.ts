/// <reference lib="webworker" />
/**
 * The globe's prep worker (U-4 performance budget: "nothing is laid out on
 * the main thread"). It fetches and slims the world outline ONCE, then answers
 * each `prep` message with `prepare(input, world)` — the per-layer GeoJSON,
 * capped and placed — and each `svg` message with the drawn fallback's
 * country paths, off the main thread.
 */
import { prepare, slimWorld, svgCountryPaths, type PrepInput, type World } from './prep'

let world: World | null = null
let loading: Promise<World> | null = null

function loadWorld(url: string): Promise<World> {
  if (world) return Promise.resolve(world)
  loading ??= fetch(url)
    .then((r) => (r.ok ? r.json() : null))
    .then((raw) => (world = slimWorld(raw)))
    .catch(() => (world = { type: 'FeatureCollection', features: [] }))
  return loading
}

type Msg =
  | { kind: 'prep'; seq: number; worldUrl: string; input: PrepInput }
  | { kind: 'svg'; seq: number; worldUrl: string; width: number; height: number }

const post = (m: unknown) => (self as unknown as Worker).postMessage(m)

self.onmessage = async (e: MessageEvent<Msg>) => {
  const m = e.data
  try {
    const w = await loadWorld(m.worldUrl)
    const t0 = performance.now()
    if (m.kind === 'svg') {
      post({ kind: 'svg', seq: m.seq, paths: svgCountryPaths(w, m.width, m.height), ms: Math.round(performance.now() - t0) })
      return
    }
    post({ kind: 'prep', seq: m.seq, out: prepare(m.input, w), ms: Math.round(performance.now() - t0) })
  } catch (err) {
    post({ kind: m.kind, seq: m.seq, error: String(err) })
  }
}
