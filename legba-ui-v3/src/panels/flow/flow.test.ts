import { describe, expect, it } from 'vitest'
import {
  COLLAPSE_THRESHOLD,
  defaultCollapsed,
  edgeLabel,
  groupNodeId,
  projectGraph,
  scopeOptions,
  searchNodes,
  type FlowCardData,
} from './flowProjection'
import ELK from 'elkjs/lib/elk.bundled.js'
import { layoutFlow, type ElkOut, type LayoutEngine } from './flowLayout'
import { flatten, lineDiff, semanticDiff, changedTexts } from './flowDiff'
import { graphFixture } from './flowFixtures'

describe('projection — payload to canvas nodes and edges', () => {
  it('renders every node expanded when nothing is collapsed, with basis on every edge', () => {
    const p = graphFixture(3)
    const { nodes, edges } = projectGraph(p, { collapsed: new Set() })
    expect(nodes).toHaveLength(p.nodes.length)
    expect(edges).toHaveLength(p.edges.length)
    for (const e of edges) expect(e.data?.bases).toHaveLength(1)
    const sel = edges.find((e) => e.source === 'store:grounding:substrate')!
    expect(sel.data?.bases).toEqual(['grounding.sources[0]'])
    expect(edgeLabel(sel.data!)).toBe('grounding.sources[0]')
  })

  it('marks paused nodes dimmed and keeps an unknown kind as a generic card', () => {
    const { nodes } = projectGraph(graphFixture(3), { collapsed: new Set() })
    const lens = nodes.find((n) => n.id === 'analyst:lens_left')!
    expect((lens.data as FlowCardData).paused).toBe(true)
    const w = nodes.find((n) => n.id === 'wiring:w1')!
    expect(w.type).toBe('flowCard')
    expect((w.data as FlowCardData).kind).toBe('unknown')
  })

  it('a collapsed group is one composite node; its edges aggregate and keep their bases', () => {
    const p = graphFixture(25)
    expect(defaultCollapsed(p)).toEqual(new Set(['sources']))
    expect(COLLAPSE_THRESHOLD).toBe(20)
    const { nodes, edges, groups } = projectGraph(p, { collapsed: new Set(['sources']) })
    expect(nodes.filter((n) => n.id.startsWith('source:'))).toHaveLength(0)
    const g = nodes.find((n) => n.id === groupNodeId('sources'))!
    expect(g.type).toBe('flowGroup')
    expect(g.data.count).toBe(25)
    const agg = edges.filter((e) => e.source === groupNodeId('sources'))
    expect(agg).toHaveLength(1)
    expect(agg[0].data).toEqual({ relation: 'feeds', bases: ['target.sources[].source_selector'], count: 25 })
    expect(edgeLabel(agg[0].data!)).toBe('target.sources[].source_selector (×25)')
    expect(groups.find((x) => x.group === 'sources')).toEqual({ group: 'sources', count: 25, paused: 0, collapsed: true })
  })

  it('edges inside one collapsed group disappear rather than loop', () => {
    const p = graphFixture(3)
    const { edges } = projectGraph(p, { collapsed: new Set(['targets', 'analyst:inline_target']) })
    expect(edges.some((e) => e.source === e.target)).toBe(false)
  })

  it('search matches label, kind, model and tool; the run overlay paints one node', () => {
    const p = graphFixture(3)
    expect(searchNodes(p, 'fast')).toEqual(new Set(['analyst:escalation']))
    expect(searchNodes(p, 'escal')).toEqual(new Set(['analyst:escalation', 'pack:escalate_finding']))
    expect(searchNodes(p, 'escalate')).toEqual(new Set(['pack:escalate_finding']))
    expect(searchNodes(p, '  ')).toEqual(new Set())
    const { nodes } = projectGraph(p, { collapsed: new Set(), matched: new Set(['pack:escalate_finding']), runStatus: { 'analyst:escalation': 'failed' } })
    expect((nodes.find((n) => n.id === 'pack:escalate_finding')!.data as FlowCardData).matched).toBe(true)
    expect((nodes.find((n) => n.id === 'analyst:escalation')!.data as FlowCardData).runStatus).toBe('failed')
    expect((nodes.find((n) => n.id === 'analyst:escalation')!.data as FlowCardData).summary).toBe('llm_planner · fast · 0 7,19 * * *')
  })

  it('projection is deterministic whatever order the payload arrives in', () => {
    const p = graphFixture(5)
    const shuffled = { ...p, nodes: [...p.nodes].reverse(), edges: [...p.edges].reverse() }
    const a = projectGraph(p, { collapsed: new Set(['targets']) })
    const b = projectGraph(shuffled, { collapsed: new Set(['targets']) })
    expect(JSON.stringify(b)).toBe(JSON.stringify(a))
  })

  it('scope options list families and target chains from the whole graph', () => {
    const opts = scopeOptions(graphFixture(2))
    expect(opts[0]).toEqual({ value: 'all', label: 'Whole system' })
    expect(opts.map((o) => o.value)).toContain('family:analyst:inline_target')
    expect(opts.map((o) => o.value)).toContain('target:country_g20_cn')
  })
})

const elk = new ELK()
const bundled: LayoutEngine = { layout: (g) => elk.layout(g) as Promise<ElkOut> }

describe('layout — ELK, computed, deterministic', () => {
  it('the same payload lays out the same way, in any input order', async () => {
    const p = graphFixture(6)
    const a = projectGraph(p, { collapsed: new Set() })
    const first = (await layoutFlow(a.nodes, a.edges, bundled)).nodes
    const second = (await layoutFlow([...a.nodes].reverse(), [...a.edges].reverse(), bundled)).nodes
    const pos = (ns: typeof first) => ns.map((n) => `${n.id}@${n.position.x},${n.position.y}`)
    expect(pos(second)).toEqual(pos(first))
    expect(new Set(first.map((n) => `${n.position.x},${n.position.y}`)).size).toBe(first.length)
    // left to right: a source sits left of the target it feeds, which sits left of its analyst.
    const x = (id: string) => first.find((n) => n.id === id)!.position.x
    expect(x('source:source.s00')).toBeLessThan(x('target:country_g20_cn'))
    expect(x('target:country_g20_cn')).toBeLessThan(x('analyst:escalation'))
    expect(x('analyst:escalation')).toBeLessThan(x('analyst:country_composition'))
  })

  it('an empty graph lays out to nothing', async () => {
    expect((await layoutFlow([], [], bundled)).nodes).toEqual([])
  })
})

describe('compare — semantic diff and prompt line diff', () => {
  it('buckets added, removed and changed leaf paths', () => {
    const before = { method: { llm: { temperature: 0.2, max_tokens: 4096 }, system_prompt: 'a\nb\nc' }, outputs: [] }
    const after = { method: { llm: { temperature: 1.0 }, system_prompt: 'a\nB\nc', options: { x: 1 } }, outputs: [] }
    const d = semanticDiff(before, after)
    expect(d.map((c) => `${c.kind}:${c.path}`)).toEqual([
      'removed:method.llm.max_tokens',
      'changed:method.llm.temperature',
      'added:method.options.x',
      'changed:method.system_prompt',
    ])
    expect(changedTexts(d).map((c) => c.path)).toEqual(['method.system_prompt'])
    expect(semanticDiff(before, before)).toEqual([])
    expect(flatten([])).toEqual(new Map([['(root)', []]]))
  })

  it('line diff keeps order and marks adds and deletes', () => {
    expect(lineDiff('a\nb\nc', 'a\nB\nc')).toEqual([
      { op: 'same', text: 'a' },
      { op: 'del', text: 'b' },
      { op: 'add', text: 'B' },
      { op: 'same', text: 'c' },
    ])
  })
})
