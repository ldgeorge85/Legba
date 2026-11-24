/**
 * Flow — automatic layered layout with ELK (elkjs, EPL-2.0), BOUNDED.
 *
 * COMPUTED, NEVER PERSISTED: positions are a function of the view. The input
 * is sorted by id before ELK sees it and ELK is told to respect model order,
 * so the same view always lays out the same way (a test pins it).
 *
 * N-Q (incident 2026-11-13) — the v0 layout ran `elk.bundled.js` on the main
 * thread over the whole graph and hung the browser. Now:
 *
 *   - the ENGINE is injected: the panel passes the web-worker engine
 *     (`flowLayoutWorker.ts`), tests pass the bundled one;
 *   - THE BUDGET: a graph over {@link MAX_VIEW_NODES} nodes or
 *     {@link MAX_VIEW_EDGES} edges is never handed to an engine
 *     ({@link layoutFlow} rejects with {@link LayoutRefused});
 *   - a TIMEOUT ({@link LAYOUT_TIMEOUT_MS}) falls back to a deterministic
 *     column layout ({@link gridLayout}) and says so (`fallback: 'timeout'`);
 *   - CANCELLABLE: an AbortSignal stops waiting (the worker engine terminates
 *     its worker); a newer request supersedes an older one in the panel.
 */
import type { FlowCanvasEdge, FlowCanvasNode } from './flowProjection'
import { MAX_VIEW_EDGES, MAX_VIEW_NODES } from './flowView'

export const CARD_W = 220
export const CARD_H = 66
export const GROUP_W = 220
export const GROUP_H = 76
export const LAYOUT_TIMEOUT_MS = 5000

export const LAYOUT_OPTIONS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.edgeRouting': 'ORTHOGONAL',
  'elk.layered.spacing.nodeNodeBetweenLayers': '90',
  'elk.spacing.nodeNode': '24',
  'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
  'elk.layered.crossingMinimization.forceNodeModelOrder': 'false',
  'elk.randomSeed': '1',
}

export interface ElkGraphIn {
  id: string
  layoutOptions: Record<string, string>
  children: Array<{ id: string; width: number; height: number }>
  edges: Array<{ id: string; sources: string[]; targets: string[] }>
}

export interface ElkOut {
  children?: Array<{ id: string; x?: number; y?: number }>
}

/** A layout engine: ELK in a worker (the panel) or the bundled ELK (tests). */
export interface LayoutEngine {
  layout(graph: ElkGraphIn): Promise<ElkOut>
  /** Stop any in-flight work (the worker engine terminates its worker). */
  cancel?(): void
}

export class LayoutRefused extends Error {
  constructor(readonly nodes: number, readonly edges: number) {
    super(`layout refused: ${nodes} nodes / ${edges} edges is over the ${MAX_VIEW_NODES} / ${MAX_VIEW_EDGES} budget`)
    this.name = 'LayoutRefused'
  }
}

export class LayoutCancelled extends Error {
  constructor() {
    super('layout cancelled')
    this.name = 'LayoutCancelled'
  }
}

export interface LayoutResult {
  nodes: FlowCanvasNode[]
  /** null = ELK placed it; 'timeout' / 'error' = the column fallback is shown. */
  fallback: null | 'timeout' | 'error'
  ms: number
}

function sizeOf(n: FlowCanvasNode): { width: number; height: number } {
  return n.type === 'flowGroup' ? { width: GROUP_W, height: GROUP_H } : { width: CARD_W, height: CARD_H }
}

const KIND_COLUMN: Record<string, number> = { source: 0, group: 0, target: 1, store: 1, analyst: 2, pack: 3, collection: 3, sink: 4 }

function columnOf(n: FlowCanvasNode): number {
  if (n.type === 'flowGroup') {
    const g = String(n.data.group)
    if (g.startsWith('sources')) return 0
    if (g === 'targets' || g === 'stores') return 1
    if (g.startsWith('analyst')) return 2
    if (g === 'packs') return 3
    if (g === 'sinks') return 4
    return 5
  }
  const c = KIND_COLUMN[String(n.data.kind)]
  return c === undefined ? 5 : c
}

/**
 * The deterministic fallback: one column per kind (sources → targets/stores →
 * analysts → packs → sinks → other), rows by id. O(n); never fails.
 */
export function gridLayout(nodes: readonly FlowCanvasNode[]): FlowCanvasNode[] {
  const sorted = [...nodes].sort((a, b) => a.id.localeCompare(b.id))
  const rows = new Map<number, number>()
  return sorted.map((n) => {
    const col = columnOf(n)
    const row = rows.get(col) === undefined ? 0 : (rows.get(col) as number)
    rows.set(col, row + 1)
    return { ...n, position: { x: col * (CARD_W + 90), y: row * (GROUP_H + 24) } } as FlowCanvasNode
  })
}

/** True when a graph of this size may be laid out. */
export function withinBudget(nodes: number, edges: number): boolean {
  return nodes <= MAX_VIEW_NODES && edges <= MAX_VIEW_EDGES
}

/**
 * Lay the view out with `engine`. Rejects with {@link LayoutRefused} over the
 * budget (the engine is never called) and {@link LayoutCancelled} on abort; a
 * timeout or an engine error resolves with the column fallback.
 */
export async function layoutFlow(
  nodes: readonly FlowCanvasNode[],
  edges: readonly FlowCanvasEdge[],
  engine: LayoutEngine,
  opts: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<LayoutResult> {
  const t0 = performance.now()
  if (!withinBudget(nodes.length, edges.length)) throw new LayoutRefused(nodes.length, edges.length)
  if (nodes.length === 0) return { nodes: [], fallback: null, ms: 0 }
  if (opts.signal?.aborted) throw new LayoutCancelled()
  const sortedNodes = [...nodes].sort((a, b) => a.id.localeCompare(b.id))
  const sortedEdges = [...edges].sort((a, b) => a.id.localeCompare(b.id))
  const graph: ElkGraphIn = {
    id: 'root',
    layoutOptions: LAYOUT_OPTIONS,
    children: sortedNodes.map((n) => ({ id: n.id, ...sizeOf(n) })),
    edges: sortedEdges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  }
  const timeoutMs = opts.timeoutMs === undefined ? LAYOUT_TIMEOUT_MS : opts.timeoutMs
  type Outcome = { kind: 'done'; out: ElkOut } | { kind: 'timeout' } | { kind: 'error' } | { kind: 'abort' }
  let timer: ReturnType<typeof setTimeout> | undefined
  let onAbort: (() => void) | undefined
  const outcome = await new Promise<Outcome>((resolve) => {
    timer = setTimeout(() => resolve({ kind: 'timeout' }), timeoutMs)
    onAbort = () => resolve({ kind: 'abort' })
    opts.signal?.addEventListener('abort', onAbort)
    engine.layout(graph).then(
      (out) => resolve({ kind: 'done', out }),
      () => resolve({ kind: 'error' }),
    )
  })
  clearTimeout(timer)
  if (onAbort) opts.signal?.removeEventListener('abort', onAbort)
  if (outcome.kind === 'abort') {
    engine.cancel?.()
    throw new LayoutCancelled()
  }
  if (outcome.kind !== 'done') {
    engine.cancel?.()
    return { nodes: gridLayout(sortedNodes), fallback: outcome.kind, ms: performance.now() - t0 }
  }
  const pos = new Map<string, { x: number; y: number }>()
  for (const c of outcome.out.children ? outcome.out.children : []) {
    if (typeof c.x === 'number' && typeof c.y === 'number') pos.set(c.id, { x: Math.round(c.x), y: Math.round(c.y) })
  }
  let spare = 0
  const laid = sortedNodes.map((n) => {
    const p = pos.get(n.id)
    return { ...n, position: p ? p : { x: -CARD_W * 2, y: (spare++) * (CARD_H + 24) } } as FlowCanvasNode
  })
  return { nodes: laid, fallback: null, ms: performance.now() - t0 }
}

/** A short stable hash (FNV-1a, 32-bit, hex) for layout memo keys. */
export function hashKey(s: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16)
}

/** The structure key of a view: ids only (highlight state never re-lays out). */
export function structureKey(nodes: readonly FlowCanvasNode[], edges: readonly FlowCanvasEdge[]): string {
  return hashKey(nodes.map((n) => n.id).join('|') + '#' + edges.map((e) => e.id).join('|'))
}

/** A small LRU of laid-out positions keyed by (scope, expanded set, payload hash). */
export class LayoutMemo {
  private readonly map = new Map<string, LayoutResult>()
  constructor(private readonly max = 24) {}
  get(key: string): LayoutResult | undefined {
    const v = this.map.get(key)
    if (v) {
      this.map.delete(key)
      this.map.set(key, v)
    }
    return v
  }
  set(key: string, v: LayoutResult): void {
    this.map.delete(key)
    this.map.set(key, v)
    while (this.map.size > this.max) this.map.delete(this.map.keys().next().value as string)
  }
}
