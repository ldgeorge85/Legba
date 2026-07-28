/**
 * deck.gl layer data shaping (P4-3, feature 2) — pure transforms.
 *
 * deck.gl renders in a camera-synced overlay ON TOP of the MapLibre basemap
 * (the `MapboxOverlay` control pattern — one shared view state, no manual
 * camera plumbing). deck.gl is heavy, so it is dynamically imported only when
 * an operator turns a deck layer on. The raw→layer-input projections live here,
 * pure, so the density (heatmap / hexagon) input and the co-mention arc graph
 * are unit-tested without WebGL; the map component only wires these arrays into
 * deck layer instances.
 */
import type { Severity } from '@/v4/world/types'

/** Severity → density weight (heavier events count for more in the aggregate). */
const SEVERITY_WEIGHT: Record<string, number> = {
  critical: 5,
  high: 4,
  medium: 3,
  low: 2,
  info: 1,
}

/** A density datum for a deck HexagonLayer / HeatmapLayer. */
export interface DensityDatum {
  /** [lon, lat] — deck's coordinate order. */
  position: [number, number]
  weight: number
  severity: Severity
}

/** The subset of a windowed signal the density projection needs. */
export interface DensityInput {
  lat: number
  lon: number
  severity: Severity
}

/** Project windowed signals to density data (weighted by severity). Rows with a
 *  non-finite coordinate are dropped. */
export function densityPoints(signals: readonly DensityInput[]): DensityDatum[] {
  const out: DensityDatum[] = []
  for (const s of signals) {
    if (!Number.isFinite(s.lat) || !Number.isFinite(s.lon)) continue
    out.push({
      position: [s.lon, s.lat],
      weight: SEVERITY_WEIGHT[s.severity] ?? 1,
      severity: s.severity,
    })
  }
  return out
}

/** One co-mention arc between two countries a signal jointly references. */
export interface CoMentionArc {
  fromIso2: string
  toIso2: string
  /** [lon, lat] deck order. */
  source: [number, number]
  target: [number, number]
  count: number
}

/** The subset of a windowed signal the arc projection needs. */
export interface ArcSignalInput {
  countries: string[]
}

const ISO2_RE = /^[A-Z]{2}$/

/**
 * Build the "narrative echo" arc graph: every signal that references >=2
 * countries contributes an arc between each unordered country pair, and arcs
 * accumulate a co-mention `count`. This is a real relationship derivable from
 * the reachable `/signals` `geo[]` column (which countries a single signal
 * co-references) — the flow layer, honest.
 *
 * Placement resolves each ISO2 to its gazetteer centroid via the injected
 * `resolve` (so this stays pure/testable — the map passes `resolveCountry`);
 * a pair with an unplaceable code is dropped (never a fabricated endpoint).
 * `minCount` filters weak links; the result is sorted strongest-first and
 * capped at `maxArcs`.
 *
 * DATA SEAM (recorded 2026-07): enrichment currently writes a SINGLE country
 * into each signal's `geo[]`, so `countries.length >= 2` never holds and the
 * arc layer is honest-empty on the live substrate. The transform is correct
 * and arcs will appear the moment multi-country geo lands upstream — do not
 * "fix" this here by synthesizing pairs.
 */
export function coMentionArcs(
  signals: readonly ArcSignalInput[],
  resolve: (iso2: string) => { lat: number; lon: number } | null,
  opts: { minCount?: number; maxArcs?: number } = {},
): CoMentionArc[] {
  const minCount = opts.minCount ?? 2
  const maxArcs = opts.maxArcs ?? 400
  const counts = new Map<string, number>()
  for (const s of signals) {
    const iso = [
      ...new Set(
        (s.countries ?? [])
          .map((c) => c.toUpperCase())
          .filter((c) => ISO2_RE.test(c)),
      ),
    ].sort()
    for (let i = 0; i < iso.length; i++) {
      for (let j = i + 1; j < iso.length; j++) {
        const key = `${iso[i]}|${iso[j]}`
        counts.set(key, (counts.get(key) ?? 0) + 1)
      }
    }
  }
  const arcs: CoMentionArc[] = []
  for (const [key, count] of counts) {
    if (count < minCount) continue
    const [a, b] = key.split('|')
    const pa = resolve(a)
    const pb = resolve(b)
    if (!pa || !pb) continue
    arcs.push({
      fromIso2: a,
      toIso2: b,
      source: [pa.lon, pa.lat],
      target: [pb.lon, pb.lat],
      count,
    })
  }
  arcs.sort((x, y) => y.count - x.count || x.fromIso2.localeCompare(y.fromIso2))
  return arcs.slice(0, maxArcs)
}
