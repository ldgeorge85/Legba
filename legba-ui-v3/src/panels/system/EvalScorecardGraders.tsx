/**
 * Accuracy page · section 4 — the grader roster (UI-1).
 *
 * One table first: per grader the graded count, the population it was drawn
 * from, what was kept out (*unmeasured*, *carried, ungraded*, unresolvable,
 * not found) and the as-of — every figure through `lib/Figure.tsx`, so a
 * grader that has graded nothing reads *unmeasured*, never 0. The graders are
 * different instruments measuring different things; the table puts them side
 * by side and never sums a column. Below it, each grader's own detail block:
 * the machine grader's roster (`EvalGraderRoster`), the operator gold set and
 * standing external truth (`EvalScorecardGoldset`), and the critic rollup
 * (`EvalScorecardCritics`).
 */
import { graderRows, type AccuracyInputs } from '@/lib/accuracyPage'
import type { ExternalTruthBoard, ScorecardRow, UnitCorrectnessBoard } from '@/lib/evalOps'
import type { GraderRoster } from '@/lib/graderRosterModel'
import { PROFILE_UI, type ProfileUi } from '@/lib/profileUi'
import { EvalGraderRoster } from './EvalGraderRoster'
import { CriticRollup } from './EvalScorecardCritics'
import { ExternalTruth, OperatorCorrectness } from './EvalScorecardGoldset'
import { FigureCell, Section } from './EvalScorecardParts'

export function GraderTable({ inputs, ui = PROFILE_UI }: { inputs: AccuracyInputs; ui?: ProfileUi }) {
  const rows = graderRows(inputs, ui)
  return (
    <div className="overflow-x-auto" data-testid="accuracy-grader-table">
      <table className="w-full border-collapse text-label">
        <thead>
          <tr className="border-b border-line text-left text-label text-ink-3">
            <th className="py-0.5 pr-2 font-normal">grader</th>
            <th className="py-0.5 pr-2 font-normal">graded</th>
            <th className="py-0.5 pr-2 font-normal">population</th>
            <th className="py-0.5 pr-2 font-normal">kept out</th>
            <th className="py-0.5 font-normal">source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-line align-baseline" data-testid={`grader-${r.id}`}>
              <td className="py-1 pr-2 text-ink-2">
                {r.grader}
                {r.note && (
                  <div className="text-label text-ink-3" data-testid={`grader-${r.id}-note`}>
                    {r.note}
                  </div>
                )}
              </td>
              <td className="py-1 pr-2">
                <FigureCell fig={r.graded} testId={`grader-${r.id}-graded`} />
              </td>
              <td className="py-1 pr-2">
                <FigureCell fig={r.population.figure} label={r.population.label} testId={`grader-${r.id}-population`} />
              </td>
              <td className="py-1 pr-2">
                {r.keptOut ? (
                  <FigureCell fig={r.keptOut.figure} label={r.keptOut.label} testId={`grader-${r.id}-kept-out`} />
                ) : (
                  <span className="text-ink-3">—</span>
                )}
              </td>
              <td className="py-1 font-mono text-label text-ink-3">{r.route}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function GradersSection({
  inputs,
  roster,
  correctness,
  critic,
  criticLoading,
  endpointPending,
}: {
  inputs: AccuracyInputs
  roster: GraderRoster | null | undefined
  correctness: UnitCorrectnessBoard | null | undefined
  critic: ScorecardRow[] | undefined
  criticLoading: boolean
  endpointPending: boolean
}) {
  const truth: ExternalTruthBoard | null = correctness?.external_truth ?? null
  return (
    <Section
      n={4}
      title="Graders"
      testId="accuracy-graders"
      lead="Each grader with the count it graded, the population that count was drawn from, and what it kept out. Different instruments — never pooled, never summed."
    >
      <GraderTable inputs={inputs} />
      <EvalGraderRoster roster={roster} />
      <OperatorCorrectness correctness={correctness} />
      <ExternalTruth truth={truth} />
      <CriticRollup rows={critic} isLoading={criticLoading} endpointPending={endpointPending} />
    </Section>
  )
}
