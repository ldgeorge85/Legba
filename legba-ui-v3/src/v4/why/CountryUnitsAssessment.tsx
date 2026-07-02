/**
 * CountryUnitsAssessment — the bounded-unit reads for a selected country (P2-T8).
 *
 * The glass tower's PRODUCT surface for a country: the four bounded reasoning
 * units (leadership-transition, energy-security, escalation, narrative /
 * coordination), each a single cited + faithfulness-verified + measured read.
 * This DEMOTES the monolithic `country_assessor` one-pager — which WorldAssessment
 * now renders below as a collapsible "feeder" — to make the small, individually
 * trustworthy units the headline.
 *
 * Each unit card carries its honest eval badge (P2-T6) and links its latest
 * finding into the Inspector (the full cited card + evidence). A unit with no
 * finding yet is shown HONESTLY ("no read yet") rather than hidden.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import { apiGet } from '@/lib/api'
import { selectRow } from '@/state/selection'
import { UnitEvalBadge } from '@/components/inspector/UnitEvalBadge'

/** The bounded units + their display labels, in headline order. */
const UNITS: { id: string; label: string }[] = [
  { id: 'leadership_transition', label: 'Leadership transition' },
  { id: 'energy_security', label: 'Energy security' },
  { id: 'escalation', label: 'Escalation' },
  { id: 'narrative_coordination', label: 'Narrative / coordination' },
]
const UNIT_IDS = UNITS.map((u) => u.id).join(',')

interface UnitFindingRow {
  id: string
  title?: string | null
  severity?: string | null
  analyst_id?: string | null
  produced_at: string
}
interface FindingsResponse {
  data: UnitFindingRow[]
}

/** Severity → hex (v4 ramp; unit severities include elevated/moderate). */
const SEVERITY_HEX: Record<string, string> = {
  critical: '#ff5555',
  high: '#ff9955',
  elevated: '#ffbb55',
  moderate: '#ffdd55',
  medium: '#ffdd55',
  low: '#55ff55',
}

export function CountryUnitsAssessment({ targetId }: { targetId: string }) {
  const { data, isLoading, error } = useQuery<FindingsResponse>({
    queryKey: ['country-units', targetId],
    refetchInterval: 5 * 60_000,
    // P1-T1 facet: analyst_id_in filters findings to the four units, target-scoped.
    queryFn: () =>
      apiGet<FindingsResponse>(
        `/findings?analyst_id_in=${UNIT_IDS}&target_id=${encodeURIComponent(targetId)}&limit=40`,
      ),
  })

  // Latest finding per unit (the units emit ~2x/day; take the newest head).
  const latestByUnit = useMemo(() => {
    const m = new Map<string, UnitFindingRow>()
    for (const row of data?.data ?? []) {
      const a = row.analyst_id
      if (!a) continue
      const prev = m.get(a)
      if (!prev || Date.parse(row.produced_at) > Date.parse(prev.produced_at)) m.set(a, row)
    }
    return m
  }, [data])

  return (
    <section data-testid="country-units-assessment">
      <div className="text-label uppercase tracking-wider text-slate-500" data-testid="country-units-scope">
        {targetId} · bounded unit reads
      </div>
      <div className="mb-4 mt-1 text-xs leading-relaxed text-slate-500">
        Each read answers ONE bounded question — cited, faithfulness-verified, and
        measured per unit. These are the product; the full synthesis below is a
        feeder being decomposed into them.
      </div>

      {error instanceof Error && (
        <div className="mb-3 rounded border border-rose-900/60 bg-rose-950/30 px-3 py-2 text-sm text-rose-300">
          Couldn’t load the unit reads: {error.message}
        </div>
      )}

      <div className="space-y-2">
        {UNITS.map((u) => {
          const f = latestByUnit.get(u.id)
          return (
            <div
              key={u.id}
              className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2"
              data-testid="unit-read-card"
              data-unit={u.id}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-label uppercase tracking-wider text-slate-400">{u.label}</span>
                <UnitEvalBadge analystId={u.id} />
                {f?.produced_at && (
                  <span className="text-xs text-slate-600">
                    · {formatDistanceToNow(Date.parse(f.produced_at))} ago
                  </span>
                )}
              </div>
              {f ? (
                <button
                  type="button"
                  onClick={() => selectRow('finding', f.id, f.title ?? u.label, { origin: 'country-units' })}
                  className="mt-1 block w-full text-left text-sm text-slate-200 hover:text-accent-info hover:underline"
                  data-testid="unit-read-link"
                >
                  {f.severity && (
                    <span
                      className="mr-1.5 inline-block h-2 w-2 rounded-full align-middle"
                      style={{ background: SEVERITY_HEX[f.severity] ?? '#8892a0' }}
                      aria-hidden
                    />
                  )}
                  {f.title ?? '(untitled read)'} <span className="text-slate-500">— read →</span>
                </button>
              ) : (
                <div className="mt-1 text-sm text-slate-600" data-testid="unit-no-read">
                  {isLoading ? 'loading…' : 'no read yet — this unit runs on a 12h cadence'}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
