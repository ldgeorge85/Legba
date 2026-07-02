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

/**
 * The receipt-chain receipt for the analyst run that produced a row — mirrors
 * `ReceiptChainNode` in `data/registry/lineage_api.py`. HONESTY CONTRACT: this
 * is a SHA-256 hash-chain, NOT an Ed25519 signature. `chain_consistent` is
 * RE-COMPUTED server-side (the trace is re-hashed and compared to the stored
 * `receipt_hash`); a mutated / forked row re-hashes differently → `false`.
 * `badge` is fixed to the honest string `'chain-consistent (single-node)'` —
 * never "signed" / "tamper-proof".
 */
export interface ReceiptChainNode {
  run_id: string
  receipt_hash: string
  prev_receipt_hash: string | null
  /** RE-COMPUTED per node, not the trust of a stored flag. */
  chain_consistent: boolean
  /** Present only when an audit_checkpoint covers this row's receipt_hash. */
  signer_did?: string | null
  /** Fixed honest label — `'chain-consistent (single-node)'`. */
  badge: string
}

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
  /** The acquisition source's real article URL — populated for signal rows, null
   *  for analyst-output / situation kinds. The clickable END of a lineage walk. */
  canonical_url?: string | null
  media_ref?: string | null
  modality?: string | null
  mime_type?: string | null
  /** The receipt-chain receipt for the run that produced this row — present on
   *  the ROOT *and* every walk node that maps to an analyst run (P1-T4). Signals
   *  / source-ingested rows carry `null` honestly (no producing trace to
   *  re-hash). See {@link ReceiptChainNode} for the honesty contract. */
  receipt?: ReceiptChainNode | null
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

// ---------------------------------------------------------------------------
// Entity knowledge-graph palettes (the old "Knowledge Graph" — screenshot 19).
//
// The entity graph (`GET /entities/graph`) carries a node `entity_class` and an
// edge `relationship_type` drawn from the substrate vocabulary
// (`legba.data.vocabulary` — ENTITY_CLASSES / RELATIONSHIP_TYPES). The old KG
// coloured NODES by entity class and EDGES by relationship type, with a chip
// row per node class and a colour legend per relationship type. These palettes
// are the single home for that mapping so the cytoscape stylesheets (which
// bypass PostCSS — hex only) and the React GraphControls (chips + legend) stay
// in lock-step. Hex values mirror the dark old-KG palette.
// ---------------------------------------------------------------------------

/** Node colour keyed by entity_class — the substrate ENTITY_CLASSES vocabulary
 *  plus the UI-recolored `country` (NER tags countries `entity`; the panels
 *  promote recognized countries to a distinct class for legibility). */
export const ENTITY_CLASS_COLORS: Record<string, string> = {
  country: '#38bdf8', // sky-400  — nation states (the dense hubs)
  person: '#f59e0b', // amber-500
  organization: '#60a5fa', // blue-400
  event: '#a78bfa', // violet-400
  location: '#34d399', // emerald-400
  region: '#2dd4bf', // teal-400
  concept: '#f472b6', // pink-400
  corporation: '#818cf8', // indigo-400
  software: '#22d3ee', // cyan-400
  entity: '#94a3b8', // slate-400 — the generic / unclassified bucket
}
export const ENTITY_CLASS_DEFAULT_COLOR = '#94a3b8' // slate-400

/** Canonical render order for the entity-class chip row (hubs first, generic
 *  last), mirroring the substrate vocabulary ordering. */
export const ENTITY_CLASS_ORDER = [
  'country',
  'person',
  'organization',
  'event',
  'location',
  'region',
  'concept',
  'corporation',
  'software',
  'entity',
] as const

export function entityClassColor(entityClass: string): string {
  return ENTITY_CLASS_COLORS[entityClass] ?? ENTITY_CLASS_DEFAULT_COLOR
}

/** Edge colour keyed by relationship_type — the substrate RELATIONSHIP_TYPES
 *  vocabulary. Distinct hues so a dense graph reads as a coloured network, with
 *  the alliance/hostility pair anchored green/red (the old-KG convention). */
export const RELATIONSHIP_COLORS: Record<string, string> = {
  AlliedWith: '#34d399', // emerald-400 — cooperation
  HostileTo: '#f87171', // red-400     — conflict
  Targets: '#fb7185', // rose-400
  SuppliesWeaponsTo: '#fbbf24', // amber-400
  PartyTo: '#c084fc', // purple-400
  ConductedVia: '#a78bfa', // violet-400
  LeaderOf: '#fcd34d', // amber-300
  MemberOf: '#60a5fa', // blue-400
  AffiliatedWith: '#818cf8', // indigo-400
  OperatesIn: '#2dd4bf', // teal-400
  LocatedIn: '#22d3ee', // cyan-400
  PartOf: '#38bdf8', // sky-400
  InvolvedIn: '#f472b6', // pink-400
  CoOccursWith: '#94a3b8', // slate-400 — the weak co-occurrence default
}
export const RELATIONSHIP_DEFAULT_COLOR = '#64748b' // slate-500

/** Canonical render order for the relationship legend (mirrors the substrate
 *  RELATIONSHIP_TYPES tuple). */
export const RELATIONSHIP_ORDER = [
  'AlliedWith',
  'HostileTo',
  'Targets',
  'SuppliesWeaponsTo',
  'PartyTo',
  'ConductedVia',
  'LeaderOf',
  'MemberOf',
  'AffiliatedWith',
  'OperatesIn',
  'LocatedIn',
  'PartOf',
  'InvolvedIn',
  'CoOccursWith',
] as const

export function relationshipColor(relType: string): string {
  return RELATIONSHIP_COLORS[relType] ?? RELATIONSHIP_DEFAULT_COLOR
}

/**
 * Order a set of present values by a canonical reference order, with any
 * leftover (vocabulary-drift) values appended alphabetically — so a chip/legend
 * row reads in the familiar order yet never silently drops an unknown value.
 */
export function orderByReference(present: Iterable<string>, reference: readonly string[]): string[] {
  const set = new Set(present)
  const ordered: string[] = []
  for (const r of reference) {
    if (set.has(r)) {
      ordered.push(r)
      set.delete(r)
    }
  }
  return [...ordered, ...[...set].sort((a, b) => a.localeCompare(b))]
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

// ---------------------------------------------------------------------------
// Entity knowledge-graph projection (the old "Knowledge Graph").
//
// The entity graph endpoint returns plain nodes (class + mention count) and
// co-occurrence-style edges (relationship_type + confidence). The old KG read
// as a *connected network*, not confetti: degree-0 singletons were dropped,
// nodes were sized by degree, and only hubs were labelled. This pure projector
// is the single home for that shaping so the panels stay thin + it's testable
// without a DOM (mirrors the lineage `projectGraph`).
// ---------------------------------------------------------------------------

/** Minimal entity node the projector needs — a structural subset of the
 *  endpoint's `EntityNode` (the panels pass the live rows straight through). */
export interface EntityGraphNode {
  /** Cytoscape id = canonical name (edge endpoints reference it). */
  id: string
  label: string
  entity_class: string
  /** Mention count — a secondary size signal (degree is primary). */
  mentions?: number
}

/** Minimal entity edge — a structural subset of the endpoint's `GraphEdge`. */
export interface EntityGraphEdge {
  source: string
  target: string
  relationship_type: string
  confidence: number
}

export interface EntityNodeData {
  id: string
  label: string
  /** Full (untruncated) name for tooltips / selection labels. */
  name: string
  entity_class: string
  /** Incident-edge count (after orphan drop). Drives node size + label gating. */
  degree: number
  /** Node diameter in px (degree-scaled). */
  size: number
  /** Whether this node's label should render (hubs only, to avoid clutter). */
  show_label: boolean
  color: string
}

export interface EntityEdgeData {
  id: string
  source: string
  target: string
  relationship_type: string
  /** Edge width (confidence-scaled). */
  w: number
  color: string
}

export interface EntityGraphElements {
  nodes: Array<{ data: EntityNodeData }>
  edges: Array<{ data: EntityEdgeData }>
}

export interface BuildEntityGraphOpts {
  /** Entity classes to render. Undefined ⇒ all. A node whose class is filtered
   *  out is dropped (along with its now-dangling edges). */
  visibleClasses?: ReadonlySet<string>
  /** Relationship types to render. Undefined ⇒ all. */
  visibleRels?: ReadonlySet<string>
  /** Keep degree-0 singletons (default false — the old KG dropped the ~70
   *  scattered dots so it read as a connected network, not confetti). */
  showOrphans?: boolean
  /** A node id to always keep + emphasise (the ego centre, for the Why graph).
   *  It renders even at degree 0 so the ego view never goes blank. */
  centerId?: string
}

/** Distinct entity classes present across a node list, in canonical order. */
export function presentEntityClasses(nodes: ReadonlyArray<{ entity_class: string }>): string[] {
  return orderByReference(
    nodes.map((n) => n.entity_class),
    ENTITY_CLASS_ORDER,
  )
}

/** Distinct relationship types present across an edge list, in canonical order. */
export function presentRelationshipTypes(
  edges: ReadonlyArray<{ relationship_type: string }>,
): string[] {
  return orderByReference(
    edges.map((e) => e.relationship_type),
    RELATIONSHIP_ORDER,
  )
}

/** Map a degree onto a node diameter (~16–58px), sqrt-scaled so a few huge hubs
 *  don't dwarf the rest. The ego centre gets a fixed emphasis bump elsewhere. */
function degreeSize(degree: number): number {
  return Math.min(58, 16 + Math.sqrt(degree) * 7)
}

/** Map a confidence (0..1) onto an edge width (~1–4px). */
function confidenceWidth(confidence: number): number {
  const c = Number.isFinite(confidence) ? Math.min(Math.max(confidence, 0), 1) : 0
  return 1 + c * 3
}

/**
 * Project raw entity nodes + edges into cytoscape-ready element data, honoring
 * class/relationship allowlists, dropping degree-0 orphans (unless asked to
 * keep them), sizing nodes by degree, and gating labels to hubs (degree above
 * a small threshold) so a dense graph stays legible. Pure + DOM-free.
 */
export function buildEntityGraphElements(
  rawNodes: ReadonlyArray<EntityGraphNode>,
  rawEdges: ReadonlyArray<EntityGraphEdge>,
  opts: BuildEntityGraphOpts = {},
): EntityGraphElements {
  const { visibleClasses, visibleRels, showOrphans = false, centerId } = opts

  // 1. Class filter — drop hidden-class nodes up front so their edges dangle.
  const classKept = new Map<string, EntityGraphNode>()
  for (const n of rawNodes) {
    if (visibleClasses && !visibleClasses.has(n.entity_class)) continue
    classKept.set(n.id, n)
  }

  // 2. Edge filter — both endpoints must survive the class filter, no
  //    self-loops, and the relationship type must pass the rel allowlist.
  const keptEdges: EntityGraphEdge[] = []
  for (const e of rawEdges) {
    if (e.source === e.target) continue
    if (!classKept.has(e.source) || !classKept.has(e.target)) continue
    if (visibleRels && !visibleRels.has(e.relationship_type)) continue
    keptEdges.push(e)
  }

  // 3. Degree (after all filtering) — drives size, label gating, orphan drop.
  const degree = new Map<string, number>()
  for (const id of classKept.keys()) degree.set(id, 0)
  for (const e of keptEdges) {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1)
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1)
  }

  // 4. Orphan drop — unless asked to show them, or the node is the ego centre.
  const renderIds = new Set<string>()
  for (const [id] of classKept) {
    const d = degree.get(id) ?? 0
    if (showOrphans || d > 0 || id === centerId) renderIds.add(id)
  }
  // The ego centre always renders even if its class was filtered out.
  if (centerId) renderIds.add(centerId)

  // Label gating: only label nodes at/above a degree threshold (hubs) so the
  // graph isn't a wall of text. Scale the threshold to the graph's own top
  // degree so a sparse ego graph still labels its handful of neighbours.
  const maxDeg = Math.max(0, ...[...renderIds].map((id) => degree.get(id) ?? 0))
  const labelThreshold = maxDeg <= 6 ? 1 : Math.max(2, Math.ceil(maxDeg * 0.15))

  const nodes: EntityGraphElements['nodes'] = []
  for (const id of renderIds) {
    const n = classKept.get(id)
    // The ego centre can be referenced by an edge but absent from the class-kept
    // map (its class was filtered) — fall back to a minimal synthetic node.
    const entity_class = n?.entity_class ?? 'entity'
    const name = n?.label ?? id
    const d = degree.get(id) ?? 0
    const isCenter = id === centerId
    nodes.push({
      data: {
        id,
        label: truncate(name, 24),
        name,
        entity_class,
        degree: d,
        size: isCenter ? Math.max(34, degreeSize(d)) : degreeSize(d),
        show_label: isCenter || d >= labelThreshold,
        color: entityClassColor(entity_class),
      },
    })
  }

  // Edges last — only those whose endpoints both survived the orphan drop.
  const edges: EntityGraphElements['edges'] = []
  let i = 0
  for (const e of keptEdges) {
    if (!renderIds.has(e.source) || !renderIds.has(e.target)) continue
    edges.push({
      data: {
        id: `e${i++}`,
        source: e.source,
        target: e.target,
        relationship_type: e.relationship_type,
        w: confidenceWidth(e.confidence),
        color: relationshipColor(e.relationship_type),
      },
    })
  }

  return { nodes, edges }
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

// ---------------------------------------------------------------------------
// Progressive one-hop-at-a-time reveal (P1-T5 — The Why's signed-lineage walk).
//
// The drill-down must be *walkable a step at a time*: show the root, then reveal
// the next hop on demand, each hop carrying its honest receipt badge, the trail
// ending at a real source URL. These pure helpers are the single home for that
// projection so `LineageGraph` (the cytoscape surface) and `ProvenanceTrail`
// (the textual chip line) stay in lock-step + are testable without a DOM.
// ---------------------------------------------------------------------------

/** The honest per-row receipt badge string (mirrors `_RECEIPT_BADGE` in
 *  `lineage_api.py`). A SHA-256 hash-chain *consistency* check — NOT a
 *  signature; never render this as "signed" / "tamper-proof". */
export const RECEIPT_BADGE = 'chain-consistent (single-node)'

/**
 * The single PRIMARY PATH of a lineage report, ordered oldest (deepest ancestor
 * / source) → newest (the selected root). A row can have several parents (it's a
 * DAG); we follow the parent that climbs furthest back (greatest `depth`, ties
 * broken by the oldest `produced_at`) so the trail reads as one line. The walk
 * is cycle-guarded (the substrate walk can re-converge). Empty for no report.
 */
export function buildPrimaryTrail(report: LineageReport | undefined): LineageNode[] {
  if (!report) return []
  const byId = new Map<string, LineageNode>()
  byId.set(report.root.id, report.root)
  for (const n of report.nodes) byId.set(n.id, n)

  // parents[child] = [parent ids] — report edges are parent→child derivations.
  const parents = new Map<string, string[]>()
  for (const e of report.edges) {
    if (!byId.has(e.parent) || !byId.has(e.child)) continue
    const arr = parents.get(e.child)
    if (arr) arr.push(e.parent)
    else parents.set(e.child, [e.parent])
  }

  // Walk root → deepest ancestor (newest → oldest), then reverse.
  const newestToOldest: LineageNode[] = []
  const seen = new Set<string>()
  let cur: LineageNode | undefined = report.root
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    newestToOldest.push(cur)
    let next: LineageNode | undefined
    for (const pid of parents.get(cur.id) ?? []) {
      if (seen.has(pid)) continue
      const p = byId.get(pid)
      if (!p) continue
      if (
        !next ||
        p.depth > next.depth ||
        (p.depth === next.depth && p.produced_at < next.produced_at)
      ) {
        next = p
      }
    }
    cur = next
  }
  return newestToOldest.reverse()
}

/** The deepest `depth` present across a report's walk nodes (0 when the root has
 *  no recorded derivations). Bounds the progressive reveal. */
export function maxLineageDepth(report: LineageReport | undefined): number {
  if (!report) return 0
  let m = 0
  for (const n of report.nodes) if (n.depth > m) m = n.depth
  return m
}

export interface ProgressiveLineage {
  /** Cytoscape elements for the DAG revealed up to `revealedDepth`. */
  elements: GraphElements
  /** The deepest hop available in the report. */
  maxDepth: number
  /** The depth actually revealed (clamped to `[1, max(1, maxDepth)]`). */
  revealedDepth: number
  /** Whether every available hop is now revealed. */
  atFull: boolean
}

/**
 * Project a lineage report into cytoscape elements revealed ONE HOP AT A TIME:
 * `revealedDepth` bounds the visible derivation rings (reusing
 * {@link buildLineageElements}' depth gate + orphan-safe re-parenting), so the
 * panel starts as a short trail and grows a ring per "expand". `revealedDepth`
 * is clamped to at least 1 (the root + its first hop) and at most the report's
 * own `maxDepth`, so a click can never reveal a hop that isn't there (no
 * dead-ends). Pure + DOM-free.
 */
export function buildProgressiveLineageElements(
  report: LineageReport | undefined,
  revealedDepth: number,
  opts: { visibleKinds?: ReadonlySet<string> } = {},
): ProgressiveLineage {
  const maxDepth = maxLineageDepth(report)
  const clamped = Math.max(1, Math.min(revealedDepth, Math.max(1, maxDepth)))
  const elements = buildLineageElements(report, {
    maxDepth: clamped,
    visibleKinds: opts.visibleKinds,
  })
  return { elements, maxDepth, revealedDepth: clamped, atFull: clamped >= maxDepth }
}
