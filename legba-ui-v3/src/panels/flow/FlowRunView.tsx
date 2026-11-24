/**
 * Flow (N-J v0) — one run, overlaid: the steps the run RECORDED (observed,
 * not declared), its model and tool calls, and the gate verdicts stored on
 * its output rows. Sizes and ids only; a body is fetched one at a time,
 * capped server-side, on request.
 */
import { useState } from 'react'
import { cn } from '@/lib/cn'
import type { FlowBodyPart, FlowRunDetail, StepSummary } from './flowTypes'
import { fetchRunBody, useFlowRun } from './useFlowData'
import { CopyButton, KV, Mono, Section } from './flowBits'

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

export function fmtMs(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n < 1000 ? `${n} ms` : `${(n / 1000).toFixed(1)} s`
}

function stepExtras(s: StepSummary): string {
  const skip = new Set(['index', 'bytes', 'phase', 'kind'])
  return Object.entries(s)
    .filter(([k, v]) => !skip.has(k) && (typeof v !== 'object' || v === null))
    .slice(0, 6)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(' · ')
}

function BodyFetch({ runId, part, index }: { runId: string; part: string; index: number | null }) {
  const [body, setBody] = useState<FlowBodyPart | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  if (body) {
    return (
      <div className="mt-1">
        <div className="flex items-center gap-2 text-[11px] text-ink-3">
          <span>{fmtBytes(body.bytes)}{body.truncated ? ` — cut at ${fmtBytes(body.cap_bytes)}` : ''}</span>
          <CopyButton text={body.text} />
        </div>
        <Mono text={body.text} maxH="max-h-64" />
      </div>
    )
  }
  return (
    <button
      type="button"
      className="text-[11px] text-accent-info hover:underline disabled:opacity-50"
      disabled={busy}
      onClick={() => {
        setBusy(true)
        fetchRunBody(runId, part, index)
          .then(setBody)
          .catch((e: unknown) => setErr(String(e)))
          .finally(() => setBusy(false))
      }}
    >
      {err ? `fetch failed: ${err}` : busy ? 'fetching…' : 'fetch body'}
    </button>
  )
}

export function RunDetailView({ run }: { run: FlowRunDetail }) {
  return (
    <div className="space-y-3" data-testid="flow-run-detail">
      <Section title="Run">
        <KV k="run" v={run.run_id} mono />
        <KV k="status" v={run.status ?? '—'} />
        <KV k="target" v={run.target_id ?? 'unbound'} />
        <KV k="trigger" v={run.trigger ?? '—'} />
        <KV k="started" v={run.started_at ?? '—'} />
        <KV k="duration" v={fmtMs(run.duration_ms)} />
        <KV k="tokens" v={String(run.tokens)} />
        <KV k="prompt stored" v={run.prompt_chars === null || run.prompt_chars === undefined ? 'none' : `${run.prompt_chars} chars`} />
        <KV k="inputs" v={`${run.input_refs.count} rows${run.input_refs.count > run.input_refs.listed ? ` (first ${run.input_refs.listed} listed)` : ''}`} />
        <KV k="outputs" v={`${run.output_refs.count} rows`} />
        {run.error ? <Mono text={JSON.stringify(run.error, null, 1)} maxH="max-h-32" /> : null}
      </Section>
      <Section title={`Steps — ${run.sequence_label}`}>
        <ol className="space-y-1" data-testid="flow-run-steps">
          {run.steps.map((s) => (
            <li key={s.index} className="border-l-2 border-line pl-2">
              <div className="flex items-baseline gap-2 text-body">
                <span className="text-ink-3 tabular-nums">{s.index + 1}.</span>
                <span className="text-ink-1">{String(s.phase ?? '—')}</span>
                <span className="text-ink-2">{String(s.kind ?? '')}</span>
                <span className="ml-auto text-[11px] text-ink-3">{fmtBytes(s.bytes)}</span>
              </div>
              <div className="text-[11px] text-ink-3 truncate">{stepExtras(s)}</div>
              <BodyFetch runId={run.run_id} part="step" index={s.index} />
            </li>
          ))}
        </ol>
      </Section>
      <Section title={`Model calls (${run.llm_calls.length})`}>
        <table className="w-full text-[11px]">
          <thead className="text-ink-3 text-left">
            <tr><th>#</th><th>leg</th><th>model</th><th>status</th><th>dur</th><th>tok in/out</th><th>finish</th><th /></tr>
          </thead>
          <tbody className="text-ink-2">
            {run.llm_calls.map((c) => (
              <tr key={c.index} className={cn(c.finish_reason === 'length' && 'text-accent-warning')}>
                <td>{c.index + 1}</td>
                <td>{String(c.leg ?? 'main')}</td>
                <td>{String(c.model ?? '—')}</td>
                <td>{String(c.status ?? '—')}</td>
                <td>{fmtMs(c.duration_ms as number | null)}</td>
                <td>{String(c.prompt_tokens ?? '—')}/{String(c.completion_tokens ?? '—')}</td>
                <td>{String(c.finish_reason ?? '—')}</td>
                <td><BodyFetch runId={run.run_id} part="llm_call" index={c.index} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      {run.tool_calls.length > 0 && (
        <Section title={`Tool calls (${run.tool_calls.length})`}>
          <table className="w-full text-[11px]">
            <thead className="text-ink-3 text-left">
              <tr><th>#</th><th>tool</th><th>status</th><th>dur</th><th>args</th><th>result</th><th /></tr>
            </thead>
            <tbody className="text-ink-2">
              {run.tool_calls.map((t) => (
                <tr key={t.index}>
                  <td>{t.index + 1}</td>
                  <td>{String(t.pack ?? '')}.{String(t.name ?? '')}</td>
                  <td>{String(t.status ?? t.outcome ?? '—')}</td>
                  <td>{fmtMs(t.duration_ms as number | null)}</td>
                  <td>{fmtBytes(t.args_bytes as number | null)}</td>
                  <td>{t.result_bytes === undefined ? 'not stored' : fmtBytes(t.result_bytes as number)}</td>
                  <td><BodyFetch runId={run.run_id} part="tool_call" index={t.index} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
      <Section title="Output rows and gate verdicts">
        {run.outputs.length === 0 ? (
          <p className="text-body text-ink-3">This run recorded no output row.</p>
        ) : (
          <ul className="space-y-2" data-testid="flow-run-outputs">
            {run.outputs.map((o) => (
              <li key={o.id} className="text-body">
                <div className="text-ink-1">{o.table} · {o.kind} · <span className="font-mono text-[11px]">{o.id}</span></div>
                <div className="text-[11px] text-ink-3">{o.schema_uri ?? 'no schema_uri'} · {fmtBytes(o.bytes)}</div>
                {o.honesty_flags && o.honesty_flags.length > 0 && (
                  <div className="text-[11px] text-ink-2">honesty flags: {o.honesty_flags.join(', ')}</div>
                )}
                {Object.keys(o.gates).length === 0 && o.judge_critiques.length === 0 ? (
                  <div className="text-[11px] text-ink-3">no gate verdict stored on this row</div>
                ) : (
                  <Mono text={JSON.stringify({ gates: o.gates, judge_critiques: o.judge_critiques }, null, 1)} maxH="max-h-40" />
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  )
}

export function FlowRunView({ runId }: { runId: string }) {
  const q = useFlowRun(runId)
  if (q.isLoading) return <p className="text-body text-ink-3">Loading run…</p>
  if (q.isError || !q.data) return <p className="text-body text-accent-critical">Could not load run {runId}.</p>
  return <RunDetailView run={q.data} />
}
