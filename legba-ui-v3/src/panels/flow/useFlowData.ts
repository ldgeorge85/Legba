/** Flow (N-J v0) — the read hooks. GET only; nothing here writes. */
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type {
  FlowBodyPart,
  FlowGraphPayload,
  FlowNodeDetail,
  FlowRunDetail,
  FlowVersionBody,
} from './flowTypes'

const enc = encodeURIComponent

/**
 * The graph WITHOUT descriptor bodies (`fields=false`; a node's body comes
 * from the node route only when it is selected). `enabled` is false until the
 * tile has been shown: a Flow tab in a restored layout fetches nothing.
 */
export function useFlowGraph(scope: string, enabled = true) {
  return useQuery({
    queryKey: ['flow', 'graph', scope],
    queryFn: () => apiGet<FlowGraphPayload>(`/v3/flow/graph?fields=false&depth=1&scope=${enc(scope)}`),
    staleTime: 60_000,
    enabled,
    refetchOnWindowFocus: false,
  })
}

export function useFlowNode(nodeId: string | null) {
  return useQuery({
    queryKey: ['flow', 'node', nodeId],
    queryFn: () => apiGet<FlowNodeDetail>(`/v3/flow/node/${enc(nodeId as string)}`),
    enabled: !!nodeId,
    staleTime: 60_000,
  })
}

export function useFlowRun(runId: string | null) {
  return useQuery({
    queryKey: ['flow', 'run', runId],
    queryFn: () => apiGet<FlowRunDetail>(`/v3/flow/run/${enc(runId as string)}`),
    enabled: !!runId,
    staleTime: 300_000,
  })
}

export function useFlowVersion(nodeId: string | null, version: string | null) {
  return useQuery({
    queryKey: ['flow', 'version', nodeId, version],
    queryFn: () => apiGet<FlowVersionBody>(`/v3/flow/version/${enc(nodeId as string)}?version=${enc(version as string)}`),
    enabled: !!nodeId && !!version,
    staleTime: Infinity,
  })
}

export function fetchRunBody(runId: string, part: string, index: number | null): Promise<FlowBodyPart> {
  const idx = index === null ? '' : `&index=${index}`
  return apiGet<FlowBodyPart>(`/v3/flow/run/${enc(runId)}/body?part=${enc(part)}${idx}`)
}
