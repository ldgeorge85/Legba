/**
 * Accuracy page · section 1 — the cohort paragraph (UI-1).
 *
 * Generated from the data, never hand-written: which units (the profile's
 * `rosters.units`) over which subject rosters (`rosters.scored_subject_tags`,
 * labelled by the profile), the grader's window and grain, the critic page's
 * span, the gold set's pinned weeks and the calibration as-of. Nouns are the
 * profile's (`lib/profileUi.ts`); where the profile declares none the platform
 * noun renders — never the exemplar's word.
 */
import { Figure, asOfText } from '@/lib/Figure'
import { absencePhrase } from '@/lib/absenceVocabulary'
import { cohortModel, type AccuracyInputs } from '@/lib/accuracyPage'
import { PROFILE_UI, type ProfileUi } from '@/lib/profileUi'
import { Section } from './EvalScorecardParts'

function andList(items: string[]): string {
  if (items.length <= 1) return items.join('')
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

export function CohortSection({ inputs, ui = PROFILE_UI }: { inputs: AccuracyInputs; ui?: ProfileUi }) {
  const m = cohortModel(inputs, ui)
  const n = m.nouns
  return (
    <Section n={1} title="Cohort" testId="accuracy-cohort">
      <p className="text-label leading-relaxed text-ink-2" data-testid="accuracy-cohort-paragraph">
        {m.units ? (
          <span data-testid="accuracy-cohort-units">
            This page reads the {m.units.length} {n.units} of the <span className="text-ink-1">{m.profile}</span>{' '}
            profile — {andList(m.units)} —{' '}
          </span>
        ) : (
          <span data-testid="accuracy-cohort-units">
            The <span className="text-ink-1">{m.profile}</span> profile declares no {n.unit} roster, so this page
            reads every {n.unit} the routes report,{' '}
          </span>
        )}
        <span data-testid="accuracy-cohort-roster">
          {m.rosters.length
            ? `over the ${n.subjects} on its ${andList(m.rosters)} roster${m.rosters.length === 1 ? '' : 's'}.`
            : `over every ${n.subject} the routes report.`}
        </span>{' '}
        {m.pending.grader ? (
          <span data-testid="accuracy-cohort-grader">The correctness grader&apos;s roster is being read.</span>
        ) : m.grader ? (
          <span data-testid="accuracy-cohort-grader">
            The correctness grader&apos;s window is the trailing{' '}
            {m.grader.nights ?? absencePhrase('dash')} nights at the {m.grader.grain ?? absencePhrase('dash')} grain:{' '}
            <Figure {...m.grader.pairs} testId="accuracy-cohort-pairs" /> across{' '}
            <Figure {...m.grader.subjects} testId="accuracy-cohort-subjects" />.
          </span>
        ) : (
          <span data-testid="accuracy-cohort-grader">
            The correctness grader has graded nothing in its window — {absencePhrase('unmeasured')}, not zero.
          </span>
        )}{' '}
        {m.pending.critic ? (
          <span data-testid="accuracy-cohort-critic">The critic page is being read.</span>
        ) : m.critic ? (
          <span data-testid="accuracy-cohort-critic">
            The critic rollup reads the newest <Figure {...m.critic.judgements} testId="accuracy-cohort-critic-n" />
            {m.critic.oldest && m.critic.newest ? `, from ${m.critic.oldest} to ${m.critic.newest}` : ''}.
          </span>
        ) : (
          <span data-testid="accuracy-cohort-critic">No critic judgement was read.</span>
        )}{' '}
        <span data-testid="accuracy-cohort-goldset">
          The operator gold set has <Figure {...m.goldWeeks} testId="accuracy-cohort-weeks" />.
        </span>{' '}
        <span data-testid="accuracy-cohort-calibration">The calibration finding is {asOfText(m.calibrationAsOf)}.</span>
      </p>
    </Section>
  )
}
