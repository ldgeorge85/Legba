/**
 * useGlobeModel — what both globe renderers (WebGL and drawn) share: the
 * rows that are on, the reads, the prepared layers, the figures, the
 * selection resolved to a place, and the click → shared-selection mapping.
 */
import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { useSelection, type Selection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { useSubjectCamera } from '@/lib/subjectGeometry'
import { layerFigures, type HoverInfo, type LayerFigure } from './chrome'
import { useGlobeReads, type GlobeReads } from './data'
import { alignWorldOnce, useGlobeLayers } from './layerState'
import { usePrep } from './usePrep'
import type { GlobeLayerId } from './catalog'
import type { CountryFacts, PrepInput, PrepOutput } from './prep'
import { placeId } from './chokepoints'

export type GlobePreset = 'board' | 'inset'

/** The rows an inset lists: its subject's own layers (no global events feed). */
export const INSET_LAYERS: GlobeLayerId[] = ['change', 'blanks', 'series', 'forecasts', 'at_sea']

export interface GlobeModel {
  on: Record<GlobeLayerId, boolean>
  setOn: (id: GlobeLayerId, v: boolean) => void
  reads: GlobeReads
  out: PrepOutput | null
  prepMs: number | null
  paths: Array<{ iso: string; name: string; d: string }> | null
  figures: Record<GlobeLayerId, LayerFigure>
  selection: Selection | null
  /** The selection as a place on this map, or null when it has none here. */
  selectedAt: { lon: number; lat: number; isos: string[]; bounds: [number, number, number, number] | null } | null
  scopeSubject: string | null
}

export function useGlobeModel(preset: GlobePreset, active: boolean, svgSize: { width: number; height: number } | null): GlobeModel {
  useEffect(() => alignWorldOnce(), [])
  const on = useGlobeLayers((s) => s.on)
  const setOn = useGlobeLayers((s) => s.set)
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const selection = useSelection((s) => s.selection)
  const { subject } = useSubjectCamera()
  const scopeSubject = preset === 'inset' ? subject?.id ?? null : null

  const effectiveOn = useMemo(() => {
    if (preset !== 'inset') return on
    const o = { ...on }
    o.events = false
    o.drift = false
    return o
  }, [on, preset])

  const reads = useGlobeReads(active, effectiveOn)

  // Re-prepare on a data change or a window change, never per frame. The
  // window is rounded to the minute so the live band's tick does not re-post.
  const startMin = Math.floor(windowStartMs / 60_000)
  const endMin = Math.floor(windowEndMs / 60_000)
  const input = useMemo<PrepInput | null>(() => {
    if (!active) return null
    return {
      window: { startMs: startMin * 60_000, endMs: endMin * 60_000 + 59_999 },
      nowMs: Date.now(),
      subjects: reads.subjects ?? [],
      changes: effectiveOn.change ? reads.changes : null,
      reads: effectiveOn.blanks ? reads.reads : null,
      points: effectiveOn.events ? reads.points : null,
      incidents: effectiveOn.at_sea ? reads.incidents : null,
      forecasts: effectiveOn.forecasts ? reads.forecasts : null,
      series: effectiveOn.series ? reads.series : null,
      scopeSubject,
    }
  }, [active, startMin, endMin, reads.subjects, reads.changes, reads.reads, reads.points, reads.incidents, reads.forecasts, reads.series, effectiveOn, scopeSubject])

  const { out, ms: prepMs, paths } = usePrep(input, svgSize)

  const figures = useMemo(
    () => layerFigures(out, reads, effectiveOn, reads.unreadable, reads.seriesAsOf),
    [out, reads, effectiveOn],
  )

  const selectedAt = useMemo(() => locate(selection, out), [selection, out])

  return { on: effectiveOn, setOn, reads, out, prepMs, paths, figures, selection, selectedAt, scopeSubject }
}

export function locate(selection: Selection | null, out: PrepOutput | null): GlobeModel['selectedAt'] {
  if (!selection || !out || selection.kind === 'place') return null
  if (selection.kind === 'target') {
    const c = out.subjectCenters[selection.id]
    return c ? { lon: c.center[0], lat: c.center[1], isos: c.isos, bounds: c.bounds } : null
  }
  for (const fc of [out.points, out.incidents, out.forecasts]) {
    const f = fc.features.find((x) => x.properties.id === selection.id)
    if (f) {
      const [lon, lat] = (f.geometry as { coordinates: [number, number] }).coordinates
      return { lon, lat, isos: [], bounds: null }
    }
  }
  return null
}

/** The subject a country click selects: the one with the most changes, then the first. */
export function subjectForCountry(facts: CountryFacts | undefined): { id: string; name: string } | null {
  if (!facts || facts.subjects.length === 0) return null
  const best = [...facts.subjects].sort((a, b) => b.changes - a.changes)[0]
  return { id: best.id, name: best.name }
}

/** A series place as the shared point (U-7): `place:portwatch:<key>`. */
export function selectPlace(key: string, name: string) {
  useSelection.getState().select({ kind: 'place', id: placeId(key), label: name, origin: 'map' })
}

export function selectSubject(id: string, name: string) {
  useSelection.getState().select({ kind: 'target', id, label: name, origin: 'map' })
}

export function selectRecord(kind: 'event' | 'situation' | 'signal', id: string, label: string) {
  useSelection.getState().select({ kind, id, label, origin: 'map' })
}

const fmtDay = (ms: number) => (Number.isFinite(ms) ? new Date(ms).toISOString().slice(0, 16).replace('T', ' ') + 'Z' : 'no time')

export function countryHover(facts: CountryFacts, x: number, y: number, gapReason: string | null): HoverInfo {
  const lines: string[] = []
  for (const s of facts.subjects.slice(0, 3)) lines.push(`${s.name} · ${s.changes} change${s.changes === 1 ? '' : 's'} since your last visit`)
  if (facts.subjects.length > 3) lines.push(`and ${facts.subjects.length - 3} more subjects`)
  if (facts.blank === 'dense') lines.push('Blank: looked, could not see — no read in the window')
  if (facts.blank === 'sparse') lines.push('Blank: not covered — no source declared')
  if (gapReason) lines.push(`Declared gap: ${gapReason}`)
  return { x, y, title: facts.name, lines }
}

export function pointHover(p: Record<string, unknown>, x: number, y: number): HoverInfo {
  return {
    x,
    y,
    title: String(p.title || '(untitled)').slice(0, 140),
    lines: [`${p.kind === 'situation' ? 'Situation' : 'Event'} · ${String(p.life ?? '')}${p.sev ? ` · ${String(p.sev)}` : ''}`, fmtDay(Number(p.ts))],
  }
}

export function incidentHover(p: Record<string, unknown>, x: number, y: number): HoverInfo {
  const coasts = String(p.coasts || '')
  return {
    x,
    y,
    title: String(p.title || 'Incident at sea').slice(0, 140),
    lines: [
      p.place ? `At sea · ${String(p.place)}` : 'At sea',
      coasts ? `Off the coasts of ${coasts} (nearest first; inferred, not filed)` : 'No coast within 200 km',
      fmtDay(Number(p.ts)),
    ],
  }
}

export function forecastHover(p: Record<string, unknown>, x: number, y: number, subjectName: string | null): HoverInfo {
  const due = Number(p.due)
  const days = Math.round((due - Date.now()) / 86_400_000)
  return {
    x,
    y,
    title: String(p.question || 'Open forecast').slice(0, 160),
    lines: [
      `Due ${new Date(due).toISOString().slice(0, 10)} · ${days >= 0 ? `in ${days} days` : `${-days} days overdue`}`,
      `${Math.round((Number(p.step) / 12) * 100)} % of its window elapsed`,
      ...(subjectName ? [`Placed at ${subjectName}`] : []),
    ],
  }
}

/** A declared gap's reason for one subject (read on hover, cached). */
export function useGapReason(subjectId: string | null): string | null {
  const q = useQuery<{ absences?: Array<{ reason?: string | null; kind?: string }> }>({
    queryKey: ['globe-gap', subjectId],
    queryFn: () => apiGet(`/v3/absence?scope=${encodeURIComponent(subjectId ?? '')}`),
    enabled: !!subjectId,
    staleTime: 300_000,
    retry: false,
  })
  const first = q.data?.absences?.find((a) => typeof a.reason === 'string' && a.reason)
  return first?.reason ? first.reason.slice(0, 140) : null
}
