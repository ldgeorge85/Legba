/**
 * N-Q — the Flow panel is safe to open: measured on a fixture with the REAL
 * whole-system graph's size and degree distribution (336 nodes, 4,434 edges,
 * 3,872 selector edges; `flowPerfFixture.ts`).
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ELK from 'elkjs/lib/elk.bundled.js'
import type { FlowGraphPayload } from './flowTypes'
import { perfGraph } from './flowPerfFixture'
import {
  initialExpanded,
  MAX_VIEW_EDGES,
  MAX_VIEW_NODES,
  projectView,
  scopePinned,
  viewGroups,
} from './flowView'
import { gridLayout, LayoutCancelled, layoutFlow, LayoutRefused, type ElkGraphIn, type ElkOut, type LayoutEngine } from './flowLayout'
import type { FlowCanvasEdge, FlowCanvasNode } from './flowProjection'

const apiGet = vi.fn()
vi.mock('@/lib/api', () => ({ apiGet: (path: string) => apiGet(path) }))

const elk = new ELK()
const calls: Array<{ nodes: number; edges: number }> = []
/** The bundled ELK, recording the size of every graph it is handed. */
const spyEngine: LayoutEngine = {
  layout: (g: ElkGraphIn) => {
    calls.push({ nodes: g.children.length, edges: g.edges.length })
    return elk.layout(g) as Promise<ElkOut>
  },
}

/** What `scope=node:<id>&depth=1` returns: the node, its direct neighbours, the edges among them. */
function neighbourhood(p: FlowGraphPayload, id: string): FlowGraphPayload {
  const keep = new Set([id])
  for (const e of p.edges) {
    if (e.source === id) keep.add(e.target)
    if (e.target === id) keep.add(e.source)
  }
  return {
    ...p,
    scope: `node:${id}`,
    nodes: p.nodes.filter((n) => keep.has(n.id)).map((n) => (n.id === id ? n : { ...n, context: true })),
    edges: p.edges.filter((e) => keep.has(e.source) && keep.has(e.target)),
  }
}

/** A target's chain: the target, what feeds it, what it binds, and one hop on from those. */
function targetChain(p: FlowGraphPayload, targetId: string): FlowGraphPayload {
  const keep = new Set([targetId])
  for (const e of p.edges) if (e.source === targetId || e.target === targetId) keep.add(e.source === targetId ? e.target : e.source)
  const first = new Set(keep)
  for (const e of p.edges) if (first.has(e.source) && e.source !== targetId && !e.target.startsWith('target:')) keep.add(e.target)
  return { ...p, scope: `target:${targetId}`, nodes: p.nodes.filter((n) => keep.has(n.id)), edges: p.edges.filter((e) => keep.has(e.source) && keep.has(e.target)) }
}

beforeEach(() => {
  calls.length = 0
})

describe('the perf fixture has the real graph’s shape', () => {
  it('336 nodes, 4,434 edges, 3,872 selector edges', () => {
    const p = perfGraph()
    expect(p.nodes).toHaveLength(336)
    expect(p.edges).toHaveLength(4434)
    expect(p.edges.filter((e) => e.basis.includes('source_selector'))).toHaveLength(3872)
  })
})

describe('the default view is a bounded overview', () => {
  it('projection + aggregation of the full payload runs under 150 ms and draws ≤ 30 nodes / ≤ 120 edges', () => {
    const p = perfGraph()
    projectView(p, { expanded: new Set() }) // warm the JIT once
    const t0 = performance.now()
    const index = viewGroups(p)
    const v = projectView(p, { expanded: initialExpanded(p, 'all', new Set(), index) }, index)
    const ms = performance.now() - t0
    console.info(`[flow-perf] overview projection ${ms.toFixed(1)} ms → ${v.nodes.length} nodes / ${v.edges.length} edges`)
    expect(ms).toBeLessThan(150)
    expect(v.overBudget).toBeNull()
    expect(v.nodes.length).toBeLessThanOrEqual(30)
    expect(v.edges.length).toBeLessThanOrEqual(120)
    expect(v.nodes.every((n) => n.type === 'flowGroup')).toBe(true)
    // The selector edges are ONE counted aggregate per (source kind → targets), never 3,872 elements.
    const sel = v.aggregates.filter((a) => a.selector)
    expect(sel.reduce((s, a) => s + a.count, 0)).toBe(3872)
    expect(sel.length).toBeLessThanOrEqual(10)
  })

  it('the overview lays out in bounded time', async () => {
    const p = perfGraph()
    const v = projectView(p, { expanded: new Set() })
    // The first ELK call in a fresh engine pays the GWT warm-up (in the browser
    // that is the worker's, off the main thread); the budget is on the layout.
    let t0 = performance.now()
    await layoutFlow(v.nodes, v.edges, spyEngine)
    console.info(`[flow-perf] overview layout, cold engine ${(performance.now() - t0).toFixed(0)} ms`)
    t0 = performance.now()
    const r = await layoutFlow(v.nodes, v.edges, spyEngine)
    const ms = performance.now() - t0
    console.info(`[flow-perf] overview layout, warm engine ${ms.toFixed(0)} ms (${v.nodes.length} nodes / ${v.edges.length} edges)`)
    expect(r.fallback).toBeNull()
    // A wall-clock bound that must hold while other suites share the host: the
    // overview takes about 0.1 s alone and has measured 0.8 s under gate load.
    // The unbounded layout this guards against took minutes.
    expect(ms).toBeLessThan(3000)
  })
})

describe('the budget is never broken', () => {
  it('the full graph is refused: no view, and the engine is never called', async () => {
    const p = perfGraph()
    const index = viewGroups(p)
    const all = projectView(p, { expanded: new Set(index.keys()) }, index)
    expect(all.overBudget).not.toBeNull()
    expect(all.nodes).toHaveLength(0)
    expect(all.edges).toHaveLength(0)
    const nodes = p.nodes.map((n) => ({ id: n.id, type: 'flowCard', position: { x: 0, y: 0 }, data: { kind: n.kind } }) as unknown as FlowCanvasNode)
    const edges = p.edges.map((e) => ({ id: e.id, source: e.source, target: e.target }) as FlowCanvasEdge)
    await expect(layoutFlow(nodes, edges, spyEngine)).rejects.toBeInstanceOf(LayoutRefused)
    expect(calls).toHaveLength(0)
  })

  it('every single-group expansion, every target neighbourhood and every target chain stays within budget or is refused', async () => {
    const p = perfGraph()
    const index = viewGroups(p)
    const views = [...index.keys()].map((g) => projectView(p, { expanded: new Set([g]) }, index))
    for (const t of p.nodes.filter((n) => n.kind === 'target')) {
      const nb = neighbourhood(p, t.id)
      views.push(projectView(nb, { expanded: initialExpanded(nb, nb.scope, scopePinned(nb, nb.scope)), pinned: scopePinned(nb, nb.scope), focus: true }))
      const ch = targetChain(p, t.id)
      const pin = new Set([t.id])
      views.push(projectView(ch, { expanded: initialExpanded(ch, ch.scope, pin), pinned: pin }))
    }
    let laidOut = 0
    for (const v of views) {
      if (v.overBudget) {
        expect(v.nodes).toHaveLength(0)
        continue
      }
      await layoutFlow(v.nodes, v.edges, { layout: async (g) => { calls.push({ nodes: g.children.length, edges: g.edges.length }); return { children: [] } } })
      laidOut += 1
    }
    expect(laidOut).toBeGreaterThan(0)
    for (const c of calls) {
      expect(c.nodes).toBeLessThanOrEqual(MAX_VIEW_NODES)
      expect(c.edges).toBeLessThanOrEqual(MAX_VIEW_EDGES)
    }
  })

  it('expanding the largest analyst family, one target chain and one neighbourhood lay out in budget', async () => {
    const p = perfGraph()
    const index = viewGroups(p)
    const family = [...index].filter(([g]) => g.startsWith('analyst:')).sort((a, b) => b[1].length - a[1].length)[0][0]
    const fam = projectView(p, { expanded: new Set([family]) }, index)
    const tgt = p.nodes.filter((n) => n.kind === 'target').sort((a, b) => a.id.localeCompare(b.id))[0].id
    const ch = targetChain(p, tgt)
    const chV = projectView(ch, { expanded: initialExpanded(ch, ch.scope, new Set([tgt])), pinned: new Set([tgt]) })
    const nb = neighbourhood(p, tgt)
    const nbV = projectView(nb, { expanded: initialExpanded(nb, nb.scope, new Set([tgt])), pinned: new Set([tgt]), focus: true })
    for (const [name, v] of [['family', fam], ['chain', chV], ['neighbourhood', nbV]] as const) {
      expect(v.overBudget).toBeNull()
      const t0 = performance.now()
      const r = await layoutFlow(v.nodes, v.edges, spyEngine)
      console.info(`[flow-perf] ${name}: ${v.nodes.length} nodes / ${v.edges.length} edges laid out in ${(performance.now() - t0).toFixed(0)} ms`)
      expect(r.fallback).toBeNull()
    }
  })

  it('initial expansion of any narrowed scope fits the budget', () => {
    const p = perfGraph()
    const index = viewGroups(p)
    for (const g of index.keys()) {
      const v = projectView(p, { expanded: initialExpanded(p, `family:${g}`, new Set(), index) }, index)
      expect(v.overBudget).toBeNull()
    }
  })
})

describe('the layout is cancellable and times out to columns', () => {
  it('a timeout resolves with the column fallback and cancels the engine', async () => {
    const cancel = vi.fn()
    const never: LayoutEngine = { layout: () => new Promise(() => {}), cancel }
    const v = projectView(perfGraph(), { expanded: new Set() })
    const r = await layoutFlow(v.nodes, v.edges, never, { timeoutMs: 20 })
    expect(r.fallback).toBe('timeout')
    expect(r.nodes).toEqual(gridLayout(v.nodes))
    expect(cancel).toHaveBeenCalled()
  })

  it('an abort rejects with LayoutCancelled and cancels the engine', async () => {
    const cancel = vi.fn()
    const never: LayoutEngine = { layout: () => new Promise(() => {}), cancel }
    const v = projectView(perfGraph(), { expanded: new Set() })
    const ctl = new AbortController()
    const pending = layoutFlow(v.nodes, v.edges, never, { signal: ctl.signal })
    ctl.abort()
    await expect(pending).rejects.toBeInstanceOf(LayoutCancelled)
    expect(cancel).toHaveBeenCalled()
  })
})

describe('the panel is inert while hidden', () => {
  it('fetches nothing until its tile is shown, then requests the graph without bodies', async () => {
    apiGet.mockReset()
    apiGet.mockResolvedValue(perfGraph())
    const { DockviewPanelApiProvider } = await import('@/components/DockviewPanelApiContext')
    const FlowPanel = (await import('./FlowPanel')).default
    const subs = new Set<(e: { isVisible: boolean }) => void>()
    const fire = (e: { isVisible: boolean }) => subs.forEach((cb) => cb(e))
    const api = {
      isVisible: false,
      onDidVisibilityChange: (cb: (e: { isVisible: boolean }) => void) => {
        subs.add(cb)
        return { dispose: () => subs.delete(cb) }
      },
      onDidDimensionsChange: () => ({ dispose: () => {} }),
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const engine: LayoutEngine = { layout: async (g) => ({ children: g.children.map((c, i) => ({ id: c.id, x: 0, y: i * 100 })) }) }
    render(
      <QueryClientProvider client={qc}>
        {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
        <DockviewPanelApiProvider value={api as any}>
          <div style={{ width: 800, height: 600 }}>
            <FlowPanel layoutEngine={engine} />
          </div>
        </DockviewPanelApiProvider>
      </QueryClientProvider>,
    )
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })
    expect(apiGet).not.toHaveBeenCalled()
    expect(screen.getByText(/loads when this tab is shown/)).toBeInTheDocument()
    await act(async () => {
      api.isVisible = true
      fire({ isVisible: true })
    })
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1))
    expect(apiGet.mock.calls[0][0]).toContain('fields=false')
    expect(apiGet.mock.calls[0][0]).toContain('scope=all')
    expect(await screen.findByTestId('flow-counts')).toHaveTextContent(/\d+ of 336 nodes drawn/)
  })
})

describe('a region\'s second view is added without activating it', () => {
  // U-2: the stale-layout merge is gone (a changed default resets cleanly);
  // what keeps the Flow from laying out at boot is that a region's later views
  // — the Flow is the Registry region's second — are added INACTIVE.
  it('the surface seed passes inactive for every view after a region\'s first', () => {
    const dock = readFileSync(join(__dirname, '..', '..', 'lib', 'surfaceDock.ts'), 'utf8')
    expect(dock).toContain('open(view, position, { seeded: true, inactive: !!anchor })')
    const src = readFileSync(join(__dirname, '..', '..', 'App.tsx'), 'utf8')
    expect(src).toContain('...(opts.inactive ? { inactive: true } : {})')
  })
})
