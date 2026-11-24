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
 * No score appears here: scoring is the track record's, behind its gate.
 */
import { useEffect, useMemo, useState } from 'react'
import { ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import type { LedgerForecast } from '@/lib/ledgerModel'
import type { ScorecardQuestion } from '@/lib/scorecardModel'
import { usePanelActionHandler } from '@/lib/panelActions'
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
import { useLedgerForecasts, useLedgerScorecard, useReceiptAnchors, useSealProof } from './ledgerQueries'
import { FORECAST_FILTERS, useLedgerView, type ForecastFilter } from './ledgerView'

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

function ForecastRow({
  f,
  q,
  voidReason,
  anchors,
  now,
  selected,
  onSelect,
}: {
  f: LedgerForecast
  q: ScorecardQuestion | undefined
  voidReason: string | null
  anchors: AnchorRow[] | null | undefined
  now: number
  selected: boolean
  onSelect: () => void
}) {
  const [open, setOpen] = useState(false)
  const state = forecastState(f, q, voidReason, new Date(now))
  const rule = resolutionRule(f)
  const check = sealCheck(f.sealed_at, anchors)
  const nums = statedNumbers(f)
  const closed = state.kind === 'resolved_yes' || state.kind === 'resolved_no' || state.kind === 'void'
  return (
    <li
      className={cn('border-b border-line py-4 pl-3', selected ? 'border-l-2 border-l-point' : 'border-l-2 border-l-transparent')}
      data-testid="ledger-forecast-row"
      data-slug={f.slug}
      data-state={state.kind}
      aria-current={selected ? 'true' : undefined}
    >
      <button type="button" className="block w-full text-left" onClick={onSelect} data-testid="ledger-forecast-select">
        <p className="max-w-[72ch] font-serif text-heading text-ink-1">{f.question}</p>
      </button>
      <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-label">
        <span className={cn('font-medium', toneClass(state))} data-testid="ledger-forecast-state">
          {state.label}
        </span>
        <span className="font-mono text-ink-2">
          sealed {day(f.sealed_at)} · due {day(f.due_at)}
          {!closed && <span className="text-ink-3"> ({dueIn(f.due_at, now)})</span>}
        </span>
        <span className={cn('font-mono', sealClass(check.state))} data-testid="ledger-forecast-seal">
          seal: {SEAL_WORD[check.state]}
        </span>
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-body" data-testid="ledger-forecast-numbers">
        {nums.map((n, i) => (
          <li key={`${n.kind}-${i}`} title={n.reason ?? undefined}>
            <span className="text-ink-3">{n.who}</span>{' '}
            {n.p == null ? (
              <span className="text-ink-3">· no number stated</span>
            ) : (
              <span className="font-mono text-ink-1">{n.text}</span>
            )}
          </li>
        ))}
      </ul>
      {state.detail && <p className="mt-1 text-label text-ink-2">{state.detail}</p>}
      <p className="mt-2 max-w-[80ch] text-body text-ink-2" data-testid="ledger-forecast-rule">
        {rule.rule}
        {rule.confirmation && <span> Confirmed by {rule.confirmation}.</span>} <span className="text-ink-3">{rule.timing}</span>
      </p>
      <button
        type="button"
        className="mt-2 text-label text-ink-3 underline decoration-line underline-offset-2 hover:text-ink-1"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        data-testid="ledger-forecast-more"
      >
        {open ? 'Hide the rule in full and the seal' : 'The rule in full, and how to check the seal'}
      </button>
      {open && (
        <div className="mt-2 space-y-1 text-label text-ink-2">
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
          <SealDetail f={f} check={check} anchors={anchors} />
        </div>
      )}
    </li>
  )
}

export default function LedgerForecasts() {
  const fq = useLedgerForecasts()
  const sq = useLedgerScorecard()
  const aq = useReceiptAnchors()
  const { forecast, filter, set } = useLedgerView()
  const now = Date.now()

  usePanelActionHandler(VIEW_ID, 'show_forecast', (args) => {
    const slug = String(args.forecast ?? '')
    const known = fq.data?.forecasts?.some((f) => f.slug === slug)
    if (!known) return false
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

  const byId = useMemo(() => new Map((sq.data?.questions ?? []).map((q) => [q.id, q])), [sq.data])
  const voids = useMemo(() => new Map((sq.data?.void_questions ?? []).map((v) => [v.slug, v.reason])), [sq.data])
  const anchors: AnchorRow[] | null | undefined = aq.isPending ? undefined : aq.isError ? null : (aq.data?.anchors ?? null)

  const all = fq.data?.forecasts ?? []
  const isClosed = (f: LedgerForecast) => f.status !== 'open'
  // Open first by due date (what resolves next), then the closed ones, newest due first.
  const shown = all
    .filter((f) => (filter === 'all' ? true : filter === 'open' ? !isClosed(f) : isClosed(f)))
    .sort((a, b) => Number(isClosed(a)) - Number(isClosed(b)) || (isClosed(a) ? b.due_at.localeCompare(a.due_at) : a.due_at.localeCompare(b.due_at)))
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
  } else if (all.length === 0) {
    body = <p className="text-body text-ink-2">No forecast has been sealed yet.</p>
  } else if (shown.length === 0) {
    body = <p className="text-body text-ink-2">{filter === 'resolved' ? 'No forecast has resolved yet.' : 'No forecast is open.'}</p>
  } else {
    body = (
      <ol className="border-t border-line" data-testid="ledger-forecast-list">
        {shown.map((f) => (
          <ForecastRow
            key={f.id}
            f={f}
            q={byId.get(f.id)}
            voidReason={voids.get(f.slug) ?? null}
            anchors={anchors}
            now={now}
            selected={forecast === f.slug}
            onSelect={() => set({ forecast: forecast === f.slug ? null : f.slug })}
          />
        ))}
      </ol>
    )
  }

  return (
    <section className="mx-auto max-w-[96ch] space-y-3" data-testid="ledger-forecasts">
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
