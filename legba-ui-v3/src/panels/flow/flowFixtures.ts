/**
 * Test fixtures for the Flow panel, shaped from a live `/v3/flow/graph` and
 * `/v3/flow/node/analyst:escalation` answer (2026-11-13), cut down. Not
 * imported by any render path.
 */
import type { FlowGraphPayload, FlowNodeDetail, FlowNodePayload } from './flowTypes'

function node(id: string, kind: string, group: string, extra: Partial<FlowNodePayload> = {}): FlowNodePayload {
  return {
    id, descriptor_id: id.split(':').slice(1).join(':'), family: kind, kind, subkind: null,
    method_kind: null, group, label: id.split(':').slice(1).join(':'), state: 'active',
    version: 'a'.repeat(64), cadence: null, model_route: {}, target_scope: null, fields: null,
    undeclared: [], ...extra,
  }
}

export function graphFixture(nSources = 25): FlowGraphPayload {
  const sources = Array.from({ length: nSources }, (_, i) =>
    node(`source:source.s${String(i).padStart(2, '0')}`, 'source', 'sources', { subkind: 'rss' }))
  const nodes: FlowNodePayload[] = [
    ...sources,
    node('target:country_g20_cn', 'target', 'targets'),
    node('target:country_watch_ir', 'target', 'targets'),
    node('analyst:escalation', 'analyst', 'analyst:inline_target', {
      method_kind: 'llm_planner', cadence: { schedule: '0 7,19 * * *' },
      model_route: { primary: { component_id: 'llm.primary.openai_compat', model: 'fast' } },
    }),
    node('analyst:country_composition', 'analyst', 'analyst:meta_findings_synthesizer', { method_kind: 'llm_planner' }),
    node('analyst:lens_left', 'analyst', 'analyst:journal_assessor', { state: 'paused' }),
    node('pack:escalate_finding', 'pack', 'packs', { tools: [{ name: 'escalate' }] }),
    node('store:grounding:substrate', 'store', 'stores'),
    node('wiring:w1', 'unknown', 'unknown:wiring'),
  ]
  const edges = [
    ...sources.map((s) => ({
      id: `${s.id}->target:country_g20_cn#target.sources[0].source_selector`, source: s.id,
      target: 'target:country_g20_cn', basis: 'target.sources[0].source_selector', relation: 'feeds',
    })),
    { id: 'target:country_g20_cn->analyst:escalation#subscription.targets.predicate', source: 'target:country_g20_cn', target: 'analyst:escalation', basis: 'subscription.targets.predicate', relation: 'binds' },
    { id: 'target:country_watch_ir->analyst:escalation#subscription.targets.predicate', source: 'target:country_watch_ir', target: 'analyst:escalation', basis: 'subscription.targets.predicate', relation: 'binds' },
    { id: 'analyst:escalation->analyst:country_composition#subscription.other_analysts[0]', source: 'analyst:escalation', target: 'analyst:country_composition', basis: 'subscription.other_analysts[0]', relation: 'subscribes' },
    { id: 'analyst:escalation->pack:escalate_finding#action_packs[0]', source: 'analyst:escalation', target: 'pack:escalate_finding', basis: 'action_packs[0]', relation: 'offered_tools' },
    { id: 'store:grounding:substrate->analyst:escalation#grounding.sources[0]', source: 'store:grounding:substrate', target: 'analyst:escalation', basis: 'grounding.sources[0]', relation: 'grounds' },
  ]
  return {
    nodes, edges, scope: 'all',
    report: {
      nodes: nodes.length, edges: edges.length, edges_by_basis: { 'target.sources': nSources },
      nodes_by_kind: {}, unresolved_refs: [], predicate_errors: [],
      inexpressible: [{ relation: 'analyst → store written (tables, side tables, indexes)', nodes: 3 }],
    },
  }
}

export const nodeDetailFixture: FlowNodeDetail = {
  node: {
    ...graphFixture().nodes.find((n) => n.id === 'analyst:escalation')!,
    fields: { method: { system_prompt: 'WHO YOU ARE. You are the ESCALATION RISK unit', max_tokens: 4096 } },
    undeclared: [
      'undeclared: the tables/stores this analyst writes are not declared (identity.type_signature.output_type names a Python type, not a store)',
      'undeclared: the step sequence inside a run lives in code; the inspector shows the steps observed in the latest trace (observed, not declared)',
    ],
  },
  edges_in: [{ id: 'e1', source: 'target:country_g20_cn', target: 'analyst:escalation', basis: 'subscription.targets.predicate', relation: 'binds' }],
  edges_out: [],
  versions: [
    { version: 'a'.repeat(64), state: 'active', is_head: true, created_at: '2026-11-12T00:00:00Z' },
    { version: 'b'.repeat(64), state: 'retired', is_head: false, created_at: '2026-11-01T00:00:00Z' },
  ],
  prompt: {
    declared: { system_prompt: 'WHO YOU ARE. You are the ESCALATION RISK unit', prompt_module: null, bounded_question: 'What is the escalation risk?', note: 'template note' },
    rendered: {
      run_id: 'r1', run_started_at: '2026-11-13T06:57:35Z', text: '[SYSTEM]\nYou are a senior all-source intelligence analyst.',
      chars_stored: 52, truncated: false, tail: null, total_chars: 52, sha256: 'p'.repeat(64),
      basis: 'analyst_traces.prompt_rendered of the most recent run that recorded one',
    },
    fresh_render: 'not produced: a fresh render needs the runtime',
  },
  output_contract: { type_signature: { output_type: 'legba.runtime.Finding' }, outputs: [], observed_rows: [], note: 'contract note' },
  params: {
    configured: { temperature: 1, max_tokens: 4096, top_p: null, routes: { primary: 'llm.primary.openai_compat' } },
    sent: [{
      role: 'primary', component_id: 'llm.primary.openai_compat', subprovider: 'vllm', model: 'fast',
      temperature_sent: 1, temperature_basis: 'descriptor method.llm.temperature, passed through',
      max_tokens_sent: null, max_tokens_basis: 'NOT SENT: vllm.py sends max_tokens only with send_max_tokens=True',
      top_p_sent: null, top_p_basis: '', thinking_sent: null, thinking_basis: '',
    }],
    hard_coded: [],
    cannot_see: ['LEGBA_LLM_SEND_MAX_TOKENS in the runtime container'],
    observed: { label: 'observed', calls: [] },
  },
  recent_runs: [{
    run_id: 'r1', status: 'success', started_at: '2026-11-13T06:57:35Z', duration_ms: 22900,
    tokens: 27929, llm_calls: 4, tool_calls: 0, finish_reasons: { stop: 4 }, target_id: 'country_g20_cn',
  }],
  observed_steps: { label: 'observed, not declared', run_id: 'r1', status: 'success', steps: [
    { index: 0, bytes: 30, phase: 'wake', kind: 'envelope' },
    { index: 1, bytes: 900, phase: 'reason', kind: 'llm_call' },
  ] },
}
