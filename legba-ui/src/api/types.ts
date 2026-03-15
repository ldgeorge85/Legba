// API response types — mirrors backend schemas

export interface IngestionStatus {
  active: boolean
  events_1h: number
  events_24h: number
  errors_1h: number
}

export interface DashboardStats {
  entities: number
  events: number
  sources: number
  goals: number
  facts: number
  situations: number
  watchlist: number
  relationships: number
  current_cycle: number
  agent_status: string
  recent_events: EventSummary[]
  active_situations: SituationSummary[]
  ingestion?: IngestionStatus
}

export interface EventSummary {
  event_id: string
  title: string
  category: string
  confidence: number
  timestamp: string
  source_name: string | null
  created_at?: string
}

export interface EventDetail extends EventSummary {
  description: string
  source_url: string | null
  source_id: string | null
  tags: string[]
  entities: EntityLink[]
  raw_data: Record<string, unknown> | null
  created_at: string
}

export interface EntityLink {
  entity_id: string
  name: string
  entity_type: string
  role: string | null
}

export interface EntitySummary {
  entity_id: string
  name: string
  entity_type: string
  first_seen: string
  last_seen: string
  event_count: number
  completeness: number | null
}

export interface EntityDetail extends EntitySummary {
  aliases: string[]
  assertions: Assertion[]
  relationships: Relationship[]
}

export interface Assertion {
  key: string
  value: string
  confidence: number
  source: string
  timestamp: string
}

export interface Relationship {
  source: string
  target: string
  rel_type: string
  properties: Record<string, unknown>
}

export interface SourceSummary {
  source_id: string
  name: string
  url: string
  source_type: string
  status: string
  fetch_count: number
  fail_count: number
  event_count: number
  last_fetched: string | null
}

export interface SourceDetail extends SourceSummary {
  description: string | null
  tags: string[]
  config: Record<string, unknown> | null
  recent_events: EventSummary[]
}

export interface Goal {
  goal_id: string
  description: string
  status: string
  priority: number
  progress_pct: number
  parent_id: string | null
  children: Goal[]
  created_at: string
  updated_at: string
}

export interface SituationSummary {
  situation_id: string
  title: string
  status: string
  severity: string
  event_count: number
  created_at: string
  updated_at: string
}

export interface SituationDetail extends SituationSummary {
  description: string
  events: EventSummary[]
}

export interface WatchItem {
  watch_id: string
  entity_name: string
  watch_type: string
  description: string | null
  entities: string[]
  keywords: string[]
  categories: string[]
  trigger_count: number
  created_at: string
}

export interface WatchTrigger {
  watch_id: string
  entity_name: string
  trigger_type: string
  details: string
  triggered_at: string
}

export interface Fact {
  fact_id: string
  subject: string
  predicate: string
  object: string
  confidence: number
  source: string
  timestamp: string
}

export interface MemoryPoint {
  id: string
  collection: string
  content: string
  significance: number
  timestamp: string
  metadata: Record<string, unknown>
}

export interface CycleSummary {
  cycle_number: number
  cycle_type: string
  started_at: string
  duration_s: number
  tool_calls: number
  llm_calls: number
  events_stored: number
  errors: number
}

export interface CycleDetail extends CycleSummary {
  phases: PhaseInfo[]
  total_tokens: number
}

export interface PhaseInfo {
  name: string
  duration_s: number
  tool_calls: number
  llm_calls: number
  tokens: number
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  rel_types: string[]
}

export interface GraphNode {
  id: string
  label: string
  type: string
  properties: Record<string, unknown>
}

export interface GraphEdge {
  source: string
  target: string
  rel_type: string
  properties: Record<string, unknown>
}

export interface GeoNode {
  id: string
  label: string
  lat: number
  lon: number
  type: string
  entity_id: string
}

export interface JournalEntry {
  cycle: number
  timestamp: string
  content?: string
  entries?: string[]  // Array of journal lines per cycle
}

export interface ReportEntry {
  cycle: number
  timestamp: string
  content: string
}

export interface JournalData {
  entries: JournalEntry[]
  consolidation?: string
}

// Analytics types
export interface EventTimeseries {
  date: string
  total: number
  conflict: number
  political: number
  economic: number
  technology: number
  health: number
  environment: number
  social: number
  disaster: number
  other: number
}

export interface CyclePerformance {
  cycle: number
  tool_calls: number
  llm_calls: number
  tokens: number
  latency_ms: number
}

export interface EntityDistribution {
  type: string
  count: number
}

export interface SourceHealth {
  source_id: string
  name: string
  fetch_count: number
  fail_count: number
  event_count: number
  health: number
}

export interface FactDistribution {
  buckets: { range: string; count: number }[]
  top_predicates: { predicate: string; count: number }[]
}

// Paginated response
export interface PaginatedResponse<T> {
  items: T[]
  total: number
  offset: number
  limit: number
}

// SSE event types
export type SSEEventType =
  | 'event:new'
  | 'watch:trigger'
  | 'cycle:start'
  | 'cycle:end'
  | 'agent:status'
  | 'situation:update'

export interface SSEEvent {
  type: SSEEventType
  data: Record<string, unknown>
}

// WebSocket consult message types
export interface ConsultMessage {
  type: 'message' | 'clear'
  content?: string
}

export interface ConsultResponse {
  type: 'thinking' | 'tool_call' | 'tool_result' | 'message' | 'error'
  content?: string
  tool?: string
  args?: Record<string, unknown>
  result?: Record<string, unknown>
}
