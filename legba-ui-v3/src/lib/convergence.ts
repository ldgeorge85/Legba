/**
 * geo_convergence_scan → map markers (P4-3, feature 4).
 *
 * The A7 geographic-convergence detector (deterministic, LLM-free) fires
 * kind='alert' rows when signals from >=3 DISTINCT source families converge in
 * one geographic bin — a 1°×1° cell of point-trustworthy coordinates, or an
 * ISO2 country bin. It is the cross-stream correlator; this surfaces it on the
 * map.
 *
 * Reachability note (honest): the read surface exposes convergence alerts only
 * through `GET /since` → its `alerts` section, which carries the alert's
 * server-generated TITLE but not its `data` payload. That title is produced
 * deterministically by `geo_convergence_scan.bin_label` + the formation /
 * dissolution templates, so the bin identity — the cell's 1° extent + ISO2, or
 * the country ISO2 — is recoverable from it. A title that does NOT match the
 * templates yields NO marker (never a fabricated point). Because `/since` is a
 * since-last-visit DIFF, this is "convergence activity in the last N days",
 * reduced to the latest event per bin (a dissolution retires a formed bin) — we
 * surface it honestly as recent convergence, not a guaranteed-complete set.
 */
import { resolveCountry } from './countryGeo'

export type ConvergenceEvent = 'formed' | 'dissolved'
export type ConvergenceBinKind = 'cell' | 'country'

/** One alert row as the `/since` alerts section returns it (mirrors `SinceAlert`). */
export interface SinceAlertRow {
  id: string
  severity: string | null
  channel: string
  summary: string
  target_id: string | null
  produced_at: string
}

/** A parsed convergence alert — bin identity + counts + placement. */
export interface ConvergenceMarker {
  id: string
  event: ConvergenceEvent
  binKind: ConvergenceBinKind
  /** Stable bin key used to dedupe formation / dissolution edges of one bin. */
  binKey: string
  iso2: string | null
  /** Placement — the cell centre, or the country gazetteer centroid. */
  lat: number
  lon: number
  /** Distinct source-family count (formation titles only; null on dissolution). */
  familyCount: number | null
  /** Contributing signal count (formation titles only; null on dissolution). */
  signalCount: number | null
  /** The rolling window the scan ran over, hours (formation titles only). */
  windowHours: number | null
  producedAt: string
  targetId: string | null
  /** The human bin label from the title, e.g. 'cell(33..34°, 44..45°) IQ'. */
  label: string
}

/** The alert `channel` resolves to trigger_class 'geo_convergence' (or, as a
 *  fallback, the emitting analyst 'geo_convergence_scan'). */
export function isConvergenceChannel(channel: string | null | undefined): boolean {
  return (
    typeof channel === 'string' &&
    channel.toLowerCase().startsWith('geo_convergence')
  )
}

// `bin_label`: cell → 'cell(<lat0>..<lat0+1>°, <lon0>..<lon0+1>°) <ISO2?>'.
const CELL_LABEL_RE =
  /^cell\(\s*(-?\d+)\.\.-?\d+°\s*,\s*(-?\d+)\.\.-?\d+°\s*\)(?:\s+([A-Za-z]{2}))?/
// country → the bare ISO2 code.
const COUNTRY_LABEL_RE = /^([A-Za-z]{2})$/

/**
 * Parse a bin label into a placed marker core, or null if unrecognized. A cell
 * places at the centre of its 1° cell (lat0+0.5, lon0+0.5) — the honest centre
 * of the extent, never a fake precise point; a country places at its gazetteer
 * centroid.
 */
export function placeBinLabel(label: string): {
  binKind: ConvergenceBinKind
  binKey: string
  iso2: string | null
  lat: number
  lon: number
} | null {
  const trimmed = label.trim()
  const cell = CELL_LABEL_RE.exec(trimmed)
  if (cell) {
    const lat0 = Number(cell[1])
    const lon0 = Number(cell[2])
    if (!Number.isFinite(lat0) || !Number.isFinite(lon0)) return null
    const iso2 = cell[3] ? cell[3].toUpperCase() : null
    return {
      binKind: 'cell',
      binKey: `cell:${lat0}:${lon0}`,
      iso2,
      lat: lat0 + 0.5,
      lon: lon0 + 0.5,
    }
  }
  const country = COUNTRY_LABEL_RE.exec(trimmed)
  if (country) {
    const iso2 = country[1].toUpperCase()
    const fix = resolveCountry(iso2)
    if (!fix) return null
    return {
      binKind: 'country',
      binKey: `country:${iso2}`,
      iso2,
      lat: fix.lat,
      lon: fix.lon,
    }
  }
  return null
}

// Formation: 'Geo convergence formed: <label> — <F> source famil… , <S> signals (<H>h)'.
const FORMED_RE =
  /^Geo convergence formed:\s*(.+?)\s+—\s+(\d+)\s+source famil\w+,\s+(\d+)\s+signals\s+\((\d+)h\)/
// Dissolution: 'Geo convergence dissolved: <label> — below <N> …'.
const DISSOLVED_RE = /^Geo convergence dissolved:\s*(.+?)\s+—\s+below\s+(\d+)/

/**
 * Parse one alert row's title into a marker, or null when it isn't a
 * recognizable geo_convergence formation / dissolution.
 */
export function parseConvergenceAlert(row: SinceAlertRow): ConvergenceMarker | null {
  const s = (row.summary ?? '').trim()
  const formed = FORMED_RE.exec(s)
  if (formed) {
    const placed = placeBinLabel(formed[1])
    if (!placed) return null
    return {
      id: row.id,
      event: 'formed',
      ...placed,
      familyCount: Number(formed[2]),
      signalCount: Number(formed[3]),
      windowHours: Number(formed[4]),
      producedAt: row.produced_at,
      targetId: row.target_id,
      label: formed[1].trim(),
    }
  }
  const dissolved = DISSOLVED_RE.exec(s)
  if (dissolved) {
    const placed = placeBinLabel(dissolved[1])
    if (!placed) return null
    return {
      id: row.id,
      event: 'dissolved',
      ...placed,
      familyCount: null,
      signalCount: null,
      windowHours: null,
      producedAt: row.produced_at,
      targetId: row.target_id,
      label: dissolved[1].trim(),
    }
  }
  return null
}

/**
 * Reduce a raw `/since` alerts list to the currently-active convergence
 * markers: geo_convergence rows only, parsed, then the latest event per bin
 * wins — a bin whose most-recent event is a dissolution is dropped.
 */
export function activeConvergenceMarkers(
  rows: readonly SinceAlertRow[],
): ConvergenceMarker[] {
  const latest = new Map<string, ConvergenceMarker>()
  for (const row of rows) {
    if (!isConvergenceChannel(row.channel)) continue
    const m = parseConvergenceAlert(row)
    if (!m) continue
    const prev = latest.get(m.binKey)
    if (!prev || Date.parse(m.producedAt) >= Date.parse(prev.producedAt)) {
      latest.set(m.binKey, m)
    }
  }
  return [...latest.values()].filter((m) => m.event === 'formed')
}
