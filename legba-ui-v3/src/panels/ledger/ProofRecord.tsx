/**
 * ProofRecord — the standing evaluations (`view:proof`, the Ledger page's
 * second panel; U-11, E-3): the blind test and the ones after it.
 *
 * Rendered ONLY from a server route, `GET /v3/eval/runs`, never from a file
 * in the repository: an evaluation's results are data and the owner decides
 * which run is published (`scripts/publish_eval_run.py`, migration 0260).
 * Signed-in readers only — the route is bearer-gated like the rest of the
 * Ledger. `runs: []` is this region's designed state: "No evaluation
 * published yet", and what a published run will show. A 404 (a registry
 * older than the route) is said as such, not as "none published".
 *
 * A published run shows where its version stands in words (current; a
 * correction and why; superseded by a later version; withdrawn and why — a
 * withdrawn run stays listed with its figures as published); the conditions
 * it ran under (as-of, the questions and their classes, the grader, the
 * temperature, the gold, the design); the arms in plain words; each score
 * with its interval and sample size, and per class; every caveat; and every
 * departure from the pre-registered design. No score is coloured: the
 * numbers carry no verdict styling (the track record's restraint). A run
 * with no caveats or no deviations says so in words.
 */
import { usePanelActionHandler } from '@/lib/panelActions'
import {
  EVAL_WILL_SHOW,
  evalRecordState,
  evalResultRows,
  evalRunFacts,
  evalStanding,
  orderEvalRuns,
  type EvalRun,
} from '@/lib/ledgerProof'
import { useEvalRuns } from './ledgerQueries'
import { useLedgerView } from './ledgerView'

const VIEW_ID = 'view:proof'

function Run({ run, runs }: { run: EvalRun; runs: EvalRun[] }) {
  const rows = evalResultRows(run)
  const caveats = run.caveats ?? []
  const deviations = run.deviations ?? []
  const anyClass = rows.some((r) => r.byClass.length)
  return (
    <article
      className="space-y-2 border-t border-line pt-3"
      data-testid="proof-run"
      data-run={run.id}
      data-status={run.status ?? 'current'}
    >
      <h4 className="font-serif text-heading text-ink-1">{run.title}</h4>
      <p className="text-body text-ink-1" data-testid="proof-standing">
        {evalStanding(run, runs)}
      </p>
      {run.question && <p className="text-body text-ink-2">{run.question}</p>}
      <ul className="space-y-0.5 font-mono text-label text-ink-3" data-testid="proof-facts">
        {evalRunFacts(run).map((f) => (
          <li key={f}>{f}</li>
        ))}
      </ul>
      <div>
        <h5 className="text-label uppercase tracking-[0.06em] text-ink-3">What was compared</h5>
        <ul className="mt-1 space-y-0.5 text-body text-ink-2">
          {run.arms.map((a, i) => (
            <li key={a.id}>
              <span className="text-ink-1">{rows[i]?.arm ?? a.label}</span>
              {a.description ? ` — ${a.description}` : ''}
            </li>
          ))}
        </ul>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-t border-line text-body" data-testid="proof-results">
          <thead>
            <tr className="text-left text-label text-ink-3">
              <th className="py-1 pr-3 font-normal">Compared</th>
              <th className="py-1 pr-3 font-normal">Measure</th>
              <th className="py-1 pr-3 font-normal">Score</th>
              <th className="py-1 pr-3 font-normal">Interval</th>
              <th className="py-1 pr-3 font-normal">Sample</th>
              {anyClass && <th className="py-1 pr-3 font-normal">By class</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-t border-line align-top">
                <td className="py-1 pr-3 text-ink-1">{r.arm}</td>
                <td className="py-1 pr-3 text-ink-2">{r.metric}</td>
                <td className="py-1 pr-3 font-mono text-ink-1">{r.score}</td>
                <td className="py-1 pr-3 font-mono text-ink-2">{r.interval}</td>
                <td className="py-1 pr-3 font-mono text-ink-2">{r.n}</td>
                {anyClass && (
                  <td className="py-1 pr-3 font-mono text-label text-ink-2" data-testid="proof-by-class">
                    {r.byClass.length ? r.byClass.join(' · ') : 'none published'}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div data-testid="proof-caveats">
        <h5 className="text-label uppercase tracking-[0.06em] text-ink-3">Caveats</h5>
        {caveats.length ? (
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-body text-ink-2">
            {caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-body text-ink-2">No caveat was recorded with this run.</p>
        )}
      </div>
      <div data-testid="proof-deviations">
        <h5 className="text-label uppercase tracking-[0.06em] text-ink-3">Departures from the registered design</h5>
        {deviations.length ? (
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-body text-ink-2">
            {deviations.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-body text-ink-2">No departure from the registered design was recorded.</p>
        )}
      </div>
    </article>
  )
}

export default function ProofRecord() {
  const q = useEvalRuns()
  const state = evalRecordState(q.isPending ? undefined : q.data, { failed: q.isError })
  const runs = q.data?.runs ?? []
  const { run: openRun, set } = useLedgerView()

  usePanelActionHandler(VIEW_ID, 'show_run', (args) => {
    const id = String(args.run ?? '')
    if (!runs.some((r) => r.id === id)) return false
    set({ run: id })
    requestAnimationFrame(() => document.querySelector(`[data-run="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'start' }))
    return true
  })

  return (
    <section className="space-y-3" data-testid="proof-record" data-state={state}>
      <h3 className="text-heading font-semibold text-ink-1">Evaluations</h3>
      {state === 'reading' && <p className="text-body text-ink-3">Reading the evaluation record…</p>}
      {state === 'error' && <p className="text-body text-ink-2">The evaluation record could not be read; nothing is shown in its place.</p>}
      {state === 'unreadable' && (
        <p className="text-body text-ink-2">The evaluation record could not be read. That is not the same as having none.</p>
      )}
      {state === 'not_served' && (
        <p className="text-body text-ink-2">
          This installation does not serve the evaluation record yet. That is not the same as none being published.
        </p>
      )}
      {state === 'none_published' && (
        <div className="space-y-2" data-testid="proof-none">
          <p className="text-body-lg text-ink-1">No evaluation published yet.</p>
          <p className="text-body text-ink-2">
            Standing tests of how well this system answers — the blind test first, others after it — appear here once the owner
            publishes a run. Each published run shows:
          </p>
          <ul className="list-disc space-y-0.5 pl-5 text-body text-ink-2">
            {EVAL_WILL_SHOW.map((w) => (
              <li key={w}>{w};</li>
            ))}
          </ul>
          <p className="text-label text-ink-3">Losses are shown beside wins, with the baseline each was measured against.</p>
        </div>
      )}
      {state === 'published' && (
        <div className="space-y-4">
          {orderEvalRuns(runs, openRun).map((r) => (
            <Run key={r.id} run={r} runs={runs} />
          ))}
        </div>
      )}
    </section>
  )
}
