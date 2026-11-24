/**
 * Flow (N-Q) — the BOUNDED view over the graph payload. Pure: no layout, no
 * fetching, no React.
 *
 * Why this exists (incident 2026-11-13): the v0 panel's first projection was
 * the whole system un-collapsed (336 nodes / 4,434 edges) and ELK laid it out
 * on the main thread — measured at more than 590 s in node, a hard browser
 * lock. Even the v0 default collapse (70 nodes / 139 edges) blocked the main
 * thread for 580 ms. This module guarantees the canvas never sees more than
 * the budget:
 *
 *   - THE OVERVIEW is one composite node per view group — sources split by
 *     kind (`sources:rss`), each analyst family, targets, packs, stores,
 *     sinks — with edges AGGREGATED per (group → group, relation) and
 *     carrying a count. No per-edge canvas element exists for an aggregate.
 *   - EXPANSION is explicit: an expanded group shows its members, and only
 *     edges among the visible set are drawn (aggregated where an end is still
 *     a group).
 *   - SELECTOR EDGES (`target.sources[].source_selector`, 3,872 of the 4,434)
 *     stay bundled at their source group in every view except a node
 *     neighbourhood (`focus`): the group's composite node stays on the canvas
 *     as a bundle even while its members are expanded.
 *   - THE BUDGET: a view over {@link MAX_VIEW_NODES} nodes or
 *     {@link MAX_VIEW_EDGES} edges is REFUSED — the caller shows the counts
 *     and asks for a narrower scope; it is never laid out or rendered.
 */
import type { FlowEdgePayload, FlowGraphPayload, FlowNodePayload } from './flowTypes'
import {
  groupNodeId,
  nodeSummary,
  type FlowCanvasEdge,
  type FlowCanvasNode,
  type FlowCardData,
} from './flowProjection'

/** The hard budget for any single layout/render. */
export const MAX_VIEW_NODES = 150
export const MAX_VIEW_EDGES = 600

/** A selector edge: a target's `sources[i].source_selector` match. */
export function isSelectorEdge(e: FlowEdgePayload): boolean {
  return e.basis.includes('source_selector')
}

/** The view group of a node: sources split by kind; everything else its backend group. */
export function viewGroupOf(n: FlowNodePayload): string {
  if (n.kind === 'source' || n.group === 'sources') return `sources:${n.subkind ? n.subkind : 'other'}`
  return n.group
}

/** Human label for a view group key (`analyst:inline_target` → `analyst · inline_target`). */
export function viewGroupLabel(group: string): string {
  // `analyst:inquiry` → "analyst · inquiry"; `competing_hypotheses` → "competing hypotheses"
  return (group.includes(':') ? group.replace(':', ' · ') : group).replace(/_/g, ' ')
}

/** "1 node" / "12 nodes" — a count and its noun, never run together. */
export function nodeCount(n: number): string {
  return `${n} ${n === 1 ? 'node' : 'nodes'}`
}

/**
 * The canvas's counts line. Group boxes (a collapsed group, or a selector
 * bundle drawn beside its expanded members) are NOT nodes of the graph, so
 * they are counted apart: a narrowed scope with every member drawn and one
 * bundle box reads "112 of 112 nodes drawn · 1 group box", never "113 on
 * canvas for 112 nodes".
 */
export function viewCountsLine(view: Pick<FlowView, 'nodes' | 'edges' | 'total'>): string {
  let cards = 0
  let boxes = 0
  for (const n of view.nodes) {
    if (n.type === 'flowGroup') boxes += 1
    else cards += 1
  }
  const box = boxes ? ` · ${boxes} group ${boxes === 1 ? 'box' : 'boxes'}` : ''
  return `${cards} of ${nodeCount(view.total.nodes)} drawn${box} · ${view.edges.length} edges drawn for ${view.total.edges} declared`
}

export interface ViewGroup {
  group: string
  count: number
  paused: number
  expanded: boolean
}

/** One aggregated relation between two canvas endpoints (listed beside the canvas). */
export interface ViewAggregate {
  id: string
  source: string
  target: string
  relation: string
  bases: string[]
  count: number
  selector: boolean
}

export interface ViewOptions {
  /** View groups shown as their members. Everything else is one composite node. */
  expanded: ReadonlySet<string>
  /** Node ids always drawn as cards, whatever their group (a scope's anchor). */
  pinned?: ReadonlySet<string>
  /** A neighbourhood view: selector edges are drawn raw (the payload is already narrow). */
  focus?: boolean
  matched?: ReadonlySet<string>
  runStatus?: Readonly<Record<string, string>>
  selected?: string | null
}

export interface FlowView {
  nodes: FlowCanvasNode[]
  edges: FlowCanvasEdge[]
  groups: ViewGroup[]
  aggregates: ViewAggregate[]
  /** Raw payload sizes, for the counts line. */
  total: { nodes: number; edges: number }
  /** Set when the view breaks the budget: nothing may be laid out or rendered. */
  overBudget: { nodes: number; edges: number } | null
}

/**
 * The view groups of a payload with member counts — computed once per payload
 * (the panel memoises it), O(nodes).
 */
export function viewGroups(payload: FlowGraphPayload): Map<string, FlowNodePayload[]> {
  const byGroup = new Map<string, FlowNodePayload[]>()
  for (const n of payload.nodes) {
    const g = viewGroupOf(n)
    const list = byGroup.get(g)
    if (list) list.push(n)
    else byGroup.set(g, [n])
  }
  return byGroup
}

function basisKey(basis: string): string {
  return basis.replace(/\[\d+\]/g, '[]')
}

function card(n: FlowNodePayload, opts: ViewOptions): Extract<FlowCanvasNode, { type: 'flowCard' }> {
  const models = new Set<string>()
  for (const r of Object.values(n.model_route ? n.model_route : {})) if (r?.model) models.add(String(r.model))
  const data: FlowCardData = {
    kind: String(n.kind),
    label: n.label,
    state: n.state,
    paused: n.state === 'paused',
    group: viewGroupOf(n),
    subkind: n.subkind ? n.subkind : null,
    models: [...models].sort(),
    summary: nodeSummary(n),
    context: n.context === true,
    matched: opts.matched ? opts.matched.has(n.id) : false,
    runStatus: opts.runStatus && opts.runStatus[n.id] ? opts.runStatus[n.id] : null,
    nodeId: n.id,
  }
  return { id: n.id, type: 'flowCard', position: { x: 0, y: 0 }, data, selected: opts.selected === n.id }
}

/**
 * payload + view options → the bounded canvas view. Deterministic (sorted by
 * id). O(nodes + edges); never allocates a canvas element per aggregated edge.
 */
export function projectView(
  payload: FlowGraphPayload,
  opts: ViewOptions,
  groupsIndex: Map<string, FlowNodePayload[]> = viewGroups(payload),
): FlowView {
  const pinned = opts.pinned ? opts.pinned : new Set<string>()
  const rep = new Map<string, string>()
  const groupOf = new Map<string, string>()
  const nodes: FlowCanvasNode[] = []
  const groups: ViewGroup[] = []
  const groupNodes = new Map<string, FlowCanvasNode>()

  const groupNode = (group: string, members: FlowNodePayload[], paused: number, bundle: boolean): FlowCanvasNode => ({
    id: groupNodeId(group),
    type: 'flowGroup',
    position: { x: 0, y: 0 },
    selected: opts.selected === groupNodeId(group),
    data: {
      kind: 'group',
      label: bundle ? `${viewGroupLabel(group)} (selector bundle)` : viewGroupLabel(group),
      group,
      count: members.length,
      paused,
      matched: members.some((m) => (opts.matched ? opts.matched.has(m.id) : false)),
      nodeId: groupNodeId(group),
    },
  })

  for (const [group, members] of [...groupsIndex].sort(([a], [b]) => a.localeCompare(b))) {
    let paused = 0
    for (const m of members) if (m.state === 'paused') paused += 1
    const expanded = opts.expanded.has(group)
    groups.push({ group, count: members.length, paused, expanded })
    for (const m of members) {
      groupOf.set(m.id, group)
      if (expanded || pinned.has(m.id)) {
        rep.set(m.id, m.id)
        nodes.push(card(m, opts))
      } else {
        rep.set(m.id, groupNodeId(group))
      }
    }
    if (!expanded && members.some((m) => !pinned.has(m.id))) {
      const g = groupNode(group, members, paused, false)
      groupNodes.set(group, g)
      nodes.push(g)
    }
  }

  const agg = new Map<string, ViewAggregate>()
  const raw: FlowCanvasEdge[] = []
  for (const e of payload.edges) {
    let s = rep.get(e.source)
    const t = rep.get(e.target)
    if (!s || !t) continue
    const selector = isSelectorEdge(e)
    // Selector edges stay bundled at their source group outside a neighbourhood.
    if (selector && !opts.focus && s === e.source && !pinned.has(e.source)) {
      const g = groupOf.get(e.source) as string
      if (!groupNodes.has(g)) {
        const members = groupsIndex.get(g) as FlowNodePayload[]
        const bundle = groupNode(g, members, members.filter((m) => m.state === 'paused').length, true)
        groupNodes.set(g, bundle)
        nodes.push(bundle)
      }
      s = groupNodeId(g)
    }
    if (s === t) continue
    if (s === e.source && t === e.target) {
      raw.push({ id: e.id, source: s, target: t, data: { relation: e.relation, bases: [e.basis], count: 1 } })
      continue
    }
    const key = `agg:${s}->${t}#${e.relation}`
    const cur = agg.get(key)
    if (cur) {
      cur.count += 1
      const b = basisKey(e.basis)
      if (!cur.bases.includes(b)) cur.bases.push(b)
    } else {
      agg.set(key, { id: key, source: s, target: t, relation: e.relation, bases: [basisKey(e.basis)], count: 1, selector })
    }
  }
  const aggregates = [...agg.values()].sort((a, b) => a.id.localeCompare(b.id))
  for (const a of aggregates) a.bases.sort()
  const edges: FlowCanvasEdge[] = [
    ...raw,
    ...aggregates.map((a) => ({
      id: a.id,
      source: a.source,
      target: a.target,
      data: { relation: a.relation, bases: a.bases, count: a.count },
    })),
  ].sort((a, b) => a.id.localeCompare(b.id))
  nodes.sort((a, b) => a.id.localeCompare(b.id))
  const overBudget = nodes.length > MAX_VIEW_NODES || edges.length > MAX_VIEW_EDGES
    ? { nodes: nodes.length, edges: edges.length }
    : null
  return {
    nodes: overBudget ? [] : nodes,
    edges: overBudget ? [] : edges,
    groups,
    aggregates: overBudget ? [] : aggregates,
    total: { nodes: payload.nodes.length, edges: payload.edges.length },
    overBudget,
  }
}

/**
 * The starting expansion for a payload. The whole-system graph starts as the
 * overview (nothing expanded). A narrowed scope starts with every group
 * expanded, then collapses the largest groups until the view fits the budget.
 */
export function initialExpanded(
  payload: FlowGraphPayload,
  scope: string,
  pinned: ReadonlySet<string> = new Set(),
  groupsIndex: Map<string, FlowNodePayload[]> = viewGroups(payload),
): Set<string> {
  if (scope === 'all') return new Set()
  const focus = scope.startsWith('node:')
  const expanded = new Set(groupsIndex.keys())
  const bySize = [...groupsIndex].sort(([a, x], [b, y]) => y.length - x.length || a.localeCompare(b)).map(([g]) => g)
  for (const g of bySize) {
    const v = projectView(payload, { expanded, pinned, focus }, groupsIndex)
    if (!v.overBudget) break
    expanded.delete(g)
  }
  return expanded
}

/** The anchor node(s) a scope pins as cards (`node:<id>` → that node; `target:<d>` → the target). */
export function scopePinned(payload: FlowGraphPayload, scope: string): Set<string> {
  if (scope.startsWith('node:')) return new Set([scope.slice('node:'.length)])
  if (scope.startsWith('target:')) {
    const d = scope.slice('target:'.length)
    return new Set(payload.nodes.filter((n) => n.kind === 'target' && n.descriptor_id === d).map((n) => n.id))
  }
  return new Set()
}

/** "3,872 feeds (target.sources[].source_selector)" — the aggregate's line. */
export function aggregateLine(a: ViewAggregate): string {
  return `${a.count.toLocaleString('en-US')} ${a.relation}${a.selector ? ' selector' : ''} edge${a.count === 1 ? '' : 's'}`
}
