/**
 * Flow (N-Q) — the ELK engine OFF the main thread.
 *
 * `elkjs/lib/elk-api.js` drives `elk-worker.min.js` in a real Web Worker; Vite
 * emits the worker script as a hashed asset (`?url`), so the 1.6 MB GWT build
 * is neither parsed nor run on the page's thread. Cancelling terminates the
 * worker (ELK cannot abort a job) and the next layout starts a fresh one.
 */
import ELK from 'elkjs/lib/elk-api.js'
import elkWorkerUrl from 'elkjs/lib/elk-worker.min.js?url'
import type { ElkGraphIn, ElkOut, LayoutEngine } from './flowLayout'

export function createWorkerEngine(): LayoutEngine {
  let elk: InstanceType<typeof ELK> | null = null
  const get = () => {
    if (!elk) elk = new ELK({ workerFactory: () => new Worker(elkWorkerUrl) })
    return elk
  }
  return {
    layout: (graph: ElkGraphIn) => get().layout(graph) as Promise<ElkOut>,
    cancel: () => {
      if (elk) {
        try {
          elk.terminateWorker()
        } catch {
          // already gone
        }
        elk = null
      }
    },
  }
}
