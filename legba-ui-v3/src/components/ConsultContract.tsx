/**
 * ConsultContract — Q-1's turn contract, drawn under a Consult answer (U-8).
 *
 * The server already writes the contract's text into the answer (the window
 * lines, the "Coverage by day" table, the competing explanations, the record
 * accounting, the "Stopped for cost" line). This block is the same contract as
 * DATA (`lib/consultContract.ts`), so a reader sees at a glance — without
 * reading the prose — which window was in force and where it came from, what
 * the record held across it and what it did not, the explanations with their
 * probabilities, what the grounding pass cut or labelled, and whether the turn
 * stopped on its cost ceiling (then the answer is a PARTIAL, said first).
 *
 * Absent is absent: a part the server did not send draws nothing (no empty
 * box), an unmeasured coverage says it was not measured and why, and a count
 * the server did not take is never printed as 0.
 */
import {
  groundingFamily,
  type ContractCoverage,
  type ContractGrounding,
  type ContractHypotheses,
  type ContractReach,
  type ContractWindow,
  type TurnContract,
} from '@/lib/consultContract'
import type { ConsultUsageReceipt } from '@/lib/consultTurnStamp'

const SOURCE_WORDS: Record<string, string> = {
  user_words: 'from your question',
  console: 'chosen in the console',
  default: 'the declared default — none was stated',
}

function fmt(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? d.toISOString().slice(0, 16).replace('T', ' ') + 'Z' : iso
}

function pct(p: number): string {
  return `${Math.round(p * 100)} %`
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

function Row({ label, children, testid }: { label: string; children: React.ReactNode; testid: string }) {
  return (
    <div className="flex min-w-0 gap-2" data-testid={testid}>
      <span className="w-24 shrink-0 uppercase tracking-[0.06em] text-ink-3">{label}</span>
      <div className="min-w-0 flex-1 text-ink-2">{children}</div>
    </div>
  )
}

function WindowRow({ w, reach }: { w: ContractWindow; reach: ContractReach | null }) {
  const reachText =
    reach == null
      ? null
      : reach.state === 'unmeasured' || reach.citedDated == null
        ? 'citations against the window: not measured'
        : `${reach.inWindow ?? '—'} of ${plural(reach.citedDated, 'dated citation')} inside the window` +
          (reach.beforeWindow ? ` · ${reach.beforeWindow} before` : '') +
          (reach.afterWindow ? ` · ${reach.afterWindow} after` : '')
  const flags: string[] = []
  if (reach?.narrowed) flags.push(`narrowed (${reach.narrowed})${reach.narrowedReason ? `: ${reach.narrowedReason}` : ''}`)
  if (reach?.widerReach) flags.push(`reached wider (${reach.widerReach})${reach.widerReason ? `: ${reach.widerReason}` : ''}`)
  return (
    <Row label="Window" testid="consult-contract-window">
      <span className="font-mono">
        {fmt(w.start)} → {fmt(w.end)}
      </span>
      {w.hours != null && <span className="font-mono"> · {w.hours} h</span>}
      <span> · {SOURCE_WORDS[w.source] ?? w.source}</span>
      {w.basis && <span className="text-ink-3"> ({w.basis})</span>}
      {w.reading && (
        <div className="text-ink-3" data-testid="consult-contract-window-reading">
          read as: {w.reading}
        </div>
      )}
      {w.anchor && !w.anchor.resolved && (
        <div className="text-accent-warning" data-testid="consult-contract-window-anchor">
          event-anchored window “{w.anchor.words}” not resolved{w.anchor.reason ? ` — ${w.anchor.reason}` : ''}; the
          window above is in force
        </div>
      )}
      {w.consoleRejected && (
        <div className="text-accent-warning" data-testid="consult-contract-window-rejected">
          the console’s window was not used: {w.consoleRejected}
        </div>
      )}
      {reachText && <div className="text-ink-3">{reachText}</div>}
      {flags.map((f) => (
        <div key={f} className="text-accent-warning">
          {f}
        </div>
      ))}
    </Row>
  )
}

function CoverageRow({ c }: { c: ContractCoverage }) {
  if (c.state !== 'measured') {
    return (
      <Row label="Coverage" testid="consult-contract-coverage">
        <span data-state="unavailable">not measured for this turn — {c.reason}</span>
      </Row>
    )
  }
  const covered = c.days.length - c.zeroDays.length
  const totals = c.days.reduce(
    (t, d) => ({ signals: t.signals + d.signals, findings: t.findings + d.findings, events: t.events + d.events }),
    { signals: 0, findings: 0, events: 0 },
  )
  return (
    <Row label="Coverage" testid="consult-contract-coverage">
      <div data-state="measured">
        {covered} of {plural(c.days.length, 'day')} hold rows · {plural(totals.signals, 'signal')} ·{' '}
        {plural(totals.findings, 'finding')} · {plural(totals.events, 'event')}
        {c.subjectTargets ? <span className="text-ink-3"> · findings and events for {c.subjectTargets.join(', ')}</span> : null}
        {c.clippedToDays != null && <span className="text-ink-3"> · counted over the last {c.clippedToDays} days</span>}
      </div>
      <div data-testid="consult-contract-gaps">
        <span className={c.zeroDays.length ? 'text-accent-warning' : 'text-ink-3'}>
          no rows: {c.zeroDays.length ? c.zeroDays.join(', ') : 'none'}
        </span>
        <span className={c.sparseDays.length ? 'text-accent-warning' : 'text-ink-3'}>
          {' '}· sparse: {c.sparseDays.length ? c.sparseDays.join(', ') : 'none'}
        </span>
        <span className={c.pausedSources.length ? 'text-accent-warning' : 'text-ink-3'}>
          {' '}· {plural(c.pausedSources.length, 'source')} paused
        </span>
        <span className={c.errorSources.length ? 'text-accent-warning' : 'text-ink-3'}>
          {' '}· {plural(c.errorSources.length, 'source')} with poll errors
        </span>
      </div>
      {(c.pausedSources.length > 0 || c.errorSources.length > 0) && (
        <div className="truncate text-ink-3" title={[...c.pausedSources.map((p) => `paused: ${p.name || p.id}`), ...c.errorSources.map((e) => `errors: ${e.id} ×${e.errors}`)].join('\n')}>
          {[...c.pausedSources.map((p) => p.name || p.id), ...c.errorSources.map((e) => `${e.id} ×${e.errors}`)].slice(0, 6).join(', ')}
        </div>
      )}
    </Row>
  )
}

function HypothesesRow({ h }: { h: ContractHypotheses }) {
  return (
    <Row label="Explanations" testid="consult-contract-hypotheses">
      {h.conforming === false && (
        <div className="text-accent-warning" data-testid="consult-contract-nonconforming">
          non-conforming after {h.repair === 'not_needed' ? 'no repair' : 'one repair'}
          {h.problems.length ? ` — ${h.problems.join('; ')}` : ''}
        </div>
      )}
      <ol className="list-decimal space-y-0.5 pl-4">
        {h.items.map((it, i) => (
          <li key={i} data-testid="consult-contract-hypothesis">
            <span className="text-ink-1">{it.explanation || '(no explanation given)'}</span>
            <span className="font-mono"> · {it.p != null ? `P ${pct(it.p)}` : 'P not stated'}</span>
            {(it.forRefs.length > 0 || it.againstRefs.length > 0) && (
              <span className="text-ink-3">
                {' '}
                · {it.forRefs.length} for, {it.againstRefs.length} against
              </span>
            )}
            {it.observable && (
              <div className="text-ink-3">
                test: {it.observable}
                {it.by ? `, by ${it.by}` : ''}
                {it.pObservable != null ? ` · chance it is observed ${pct(it.pObservable)} (a different quantity from P)` : ''}
              </div>
            )}
          </li>
        ))}
      </ol>
      <div className="text-ink-3">something else: {h.somethingElseP != null ? `P ${pct(h.somethingElseP)}` : 'not stated'}</div>
    </Row>
  )
}

function GroundingRow({ g, mode }: { g: ContractGrounding; mode: string | null }) {
  const cut = groundingFamily(g, 'cut')
  const labelled = groundingFamily(g, 'labelled')
  const moved = groundingFamily(g, 'moved')
  const kept = groundingFamily(g, 'kept')
  return (
    <Row label="Grounding" testid="consult-contract-grounding">
      <span>{(mode ?? g.mode) === 'record_only' ? 'record only' : 'cite rows'}</span>
      {g.applied ? (
        <span>
          {' '}
          · applied: {kept} kept · <span className={cut ? 'text-accent-warning' : ''}>{cut} cut</span> · {labelled} labelled
          {moved ? ` · ${moved} moved under “not from the record”` : ''}
        </span>
      ) : (
        <span className="text-ink-3">
          {' '}
          · {g.notAppliedReason ? `not applied: ${g.notAppliedReason}` : `counted, not enforced: ${kept} kept, ${cut + labelled} without a resolving row`}
        </span>
      )}
      {g.numbersUnsupported != null && g.numbersUnsupported > 0 && (
        <span className="text-accent-warning"> · {plural(g.numbersUnsupported, 'number')} no cited row states</span>
      )}
      {g.numberRowsClipped != null && g.numberRowsClipped > 0 && (
        <span className="text-accent-warning" data-testid="consult-contract-number-unchecked">
          {' '}
          · number check read {plural(g.numberRowsClipped, 'long cited row')} only in part
          {g.numberCharsUnchecked != null ? ` (${g.numberCharsUnchecked.toLocaleString()} characters unchecked)` : ''}
        </span>
      )}
      {g.verifierState && (
        <span className="text-ink-3">
          {' '}
          · verifier {g.verifierState === 'judged' ? `listed ${g.verifierItems ?? '—'} unsupported` : g.verifierState}
        </span>
      )}
    </Row>
  )
}

function CostRow({ stop, usage }: { stop: string | null; usage: ConsultUsageReceipt | null | undefined }) {
  const u = (usage ?? null) as (ConsultUsageReceipt & { max_cost_usd?: unknown; turn_ceiling_usd?: unknown }) | null
  const spent = typeof u?.cost_usd === 'number' ? `$${u.cost_usd.toFixed(2)}` : null
  const ceiling =
    typeof u?.turn_ceiling_usd === 'number'
      ? `turn ceiling $${u.turn_ceiling_usd.toFixed(2)}`
      : typeof u?.max_cost_usd === 'number'
        ? `run ceiling $${u.max_cost_usd.toFixed(2)}`
        : null
  if (!stop && !spent && !ceiling) return null
  return (
    <Row label="Cost" testid="consult-contract-cost">
      {stop ? (
        <span className="text-accent-warning" data-state="stopped">
          stopped on the cost ceiling — partial answer
        </span>
      ) : (
        <span data-state="ran">ran to its end</span>
      )}
      {spent && <span className="font-mono"> · {spent}</span>}
      {ceiling && <span className="font-mono text-ink-3"> · {ceiling}</span>}
      {stop && <div className="text-ink-3">{stop.replace(/^>\s*/, '').replace(/\*\*/g, '')}</div>}
    </Row>
  )
}

export function ConsultContractBlock({
  contract,
  usage,
}: {
  contract: TurnContract
  usage?: ConsultUsageReceipt | null
}) {
  const parts = [contract.window, contract.coverage, contract.hypotheses, contract.grounding, contract.costStop]
  if (parts.every((p) => p == null)) return null
  return (
    <div
      className="space-y-1 rounded border border-line bg-surface-200 px-2 py-1.5 text-label"
      data-testid="consult-contract"
      data-armed={contract.armed ? '1' : '0'}
    >
      {contract.costStop && (
        <div
          className="rounded border border-accent-warning/50 bg-accent-warning/10 px-2 py-1 text-accent-warning"
          data-testid="consult-contract-partial"
        >
          Partial answer: this turn stopped drilling at its cost ceiling and answered from what it had gathered.
        </div>
      )}
      {contract.recovery && (
        <Row label="Recovered" testid="consult-contract-recovery">
          written over an earlier turn’s evidence · window{' '}
          {contract.recovery.windowFrom === 'original_turn' ? 'of the original turn' : 're-read from the question'} ·
          coverage {contract.recovery.coverageFrom === 'original_turn' ? 'of the original turn' : 'measured at recovery'}
          {contract.recovery.originalCostStop && (
            <span className="text-accent-warning"> · the original turn stopped drilling for cost</span>
          )}
        </Row>
      )}
      {contract.window && <WindowRow w={contract.window} reach={contract.reach} />}
      {contract.coverage && <CoverageRow c={contract.coverage} />}
      {contract.hypotheses && contract.hypotheses.items.length > 0 && <HypothesesRow h={contract.hypotheses} />}
      {contract.hypotheses && contract.hypotheses.items.length === 0 && contract.hypotheses.conforming === false && (
        <HypothesesRow h={contract.hypotheses} />
      )}
      {contract.grounding && <GroundingRow g={contract.grounding} mode={contract.groundingMode} />}
      {(contract.armed || contract.costStop) && <CostRow stop={contract.costStop} usage={usage} />}
    </div>
  )
}
