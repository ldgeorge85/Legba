/**
 * The two judge-independent axes of the Accuracy page's grader section (UI-1),
 * moved out of the panel file unchanged in substance:
 *
 *   - **operator gold-set correctness** (M-1, `/v3/eval/correctness`) — a
 *     human's read of whether a finding was right. Every figure renders the
 *     server-composed `display` verbatim, so the verdict mix and the
 *     sufficiency status always travel with the number (a measured 0.00 stays
 *     0.00; a unit below its floor reads *indicative only*).
 *   - **standing external truth** (W-6, the same route's `external_truth`
 *     block) — graded against the world. The headline is a PAIR (record and
 *     voice, never summed); `instrument_limited` renders as a sentence, not a
 *     number; an absent n renders `—`, never 0.
 *
 * Faithfulness is shown beside correctness for contrast and never averaged
 * into it.
 */
import { InfoTip } from '@/components/InfoTip'
import { humanizeId } from '@/lib/deskNames'
import {
  correctnessLabel,
  externalTruthLabel,
  fmtRate,
  orderedStrata,
  type ExternalTruthBoard,
  type ExternalTruthPopulation,
  type UnitCorrectnessBoard,
} from '@/lib/evalOps'

export function OperatorCorrectness({ correctness }: { correctness: UnitCorrectnessBoard | null | undefined }) {
  return (
      <>
      {/* M-1 — the OPERATOR gold-set correctness axis, the platform's only
          JUDGE-INDEPENDENT quality signal. Deliberately its own block, beside
          calibration and never inside it: faithfulness asks "is the prose
          faithful to its cites?", correctness asks "was the read RIGHT?", and
          a finding can score high on the first while failing the second — which
          is exactly what the first gold-set round measured. Every figure here
          renders the server-composed `display` string verbatim, so the verdict
          mix and the sufficiency status always travel with the number. */}
      <div
        className="bg-surf-2 border border-line rounded p-2 mb-2 space-y-1.5 text-xs"
        data-testid="eval-operator-correctness"
      >
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-ink-3 text-label uppercase tracking-wide">
            operator correctness
          </span>
          <InfoTip
            text={
              correctness?.honesty_note ??
              'Operator gold-set correctness is judge-independent and never pooled with faithfulness or calibration.'
            }
            className="text-ink-3 text-label"
            testId="operator-correctness-explain"
          >
            gold-set verdicts (judge-independent, never pooled)
          </InfoTip>
        </div>
        {!correctness?.available ? (
          <div
            className="text-ink-3 text-label py-1"
            data-testid="operator-correctness-empty"
          >
            no operator verdicts yet — the weekly gold-set worksheet has not been
            labelled
          </div>
        ) : (
          <>
            {correctness.fleet && (
              <div
                className="text-ink-1 font-mono"
                data-testid="operator-correctness-fleet"
              >
                {correctnessLabel(correctness.fleet)}
              </div>
            )}
            {correctness.units.map((row) => (
              <div
                key={row.unit}
                className="flex items-baseline gap-2 flex-wrap"
                data-testid={`operator-correctness-${row.unit}`}
              >
                <span className="w-40 shrink-0 text-ink-2">
                  {humanizeId(row.unit)}
                </span>
                <span
                  className={
                    row.sufficient ? 'text-ink-1' : 'text-ink-2 italic'
                  }
                >
                  {correctnessLabel(row)}
                </span>
                {/* The CONTRAST that motivates this axis existing at all —
                    shown beside it, never averaged into it. */}
                {typeof row.faithfulness === 'number' && (
                  <span
                    className="text-ink-3 text-label"
                    title={
                      'Faithfulness is a DIFFERENT measurement (prose vs its own ' +
                      'cites), graded by the machine judge' +
                      (row.judge_pipeline_version
                        ? ` (${row.judge_pipeline_version})`
                        : ' (pre-split-key population)') +
                      '. It is never averaged with correctness.'
                    }
                  >
                    faithfulness {row.faithfulness.toFixed(2)}
                  </span>
                )}
              </div>
            ))}
            <div
              className="text-ink-3 text-label italic"
              data-testid="operator-correctness-honesty-note"
            >
              {correctness.honesty_note}
            </div>
          </>
        )}
      </div>
      </>
  )
}

export function ExternalTruth({ truth }: { truth: ExternalTruthBoard | null | undefined }) {
  return (
      <>
      {/* W-6 — STANDING EXTERNAL TRUTH. The fourth stacked section, immediately
          after the operator axis and for the same reason it sits there: this is
          the only other JUDGE-INDEPENDENT number the platform has, and it is
          graded against the WORLD rather than against a human's read of one
          finding. Three rules are structural here and none of them is a
          preference:

          - The headline is a PAIR (F-12). The record is the DESKS' sentences,
            quoted verbatim under the assembly; the voice is the Assessment's own.
            They are rendered side by side and never summed.
          - `instrument_limited` renders as a SENTENCE, not a number with a flag.
            A week whose two grader families disagreed below 0.75 raw overlap does
            not get to publish a rate at all — that is R3's stop rule, made
            continuous.
          - `decided_rate` is always beside the accuracy, because NOT_FOUND is a
            statement about the SEARCH and a dashboard showing only the first
            number will be read as if it were the second. */}
      <div
        className="bg-surf-2 border border-line rounded p-2 mb-2 space-y-1.5 text-xs"
        data-testid="eval-external-truth"
      >
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-ink-3 text-label uppercase tracking-wide">
            standing external truth
          </span>
          <InfoTip
            text={
              truth?.honesty_note ??
              'Standing external truth grades the read\'s claims against sources outside this pipeline. It is never pooled with faithfulness, calibration or the operator gold set.'
            }
            className="text-ink-3 text-label"
            testId="external-truth-explain"
          >
            graded against the world (web-grounded, never pooled)
          </InfoTip>
        </div>
        {!truth?.available ? (
          <div
            className="text-ink-3 text-label py-1"
            data-testid="external-truth-empty"
          >
            no claims graded against the world yet — the standing auditor's ledger
            is empty for this window
          </div>
        ) : (
          <>
            {/* The PAIR. Two numbers, never one — the note says why. */}
            <div
              className="text-ink-1 font-mono"
              data-testid="external-truth-headline"
            >
              record {fmtRate(truth.headline?.record?.accuracy)} (n=
              {truth.headline?.record?.n_decided ?? '—'}) · voice{' '}
              {fmtRate(truth.headline?.voice?.accuracy)} (n=
              {truth.headline?.voice?.n_decided ?? '—'})
            </div>
            <div
              className="text-ink-3 text-label"
              data-testid="external-truth-headline-note"
            >
              {truth.headline?.note}
            </div>
            {truth.populations.map((pop: ExternalTruthPopulation) => (
              <div
                key={pop.population}
                className="space-y-1"
                data-testid={`external-truth-${pop.population}`}
              >
                <div
                  className={
                    pop.instrument?.instrument_limited
                      ? 'text-amber-300 italic'
                      : pop.sufficient
                        ? 'text-ink-1'
                        : 'text-ink-2 italic'
                  }
                  data-testid={`external-truth-label-${pop.population}`}
                >
                  {externalTruthLabel(pop)}
                </div>
                <div className="text-ink-3 text-label flex gap-3 flex-wrap">
                  <span data-testid={`external-truth-instrument-${pop.population}`}>
                    grader overlap {fmtRate(pop.instrument?.overlap_raw)} (n=
                    {pop.instrument?.overlap_n ?? '—'}
                    {pop.instrument?.band ? `, ${pop.instrument.band}` : ''})
                  </span>
                  <span>
                    excluded: {pop.n_uncheckable} uncheckable · {pop.n_unchecked}{' '}
                    unchecked · {pop.not_found} not-found
                  </span>
                  {typeof pop.search?.degraded_share === 'number' && (
                    <span
                      title="A week when the search plane lost engines is not a week the world got quieter."
                    >
                      search degraded {fmtRate(pop.search.degraded_share)}
                    </span>
                  )}
                </div>
                {orderedStrata(pop).map(([axis, cells]) => (
                  <div
                    key={axis}
                    className="flex items-baseline gap-2 flex-wrap text-label"
                    data-testid={`external-truth-stratum-${pop.population}-${axis}`}
                  >
                    <span className="w-28 shrink-0 text-ink-3">{axis}</span>
                    {cells.length === 0 ? (
                      <span className="text-ink-3 italic">no rows yet</span>
                    ) : (
                      cells.map(([value, cell]) => (
                        <span key={value} className="text-ink-2">
                          {value}{' '}
                          <span className="font-mono text-ink-1">
                            {fmtRate(cell.accuracy)}
                          </span>
                          <span className="text-ink-3"> (n={cell.n_decided})</span>
                        </span>
                      ))
                    )}
                  </div>
                ))}
              </div>
            ))}
            <div
              className="text-ink-3 text-label italic"
              data-testid="external-truth-honesty-note"
            >
              {truth.honesty_note}
            </div>
          </>
        )}
      </div>
      </>
  )
}
