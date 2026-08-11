import { describe, expect, it } from 'vitest'
import {
  ALL_FAMILIES,
  EMPTY_CANVAS,
  POLARITY_COLORS,
  buildWalkElements,
  drawableEdges,
  drawnDegree,
  drawnFamilyStats,
  edgeColor,
  edgeWidth,
  egoQueryString,
  facetSummary,
  familyOf,
  familyStyle,
  hiddenEdgeCount,
  hiddenFamilyRows,
  mergeEgo,
  nodeSize,
  presentFamilies,
  seedCanvas,
  type EgoResponse,
  type WalkEdge,
  type WalkNode,
} from '@/lib/graphWalkModel'

const US = '66c795b7-73ba-44a3-9cfe-60cc2b7dfbb9'
const IRAN = '8e7c0a9e-4950-40af-8ced-06c55c4923ae'
const RUSSIA = '1f64f392-0751-45cf-96e4-a8d9d94762dd'

function node(id: string, name: string, degree = 10): WalkNode {
  return {
    id,
    canonical_name: name,
    entity_class: 'country',
    entity_type: 'country',
    geo_country: null,
    degree,
    resolved: true,
  }
}

function edge(
  id: string,
  src: string,
  dst: string,
  over: Partial<WalkEdge> = {},
): WalkEdge {
  return {
    id,
    src_id: src,
    dst_id: dst,
    direction: 'out',
    edge_family: 'relation',
    edge_type: 'hostile to',
    polarity: -1,
    confidence: 0.9,
    observed_count: 2,
    intent: 'hostile',
    channel: 'direct',
    source_type: 'agent',
    valid_from: null,
    first_seen_at: null,
    last_seen_at: null,
    has_evidence: true,
    signal_count: 3,
    ...over,
  }
}

function ego(over: Partial<EgoResponse> = {}): EgoResponse {
  return {
    anchor: node(US, 'United States', 693),
    nodes: [node(IRAN, 'Iran', 595)],
    edges: [edge('e1', US, IRAN)],
    stitch_edges: [],
    facets: [],
    degree_total: 693,
    degree_matched: 1,
    truncated: false,
    filters: {
      family: [],
      edge_type: [],
      polarity: [],
      min_confidence: 0,
      since: null,
      until: null,
      direction: 'both',
      limit: 80,
    },
    ...over,
  }
}

// ---- family distinctness: the core of the brief ----

describe('family rendering is visually distinct', () => {
  it('gives every family its own line style', () => {
    const styles = ALL_FAMILIES.map((f) => familyStyle(f).lineStyle)
    // relation solid, cooccurrence dotted — the two that must never be confused
    expect(familyStyle('relation').lineStyle).toBe('solid')
    expect(familyStyle('cooccurrence').lineStyle).toBe('dotted')
    expect(familyStyle('reference').lineStyle).toBe('dashed')
    expect(new Set(styles).size).toBeGreaterThan(1)
  })

  it('draws relation heaviest and cooccurrence lightest', () => {
    const rel = edgeWidth({ edge_family: 'relation', confidence: 1 })
    const ref = edgeWidth({ edge_family: 'reference', confidence: 1 })
    const co = edgeWidth({ edge_family: 'cooccurrence', confidence: 1 })
    expect(rel).toBeGreaterThan(ref)
    expect(ref).toBeGreaterThan(co)
  })

  it('makes cooccurrence recede and relation dominate by opacity', () => {
    expect(familyStyle('cooccurrence').opacity).toBeLessThan(
      familyStyle('relation').opacity,
    )
  })

  it('never labels a cooccurrence edge', () => {
    expect(familyStyle('cooccurrence').labelled).toBe(false)
    expect(familyStyle('relation').labelled).toBe(true)
  })

  it('applies the signed palette ONLY to relation', () => {
    // relation carries the only real signed distribution in the store
    expect(edgeColor({ edge_family: 'relation', polarity: -1 })).toBe(
      POLARITY_COLORS['-1'],
    )
    expect(edgeColor({ edge_family: 'relation', polarity: 1 })).toBe(
      POLARITY_COLORS['1'],
    )
    // a cooccurrence edge is a statistic, not a claim — colouring it by
    // polarity would imply an assertion the store never made
    expect(edgeColor({ edge_family: 'cooccurrence', polarity: 1 })).toBe(
      familyStyle('cooccurrence').color,
    )
    expect(edgeColor({ edge_family: 'reference', polarity: -1 })).toBe(
      familyStyle('reference').color,
    )
  })

  it('falls back to cooccurrence styling for an unknown family', () => {
    expect(familyOf('nonsense')).toBe('cooccurrence')
  })
})

describe('nodeSize', () => {
  it('log-scales so a degree-1 node and a degree-693 hub differ but stay bounded', () => {
    const small = nodeSize(1, false)
    const hub = nodeSize(693, false)
    expect(hub).toBeGreaterThan(small)
    expect(hub).toBeLessThanOrEqual(46)
  })

  it('bumps the anchor so it is never ambiguous', () => {
    expect(nodeSize(10, true)).toBeGreaterThan(nodeSize(10, false))
  })

  it('handles a zero/absent degree without NaN', () => {
    expect(Number.isFinite(nodeSize(0, false))).toBe(true)
  })
})

// ---- the accumulating walk ----

describe('canvas accumulation', () => {
  it('seeds from one ego response', () => {
    const c = seedCanvas(ego())
    expect(c.anchorId).toBe(US)
    expect(Object.keys(c.nodes).sort()).toEqual([US, IRAN].sort())
    expect(c.expanded).toEqual([US])
  })

  it('merges a second hop without losing the first', () => {
    let c = seedCanvas(ego())
    c = mergeEgo(
      c,
      ego({
        anchor: node(IRAN, 'Iran', 595),
        nodes: [node(RUSSIA, 'Russia', 592)],
        edges: [edge('e2', IRAN, RUSSIA)],
      }),
      IRAN,
    )
    expect(Object.keys(c.nodes).sort()).toEqual([US, IRAN, RUSSIA].sort())
    expect(Object.keys(c.edges).sort()).toEqual(['e1', 'e2'])
    // the anchor of the WALK does not move when you expand a neighbour
    expect(c.anchorId).toBe(US)
    expect(c.expanded).toEqual([US, IRAN])
  })

  it('folds stitch edges in exactly like ego edges', () => {
    let c = seedCanvas(ego())
    c = mergeEgo(
      c,
      ego({
        anchor: node(IRAN, 'Iran'),
        nodes: [node(RUSSIA, 'Russia')],
        edges: [],
        stitch_edges: [edge('s1', RUSSIA, US, { direction: 'stitch' })],
      }),
      IRAN,
    )
    expect(c.edges['s1']).toBeDefined()
  })

  it('never records the same expansion twice', () => {
    let c = seedCanvas(ego())
    c = mergeEgo(c, ego(), US)
    expect(c.expanded).toEqual([US])
  })

  it('drops an edge whose endpoint is not on the canvas', () => {
    const c = seedCanvas(ego())
    const withDangling = {
      ...c,
      edges: { ...c.edges, dangling: edge('dangling', US, 'not-on-canvas') },
    }
    expect(drawableEdges(withDangling).map((e) => e.id)).toEqual(['e1'])
  })

  it('counts drawn degree per node', () => {
    const c = seedCanvas(ego())
    expect(drawnDegree(c, US)).toBe(1)
  })
})

// ---- projection ----

describe('buildWalkElements', () => {
  it('marks the anchor and computes the unexplored affordance', () => {
    const els = buildWalkElements(seedCanvas(ego()))
    const anchor = els.find((e) => e.data.id === US)!
    const neighbour = els.find((e) => e.data.id === IRAN)!
    expect(anchor.data.anchor).toBe(true)
    expect(anchor.data.expanded).toBe(true)
    // Iran has degree 595 and exactly one edge drawn ⇒ 594 unexplored, and it
    // has not been expanded, so it advertises itself as clickable.
    expect(neighbour.data.unexplored).toBe(594)
    expect(neighbour.data.expandable).toBe(true)
  })

  it('does not mark an already-expanded node as expandable', () => {
    const els = buildWalkElements(seedCanvas(ego()))
    expect(els.find((e) => e.data.id === US)!.data.expandable).toBe(false)
  })

  it('filters by family and drops the nodes that lose all their edges', () => {
    const resp = ego({
      nodes: [node(IRAN, 'Iran'), node(RUSSIA, 'Russia')],
      edges: [
        edge('e1', US, IRAN, { edge_family: 'relation' }),
        edge('e2', US, RUSSIA, { edge_family: 'cooccurrence' }),
      ],
    })
    const els = buildWalkElements(seedCanvas(resp), {
      visibleFamilies: new Set(['relation']),
    })
    const ids = els.map((e) => e.data.id)
    expect(ids).toContain(IRAN)
    expect(ids).not.toContain(RUSSIA)
    expect(ids).toContain('e1')
    expect(ids).not.toContain('e2')
  })

  it('keeps the anchor on screen even when every edge is filtered away', () => {
    const els = buildWalkElements(seedCanvas(ego()), {
      visibleFamilies: new Set(['structural']),
    })
    expect(els.map((e) => e.data.id)).toEqual([US])
  })

  it('carries the family styling onto the element data', () => {
    const els = buildWalkElements(seedCanvas(ego()))
    const e1 = els.find((e) => e.data.id === 'e1')!
    expect(e1.data.lineStyle).toBe('solid')
    expect(e1.data.color).toBe(POLARITY_COLORS['-1'])
    expect(e1.data.label).toBe('hostile to')
  })

  it('leaves a cooccurrence edge unlabelled even when labelling is on', () => {
    const resp = ego({
      edges: [edge('e1', US, IRAN, { edge_family: 'cooccurrence', edge_type: 'co occurs with' })],
    })
    const els = buildWalkElements(seedCanvas(resp), { labelEdges: true })
    expect(els.find((e) => e.data.id === 'e1')!.data.label).toBe('')
  })

  it('renders an unresolved endpoint greyed rather than omitting it', () => {
    const resp = ego({
      nodes: [{ ...node(IRAN, 'unresolved:8e7c0a9e'), resolved: false }],
    })
    const els = buildWalkElements(seedCanvas(resp))
    const n = els.find((e) => e.data.id === IRAN)!
    expect(n.data.resolved).toBe(false)
  })
})

// ---- honest disclosure ----

describe('facetSummary', () => {
  const facets = [
    { edge_family: 'cooccurrence', edge_type: 'co occurs with', count: 583, negative: 0, neutral: 583, positive: 0 },
    { edge_family: 'relation', edge_type: 'hostile to', count: 31, negative: 30, neutral: 1, positive: 0 },
    { edge_family: 'relation', edge_type: 'allied with', count: 9, negative: 0, neutral: 0, positive: 9 },
  ]

  it('rolls per-type facets up to families', () => {
    const s = facetSummary(facets)
    const rel = s.find((r) => r.family === 'relation')!
    expect(rel.count).toBe(40)
    expect(rel.negative).toBe(30)
    expect(rel.positive).toBe(9)
  })

  it('marks the families the current filter is hiding', () => {
    const s = facetSummary(facets, new Set(['relation']))
    expect(s.find((r) => r.family === 'cooccurrence')!.visible).toBe(false)
    expect(s.find((r) => r.family === 'relation')!.visible).toBe(true)
  })

  it('reports how many edges the view is withholding', () => {
    // the disclosure that keeps a filtered view from passing as a whole one
    expect(hiddenEdgeCount(facetSummary(facets, new Set(['relation'])))).toBe(583)
    expect(hiddenEdgeCount(facetSummary(facets))).toBe(0)
  })

  it('orders families canonically regardless of facet order', () => {
    expect(facetSummary(facets).map((r) => r.family)).toEqual([
      'relation',
      'cooccurrence',
    ])
  })

  it('lists only the families actually present', () => {
    expect(presentFamilies(facets)).toEqual(['relation', 'cooccurrence'])
  })
})

// ---- the disclosure surface must track the merged canvas ----

describe('per-hop disclosure', () => {
  const usFacets = [
    { edge_family: 'cooccurrence', edge_type: 'co occurs with', count: 585, negative: 0, neutral: 585, positive: 0 },
    { edge_family: 'relation', edge_type: 'hostile to', count: 89, negative: 38, neutral: 51, positive: 0 },
  ]
  const iranFacets = [
    { edge_family: 'cooccurrence', edge_type: 'co occurs with', count: 301, negative: 0, neutral: 301, positive: 0 },
  ]

  function walked() {
    let c = seedCanvas(
      ego({ facets: usFacets, degree_matched: 111, truncated: true }),
    )
    c = mergeEgo(
      c,
      ego({
        anchor: node(IRAN, 'Iran', 595),
        nodes: [node(RUSSIA, 'Russia', 592)],
        edges: [edge('e2', IRAN, RUSSIA)],
        facets: iranFacets,
        degree_matched: 330,
        truncated: true,
      }),
      IRAN,
    )
    return c
  }

  it('records every hop, so the denominator moves when the canvas does', () => {
    const c = walked()
    expect(c.hops.map((h) => h.name)).toEqual(['United States', 'Iran'])
    // the anchor hop's numbers are NOT the walk's numbers once it has grown
    expect(c.hops[0].matched).toBe(111)
    expect(c.hops[1].matched).toBe(330)
    expect(c.hops.every((h) => h.truncated)).toBe(true)
  })

  it('refreshes a re-walked anchor in place instead of listing it twice', () => {
    let c = seedCanvas(ego({ degree_matched: 111 }))
    c = mergeEgo(c, ego({ degree_matched: 222 }), US)
    expect(c.hops).toHaveLength(1)
    expect(c.hops[0].matched).toBe(222)
  })

  it('counts hostile edges from the CANVAS, not from a facet denominator', () => {
    // 38 hostile relation edges exist around the anchor; exactly one of them
    // is drawn. The strip must say what is on screen.
    const stats = drawnFamilyStats(walked(), new Set(['relation', 'reference']))
    const rel = stats.find((s) => s.family === 'relation')!
    expect(rel.drawn).toBe(2)
    expect(rel.negative).toBe(2)
    expect(rel.negative).not.toBe(38)
  })

  it('reports no row at all for a family with nothing drawn', () => {
    // the defect: "38 hostile" persisting under a canvas holding zero
    // relation edges, because the count came from facets
    const c = walked()
    const stats = drawnFamilyStats(c, new Set(['cooccurrence']))
    expect(stats.find((s) => s.family === 'relation')).toBeUndefined()
    expect(stats).toEqual([])
  })

  it('never counts an edge the canvas is not drawing', () => {
    const c = seedCanvas(
      ego({
        nodes: [node(IRAN, 'Iran'), node(RUSSIA, 'Russia')],
        edges: [
          edge('e1', US, IRAN, { edge_family: 'relation', polarity: -1 }),
          edge('e2', US, RUSSIA, { edge_family: 'cooccurrence', polarity: -1 }),
        ],
      }),
    )
    const visible = new Set(['relation'])
    const stats = drawnFamilyStats(c, visible)
    const els = buildWalkElements(c, { visibleFamilies: visible })
    const drawnEdges = els.filter((e) => (e.data as { source?: string }).source)
    // the invariant that keeps the strip and the canvas from ever disagreeing
    expect(stats.reduce((n, s) => n + s.drawn, 0)).toBe(drawnEdges.length)
  })

  it('attributes each hidden count to the hop it belongs to', () => {
    const rows = hiddenFamilyRows(walked().hops, new Set(['relation', 'reference']))
    const co = rows.find((r) => r.family === 'cooccurrence')!
    // summing 585 + 301 would double-count any co-mention BETWEEN the two
    // anchors, so the hops stay separate and named
    expect(co.hops).toEqual([
      { name: 'United States', count: 585 },
      { name: 'Iran', count: 301 },
    ])
  })

  it('says nothing about a family the view is actually drawing', () => {
    const rows = hiddenFamilyRows(walked().hops, new Set(ALL_FAMILIES))
    expect(rows).toEqual([])
  })
})

// ---- query construction ----

describe('egoQueryString', () => {
  const base = {
    entityId: US,
    families: new Set(['relation', 'reference']),
    minConfidence: 0,
    sinceDays: null,
    limit: 80,
  }

  it('sends the selected families as repeated params', () => {
    const qs = egoQueryString(base)
    expect(qs).toContain('entity_id=' + US)
    expect(qs).toContain('family=relation')
    expect(qs).toContain('family=reference')
    expect(qs).toContain('limit=80')
  })

  it('omits family entirely when everything is selected', () => {
    const qs = egoQueryString({ ...base, families: new Set(ALL_FAMILIES) })
    expect(qs).not.toContain('family=')
  })

  it('omits a zero confidence floor', () => {
    expect(egoQueryString(base)).not.toContain('min_confidence')
    expect(egoQueryString({ ...base, minConfidence: 0.5 })).toContain(
      'min_confidence=0.5',
    )
  })

  it('turns a day window into an absolute since instant', () => {
    const qs = egoQueryString({ ...base, sinceDays: 30 })
    const since = new URLSearchParams(qs).get('since')!
    const ageDays = (Date.now() - new Date(since).getTime()) / 86_400_000
    expect(ageDays).toBeGreaterThan(29.9)
    expect(ageDays).toBeLessThan(30.1)
  })

  it('passes the known canvas so the server can stitch induced edges', () => {
    const qs = egoQueryString({ ...base, known: [IRAN, RUSSIA] })
    expect(new URLSearchParams(qs).getAll('known')).toEqual([IRAN, RUSSIA])
  })

  it('emits no known param when the canvas is empty', () => {
    expect(egoQueryString(base)).not.toContain('known=')
  })
})

describe('EMPTY_CANVAS', () => {
  it('projects to nothing', () => {
    expect(buildWalkElements(EMPTY_CANVAS)).toEqual([])
  })
})
