/**
 * The band-calibration section of the Eval Scorecard (`system.eval_scorecard`),
 * extracted as its own module so the panel stays a thin stack of sections.
 *
 * It renders `GET /api/v1/v3/eval/calibration`'s additive `band_calibration`
 * key — the freshest `band_calibration_tracker` finding, reduced by
 * `v3_api._reduce_band_calibration`. The tracker turns scorecard band
 * TRANSITIONS into resolvable claims, auto-resolves them at fixed 14d/28d
 * horizons against LATER scorecard rows only, and grades each on the band
 * ladder. Everything below is that grading, and nothing else.
 *
 * Four rules are structural here, not stylistic:
 *
 *   1. **These are not Brier scores and the section says so.** Bands are
 *      ordinal risk categories, not probabilities. The route carries a hard
 *      `no_brier` flag and a verbatim `honesty_note`; both render — the flag
 *      as the statement it stands for (`BAND_NO_BRIER_STATEMENT`), the note
 *      word for word. Nothing in this file may relabel a persistence rate as
 *      a skill, probability or accuracy number.
 *   2. **The two absences are different.** `available: false` means no tracker
 *      finding exists — "not measured yet", with NO as-of, because there is no
 *      run to date. `available: true, claims_total: 0` means the tracker ran
 *      and found nothing to claim — that one HAS an as-of, and showing it is
 *      the difference between "we never looked" and "we looked and it was
 *      quiet".
 *   3. **A rate carries its denominator, and a zero denominator is `—`.**
 *      `persistence_rate` / `reversal_rate` are `null` exactly when `scored`
 *      is 0; `bandRateLabel` renders that honestly. Outcome shares are taken
 *      over `resolved` — the denominator the counts actually sum to — and
 *      named as such.
 *   4. **The excluded outcomes stay visible.** `insufficient` and
 *      `unresolvable` are honest abstains held OUT of both rate denominators.
 *      They render in the outcome table, muted and labelled as excluded, so
 *      the rate's denominator can never silently shrink without the reader
 *      seeing where the claims went.
 *
 * All ordering / share / slice logic is pure and lives in `@/lib/evalOps`.
 */

import { InfoTip } from '@/components/InfoTip'
import {
  BAND_CALIBRATION_HORIZON_ORDER,
  BAND_NO_BRIER_STATEMENT,
  BAND_OUTCOME_MEANING,
  bandCalibrationState,
  bandOutcomeRows,
  bandRateLabel,
  bandShareLabel,
  bandSliceHorizon,
  bandSliceRows,
  orderedBandHorizons,
  relTime,
  type BandCalibrationHorizon,
  type BandCalibrationSection,
  type BandCalibrationSlice,
} from '@/lib/evalOps'

/** What a missing string/number renders as. Never `0`, never blank. */
const ABSENT = '—'

/** The pinned resolution spec, explained. Keyed by the id the tracker stamps on
 *  every claim (`band_calibration_tracker.RESOLUTION_SPEC`), so an unrecognised
 *  future spec renders its id with no invented gloss. */
const RESOLUTION_SPEC_EXPLAIN: Record<string, string> = {
  hard_band_at_horizon_v1:
    'For each claim (desk, dimension, from→to band, T0) the resolver reads the LATEST ' +
    'scorecard row in (T0, T0+horizon] and grades that row\'s band on the ladder. ' +
    'Scorecard rows are append-only, so the row set inside the horizon is frozen once ' +
    'the horizon passes — every grade is stable and independently recomputable.',
}

function specTitle(spec: string | null): string {
  if (!spec) return 'no resolution spec on this finding'
  return (
    RESOLUTION_SPEC_EXPLAIN[spec] ??
    `resolution spec ${spec} — this build carries no description for it`
  )
}

/** The horizon's rate pair + the denominators behind it. One line, because the
 *  two rates are complements over the same `scored` denominator. */
function HorizonRates({ label, h }: { label: string; h: BandCalibrationHorizon }) {
  return (
    <div
      className="flex items-baseline gap-2 flex-wrap"
      data-testid={`band-calibration-horizon-${label}`}
    >
      <span className="w-10 shrink-0 text-slate-400 font-mono">{label}</span>
      <span className="text-slate-300">
        persistence{' '}
        <span
          className="font-mono text-slate-200"
          data-testid={`band-calibration-persistence-${label}`}
        >
          {bandRateLabel(h.persistence_rate)}
        </span>
      </span>
      <span className="text-slate-300">
        reversal{' '}
        <span
          className="font-mono text-slate-200"
          data-testid={`band-calibration-reversal-${label}`}
        >
          {bandRateLabel(h.reversal_rate)}
        </span>
      </span>
      <span
        className="text-slate-600 text-[10px]"
        title={
          `confirmed=${h.confirmed} reverted=${h.reverted} (n_scored=${h.scored}) · ` +
          `excluded: insufficient=${h.excluded_insufficient} unresolvable=${h.excluded_unresolvable}`
        }
        data-testid={`band-calibration-scored-${label}`}
      >
        n scored={h.scored} · {h.resolved} resolved of {h.resolved + h.open} ({h.open} open)
      </span>
    </div>
  )
}

/** One horizon's graded-outcome breakdown. The counts are the server's; the
 *  share is over `resolved`, named in the header so it can never be read as a
 *  share of the rate denominator (which excludes two of these rows). */
function OutcomeTable({ label, h }: { label: string; h: BandCalibrationHorizon }) {
  const rows = bandOutcomeRows(h)
  if (rows.length === 0) {
    return (
      <div
        className="text-slate-600 text-[10px] pl-10"
        data-testid={`band-calibration-outcomes-empty-${label}`}
      >
        nothing resolved at {label} yet — no outcome has occurred, which is not the
        same as every outcome being zero
      </div>
    )
  }
  return (
    <table
      className="w-full text-[10px] border-collapse"
      data-testid={`band-calibration-outcomes-${label}`}
    >
      <thead>
        <tr className="text-slate-600">
          <th className="text-left font-normal pl-10 py-0.5">{label} outcome</th>
          <th className="text-right font-normal py-0.5 w-12">claims</th>
          <th className="text-right font-normal py-0.5 w-24">share of resolved</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr
            key={r.outcome}
            className={r.scored ? 'text-slate-300' : 'text-slate-500'}
            data-testid={`band-calibration-outcome-${label}-${r.outcome}`}
            title={BAND_OUTCOME_MEANING[r.outcome] ?? r.outcome}
          >
            <td className="pl-10 py-0.5">
              {r.outcome}
              {!r.scored && (
                <span className="text-slate-600"> · excluded from both rates</span>
              )}
            </td>
            <td className="text-right font-mono py-0.5">{r.n}</td>
            <td className="text-right font-mono py-0.5">{bandShareLabel(r.share)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** `by_direction` / `by_dimension` as one compact table: the slice, its claim
 *  count, and its persistence rate at each horizon. A slice that never carried
 *  a horizon reads `—`, never a zeroed block. */
function SliceTable({
  id,
  caption,
  explain,
  slices,
  emptyText,
}: {
  id: string
  caption: string
  explain: string
  slices: Record<string, BandCalibrationSlice>
  emptyText: string
}) {
  const rows = bandSliceRows(slices)
  const horizons = BAND_CALIBRATION_HORIZON_ORDER
  if (rows.length === 0) {
    return (
      <div className="space-y-0.5">
        <div className="text-slate-500 text-[10px] uppercase tracking-wide">{caption}</div>
        <div className="text-slate-600 text-[10px]" data-testid={`band-calibration-${id}-empty`}>
          {emptyText}
        </div>
      </div>
    )
  }
  return (
    <div className="space-y-0.5">
      <InfoTip
        text={explain}
        className="text-slate-500 text-[10px] uppercase tracking-wide"
        testId={`band-calibration-${id}-explain`}
      >
        {caption}
      </InfoTip>
      <table
        className="w-full text-[10px] border-collapse"
        data-testid={`band-calibration-${id}`}
      >
        <thead>
          <tr className="text-slate-600">
            <th className="text-left font-normal py-0.5">{id === 'by-direction' ? 'direction' : 'dimension'}</th>
            <th className="text-right font-normal py-0.5 w-12">claims</th>
            {horizons.map((hz) => (
              <th key={hz} className="text-right font-normal py-0.5 w-32">
                {hz} persistence
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr
              key={r.key}
              className="text-slate-300"
              data-testid={`band-calibration-${id}-${r.key}`}
            >
              <td className="py-0.5">{r.key}</td>
              <td className="text-right font-mono py-0.5">{r.claims}</td>
              {horizons.map((hz) => {
                const h = bandSliceHorizon(r.slice, hz)
                return (
                  <td
                    key={hz}
                    className="text-right font-mono py-0.5 text-slate-200"
                    data-testid={`band-calibration-${id}-${r.key}-${hz}`}
                    title={
                      h == null
                        ? `no ${hz} block on this slice`
                        : `confirmed=${h.confirmed} reverted=${h.reverted} (n_scored=${h.scored})`
                    }
                  >
                    {h == null ? ABSENT : bandRateLabel(h.persistence_rate)}
                    {h != null && (
                      <span className="text-slate-600"> (n={h.scored})</span>
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The band-calibration section. `section` is the route's `band_calibration`
 * key — `null` when the read itself failed, which reads the same as
 * "not measured yet" because neither state has a number to show.
 */
export function EvalBandCalibration({
  section,
}: {
  section: BandCalibrationSection | null | undefined
}) {
  const state = bandCalibrationState(section)

  return (
    <div
      className="bg-surface-100 border border-slate-800 rounded p-2 mb-2 space-y-1.5 text-xs"
      data-testid="eval-band-calibration"
    >
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="text-slate-500 text-[10px] uppercase tracking-wide">
          band calibration
        </span>
        <InfoTip
          text={BAND_NO_BRIER_STATEMENT}
          className="text-slate-600 text-[10px]"
          testId="band-calibration-explain"
        >
          persistence / reversal rate (not a Brier score)
        </InfoTip>
      </div>

      {state !== 'graded' ? (
        <div className="text-slate-500 text-[10px] py-1" data-testid="band-calibration-empty">
          {state === 'unavailable' ? (
            <span data-testid="band-calibration-unavailable">
              not measured yet — the band-calibration tracker has produced no finding, so
              there is no as-of to show
            </span>
          ) : (
            <span data-testid="band-calibration-no-claims">
              no band transitions graded yet — the tracker ran
              {section?.produced_at ? ` ${relTime(section.produced_at)}` : ''} and logged
              zero claims
            </span>
          )}
        </div>
      ) : (
        <>
          {/* Header: which spec graded these, how many claims, as of when. */}
          <div
            className="flex items-baseline gap-2 flex-wrap text-slate-600 text-[10px]"
            data-testid="band-calibration-header"
          >
            <span
              className="font-mono text-slate-400"
              title={specTitle(section!.resolution_spec)}
              data-testid="band-calibration-resolution-spec"
            >
              {section!.resolution_spec ?? ABSENT}
            </span>
            <span data-testid="band-calibration-claims-total">
              {section!.claims_total} claim{section!.claims_total === 1 ? '' : 's'} logged
            </span>
            <span data-testid="band-calibration-asof">
              as of {section!.produced_at ? relTime(section!.produced_at) : ABSENT}
            </span>
          </div>

          {/* One block per horizon, 14d first: the rate pair, then the graded
              outcomes it was computed from. */}
          {orderedBandHorizons(section!.horizons).map(([label, h]) => (
            <div key={label} className="space-y-0.5">
              <HorizonRates label={label} h={h} />
              <OutcomeTable label={label} h={h} />
            </div>
          ))}

          <SliceTable
            id="by-direction"
            caption="by direction"
            explain={
              'Deterioration claims (the band moved up the ladder) and improvement claims ' +
              '(it moved down) are graded separately: a harness that persists on one and ' +
              'reverts on the other is not the same instrument as one that does neither.'
            }
            slices={section!.by_direction}
            emptyText="no per-direction split on this finding"
          />

          <SliceTable
            id="by-dimension"
            caption="by dimension"
            explain={
              'The same rates per scorecard dimension. A dimension whose bands never hold ' +
              'is a statement about that dimension\'s banding, not about the desks reading it.'
            }
            slices={section!.by_dimension}
            emptyText="no per-dimension split on this finding"
          />

          {/* The honesty contract: the flag spelled out, then the route's own
              note verbatim. Both, because they say different things — the flag
              is what cannot exist here, the note is why. */}
          {section!.no_brier && (
            <div
              className="text-slate-500 text-[10px]"
              data-testid="band-calibration-no-brier"
            >
              {BAND_NO_BRIER_STATEMENT}
            </div>
          )}
          <div
            className="text-slate-600 text-[10px] italic"
            data-testid="band-calibration-honesty-note"
          >
            {section!.honesty_note ?? BAND_NO_BRIER_STATEMENT}
          </div>
        </>
      )}
    </div>
  )
}

export default EvalBandCalibration
