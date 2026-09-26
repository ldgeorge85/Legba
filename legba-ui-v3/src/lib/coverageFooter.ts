/**
 * coverageFooter — the status bar's coverage line, as four figures.
 *
 * ## What replaced what
 *
 * The status bar used to state the CONSOLE's health: the deployment mode, how
 * many panel kinds are registered, when the registry list last refreshed. None
 * of that is about the intelligence. World Monitor's footer says
 * "Digest coverage: complete — 120 publishers, 287 items, feeds 235/245,
 * categories 17/17" on every page, and that sentence is the one piece of their
 * chrome worth taking: a standing, unprompted statement of how much of the
 * thing the reader is reading was actually covered.
 *
 * Folded onto our objects it is four figures, each over one of ours:
 *
 *   * **sources** — the firing matrix over the wired roster
 *     (`/v3/system/source-firing`): how many registered sources are firing, and
 *     how their freshness grades against each source's OWN declared cadence.
 *   * **gaps** — the TYPED ABSENCE count for the ACTIVE scope
 *     (`/v3/absence?scope=`): not "nothing was shown", but how many named,
 *     proof-carrying absences this desk has on record.
 *   * **judge** — the faithfulness mean and its denominator
 *     (`/v3/system/judge-stats`).
 *   * **grader** — how much of the desk roster the independent grader reached
 *     (`/v3/eval/grader_roster`).
 *
 * ## The four rules every figure keeps
 *
 *   1. **A number carries its unit and its denominator.** `38/57 firing` never
 *      renders as `38`, and a share never renders without what it is a share
 *      OF. The compact value is the headline; the full sentence — absolute
 *      as-of, every denominator, every caveat — is the figure's `title`.
 *   2. **An absent reading prints {@link UNMEASURED}, never `0`.** A route that
 *      did not answer, a mean with no samples behind it, a roster that has
 *      never run: all of those are the word, and `unmeasured: true` says so to
 *      the renderer as well as to the reader.
 *   3. **A measured zero is a zero.** `/v3/absence` reading a desk and finding
 *      no typed absence is a real answer and prints `0 typed`. Collapsing it
 *      into "unmeasured" would throw away the very distinction typed absence
 *      exists to make.
 *   4. **Nothing is derived that a route did not carry.** Every count here is
 *      the length of a list the server sent or a field it stamped. There is no
 *      second statistic, no rollup across routes, and no figure that would
 *      still render if its route went away.
 *
 * Pure — no fetch, no React. The inputs are typed as the wire shapes but every
 * accessor is guarded, because a stubbed or half-migrated route can hand back
 * something that is not the shape its type claims, and a footer that throws
 * takes the whole shell's chrome with it.
 */

import type { PanelKind } from '@/types'
import type { JudgeStatsResponse, SourceFiringRow } from '@/lib/api'
import type { GraderRoster } from '@/lib/graderRosterModel'
import type { AbsenceResponse } from '@/lib/absenceModel'
import { relativeTime } from '@/lib/findingsViews'

/** What a figure with no measurement behind it prints. Never `0`, never blank. */
export const UNMEASURED = 'unmeasured'

export type CoverageFigureId = 'sources' | 'absence' | 'judge' | 'grader'

export interface CoverageFigure {
  id: CoverageFigureId
  /** The short noun in front of the value. */
  label: string
  /** The figure WITH its unit, or {@link UNMEASURED}. */
  value: string
  /** The as-of, relative — null when the reading states no instant of its own. */
  asOf: string | null
  /** The whole sentence: units, denominators, absolute as-of, and why, if absent. */
  title: string
  /** True when no measurement stands behind `value`. */
  unmeasured: boolean
}

/**
 * The panel that OWNS each figure — one click on a figure opens it.
 *
 * A figure the reader cannot drill into is a number with no way to argue with
 * it, so every id here resolves to a real panel kind (pinned by
 * `coverageFooter.test.ts` against the panel registry).
 */
export const COVERAGE_FIGURE_PANEL: Record<CoverageFigureId, PanelKind> = {
  sources: 'system.source_health',
  // The GAPS band on the Morning Read is the surface that reads
  // `/v3/absence?scope=` per desk and renders each absence's kind, proof and
  // clock — the same read this figure counts.
  absence: 'v4.morning_read',
  judge: 'system.judge_stats',
  grader: 'system.eval_scorecard',
}

const NUM = new Intl.NumberFormat('en-US')

/** `relativeTime`, but null rather than an echo of an unparseable string. */
function ago(iso: string | null | undefined, now: number): string | null {
  if (!iso || typeof iso !== 'string') return null
  if (!Number.isFinite(Date.parse(iso))) return null
  return relativeTime(iso, now)
}

/** A figure that has nothing behind it, with the reason it has nothing. */
function absent(id: CoverageFigureId, label: string, why: string): CoverageFigure {
  return { id, label, value: UNMEASURED, asOf: null, title: why, unmeasured: true }
}

// ---------------------------------------------------------------------------
// sources — the firing matrix over the wired roster.
// ---------------------------------------------------------------------------

/**
 * `GET /v3/system/source-firing` folded to one figure.
 *
 * `status` and `freshness_grade` are counted SEPARATELY and never merged: a
 * source can be firing and still be past the budget derived from its own
 * declared cadence, and `ungraded` / `empty` are absences of a grade rather
 * than bad grades — folding either into "stale" would invent a fault.
 */
export function sourcesFigure(
  rows: SourceFiringRow[] | null | undefined,
  failed: boolean,
  now: number = Date.now(),
): CoverageFigure {
  if (failed) {
    return absent('sources', 'sources', 'the source-firing matrix could not be read — /v3/system/source-firing did not answer, so there is no coverage reading to give')
  }
  if (!Array.isArray(rows)) {
    return absent('sources', 'sources', 'the source-firing matrix has not been read yet — /v3/system/source-firing')
  }
  if (rows.length === 0) {
    return absent('sources', 'sources', 'no source is wired here — the firing matrix is empty, which is a roster with nothing in it rather than a roster firing at zero')
  }
  const wired = rows.length
  let firing = 0
  let silent = 0
  let errored = 0
  let paused = 0
  let ok = 0
  let stale = 0
  let warn = 0
  let ungraded = 0
  let empty = 0
  let freshest: number | null = null
  for (const r of rows) {
    if (r?.status === 'firing') firing++
    else if (r?.status === 'silent') silent++
    else if (r?.status === 'error') errored++
    else if (r?.status === 'paused') paused++
    if (r?.freshness_grade === 'ok') ok++
    else if (r?.freshness_grade === 'stale') stale++
    else if (r?.freshness_grade === 'warn') warn++
    else if (r?.freshness_grade === 'empty') empty++
    else ungraded++
    const t = r?.last_seen_at ? Date.parse(r.last_seen_at) : NaN
    if (Number.isFinite(t) && (freshest == null || t > freshest)) freshest = t
  }
  const freshIso = freshest != null ? new Date(freshest).toISOString() : null
  return {
    id: 'sources',
    label: 'sources',
    value: `${NUM.format(firing)}/${NUM.format(wired)} firing`,
    asOf: ago(freshIso, now),
    title:
      `${NUM.format(firing)} firing · ${NUM.format(silent)} silent · ` +
      `${NUM.format(errored)} in error · ${NUM.format(paused)} paused, ` +
      `of ${NUM.format(wired)} wired sources. ` +
      `Freshness against each source's own declared cadence: ` +
      `${NUM.format(ok)} ok · ${NUM.format(warn)} warn · ${NUM.format(stale)} stale; ` +
      `${NUM.format(empty)} with no signal to grade and ${NUM.format(ungraded)} ungraded — ` +
      `neither is a stale source. ` +
      (freshIso
        ? `Freshest signal ${freshIso}.`
        : 'No source carries a signal instant, so this figure has no as-of.'),
    unmeasured: false,
  }
}

// ---------------------------------------------------------------------------
// gaps — typed absence for the ACTIVE scope.
// ---------------------------------------------------------------------------

export interface AbsenceFigureInput {
  /** The desk `/v3/absence` was asked about, or null when none is in scope. */
  scopeTargetId: string | null
  /** The scope's human label, for the figure's own caption. */
  scopeLabel?: string | null
  res: AbsenceResponse | null | undefined
  failed: boolean
}

/**
 * `GET /v3/absence?scope=<desk>` folded to one figure.
 *
 * The route answers ONE desk at a time, so a world scope (or no scope) has no
 * count to give — and says that, rather than showing a zero that would read as
 * "this desk has no gaps". A desk that WAS read and carries no typed absence
 * prints `0 typed`: a measured zero, which is the whole point of the
 * vocabulary.
 */
export function absenceFigure(
  input: AbsenceFigureInput,
  now: number = Date.now(),
): CoverageFigure {
  const { scopeTargetId, res, failed } = input
  if (!scopeTargetId) {
    return absent(
      'absence',
      'gaps',
      'no single desk is in scope — /v3/absence answers one desk at a time, so there is no typed-absence count to give here. Scope a desk and this figure fills in.',
    )
  }
  if (failed) {
    return absent(
      'absence',
      'gaps',
      `the typed-absence read for ${scopeTargetId} failed — /v3/absence?scope=${scopeTargetId} did not answer`,
    )
  }
  if (!res || !Array.isArray(res.absences)) {
    return absent(
      'absence',
      'gaps',
      `the typed absences for ${scopeTargetId} have not been read yet — /v3/absence?scope=${scopeTargetId}`,
    )
  }
  const n = res.absences.length
  const staleN = res.absences.filter((a) => a?.stale === true).length
  const notMeasured = Array.isArray(res.not_measured) ? res.not_measured.length : 0
  const label = input.scopeLabel || scopeTargetId
  return {
    id: 'absence',
    label: 'gaps',
    value: `${NUM.format(n)} typed`,
    asOf: ago(res.read_at, now),
    title:
      `${NUM.format(n)} typed absence${n === 1 ? '' : 's'} on ${label}, each with the proof of ` +
      `what was checked and the clock saying whether anyone has looked since. ` +
      (staleN > 0
        ? `${NUM.format(staleN)} of them are last known and NOT re-checked. `
        : 'None of them is past its re-check. ') +
      (notMeasured > 0
        ? `${NUM.format(notMeasured)} absence kind${notMeasured === 1 ? '' : 's'} could not be read for this scope at all. `
        : 'Every absence kind was read for this scope. ') +
      `Read at ${res.read_at}.`,
    unmeasured: false,
  }
}

// ---------------------------------------------------------------------------
// judge — faithfulness and its denominator.
// ---------------------------------------------------------------------------

/**
 * `GET /v3/system/judge-stats` folded to one figure.
 *
 * `measured: false` is a FAILED READ and says so — it is not a quiet judge.
 * A null `faithfulness_mean` is the word, never `0.00`, and the mean never
 * renders without the `n` it was taken over.
 */
export function judgeFigure(
  res: JudgeStatsResponse | null | undefined,
  failed: boolean,
  now: number = Date.now(),
): CoverageFigure {
  if (failed) {
    return absent('judge', 'judge', 'the judge stats could not be read — /v3/system/judge-stats did not answer')
  }
  if (!res || typeof res !== 'object' || !res.totals || typeof res.totals !== 'object') {
    return absent('judge', 'judge', 'the judge stats have not been read yet — /v3/system/judge-stats')
  }
  if (res.measured === false) {
    return absent('judge', 'judge', 'the judge-stats read itself failed — this is a failed read, not a quiet judge')
  }
  const t = res.totals
  const mean = typeof t.faithfulness_mean === 'number' ? t.faithfulness_mean : null
  const window = typeof res.window_days === 'number' ? `${res.window_days}d` : 'an unstated window'
  const stamp = typeof res.generated_at === 'string' ? res.generated_at : null
  if (mean == null) {
    return absent(
      'judge',
      'judge',
      `no critique in the ${window} window carries a faithfulness score, so there is no mean to state — ` +
        `${NUM.format(t.critiques ?? 0)} critiques were seen. Generated ${stamp ?? 'at an unstated time'}.`,
    )
  }
  return {
    id: 'judge',
    label: 'judge',
    value: `${mean.toFixed(2)} faithfulness`,
    asOf: ago(stamp, now),
    title:
      `Mean faithfulness ${mean.toFixed(4)} over ${NUM.format(t.faithfulness_n ?? 0)} scored ` +
      `critiques in a ${window} window (${NUM.format(t.critiques ?? 0)} critiques total; ` +
      `${NUM.format(t.attributed ?? 0)} attributed to a named provider, ` +
      `${NUM.format(t.unattributed ?? 0)} could not be attributed). ` +
      (res.pools_across_pipeline_versions
        ? 'The window straddles a judge-pipeline change, so this mean pools two different graders. '
        : '') +
      `Generated ${stamp ?? 'at an unstated time'}.`,
    unmeasured: false,
  }
}

// ---------------------------------------------------------------------------
// grader — how much of the roster the independent grader reached.
// ---------------------------------------------------------------------------

/**
 * `GET /v3/eval/grader_roster` folded to one figure.
 *
 * The figure is COVERAGE OF THE ROSTER, not correctness: how many desks the
 * independent reference reached at all. Correctness rides in the title beside
 * its own coverage share, because neither is meaningful alone — and a desk the
 * reference decided nothing about is `unmeasured`, never a zero, so it is
 * counted in the denominator and excluded from the mean, exactly as the roster
 * itself does.
 */
export function graderFigure(
  res: GraderRoster | null | undefined,
  failed: boolean,
  now: number = Date.now(),
): CoverageFigure {
  if (failed) {
    return absent('grader', 'grader', 'the grader roster could not be read — /v3/eval/grader_roster did not answer')
  }
  if (!res || typeof res !== 'object' || !res.roster || typeof res.roster !== 'object') {
    return absent('grader', 'grader', 'the grader roster has not been read yet — /v3/eval/grader_roster')
  }
  if (res.available === false) {
    return absent('grader', 'grader', 'the grader has not produced a graded night yet — an empty roster, not a roster of zeros')
  }
  const graded = res.roster.desks_graded ?? 0
  const unmeasured = res.roster.desks_unmeasured ?? 0
  const total = graded + unmeasured
  const correctness = res.roster.correctness_mean_of_desks
  const coverage = res.roster.coverage_mean_of_desks
  const pct = (v: number | null | undefined) =>
    typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : UNMEASURED
  return {
    id: 'grader',
    label: 'grader',
    value: `${NUM.format(graded)}/${NUM.format(total)} desks graded`,
    asOf: ago(res.as_of, now),
    title:
      `${NUM.format(graded)} of ${NUM.format(total)} ${res.grain ?? 'desk'}-grain rows were graded ` +
      `over ${NUM.format(res.nights ?? 0)} night${res.nights === 1 ? '' : 's'}; ` +
      `${NUM.format(unmeasured)} are unmeasured — the reference decided nothing they said, ` +
      `which is not a zero and is left out of both means. ` +
      `Mean of desks: correctness ${pct(correctness)} at coverage ${pct(coverage)} — ` +
      `read them together, a high correctness at a thin coverage is a handful of claims confirmed. ` +
      `As of ${res.as_of ?? 'an unstated time'}.`,
    unmeasured: false,
  }
}
