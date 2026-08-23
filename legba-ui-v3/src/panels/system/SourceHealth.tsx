/**
 * Source Health (`system.source_health`) — the source-quality ROLLUP.
 *
 * Three server surfaces that had no UI consumer at all, folded into one panel:
 *
 *   1. `/v3/system/staleness-debt` — the debt strip. How many consumer flags
 *      are open because a foundation moved under them, and why.
 *   2. `/v3/source-quality`        — one row per source: what it ASSERTS, what
 *      it EARNED, and what we COMPUTED about its freshness.
 *   3. `/v3/sources/{id}/quality`  — the per-source drill-down (ratings +
 *      dossier), fetched LAZILY: only the expanded source is requested.
 *
 * The whole design is one distinction: ASSERTED vs EARNED. They occupy two
 * separately-headed column groups with a hard rule between them and they are
 * never combined into a "quality score", because an admiralty grade is
 * testimony (someone's claim about a source) and a contested-claim record is
 * evidence (what happened when the claim met another one). A source can assert
 * A1 and have lost every contest it entered; a panel that averaged the two
 * would hide exactly the case worth seeing.
 *
 * The rest follows from that:
 *   * Every rate carries its n and the name of the field it came from. The
 *     headline is `win_rate_smoothed` with `win_rate_lower` beside it; the raw
 *     rate is demoted to secondary because it is the one that flatters n=2.
 *   * `earned: null` (no track-record row — nothing measured) renders
 *     differently from `contested_total: 0` (a row that has never been
 *     contested). Neither renders as a zero.
 *   * `low_sample` is a loud amber flag, not a footnote.
 *   * `empty` / `ungraded` freshness read MUTED — they are absences of a grade,
 *     not failing grades (tone comes from `lib/sourceFreshness`).
 *   * `match_verified` is false on the wire today, so the debt numbers carry a
 *     caveat rather than a checkmark.
 *   * A 503 from `/v3/source-quality` means migration 0115's view is absent —
 *     rendered as "not provisioned", never as a verdict about the sources.
 *
 * All of that logic lives in `lib/sourceHealth.ts` and is unit-tested without a
 * DOM; this file is rendering.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { cn } from '@/lib/cn'
import { selectRow } from '@/state/selection'
import {
  fetchSourceQuality,
  fetchSourceQualityDetail,
  fetchStalenessDebt,
} from '@/lib/api'
import type { SourceQualityDetail, SourceQualityRow, StalenessDebtResponse } from '@/lib/api'
import { freshnessTitle, freshnessTone } from '@/lib/sourceFreshness'
import type { FreshnessTone } from '@/lib/sourceFreshness'
import {
  ABSENT,
  ATTENTION_FLAG_LABEL,
  ATTENTION_FLAG_TITLE,
  EARNED_STATE_LABEL,
  EARNED_STATE_TITLE,
  SOURCE_SORTS,
  apiErrorDetail,
  assertedGrade,
  assertedSummary,
  assertedVsEarned,
  attentionFlags,
  classifyQualityError,
  corroborationDisplay,
  describeRating,
  earnedRecordState,
  earnedSummary,
  formatPercent,
  isFreshnessAbsence,
  lastSignalText,
  matchVerifiedCaveat,
  openWindowText,
  reasonBreakdown,
  signalVolumeText,
  sortRatings,
  sortSourceQuality,
  stalenessHeadline,
  winRateDisplay,
  winRateLowerDisplay,
  winRateRawDisplay,
} from '@/lib/sourceHealth'
import type { AttentionFlag, EarnedRecordState, RateText, SourceSort } from '@/lib/sourceHealth'
import type { PanelProps } from '@/types'

/** Freshness tone → classes. `muted` is deliberately colourless: `empty` and
 *  `ungraded` are absences of a grade, and painting them rose would report a
 *  fault the server never claimed. */
const FRESHNESS_PILL: Record<FreshnessTone, string> = {
  ok: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300',
  watch: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
  bad: 'border-rose-500/40 bg-rose-500/15 text-rose-300',
  muted: 'border-line bg-surf-2 text-ink-3',
}

/**
 * Record-state tone. Note what is NOT here: `measured` is not green. Being
 * measured is not being good — the goodness, if any, is in the rate below it.
 * The two absences are distinguished by shape (dashed = no row at all) as well
 * as by text, so they never read as the same thing.
 */
const EARNED_STATE_PILL: Record<EarnedRecordState, string> = {
  measured: 'border-line-strong bg-surf-3 text-ink-1',
  'low-sample': 'border-amber-500/40 bg-amber-500/15 text-amber-300',
  'never-contested': 'border-line bg-surf-2 text-ink-3',
  'no-record': 'border-dashed border-line-strong bg-surf-base text-ink-3',
}

const FLAG_PILL: Record<AttentionFlag, string> = {
  losing_contests: 'border-rose-500/40 bg-rose-500/15 text-rose-300',
  asserted_unbacked: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
  low_sample: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
  overdue: 'border-amber-500/40 bg-amber-500/15 text-amber-300',
  never_contested: 'border-line bg-surf-2 text-ink-3',
  no_track_record: 'border-dashed border-line-strong bg-surf-base text-ink-3',
}

export default function SourceHealthPanel({ registration }: PanelProps) {
  const [sort, setSort] = useState<SourceSort>('attention')
  const [contestedOnly, setContestedOnly] = useState(false)
  const [includePrivate, setIncludePrivate] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)

  const debt = useQuery({
    queryKey: ['staleness-debt'],
    queryFn: () => fetchStalenessDebt(),
    refetchInterval: 60_000,
  })

  const quality = useQuery({
    queryKey: ['source-quality', contestedOnly],
    queryFn: () => fetchSourceQuality({ contestedOnly, limit: 200 }),
    refetchInterval: 60_000,
  })

  // Lazy by construction: the drill-down is only requested for the row the
  // operator opened, and re-requested when the private-ratings scope changes.
  const detail = useQuery({
    queryKey: ['source-quality-detail', expanded, includePrivate],
    queryFn: () => fetchSourceQualityDetail(expanded as string, { includePrivate }),
    enabled: expanded != null,
    refetchInterval: 60_000,
  })

  const rows = useMemo(
    () => sortSourceQuality(quality.data ?? [], sort),
    [quality.data, sort],
  )

  const fault = classifyQualityError(quality.error)

  const subtitle = fault
    ? fault.kind === 'not_provisioned'
      ? 'the source_quality view is not provisioned here'
      : 'the source-quality rollup could not be read'
    : `${rows.length} sources · asserted ≠ earned · ${debt.data ? `${debt.data.open_flags} open staleness flags (unverified)` : 'staleness debt loading'}`

  return (
    <PanelChrome
      registration={registration}
      subtitle={subtitle}
      onRefresh={() => {
        void quality.refetch()
        void debt.refetch()
        if (expanded != null) void detail.refetch()
      }}
      actions={
        <div className="flex items-center gap-1" data-testid="source-health-actions">
          {SOURCE_SORTS.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setSort(s.id)}
              data-testid={`source-health-sort-${s.id}`}
              className={cn(
                'rounded border px-2 py-0.5 text-label',
                sort === s.id
                  ? 'border-line-strong bg-surf-3 text-ink-1'
                  : 'border-line text-ink-3 hover:text-ink-1',
              )}
            >
              {s.label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setContestedOnly((v) => !v)}
            data-testid="source-health-contested-only"
            title="Only sources that have entered at least one contested claim."
            className={cn(
              'rounded border px-2 py-0.5 text-label',
              contestedOnly
                ? 'border-line-strong bg-surf-3 text-ink-1'
                : 'border-line text-ink-3 hover:text-ink-1',
            )}
          >
            contested only
          </button>
        </div>
      }
    >
      <div className="space-y-3">
        <StalenessStrip
          debt={debt.data}
          isLoading={debt.isLoading}
          error={debt.error}
        />

        {quality.isLoading && (
          <div className="text-body text-ink-3" data-testid="source-health-loading">
            reading the source-quality rollup…
          </div>
        )}

        {fault?.kind === 'not_provisioned' && (
          <div
            className="rounded border border-line-strong bg-surf-1 p-2 text-body text-ink-2"
            data-testid="source-health-not-provisioned"
          >
            <div className="text-label uppercase tracking-wider text-ink-3">
              view not provisioned
            </div>
            <p className="mt-1">{fault.text}</p>
            <p className="mt-1 text-label text-ink-3">
              server said: <span className="font-mono">{fault.detail}</span>
            </p>
          </div>
        )}

        {fault?.kind === 'error' && (
          <div
            className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
            data-testid="source-health-error"
          >
            {fault.text}
          </div>
        )}

        {!quality.isLoading && fault == null && rows.length === 0 && (
          <div className="text-body text-ink-3" data-testid="source-health-empty">
            {contestedOnly
              ? 'No source has entered a contested claim yet — that is an absence of contests, not a clean record.'
              : 'The source-quality view returned no rows.'}
          </div>
        )}

        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse" data-testid="source-health-table">
              <thead>
                <tr className="border-b border-line">
                  <th
                    rowSpan={2}
                    className="px-2 py-1 text-left align-bottom text-label uppercase tracking-wider text-ink-3"
                  >
                    source
                  </th>
                  <th
                    colSpan={2}
                    className="border-l border-line-strong bg-surf-1 px-2 py-1 text-left text-label uppercase tracking-wider text-ink-3"
                    data-testid="source-health-asserted-header"
                    title="Claims about this source — an admiralty grade, a compiled dossier, a host score. Testimony, not evidence."
                  >
                    asserted · claimed, not evidence
                  </th>
                  <th
                    colSpan={3}
                    className="border-l border-line-strong px-2 py-1 text-left text-label uppercase tracking-wider text-ink-1"
                    data-testid="source-health-earned-header"
                    title="What this source actually earned — its measured contested-claim track record."
                  >
                    earned · measured track record
                  </th>
                  <th
                    colSpan={2}
                    rowSpan={1}
                    className="border-l border-line-strong px-2 py-1 text-left text-label uppercase tracking-wider text-ink-3"
                  >
                    computed
                  </th>
                </tr>
                <tr className="border-b border-line text-label uppercase tracking-wider text-ink-3">
                  <th className="border-l border-line-strong bg-surf-1 px-2 py-1 text-left">
                    grade
                  </th>
                  <th className="bg-surf-1 px-2 py-1 text-left">ratings / dossier</th>
                  <th className="border-l border-line-strong px-2 py-1 text-left">record</th>
                  <th className="px-2 py-1 text-left">win rate (smoothed)</th>
                  <th className="px-2 py-1 text-left">corroboration</th>
                  <th className="border-l border-line-strong px-2 py-1 text-left">freshness</th>
                  <th className="px-2 py-1 text-left">flags</th>
                </tr>
              </thead>
              {rows.map((row) => (
                <SourceRows
                  key={row.source_id}
                  row={row}
                  expanded={expanded === row.source_id}
                  includePrivate={includePrivate}
                  onToggleIncludePrivate={() => setIncludePrivate((v) => !v)}
                  detail={expanded === row.source_id ? detail.data : undefined}
                  detailLoading={expanded === row.source_id && detail.isLoading}
                  detailError={expanded === row.source_id ? detail.error : null}
                  onToggle={() => {
                    const next = expanded === row.source_id ? null : row.source_id
                    setExpanded(next)
                    if (next) {
                      selectRow('source', row.source_id, row.source_id, {
                        origin: 'source_health',
                      })
                    }
                  }}
                />
              ))}
            </table>
          </div>
        )}
      </div>
    </PanelChrome>
  )
}

// ---------------------------------------------------------------------------
// Region 1 — the staleness-debt strip.
// ---------------------------------------------------------------------------

function StalenessStrip({
  debt,
  isLoading,
  error,
}: {
  debt: StalenessDebtResponse | undefined
  isLoading: boolean
  error: unknown
}) {
  if (isLoading) {
    return (
      <div className="text-body text-ink-3" data-testid="source-health-debt-loading">
        reading staleness debt…
      </div>
    )
  }
  if (error != null) {
    return (
      <div
        className="rounded border border-rose-500/40 bg-rose-500/10 p-2 text-body text-rose-300"
        data-testid="source-health-debt-error"
      >
        Could not read staleness debt — {apiErrorDetail(error)}
      </div>
    )
  }
  if (!debt) return null

  const caveat = matchVerifiedCaveat(debt)
  const breakdown = reasonBreakdown(debt)

  return (
    <section
      className="rounded border border-line bg-surf-1 pad-density"
      data-testid="source-health-debt"
    >
      <div className="text-label uppercase tracking-wider text-ink-3">
        staleness debt · consumers flagged by a foundation that moved
      </div>

      {caveat && (
        <div
          className="mt-1 flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 p-1.5 text-body text-amber-300"
          data-testid="source-health-debt-caveat"
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>{caveat}</span>
        </div>
      )}

      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1" data-testid="source-health-debt-stats">
        <Stat label="staleness_debt" value={debt.staleness_debt} />
        <Stat label="open_flags" value={debt.open_flags} />
        <Stat label="flagged_consumers" value={debt.flagged_consumers} />
        <Stat label="moved_foundations" value={debt.moved_foundations} />
        <Stat label="superseded_consumer_flags" value={debt.superseded_consumer_flags} />
        <Stat label="closed_flags" value={debt.closed_flags} />
      </div>

      <div className="mt-1 text-label text-ink-3" data-testid="source-health-debt-window">
        {stalenessHeadline(debt)} · {openWindowText(debt)}
      </div>

      <div className="mt-1.5">
        <div className="text-label uppercase tracking-wider text-ink-3">
          by reason ({breakdown.rows.length}
          {breakdown.truncated ? ', capped' : ''})
        </div>
        {breakdown.rows.length === 0 ? (
          <div className="text-body text-ink-3" data-testid="source-health-debt-reasons-empty">
            no open flags to attribute
          </div>
        ) : (
          <div className="mt-0.5 flex flex-wrap gap-1" data-testid="source-health-debt-reasons">
            {breakdown.rows.map((r) => (
              <span
                key={r.reason}
                className="rounded border border-line bg-surf-2 px-1.5 py-0.5 text-label text-ink-2"
                data-testid={`source-health-debt-reason-${r.reason}`}
                title={`${r.open_flags} open flag(s) attributed to ${r.reason}`}
              >
                <span className="font-mono">{r.reason}</span>{' '}
                <span className="text-ink-1">{r.open_flags}</span>
                {r.share != null && (
                  <span className="text-ink-3"> · {formatPercent(r.share)}</span>
                )}
              </span>
            ))}
          </div>
        )}
        {breakdown.truncated && (
          <div className="mt-0.5 text-label text-amber-300" data-testid="source-health-debt-uncounted">
            {breakdown.uncounted} open flag(s) are not explained by this breakdown — the server
            caps `by_reason` at 50 rows.
          </div>
        )}
      </div>
    </section>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="text-label text-ink-3">{label}</div>
      <div className="text-heading font-semibold text-ink-1" data-testid={`source-health-stat-${label}`}>
        {value}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Region 2 + 3 — one source's row, and its lazy drill-down.
// ---------------------------------------------------------------------------

function SourceRows({
  row,
  expanded,
  includePrivate,
  onToggleIncludePrivate,
  detail,
  detailLoading,
  detailError,
  onToggle,
}: {
  row: SourceQualityRow
  expanded: boolean
  includePrivate: boolean
  onToggleIncludePrivate: () => void
  detail: SourceQualityDetail | undefined
  detailLoading: boolean
  detailError: unknown
  onToggle: () => void
}) {
  const id = row.source_id
  const state = earnedRecordState(row.earned)
  const grade = assertedGrade(row.asserted)
  const flags = attentionFlags(row)
  const tone = freshnessTone(row.computed.freshness_grade)
  const smoothed = winRateDisplay(row.earned)
  const lower = winRateLowerDisplay(row.earned)
  const raw = winRateRawDisplay(row.earned)
  const corr = corroborationDisplay(row.earned)

  return (
    <tbody className="border-b border-line align-top">
      <tr className="hover:bg-surf-1" data-testid={`source-health-row-${id}`}>
        <td className="px-2 py-1.5">
          <button
            type="button"
            onClick={onToggle}
            data-testid={`source-health-toggle-${id}`}
            className="flex items-start gap-1.5 text-left"
          >
            {expanded ? (
              <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
            ) : (
              <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
            )}
            <span className="min-w-0">
              <span className="block font-mono text-body text-ink-1">{id}</span>
              <span className="block text-label text-ink-3">
                {row.registered ? 'registered' : 'NOT registered'}
                {row.declared_kind ? ` · ${row.declared_kind}` : ''}
                {row.declared_state ? ` · ${row.declared_state}` : ''}
              </span>
              {row.endpoint_host && (
                <span className="block font-mono text-label text-ink-3">{row.endpoint_host}</span>
              )}
            </span>
          </button>
        </td>

        {/* ---- ASSERTED: a claim. Muted on purpose — it is not evidence. ---- */}
        <td
          className="border-l border-line-strong bg-surf-1 px-2 py-1.5"
          data-testid={`source-health-asserted-${id}`}
        >
          {grade ? (
            <span
              className="rounded border border-line bg-surf-2 px-1.5 py-0.5 text-label text-ink-2"
              data-testid={`source-health-asserted-grade-${id}`}
              title={assertedSummary(row.asserted)}
            >
              claims {grade}
            </span>
          ) : (
            <span className="text-label text-ink-3" data-testid={`source-health-asserted-grade-${id}`}>
              no grade asserted
            </span>
          )}
          {row.asserted.admiralty_rater && (
            <div className="mt-0.5 text-label text-ink-3">
              rater <span className="font-mono">{row.asserted.admiralty_rater}</span>
            </div>
          )}
        </td>
        <td className="bg-surf-1 px-2 py-1.5 text-label text-ink-3">
          <div>
            {row.asserted.public_rating_count} public / {row.asserted.private_rating_count} private
          </div>
          <div>
            {row.asserted.has_dossier ? 'dossier on file' : 'no dossier'}
            {row.asserted.host_tier ? ` · host ${row.asserted.host_tier}` : ''}
            {row.asserted.host_score != null ? ` ${formatPercent(row.asserted.host_score)}` : ''}
          </div>
          {row.asserted.host_state_affiliation === true && (
            <div className="text-amber-300">state-affiliated host</div>
          )}
        </td>

        {/* ---- EARNED: what was measured. ---- */}
        <td
          className="border-l border-line-strong px-2 py-1.5"
          data-testid={`source-health-earned-${id}`}
        >
          <span
            className={cn('rounded border px-1.5 py-0.5 text-label', EARNED_STATE_PILL[state])}
            data-testid={`source-health-earned-state-${id}`}
            title={EARNED_STATE_TITLE[state]}
          >
            {EARNED_STATE_LABEL[state]}
          </span>
          {row.earned ? (
            <div className="mt-0.5 text-label text-ink-3">
              {row.earned.wins}W / {row.earned.losses}L · n={row.earned.contested_total}
            </div>
          ) : (
            <div className="mt-0.5 text-label text-ink-3">nothing measured</div>
          )}
        </td>

        <td className="px-2 py-1.5" data-testid={`source-health-winrate-${id}`}>
          {smoothed == null || lower == null || raw == null ? (
            <span className="text-label text-ink-3">
              no track-record row — no rate exists to show
            </span>
          ) : row.earned && row.earned.contested_total === 0 ? (
            <span className="text-label text-ink-3">
              never contested ({ABSENT} over n=0) — no rate is computable
            </span>
          ) : (
            <>
              <div className="text-body text-ink-1">
                <span data-testid={`source-health-winrate-value-${id}`}>{smoothed.value}</span>{' '}
                <span className="text-label text-ink-3">{smoothed.n}</span>
              </div>
              <div className="text-label text-ink-3">
                {smoothed.basis} · lower {lower.value} ({lower.basis})
              </div>
              <RateLine rate={raw} />
              {row.earned?.low_sample && (
                <div
                  className="mt-0.5 rounded border border-amber-500/40 bg-amber-500/15 px-1.5 py-0.5 text-label text-amber-300"
                  data-testid={`source-health-low-sample-${id}`}
                  title={ATTENTION_FLAG_TITLE.low_sample}
                >
                  low sample — {smoothed.n}, too few contests to mean anything
                </div>
              )}
            </>
          )}
        </td>

        <td className="px-2 py-1.5" data-testid={`source-health-corroboration-${id}`}>
          {corr == null ? (
            <span className="text-label text-ink-3">no track-record row</span>
          ) : (
            <>
              <div className="text-body text-ink-1">
                {corr.value}{' '}
                <span className="text-label text-ink-3">
                  {corr.n} corroboration checks
                </span>
              </div>
              <div className="text-label text-ink-3">
                {corr.absent
                  ? 'corroboration_rate is null — not computed'
                  : `${row.earned?.corroborated ?? 0} corroborated`}
              </div>
            </>
          )}
        </td>

        {/* ---- COMPUTED ---- */}
        <td
          className="border-l border-line-strong px-2 py-1.5"
          data-testid={`source-health-freshness-${id}`}
        >
          <span
            className={cn('rounded border px-1.5 py-0.5 text-label', FRESHNESS_PILL[tone])}
            title={freshnessTitle(row.computed.freshness_grade, row.computed.budget_minutes)}
            data-testid={`source-health-freshness-grade-${id}`}
          >
            {row.computed.freshness_grade}
            {isFreshnessAbsence(row.computed.freshness_grade) ? ' (absence, not a fault)' : ''}
          </span>
          <div className="mt-0.5 text-label text-ink-3">{lastSignalText(row.computed)}</div>
          <div className="text-label text-ink-3">{signalVolumeText(row.computed)}</div>
        </td>

        <td className="px-2 py-1.5" data-testid={`source-health-flags-${id}`}>
          {flags.length === 0 ? (
            <span className="text-label text-ink-3">—</span>
          ) : (
            <div className="flex flex-wrap gap-1">
              {flags.map((f) => (
                <span
                  key={f}
                  className={cn('rounded border px-1.5 py-0.5 text-label', FLAG_PILL[f])}
                  data-testid={`source-health-flag-${id}-${f}`}
                  title={ATTENTION_FLAG_TITLE[f]}
                >
                  {ATTENTION_FLAG_LABEL[f]}
                </span>
              ))}
            </div>
          )}
        </td>
      </tr>

      {expanded && (
        <tr data-testid={`source-health-detail-${id}`}>
          <td colSpan={8} className="border-t border-line bg-surf-base px-2 py-2">
            <SourceDetail
              row={row}
              detail={detail}
              loading={detailLoading}
              error={detailError}
              includePrivate={includePrivate}
              onToggleIncludePrivate={onToggleIncludePrivate}
            />
          </td>
        </tr>
      )}
    </tbody>
  )
}

/** A secondary rate line — always the field name, the value, and the n. */
function RateLine({ rate }: { rate: RateText }) {
  return (
    <div className="text-label text-ink-3">
      {rate.basis} {rate.value} {rate.n}
      {rate.absent ? ' (null — not computed, not zero)' : ''}
    </div>
  )
}

function SourceDetail({
  row,
  detail,
  loading,
  error,
  includePrivate,
  onToggleIncludePrivate,
}: {
  row: SourceQualityRow
  detail: SourceQualityDetail | undefined
  loading: boolean
  error: unknown
  includePrivate: boolean
  onToggleIncludePrivate: () => void
}) {
  const id = row.source_id
  const split = assertedVsEarned(detail ?? row)
  const earned = (detail ?? row).earned
  const ratings = detail ? sortRatings(detail.ratings) : []

  return (
    <div className="space-y-2">
      <div className="grid gap-2 md:grid-cols-2">
        <Section label="Asserted — the claim">
          <p className="text-body text-ink-2" data-testid={`source-health-detail-asserted-${id}`}>
            {split.asserted}
          </p>
          {row.asserted.host_rationale && (
            <p className="mt-1 text-label text-ink-3">
              host rationale: {row.asserted.host_rationale}
            </p>
          )}
        </Section>
        <Section label="Earned — the measured record">
          <p className="text-body text-ink-1" data-testid={`source-health-detail-earned-${id}`}>
            {split.earned}
          </p>
          {earned && (
            <p className="mt-1 text-label text-ink-3">
              corroborated {earned.corroborated}/{earned.corroboration_total} ·{' '}
              {corroborationDisplay(earned)?.basis} {formatPercent(earned.corroboration_rate)} · lag{' '}
              {earned.lag_hours}h · sample as of {earned.sample_as_of} · computed{' '}
              {earned.computed_at}
            </p>
          )}
        </Section>
      </div>

      {split.tension && (
        <div
          className="flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 p-1.5 text-body text-amber-300"
          data-testid={`source-health-tension-${id}`}
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>{split.tension}</span>
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onToggleIncludePrivate}
          data-testid={`source-health-include-private-${id}`}
          className={cn(
            'rounded border px-2 py-0.5 text-label',
            includePrivate
              ? 'border-line-strong bg-surf-3 text-ink-1'
              : 'border-line text-ink-3 hover:text-ink-1',
          )}
        >
          {includePrivate ? 'including private ratings' : 'public ratings only'}
        </button>
        {detail && (
          <span className="text-label text-ink-3">
            server returned includes_private={String(detail.includes_private)}
          </span>
        )}
      </div>

      {loading && (
        <div className="text-body text-ink-3" data-testid={`source-health-detail-loading-${id}`}>
          reading the per-source drill-down…
        </div>
      )}

      {error != null && (
        <div
          className="rounded border border-rose-500/40 bg-rose-500/10 p-1.5 text-body text-rose-300"
          data-testid={`source-health-detail-error-${id}`}
        >
          Could not read this source's drill-down — {apiErrorDetail(error)}
        </div>
      )}

      {detail && (
        <>
          <Section label={`Ratings (${ratings.length}) — each one a claim, with its author`}>
            {ratings.length === 0 ? (
              <p className="text-body text-ink-3" data-testid={`source-health-ratings-empty-${id}`}>
                No rating has ever been filed for this source
                {includePrivate ? '' : ' in the public visibility class'} — an absence of
                assessment, not a poor one.
              </p>
            ) : (
              <ul className="space-y-1" data-testid={`source-health-ratings-${id}`}>
                {ratings.map((r) => (
                  <li
                    key={r.rating_id}
                    className="rounded border border-line bg-surf-1 px-1.5 py-1"
                    data-testid={`source-health-rating-${r.rating_id}`}
                  >
                    <div className="text-body text-ink-2">{describeRating(r)}</div>
                    <div className="text-label text-ink-3">rated {r.rated_at}</div>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section label="Dossier — a compiled claim about the source, not a measurement">
            {detail.dossier == null ? (
              <p className="text-body text-ink-3" data-testid={`source-health-dossier-empty-${id}`}>
                No dossier has been compiled for this source.
              </p>
            ) : (
              <div data-testid={`source-health-dossier-${id}`}>
                <div className="text-label text-ink-3">
                  compiled by{' '}
                  <span className="font-mono text-ink-2">{detail.dossier.compiled_by}</span> at{' '}
                  {detail.dossier.compiled_at} · {detail.dossier.references.length} reference(s)
                </div>
                <pre className="mt-0.5 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-line bg-surf-base p-2 font-mono text-label text-ink-2">
                  {detail.dossier.dossier_md}
                </pre>
              </div>
            )}
          </Section>
        </>
      )}

      <Section label="Earned track record — every rate with its own denominator">
        {earned == null ? (
          <p className="text-body text-ink-3" data-testid={`source-health-detail-norecord-${id}`}>
            {earnedSummary(null)}
          </p>
        ) : (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-body">
            <Field k="wins / losses" v={`${earned.wins} / ${earned.losses}`} />
            <Field k="contested_total (n)" v={String(earned.contested_total)} />
            <Field
              k="win_rate_smoothed"
              v={`${formatPercent(earned.win_rate_smoothed)} over n=${earned.contested_total}`}
            />
            <Field
              k="win_rate_lower"
              v={`${formatPercent(earned.win_rate_lower)} over n=${earned.contested_total}`}
            />
            <Field
              k="win_rate_raw"
              v={
                earned.win_rate_raw == null
                  ? `${ABSENT} (null — not computed) over n=${earned.contested_total}`
                  : `${formatPercent(earned.win_rate_raw)} over n=${earned.contested_total}`
              }
            />
            <Field
              k="corroboration_rate"
              v={
                earned.corroboration_rate == null
                  ? `${ABSENT} (null — not computed) over n=${earned.corroboration_total}`
                  : `${formatPercent(earned.corroboration_rate)} over n=${earned.corroboration_total}`
              }
            />
            <Field k="low_sample" v={String(earned.low_sample)} />
          </dl>
        )}
      </Section>
    </div>
  )
}

function Field({ k, v }: { k: string; v: string }) {
  return (
    <div className="contents">
      <dt className="text-ink-3">{k}</dt>
      <dd className="min-w-0 break-words font-mono text-ink-2">{v}</dd>
    </div>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-label uppercase tracking-wider text-ink-3">{label}</div>
      <div className="mt-0.5">{children}</div>
    </div>
  )
}
