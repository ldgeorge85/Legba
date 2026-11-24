/**
 * The Accuracy page (`system.eval_scorecard`, UI-1; UI-D1 Table A, D12) —
 * the roster-wide answer to "is what it produced any good?", over units ×
 * the profile's subject roster, in the Trust stance.
 *
 * Five sections, each in its own file, in this order:
 *
 *   1. Cohort (`EvalScorecardCohort`) — which units over which roster, the
 *      windows and the as-ofs, generated from the data and the profile.
 *   2. Sealed ledgers (`EvalScorecardLedger`) — a pointer to the forecast
 *      ledger's one home (the Ledger surface's Forecasts region, beside this
 *      page), then the gold set's stages, each with its denominator and as-of.
 *   3. Calibration (`EvalScorecardCalibration`) — the skill scoreboard, band
 *      calibration (`EvalBandCalibration`) and the banded verdicts per subject.
 *   4. Graders (`EvalScorecardGraders`) — per grader the graded count, the
 *      population and what was kept out; then each grader's detail block.
 *   5. What this page does not publish (`EvalScorecardNotPublished`) — the
 *      generated list (`lib/not_published.json`, `scripts/gen_not_published.py`).
 *
 * This file only reads the routes and stacks the sections; every derivation is
 * pure in `lib/accuracyPage.ts` / `lib/evalOps.ts`. Each route has its own
 * query, and a 404 on any of them reads as that section's honest absence,
 * never an error and never a zero. The forecast ledger is not on this page: it
 * is the Ledger surface's own region (`components/MountedView`).
 */
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet, ApiError } from '@/lib/api'
import type { AccuracyInputs } from '@/lib/accuracyPage'
import type {
  CalibrationScoreboard,
  CountryScorecard,
  ScorecardRow,
  UnitCorrectnessBoard,
} from '@/lib/evalOps'
import type { GraderRoster } from '@/lib/graderRosterModel'
import { PROFILE_UI } from '@/lib/profileUi'
import type { PanelProps } from '@/types'
import { ROSTER_PIN, usePinScope } from '@/lib/consultContext'
import { CalibrationSection } from './EvalScorecardCalibration'
import { CohortSection } from './EvalScorecardCohort'
import { GradersSection } from './EvalScorecardGraders'
import { LedgerSection } from './EvalScorecardLedger'
import { NotPublishedSection } from './EvalScorecardNotPublished'

/** GET a route; a 404 (unwired) reads as `fallback`, never an error. */
async function getOr<T>(path: string, fallback: T): Promise<T> {
  try {
    return await apiGet<T>(path)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return fallback
    throw e
  }
}

export default function EvalScorecardPanel({ registration }: PanelProps) {
  const [endpointPending, setEndpointPending] = useState(false)
  // THE PIN HOOK (UI-B6): the Accuracy page reads every unit over the whole
  // roster, so the pin it sets is the roster — never one subject.
  usePinScope(ROSTER_PIN, 'accuracy')

  const critic = useQuery<ScorecardRow[]>({
    queryKey: ['eval-scorecard'],
    queryFn: async () => {
      try {
        const rows = await apiGet<ScorecardRow[]>('/v3/eval/scorecard?limit=500')
        setEndpointPending(false)
        return rows
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) {
          setEndpointPending(true)
          return []
        }
        throw e
      }
    },
    refetchInterval: 60_000,
  })
  const calQ = useQuery<CalibrationScoreboard | null>({
    queryKey: ['eval-calibration'],
    queryFn: () => getOr<CalibrationScoreboard | null>('/v3/eval/calibration', null),
    refetchInterval: 60_000,
  })
  const correctnessQ = useQuery<UnitCorrectnessBoard | null>({
    queryKey: ['eval-correctness'],
    queryFn: () => getOr<UnitCorrectnessBoard | null>('/v3/eval/correctness', null),
    refetchInterval: 60_000,
  })
  const rosterQ = useQuery<GraderRoster | null>({
    queryKey: ['eval-grader-roster'],
    queryFn: () => getOr<GraderRoster | null>('/v3/eval/grader_roster?nights=7', null),
    refetchInterval: 60_000,
  })
  const { data: scorecards } = useQuery<CountryScorecard[]>({
    queryKey: ['eval-country-scorecard'],
    queryFn: () => getOr<CountryScorecard[]>('/v3/eval/country_scorecard', []),
    refetchInterval: 60_000,
  })
  const correctness = correctnessQ.data
  const cal = calQ.data
  const roster = rosterQ.data
  // First paint is the cohort and the stage tables, never a spinner: a route
  // still in flight reads "—, reading <route>" in place, never *unmeasured*.
  const inputs: AccuracyInputs = {
    roster,
    critic: critic.data,
    correctness,
    calibration: cal,
    readAt: correctnessQ.dataUpdatedAt > 0 ? new Date(correctnessQ.dataUpdatedAt).toISOString() : null,
    pending: {
      roster: rosterQ.isPending,
      critic: critic.isPending,
      correctness: correctnessQ.isPending,
      calibration: calQ.isPending,
    },
  }
  const units = PROFILE_UI.unit_noun.plural.toLowerCase()
  const subjects = PROFILE_UI.subject.plural.toLowerCase()

  return (
    <PanelChrome
      registration={registration}
      subtitle={`Accuracy — ${units} × ${subjects}`}
      onRefresh={() => critic.refetch()}
    >
      {critic.error instanceof Error && (
        <div className="text-sm text-rose-400">error: {critic.error.message}</div>
      )}
      <CohortSection inputs={inputs} />
      <LedgerSection inputs={inputs} />
      <CalibrationSection cal={cal} scorecards={scorecards} />
      <GradersSection
        inputs={inputs}
        roster={roster}
        correctness={correctness}
        critic={critic.data}
        criticLoading={critic.isLoading}
        endpointPending={endpointPending}
      />
      <NotPublishedSection inputs={inputs} />
    </PanelChrome>
  )
}
