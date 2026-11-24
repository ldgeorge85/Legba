/**
 * Flow (N-Q) — the layout effect: off the main thread, cancellable, memoised,
 * budgeted.
 *
 *   - runs only while `enabled` (the tile is visible) and only for a view
 *     inside the budget (an over-budget view never reaches here: the view is
 *     empty and `projectView` says `overBudget`);
 *   - keyed by `memoKey` = (scope, expanded set, payload hash, structure); a
 *     key seen before is served from the memo with no layout;
 *   - a newer key aborts the older request (the worker is terminated) and
 *     unmount aborts too;
 *   - a timeout falls back to the column layout and reports it.
 */
import { useEffect, useRef, useState } from 'react'
import type { FlowCanvasEdge, FlowCanvasNode } from './flowProjection'
import {
  gridLayout,
  LayoutCancelled,
  layoutFlow,
  LayoutMemo,
  type LayoutEngine,
  type LayoutResult,
} from './flowLayout'

const MEMO = new LayoutMemo(24)

/** An engine that never runs ELK: the column layout (no Worker available, e.g. jsdom). */
export const GRID_ENGINE: LayoutEngine = {
  layout: async (g) => {
    const fake = g.children.map((c) => ({ id: c.id, type: 'flowCard', position: { x: 0, y: 0 }, data: { kind: '' } }) as unknown as FlowCanvasNode)
    return { children: gridLayout(fake).map((n) => ({ id: n.id, x: n.position.x, y: n.position.y })) }
  },
}

let workerEngine: Promise<LayoutEngine> | null = null

/** The browser's engine: ELK in a Web Worker, loaded on first use. */
function defaultEngine(): Promise<LayoutEngine> {
  if (typeof Worker === 'undefined') return Promise.resolve(GRID_ENGINE)
  if (!workerEngine) workerEngine = import('./flowLayoutWorker').then((m) => m.createWorkerEngine())
  return workerEngine
}

export interface LaidOut {
  key: string
  result: LayoutResult
}

export function useFlowLayout(
  nodes: readonly FlowCanvasNode[],
  edges: readonly FlowCanvasEdge[],
  memoKey: string,
  enabled: boolean,
  engine?: LayoutEngine,
): { laid: LaidOut | null; pending: boolean } {
  const [laid, setLaid] = useState<LaidOut | null>(null)
  const latest = useRef({ nodes, edges })
  latest.current = { nodes, edges }

  useEffect(() => {
    if (!enabled || nodes.length === 0) return
    const hit = MEMO.get(memoKey)
    if (hit) {
      setLaid({ key: memoKey, result: hit })
      return
    }
    const ctl = new AbortController()
    const { nodes: n, edges: e } = latest.current
    void (async () => {
      const eng = engine ? engine : await defaultEngine()
      if (ctl.signal.aborted) return
      try {
        const result = await layoutFlow(n, e, eng, { signal: ctl.signal })
        if (ctl.signal.aborted) return
        if (!result.fallback) MEMO.set(memoKey, result)
        setLaid({ key: memoKey, result })
      } catch (err) {
        if (err instanceof LayoutCancelled) return
        // LayoutRefused cannot happen for a budgeted view; anything else shows columns.
        if (!ctl.signal.aborted) setLaid({ key: memoKey, result: { nodes: gridLayout(n), fallback: 'error', ms: 0 } })
      }
    })()
    return () => ctl.abort()
    // nodes/edges are read through `latest`: the key already encodes their structure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memoKey, enabled, engine])

  const current = laid && laid.key === memoKey ? laid : null
  return { laid: current, pending: enabled && nodes.length > 0 && !current }
}
