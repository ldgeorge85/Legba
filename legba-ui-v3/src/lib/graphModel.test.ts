import { describe, it, expect } from 'vitest'
import {
  buildLineageElements,
  presentRowKinds,
  projectGraph,
  relationshipTypes,
  kindColor,
  toRowKind,
  truncate,
  type LineageReport,
} from './graphModel'

function report(): LineageReport {
  // root finding ← signal (parent); finding → situation (child); situation → prediction
  return {
    root: {
      id: 'F',
      row_kind: 'finding',
      title: 'Border buildup',
      produced_at: '2026-06-02T00:00:00Z',
      target_id: 'brazil',
      analyst_id: 'inline.brazil',
      schema_uri: 's',
      depth: 0,
    },
    nodes: [
      { id: 'S', row_kind: 'signal', title: 'RSS item', produced_at: '2026-06-01T00:00:00Z', target_id: 'brazil', analyst_id: null, schema_uri: 's', depth: 1 },
      { id: 'SIT', row_kind: 'situation', title: 'Escalation', produced_at: '2026-06-03T00:00:00Z', target_id: 'brazil', analyst_id: 'inline.brazil', schema_uri: 's', depth: 1 },
      { id: 'P', row_kind: 'prediction', title: 'Will escalate', produced_at: '2026-06-04T00:00:00Z', target_id: 'brazil', analyst_id: 'predictor', schema_uri: 's', depth: 2 },
    ],
    edges: [
      { parent: 'S', child: 'F' }, // rel = finding (child F is a finding)
      { parent: 'F', child: 'SIT' }, // rel = situation
      { parent: 'SIT', child: 'P' }, // rel = prediction
    ],
    truncated_at_depth: false,
  }
}

describe('relationshipTypes', () => {
  it('returns distinct child-kind rels in canonical order', () => {
    expect(relationshipTypes(report())).toEqual(['finding', 'situation', 'prediction'])
  })
  it('is empty for undefined', () => {
    expect(relationshipTypes(undefined)).toEqual([])
  })
})

describe('projectGraph', () => {
  it('projects all nodes + edges when unfiltered', () => {
    const g = projectGraph(report())
    expect(g.nodes.map((n) => n.data.id).sort()).toEqual(['F', 'P', 'S', 'SIT'])
    expect(g.edges).toHaveLength(3)
  })

  it('marks the root node', () => {
    const g = projectGraph(report())
    const root = g.nodes.find((n) => n.data.id === 'F')
    expect(root?.data.is_root).toBe(true)
    const sig = g.nodes.find((n) => n.data.id === 'S')
    expect(sig?.data.is_root).toBe(false)
  })

  it('tags each edge with rel = child row_kind', () => {
    const g = projectGraph(report())
    const rels = Object.fromEntries(g.edges.map((e) => [e.data.id, e.data.rel]))
    expect(rels['S->F']).toBe('finding')
    expect(rels['F->SIT']).toBe('situation')
    expect(rels['SIT->P']).toBe('prediction')
  })

  it('filters by relationship type and prunes orphaned non-root nodes', () => {
    // Only keep "finding" rel → only the S->F edge survives.
    const g = projectGraph(report(), new Set(['finding']))
    expect(g.edges.map((e) => e.data.id)).toEqual(['S->F'])
    // S and F are touched; SIT and P are pruned. Root (F) always kept.
    expect(g.nodes.map((n) => n.data.id).sort()).toEqual(['F', 'S'])
  })

  it('keeps the root visible even when a filter strips every edge', () => {
    const g = projectGraph(report(), new Set(['nonexistent']))
    expect(g.edges).toHaveLength(0)
    expect(g.nodes.map((n) => n.data.id)).toEqual(['F'])
  })

  it('colors edges by their rel kind', () => {
    const g = projectGraph(report())
    const e = g.edges.find((x) => x.data.id === 'F->SIT')
    expect(e?.data.color).toBe(kindColor('situation'))
  })

  it('returns empty elements for an undefined report', () => {
    expect(projectGraph(undefined)).toEqual({ nodes: [], edges: [] })
  })
})

describe('helpers', () => {
  it('kindColor falls back to slate for unknown kinds', () => {
    expect(kindColor('finding')).toBe('#fcd34d')
    expect(kindColor('mystery')).toBe('#94a3b8')
  })
  it('truncate adds an ellipsis past the limit', () => {
    expect(truncate('hello world', 5)).toBe('hell…')
    expect(truncate('hi', 5)).toBe('hi')
    expect(truncate(null, 5)).toBe('')
  })
  it('toRowKind coerces unknown strings to finding', () => {
    expect(toRowKind('situation')).toBe('situation')
    expect(toRowKind('mystery')).toBe('finding')
    expect(toRowKind(null)).toBe('finding')
  })
})

describe('presentRowKinds', () => {
  it('lists root + node kinds in canonical order, deduped', () => {
    // root finding + signal, situation, prediction
    expect(presentRowKinds(report())).toEqual(['signal', 'finding', 'situation', 'prediction'])
  })
  it('is empty for undefined', () => {
    expect(presentRowKinds(undefined)).toEqual([])
  })
})

describe('buildLineageElements', () => {
  it('renders the full graph at max depth with no kind filter', () => {
    const g = buildLineageElements(report(), { maxDepth: 4 })
    expect(g.nodes.map((n) => n.data.id).sort()).toEqual(['F', 'P', 'S', 'SIT'])
    expect(g.edges.map((e) => e.data.id).sort()).toEqual(['F->SIT', 'S->F', 'SIT->P'])
  })

  it('prunes nodes beyond the depth bound (root always kept)', () => {
    // depth 1 drops P (depth 2); keeps S, F, SIT.
    const g = buildLineageElements(report(), { maxDepth: 1 })
    expect(g.nodes.map((n) => n.data.id).sort()).toEqual(['F', 'S', 'SIT'])
    expect(g.edges.map((e) => e.data.id).sort()).toEqual(['F->SIT', 'S->F'])
  })

  it('re-parents edges across a hidden intermediate (orphan-safe)', () => {
    // Hide situation: SIT (between F and P) disappears, and P re-parents to F.
    const g = buildLineageElements(report(), {
      maxDepth: 4,
      visibleKinds: new Set(['signal', 'finding', 'prediction']),
    })
    expect(g.nodes.map((n) => n.data.id).sort()).toEqual(['F', 'P', 'S'])
    // P's nearest visible ancestor is F (SIT was skipped) → F->P, plus S->F.
    expect(g.edges.map((e) => e.data.id).sort()).toEqual(['F->P', 'S->F'])
  })

  it('keeps the root even when its kind is filtered out', () => {
    const g = buildLineageElements(report(), {
      maxDepth: 4,
      visibleKinds: new Set(['signal']),
    })
    // Root F always renders; S is visible; SIT/P are hidden. S re-parents
    // to F (its only ancestor), so the one surviving edge is S->F.
    expect(g.nodes.map((n) => n.data.id).sort()).toEqual(['F', 'S'])
    expect(g.edges.map((e) => e.data.id)).toEqual(['S->F'])
  })

  it('returns empty elements for an undefined report', () => {
    expect(buildLineageElements(undefined)).toEqual({ nodes: [], edges: [] })
  })
})
