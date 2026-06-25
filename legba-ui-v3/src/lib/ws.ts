/**
 * WebSocket subscription helper for registry events.
 *
 * The registry router exposes `/api/v1/registry/events?token=...&filter=<subject>`.
 * Panels that need live updates (NATS-fed) call `subscribeRegistryEvents`.
 *
 * Auto-reconnect with 1s/2s/4s/8s/16s backoff; cap at 30s.
 */

export type RegistryEvent = {
  type: 'event' | 'subscribed' | 'heartbeat'
  subject?: string
  payload?: Record<string, unknown>
  ts: string
  filter?: string
}

export interface Subscription {
  close: () => void
}

export function subscribeRegistryEvents(
  filter: string,
  onEvent: (ev: RegistryEvent) => void,
  onError?: (err: Event) => void,
): Subscription {
  let ws: WebSocket | null = null
  let closed = false
  let backoff = 1000

  const token = localStorage.getItem('legba_token') ?? ''
  // Resolve `ws://` vs `wss://` from current location protocol.
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url =
    `${proto}//${window.location.host}` +
    `/api/v1/registry/events?filter=${encodeURIComponent(filter)}` +
    (token ? `&token=${encodeURIComponent(token)}` : '')

  const open = () => {
    if (closed) return
    try {
      ws = new WebSocket(url)
    } catch (err) {
      if (onError) onError(err as Event)
      schedule()
      return
    }
    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data) as RegistryEvent
        onEvent(data)
        backoff = 1000 // reset on successful frame
      } catch {
        // ignore malformed frames
      }
    }
    ws.onerror = (err) => {
      if (onError) onError(err)
    }
    ws.onclose = () => {
      if (!closed) schedule()
    }
  }

  const schedule = () => {
    setTimeout(open, backoff)
    backoff = Math.min(backoff * 2, 30_000)
  }

  open()

  return {
    close: () => {
      closed = true
      try {
        ws?.close()
      } catch {
        /* ignore */
      }
    },
  }
}
