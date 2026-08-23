/**
 * Judge-stats model (GLASS-3) — the arithmetic behind the provider-drift read.
 *
 * All of it is pure so the honesty rules can be pinned without a DOM. The rules
 * are not stylistic: this panel exists because a provider change was measured to
 * flip 13.6% of judge verdicts, and an instrument built to detect a real 13.6%
 * effect must not be capable of inventing one.
 *
 * Three properties everything here holds to:
 *
 *   1. A statistic NEVER travels without its n. `formatMeasure` cannot render a
 *      number without one, and returns the honest-absence string when the count
 *      is zero — a mean over nothing is not 0.0, it is unmeasured.
 *   2. A DELTA between two providers is only reported when BOTH sides clear a
 *      minimum sample. Two means over nine verdicts each will differ by a lot,
 *      always, and reporting that as drift would bury the real signal under
 *      noise the operator cannot distinguish from it.
 *   3. Sentinel labels are never treated as providers. `(mixed)`, `(unrouted)`
 *      and `(no receipt)` are the three distinct ways attribution can fail, and
 *      folding any of them into a provider's numbers — or into a "provider
 *      count" — would attribute verdicts to a party that was never asked.
 */

import type {
  JudgeStatsCell,
  JudgeStatsProvider,
  JudgeStatsResponse,
} from '@/lib/api'

/** Rendered wherever a statistic has no sample behind it. */
export const UNMEASURED = 'unmeasured'

/**
 * Both sides of a comparison need at least this many scored verdicts before a
 * delta is reported as drift.
 *
 * The number is a judgement, and it is deliberately not 1. The effect this panel
 * is built to catch is ~13.6% of verdicts flipping; at n=10 the sampling noise on
 * a mean comfortably exceeds that, so a smaller floor would produce a steady
 * stream of "drift" that is nothing, and an operator who learns to ignore this
 * readout has lost the instrument. Below the floor the panel says how far it is
 * from being able to answer, which is a different and useful statement.
 */
export const MIN_COMPARABLE_N = 30

/** Render a statistic WITH the sample it came from, or say it is unmeasured. */
export function formatMeasure(
  value: number | null | undefined,
  n: number,
  opts: { digits?: number; percent?: boolean } = {},
): string {
  if (value == null || n <= 0) return UNMEASURED
  const digits = opts.digits ?? (opts.percent ? 1 : 3)
  const shown = opts.percent ? `${(value * 100).toFixed(digits)}%` : value.toFixed(digits)
  return `${shown} (n=${n})`
}

/** Providers that are real upstreams — the only rows a comparison may use. */
export function realProviders(rows: JudgeStatsProvider[]): JudgeStatsProvider[] {
  return rows.filter((p) => !p.is_sentinel)
}

/** The three attribution-failure buckets, kept separate from the real ones. */
export function sentinelRows(rows: JudgeStatsProvider[]): JudgeStatsProvider[] {
  return rows.filter((p) => p.is_sentinel)
}

export interface StatusSlice {
  status: string
  n: number
  /** Share of the provider's own total, or null when it has no verdicts. */
  share: number | null
}

/**
 * The verdict mix for one provider, in the server's declared status order so
 * two providers' bars are always read against the same axis. Statuses the
 * server declared but this provider never produced are included at zero —
 * an absent bucket and an empty one look identical otherwise.
 */
export function statusMix(
  provider: JudgeStatsProvider,
  statuses: string[],
): StatusSlice[] {
  const known = statuses.length > 0 ? statuses : Object.keys(provider.by_status)
  const extra = Object.keys(provider.by_status).filter((s) => !known.includes(s))
  return [...known, ...extra].map((status) => {
    const n = provider.by_status[status] ?? 0
    return { status, n, share: provider.n > 0 ? n / provider.n : null }
  })
}

export type DriftVerdict = 'drift' | 'steady' | 'insufficient' | 'single-provider'

export interface DriftReadout {
  verdict: DriftVerdict
  /** The two providers compared, highest-volume first. Empty when < 2 exist. */
  compared: string[]
  /** Absolute difference in faithfulness mean, null when not comparable. */
  faithfulnessDelta: number | null
  /** Absolute difference in adjudicated share, null when not comparable. */
  adjudicatedDelta: number | null
  /** Plain-language line the panel renders verbatim. */
  summary: string
}

/**
 * Compare the two highest-volume REAL providers.
 *
 * Returns a verdict rather than a bare number so the panel cannot render a
 * delta it has not earned. `insufficient` carries how short the sample is; it is
 * the expected state on a fresh window and must not read as "no drift".
 */
export function driftReadout(
  res: Pick<JudgeStatsResponse, 'providers'>,
  minN: number = MIN_COMPARABLE_N,
): DriftReadout {
  const real = realProviders(res.providers)
    .slice()
    .sort((a, b) => b.n - a.n)

  if (real.length < 2) {
    return {
      verdict: 'single-provider',
      compared: real.map((p) => p.served_by),
      faithfulnessDelta: null,
      adjudicatedDelta: null,
      summary:
        real.length === 0
          ? 'No verdict in this window could be attributed to a named serving provider — nothing to compare.'
          : `Every attributed verdict came from ${real[0].served_by}. Provider drift is not measurable against a single provider.`,
    }
  }

  const [a, b] = real
  const compared = [a.served_by, b.served_by]
  const thin = [a, b].filter((p) => p.faithfulness_n < minN)
  if (thin.length > 0) {
    const detail = thin
      .map((p) => `${p.served_by} has ${p.faithfulness_n} of ${minN}`)
      .join('; ')
    return {
      verdict: 'insufficient',
      compared,
      faithfulnessDelta: null,
      adjudicatedDelta: null,
      summary:
        `Not enough scored verdicts to compare ${a.served_by} against ` +
        `${b.served_by} yet — ${detail}. This is not a finding of "no drift".`,
    }
  }

  const fDelta =
    a.faithfulness_mean != null && b.faithfulness_mean != null
      ? Math.abs(a.faithfulness_mean - b.faithfulness_mean)
      : null
  const aDelta =
    a.adjudicated_share != null && b.adjudicated_share != null
      ? Math.abs(a.adjudicated_share - b.adjudicated_share)
      : null

  // The threshold is the measured effect this panel was built to surface.
  const drifting = fDelta != null && fDelta >= 0.05
  return {
    verdict: drifting ? 'drift' : 'steady',
    compared,
    faithfulnessDelta: fDelta,
    adjudicatedDelta: aDelta,
    summary: drifting
      ? `${a.served_by} and ${b.served_by} disagree: faithfulness means differ by ` +
        `${fDelta!.toFixed(3)} over n=${a.faithfulness_n} and n=${b.faithfulness_n}. ` +
        'Same judge, different upstream — treat scores from the two as separate populations.'
      : `${a.served_by} and ${b.served_by} agree within ${(fDelta ?? 0).toFixed(3)} ` +
        `over n=${a.faithfulness_n} and n=${b.faithfulness_n}.`,
  }
}

/** One day's row in the day x provider grid. */
export interface DayRow {
  day: string
  total: number
  /** provider label -> verdicts that day. Only providers present that day. */
  byProvider: Record<string, number>
}

/** Fold the cube's cells into a per-day view, newest day first. */
export function cellsByDay(cells: JudgeStatsCell[]): DayRow[] {
  const days = new Map<string, DayRow>()
  for (const c of cells) {
    let row = days.get(c.day)
    if (!row) {
      row = { day: c.day, total: 0, byProvider: {} }
      days.set(c.day, row)
    }
    row.total += c.n
    row.byProvider[c.served_by] = (row.byProvider[c.served_by] ?? 0) + c.n
  }
  return [...days.values()].sort((x, y) => (x.day < y.day ? 1 : x.day > y.day ? -1 : 0))
}

/**
 * The one-line panel subtitle.
 *
 * `measured: false` is the degraded read, and it gets said plainly — the server
 * returns an all-defaults payload at HTTP 200 rather than a 500, so a panel that
 * did not check this field would render a failed query as a quiet engine.
 */
export function summaryLine(res: JudgeStatsResponse): string {
  if (!res.measured) {
    return `could not read the judge ledger for the last ${res.window_days}d — this is a failed read, not a quiet judge`
  }
  const t = res.totals
  if (t.critiques === 0) {
    return `no faithfulness verdicts in the last ${res.window_days}d`
  }
  const attributed = `${t.attributed} of ${t.critiques} attributed to a named provider`
  const provs = `${t.providers} provider${t.providers === 1 ? '' : 's'}`
  return `${t.critiques} verdicts over ${res.window_days}d · ${attributed} · ${provs}`
}

/**
 * The pooled-stamp caveat, or null when the window sits inside one stamp.
 *
 * Pooling faithfulness across a judge-pipeline change averages two different
 * graders and can flatten a regression into a straight line — the reason the
 * server keeps `judge_pipeline_version` in the cube's grain at all.
 */
export function pipelineCaveat(res: JudgeStatsResponse): string | null {
  if (!res.pools_across_pipeline_versions) return null
  const stamps = res.pipeline_versions.map((v) => v.judge_pipeline_version).join(' · ')
  return (
    `This window spans ${res.pipeline_versions.length} judge-pipeline stamps ` +
    `(${stamps}). The pooled mean below averages across a judge change — read ` +
    'the per-stamp rows, not the total.'
  )
}

/** Look up a sentinel's server-supplied meaning; null for a real provider. */
export function sentinelMeaning(
  label: string,
  sentinels: Record<string, string>,
): string | null {
  return sentinels[label] ?? null
}
