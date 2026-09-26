/**
 * The grader-roster section of the Eval Scorecard (`system.eval_scorecard`),
 * extracted as its own module the way `EvalBandCalibration` was, so the panel
 * stays a thin stack of sections rather than growing a sixth inline block.
 *
 * It renders `GET /api/v1/v3/eval/grader_roster?nights=7` — the nightly
 * correctness grader's whole fleet: each desk's newest graded night, its
 * trailing means over the window, and the roster totals. Until this section
 * existed the number had a per-unit badge in the Inspector and NOTHING
 * fleet-wide: there was nowhere to see that the roster's trailing correctness
 * sits near half at a coverage under a tenth — nine claims in ten the
 * reference never looked at.
 *
 * Four rules are structural here, not stylistic:
 *
 *   1. **Correctness never renders without coverage.** Both headline rows
 *      carry the pair, and the table puts the two columns side by side. There
 *      is no place in this section where a correctness figure stands alone.
 *   2. **Two roster figures, each labelled.** The mean of desks (one desk, one
 *      vote) and the claim-pooled figure (every claim once) are different
 *      numbers that routinely disagree; both render, each with the denominator
 *      it rests on, and neither is ever presented as "the" correctness.
 *   3. **Unmeasured is a word, not a zero.** A null `correctness_share` means
 *      the reference decided nothing that desk said. It prints `unmeasured`,
 *      it is counted in `desks_unmeasured`, and it is excluded from the means —
 *      the server does that arithmetic, and nothing here re-adds it. A coverage
 *      of `0.0%` IS shown as a number, because a reference that bore on nothing
 *      is a measurement.
 *   4. **Thinnest coverage first.** The sort is ascending on coverage because
 *      the desks nobody looked at are the finding; a correctness-ranked table
 *      would bury them under the desks that happened to score well.
 *   5. **One grain, and the header says which.** A composition is graded
 *      against the same reference as the desks it composes over, so the two
 *      grains are never pooled; the route echoes the grain it filtered on and
 *      this section renders it rather than leaving the population implicit.
 *
 * This axis is NOT the operator gold-set axis rendered directly below it
 * (`/v3/eval/correctness`, a human's read over `correctness_labels`). Same
 * word, different instrument, never pooled — which is why they are two
 * sections and not two rows of one table.
 *
 * All ordering / label / headline logic is pure and lives in
 * `@/lib/graderRosterModel`.
 */

import { InfoTip } from '@/components/InfoTip'
import { relTime } from '@/lib/evalOps'
import { humanizeId } from '@/lib/deskNames'
import {
  ABSENT,
  GRADER_ROSTER_EXPLAIN,
  ROSTER_MEAN_EXPLAIN,
  correctnessCell,
  coverageCell,
  pct,
  referenceCell,
  rosterHeadlines,
  sortByCoverageAscending,
  type DeskRosterRow,
  type GraderRoster,
} from '@/lib/graderRosterModel'

/** One desk's row. The server's verbatim badge is the row title, so the
 *  split-out columns always have the one-line original behind them. */
function DeskRow({ desk }: { desk: DeskRosterRow }) {
  const key = `${desk.target_id}:${desk.analyst_id}`
  const unmeasured = desk.latest.correctness_share == null
  return (
    <tr
      className="text-slate-300 align-baseline"
      data-testid={`grader-roster-row-${key}`}
      title={desk.latest.badge}
    >
      <td className="py-0.5 pr-2">
        <span className="text-slate-300">{humanizeId(desk.target_id)}</span>
        <span className="text-slate-600"> · </span>
        <span className="text-slate-400">{humanizeId(desk.analyst_id)}</span>
      </td>
      <td
        className={`text-right font-mono py-0.5 ${
          unmeasured ? 'text-slate-500 italic' : 'text-slate-200'
        }`}
        data-testid={`grader-roster-correctness-${key}`}
      >
        {correctnessCell(desk.latest)}
      </td>
      <td
        className="text-right font-mono py-0.5 text-slate-200"
        data-testid={`grader-roster-coverage-${key}`}
      >
        {coverageCell(desk.latest)}
      </td>
      <td
        className="text-right font-mono py-0.5 text-slate-400"
        data-testid={`grader-roster-nights-${key}`}
        title={
          `window mean correctness ${pct(desk.window.correctness_mean)} · ` +
          `window mean coverage ${pct(desk.window.coverage_mean)}`
        }
      >
        {desk.window.nights_graded}
      </td>
      <td
        className={`py-0.5 pl-2 ${
          desk.latest.reference_state === 'current' ? 'text-slate-500' : 'text-amber-300'
        }`}
        data-testid={`grader-roster-reference-${key}`}
      >
        {referenceCell(desk.latest)}
      </td>
      <td
        className="py-0.5 pl-2 text-slate-500"
        data-testid={`grader-roster-family-${key}`}
        title={
          desk.latest.single_family
            ? 'One grader family stood behind this number — a real reading, and a weaker one.'
            : 'More than one grader family answered this unit.'
        }
      >
        {desk.latest.single_family ? 'single-family' : ABSENT}
      </td>
    </tr>
  )
}

/**
 * The grader-roster section. `roster` is the route's envelope — `null` /
 * `undefined` while it has not loaded or the route is unwired, which reads the
 * same as "no grader rows yet" because neither state has a number to show.
 */
export function EvalGraderRoster({
  roster,
}: {
  roster: GraderRoster | null | undefined
}) {
  const desks = sortByCoverageAscending(roster?.desks)
  const graded = Boolean(roster?.available) && desks.length > 0

  return (
    <div
      className="bg-surface-100 border border-slate-800 rounded p-2 mb-2 space-y-1.5 text-xs"
      data-testid="eval-grader-roster"
    >
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="text-slate-500 text-[10px] uppercase tracking-wide">
          grader correctness, roster
        </span>
        <InfoTip
          text={GRADER_ROSTER_EXPLAIN}
          className="text-slate-600 text-[10px]"
          testId="grader-roster-explain"
        >
          correctness AND coverage per desk (machine grader, never pooled)
        </InfoTip>
      </div>

      {!graded ? (
        <div
          className="text-slate-500 text-[10px] py-1"
          data-testid="grader-roster-empty"
        >
          no grader rows yet — the correctness grader has written nothing inside this
          window, so there is no as-of and no figure to show
        </div>
      ) : (
        <>
          {/* The header: BOTH roster figures, each over the denominator it
              rests on, then how many desks are graded vs unmeasured and when
              the grader last wrote one of these numbers. */}
          <div className="space-y-0.5" data-testid="grader-roster-headline">
            {rosterHeadlines(roster!.roster).map((h) => (
              <div
                key={h.label}
                className="flex items-baseline gap-2 flex-wrap"
                data-testid={`grader-roster-headline-${h.label.replace(/\s+/g, '-')}`}
              >
                <InfoTip
                  text={ROSTER_MEAN_EXPLAIN}
                  className="w-28 shrink-0 text-slate-400"
                  testId={`grader-roster-basis-${h.label.replace(/\s+/g, '-')}`}
                >
                  {h.label}
                </InfoTip>
                <span className="text-slate-300">
                  correctness{' '}
                  <span className="font-mono text-slate-200">{h.correctness}</span>
                </span>
                <span className="text-slate-300">
                  coverage <span className="font-mono text-slate-200">{h.coverage}</span>
                </span>
                <span className="text-slate-600 text-[10px]">{h.basis}</span>
              </div>
            ))}
          </div>

          <div
            className="flex items-baseline gap-2 flex-wrap text-slate-600 text-[10px]"
            data-testid="grader-roster-counts"
          >
            <span data-testid="grader-roster-desks">
              {roster!.roster.desks_graded} desk
              {roster!.roster.desks_graded === 1 ? '' : 's'} graded ·{' '}
              {roster!.roster.desks_unmeasured} unmeasured
            </span>
            <span data-testid="grader-roster-window">
              {roster!.nights}-night window
            </span>
            {/* WHICH population these figures describe. Compositions are
                graded against the same references as the desks they compose
                over, so the roster is one grain at a time and says which. */}
            <span
              data-testid="grader-roster-grain"
              title={
                'Desks and compositions are graded against the same references, so ' +
                'pooling them would count the same claims twice. This roster is one ' +
                'grain at a time.'
              }
            >
              {roster!.grain} grain
            </span>
            <span data-testid="grader-roster-asof">
              as of {roster!.as_of ? relTime(roster!.as_of) : ABSENT}
            </span>
          </div>

          {/* Thinnest coverage first — the desks the reference never looked at
              are the point of the section. */}
          <table
            className="w-full text-[10px] border-collapse"
            data-testid="grader-roster-table"
          >
            <thead>
              <tr className="text-slate-600">
                <th className="text-left font-normal py-0.5">desk</th>
                <th className="text-right font-normal py-0.5 w-40">correctness</th>
                <th className="text-right font-normal py-0.5 w-32">coverage</th>
                <th className="text-right font-normal py-0.5 w-16">nights</th>
                <th className="text-left font-normal py-0.5 pl-2 w-28">reference</th>
                <th className="text-left font-normal py-0.5 pl-2 w-24">families</th>
              </tr>
            </thead>
            <tbody>
              {desks.map((d) => (
                <DeskRow key={`${d.target_id}:${d.analyst_id}`} desk={d} />
              ))}
            </tbody>
          </table>

          <div
            className="text-slate-600 text-[10px] italic"
            data-testid="grader-roster-honesty-note"
          >
            {roster!.honesty_note}
          </div>
        </>
      )}
    </div>
  )
}

export default EvalGraderRoster
