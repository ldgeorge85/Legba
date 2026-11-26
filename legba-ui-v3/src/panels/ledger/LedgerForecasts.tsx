/**
 * LedgerForecasts — THE one home of forecasts (`view:forecasts`, the Ledger
 * page's stage; U-11 replaces the U-2 table host).
 *
 * Every sealed forecast, open first by due date, then the resolved and voided
 * ones: the question; each forecaster's probability (an absence says so);
 * when it was sealed and when it is due; its resolution rule in plain words
 * with the void rules verbatim; its state (open, past due, a proposal awaiting
 * a person's confirmation, waiting on a judgement, resolved yes or no, voided
 * with its reason); and the SEAL, shown as checkable — its hash, what it
 * covers, the leaf the daily anchor folds, and whether an anchor covers it yet
 * (`lib/ledgerProof.ts::sealCheck`), and — F-1 — the seal's inclusion path in
 * the first anchor that kept its leaves, folded HERE in the browser
 * (`sealProofCheck`): "included in the anchor of <day>" is said only when the
 * path checks against the root the anchors route lists. The outside timestamp
 * on that root is never verified in the console.
 *
 * A resolved forecast shows its score only when the scorecard says enough
 * questions have resolved (`scoresWithheld`); until then the cell is a dash.
 */
import { useEffect, useMemo, useState } from 'react'
import { ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import type { LedgerForecast } from '@/lib/ledgerModel'
import { fmtBrier, scoresMin, scoresWithheld, type ScorecardQuestion, type ScorecardResponse } from '@/lib/scorecardModel'
import { usePanelActionHandler } from '@/lib/panelActions'
import { useAsOf } from '@/lib/asOf'
import {
  SEAL_COVERS,
  day,
  forecastState,
  parseTs,
  questionLeafInput,
  resolutionRule,
  sealCheck,
  sealProofCheck,
  statedNumbers,
  type AnchorRow,
  type ForecastState,
  type SealCheck,
  type SealProof,
} from '@/lib/ledgerProof'
import { useLedgerForecasts, useLedgerScorecard, useReceiptAnchors, useResolutionRecords, useSealProof } from './ledgerQueries'
import { FORECAST_FILTERS, sortForecasts, useLedgerView, type ForecastFilter, type ForecastSort } from './ledgerView'
import { PointFilterLine, pointLabel, usePointFilter } from '@/components/PointFilter'

const VIEW_ID = 'view:forecasts'
const DAY_MS = 86_400_000

function dueIn(dueAt: string, now: number): string {
  const t = parseTs(dueAt)
  if (t == null) return ''
  const d = Math.ceil((t - now) / DAY_MS)
  if (d > 1) return `in ${d} days`
  if (d === 1) return 'tomorrow'
  if (d === 0) return 'today'
  return `${-d} days ago`
}

const FILTER_WORDS: Record<ForecastFilter, string> = { open: 'Open', resolved: 'Resolved', all: 'All' }

function toneClass(s: ForecastState): string {
  return s.tone === 'warning' ? 'text-accent-warning' : 'text-ink-1'
}

function sealClass(s: SealCheck['state']): string {
  if (s === 'anchored') return 'text-accent-ok'
  if (s === 'pending' || s === 'unreadable') return 'text-accent-warning'
  return 'text-ink-2'
}

const SEAL_WORD: Record<SealCheck['state'], string> = {
  unsealed: 'not sealed',
  reading: 'reading…',
  unreadable: 'anchors unread',
  not_covered: 'not covered yet',
  pending: 'anchor pending',
  anchored: 'anchored',
}

async function sha256Hex(text: string): Promise<string | null> {
  try {
    const subtle = globalThis.crypto?.subtle
    if (!subtle) return null
    const buf = await subtle.digest('SHA-256', new TextEncoder().encode(text))
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('')
  } catch {
    return null
  }
}

const PROOF_CLASS: Record<SealProof['state'], string> = {
  reading: 'text-ink-3',
  unavailable: 'text-ink-2',
  not_stored: 'text-ink-2',
  included: 'text-accent-ok',
  failed: 'text-accent-warning',
}

/**
 * F-1: the seal's inclusion path, checked in the browser
 * (`lib/ledgerProof.ts::sealProofCheck`) — read only once the seal is open.
 */
function InclusionCheck({ f, anchors }: { f: LedgerForecast; anchors: AnchorRow[] | null | undefined }) {
  const pq = useSealProof(f.id, true)
  const proof = pq.isPending ? undefined : pq.isError ? null : pq.data
  const [res, setRes] = useState<SealProof | null>(null)
  useEffect(() => {
    let live = true
    void sealProofCheck(f, proof, anchors).then((r) => live && setRes(r))
    return () => {
      live = false
    }
  }, [f, proof, anchors])
  const r = res ?? { state: 'reading' as const, text: 'Checking the inclusion path…' }
  return (
    <div className={cn(PROOF_CLASS[r.state])} data-testid="forecast-seal-inclusion" data-state={r.state}>
      {r.text}
    </div>
  )
}

function SealDetail({ f, check, anchors }: { f: LedgerForecast; check: SealCheck; anchors: AnchorRow[] | null | undefined }) {
  const [leaf, setLeaf] = useState<string | null>(null)
  const sha = f.content_sha256 ?? null
  useEffect(() => {
    let live = true
    if (sha) void sha256Hex(questionLeafInput(f.id, sha)).then((h) => live && setLeaf(h))
    return () => {
      live = false
    }
  }, [f.id, sha])
  return (
    <div className="mt-2 space-y-2 border-l border-line pl-3 text-label text-ink-2" data-testid="forecast-seal-detail">
      <div>
        <span className="text-ink-3">The seal covers: </span>
        {SEAL_COVERS.join('; ')}. Each forecaster's number carries its own hash, bound to this one.
      </div>
      <div className="font-mono break-all">
        <span className="font-sans text-ink-3">Question hash (SHA-256): </span>
        <span data-testid="forecast-seal-hash">{sha ?? 'not reported by the route'}</span>
      </div>
      {sha && (
        <div className="font-mono break-all">
          <span className="font-sans text-ink-3">Anchor leaf, SHA-256 of </span>
          <code>forecast_question:&lt;id&gt;&lt;hash&gt;</code>
          <span className="font-sans text-ink-3">: </span>
          <span data-testid="forecast-seal-leaf">{leaf ?? '…'}</span>
        </div>
      )}
      <div className={cn(sealClass(check.state))} data-testid="forecast-seal-check" data-state={check.state}>
        {check.text}
      </div>
      {check.anchor && (
        <div className="font-mono break-all">
          <span className="font-sans text-ink-3">Anchor root of {check.anchor.day} ({check.anchor.leafCount} leaves): </span>
          {check.anchor.root}
        </div>
      )}
      {f.sealed_at && <InclusionCheck f={f} anchors={anchors} />}
    </div>
  )
}

const STATUS_WORD: Record<ForecastState['kind'], string> = {
  open: 'Open',
  overdue: 'Past due',
  awaiting_confirmation: 'Proposed',
  needs_judgement: 'Needs a judgement',
  resolved_yes: 'Resolved yes',
  resolved_no: 'Resolved no',
  void: 'Void',
}

/** The forecast's score once resolved: the first issuer's Brier figure, "—" until scores are shown. */
function scoreCell(q: ScorecardQuestion | undefined, sc: ScorecardResponse | null | undefined, state: ForecastState) {
  if (state.kind !== 'resolved_yes' && state.kind !== 'resolved_no') return { text: '', title: undefined }
  if (scoresWithheld(sc)) return { text: '—', title: `Scores appear once ${scoresMin(sc)} questions have resolved` }
  const scored = (q?.numbers ?? []).find((n) => n.brier != null)
  if (!scored) return { text: '—', title: 'No score was recorded for this question' }
  return { text: fmtBrier(scored.brier), title: `${scored.issuer_kind.replace(/[_-]+/g, ' ')}: lower is better` }
}

/** The history of one forecast, from its resolution records (read only once it is opened). */
function History({ id }: { id: string }) {
  const rq = useResolutionRecords(id)
  const records = rq.data?.records ?? []
  if (records.length === 0) return null
  return (
    <p data-testid="ledger-forecast-history">
      <span className="text-ink-3">History: </span>
      {records.map((r) => `${r.record_kind} ${r.outcome} ${day(r.recorded_at)}`).join(' · ')}
    </p>
  )
}

/** The detail sheet under a row: full rule, void rule, state, seal and history. */
function ForecastDetail({ f, state, anchors }: { f: LedgerForecast; state: ForecastState; anchors: AnchorRow[] | null | undefined }) {
  const rule = resolutionRule(f)
  const check = sealCheck(f.sealed_at, anchors)
  const nums = statedNumbers(f)
  return (
    <div className="space-y-2 border-l-2 border-l-point px-3 py-3 text-label text-ink-2" data-testid="ledger-forecast-sheet">
      <p className="max-w-[80ch] font-serif text-heading text-ink-1">{f.question}</p>
      <p className="font-medium text-ink-1" data-testid="ledger-forecast-state">
        {state.label}
      </p>
      {state.detail && <p>{state.detail}</p>}
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-body" data-testid="ledger-forecast-numbers">
        {nums.map((n, i) => (
          <li key={`${n.kind}-${i}`} title={n.reason ?? undefined}>
            <span className="text-ink-3">{n.who}</span>{' '}
            {n.p == null ? <span className="text-ink-3">· no number stated</span> : <span className="font-mono text-ink-1">{n.text}</span>}
          </li>
        ))}
      </ul>
      <p className="font-mono">
        sealed {day(f.sealed_at)} · due {day(f.due_at)}
      </p>
      <p className="max-w-[80ch] text-body text-ink-2" data-testid="ledger-forecast-rule">
        {rule.rule}
        {rule.confirmation && <span> Confirmed by {rule.confirmation}.</span>} <span className="text-ink-3">{rule.timing}</span>
      </p>
      {rule.condition && (
        <p>
          <span className="text-ink-3">Only counts if: </span>
          {rule.condition}
        </p>
      )}
      {rule.voidRule && (
        <p>
          <span className="text-ink-3">Void if: </span>
          {rule.voidRule}
        </p>
      )}
      <History id={f.id} />
      <p className={cn('font-mono', sealClass(check.state))} data-testid="ledger-forecast-seal">
        seal: {SEAL_WORD[check.state]}
      </p>
      <SealDetail f={f} check={check} anchors={anchors} />
    </div>
  )
}

const COLS = 'px-2 py-1.5 text-left align-baseline'

function ForecastRow({
  f,
  q,
  sc,
  voidReason,
  anchors,
  now,
  selected,
  onSelect,
}: {
  f: LedgerForecast
  q: ScorecardQuestion | undefined
  sc: ScorecardResponse | null | undefined
  voidReason: string | null
  anchors: AnchorRow[] | null | undefined
  now: number
  selected: boolean
  onSelect: () => void
}) {
  const state = forecastState(f, q, voidReason, new Date(now))
  const nums = statedNumbers(f).filter((n) => n.p != null)
  const lead = nums[0]
  const closed = state.kind === 'resolved_yes' || state.kind === 'resolved_no' || state.kind === 'void'
  const score = scoreCell(q, sc, state)
  const subject = (f.subject_keys ?? []).join(', ')
  return (
    <>
      <tr
        className={cn(
          'cursor-pointer border-b border-line-subtle hover:bg-surf-2',
          selected && 'bg-surf-2',
        )}
        data-testid="ledger-forecast-row"
        data-slug={f.slug}
        data-state={state.kind}
        aria-current={selected ? 'true' : undefined}
        onClick={onSelect}
      >
        <td className={cn(COLS, 'max-w-0 w-[44%]', selected ? 'border-l-2 border-l-point' : 'border-l-2 border-l-transparent')}>
          <button type="button" className="block w-full truncate text-left text-body text-ink-1" title={f.question} data-testid="ledger-forecast-select">
            {f.question}
          </button>
        </td>
        <td className={cn(COLS, 'whitespace-nowrap font-mono text-ink-1')} data-testid="ledger-forecast-p" title={nums.map((n) => `${n.who} ${n.text}`).join(' · ') || 'no number stated'}>
          {lead ? lead.text : '—'}
          {nums.length > 1 ? <span className="font-sans text-ink-3"> {lead!.who} +{nums.length - 1}</span> : null}
        </td>
        <td className={cn(COLS, 'whitespace-nowrap font-mono text-label text-ink-2')} title={closed ? undefined : dueIn(f.due_at, now)}>
          {day(f.due_at)}
        </td>
        <td className={cn(COLS, 'max-w-0 w-[16%] truncate text-label text-ink-2')} title={subject}>
          {subject || '—'}
        </td>
        <td className={cn(COLS, 'whitespace-nowrap text-label', toneClass(state))} title={state.label} data-testid="ledger-forecast-status">
          {STATUS_WORD[state.kind]}
        </td>
        <td className={cn(COLS, 'whitespace-nowrap font-mono text-label text-ink-2')} title={score.title} data-testid="ledger-forecast-score">
          {score.text}
        </td>
      </tr>
      {selected && (
        <tr data-testid="ledger-forecast-detail" data-slug={f.slug}>
          <td colSpan={6} className="border-b border-line bg-surf-1">
            <ForecastDetail f={f} state={state} anchors={anchors} />
          </td>
        </tr>
      )}
    </>
  )
}

function SortHead({ label, by, sort, onSort }: { label: string; by: ForecastSort['by']; sort: ForecastSort; onSort: (by: ForecastSort['by']) => void }) {
  const on = sort.by === by
  return (
    <th className={cn(COLS, 'font-normal text-ink-3')} aria-sort={on ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className={cn('hover:text-ink-1', on && 'text-ink-1')} onClick={() => onSort(by)} data-testid={`ledger-sort-${by}`}>
        {label}
        {on ? (sort.dir === 'asc' ? ' ↑' : ' ↓') : ''}
      </button>
    </th>
  )
}

export default function LedgerForecasts() {
  // AS RECORDED: with an instant set the table is the ledger as it stood
  // then; the scorecard (today's resolutions and scores) is left out, so a
  // past row's state is the one it had at that instant.
  const asOfMs = useAsOf()
  const fq = useLedgerForecasts(asOfMs)
  const sqNow = useLedgerScorecard()
  const scorecard = asOfMs === null ? sqNow.data : undefined
  const aq = useReceiptAnchors()
  const { forecast, filter, sort, set } = useLedgerView()
  const now = Date.now()
  // THE POINT: with a subject in focus the table narrows to the forecasts
  // about it (their subject keys are subject ids); "Show all" undoes that here.
  const pointFilter = usePointFilter()

  usePanelActionHandler(VIEW_ID, 'show_forecast', (args) => {
    const slug = String(args.forecast ?? '')
    const found = fq.data?.forecasts?.find((f) => f.slug === slug)
    if (!found) return false
    // a forecast about another subject than the point is shown in the full table
    if (pointFilter.filtered && !(found.subject_keys ?? []).includes(pointFilter.point!.id)) pointFilter.showAll()
    set({ forecast: slug, filter: 'all' })
    requestAnimationFrame(() => document.querySelector(`[data-slug="${CSS.escape(slug)}"]`)?.scrollIntoView({ block: 'center' }))
    return true
  })
  usePanelActionHandler(VIEW_ID, 'filter', (args) => {
    const v = String(args.show ?? '') as ForecastFilter
    if (!FORECAST_FILTERS.includes(v)) return false
    set({ filter: v })
    return true
  })

  const byId = useMemo(() => new Map((scorecard?.questions ?? []).map((q) => [q.id, q])), [scorecard])
  const voids = useMemo(() => new Map((scorecard?.void_questions ?? []).map((v) => [v.slug, v.reason])), [scorecard])
  const anchors: AnchorRow[] | null | undefined = aq.isPending ? undefined : aq.isError ? null : (aq.data?.anchors ?? null)

  const every = fq.data?.forecasts ?? []
  const pointId = pointFilter.filtered ? pointFilter.point!.id : null
  const all = pointId ? every.filter((f) => (f.subject_keys ?? []).includes(pointId)) : every
  const isClosed = (f: LedgerForecast) => f.status !== 'open'
  // Default: open first by due date (what resolves next), then the closed ones, newest due first.
  const shown = sortForecasts(
    all.filter((f) => (filter === 'all' ? true : filter === 'open' ? !isClosed(f) : isClosed(f))),
    sort,
  )
  const onSort = (by: ForecastSort['by']) => set({ sort: sort.by === by ? { by, dir: sort.dir === 'asc' ? 'desc' : 'asc' } : { by, dir: 'asc' } })
  const nOpen = all.filter((f) => !isClosed(f)).length
  const nClosed = all.length - nOpen
  const nextDue = all
    .filter((f) => !isClosed(f))
    .map((f) => f.due_at)
    .sort()[0]

  let body: React.ReactNode
  if (fq.isPending) body = <p className="text-body text-ink-3">Reading the forecasts…</p>
  else if (fq.isError) {
    const notServed = fq.error instanceof ApiError && fq.error.status === 404
    body = (
      <p className="text-body text-ink-2" data-testid="ledger-forecasts-error">
        {notServed ? 'This installation does not serve the forecast ledger yet.' : 'The forecasts could not be read; nothing is shown in their place.'}
      </p>
    )
  } else if (fq.data && fq.data.available === false) {
    body = (
      <p className="text-body text-ink-2" data-testid="ledger-forecasts-error">
        The forecast ledger could not be read. That is not the same as having no forecasts.
      </p>
    )
  } else if (all.length === 0 && pointId) {
    body = (
      <p className="text-body text-ink-2" data-testid="ledger-forecasts-none-on-point">
        No sealed forecast is about {pointLabel(pointFilter.point!)}.
      </p>
    )
  } else if (all.length === 0) {
    body = <p className="text-body text-ink-2">No forecast has been sealed yet.</p>
  } else if (shown.length === 0) {
    body = <p className="text-body text-ink-2">{filter === 'resolved' ? 'No forecast has resolved yet.' : 'No forecast is open.'}</p>
  } else {
    body = (
      <table className="w-full table-fixed border-t border-line text-body" data-testid="ledger-forecast-list">
        <thead>
          <tr className="border-b border-line text-label">
            <th className={cn(COLS, 'w-[44%] font-normal text-ink-3')}>Question</th>
            <th className={cn(COLS, 'w-[11%] font-normal text-ink-3')}>Probability</th>
            <SortHead label="Due" by="due" sort={sort} onSort={onSort} />
            <th className={cn(COLS, 'w-[16%] font-normal text-ink-3')}>Subject</th>
            <SortHead label="Status" by="status" sort={sort} onSort={onSort} />
            <th className={cn(COLS, 'font-normal text-ink-3')} title="Lower is better; shown once enough questions have resolved">
              Score
            </th>
          </tr>
        </thead>
        <tbody>
          {shown.map((f) => (
            <ForecastRow
              key={f.id}
              f={f}
              q={byId.get(f.id)}
              sc={scorecard}
              voidReason={voids.get(f.slug) ?? null}
              anchors={anchors}
              now={now}
              selected={forecast === f.slug}
              onSelect={() => set({ forecast: forecast === f.slug ? null : f.slug })}
            />
          ))}
        </tbody>
      </table>
    )
  }

  return (
    <section className="space-y-3" data-testid="ledger-forecasts">
      <header className="space-y-1">
        <h2 className="text-heading-lg font-semibold text-ink-1">Forecasts</h2>
        <p className="text-body text-ink-2" data-testid="ledger-forecasts-counts">
          {fq.isSuccess && fq.data.available !== false ? (
            <>
              <span className="font-mono">{all.length}</span> sealed · <span className="font-mono">{nOpen}</span> open ·{' '}
              <span className="font-mono">{nClosed}</span> closed
              {nextDue && (
                <>
                  {' '}
                  · next due <span className="font-mono">{day(nextDue)}</span>
                </>
              )}
            </>
          ) : (
            '—'
          )}
        </p>
        <p className="max-w-[80ch] text-label text-ink-3">
          Each forecast was sealed before its outcome was known: the question, the probability and the rule that settles it
          cannot change after the seal. A resolver proposes outcomes; nothing closes until a person confirms it.
        </p>
      </header>
      <PointFilterLine state={pointFilter} testId="ledger-forecasts-point" />
      <div className="flex gap-1" role="tablist" aria-label="Which forecasts">
        {FORECAST_FILTERS.map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={filter === k}
            onClick={() => set({ filter: k })}
            className={cn(
              'border px-2.5 py-1 text-label',
              filter === k ? 'border-line-strong bg-surf-3 text-ink-1' : 'border-line text-ink-3 hover:text-ink-1',
            )}
            data-testid={`ledger-filter-${k}`}
          >
            {FILTER_WORDS[k]}
          </button>
        ))}
      </div>
      {body}
    </section>
  )
}
