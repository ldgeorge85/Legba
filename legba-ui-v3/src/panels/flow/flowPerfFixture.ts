/**
 * The Flow PERF fixture: the live whole-system graph's structure (336 nodes,
 * 4,434 edges — 3,872 of them selector edges — with the real groups, kinds,
 * relations and degree distribution), ids scrambled and no descriptor bodies.
 * Test-only; not imported by any render path.
 */
import raw from './fixtures/flowPerfGraph.json'
import type { FlowGraphPayload, FlowNodePayload } from './flowTypes'

interface Compact {
  kinds: string[]
  groups: string[]
  subkinds: string[]
  relations: string[]
  bases: string[]
  nodes: Array<[number, number, number, number]>
  edges: Array<[number, number, number, number]>
}

export function perfGraph(): FlowGraphPayload {
  const c = raw as unknown as Compact
  const nodes: FlowNodePayload[] = c.nodes.map(([k, g, s, paused], i) => {
    const kind = c.kinds[k]
    const id = `${kind}:n${String(i).padStart(3, '0')}`
    return {
      id, descriptor_id: `n${i}`, family: kind, kind, subkind: s >= 0 ? c.subkinds[s] : null,
      method_kind: null, group: c.groups[g], label: `n${i}`, state: paused ? 'paused' : 'active',
      version: null, cadence: null, model_route: {}, target_scope: null, fields: null, undeclared: [],
    }
  })
  const edges = c.edges.map(([s, t, r, b], i) => {
    const basis = c.bases[b].replace('[]', `[${i % 7}]`)
    return { id: `${nodes[s].id}->${nodes[t].id}#${basis}#${i}`, source: nodes[s].id, target: nodes[t].id, basis, relation: c.relations[r] }
  })
  const edgesByBasis: Record<string, number> = {}
  for (const e of edges) edgesByBasis[e.basis] = (edgesByBasis[e.basis] === undefined ? 0 : edgesByBasis[e.basis]) + 1
  return {
    nodes,
    edges,
    scope: 'all',
    generated_at: '2026-11-13T00:00:00Z',
    fields_included: false,
    report: { nodes: nodes.length, edges: edges.length, edges_by_basis: edgesByBasis, nodes_by_kind: {}, unresolved_refs: [], predicate_errors: [], inexpressible: [] },
  }
}
