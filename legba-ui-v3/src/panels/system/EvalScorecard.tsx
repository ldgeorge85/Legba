/**
 * S3 / UI-5 (Tier E). Eval Scorecard (`system.eval`) — analyst-quality surface.
 *
 * "Is this analyst getting better?" — per-analyst rubric scores over time,
 * critic-judge overall trend, and ground-truth backtest accuracy where present.
 *
 * Reads `GET /api/v1/v3/eval/scorecard?analyst_id=&since=&limit=` — one row per
 * critic judgement (per-axis rubric breakdown + overall + optional backtest
 * accuracy). All grouping / trend / axis-mean logic lives in `@/lib/evalOps`
 * so it is unit-tested without a DOM.
 *
 * Worst-scoring analysts surface first (they need attention). Selecting an
 * analyst expands its critic-score trend chart + per-axis rubric bars.
 *
 * NOTE: the cross-analyst `/v3/eval/scorecard` rollup endpoint is not yet wired
 * in the registry API (404 today). Until it lands, this singleton shows an
 * empty state pointing operators at the per-analyst Critiques panel (A5,
 * `/analysts/{id}/critiques`), which carries the same judge-score data scoped
 * to one analyst. A 404 is treated as "endpoint pending", not an error.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { PanelChrome } from '@/components/PanelChrome'
import { InfoTip } from '@/components/InfoTip'
import { apiGet, ApiError } from '@/lib/api'
import {
  bandCalibrationEmpty,
  bandRateLabel,
  bandTone,
  buildScorecards,
  calibrationBanner,
  critScoreTrend,
  evalBadge,
  insufficientLabel,
  isInsufficient,
  orderedBandHorizons,
  relTime,
  scoreBand,
  type AcuteTag,
  type BandTone,
  type CalibrationScoreboard,
  type CountryScorecard,
  type DimensionBand,
  type ScorecardRow,
  type ScoreBand,
} from '@/lib/evalOps'
import type { PanelProps } from '@/types'
import { RecordLink } from '@/components/inspector/RecordLink'
import { ProvenanceStateBadge } from '@/components/ProvenanceBadge'
import { resolveNumberProvenance } from '@/lib/provenance'
import { humanizeId } from '@/lib/deskNames'
import { FAITHFULNESS_EXPLAIN } from '@/lib/verdictModel'

/** U-5 — reused wherever this panel shows a raw "faithfulness N | correctness
 *  N (n=k)" / "unmeasured" badge, so the honest-absence idiom (never a
 *  fabricated score) reads as "nothing measured yet", not as an error. */
const EVAL_BADGE_EXPLAIN =
  `${FAITHFULNESS_EXPLAIN} Correctness (when shown) is graded against operator ` +
  'gold labels. "Unmeasured" means neither has been computed yet for this ' +
  'basis claim — an honest absence, not a broken score.'

const BAND_PILL: Record<ScoreBand, string> = {
  good: 'bg-emerald-900 text-emerald-200',
  warn: 'bg-amber-900 text-amber-200',
  bad: 'bg-rose-900 text-rose-200',
}
const BAND_BAR: Record<ScoreBand, string> = {
  good: 'bg-emerald-500',
  warn: 'bg-amber-500',
  bad: 'bg-rose-500',
}
const ACUTE_PILL: Record<AcuteTag, string> = {
  ready: 'bg-emerald-900 text-emerald-200',
  accumulating: 'bg-amber-900 text-amber-200',
  degenerate: 'bg-rose-900 text-rose-200',
}

// Coarse band-tone → pill color. `insufficient` is a MUTED honest tone, never a
// severity color — an insufficient band renders no colored pill at all.
const TONE_PILL: Record<BandTone, string> = {
  good: 'bg-emerald-900 text-emerald-200',
  watch: 'bg-amber-900 text-amber-200',
  elevated: 'bg-orange-900 text-orange-200',
  high: 'bg-rose-900 text-rose-200',
  critical: 'bg-red-900 text-red-200',
  insufficient: 'bg-slate-800 text-slate-400',
}

// The bounded unit dimensions (analyst_ids) a scorecard cards, in display order.
// There are now SIX; any further extras still render after these (orderedDimensions).
const DIMENSION_ORDER = [
  'leadership_transition',
  'energy_security',
  'escalation',
  'narrative_coordination',
  'internal_stability',
  'military_posture',
] as const

/** Order a scorecard's dimensions: the 4 known units first, then any extras. */
function orderedDimensions(
  dims: Record<string, DimensionBand>,
): Array<[string, DimensionBand]> {
  const known = DIMENSION_ORDER.filter((u) => u in dims).map(
    (u) => [u, dims[u]] as [string, DimensionBand],
  )
  const extras = Object.keys(dims)
    .filter((u) => !(DIMENSION_ORDER as readonly string[]).includes(u))
    .sort()
    .map((u) => [u, dims[u]] as [string, DimensionBand])
  return [...known, ...extras]
}

// Target ids arrive as `country_<tier>_<iso2>` (e.g. country_g20_tr); a
// g20/watch desk id resolves to its country name, anything else (unit/analyst
// ids) is humanized generically (drop the plumbing prefix, split, title-case).
// The shared `lib/deskNames.ts` util is the ONE place this mapping lives (the
// Desks nav group and the Wall's movers list use the same resolver — U-2).
// The raw id is still used for keys / RecordLink drills.
const displayName = humanizeId

// Tone severity ordering — for rolling per-dimension bands up to one headline.
const TONE_SEVERITY: BandTone[] = ['insufficient', 'good', 'watch', 'elevated', 'high', 'critical']

/** Roll the per-dimension bands up to a single country headline tone: the most
 *  severe banded dimension, or `insufficient` when nothing is banded yet. */
function countryHeadline(sc: CountryScorecard): BandTone {
  let worst: BandTone = 'insufficient'
  for (const dim of Object.values(sc.dimensions)) {
    if (isInsufficient(dim)) continue
    const t = bandTone(dim.band)
    if (TONE_SEVERITY.indexOf(t) > TONE_SEVERITY.indexOf(worst)) worst = t
  }
  return worst
}

export default function EvalScorecardPanel({ registration }: PanelProps) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const [endpointPending, setEndpointPending] = useState(false)
  // Which (target:unit) band is expanded to its basis sub-claims.
  const [openBand, setOpenBand] = useState<string | null>(null)
  // Which country's collapsed "insufficient" group is expanded to its why-drill.
  const [openInsufficient, setOpenInsufficient] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery<ScorecardRow[]>({
    queryKey: ['eval-scorecard'],
    queryFn: async () => {
      try {
        const rows = await apiGet<ScorecardRow[]>('/v3/eval/scorecard?limit=500')
        setEndpointPending(false)
        return rows
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) {
          // cross-analyst rollup endpoint not wired yet — empty, not an error.
          setEndpointPending(true)
          return []
        }
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  // The honest skill scoreboard (P4-T4). Its own endpoint — a 404 while the
  // registry route is unwired reads as "no calibration yet", never an error.
  const { data: cal } = useQuery<CalibrationScoreboard | null>({
    queryKey: ['eval-calibration'],
    queryFn: async () => {
      try {
        return await apiGet<CalibrationScoreboard>('/v3/eval/calibration')
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  // The banded per-country scorecard (P4-T3/T5). Its own registry route — a 404
  // while unwired reads as "no scorecard computed yet", never an error. Empty
  // list is a first-class honest state (no country carded yet).
  const { data: scorecards } = useQuery<CountryScorecard[]>({
    queryKey: ['eval-country-scorecard'],
    queryFn: async () => {
      try {
        return await apiGet<CountryScorecard[]>('/v3/eval/country_scorecard')
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  const cards = useMemo(() => buildScorecards(data ?? []), [data])
  const banner = useMemo(() => calibrationBanner(cal), [cal])
  // P4-5 — live|fallback|absent on the calibration NUMBERS. A number reads
  // `live` when a real value backs it, `absent` when the pilot is missing OR the
  // sample is too thin for an honest figure (insufficient = absent, never a bare
  // positive number). The route carries no fallback-vs-live signal, so
  // `fallback` is never emitted — that is the seam a backend fallback-flag would
  // fill (pass an explicit `fallback` into resolveNumberProvenance then).
  const exogenousState = resolveNumberProvenance({
    value: 1,
    treatAsAbsent: banner.absent || banner.exogenous.insufficient,
  })
  const acuteState = resolveNumberProvenance({
    value: banner.acute.bss,
    treatAsAbsent: banner.absent,
  })
  const countryCards = useMemo(
    () =>
      [...(scorecards ?? [])].sort((a, b) =>
        (a.target_id ?? '').localeCompare(b.target_id ?? ''),
      ),
    [scorecards],
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${cards.length} analyst${cards.length === 1 ? '' : 's'} scored`}
      onRefresh={() => refetch()}
    >
      {isLoading && <div className="text-slate-500 text-sm">loading scorecard…</div>}
      {error instanceof Error && (
        <div className="text-rose-400 text-sm">error: {error.message}</div>
      )}

      {/* Honest skill scoreboard — exogenous Brier + acute-forecast BSS. A thin
          sample shows the verbatim INSUFFICIENT message; a degenerate pilot shows
          "skill claim withheld"; never a bare positive number. */}
      <div
        className="bg-surface-100 border border-slate-800 rounded p-2 mb-2 space-y-1.5 text-xs"
        data-testid="eval-calibration-scoreboard"
      >
        <div className="text-slate-500 text-[10px] uppercase tracking-wide">
          skill scoreboard
        </div>
        <div className="flex items-baseline gap-2">
          <span className="w-32 shrink-0 text-slate-400">exogenous Brier</span>
          <span
            className={
              banner.exogenous.insufficient
                ? 'text-amber-300'
                : banner.absent
                  ? 'text-slate-500'
                  : 'text-slate-200 font-mono'
            }
            data-testid="eval-calibration-exogenous"
          >
            {banner.exogenous.label}
          </span>
          <ProvenanceStateBadge state={exogenousState} className="ml-auto" />
        </div>
        <div className="flex items-baseline gap-2">
          <span className="w-32 shrink-0 text-slate-400">acute-forecast BSS</span>
          <span
            className={`shrink-0 rounded px-1 text-[10px] font-mono ${ACUTE_PILL[banner.acute.tag]}`}
            data-testid="eval-calibration-acute-tag"
          >
            {banner.acute.tag}
          </span>
          <span
            className={banner.acute.bss !== null ? 'text-slate-200 font-mono' : 'text-slate-400'}
            data-testid="eval-calibration-acute-label"
          >
            {banner.acute.label}
          </span>
          <ProvenanceStateBadge state={acuteState} className="ml-auto" />
        </div>
        {banner.absent && (
          <div className="text-slate-600 text-[10px]">
            no forecast / calibration pilot has been computed yet
          </div>
        )}
        {!banner.absent && cal?.produced_at && (
          <div className="text-slate-600 text-[10px]">
            computed {relTime(cal.produced_at)}
          </div>
        )}
      </div>

      {/* Band-calibration harness (P2-3) — persistence/reversal rates over
          hard band-ladder transitions at fixed 14d/28d horizons, graded
          held/reverted/worsened against LATER scorecard rows only. HONESTLY
          NOT a Brier score (bands are ordinal risk categories, not
          probabilities) — the route's own honesty_note states this, and this
          panel never relabels a rate as a skill/probability number. An
          honest awaiting state renders until the tracker has graded at least
          one claim. */}
      <div
        className="bg-surface-100 border border-slate-800 rounded p-2 mb-2 space-y-1.5 text-xs"
        data-testid="eval-band-calibration"
      >
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-slate-500 text-[10px] uppercase tracking-wide">
            band calibration
          </span>
          <span
            className="text-slate-600 text-[10px]"
            title="Ordinal band-persistence stability — NOT a probability or Brier score."
          >
            persistence / reversal rate (not a Brier score)
          </span>
        </div>
        {bandCalibrationEmpty(cal?.band_calibration) ? (
          <div className="text-slate-500 text-[10px] py-1" data-testid="band-calibration-empty">
            no band transitions graded yet
          </div>
        ) : (
          <>
            <div className="text-slate-600 text-[10px]">
              {cal!.band_calibration!.claims_total} claim
              {cal!.band_calibration!.claims_total === 1 ? '' : 's'} logged
              {cal!.band_calibration!.produced_at &&
                ` · ${relTime(cal!.band_calibration!.produced_at)}`}
            </div>
            {orderedBandHorizons(cal!.band_calibration!.horizons).map(([label, h]) => (
              <div
                key={label}
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
                >
                  n={h.scored}
                </span>
              </div>
            ))}
            <div className="text-slate-600 text-[10px] italic" data-testid="band-calibration-honesty-note">
              {cal!.band_calibration!.honesty_note ??
                'Ordinal band-persistence stability — not a Brier score.'}
            </div>
          </>
        )}
      </div>

      {/* Banded per-country scorecard (P4-T3/T5). One honest card per active G20
          country: a band per dimension, click a band → its verified sub-claims
          (basis findings, each a P1 evidence + signed-lineage drill), a per-dim
          faithfulness+correctness badge, and an explicit not-enough-verified
          state for an insufficient-evidence band — never a fabricated band. */}
      <div className="mb-2 space-y-2" data-testid="scorecard-country-list">
        <div className="text-slate-500 text-[10px] uppercase tracking-wide">
          banded verdicts (per country)
        </div>
        {countryCards.length === 0 && (
          <div
            className="text-slate-500 text-center py-3 text-xs"
            data-testid="scorecard-empty"
          >
            no scorecard computed yet
          </div>
        )}
        {countryCards.map((sc) => {
          const dims = orderedDimensions(sc.dimensions)
          const bandedN = dims.filter(([, d]) => !isInsufficient(d)).length
          return (
            <div
              key={sc.target_id}
              className="bg-surface-100 border border-slate-800 rounded p-2 space-y-1.5"
              data-testid={`scorecard-card-${sc.target_id}`}
            >
              <div className="flex items-baseline gap-2">
                <RecordLink
                  kind="target"
                  id={sc.target_id}
                  label={displayName(sc.target_id)}
                  origin="scorecard"
                  className="truncate text-slate-200"
                />
                {/* One rolled-up headline verdict per country — the most severe
                    banded dimension (insufficient when nothing is banded yet). */}
                {(() => {
                  const tone = countryHeadline(sc)
                  return (
                    <span
                      className={`shrink-0 rounded px-1 text-[10px] font-mono ${TONE_PILL[tone]}`}
                      data-testid={`scorecard-headline-${sc.target_id}`}
                      title="rolled-up country verdict (most severe banded dimension)"
                    >
                      {tone === 'insufficient' ? 'insufficient' : tone}
                    </span>
                  )
                })()}
                <span className="text-slate-600 text-[10px] shrink-0">
                  {bandedN}/{dims.length} banded
                </span>
                {sc.generated_at && (
                  <span className="text-slate-600 text-[10px] shrink-0 ml-auto">
                    {relTime(sc.generated_at)}
                  </span>
                )}
              </div>

              <div className="space-y-1">
                {/* Banded dimensions — one row each, drill to verified sub-claims. */}
                {dims
                  .filter(([, dim]) => !isInsufficient(dim))
                  .map(([unit, dim]) => {
                    const bandKey = `${sc.target_id}:${unit}`
                    const open = openBand === bandKey
                    const flagged = dim.eval?.faithfulness_flagged === true
                    return (
                      <div
                        key={unit}
                        className="text-[11px]"
                        data-testid={`scorecard-dim-${sc.target_id}-${unit}`}
                      >
                        <div className="flex items-baseline gap-2">
                          <span className="w-40 shrink-0 truncate text-slate-400" title={unit}>
                            {displayName(unit)}
                          </span>
                          <button
                            type="button"
                            className={`shrink-0 rounded px-1 text-[10px] font-mono ${TONE_PILL[bandTone(dim.band)]}`}
                            onClick={() => setOpenBand(open ? null : bandKey)}
                            data-testid={`scorecard-band-${sc.target_id}-${unit}`}
                            title={`${dim.basis.length} verified sub-claim${dim.basis.length === 1 ? '' : 's'}`}
                          >
                            {dim.band}
                          </button>
                          {dim.effective_confidence !== null && (
                            <span className="text-slate-500 font-mono text-[10px]">
                              eff {dim.effective_confidence.toFixed(2)}
                            </span>
                          )}
                          {flagged && (
                            <span
                              className="text-rose-400 text-[10px] shrink-0"
                              title="aggregate faithfulness below floor"
                            >
                              ⚑ low faithfulness
                            </span>
                          )}
                        </div>

                        {/* Expanded basis — the verified sub-claims this band rests
                            on. Each basis id is a P1 evidence + signed-lineage drill.
                            basis.length===0 never renders a drill target. */}
                        {open && (
                          <div className="mt-1 ml-40 pl-2 border-l border-slate-800 space-y-1">
                            {dim.basis.length === 0 ? (
                              <div className="text-slate-600 text-[10px]">no basis sub-claim</div>
                            ) : (
                              dim.basis.map((basisId) => (
                                <div key={basisId} data-testid={`scorecard-basis-${basisId}`}>
                                  <RecordLink
                                    kind="finding"
                                    id={basisId}
                                    label="sub-claim"
                                    origin="scorecard"
                                    className="text-[10px]"
                                  />
                                </div>
                              ))
                            )}
                            <InfoTip
                              text={EVAL_BADGE_EXPLAIN}
                              className={flagged ? 'text-rose-300 text-[10px]' : 'text-slate-500 text-[10px]'}
                              testId={`scorecard-eval-badge-${sc.target_id}-${unit}`}
                            >
                              {evalBadge(dim.eval)}
                            </InfoTip>
                          </div>
                        )}
                      </div>
                    )
                  })}

                {/* The identical "insufficient" rows collapse behind ONE why-drill
                    (they all say the same thing) instead of a wall of red-ish
                    rows — the honest state stays one click away. */}
                {(() => {
                  const insuff = dims.filter(([, dim]) => isInsufficient(dim))
                  if (insuff.length === 0) return null
                  const open = openInsufficient === sc.target_id
                  return (
                    <div
                      className="text-[11px]"
                      data-testid={`scorecard-insufficient-group-${sc.target_id}`}
                    >
                      <button
                        type="button"
                        className="flex w-full items-baseline gap-2 text-left"
                        onClick={() => setOpenInsufficient(open ? null : sc.target_id)}
                        data-testid={`scorecard-insufficient-toggle-${sc.target_id}`}
                        aria-expanded={open}
                      >
                        <span className="rounded bg-slate-800 px-1 text-[10px] text-slate-400">
                          {insuff.length} dimension{insuff.length === 1 ? '' : 's'} insufficient
                        </span>
                        <span className="text-slate-500 text-[10px]">{open ? 'hide' : 'why?'}</span>
                      </button>
                      {open && (
                        <div className="mt-1 ml-2 pl-2 border-l border-slate-800 space-y-0.5">
                          {insuff.map(([unit, dim]) => (
                            <div
                              key={unit}
                              className="flex items-baseline gap-2"
                              data-testid={`scorecard-insufficient-${sc.target_id}-${unit}`}
                            >
                              <span
                                className="w-40 shrink-0 truncate text-slate-400"
                                title={unit}
                              >
                                {displayName(unit)}
                              </span>
                              <InfoTip
                                text={
                                  'No band has been computed for this dimension yet — an honest ' +
                                  'absence (nothing measured), not an error. ' +
                                  `Reason: ${insufficientLabel(dim.reason)}.`
                                }
                                className="text-slate-500 text-[10px]"
                                testId={`scorecard-insufficient-reason-${sc.target_id}-${unit}`}
                              >
                                {insufficientLabel(dim.reason)}
                              </InfoTip>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })()}
              </div>

              {/* The P3 composition aggregate node. */}
              <div className="flex items-baseline gap-2 text-[10px] border-t border-slate-800 pt-1">
                <span className="w-40 shrink-0 text-slate-500">composition</span>
                {sc.composition.present && sc.composition.basis[0] ? (
                  <RecordLink
                    kind="finding"
                    id={sc.composition.basis[0]}
                    label="composition"
                    origin="scorecard"
                    className="text-[10px]"
                  />
                ) : (
                  <span className="text-slate-600">no verified composition</span>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div className="flex-1 overflow-auto space-y-2 text-xs" data-testid="eval-scorecard-list">
        {!isLoading && cards.length === 0 && !endpointPending && (
          <div className="text-slate-500 text-center py-4">
            no critic judgements yet — eval loop hasn't scored any analysts
          </div>
        )}
        {!isLoading && cards.length === 0 && endpointPending && (
          <div
            className="text-slate-400 text-center py-4 space-y-1"
            data-testid="eval-endpoint-pending"
          >
            <div>cross-analyst scorecard rollup not yet wired</div>
            <div className="text-[11px] text-slate-500">
              per-analyst critic scores are live in the Critiques panel
              (<code className="font-mono">/analysts/&#123;id&#125;/critiques</code>)
            </div>
          </div>
        )}
        {cards.map((c) => {
          const band = scoreBand(c.latest_overall)
          const open = expanded === c.analyst_id
          const trendSign = c.trend_delta >= 0 ? '+' : ''
          const trendColor =
            c.trend_delta > 0.001
              ? 'text-emerald-400'
              : c.trend_delta < -0.001
                ? 'text-rose-400'
                : 'text-slate-400'
          const trend = critScoreTrend(c.rows)
          return (
            <div
              key={c.analyst_id}
              className="bg-surface-100 border border-slate-800 rounded p-2"
              data-testid={`eval-card-${c.analyst_id}`}
            >
              <div>
                <div className="flex items-baseline gap-2">
                  <button
                    className="flex min-w-0 flex-1 items-baseline gap-2 text-left"
                    onClick={() => setExpanded(open ? null : c.analyst_id)}
                    data-testid={`eval-card-header-${c.analyst_id}`}
                  >
                    <span className={`shrink-0 rounded px-1 text-[10px] font-mono ${BAND_PILL[band]}`}>
                      {(c.latest_overall * 100).toFixed(0)}
                    </span>
                    <span className="truncate text-slate-200" title={c.analyst_id}>
                      {displayName(c.analyst_id)}
                    </span>
                  </button>
                  <RecordLink
                    kind="analyst"
                    id={c.analyst_id}
                    label="inspect"
                    origin="eval-scorecard"
                    className="shrink-0 text-[10px]"
                  />
                  <span className={`font-mono ${trendColor}`} title="trend over window">
                    {trendSign}
                    {(c.trend_delta * 100).toFixed(1)}%
                  </span>
                  {c.latest_accuracy !== null && (
                    <span className="text-slate-500 font-mono shrink-0" title="ground-truth backtest accuracy">
                      gt {(c.latest_accuracy * 100).toFixed(0)}%
                    </span>
                  )}
                  <span className="text-slate-600 shrink-0">{c.rows.length} judged</span>
                </div>
                {/* per-axis rubric mean bars */}
                <div className="mt-1.5 space-y-1">
                  {Object.entries(c.axis_means).map(([axis, v]) => (
                    <div key={axis} className="flex items-center gap-2" data-testid={`eval-axis-${c.analyst_id}-${axis}`}>
                      <span className="w-24 shrink-0 text-slate-400 truncate">{axis}</span>
                      <div className="flex-1 h-1.5 bg-surface-200 rounded overflow-hidden">
                        <div
                          className={`h-full ${BAND_BAR[scoreBand(v)]}`}
                          style={{ width: `${Math.round(v * 100)}%` }}
                        />
                      </div>
                      <span className="w-9 text-right text-slate-500 font-mono">
                        {(v * 100).toFixed(0)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {open && trend.length > 1 && (
                <div className="mt-2 border-t border-slate-800 pt-2">
                  <div className="text-slate-500 text-[10px] uppercase tracking-wide mb-1">
                    critic-score trend
                  </div>
                  <div className="h-32" data-testid={`eval-trend-${c.analyst_id}`}>
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={trend} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.4} />
                        <XAxis dataKey="label" stroke="#94a3b8" fontSize={9} />
                        <YAxis
                          domain={[0, 1]}
                          stroke="#94a3b8"
                          fontSize={9}
                          width={34}
                          tickFormatter={(v: number) => `${(v * 100).toFixed(0)}`}
                        />
                        <Tooltip
                          contentStyle={{
                            background: '#1e293b',
                            border: '1px solid #334155',
                            borderRadius: 4,
                            fontSize: 11,
                          }}
                          labelStyle={{ color: '#cbd5e1' }}
                          formatter={(v: unknown) =>
                            typeof v === 'number' ? [`${(v * 100).toFixed(1)}%`, 'overall'] : [String(v), 'overall']
                          }
                        />
                        <Line
                          type="monotone"
                          dataKey="overall"
                          stroke="#38bdf8"
                          strokeWidth={2}
                          dot={{ r: 2 }}
                          isAnimationActive={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              )}
              {open && trend.length <= 1 && (
                <div className="mt-2 border-t border-slate-800 pt-2 text-slate-600 text-[10px]">
                  only one judgement — no trend yet
                </div>
              )}
            </div>
          )
        })}
      </div>
    </PanelChrome>
  )
}
