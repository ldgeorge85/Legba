/**
 * Accuracy page · section 3 — calibration (UI-1).
 *
 * Three blocks, each over its own key and never pooled: the honest skill
 * scoreboard (the exogenous Brier and the segregated acute-forecast BSS — a
 * thin sample or a degenerate pilot reads as a withheld state, never a bare
 * positive number), the band-calibration harness (`EvalBandCalibration` —
 * ordinal persistence/reversal with each horizon's own denominator, never a
 * Brier score), and the banded verdicts per subject that harness grades
 * (`EvalScorecardBands`).
 */
import { ProvenanceStateBadge } from '@/components/ProvenanceBadge'
import {
  calibrationBanner,
  relTime,
  type AcuteTag,
  type CalibrationScoreboard,
  type CountryScorecard,
} from '@/lib/evalOps'
import { resolveNumberProvenance } from '@/lib/provenance'
import { EvalBandCalibration } from './EvalBandCalibration'
import { SubjectBands } from './EvalScorecardBands'
import { Section } from './EvalScorecardParts'

const ACUTE_PILL: Record<AcuteTag, string> = {
  ready: 'bg-emerald-900 text-emerald-200',
  accumulating: 'bg-amber-900 text-amber-200',
  degenerate: 'bg-rose-900 text-rose-200',
}

/** The honest skill scoreboard — exogenous Brier + acute-forecast BSS. */
export function SkillScoreboard({ cal }: { cal: CalibrationScoreboard | null | undefined }) {
  const banner = calibrationBanner(cal)
  // P4-5 — live|fallback|absent on the calibration NUMBERS. A number reads
  // `live` when a real value backs it, `absent` when the pilot is missing OR the
  // sample is too thin for an honest figure. The route carries no
  // fallback-vs-live signal, so `fallback` is never emitted (SEAMS #46).
  const exogenousState = resolveNumberProvenance({
    value: 1,
    treatAsAbsent: banner.absent || banner.exogenous.insufficient,
  })
  const acuteState = resolveNumberProvenance({
    value: banner.acute.bss,
    treatAsAbsent: banner.absent,
  })
  return (
      <>
      {/* Honest skill scoreboard — exogenous Brier + acute-forecast BSS. A thin
          sample shows the verbatim INSUFFICIENT message; a degenerate pilot shows
          "skill claim withheld"; never a bare positive number. */}
      <div
        className="bg-surf-2 border border-line rounded p-2 mb-2 space-y-1.5 text-xs"
        data-testid="eval-calibration-scoreboard"
      >
        <div className="text-ink-3 text-label uppercase tracking-wide">
          skill scoreboard
        </div>
        <div className="text-amber-300 text-label" data-testid="eval-calibration-legacy-label">
          {banner.legacyLabel} — not the sealed forecast ledger
          {banner.staleNote ? ` · ${banner.staleNote}` : ''}
        </div>
        <div className="flex items-baseline gap-2">
          <span className="w-32 shrink-0 text-ink-2">exogenous Brier</span>
          <span
            className={
              banner.exogenous.insufficient
                ? 'text-amber-300'
                : banner.absent
                  ? 'text-ink-3'
                  : 'text-ink-1 font-mono'
            }
            data-testid="eval-calibration-exogenous"
          >
            {banner.exogenous.label}
          </span>
          <ProvenanceStateBadge state={exogenousState} className="ml-auto" />
        </div>
        <div className="flex items-baseline gap-2">
          <span className="w-32 shrink-0 text-ink-2">acute-forecast BSS</span>
          <span
            className={`shrink-0 rounded px-1 text-label font-mono ${ACUTE_PILL[banner.acute.tag]}`}
            data-testid="eval-calibration-acute-tag"
          >
            {banner.acute.tag}
          </span>
          <span
            className={banner.acute.bss !== null ? 'text-ink-1 font-mono' : 'text-ink-2'}
            data-testid="eval-calibration-acute-label"
          >
            {banner.acute.label}
          </span>
          <ProvenanceStateBadge state={acuteState} className="ml-auto" />
        </div>
        {banner.absent && (
          <div className="text-ink-3 text-label">
            no forecast / calibration pilot has been computed yet
          </div>
        )}
        {!banner.absent && cal?.produced_at && (
          <div className="text-ink-3 text-label">
            computed {relTime(cal.produced_at)}
          </div>
        )}
      </div>
      </>
  )
}

export function CalibrationSection({
  cal,
  scorecards,
}: {
  cal: CalibrationScoreboard | null | undefined
  scorecards: CountryScorecard[] | undefined
}) {
  return (
    <Section
      n={3}
      title="Calibration"
      testId="accuracy-calibration"
      lead="The exogenous Brier and the acute pilot are separate keys and never pooled; band calibration grades the scorecard's ordinal bands and is never a Brier score."
    >
      <SkillScoreboard cal={cal} />
      <EvalBandCalibration section={cal?.band_calibration} />
      <SubjectBands scorecards={scorecards} />
    </Section>
  )
}
