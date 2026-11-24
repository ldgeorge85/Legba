/**
 * Flow (N-J v0) — the wire shapes of `/api/v1/v3/flow/*`.
 *
 * Mirrors `src/legba/data/registry/flow_{graph,api,runs,params}.py`. Every
 * field the backend may omit is optional; `fields` is the descriptor body
 * verbatim (any JSON — an unknown family or a non-object body survives).
 */

export type FlowNodeKind =
  | 'source'
  | 'target'
  | 'analyst'
  | 'pack'
  | 'collection'
  | 'store'
  | 'sink'
  | 'unresolved'
  | 'unknown'

export interface ModelRoute {
  component_id: string
  model?: string | null
  component_state?: string | null
}

export interface FlowNodePayload {
  id: string
  descriptor_id: string | null
  family: string | null
  kind: FlowNodeKind | string
  subkind?: string | null
  method_kind?: string | null
  group: string
  label: string
  state: string | null
  version: string | null
  cadence?: Record<string, unknown> | null
  model_route?: Record<string, ModelRoute>
  target_scope?: Record<string, unknown> | null
  fields: unknown
  undeclared: string[]
  tools?: Array<{ name?: string | null; impl?: string | null; async_job?: boolean | null }>
  /** Set by a scoped graph: true for a neighbour shown as context. */
  context?: boolean
}

export interface FlowEdgePayload {
  id: string
  source: string
  target: string
  basis: string
  relation: string
  data_types?: string[]
  time_window?: string | null
  enabled?: boolean
}

export interface InexpressibleRow {
  relation: string
  nodes: number
}

export interface FlowReport {
  nodes: number
  edges: number
  edges_by_basis: Record<string, number>
  nodes_by_kind: Record<string, number>
  unresolved_refs: Array<{ id: string; via: string }>
  predicate_errors: Array<Record<string, unknown>>
  inexpressible: InexpressibleRow[]
  [k: string]: unknown
}

export interface FlowGraphPayload {
  nodes: FlowNodePayload[]
  edges: FlowEdgePayload[]
  report: FlowReport
  scope: string
  generated_at?: string
  fields_included?: boolean
  timings_ms?: Record<string, number>
}

export interface RunSummary {
  run_id: string
  analyst_version?: string | null
  target_id?: string | null
  trigger?: string | null
  status: string | null
  started_at: string | null
  duration_ms: number | null
  tokens: number | null
  llm_calls: number | null
  tool_calls: number | null
  finish_reasons: Record<string, number>
}

export interface StepSummary {
  index: number
  bytes: number
  phase?: string
  kind?: string
  [k: string]: unknown
}

export interface SentRoute {
  role: string
  component_id: string
  subprovider: string
  model?: string | null
  component_max_tokens?: number | null
  temperature_sent: number | null
  temperature_basis: string
  max_tokens_sent: number | null
  max_tokens_basis: string
  top_p_sent: number | null
  top_p_basis: string
  thinking_sent: unknown
  thinking_basis: string
}

export interface ObservedCall {
  component_id: string | null
  model: string | null
  leg: string
  calls: number
  finish_reasons: Record<string, number>
  max_completion_tokens: number
  max_prompt_tokens: number
}

export interface FlowParams {
  configured: {
    temperature: number | null
    max_tokens: number | null
    top_p: number | null
    routes: Record<string, string>
    [k: string]: unknown
  }
  sent: SentRoute[]
  hard_coded: Array<{ leg: string; temperature_sent: number; file: string }>
  cannot_see: string[]
  observed?: { label: string; calls: ObservedCall[] }
}

export interface FlowPrompt {
  declared: {
    system_prompt: string | null
    prompt_module: string | null
    bounded_question: string | null
    note: string
  }
  rendered: null | {
    run_id: string
    run_started_at: string | null
    analyst_version?: string | null
    text: string | null
    chars_stored: number
    truncated: boolean
    tail: string | null
    total_chars: number | null
    sha256: string | null
    basis: string
  }
  fresh_render: string
}

export interface VersionRow {
  version: string
  state: string
  is_head: boolean
  created_at: string | null
}

export interface FlowNodeDetail {
  node: FlowNodePayload
  edges_in: FlowEdgePayload[]
  edges_out: FlowEdgePayload[]
  versions?: VersionRow[]
  prompt?: FlowPrompt
  output_contract?: {
    type_signature: unknown
    outputs: unknown
    observed_rows: Array<{ table: string; id: string; kind: string; schema_uri: string | null }>
    note: string
  }
  params?: FlowParams
  recent_runs?: RunSummary[]
  observed_steps?: null | { label: string; run_id: string; status: string | null; steps: StepSummary[] }
}

export interface FlowRunDetail {
  run_id: string
  analyst_id: string
  analyst_version?: string | null
  target_id?: string | null
  trigger?: string | null
  status: string | null
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  tokens: number
  receipt_hash?: string | null
  prompt_sha256?: string | null
  prompt_chars?: number | null
  error?: unknown
  sequence_label: string
  steps: StepSummary[]
  llm_calls: Array<Record<string, unknown> & { index: number }>
  tool_calls: Array<Record<string, unknown> & { index: number }>
  input_refs: { count: number; ids: string[]; listed: number }
  output_refs: { count: number; ids: string[] }
  outputs: Array<{
    id: string
    table: string
    kind: string
    title?: string | null
    schema_uri?: string | null
    produced_at?: string | null
    bytes?: number | null
    honesty_flags?: string[] | null
    gates: Record<string, unknown>
    judge_critiques: Array<Record<string, unknown>>
  }>
}

export interface FlowVersionBody {
  node_id: string
  version: string
  state: string
  is_head: boolean
  created_at: string | null
  fields: unknown
}

export interface FlowBodyPart {
  part: string
  index: number | null
  bytes: number
  truncated: boolean
  cap_bytes: number
  text: string
}
