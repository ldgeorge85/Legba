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
 * The panel stacks six sections, each over its own route key and never pooled
 * with another: the honest skill scoreboard (exogenous Brier + acute BSS), the
 * band-calibration harness (`EvalBandCalibration` — ordinal persistence, never
 * a Brier score), the operator gold-set correctness axis, standing external
 * truth, the banded per-desk country scorecard with its per-band drill, and the
 * per-analyst critic rollup.
 *
 * "download CSV" (7b-iii) on the banded per-country section serialises the
 * bands the page is showing — one row per (desk, dimension), insufficient
 * dimensions included — straight from the `CountryScorecard[]` in hand
 * (`@/lib/bandCsv`). No second fetch and no new route, so the file cannot
 * disagree with the section it came from.
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
import { Download } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { InfoTip } from '@/components/InfoTip'
import { apiGet, ApiError } from '@/lib/api'
import { BAND_CSV_MIME, buildBandCsv } from '@/lib/bandCsv'
import { downloadText } from '@/lib/reportDownload'
import {
  bandTone,
  buildScorecards,
  calibrationBanner,
  correctnessLabel,
  critScoreTrend,
  evalBadge,
  externalTruthLabel,
  fmtRate,
  insufficientLabel,
  orderedStrata,
  isInsufficient,
  orderedDimensions,
  relTime,
  scoreBand,
  type AcuteTag,
  type BandTone,
  type CalibrationScoreboard,
  type CountryScorecard,
  type ExternalTruthPopulation,
  type ScorecardRow,
  type ScoreBand,
  type UnitCorrectnessBoard,
} from '@/lib/evalOps'
import type { PanelProps } from '@/types'
import { RecordLink } from '@/components/inspector/RecordLink'
import { ProvenanceStateBadge } from '@/components/ProvenanceBadge'
import { resolveNumberProvenance } from '@/lib/provenance'
import { humanizeId } from '@/lib/deskNames'
import { FAITHFULNESS_EXPLAIN } from '@/lib/verdictModel'
import { EvalBandCalibration } from './EvalBandCalibration'
import { EvalGraderRoster } from './EvalGraderRoster'
import type { GraderRoster } from '@/lib/graderRosterModel'
import { ScaleStamp } from '@/components/ScaleStamp'

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

  // M-1 — the OPERATOR gold-set correctness axis. Its OWN endpoint, not a
  // section of /eval/calibration: correctness is a human's read of whether a
  // finding was right; calibration and faithfulness are aggregates over the
  // pipeline's own judge, and the two are never pooled. A 404 while unwired
  // reads as "no correctness surface yet", never an error.
  const { data: correctness } = useQuery<UnitCorrectnessBoard | null>({
    queryKey: ['eval-correctness'],
    queryFn: async () => {
      try {
        return await apiGet<UnitCorrectnessBoard>('/v3/eval/correctness')
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null
        throw e
      }
    },
    refetchInterval: 60_000,
  })

  // c1 — the machine grader's ROSTER (correctness + coverage per desk over a
  // trailing 7-night window). Its own endpoint, never pooled with the operator
  // gold-set axis above: a 404 while unwired reads as "no grader rows yet",
  // never an error, and never a roster of zeros.
  const { data: graderRoster } = useQuery<GraderRoster | null>({
    queryKey: ['eval-grader-roster'],
    queryFn: async () => {
      try {
        return await apiGet<GraderRoster>('/v3/eval/grader_roster?nights=7')
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

  // W-6 rides the SAME route as the operator axis — one read, two
  // judge-independent axes, still never pooled: they are separate blocks on the
  // response and separate sections on the page.
  const truth = correctness?.external_truth ?? null
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

  /** 7b-iii — the banded section as CSV, built from the rows already on this
   *  page (`@/lib/bandCsv`): no second read, so the file and the section can
   *  never disagree. Every dimension travels, insufficient ones included. */
  function downloadBandCsv() {
    if (countryCards.length === 0) return
    const csv = buildBandCsv(countryCards)
    downloadText(csv.filename, BAND_CSV_MIME, csv.content)
  }

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
          probabilities). The whole section, its two honest absences and its
          per-horizon / per-direction / per-dimension tables live in
          `EvalBandCalibration` — see that module's banner for the four rules
          that make it honest rather than flattering. */}
      <EvalBandCalibration section={cal?.band_calibration} />

      {/* c1 — the machine GRADER's roster: correctness AND coverage for every
          graded desk, thinnest coverage first. Placed directly above the
          operator gold-set axis because the two share a word and nothing else:
          this one is graded against an independent reference built without
          seeing the platform, that one is a human's read of whether a finding
          was right. They are never pooled, and two adjacent sections is how
          the page says so. The whole section — its two labelled roster figures,
          its unmeasured-is-not-zero rule and its table — lives in
          `EvalGraderRoster`; see that module's banner for the four rules. */}
      <EvalGraderRoster roster={graderRoster} />

      {/* M-1 — the OPERATOR gold-set correctness axis, the platform's only
          JUDGE-INDEPENDENT quality signal. Deliberately its own block, beside
          calibration and never inside it: faithfulness asks "is the prose
          faithful to its cites?", correctness asks "was the read RIGHT?", and
          a finding can score high on the first while failing the second — which
          is exactly what the first gold-set round measured. Every figure here
          renders the server-composed `display` string verbatim, so the verdict
          mix and the sufficiency status always travel with the number. */}
      <div
        className="bg-surface-100 border border-slate-800 rounded p-2 mb-2 space-y-1.5 text-xs"
        data-testid="eval-operator-correctness"
      >
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-slate-500 text-[10px] uppercase tracking-wide">
            operator correctness
          </span>
          <InfoTip
            text={
              correctness?.honesty_note ??
              'Operator gold-set correctness is judge-independent and never pooled with faithfulness or calibration.'
            }
            className="text-slate-600 text-[10px]"
            testId="operator-correctness-explain"
          >
            gold-set verdicts (judge-independent, never pooled)
          </InfoTip>
        </div>
        {!correctness?.available ? (
          <div
            className="text-slate-500 text-[10px] py-1"
            data-testid="operator-correctness-empty"
          >
            no operator verdicts yet — the weekly gold-set worksheet has not been
            labelled
          </div>
        ) : (
          <>
            {correctness.fleet && (
              <div
                className="text-slate-300 font-mono"
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
                <span className="w-40 shrink-0 text-slate-400">
                  {humanizeId(row.unit)}
                </span>
                <span
                  className={
                    row.sufficient ? 'text-slate-200' : 'text-slate-400 italic'
                  }
                >
                  {correctnessLabel(row)}
                </span>
                {/* The CONTRAST that motivates this axis existing at all —
                    shown beside it, never averaged into it. */}
                {typeof row.faithfulness === 'number' && (
                  <span
                    className="text-slate-600 text-[10px]"
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
              className="text-slate-600 text-[10px] italic"
              data-testid="operator-correctness-honesty-note"
            >
              {correctness.honesty_note}
            </div>
          </>
        )}
      </div>

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
        className="bg-surface-100 border border-slate-800 rounded p-2 mb-2 space-y-1.5 text-xs"
        data-testid="eval-external-truth"
      >
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-slate-500 text-[10px] uppercase tracking-wide">
            standing external truth
          </span>
          <InfoTip
            text={
              truth?.honesty_note ??
              'Standing external truth grades the read\'s claims against sources outside this pipeline. It is never pooled with faithfulness, calibration or the operator gold set.'
            }
            className="text-slate-600 text-[10px]"
            testId="external-truth-explain"
          >
            graded against the world (web-grounded, never pooled)
          </InfoTip>
        </div>
        {!truth?.available ? (
          <div
            className="text-slate-500 text-[10px] py-1"
            data-testid="external-truth-empty"
          >
            no claims graded against the world yet — the standing auditor's ledger
            is empty for this window
          </div>
        ) : (
          <>
            {/* The PAIR. Two numbers, never one — the note says why. */}
            <div
              className="text-slate-300 font-mono"
              data-testid="external-truth-headline"
            >
              record {fmtRate(truth.headline?.record?.accuracy)} (n=
              {truth.headline?.record?.n_decided ?? 0}) · voice{' '}
              {fmtRate(truth.headline?.voice?.accuracy)} (n=
              {truth.headline?.voice?.n_decided ?? 0})
            </div>
            <div
              className="text-slate-600 text-[10px]"
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
                        ? 'text-slate-200'
                        : 'text-slate-400 italic'
                  }
                  data-testid={`external-truth-label-${pop.population}`}
                >
                  {externalTruthLabel(pop)}
                </div>
                <div className="text-slate-600 text-[10px] flex gap-3 flex-wrap">
                  <span data-testid={`external-truth-instrument-${pop.population}`}>
                    grader overlap {fmtRate(pop.instrument?.overlap_raw)} (n=
                    {pop.instrument?.overlap_n ?? 0}
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
                    className="flex items-baseline gap-2 flex-wrap text-[10px]"
                    data-testid={`external-truth-stratum-${pop.population}-${axis}`}
                  >
                    <span className="w-28 shrink-0 text-slate-500">{axis}</span>
                    {cells.length === 0 ? (
                      <span className="text-slate-600 italic">no rows yet</span>
                    ) : (
                      cells.map(([value, cell]) => (
                        <span key={value} className="text-slate-400">
                          {value}{' '}
                          <span className="font-mono text-slate-300">
                            {fmtRate(cell.accuracy)}
                          </span>
                          <span className="text-slate-600"> (n={cell.n_decided})</span>
                        </span>
                      ))
                    )}
                  </div>
                ))}
              </div>
            ))}
            <div
              className="text-slate-600 text-[10px] italic"
              data-testid="external-truth-honesty-note"
            >
              {truth.honesty_note}
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
        <div className="flex items-baseline gap-2">
          <span className="text-slate-500 text-[10px] uppercase tracking-wide">
            banded verdicts (per country)
          </span>
          <button
            type="button"
            onClick={downloadBandCsv}
            disabled={countryCards.length === 0}
            className="ml-auto flex items-center gap-1 rounded border border-slate-700 bg-surface-200 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-surface-300 disabled:opacity-40"
            title={
              countryCards.length === 0
                ? 'No scorecard computed yet — there is nothing to export'
                : 'Download these bands as CSV — one row per (desk, dimension), ' +
                  'built from the rows on this page'
            }
            data-testid="scorecard-csv"
          >
            <Download className="h-3 w-3" aria-hidden />
            download CSV
          </button>
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
                {/* H12/K3 — the instrument stamps this card's bands were
                    computed under. The scorecard's bands are a LADDER, not a
                    measure, so the card carries a method revision and no scale
                    of its own: the chip's method-only state is the honest
                    render, and a pre-H12 card reads "unstamped". The
                    generated-at beside it is this card's as-of. */}
                <ScaleStamp
                  stamp={{
                    scale: null,
                    method: sc.method_version ?? null,
                    state: sc.method_version ? 'method-only' : 'unstamped',
                  }}
                  testId={`scorecard-scale-stamp-${sc.target_id}`}
                />
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
