/**
 * Projection for the graph WALK panel (`system.graph_walk`, K-G4).
 *
 * Mirrors `GET /api/v1/graph/ego` + `GET /api/v1/graph/edge/{id}`
 * (`src/legba/data/registry/graph_walk_api.py`) and turns an accumulating walk
 * into cytoscape elements.
 *
 * The whole point of this module is that **the three edge families must not
 * look alike**. `entity_edges` is a reified, evidentiary store with tiers that
 * mean genuinely different things, and rendering them identically is what
 * turns a graph into a hairball:
 *
 *   - `relation`     — an asserted claim about the world ("hostile to",
 *                      "allied with"). The only family with a real signed
 *                      distribution (measured live: 205 neg / 324 neutral /
 *                      352 pos), so it is the only one coloured BY POLARITY,
 *                      drawn solid and heaviest, and always labelled.
 *   - `reference`    — institutional membership, mostly seeded. Sky, dashed,
 *                      mid-weight: structural scaffolding rather than a claim
 *                      about behaviour.
 *   - `cooccurrence` — two actors were named in the same document. A
 *                      statistic, not an assertion (~100% polarity 0, and
 *                      8,722 of the graph's 12,566 edges). Faint, dotted,
 *                      thin, never labelled — present as texture, never
 *                      competing with the claims.
 *   - `structural`   — reserved; no rows live yet, styled so the first one is
 *                      immediately legible rather than silently grey.
 *
 * Everything here is pure so it can be unit-tested without a canvas; the
 * cytoscape mount and fetch orchestration live in the panel.
 */
import type { ElementDefinition } from 'cytoscape'
import { entityClassColor, truncate } from '@/lib/graphModel'

// ---- wire types (mirror the Pydantic models) ----

export interface WalkNode {
  id: string
  canonical_name: string
  entity_class: string
  entity_type: string
  geo_country: string | null
  degree: number
  resolved: boolean
}

export type EdgeFamily = 'relation' | 'reference' | 'cooccurrence' | 'structural'

export interface WalkEdge {
  id: string
  src_id: string
  dst_id: string
  direction: 'out' | 'in' | 'stitch'
  edge_family: string
  edge_type: string
  polarity: number
  confidence: number
  observed_count: number
  intent: string
  channel: string
  source_type: string
  valid_from: string | null
  first_seen_at: string | null
  last_seen_at: string | null
  has_evidence: boolean
  signal_count: number
}

export interface WalkFacet {
  edge_family: string
  edge_type: string
  count: number
  negative: number
  neutral: number
  positive: number
}

export interface EgoFilters {
  family: string[]
  edge_type: string[]
  polarity: number[]
  min_confidence: number
  since: string | null
  until: string | null
  direction: string
  limit: number
}

export interface EgoResponse {
  anchor: WalkNode
  nodes: WalkNode[]
  edges: WalkEdge[]
  stitch_edges: WalkEdge[]
  facets: WalkFacet[]
  degree_total: number
  degree_matched: number
  truncated: boolean
  filters: EgoFilters
}

export interface EvidenceSignal {
  id: string
  title: string
  url: string | null
  source_id: string
  fetched_at: string | null
  language: string | null
}

export interface EdgeEvidence {
  edge: WalkEdge
  src: WalkNode
  dst: WalkNode
  evidence_available: boolean
  detail: string
  evidence_text: string
  signals: EvidenceSignal[]
  unresolved_signal_ids: string[]
  signal_count: number
  promoted_from_proposed_edge: string | null
  derived_from: string[]
  analyst_id: string | null
  run_id: string | null
  produced_at: string | null
}

// ---- family + polarity palette ----

export const ALL_FAMILIES: readonly EdgeFamily[] = [
  'relation',
  'reference',
  'cooccurrence',
  'structural',
]

/** Polarity colours — applied to `relation` only, where polarity is real. */
export const POLARITY_COLORS: Record<string, string> = {
  '-1': '#fb7185', // rose-400   — hostile / negative
  '0': '#94a3b8', // slate-400  — neutral
  '1': '#34d399', // emerald-400 — allied / positive
}

export interface FamilyStyle {
  /** Base colour when polarity does not drive it. */
  color: string
  lineStyle: 'solid' | 'dashed' | 'dotted'
  /** Width floor; confidence adds up to `widthGain` on top. */
  width: number
  widthGain: number
  opacity: number
  /** Whether an edge of this family ever carries a text label. */
  labelled: boolean
  /** Human label for the legend. */
  label: string
}

export const FAMILY_STYLES: Record<EdgeFamily, FamilyStyle> = {
  relation: {
    color: POLARITY_COLORS['0'],
    lineStyle: 'solid',
    width: 2,
    widthGain: 2.6,
    opacity: 0.95,
    labelled: true,
    label: 'relation — asserted claim',
  },
  reference: {
    color: '#38bdf8', // sky-400
    lineStyle: 'dashed',
    width: 1.3,
    widthGain: 1.4,
    opacity: 0.72,
    labelled: true,
    label: 'reference — membership',
  },
  cooccurrence: {
    color: '#475569', // slate-600
    lineStyle: 'dotted',
    width: 0.8,
    widthGain: 0.7,
    opacity: 0.3,
    labelled: false,
    label: 'cooccurrence — co-mention',
  },
  structural: {
    color: '#a78bfa', // violet-400
    lineStyle: 'dashed',
    width: 1.8,
    widthGain: 1.0,
    opacity: 0.85,
    labelled: true,
    label: 'structural — derived',
  },
}

export function familyOf(raw: string): EdgeFamily {
  return (ALL_FAMILIES as readonly string[]).includes(raw)
    ? (raw as EdgeFamily)
    : 'cooccurrence'
}

export function familyStyle(raw: string): FamilyStyle {
  return FAMILY_STYLES[familyOf(raw)]
}

/**
 * Edge colour. `relation` is the only family whose polarity carries meaning,
 * so it is the only one that gets the signed ramp — colouring a cooccurrence
 * edge "neutral green" would imply an assertion the store never made.
 */
export function edgeColor(edge: Pick<WalkEdge, 'edge_family' | 'polarity'>): string {
  if (familyOf(edge.edge_family) === 'relation') {
    return POLARITY_COLORS[String(edge.polarity)] ?? POLARITY_COLORS['0']
  }
  return familyStyle(edge.edge_family).color
}

export function edgeWidth(edge: Pick<WalkEdge, 'edge_family' | 'confidence'>): number {
  const style = familyStyle(edge.edge_family)
  const conf = Number.isFinite(edge.confidence) ? Math.min(Math.max(edge.confidence, 0), 1) : 0
  return Number((style.width + conf * style.widthGain).toFixed(2))
}

/**
 * Node diameter from open degree, log-scaled.
 *
 * Degree in this graph spans 1 → 693, so a linear ramp would render everything
 * except the handful of hubs as the same dot. The anchor gets a bump so the
 * thing you are standing on is never ambiguous.
 */
export function nodeSize(degree: number, isAnchor: boolean): number {
  const d = Math.max(0, degree || 0)
  const size = 14 + Math.log2(d + 1) * 3.6
  return Math.round(Math.min(46, size) + (isAnchor ? 9 : 0))
}

// ---- the accumulating canvas ----

/**
 * One anchored hop of the walk, kept so the disclosure surface can describe
 * the WHOLE canvas instead of only the hop it started from.
 *
 * `matched` and the facets are per-ANCHOR quantities: they are true of the
 * neighbourhood of one actor, and two anchors can hold the same edge between
 * them. So they are retained per hop and reported per hop — summing them into
 * a single denominator would double-count exactly the edges that make a walk
 * worth taking.
 */
export interface WalkHop {
  anchorId: string
  name: string
  /** Edges matching the filters for this anchor, BEFORE the limit. */
  matched: number
  /** Edges this hop actually returned. */
  returned: number
  truncated: boolean
  /** This hop's UNFILTERED facets — the honest denominator for ITS anchor. */
  facets: WalkFacet[]
}

export interface WalkCanvas {
  anchorId: string | null
  nodes: Record<string, WalkNode>
  edges: Record<string, WalkEdge>
  /** Nodes the operator has already expanded — they carry no "+" affordance. */
  expanded: string[]
  /** Every hop folded in so far, in walk order. */
  hops: WalkHop[]
}

export const EMPTY_CANVAS: WalkCanvas = {
  anchorId: null,
  nodes: {},
  edges: {},
  expanded: [],
  hops: [],
}

/** Start a fresh walk from one ego response. */
export function seedCanvas(resp: EgoResponse): WalkCanvas {
  return mergeEgo(
    { ...EMPTY_CANVAS, anchorId: resp.anchor.id },
    resp,
    resp.anchor.id,
  )
}

/**
 * Fold an ego response into the canvas.
 *
 * Later responses win on node metadata (a neighbour first seen through a
 * truncated hop may arrive with a stale degree), but `expanded` only ever
 * grows. Stitch edges are merged exactly like ego edges — they are ordinary
 * edges that simply do not touch this hop's anchor.
 *
 * The hop's own disclosure numbers are recorded here too. Without that the
 * panel's honesty surface describes the first hop forever: the canvas grows
 * under a denominator that never moves, and a strip reading "80 of 111" over
 * 237 drawn edges is worse than no strip at all.
 */
export function mergeEgo(
  canvas: WalkCanvas,
  resp: EgoResponse,
  expandedId?: string,
): WalkCanvas {
  const nodes = { ...canvas.nodes }
  const edges = { ...canvas.edges }

  nodes[resp.anchor.id] = resp.anchor
  for (const n of resp.nodes) nodes[n.id] = n
  for (const e of [...resp.edges, ...resp.stitch_edges]) edges[e.id] = e

  const expanded = canvas.expanded.slice()
  if (expandedId && !expanded.includes(expandedId)) expanded.push(expandedId)

  const hop: WalkHop = {
    anchorId: resp.anchor.id,
    name: resp.anchor.canonical_name,
    matched: resp.degree_matched,
    returned: resp.edges.length,
    truncated: resp.truncated,
    facets: resp.facets,
  }
  const hops = canvas.hops.slice()
  const at = hops.findIndex((h) => h.anchorId === hop.anchorId)
  // Re-walking an anchor refreshes its numbers in place rather than appending
  // a second row for the same actor.
  if (at >= 0) hops[at] = hop
  else hops.push(hop)

  return {
    anchorId: canvas.anchorId ?? resp.anchor.id,
    nodes,
    edges,
    expanded,
    hops,
  }
}

/** Edges whose BOTH endpoints are on the canvas — never draw a dangling edge. */
export function drawableEdges(canvas: WalkCanvas): WalkEdge[] {
  return Object.values(canvas.edges).filter(
    (e) => canvas.nodes[e.src_id] && canvas.nodes[e.dst_id],
  )
}

/** How many of a node's edges are already drawn — powers the "+N" affordance. */
export function drawnDegree(canvas: WalkCanvas, nodeId: string): number {
  return drawableEdges(canvas).filter((e) => e.src_id === nodeId || e.dst_id === nodeId)
    .length
}

/**
 * The single visibility predicate. Spelled once so the canvas, the drawn
 * counts and the disclosure strip cannot disagree about what is on screen —
 * the strip claiming a family the canvas is not drawing is defect (3).
 */
export function isFamilyVisible(
  family: string,
  visibleFamilies?: ReadonlySet<string>,
): boolean {
  return (
    !visibleFamilies ||
    visibleFamilies.size === 0 ||
    visibleFamilies.has(familyOf(family))
  )
}

export interface BuildWalkOpts {
  /** Families the operator currently wants drawn. Empty ⇒ all. */
  visibleFamilies?: ReadonlySet<string>
  /** Label every edge (ego detail) vs only the claims. */
  labelEdges?: boolean
}

/**
 * Project the canvas into cytoscape elements.
 *
 * Node `data.unexplored` is the count of edges the store holds for a node
 * beyond what is currently drawn. It is the honest expand affordance: it tells
 * the operator that clicking will actually reveal something, and how much,
 * rather than making every node look equally promising.
 */
export function buildWalkElements(
  canvas: WalkCanvas,
  opts: BuildWalkOpts = {},
): ElementDefinition[] {
  const edges = drawableEdges(canvas).filter((e) =>
    isFamilyVisible(e.edge_family, opts.visibleFamilies),
  )

  // Only draw nodes that still have an edge after family filtering, plus the
  // anchor (which must never vanish from under the operator).
  const connected = new Set<string>()
  for (const e of edges) {
    connected.add(e.src_id)
    connected.add(e.dst_id)
  }
  if (canvas.anchorId) connected.add(canvas.anchorId)

  const drawnByNode = new Map<string, number>()
  for (const e of edges) {
    drawnByNode.set(e.src_id, (drawnByNode.get(e.src_id) ?? 0) + 1)
    drawnByNode.set(e.dst_id, (drawnByNode.get(e.dst_id) ?? 0) + 1)
  }

  const nodeEls: ElementDefinition[] = Object.values(canvas.nodes)
    .filter((n) => connected.has(n.id))
    .map((n) => {
      const isAnchor = n.id === canvas.anchorId
      const drawn = drawnByNode.get(n.id) ?? 0
      const unexplored = Math.max(0, (n.degree || 0) - drawn)
      return {
        data: {
          id: n.id,
          label: truncate(n.canonical_name, 28),
          entity_class: n.entity_class,
          color: n.resolved ? entityClassColor(n.entity_class) : '#64748b',
          size: nodeSize(n.degree, isAnchor),
          degree: n.degree,
          drawn,
          unexplored,
          anchor: isAnchor,
          expanded: canvas.expanded.includes(n.id),
          resolved: n.resolved,
          // Cytoscape selectors cannot test numeric ranges portably, so the
          // boolean the stylesheet keys on is computed here.
          expandable: unexplored > 0 && !canvas.expanded.includes(n.id),
        },
      }
    })

  const edgeEls: ElementDefinition[] = edges.map((e) => {
    const style = familyStyle(e.edge_family)
    const showLabel = opts.labelEdges !== false && style.labelled
    return {
      data: {
        id: e.id,
        source: e.src_id,
        target: e.dst_id,
        label: showLabel ? e.edge_type : '',
        edge_family: familyOf(e.edge_family),
        edge_type: e.edge_type,
        color: edgeColor(e),
        w: edgeWidth(e),
        lineStyle: style.lineStyle,
        opacity: style.opacity,
        polarity: e.polarity,
        confidence: e.confidence,
        has_evidence: e.has_evidence,
      },
    }
  })

  return [...nodeEls, ...edgeEls]
}

// ---- honest disclosure ----

export interface FacetSummary {
  family: EdgeFamily
  count: number
  negative: number
  positive: number
  visible: boolean
}

/**
 * Roll per-type facets up to families.
 *
 * Facets come back UNFILTERED from the API on purpose, so this is what lets
 * the panel say "583 cooccurrence edges hidden" instead of quietly presenting
 * a filtered neighbourhood as the whole one.
 */
export function facetSummary(
  facets: readonly WalkFacet[],
  visibleFamilies?: ReadonlySet<string>,
): FacetSummary[] {
  const byFamily = new Map<EdgeFamily, FacetSummary>()
  for (const f of facets) {
    const fam = familyOf(f.edge_family)
    const cur =
      byFamily.get(fam) ??
      ({ family: fam, count: 0, negative: 0, positive: 0, visible: true } as FacetSummary)
    cur.count += f.count
    cur.negative += f.negative
    cur.positive += f.positive
    byFamily.set(fam, cur)
  }
  const out = [...byFamily.values()]
  for (const row of out) {
    row.visible = isFamilyVisible(row.family, visibleFamilies)
  }
  return out.sort(
    (a, b) => ALL_FAMILIES.indexOf(a.family) - ALL_FAMILIES.indexOf(b.family),
  )
}

/** Total edges the store holds for the anchor that the current view is not showing. */
export function hiddenEdgeCount(summary: readonly FacetSummary[]): number {
  return summary.filter((s) => !s.visible).reduce((n, s) => n + s.count, 0)
}

export interface DrawnFamilyStat {
  family: EdgeFamily
  /** Edges of this family currently ON the canvas. Never a facet count. */
  drawn: number
  negative: number
  positive: number
}

/**
 * Per-family counts of what is ACTUALLY drawn, over the whole merged canvas.
 *
 * The strip used to take its polarity counts from the anchor hop's facets,
 * which are counts over an actor's unfiltered neighbourhood — so it kept
 * announcing "38 hostile" under a canvas holding no hostile edge at all, and
 * it never moved when a second hop doubled the picture. A count taken from the
 * canvas cannot contradict the canvas: a family with nothing drawn produces no
 * row, and every row moves the moment the drawing does.
 */
export function drawnFamilyStats(
  canvas: WalkCanvas,
  visibleFamilies?: ReadonlySet<string>,
): DrawnFamilyStat[] {
  const byFamily = new Map<EdgeFamily, DrawnFamilyStat>()
  for (const e of drawableEdges(canvas)) {
    if (!isFamilyVisible(e.edge_family, visibleFamilies)) continue
    const fam = familyOf(e.edge_family)
    const cur =
      byFamily.get(fam) ?? { family: fam, drawn: 0, negative: 0, positive: 0 }
    cur.drawn += 1
    if (e.polarity < 0) cur.negative += 1
    else if (e.polarity > 0) cur.positive += 1
    byFamily.set(fam, cur)
  }
  return [...byFamily.values()].sort(
    (a, b) => ALL_FAMILIES.indexOf(a.family) - ALL_FAMILIES.indexOf(b.family),
  )
}

export interface HiddenFamilyRow {
  family: EdgeFamily
  /** One entry per hop that is withholding edges of this family. */
  hops: { name: string; count: number }[]
}

/**
 * What each hop's neighbourhood holds that the family filter is withholding.
 *
 * Reported per hop rather than summed: these are counts over a single actor's
 * neighbourhood, and an edge between two anchored actors sits in both of them.
 * Naming the hop is also the more useful answer — "this actor carries 585
 * co-mentions and that one carries 301" is a fact about the walk, where one
 * merged number would only be a fact about arithmetic.
 */
export function hiddenFamilyRows(
  hops: readonly WalkHop[],
  visibleFamilies?: ReadonlySet<string>,
): HiddenFamilyRow[] {
  const byFamily = new Map<EdgeFamily, HiddenFamilyRow>()
  for (const hop of hops) {
    for (const row of facetSummary(hop.facets, visibleFamilies)) {
      if (row.visible || row.count <= 0) continue
      const cur = byFamily.get(row.family) ?? { family: row.family, hops: [] }
      cur.hops.push({ name: hop.name, count: row.count })
      byFamily.set(row.family, cur)
    }
  }
  return [...byFamily.values()].sort(
    (a, b) => ALL_FAMILIES.indexOf(a.family) - ALL_FAMILIES.indexOf(b.family),
  )
}

/** The families actually present in a facet set — drives the legend/chips. */
export function presentFamilies(facets: readonly WalkFacet[]): EdgeFamily[] {
  const seen = new Set<EdgeFamily>()
  for (const f of facets) seen.add(familyOf(f.edge_family))
  return ALL_FAMILIES.filter((f) => seen.has(f))
}

// ---- query-string construction ----

export interface WalkQuery {
  entityId: string
  families: ReadonlySet<string>
  minConfidence: number
  sinceDays: number | null
  limit: number
  known?: readonly string[]
}

/**
 * Build the `/graph/ego` query string.
 *
 * `family` is omitted entirely when every family is selected: sending all four
 * is equivalent but makes the server's index condition needlessly wide, and an
 * omitted filter is what the API documents as "all".
 */
export function egoQueryString(q: WalkQuery): string {
  const params = new URLSearchParams()
  params.set('entity_id', q.entityId)
  if (q.families.size > 0 && q.families.size < ALL_FAMILIES.length) {
    for (const f of ALL_FAMILIES) if (q.families.has(f)) params.append('family', f)
  }
  if (q.minConfidence > 0) params.set('min_confidence', String(q.minConfidence))
  if (q.sinceDays && q.sinceDays > 0) {
    const since = new Date(Date.now() - q.sinceDays * 86_400_000)
    params.set('since', since.toISOString())
  }
  params.set('limit', String(q.limit))
  for (const k of q.known ?? []) params.append('known', k)
  return params.toString()
}
