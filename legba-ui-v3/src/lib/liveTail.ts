/**
 * Batched live tail (v4) — FROZEN surface (UI_V4_PLAN §2.4).
 *
 * Wraps the existing single-event `subscribeRegistryEvents` (src/lib/ws.ts) and
 * buffers events, flushing the batch every `intervalMs` (default 5s). Per-event
 * rendering at ~10-15k signals/day is the known anti-pattern; the World map +
 * feed and the Flow telemetry all consume batches so they pulse, not thrash.
 *
 *   useBatchedTail('legba.signals.>', (batch) => addPoints(batch))
 */
import { useEffect, useRef, useState } from 'react'
import { subscribeRegistryEvents, type RegistryEvent } from './ws'

export interface BatchedTailOpts {
  intervalMs?: number
  enabled?: boolean
}

export function useBatchedTail(
  filter: string,
  onBatch: (events: RegistryEvent[]) => void,
  opts: BatchedTailOpts = {},
): { connected: boolean } {
  const { intervalMs = 5000, enabled = true } = opts
  const cb = useRef(onBatch)
  cb.current = onBatch
  const buf = useRef<RegistryEvent[]>([])
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!enabled || !filter) {
      setConnected(false)
      return
    }
    let live = true
    const sub = subscribeRegistryEvents(
      filter,
      (ev) => {
        // Only real NATS events accumulate; skip subscribed/heartbeat frames.
        if (live && ev.type === 'event') buf.current.push(ev)
      },
      () => {
        if (live) setConnected(false)
      },
    )
    setConnected(true)
    const timer = setInterval(() => {
      if (!live || buf.current.length === 0) return
      const batch = buf.current
      buf.current = []
      cb.current(batch)
    }, intervalMs)
    return () => {
      live = false
      clearInterval(timer)
      sub.close()
    }
  }, [filter, enabled, intervalMs])

  return { connected }
}
