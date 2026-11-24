/**
 * The subject field (U-4; layout design §3 "For a profile with no
 * geography") — what takes the map's place when the profile declares
 * `map.geometry: none`: one tile per subject, grouped by the roster tag the
 * profile gives it, with the map's own encodings — the ink wash for change
 * since the last visit, the hatch for blanks, a ring for open forecasts.
 * Clicking a tile sets the shared point (the subject). The time band and
 * Consult work unchanged. The profile supplies the noun.
 *
 * Minimal by design (the lane's honest scope): no series sparkline in a tile
 * yet, and the grouping is the roster tag rather than the projection's
 * subject groups.
 */
import { useMemo } from 'react'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { PROFILE_UI, rosterLabel } from '@/lib/profileUi'
import { useGlobeReads } from './data'
import { washStep, type PrepChange, type PrepForecast, type PrepRead, type PrepSubject } from './prep'
import { LegendMark } from './chrome'
import { T } from './style'
import type { GlobeLayerId } from './catalog'

const ON: Record<GlobeLayerId, boolean> = { change: true, blanks: true, series: false, forecasts: true, events: false, at_sea: false, drift: false }

export interface FieldTile {
  id: string
  name: string
  group: string
  changes: number
  blank: 'dense' | 'sparse' | null
  forecasts: number
}

export function fieldTiles(
  subjects: PrepSubject[],
  tags: Map<string, string>,
  changes: PrepChange[] | null,
  reads: PrepRead[] | null,
  forecasts: PrepForecast[] | null,
  window: { startMs: number; endMs: number },
): FieldTile[] {
  const inWin = (t: number) => Number.isFinite(t) && t >= window.startMs && t <= window.endMs
  const ch = new Map<string, number>()
  for (const c of changes ?? []) if (c.targetId && inWin(c.atMs)) ch.set(c.targetId, (ch.get(c.targetId) ?? 0) + 1)
  const read = new Set((reads ?? []).filter((r) => inWin(r.atMs)).map((r) => r.targetId))
  const fc = new Map<string, number>()
  for (const f of forecasts ?? []) for (const k of f.subjectKeys) fc.set(k, (fc.get(k) ?? 0) + 1)
  return subjects
    .map((s) => ({
      id: s.id,
      name: s.name,
      group: tags.get(s.id) ?? '',
      changes: ch.get(s.id) ?? 0,
      blank: (s.hasSources === false ? 'sparse' : s.hasSources && reads && !read.has(s.id) ? 'dense' : null) as FieldTile['blank'],
      forecasts: fc.get(s.id) ?? 0,
    }))
    .sort((a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name))
}

export default function SubjectField() {
  const reads = useGlobeReads(true, ON)
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const selection = useSelection((s) => s.selection)
  const tiles = useMemo(
    () => fieldTiles(reads.subjects ?? [], new Map(), reads.changes, reads.reads, reads.forecasts, { startMs: windowStartMs, endMs: windowEndMs }),
    [reads.subjects, reads.changes, reads.reads, reads.forecasts, windowStartMs, windowEndMs],
  )
  const groups = useMemo(() => {
    const m = new Map<string, FieldTile[]>()
    for (const t of tiles) m.set(t.group, [...(m.get(t.group) ?? []), t])
    return [...m.entries()]
  }, [tiles])
  const noun = PROFILE_UI.subject.plural ?? 'subjects'

  return (
    <div className="flex h-full w-full flex-col overflow-auto bg-surf-1 px-4 py-3" data-testid="subject-field">
      <div className="flex items-baseline justify-between border-b border-line pb-1">
        <span className="text-label uppercase tracking-[0.06em] text-ink-3">{noun}</span>
        <span className="font-mono text-label tabular-nums text-ink-3">{reads.subjects === null ? 'no read yet' : `${tiles.length} ${noun.toLowerCase()}`}</span>
      </div>
      {groups.map(([group, list]) => (
        <section key={group || '-'} className="mt-2">
          {group && <div className="mb-1 text-label uppercase tracking-[0.06em] text-ink-3">{rosterLabel(group)}</div>}
          <div className="grid gap-1" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))' }}>
            {list.map((t) => {
              const step = washStep(t.changes)
              const sel = selection?.kind === 'target' && selection.id === t.id
              return (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => useSelection.getState().select({ kind: 'target', id: t.id, label: t.name, origin: 'subject-field' })}
                  className="relative h-12 overflow-hidden rounded-control border px-2 py-1 text-left"
                  style={{
                    background: step ? T.wash[step - 1] : T.land,
                    borderColor: sel ? T.accent : step === 3 ? T.ink : T.rule,
                    borderWidth: sel ? 2 : 1,
                    backgroundImage: t.blank
                      ? `repeating-linear-gradient(45deg, rgba(143,154,162,${t.blank === 'dense' ? 0.5 : 0.3}) 0 1px, transparent 1px ${t.blank === 'dense' ? 6 : 12}px)`
                      : undefined,
                  }}
                  title={`${t.name} · ${t.changes} changes since your last visit${t.blank === 'dense' ? ' · looked, could not see' : t.blank === 'sparse' ? ' · not covered' : ''}${t.forecasts ? ` · ${t.forecasts} open forecasts` : ''}`}
                  data-testid={`subject-field-tile-${t.id}`}
                >
                  <span className="line-clamp-2 text-label text-ink-1">{t.name}</span>
                  {t.forecasts > 0 && (
                    <span className="absolute bottom-1 right-1.5 flex items-center gap-1 font-mono text-label text-ink-2">
                      <svg width="12" height="12" aria-hidden>
                        <circle cx="6" cy="6" r="4.5" fill="none" stroke={T.ink} strokeWidth="1.25" />
                      </svg>
                      {t.forecasts}
                    </span>
                  )}
                </button>
              )
            })}
          </div>
        </section>
      ))}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-line-subtle pt-2 text-label text-ink-3">
        <span className="flex items-center gap-1.5"><LegendMark mark="wash2" /> changes since your last visit</span>
        <span className="flex items-center gap-1.5"><LegendMark mark="hatch-dense" /> looked, could not see</span>
        <span className="flex items-center gap-1.5"><LegendMark mark="hatch-sparse" /> not covered</span>
        <span className="flex items-center gap-1.5"><LegendMark mark="ring" /> open forecasts</span>
      </div>
    </div>
  )
}
