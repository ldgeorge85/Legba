/**
 * Flow (N-J v0) — the PURE projection from the graph payload to canvas
 * nodes and edges. No layout here (flowLayout.ts), no fetching, no state.
 *
 * Spaghetti control is GROUPING: every node belongs to the `group` the
 * backend derived (an analyst's kind, `sources`, `targets`, `packs`,
 * `stores`, `sinks`). A COLLAPSED group renders as one composite node; edges
 * into and out of it are aggregated per (from, to, relation) and keep every
 * basis they stand for, so collapsing never hides WHY an edge exists.
 *
 * N-Q: the PANEL renders through `flowView.ts` (the bounded overview and the
 * budget); `projectGraph` / `defaultCollapsed` remain the unbounded v0
 * projection and are not used by any render path.
 */
import type { Edge, Node } from '@xyflow/react'
import type { FlowEdgePayload, FlowGraphPayload, FlowNodePayload } from './flowTypes'

/** A group larger than this starts collapsed. */
export const COLLAPSE_THRESHOLD = 20

export interface FlowCardData extends Record<string, unknown> {
  kind: string
  label: string
  state: string | null
  paused: boolean
  group: string
  subkind: string | null
  /** The model aliases on the node's routes, e.g. "fast". */
  models: string[]
  /** One-line "what it does" under the title. */
  summary: string
  context: boolean
  matched: boolean
  runStatus: string | null
  nodeId: string
}

export interface FlowGroupData extends Record<string, unknown> {
  kind: 'group'
  label: string
  group: string
  count: number
  paused: number
  matched: boolean
  nodeId: string
}

export interface FlowEdgeData extends Record<string, unknown> {
  relation: string
  /** Every basis this (possibly aggregated) edge stands for, sorted. */
  bases: string[]
  count: number
}

export type FlowCanvasNode = Node<FlowCardData, 'flowCard'> | Node<FlowGroupData, 'flowGroup'>
export type FlowCanvasEdge = Edge<FlowEdgeData>

export interface GroupInfo {
  group: string
  count: number
  paused: number
  collapsed: boolean
}

export interface ProjectionOptions {
  collapsed: ReadonlySet<string>
  /** Node ids matched by the search box (highlighted). */
  matched?: ReadonlySet<string>
  /** node id → run status to paint (the run overlay). */
  runStatus?: Readonly<Record<string, string>>
}

export interface Projection {
  nodes: FlowCanvasNode[]
  edges: FlowCanvasEdge[]
  groups: GroupInfo[]
}

export const groupNodeId = (group: string): string => `group:${group}`

/** Human label for a backend group key (`analyst:inline_target` → `analyst · inline_target`). */
export function groupLabel(group: string): string {
  return group.includes(':') ? group.replace(':', ' · ') : group
}

/** Groups with more than {@link COLLAPSE_THRESHOLD} members. */
export function defaultCollapsed(payload: FlowGraphPayload): Set<string> {
  const counts = new Map<string, number>()
  for (const n of payload.nodes) {
    const c = counts.get(n.group)
    counts.set(n.group, c === undefined ? 1 : c + 1)
  }
  return new Set([...counts].filter(([, c]) => c > COLLAPSE_THRESHOLD).map(([g]) => g))
}

function models(n: FlowNodePayload): string[] {
  const out = new Set<string>()
  for (const r of Object.values(n.model_route ?? {})) if (r?.model) out.add(String(r.model))
  return [...out].sort()
}

/** One line under the title: what the node does, from declared fields only. */
export function nodeSummary(n: FlowNodePayload): string {
  const cad = n.cadence ?? null
  const schedule = cad && typeof cad.schedule === 'string' ? cad.schedule : null
  switch (n.kind) {
    case 'analyst': {
      const m = models(n)
      const parts = [n.method_kind ?? n.subkind ?? 'analyst']
      if (m.length) parts.push(m.join('+'))
      if (schedule) parts.push(schedule)
      return parts.join(' · ')
    }
    case 'source':
      return [n.subkind ?? 'source', schedule].filter(Boolean).join(' · ')
    case 'pack':
      return `${(n.tools ?? []).length} tools`
    case 'unresolved':
      return 'referenced, not an active or paused head'
    default:
      return n.subkind ?? n.kind
  }
}

/** Case-insensitive search over label, id, kind, group, models and tool names. */
export function searchNodes(payload: FlowGraphPayload, query: string): Set<string> {
  const q = query.trim().toLowerCase()
  const out = new Set<string>()
  if (!q) return out
  for (const n of payload.nodes) {
    const hay = [n.label, n.id, n.kind, n.subkind ?? '', n.group, ...models(n),
      ...(n.tools ?? []).map((t) => t.name ?? '')].join(' ').toLowerCase()
    if (hay.includes(q)) out.add(n.id)
  }
  return out
}

function cardOf(n: FlowNodePayload, opts: ProjectionOptions): Node<FlowCardData, 'flowCard'> {
  return {
    id: n.id,
    type: 'flowCard',
    position: { x: 0, y: 0 },
    data: {
      kind: String(n.kind),
      label: n.label,
      state: n.state,
      paused: n.state === 'paused',
      group: n.group,
      subkind: n.subkind ?? null,
      models: models(n),
      summary: nodeSummary(n),
      context: n.context === true,
      matched: opts.matched?.has(n.id) ?? false,
      runStatus: opts.runStatus?.[n.id] ?? null,
      nodeId: n.id,
    },
  }
}

function basisKey(e: FlowEdgePayload): string {
  // `target.sources[3].source_selector` → `target.sources[].source_selector`
  return e.basis.replace(/\[\d+\]/g, '[]')
}

/** payload + view options → canvas nodes/edges, sorted by id (deterministic). */
export function projectGraph(payload: FlowGraphPayload, opts: ProjectionOptions): Projection {
  const byGroup = new Map<string, FlowNodePayload[]>()
  for (const n of payload.nodes) {
    const list = byGroup.get(n.group) ?? []
    list.push(n)
    byGroup.set(n.group, list)
  }
  const rep = new Map<string, string>()
  const nodes: FlowCanvasNode[] = []
  const groups: GroupInfo[] = []
  for (const [group, members] of [...byGroup].sort(([a], [b]) => a.localeCompare(b))) {
    const paused = members.filter((m) => m.state === 'paused').length
    const collapsed = opts.collapsed.has(group)
    groups.push({ group, count: members.length, paused, collapsed })
    if (collapsed) {
      const gid = groupNodeId(group)
      for (const m of members) rep.set(m.id, gid)
      nodes.push({
        id: gid,
        type: 'flowGroup',
        position: { x: 0, y: 0 },
        data: {
          kind: 'group', label: groupLabel(group), group, count: members.length, paused,
          matched: members.some((m) => opts.matched?.has(m.id)), nodeId: gid,
        },
      })
    } else {
      for (const m of members) {
        rep.set(m.id, m.id)
        nodes.push(cardOf(m, opts))
      }
    }
  }
  const agg = new Map<string, { source: string; target: string; relation: string; bases: Set<string>; count: number; rawId?: string; basis?: string }>()
  for (const e of payload.edges) {
    const s = rep.get(e.source)
    const t = rep.get(e.target)
    if (!s || !t || s === t) continue
    const aggregated = s !== e.source || t !== e.target
    const key = aggregated ? `agg:${s}->${t}#${e.relation}` : e.id
    const cur = agg.get(key) ?? { source: s, target: t, relation: e.relation, bases: new Set<string>(), count: 0 }
    cur.bases.add(aggregated ? basisKey(e) : e.basis)
    cur.count += 1
    agg.set(key, cur)
  }
  const edges: FlowCanvasEdge[] = [...agg]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([id, a]) => ({
      id,
      source: a.source,
      target: a.target,
      data: { relation: a.relation, bases: [...a.bases].sort(), count: a.count },
    }))
  nodes.sort((a, b) => a.id.localeCompare(b.id))
  return { nodes, edges, groups }
}

/** The hover label of an edge: its basis (or bases, when aggregated). */
export function edgeLabel(d: FlowEdgeData): string {
  const head = d.bases.length === 1 ? d.bases[0] : `${d.bases.length} bases: ${d.bases.join(', ')}`
  return d.count > 1 ? `${head} (×${d.count})` : head
}

/** Scope options from the whole-system graph: families and targets. */
export function scopeOptions(all: FlowGraphPayload | undefined): Array<{ value: string; label: string }> {
  const out = [{ value: 'all', label: 'Whole system' }]
  if (!all) return out
  const groups = [...new Set(all.nodes.map((n) => n.group))].sort()
  for (const g of groups) out.push({ value: `family:${g}`, label: `Family: ${groupLabel(g)}` })
  for (const t of all.nodes.filter((n) => n.kind === 'target').sort((a, b) => a.id.localeCompare(b.id))) {
    out.push({ value: `target:${t.descriptor_id}`, label: `Target chain: ${t.label}` })
  }
  return out
}

