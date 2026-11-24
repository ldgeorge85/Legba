/**
 * ResolutionQueue — proposed forecast resolutions awaiting a person
 * (`view:resolutions`, the operator side's Gate page; U-11, made a decision
 * surface by F-1). Kept OUT of the reader's Ledger: the reader sees a
 * forecast's state ("awaiting confirmation", then the result and which role
 * confirmed it); the person deciding sees the queue and the evidence.
 *
 * The queue is the server's (`GET /v3/ledger/resolution-queue`): open
 * questions whose newest proposal no act has answered, each with what THIS
 * person may do — a contributor never sees a question they entered a number
 * on (the count of those is said). Opening one shows, in this order: the
 * evidence the resolver cites; the clauses of the sealed rule each piece
 * meets; the rule in plain words with its void rules; the judgements it waits
 * on; then the decision — Confirm the proposal's outcome, Reject it (a reason
 * is required), or, where the rule cannot decide, decide yes / no / void on
 * your judgement (a reason is required; so is one for any void). Every
 * decision passes an "are you sure" step built into the page (the viewer has
 * no browser dialogs), and what happened is stated afterwards. A refusal is
 * shown in the route's own words.
 *
 * A registry without the route (404) keeps U-11's read-only queue: the
 * scorecard's reading, the record route's evidence, and the host command.
 */
import { useState } from 'react'
import { ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import { usePanelActionHandler } from '@/lib/panelActions'
import {
  confirmCommand,
  day,
  decisionDone,
  decisionProblem,
  decisionSummary,
  evidenceLine,
  refusalWords,
  resolutionQueue,
  resolutionRule,
  roleWords,
  type Decision,
  type DecisionResult,
  type Outcome,
  type QueueEntry,
  type ResolutionRecord,
} from '@/lib/ledgerProof'
import { useDecide, useLedgerScorecard, useResolutionQueue, useResolutionRecords } from './ledgerQueries'
import { useLedgerView } from './ledgerView'

const VIEW_ID = 'view:resolutions'

const H5 = 'text-label uppercase tracking-[0.06em] text-ink-3'
const BTN = 'border border-line-strong px-2.5 py-1 text-label text-ink-1 hover:bg-surf-3 disabled:opacity-50'

function EvidenceList({ evidence, total }: { evidence: Record<string, unknown>[]; total: number }) {
  return (
    <div data-testid="resolution-evidence">
      <h5 className={H5}>
        Evidence cited ({total}
        {total > evidence.length ? `, first ${evidence.length} shown` : ''})
      </h5>
      {evidence.length === 0 ? (
        <p className="mt-1 text-body text-ink-2">The resolver cites no evidence for this reading.</p>
      ) : (
        <ol className="mt-1 space-y-1.5 text-body">
          {evidence.map((e, i) => {
            const l = evidenceLine(e)
            return (
              <li key={`${l.ref}-${i}`} className="border-l border-line pl-2">
                <div className="text-ink-1">
                  {l.url ? (
                    <a className="underline decoration-line underline-offset-2 hover:text-ink-1" href={l.url} target="_blank" rel="noreferrer">
                      {l.title}
                    </a>
                  ) : (
                    l.title
                  )}
                </div>
                <div className="font-mono text-label text-ink-3">
                  {[l.where, l.when, l.clause && `clause ${l.clause}`, l.ref].filter(Boolean).join(' · ')}
                </div>
                {l.span && <div className="text-label text-ink-2">“{l.span}”</div>}
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

function Clauses({ clauses }: { clauses: { id?: string; text?: string; status?: string }[] }) {
  if (clauses.length === 0) return null
  return (
    <div>
      <h5 className={H5}>The sealed rule, clause by clause</h5>
      <ul className="mt-1 space-y-0.5 text-body">
        {clauses.map((c, i) => (
          <li key={c.id ?? i}>
            <span className={cn('font-mono', c.status === 'met' ? 'text-accent-ok' : c.status === 'unmet' ? 'text-accent-critical' : 'text-accent-warning')}>
              {c.status ?? 'unknown'}
            </span>{' '}
            <span className="text-ink-2">{c.text ?? c.id}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function RuleInWords({ e }: { e: QueueEntry }) {
  const rule = resolutionRule({
    resolver_kind: e.resolver_kind,
    resolver_spec: e.resolver_spec ?? {},
    void_rules: e.void_rules ?? null,
    condition: e.condition ?? null,
    due_at: e.due_at,
    grace_hours: e.grace_hours,
  })
  return (
    <div data-testid="resolution-rule">
      <h5 className={H5}>The rule it was sealed with</h5>
      <p className="mt-1 text-body text-ink-1">
        {rule.rule}
        {rule.confirmation && <span> Confirmed by {rule.confirmation}.</span>} <span className="text-ink-3">{rule.timing}</span>
      </p>
      {rule.condition && (
        <p className="text-label text-ink-2">
          <span className="text-ink-3">Only counts if: </span>
          {rule.condition}
        </p>
      )}
      {rule.voidRule && (
        <p className="text-label text-ink-2">
          <span className="text-ink-3">Void if: </span>
          {rule.voidRule}
        </p>
      )}
    </div>
  )
}

type Phase = { step: 'choose' } | { step: 'sure'; decision: Decision } | { step: 'done'; result: DecisionResult }

function Decide({ e }: { e: QueueEntry }) {
  const [reason, setReason] = useState('')
  const [problem, setProblem] = useState<string | null>(null)
  const [phase, setPhase] = useState<Phase>({ step: 'choose' })
  const decide = useDecide()
  const p = e.proposal

  const ask = (d: Decision) => {
    const why = decisionProblem(e, d)
    setProblem(why)
    if (!why) setPhase({ step: 'sure', decision: d })
  }
  const send = (d: Decision) =>
    decide.mutate(
      { recordId: p.record_id, questionId: e.question_id, decision: d },
      { onSuccess: (result) => setPhase({ step: 'done', result }) },
    )

  if (phase.step === 'done') {
    return (
      <p className="border border-line bg-surf-2 p-2 text-body text-ink-1" data-testid="resolution-done" role="status">
        {decisionDone(phase.result)}
      </p>
    )
  }
  if (phase.step === 'sure') {
    const d = phase.decision
    return (
      <div className="space-y-2 border border-line-strong bg-surf-2 p-2" data-testid="resolution-sure" role="alertdialog" aria-label="Check before recording">
        <p className="text-body text-ink-1">{decisionSummary(e, d)}</p>
        {d.reason.trim() && (
          <p className="text-label text-ink-2">
            <span className="text-ink-3">Your reason: </span>
            {d.reason.trim()}
          </p>
        )}
        {decide.isError && (
          <p className="text-label text-accent-warning" data-testid="resolution-refused">
            {refusalWords(decide.error instanceof ApiError ? decide.error.status : null, decide.error instanceof ApiError ? decide.error.body : null)}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <button type="button" className={BTN} disabled={decide.isPending} onClick={() => send(d)} data-testid="resolution-record">
            {decide.isPending ? 'Recording…' : 'Yes, record it'}
          </button>
          <button
            type="button"
            className="px-2.5 py-1 text-label text-ink-3 underline decoration-line underline-offset-2 hover:text-ink-1"
            disabled={decide.isPending}
            onClick={() => {
              decide.reset()
              setPhase({ step: 'choose' })
            }}
            data-testid="resolution-back"
          >
            Go back
          </button>
        </div>
      </div>
    )
  }

  const outcome = p.outcome as Outcome
  return (
    <div className="space-y-2" data-testid="resolution-decide">
      <h5 className={H5}>Your decision</h5>
      <label className="block text-label text-ink-2">
        Reason <span className="text-ink-3">(required to reject, to void, and for a judgement)</span>
        <textarea
          className="mt-1 block w-full border border-line bg-surf-1 p-1.5 text-body text-ink-1"
          rows={2}
          maxLength={2000}
          value={reason}
          onChange={(ev) => setReason(ev.target.value)}
          data-testid="resolution-reason"
        />
      </label>
      <div className="flex flex-wrap gap-2">
        {e.may.confirm && (
          <button type="button" className={BTN} onClick={() => ask({ verb: 'confirm', outcome, reason })} data-testid="resolution-confirm">
            Confirm: {outcome}
          </button>
        )}
        {e.may.judgement &&
          (['yes', 'no', 'void'] as Outcome[]).map((o) => (
            <button key={o} type="button" className={BTN} onClick={() => ask({ verb: 'judgement', outcome: o, reason })} data-testid={`resolution-judge-${o}`}>
              Decide: {o}
            </button>
          ))}
        {e.may.reject && (
          <button type="button" className={BTN} onClick={() => ask({ verb: 'reject', reason })} data-testid="resolution-reject">
            Reject the proposal
          </button>
        )}
      </div>
      {!e.may.confirm && !e.may.judgement && !e.may.reject && (
        <p className="text-label text-ink-2">Nothing to decide yet: the resolver has not reached a reading. It keeps reading.</p>
      )}
      {problem && (
        <p className="text-label text-accent-warning" data-testid="resolution-problem">
          {problem}
        </p>
      )}
    </div>
  )
}

function Entry({ e }: { e: QueueEntry }) {
  const p = e.proposal
  return (
    <div className="space-y-3" data-testid="resolution-proposal" data-record={p.record_id}>
      <EvidenceList evidence={p.evidence} total={p.evidence_total} />
      <Clauses clauses={p.clauses ?? []} />
      <RuleInWords e={e} />
      {(p.judgements ?? []).length > 0 && (
        <div>
          <h5 className={H5}>Judgements the rule cannot make</h5>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-body text-ink-2">
            {(p.judgements ?? []).map((j, i) => (
              <li key={j.key ?? i}>{j.question ?? j.key}</li>
            ))}
          </ul>
        </div>
      )}
      {p.void_rule && <p className="font-mono text-label text-ink-3 break-all">void rule weighed: {JSON.stringify(p.void_rule)}</p>}
      <Decide key={p.record_id} e={e} />
    </div>
  )
}

function label(e: QueueEntry): string {
  const o = e.proposal.outcome
  if (o === 'yes' || o === 'no' || o === 'void') return `Proposed: ${o}`
  return o === 'needs_judgement' ? 'Needs a judgement' : 'Not yet determined'
}

export default function ResolutionQueue() {
  const rq = useResolutionQueue()
  const { proposal, set } = useLedgerView()
  const items = rq.data?.items ?? []
  const open = items.find((i) => i.question_id === proposal) ?? null

  usePanelActionHandler(VIEW_ID, 'show_forecast', (args) => {
    const slug = String(args.forecast ?? '')
    const hit = items.find((i) => i.slug === slug)
    if (!hit) return false
    set({ proposal: hit.question_id })
    return true
  })

  if (rq.isSuccess && rq.data === null) return <LegacyQueue />

  const forbidden = rq.isError && rq.error instanceof ApiError && rq.error.status === 403
  return (
    <section className="space-y-3" data-testid="resolution-queue">
      <h3 className="text-heading font-semibold text-ink-1">Forecast resolutions awaiting a person</h3>
      <p className="text-label text-ink-3">
        The resolver runs every six hours and only proposes. Read the evidence and the rule, then decide; nothing closes
        until a person does.
        {rq.data && ` You decide as ${roleWords(rq.data.role)}.`}
      </p>
      {rq.isPending && <p className="text-body text-ink-3">Reading the proposals…</p>}
      {forbidden && (
        <p className="text-body text-ink-2" data-testid="resolution-queue-forbidden">
          Deciding a forecast’s outcome needs the contributor role or above.
        </p>
      )}
      {rq.isError && !forbidden && <p className="text-body text-ink-2">The proposals could not be read.</p>}
      {rq.data && rq.data.available === false && (
        <p className="text-body text-ink-2">The resolution record could not be read, so the queue is not known.</p>
      )}
      {rq.data && rq.data.available !== false && items.length === 0 && (
        <p className="text-body text-ink-2" data-testid="resolution-queue-empty">
          Nothing is waiting for you: no open forecast has a proposal without a decision.
        </p>
      )}
      {rq.data && rq.data.hidden > 0 && (
        <p className="text-label text-ink-2" data-testid="resolution-queue-hidden">
          {rq.data.hidden} more {rq.data.hidden === 1 ? 'is' : 'are'} not shown: {rq.data.hidden_reason ?? 'an admin or the owner decides those'}.
        </p>
      )}
      {items.length > 0 && (
        <ol className="border-t border-line" data-testid="resolution-queue-list">
          {items.map((it) => (
            <li key={it.question_id} className="border-b border-line py-2" data-testid="resolution-queue-item" data-slug={it.slug}>
              <button
                type="button"
                className="block w-full text-left"
                aria-expanded={open?.question_id === it.question_id}
                onClick={() => set({ proposal: open?.question_id === it.question_id ? null : it.question_id })}
              >
                <span className={cn('text-label font-medium', it.may.confirm ? 'text-accent-warning' : 'text-ink-2')}>{label(it)}</span>
                <span className="ml-2 font-mono text-label text-ink-3">
                  {it.slug} · proposed {day(it.proposal.recorded_at)} · {it.proposal.evidence_total} evidence · due {day(it.due_at)}
                </span>
                <p className="mt-1 font-serif text-body-lg text-ink-1">{it.question}</p>
              </button>
              {open?.question_id === it.question_id && (
                <div className="mt-2">
                  <Entry e={it} />
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// A registry without the decision routes: U-11's read-only queue.
// ---------------------------------------------------------------------------

function LegacyProposal({ rec }: { rec: ResolutionRecord }) {
  return (
    <div className="space-y-3" data-testid="resolution-proposal" data-record={rec.id}>
      <p className="font-mono text-label text-ink-3">
        record {rec.id} · {rec.record_kind} · {rec.outcome} · as of {day(rec.as_of)} · rule {rec.resolver_kind} {rec.resolver_version}
      </p>
      <Clauses clauses={rec.clauses} />
      <EvidenceList evidence={rec.evidence} total={rec.evidence_total} />
      {rec.judgements.length > 0 && (
        <div>
          <h5 className={H5}>Judgements the rule cannot make</h5>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-body text-ink-2">
            {rec.judgements.map((j, i) => (
              <li key={j.key ?? i}>{j.question ?? j.key}</li>
            ))}
          </ul>
        </div>
      )}
      {Object.keys(rec.searched ?? {}).length > 0 && (
        <p className="font-mono text-label text-ink-3 break-all">searched: {JSON.stringify(rec.searched)}</p>
      )}
      {rec.void_rule && <p className="font-mono text-label text-ink-3 break-all">void rule weighed: {JSON.stringify(rec.void_rule)}</p>}
    </div>
  )
}

function LegacyDetail({ questionId }: { questionId: string }) {
  const q = useResolutionRecords(questionId)
  if (q.isPending) return <p className="text-body text-ink-3">Reading the resolution record…</p>
  if (q.isError) return <p className="text-body text-ink-2">The resolution record could not be read.</p>
  if (q.data === null) {
    return <p className="text-body text-ink-2" data-testid="resolution-records-missing">This registry does not serve the resolution record yet; only the evidence count is known.</p>
  }
  if (!q.data.available) return <p className="text-body text-ink-2">The resolution record could not be read (its table is not readable).</p>
  const proposal = q.data.records.find((r) => r.record_kind === 'proposal')
  return (
    <div className="space-y-3">
      {proposal ? <LegacyProposal rec={proposal} /> : <p className="text-body text-ink-2">No proposal is on record for this question.</p>}
      <div className="border border-line bg-surf-2 p-2 text-label text-ink-2" data-testid="resolution-confirm-gap">
        <p>
          Confirming is a write, and this registry serves no confirm route: nothing here closes a forecast. On the host, a
          person confirms with:
        </p>
        {proposal && <code className="mt-1 block font-mono text-ink-1 break-all">{confirmCommand(proposal.id)}</code>}
      </div>
      {q.data.records.length > 1 && (
        <p className="font-mono text-label text-ink-3">
          earlier on record: {q.data.records.filter((r) => r !== proposal).map((r) => `${r.record_kind} ${r.outcome} ${day(r.recorded_at)}`).join(' · ')}
        </p>
      )}
    </div>
  )
}

function LegacyQueue() {
  const sq = useLedgerScorecard()
  const items = resolutionQueue(sq.data)
  const { proposal, set } = useLedgerView()
  const open = items.find((i) => i.id === proposal) ?? null
  return (
    <section className="space-y-3" data-testid="resolution-queue">
      <h3 className="text-heading font-semibold text-ink-1">Forecast resolutions awaiting a person</h3>
      <p className="text-label text-ink-3">
        The resolver runs every six hours and only proposes. Read the evidence, then confirm or override on the host.
      </p>
      {sq.isPending && <p className="text-body text-ink-3">Reading the proposals…</p>}
      {sq.isError && <p className="text-body text-ink-2">The proposals could not be read.</p>}
      {sq.isSuccess && (sq.data === null || sq.data.available === false) && (
        <p className="text-body text-ink-2">The forecast ledger could not be read, so the queue is not known.</p>
      )}
      {sq.isSuccess && sq.data && sq.data.available !== false && items.length === 0 && (
        <p className="text-body text-ink-2" data-testid="resolution-queue-empty">
          Nothing is waiting: no open forecast has a proposal without a decision.
        </p>
      )}
      {items.length > 0 && (
        <ol className="border-t border-line" data-testid="resolution-queue-list">
          {items.map((it) => (
            <li key={it.id} className="border-b border-line py-2" data-testid="resolution-queue-item" data-slug={it.slug}>
              <button
                type="button"
                className="block w-full text-left"
                aria-expanded={open?.id === it.id}
                onClick={() => set({ proposal: open?.id === it.id ? null : it.id })}
              >
                <span className={cn('text-label font-medium', it.closing ? 'text-accent-warning' : 'text-ink-2')}>{it.label}</span>
                <span className="ml-2 font-mono text-label text-ink-3">
                  {it.slug} · proposed {day(it.recordedAt)} · {it.evidenceCount} evidence
                </span>
                <p className="mt-1 font-serif text-body-lg text-ink-1">{it.question}</p>
              </button>
              {it.judgements.length > 0 && <p className="mt-1 text-label text-ink-2">Waits on: {it.judgements.map((j) => j.question).join(' · ')}</p>}
              {open?.id === it.id && (
                <div className="mt-2">
                  <LegacyDetail questionId={it.id} />
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
