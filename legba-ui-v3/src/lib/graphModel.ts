/**
 * Entity / relationship-graph data layer (UI-3 / Tier B — Graph panel).
 *
 * The substrate has an Apache AGE graph but no dedicated `/entities/graph`
 * REST endpoint (frozen surface). The honest available source for a
 * *relationship* graph is the lineage walk
 * (`GET /api/v1/lineage/{kind}/{id}?direction=both&depth=N`), which returns
 * a `root`, a flat `nodes` list (each carrying `row_kind`), and *real*
 * `edges` (parent→child derivation tuples — NOT synthesized).
 *
 * This module is the single pure home for projecting that report into
 * cytoscape elements + deriving the relationship-type taxonomy, so it can
 * be unit-tested without a DOM (mirrors `timelinePoints.ts` / `geoPoints.ts`).
 *
 * --- Relationship types ---
 * v2's Graph scored "edges colored by relationship type + edge-type
 * checkboxes filter visible relationships". In the provenance graph the
 * relationship between a parent and child *is* the derivation, and its
 * type is most usefully read as "what kind of row was produced", i.e. the
 * child node's `row_kind` (signal→finding, finding→situation, …). We
 * expose that as the edge `rel` so the panel can color + filter by it.
 */

/** Lineage node — mirrors `LineageNode` in `data/registry/lineage_api.py`. */
export interface LineageNode {
  id: string
  row_kind: string
  title: string | null
  produced_at: string
  target_id: string | null
  analyst_id: string | null
  schema_uri: string
  depth: number
  /** The report payload (summary/body/assessment/…) — present on the ROOT node
   *  only; the Inspector renders it as the report. Null on walk nodes. */
  body?: Record<string, unknown> | null
}

/** Lineage edge — `parent` ∈ `child.derived_from`. */
export interface LineageEdge {
  parent: string
  child: string
}

/** Response body of `GET /api/v1/lineage/{kind}/{id}`. */
export interface LineageReport {
  root: LineageNode
  nodes: LineageNode[]
  edges: LineageEdge[]
  truncated_at_depth?: boolean
}

/** Substrate row kinds the lineage walk can surface (lineage_api `_TABLES_BY_KIND`). */
export const ROW_KINDS = [
  'signal',
  'finding',
  'meta_finding',
  'alert',
  'critique',
  'situation',
  'hypothesis',
  'prediction',
  'prompt_module_candidate',
] as const
export type RowKind = (typeof ROW_KINDS)[number]

/**
 * Palette tokens hard-coded as hex — cytoscape's stylesheet doesn't run
 * through PostCSS, so we can't use Tailwind class names there.
 */
export const KIND_COLORS: Record<string, string> = {
  signal: '#60a5fa', // blue-400
  finding: '#fcd34d', // amber-300
  meta_finding: '#fbbf24', // amber-400
  alert: '#f87171', // red-400
  critique: '#a78bfa', // violet-400
  situation: '#34d399', // emerald-400
  hypothesis: '#fb7185', // rose-400
  prediction: '#22d3ee', // cyan-400
  prompt_module_candidate: '#c084fc', // purple-400
}
export const KIND_DEFAULT_COLOR = '#94a3b8' // slate-400

export function kindColor(kind: string): string {
  return KIND_COLORS[kind] ?? KIND_DEFAULT_COLOR
}

export interface GraphNodeData {
  id: string
  label: string
  title: string
  row_kind: string
  produced_at: string
  analyst_id: string | null
  target_id: string | null
  is_root: boolean
  color: string
}

export interface GraphEdgeData {
  id: string
  source: string
  target: string
  /** Relationship type = child node's row_kind (derived `rel`). */
  rel: string
  color: string
}

export interface GraphElements {
  nodes: Array<{ data: GraphNodeData }>
  edges: Array<{ data: GraphEdgeData }>
}

/** Truncate a label to `n` chars with an ellipsis. */
export function truncate(s: string | null | undefined, n: number): string {
  if (!s) return ''
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`
}

/**
 * The distinct relationship types present in a report, sorted by the
 * canonical ROW_KINDS order then alpha. Drives the filter checkboxes.
 */
export function relationshipTypes(report: LineageReport | undefined): string[] {
  if (!report) return []
  const childKind = new Map<string, string>()
  childKind.set(report.root.id, report.root.row_kind)
  for (const n of report.nodes) childKind.set(n.id, n.row_kind)
  const set = new Set<string>()
  for (const e of report.edges) {
    const rel = childKind.get(e.child)
    if (rel) set.add(rel)
  }
  return [...set].sort((a, b) => {
    const ia = (ROW_KINDS as readonly string[]).indexOf(a)
    const ib = (ROW_KINDS as readonly string[]).indexOf(b)
    if (ia !== ib) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
    return a.localeCompare(b)
  })
}

/**
 * Project a lineage report into cytoscape node/edge element data, honoring
 * an optional relationship-type allowlist. When `visibleRels` is provided,
 * edges whose `rel` is not in the set are dropped, and nodes left with no
 * incident (kept) edge — other than the root — are dropped too, so the
 * filter genuinely prunes the rendered graph (v2 "edge-type checkboxes
 * filter visible relationships").
 */
export function projectGraph(
  report: LineageReport | undefined,
  visibleRels?: ReadonlySet<string>,
): GraphElements {
  if (!report) return { nodes: [], edges: [] }

  const nodeById = new Map<string, LineageNode>()
  nodeById.set(report.root.id, report.root)
  for (const n of report.nodes) nodeById.set(n.id, n)

  // Edge `rel` = child node's row_kind (the produced relationship type).
  const allEdges = report.edges
    .map((e) => {
      const child = nodeById.get(e.child)
      const rel = child?.row_kind ?? 'unknown'
      return { ...e, rel }
    })
    .filter((e) => nodeById.has(e.parent) && nodeById.has(e.child))

  const keptEdges = visibleRels ? allEdges.filter((e) => visibleRels.has(e.rel)) : allEdges

  // Nodes touched by a kept edge — plus the root, which always renders so
  // the panel never goes blank when a filter strips every edge.
  const touched = new Set<string>([report.root.id])
  for (const e of keptEdges) {
    touched.add(e.parent)
    touched.add(e.child)
  }

  const nodes: GraphElements['nodes'] = []
  for (const id of touched) {
    const n = nodeById.get(id)
    if (!n) continue
    nodes.push({
      data: {
        id: n.id,
        label: truncate(n.title ?? n.row_kind, 28),
        title: n.title ?? '(untitled)',
        row_kind: n.row_kind,
        produced_at: n.produced_at,
        analyst_id: n.analyst_id,
        target_id: n.target_id,
        is_root: n.id === report.root.id,
        color: kindColor(n.row_kind),
      },
    })
  }

  const edges: GraphElements['edges'] = keptEdges.map((e) => ({
    data: {
      id: `${e.parent}->${e.child}`,
      source: e.parent,
      target: e.child,
      rel: e.rel,
      color: kindColor(e.rel),
    },
  }))

  return { nodes, edges }
}

// ---------------------------------------------------------------------------
// UI-3 depth-bounded, row-kind-filtered projection (Graph panel)
// ---------------------------------------------------------------------------

/** Coerce an arbitrary lineage row_kind string to a known RowKind, else 'finding'. */
export function toRowKind(k: string | null | undefined): RowKind {
  return (ROW_KINDS as readonly string[]).includes(k ?? '') ? (k as RowKind) : 'finding'
}

/**
 * The distinct row_kinds actually present in a report (root + nodes), in
 * canonical ROW_KINDS order. Drives the row-kind filter chips — only kinds
 * present render, so the operator never sees a dead chip.
 */
export function presentRowKinds(report: LineageReport | undefined): string[] {
  if (!report) return []
  const set = new Set<string>([report.root.row_kind])
  for (const n of report.nodes) set.add(n.row_kind)
  return [...set].sort((a, b) => {
    const ia = (ROW_KINDS as readonly string[]).indexOf(a)
    const ib = (ROW_KINDS as readonly string[]).indexOf(b)
    if (ia !== ib) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
    return a.localeCompare(b)
  })
}

export interface BuildLineageOpts {
  /** Drop nodes whose `depth` exceeds this (root depth 0 always kept). 1..4. */
  maxDepth?: number
  /** Allowlist of row_kinds to render (root always kept). Undefined ⇒ all. */
  visibleKinds?: ReadonlySet<string>
}

/**
 * Project a lineage report into cytoscape elements honouring a depth bound
 * AND a row-kind allowlist, with **orphan-safe edge re-parenting**: when an
 * intermediate node is hidden (by depth or by kind), its children are
 * reconnected to the nearest *visible* ancestor so the graph stays one
 * connected component instead of shedding subtrees. The root always renders.
 */
export function buildLineageElements(
  report: LineageReport | undefined,
  opts: BuildLineageOpts = {},
): GraphElements {
  if (!report) return { nodes: [], edges: [] }
  const maxDepth = opts.maxDepth ?? Infinity
  const visibleKinds = opts.visibleKinds

  const nodeById = new Map<string, LineageNode>()
  nodeById.set(report.root.id, report.root)
  for (const n of report.nodes) nodeById.set(n.id, n)

  // parent[child] = parent — the report's edges are parent→child derivations.
  const parentOf = new Map<string, string>()
  for (const e of report.edges) {
    if (nodeById.has(e.parent) && nodeById.has(e.child)) parentOf.set(e.child, e.parent)
  }

  const isVisible = (id: string): boolean => {
    const n = nodeById.get(id)
    if (!n) return false
    if (id === report.root.id) return true
    if (n.depth > maxDepth) return false
    if (visibleKinds && !visibleKinds.has(n.row_kind)) return false
    return true
  }

  // Re-parent: walk up the parent chain until a visible ancestor is found
  // (or the chain runs out). Cycle-guarded.
  const nearestVisibleAncestor = (id: string): string | null => {
    const seen = new Set<string>([id])
    let cur = parentOf.get(id)
    while (cur && !seen.has(cur)) {
      if (isVisible(cur)) return cur
      seen.add(cur)
      cur = parentOf.get(cur)
    }
    return null
  }

  const visibleIds = [...nodeById.keys()].filter(isVisible)

  const nodes: GraphElements['nodes'] = []
  for (const id of visibleIds) {
    const n = nodeById.get(id)!
    nodes.push({
      data: {
        id: n.id,
        label: truncate(n.title ?? n.row_kind, 28),
        title: n.title ?? '(untitled)',
        row_kind: n.row_kind,
        produced_at: n.produced_at,
        analyst_id: n.analyst_id,
        target_id: n.target_id,
        is_root: n.id === report.root.id,
        color: kindColor(n.row_kind),
      },
    })
  }

  // Re-parent each derivation edge onto visible endpoints. For an original
  // parent→child edge whose child is visible: if the parent is hidden, hop
  // up the parent's derivation chain to the nearest visible ancestor and
  // re-attach there (so hiding an intermediate doesn't shed its subtree).
  // Edges whose child is hidden are dropped — that child's descendants
  // re-attach via their own walk. De-duped (several can collapse onto one).
  const edgeSeen = new Set<string>()
  const edges: GraphElements['edges'] = []
  for (const e of report.edges) {
    if (!nodeById.has(e.parent) || !nodeById.has(e.child)) continue
    if (!isVisible(e.child)) continue
    const parent = isVisible(e.parent) ? e.parent : nearestVisibleAncestor(e.parent)
    if (!parent || parent === e.child) continue
    const edgeId = `${parent}->${e.child}`
    if (edgeSeen.has(edgeId)) continue
    edgeSeen.add(edgeId)
    const rel = nodeById.get(e.child)?.row_kind ?? 'unknown'
    edges.push({
      data: { id: edgeId, source: parent, target: e.child, rel, color: kindColor(rel) },
    })
  }

  return { nodes, edges }
}
