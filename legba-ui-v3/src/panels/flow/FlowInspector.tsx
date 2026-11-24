/**
 * Flow (N-J v0) — the node inspector. READ-ONLY: every field, the prompt
 * (declared and as last rendered), the output contract, sampling configured
 * vs sent, recent runs, versions with a compare view, and what the registry
 * cannot say about this node ("undeclared"). There is no edit affordance:
 * nothing here writes, and v0 has no draft store.
 */
import { useState } from 'react'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { cn } from '@/lib/cn'
import type { FlowEdgePayload, FlowNodeDetail, FlowParams, FlowPrompt, RunSummary } from './flowTypes'
import { useFlowNode } from './useFlowData'
import { CopyButton, KV, Mono, Section, asText } from './flowBits'
import { FlowCompare } from './FlowCompare'
import { FlowRunView, fmtMs } from './FlowRunView'

export const INSPECTOR_TABS: readonly PanelTabDef[] = [
  { id: 'definition', label: 'Definition' },
  { id: 'prompt', label: 'Prompt' },
  { id: 'contract', label: 'Contract' },
  { id: 'params', label: 'Sampling' },
  { id: 'runs', label: 'Runs' },
  { id: 'versions', label: 'Versions' },
  { id: 'undeclared', label: 'Undeclared' },
]

function EdgeList({ title, edges, end }: { title: string; edges: FlowEdgePayload[]; end: 'source' | 'target' }) {
  if (edges.length === 0) return <KV k={title} v="none declared" />
  const shown = edges.slice(0, 40)
  return (
    <Section title={`${title} (${edges.length})`}>
      <ul className="text-[11px] space-y-0.5">
        {shown.map((e) => (
          <li key={e.id} className="flex gap-2 min-w-0">
            <span className="text-ink-1 truncate">{e[end]}</span>
            <span className="text-ink-3 truncate">via {e.basis}</span>
          </li>
        ))}
        {edges.length > shown.length && <li className="text-ink-3">… {edges.length - shown.length} more</li>}
      </ul>
    </Section>
  )
}

function DefinitionTab({ d }: { d: FlowNodeDetail }) {
  const n = d.node
  const fieldsText = asText(n.fields)
  return (
    <div className="space-y-3">
      <Section title="Identity">
        <KV k="node" v={n.id} mono />
        <KV k="kind" v={`${n.kind}${n.subkind ? ` · ${n.subkind}` : ''}`} />
        <KV k="group" v={n.group} />
        <KV k="lifecycle" v={n.state ?? '—'} />
        <KV k="version hash" v={n.version ?? '—'} mono />
        {n.method_kind ? <KV k="method" v={n.method_kind} /> : null}
        {n.cadence ? <KV k="cadence" v={asText(n.cadence)} mono /> : null}
        {n.target_scope ? <KV k="target scope" v={asText(n.target_scope)} mono /> : null}
        {n.model_route && Object.keys(n.model_route).length > 0 ? (
          <KV
            k="model route"
            v={Object.entries(n.model_route).map(([role, r]) => `${role}: ${r.component_id} → ${r.model ?? 'model unknown'}`).join('; ')}
          />
        ) : null}
      </Section>
      {n.tools && n.tools.length > 0 && (
        <Section title={`Tools (${n.tools.length})`}>
          <ul className="text-body text-ink-1">{n.tools.map((t, i) => <li key={i}>{t.name ?? '(unnamed)'}</li>)}</ul>
        </Section>
      )}
      <EdgeList title="Reads from" edges={d.edges_in} end="source" />
      <EdgeList title="Feeds" edges={d.edges_out} end="target" />
      <Section title="Every field (the descriptor body, verbatim)">
        <div className="flex justify-end"><CopyButton text={fieldsText} /></div>
        <Mono text={fieldsText} testId="flow-fields" />
      </Section>
    </div>
  )
}

function PromptTab({ p }: { p: FlowPrompt | undefined }) {
  if (!p) return <p className="text-body text-ink-3">This node has no prompt: only analysts carry one.</p>
  const r = p.rendered
  return (
    <div className="space-y-3">
      <Section title="As last rendered (from a stored run)">
        {r && r.text ? (
          <>
            <p className="text-[11px] text-ink-3">{r.basis}</p>
            <KV k="run" v={r.run_id} mono />
            <KV k="at" v={r.run_started_at ?? '—'} />
            <KV k="size" v={`${r.chars_stored} chars stored${r.total_chars ? ` of ${r.total_chars}` : ''}${r.truncated ? ' — the middle is not stored' : ''}`} />
            <KV k="sha256" v={r.sha256 ?? '—'} mono />
            <div className="flex justify-end"><CopyButton text={r.text} /></div>
            <Mono text={r.text} testId="flow-prompt-rendered" />
            {r.tail ? (
              <>
                <h4 className="text-label text-ink-3">Stored tail (the last part of the same prompt)</h4>
                <Mono text={r.tail} maxH="max-h-48" />
              </>
            ) : null}
          </>
        ) : (
          <p className="text-body text-ink-3">No stored run of this analyst recorded a rendered prompt.</p>
        )}
      </Section>
      <Section title="As declared">
        <p className="text-[11px] text-ink-3">{p.declared.note}</p>
        {p.declared.bounded_question ? <KV k="question" v={p.declared.bounded_question} /> : null}
        {p.declared.prompt_module ? <KV k="prompt module" v={p.declared.prompt_module} mono /> : null}
        {p.declared.system_prompt ? (
          <>
            <div className="flex justify-end"><CopyButton text={p.declared.system_prompt} /></div>
            <Mono text={p.declared.system_prompt} testId="flow-prompt-declared" />
          </>
        ) : (
          <p className="text-body text-ink-3">The descriptor holds no prompt text of its own.</p>
        )}
      </Section>
      <p className="text-[11px] text-ink-3">{p.fresh_render}</p>
    </div>
  )
}

function ParamsTab({ p }: { p: FlowParams | undefined }) {
  if (!p) return <p className="text-body text-ink-3">Sampling applies to analysts only.</p>
  const c = p.configured
  return (
    <div className="space-y-3">
      <Section title="Configured (descriptor)">
        <KV k="temperature" v={c.temperature === null ? 'not set' : String(c.temperature)} />
        <KV k="max_tokens" v={c.max_tokens === null ? 'not set' : String(c.max_tokens)} />
        <KV k="top_p" v={c.top_p === null ? 'not set' : String(c.top_p)} />
        <KV k="routes" v={Object.entries(c.routes).map(([k, v]) => `${k}: ${v}`).join('; ') || 'none'} />
      </Section>
      <Section title="Sent (from the code that builds the request)">
        {p.sent.length === 0 ? (
          <p className="text-body text-ink-3">No model route is declared on this descriptor.</p>
        ) : (
          <table className="w-full text-[11px]" data-testid="flow-params-sent">
            <thead className="text-left text-ink-3"><tr><th>route</th><th>temperature</th><th>max_tokens</th></tr></thead>
            <tbody>
              {p.sent.map((s) => (
                <tr key={s.role} className="align-top">
                  <td className="pr-2 text-ink-1">{s.role}<div className="text-ink-3">{s.subprovider} · {s.model ?? 'model unknown'}</div></td>
                  <td className="pr-2 text-ink-1">{s.temperature_sent === null ? 'not sent' : String(s.temperature_sent)}<div className="text-ink-3">{s.temperature_basis}</div></td>
                  <td className="text-ink-1">{s.max_tokens_sent === null ? 'not sent' : String(s.max_tokens_sent)}<div className="text-ink-3">{s.max_tokens_basis}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {p.hard_coded.length > 0 && (
          <ul className="text-[11px] text-ink-2">
            {p.hard_coded.map((h, i) => (
              <li key={i}>hard-coded: {h.leg} sends temperature {h.temperature_sent} ({h.file})</li>
            ))}
          </ul>
        )}
      </Section>
      {p.observed && (
        <Section title={p.observed.label}>
          {p.observed.calls.length === 0 ? (
            <p className="text-body text-ink-3">No model call in the recent runs.</p>
          ) : (
            <table className="w-full text-[11px]">
              <thead className="text-left text-ink-3"><tr><th>leg</th><th>model</th><th>calls</th><th>finish</th><th>max out</th></tr></thead>
              <tbody className="text-ink-2">
                {p.observed.calls.map((o, i) => (
                  <tr key={i}>
                    <td>{o.leg}</td><td>{o.model ?? '—'}</td><td>{o.calls}</td>
                    <td>{Object.entries(o.finish_reasons).map(([k, v]) => `${k} ${v}`).join(', ')}</td>
                    <td>{o.max_completion_tokens}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>
      )}
      <Section title="Not visible from here">
        <ul className="text-[11px] text-ink-3 list-disc pl-4">{p.cannot_see.map((s, i) => <li key={i}>{s}</li>)}</ul>
      </Section>
    </div>
  )
}

function RunsTab({ d, runId, onRun }: { d: FlowNodeDetail; runId: string | null; onRun: (id: string | null) => void }) {
  const runs: RunSummary[] = d.recent_runs ?? []
  if (!d.recent_runs) return <p className="text-body text-ink-3">Runs are recorded for analysts only.</p>
  return (
    <div className="space-y-3">
      <Section title={`Recent runs (${runs.length})`}>
        {runs.length === 0 ? (
          <p className="text-body text-ink-3">No run of this analyst is stored.</p>
        ) : (
          <table className="w-full text-[11px]" data-testid="flow-runs">
            <thead className="text-left text-ink-3"><tr><th>started</th><th>status</th><th>target</th><th>dur</th><th>tokens</th><th>finish</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.run_id}
                  className={cn('cursor-pointer text-ink-2 hover:bg-surf-3', runId === r.run_id && 'bg-surf-3 text-ink-1')}
                  onClick={() => onRun(runId === r.run_id ? null : r.run_id)}
                  data-testid={`flow-run-row-${r.run_id}`}
                >
                  <td>{r.started_at?.slice(0, 19).replace('T', ' ') ?? '—'}</td>
                  <td className={cn(r.status !== 'success' && 'text-accent-critical')}>{r.status ?? '—'}</td>
                  <td>{r.target_id ?? '—'}</td>
                  <td>{fmtMs(r.duration_ms)}</td>
                  <td>{r.tokens === null ? '—' : r.tokens}</td>
                  <td>{Object.entries(r.finish_reasons).map(([k, v]) => `${k} ${v}`).join(', ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      {runId ? (
        <FlowRunView runId={runId} />
      ) : d.observed_steps ? (
        <Section title={`Internal sequence — ${d.observed_steps.label} (latest run)`}>
          <ol className="text-[11px] text-ink-2 space-y-0.5" data-testid="flow-observed-steps">
            {d.observed_steps.steps.map((s) => (
              <li key={s.index}>{s.index + 1}. {String(s.phase ?? '—')} · {String(s.kind ?? '')}</li>
            ))}
          </ol>
        </Section>
      ) : null}
    </div>
  )
}

export function InspectorBody({ d, runId, onRun }: { d: FlowNodeDetail; runId: string | null; onRun: (id: string | null) => void }) {
  const [tab, setTab] = useState('definition')
  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="flow-inspector">
      <div className="border-b border-line px-3 py-2">
        <div className="text-heading text-ink-1 truncate">{d.node.label}</div>
        <div className="text-[11px] text-ink-3 truncate">{d.node.id} · {d.node.state ?? 'no lifecycle'} · read-only</div>
      </div>
      <div className="px-2 pt-1">
        <PanelTabStrip tabs={INSPECTOR_TABS} active={tab} onChange={setTab} ariaLabel="Inspector sections" testIdPrefix="flow-inspector-tab" />
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {tab === 'definition' && <DefinitionTab d={d} />}
        {tab === 'prompt' && <PromptTab p={d.prompt} />}
        {tab === 'contract' && (
          d.output_contract ? (
            <div className="space-y-3">
              <p className="text-[11px] text-ink-3">{d.output_contract.note}</p>
              <Section title="Type signature"><Mono text={asText(d.output_contract.type_signature)} maxH="max-h-40" /></Section>
              <Section title="Output bindings"><Mono text={asText(d.output_contract.outputs)} maxH="max-h-40" /></Section>
              <Section title="Rows the latest run wrote">
                {d.output_contract.observed_rows.length === 0 ? (
                  <p className="text-body text-ink-3">The latest run wrote no row to analyst_outputs or journal_entries.</p>
                ) : (
                  <ul className="text-[11px] text-ink-2">
                    {d.output_contract.observed_rows.map((r) => <li key={r.id}>{r.table} · {r.kind} · {r.schema_uri ?? 'no schema_uri'}</li>)}
                  </ul>
                )}
              </Section>
            </div>
          ) : <p className="text-body text-ink-3">Output contracts are declared on analysts only.</p>
        )}
        {tab === 'params' && <ParamsTab p={d.params} />}
        {tab === 'runs' && <RunsTab d={d} runId={runId} onRun={onRun} />}
        {tab === 'versions' && (
          d.versions ? <FlowCompare nodeId={d.node.id} versions={d.versions} />
            : <p className="text-body text-ink-3">This node is derived, not a stored descriptor: it has no versions.</p>
        )}
        {tab === 'undeclared' && (
          <Section title="What the registry cannot say about this node">
            {d.node.undeclared.length === 0 ? (
              <p className="text-body text-ink-3">Nothing listed.</p>
            ) : (
              <ul className="list-disc pl-4 text-body text-ink-2" data-testid="flow-undeclared">
                {d.node.undeclared.map((u, i) => <li key={i}>{u}</li>)}
              </ul>
            )}
          </Section>
        )}
      </div>
    </div>
  )
}

export function FlowInspector({ nodeId, runId, onRun }: { nodeId: string; runId: string | null; onRun: (id: string | null) => void }) {
  const q = useFlowNode(nodeId)
  if (q.isLoading) return <p className="p-3 text-body text-ink-3">Loading {nodeId}…</p>
  if (q.isError || !q.data) return <p className="p-3 text-body text-accent-critical">Could not load {nodeId}.</p>
  return <InspectorBody key={nodeId} d={q.data} runId={runId} onRun={onRun} />
}
