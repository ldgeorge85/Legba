/**
 * useLiveTail — React hook over the registry WS multiplexer (src/lib/ws.ts).
 *
 * Each subscription opens a NATS subject filter on the backend
 * (/api/v1/registry/events?filter=…) and streams matching events to the
 * callback. Panels use this for live-tail (e.g. `legba.signals.>` for new
 * signals, `descriptor.>` for registry changes) instead of polling.
 *
 *   const { connected } = useLiveTail('legba.signals.>', (ev) => {...}, enabled)
 */
import { useEffect, useRef, useState } from 'react'
import { subscribeRegistryEvents, type RegistryEvent } from './ws'

export function useLiveTail(
  filter: string,
  onEvent: (ev: RegistryEvent) => void,
  enabled = true,
): { connected: boolean } {
  const cb = useRef(onEvent)
  cb.current = onEvent
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
        if (!live) return
        setConnected(true)
        cb.current(ev)
      },
      () => {
        if (live) setConnected(false)
      },
    )
    setConnected(true)
    return () => {
      live = false
      sub.close()
    }
  }, [filter, enabled])

  return { connected }
}
