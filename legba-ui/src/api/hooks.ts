import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type {
  DashboardStats,
  SignalSummary,
  SignalDetail,
  SignalFacets,
  EventSummary,
  EventDetail,
  EntitySummary,
  EntityDetail,
  EntityMergeResult,
  SourceSummary,
  SourceDetail,
  Goal,
  SituationSummary,
  SituationDetail,
  WatchItem,
  WatchTrigger,
  Fact,
  MemoryPoint,
  CycleSummary,
  CycleDetail,
  GraphData,
  GraphNode,
  GraphEdge,
  GeoNode,
  JournalData,
  ReportEntry,
  PaginatedResponse,
  EventTimeseries,
  CyclePerformance,
  EntityDistribution,
  SourceHealth,
  FactDistribution,
  ScorecardData,
  SearchResults,
  EventGeoCollection,
  SignalGeoCollection,
  EventFacets,
  PredicateCount,
  EntityTypeCount,
  HypothesisSummary,
  SituationBrief,
} from './types'

// ── Dashboard ──

export function useDashboard() {
  return useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<DashboardStats>('/api/v2/dashboard'),
    refetchInterval: 30_000,
  })
}

// ── Scorecard ──

export function useScorecard() {
  return useQuery({
    queryKey: ['scorecard'],
    queryFn: () => api.get<ScorecardData>('/api/v2/scorecard'),
    refetchInterval: 120_000,
  })
}

// ── Events (derived real-world occurrences) ──

export function useEvents(params: {
  offset?: number
  limit?: number
  category?: string
  severity?: string
  event_type?: string
  min_signals?: number
  q?: string
}) {
  return useQuery({
    queryKey: ['events', params],
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.offset) sp.set('offset', String(params.offset))
      if (params.limit) sp.set('limit', String(params.limit))
      if (params.category) sp.set('category', params.category)
      if (params.severity) sp.set('severity', params.severity)
      if (params.event_type) sp.set('event_type', params.event_type)
      if (params.min_signals != null) sp.set('min_signals', String(params.min_signals))
      if (params.q) sp.set('q', params.q)
      return api.get<PaginatedResponse<EventSummary>>(`/api/v2/events?${sp}`)
    },
  })
}

export function useEventFacets() {
  return useQuery({
    queryKey: ['events', 'facets'],
    queryFn: () => api.get<EventFacets>('/api/v2/events/facets'),
    staleTime: 60_000,
  })
}

export function useEvent(eventId: string | null) {
  return useQuery({
    queryKey: ['event', eventId],
    queryFn: () => api.get<EventDetail>(`/api/v2/events/${eventId}`),
    enabled: !!eventId,
  })
}

// ── Signals (raw ingested material) ──

export function useSignals(params: {
  offset?: number
  limit?: number
  category?: string
  q?: string
  source?: string
  start_date?: string
  end_date?: string
  min_confidence?: number
}) {
  return useQuery({
    queryKey: ['signals', params],
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.offset) sp.set('offset', String(params.offset))
      if (params.limit) sp.set('limit', String(params.limit))
      if (params.category) sp.set('category', params.category)
      if (params.q) sp.set('q', params.q)
      if (params.source) sp.set('source', params.source)
      if (params.start_date) sp.set('start_date', params.start_date)
      if (params.end_date) sp.set('end_date', params.end_date)
      if (params.min_confidence != null) sp.set('min_confidence', String(params.min_confidence))
      return api.get<PaginatedResponse<SignalSummary>>(`/api/v2/signals?${sp}`)
    },
  })
}

export function useSignalFacets() {
  return useQuery({
    queryKey: ['signals', 'facets'],
    queryFn: () => api.get<SignalFacets>('/api/v2/signals/facets'),
    staleTime: 60_000,
  })
}

export function useSignal(signalId: string | null) {
  return useQuery({
    queryKey: ['signal', signalId],
    queryFn: () => api.get<SignalDetail>(`/api/v2/signals/${signalId}`),
    enabled: !!signalId,
  })
}

export function useDeleteSignal() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (signalId: string) => api.delete(`/api/v2/signals/${signalId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['signals'] }),
  })
}

// ── Entities ──

export function useEntities(params: {
  offset?: number
  limit?: number
  type?: string
  q?: string
  min_completeness?: number
  created_after?: string
}) {
  return useQuery({
    queryKey: ['entities', params],
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.offset) sp.set('offset', String(params.offset))
      if (params.limit) sp.set('limit', String(params.limit))
      if (params.type) sp.set('type', params.type)
      if (params.q) sp.set('q', params.q)
      if (params.min_completeness != null) sp.set('min_completeness', String(params.min_completeness))
      if (params.created_after) sp.set('created_after', params.created_after)
      return api.get<PaginatedResponse<EntitySummary>>(`/api/v2/entities?${sp}`)
    },
  })
}

export function useEntity(entityId: string | null) {
  return useQuery({
    queryKey: ['entity', entityId],
    queryFn: () => api.get<EntityDetail>(`/api/v2/entities/${entityId}`),
    enabled: !!entityId,
  })
}

export function useSearchEntities(query: string) {
  return useQuery({
    queryKey: ['entities', 'search', query],
    queryFn: () => {
      const sp = new URLSearchParams()
      sp.set('q', query)
      sp.set('limit', '10')
      return api.get<PaginatedResponse<EntitySummary>>(`/api/v2/entities?${sp}`)
    },
    enabled: query.length >= 2,
  })
}

export function useMergePreview(keepId: string | null, removeId: string | null) {
  return useQuery({
    queryKey: ['entity-merge-preview', keepId, removeId],
    queryFn: () =>
      api.post<EntityMergeResult>('/api/entities/merge', {
        keep_id: keepId,
        remove_id: removeId,
        preview: true,
      }),
    enabled: !!keepId && !!removeId,
  })
}

export function useMergeEntities() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ keepId, removeId }: { keepId: string; removeId: string }) =>
      api.post<EntityMergeResult>('/api/entities/merge', {
        keep_id: keepId,
        remove_id: removeId,
        preview: false,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['entities'] })
      qc.invalidateQueries({ queryKey: ['entity'] })
      qc.invalidateQueries({ queryKey: ['graph'] })
      qc.invalidateQueries({ queryKey: ['facts'] })
      qc.invalidateQueries({ queryKey: ['events'] })
      qc.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })
}

// ── Sources ──

export function useSources(params: { offset?: number; limit?: number; status?: string; q?: string }) {
  return useQuery({
    queryKey: ['sources', params],
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.offset) sp.set('offset', String(params.offset))
      if (params.limit) sp.set('limit', String(params.limit))
      if (params.status) sp.set('status', params.status)
      if (params.q) sp.set('q', params.q)
      return api.get<PaginatedResponse<SourceSummary>>(`/api/v2/sources?${sp}`)
    },
  })
}

export function useSource(sourceId: string | null) {
  return useQuery({
    queryKey: ['source', sourceId],
    queryFn: () => api.get<SourceDetail>(`/api/v2/sources/${sourceId}`),
    enabled: !!sourceId,
  })
}

// ── Goals ──

export function useGoals() {
  return useQuery({
    queryKey: ['goals'],
    queryFn: () => api.get<Goal[]>('/api/v2/goals'),
  })
}

// ── Situations ──

export function useSituations(params: { status?: string } = {}) {
  return useQuery({
    queryKey: ['situations', params],
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.status) sp.set('status', params.status)
      return api.get<SituationSummary[]>(`/api/v2/situations?${sp}`)
    },
  })
}

export function useSituation(situationId: string | null) {
  return useQuery({
    queryKey: ['situation', situationId],
    queryFn: () => api.get<SituationDetail>(`/api/v2/situations/${situationId}`),
    enabled: !!situationId,
  })
}

// ── Watchlist ──

export function useWatchlist() {
  return useQuery({
    queryKey: ['watchlist'],
    queryFn: () => api.get<WatchItem[]>('/api/v2/watchlist'),
  })
}

export function useWatchTriggers() {
  return useQuery({
    queryKey: ['watchlist', 'triggers'],
    queryFn: () => api.get<WatchTrigger[]>('/api/v2/watchlist/triggers'),
  })
}

export function useCreateWatch() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { name: string; description: string; entities: string[]; keywords: string[]; categories: string[]; priority: string }) =>
      api.post('/api/v2/watchlist', body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['watchlist'] }),
  })
}

export function useUpdateWatch() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ watchId, body }: { watchId: string; body: { name?: string; description?: string; priority?: string } }) =>
      api.put(`/api/v2/watchlist/${watchId}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['watchlist'] }),
  })
}

export function useDeleteWatch() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (watchId: string) => api.delete(`/api/v2/watchlist/${watchId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['watchlist'] }),
  })
}

// ── Facts ──

export function useFacts(params: {
  offset?: number
  limit?: number
  q?: string
  predicate?: string
  min_confidence?: number
  subject?: string
}) {
  return useQuery({
    queryKey: ['facts', params],
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.offset) sp.set('offset', String(params.offset))
      if (params.limit) sp.set('limit', String(params.limit))
      if (params.q) sp.set('q', params.q)
      if (params.predicate) sp.set('predicate', params.predicate)
      if (params.min_confidence != null) sp.set('min_confidence', String(params.min_confidence))
      if (params.subject) sp.set('subject', params.subject)
      return api.get<PaginatedResponse<Fact>>(`/api/v2/facts?${sp}`)
    },
  })
}

export function useFactPredicates() {
  return useQuery({
    queryKey: ['facts', 'predicates'],
    queryFn: () => api.get<PredicateCount[]>('/api/v2/facts/predicates'),
    staleTime: 60_000,
  })
}

export function useEntityTypes() {
  return useQuery({
    queryKey: ['entities', 'types'],
    queryFn: () => api.get<EntityTypeCount[]>('/api/v2/entities/types'),
    staleTime: 60_000,
  })
}

// ── Memory ──

export function useMemory(params: { collection?: string; q?: string; offset?: string }) {
  return useQuery({
    queryKey: ['memory', params],
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.collection) sp.set('collection', params.collection)
      if (params.q) sp.set('q', params.q)
      if (params.offset) sp.set('offset', params.offset)
      return api.get<{ points: MemoryPoint[]; next_offset: string | null }>(`/api/v2/memory?${sp}`)
    },
  })
}

// ── Cycles ──

export function useCycles(params: { offset?: number; limit?: number }) {
  return useQuery({
    queryKey: ['cycles', params],
    refetchInterval: 60_000,
    queryFn: () => {
      const sp = new URLSearchParams()
      if (params.offset) sp.set('offset', String(params.offset))
      if (params.limit) sp.set('limit', String(params.limit))
      return api.get<PaginatedResponse<CycleSummary>>(`/api/v2/cycles?${sp}`)
    },
  })
}

export function useCycle(cycleNumber: number | null) {
  return useQuery({
    queryKey: ['cycle', cycleNumber],
    queryFn: () => api.get<CycleDetail>(`/api/v2/cycles/${cycleNumber}`),
    enabled: cycleNumber != null,
  })
}

// ── Graph ──

// Transform Cytoscape-format response ({nodes: [{data: {...}}]}) to flat format
function transformGraphResponse(raw: any): GraphData {
  const nodes: GraphNode[] = (raw.nodes ?? []).map((n: any) => {
    const d = n.data ?? n
    return { id: d.id, label: d.name ?? d.label ?? d.id, type: d.type ?? 'unknown', properties: d }
  })
  const edges: GraphEdge[] = (raw.edges ?? []).map((e: any) => {
    const d = e.data ?? e
    return { source: d.source, target: d.target, rel_type: d.type ?? d.rel_type ?? 'related', properties: d }
  })
  return { nodes, edges, rel_types: [] }
}

export function useGraph() {
  return useQuery({
    queryKey: ['graph'],
    queryFn: async () => {
      const raw = await api.get<any>('/api/graph')
      return transformGraphResponse(raw)
    },
    staleTime: 60_000,
  })
}

export function useEgoGraph(entity: string | null, depth = 1) {
  return useQuery({
    queryKey: ['graph', 'ego', entity, depth],
    queryFn: async () => {
      const raw = await api.get<any>(`/api/graph/ego?entity=${encodeURIComponent(entity!)}&depth=${depth}`)
      return transformGraphResponse(raw)
    },
    enabled: !!entity,
  })
}

export function useGeoData() {
  return useQuery({
    queryKey: ['graph', 'geo'],
    queryFn: async () => {
      const raw = await api.get<any>('/api/graph/geo')
      const nodes: GeoNode[] = (raw.nodes ?? []).map((n: any) => ({
        id: n.id ?? n.name,
        label: n.name ?? n.id,
        lat: n.lat,
        lon: n.lon,
        type: n.type ?? 'unknown',
        entity_id: n.entity_id ?? n.id ?? n.name,
      }))
      return { nodes }
    },
    staleTime: 60_000,
  })
}

export function useEventGeoData() {
  return useQuery({
    queryKey: ['events', 'geo'],
    queryFn: () => api.get<EventGeoCollection>('/api/v2/events/geo'),
    staleTime: 60_000,
  })
}

export function useSignalGeoData() {
  return useQuery({
    queryKey: ['signals', 'geo'],
    queryFn: () => api.get<SignalGeoCollection>('/api/v2/signals/geo'),
    staleTime: 60_000,
  })
}

// ── Journal ──

export function useJournal() {
  return useQuery({
    queryKey: ['journal'],
    refetchInterval: 60_000,
    queryFn: async () => {
      const data = await api.get<JournalData>('/api/journal')
      return {
        entries: data.entries ?? [],
        consolidation: data.consolidation,
      } as JournalData
    },
    staleTime: 60_000,
  })
}

// ── Reports ──

export function useReports() {
  return useQuery({
    queryKey: ['reports'],
    queryFn: () => api.get<ReportEntry[]>('/api/reports'),
    staleTime: 60_000,
    refetchInterval: 60_000,
  })
}

// ── Global Search ──

export function useGlobalSearch(query: string) {
  return useQuery({
    queryKey: ['search', query],
    queryFn: () => api.get<SearchResults>(`/api/v2/search?q=${encodeURIComponent(query)}`),
    enabled: query.length >= 2,
  })
}

// ── Analytics ──

export function useEventTimeseries(days = 30) {
  return useQuery({
    queryKey: ['stats', 'events-timeseries', days],
    queryFn: () => api.get<EventTimeseries[]>(`/api/stats/events-timeseries?days=${days}`),
    staleTime: 60_000,
  })
}

export function useCyclePerformance(last = 50) {
  return useQuery({
    queryKey: ['stats', 'cycle-performance', last],
    queryFn: () => api.get<CyclePerformance[]>(`/api/stats/cycle-performance?last=${last}`),
    staleTime: 60_000,
  })
}

export function useEntityDistribution() {
  return useQuery({
    queryKey: ['stats', 'entity-distribution'],
    queryFn: () => api.get<EntityDistribution[]>('/api/stats/entity-distribution'),
    staleTime: 60_000,
  })
}

export function useSourceHealth() {
  return useQuery({
    queryKey: ['stats', 'source-health'],
    queryFn: () => api.get<SourceHealth[]>('/api/stats/source-health'),
    staleTime: 60_000,
  })
}

export function useFactDistribution() {
  return useQuery({
    queryKey: ['stats', 'fact-distribution'],
    queryFn: () => api.get<FactDistribution>('/api/stats/fact-distribution'),
    staleTime: 60_000,
  })
}

export function useHypotheses(status: string = 'active') {
  return useQuery({
    queryKey: ['hypotheses', status],
    queryFn: () => api.get<HypothesisSummary[]>(`/api/v2/hypotheses?status=${status}`),
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
}

export function useBriefs(limit: number = 20) {
  return useQuery({
    queryKey: ['briefs', limit],
    queryFn: () => api.get<SituationBrief[]>(`/api/v2/briefs?limit=${limit}`),
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
}
