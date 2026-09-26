/**
 * GapStrip — the per-desk gap strip (7b-ii), above the desk's reads.
 *
 * A desk is nine bounded units (docs/ANALYSIS.md §4.1); nothing on the target
 * surface previously showed, at a glance, which of the nine have a current
 * read, which banded insufficient evidence, which are stale against their own
 * cadence, and which never ran at all. This renders one cell per unit,
 * coloured by state, with the latest read's time and judge score on hover, and
 * a click that selects that unit's latest read into the Inspector — a missing
 * or excluded read is a DECLARED gap (its own honest colour), never a blank.
 *
 * Two routes the UI already calls elsewhere carry the cell states, no changed
 * route (see `lib/gapStripModel.ts` for the full sourcing note):
 *   - `GET /findings?analyst_id_in=<unit ids>&target_id=` — the actual latest
 *     finding per unit (the `CountryUnitsAssessment` precedent).
 *   - `GET /v3/eval/country_scorecard?target_id=` — the banded scorecard's
 *     per-dimension verdict (band / reason / critic_score).
 * All state derivation lives in `gapStripModel.ts`, DOM-free and unit-tested.
 *
 * WHY ABSENT (7b/k5). A declared gap the reader cannot interrogate is only a
 * prettier blank, so every non-current cell now drills: the click selects a
 * TYPED ABSENCE (`lib/absenceModel.ts`) and the Inspector resolves it against
 * `GET /v3/absence?scope=<desk>`, rendering that absence's kind, its proof —
 * what was checked, when, and the record of that look — and its clock, so an
 * absence measured days ago and never re-checked says so instead of passing
 * for a current one. A `current` cell keeps opening its read, unchanged.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet, ApiError } from '@/lib/api'
import { selectRow } from '@/state/selection'
import { InfoTip } from '@/components/InfoTip'
import { cn } from '@/lib/cn'
import { insufficientLabel, type CountryScorecard } from '@/lib/evalOps'
import {
  GAP_STRIP_UNITS,
  deriveGapStrip,
  type GapCellResult,
  type GapCellState,
  type GapLatestFinding,
  type GapUnit,
} from '@/lib/gapStripModel'
import { absenceId, type AbsenceKind } from '@/lib/absenceModel'

interface FindingRow {
  id: string
  analyst_id: string | null
  produced_at: string
  severity?: string | null
}
interface FindingsResponse {
  data: FindingRow[]
}

const UNIT_IDS = GAP_STRIP_UNITS.map((u) => u.id).join(',')

const STATE_STYLE: Record<GapCellState, string> = {
  current: 'border-emerald-700 bg-emerald-900/50 text-emerald-200',
  stale: 'border-amber-700 bg-amber-900/50 text-amber-200',
  // Insufficient-evidence is its own honest tone — never a severity colour
  // (mirrors `EvalScorecard`'s `TONE_PILL.insufficient`).
  insufficient: 'border-slate-700 bg-slate-800/70 text-slate-400',
  none: 'border-dashed border-slate-800 bg-transparent text-slate-600',
}

const STATE_LABEL: Record<GapCellState, string> = {
  current: 'current',
  stale: 'stale',
  insufficient: 'insufficient evidence',
  none: 'no read yet',
}

function judgeScoreText(score: number | null): string {
  return score === null ? 'judge score unmeasured' : `judge score ${(score * 100).toFixed(0)}%`
}

/**
 * The absence kind a non-current cell drills into.
 *
 * The cell knows its own state; the ROUTE owns the classification (a silent
 * unit is `not_collected` or `source_stale` depending on whether it ever ran,
 * which the strip cannot see). So this names the kind the cell has EVIDENCE
 * for and the resolver matches on the subject across the unit kinds — neither
 * side asserts a classification it does not own.
 */
function drillKind(state: GapCellState): AbsenceKind | null {
  switch (state) {
    case 'none':
      return 'not_collected'
    case 'stale':
      return 'source_stale'
    case 'insufficient':
      return 'below_floor'
    default:
      return null
  }
}

/** The hover explainer — exact time, judge score, and why the colour. */
function cellTooltip(unit: GapUnit, cell: GapCellResult): string {
  const drill = ' Click for why it is absent, with the proof.'
  if (cell.state === 'none') {
    return `${unit.label} — no read yet (runs every ${unit.cadenceHours}h).${drill}`
  }
  const when = cell.producedAt ? new Date(cell.producedAt).toLocaleString() : 'unknown time'
  const base = `${unit.label} — ${when} · ${judgeScoreText(cell.judgeScore)}`
  if (cell.state === 'insufficient') {
    return `${base} · insufficient evidence: ${insufficientLabel(cell.reason)}.${drill}`
  }
  if (cell.state === 'stale') {
    return `${base} · stale — older than its ${unit.cadenceHours}h cadence.${drill}`
  }
  return `${base} · current — inside its ${unit.cadenceHours}h cadence`
}

export function GapStrip({ targetId }: { targetId: string | undefined }) {
  const findingsQ = useQuery<FindingsResponse>({
    enabled: !!targetId,
    queryKey: ['desk-gap-strip-findings', targetId],
    queryFn: async () => {
      try {
        return await apiGet<FindingsResponse>(
          `/findings?analyst_id_in=${UNIT_IDS}&target_id=${encodeURIComponent(targetId!)}&limit=100`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [] }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const scorecardQ = useQuery<CountryScorecard[]>({
    enabled: !!targetId,
    queryKey: ['desk-gap-strip-scorecard', targetId],
    queryFn: async () => {
      try {
        return await apiGet<CountryScorecard[]>(
          `/v3/eval/country_scorecard?target_id=${encodeURIComponent(targetId!)}`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const latestByUnit = useMemo(() => {
    const m = new Map<string, GapLatestFinding>()
    for (const row of findingsQ.data?.data ?? []) {
      const a = row.analyst_id
      if (!a) continue
      const prev = m.get(a)
      if (!prev || Date.parse(row.produced_at) > Date.parse(prev.producedAt)) {
        m.set(a, { id: row.id, producedAt: row.produced_at, severity: row.severity })
      }
    }
    return m
  }, [findingsQ.data])

  const dimensions = scorecardQ.data?.[0]?.dimensions

  const cells = useMemo(
    () => deriveGapStrip({ latestByUnit, dimensions }),
    [latestByUnit, dimensions],
  )

  if (!targetId) return null

  return (
    <section className="mb-2" data-testid="desk-gap-strip">
      <div className="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wide text-slate-500">
        unit reads
        <InfoTip
          testId="desk-gap-strip-explain"
          text={
            'One cell per bounded unit. Green = a read inside the unit\'s own ' +
            'cadence. Amber = the latest read is older than that cadence. ' +
            'Grey = the latest read banded insufficient evidence (excluded by ' +
            'the scorecard). Dashed = no read yet — every state is a declared ' +
            'gap, never a blank. Click a green cell to open its read; click ' +
            'any other to see WHY it is absent — the kind of absence, what ' +
            'was checked and when, and whether anyone has looked since.'
          }
        >
          ?
        </InfoTip>
      </div>
      <div className="flex flex-wrap gap-1">
        {GAP_STRIP_UNITS.map((unit, i) => {
          const cell = cells[i]
          // A gap cell drills into WHY it is absent; a current one opens its
          // read. Every cell is therefore clickable — the `none` cell most of
          // all, since it is the one with no record to open and was, before
          // this, the only state a reader could not interrogate at all.
          const absenceKind = drillKind(cell.state)
          const clickable = absenceKind !== null || cell.findingId !== null
          const onSelect = absenceKind
            ? () =>
                selectRow(
                  'absence',
                  absenceId(targetId, absenceKind, unit.id),
                  `${unit.label} — why absent`,
                  { origin: 'desk-gap-strip' },
                )
            : () =>
                selectRow('finding', cell.findingId as string, unit.label, {
                  origin: 'desk-gap-strip',
                })
          return (
            <InfoTip
              key={unit.id}
              text={cellTooltip(unit, cell)}
              testId={`desk-gap-cell-tip-${unit.id}`}
              interactiveParent
            >
              <button
                type="button"
                disabled={!clickable}
                onClick={clickable ? onSelect : undefined}
                data-testid={`desk-gap-cell-${unit.id}`}
                data-state={cell.state}
                aria-label={`${unit.label}: ${STATE_LABEL[cell.state]}`}
                className={cn(
                  'inline-flex h-6 min-w-[2.25rem] items-center justify-center rounded border px-1.5',
                  'text-[10px] font-medium tracking-wide',
                  clickable ? 'cursor-pointer hover:brightness-125' : 'cursor-default',
                  STATE_STYLE[cell.state],
                )}
              >
                {unit.short}
              </button>
            </InfoTip>
          )
        })}
      </div>
    </section>
  )
}
