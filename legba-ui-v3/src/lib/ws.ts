/**
 * WebSocket subscription helper for registry events.
 *
 * The registry router exposes `/api/v1/registry/events?filter=<subject>`.
 * Panels that need live updates (NATS-fed) call `subscribeRegistryEvents`.
 *
 * AUTH: the bearer travels in the `legba.bearer.v1` SUBPROTOCOL, never the
 * URL. A `?token=` query param is printed verbatim in the browser's own
 * console warnings on a failed upgrade, kept in history/referrer surfaces,
 * and written to every access log that records the request line — and the
 * value is byte-identical to LEGBA_REGISTRY_API_TOKEN, the admin credential
 * for the whole registry API. A browser can't set `Authorization` on a WS
 * upgrade but it CAN offer subprotocols, which travel as a header.
 *
 * The credential is base64url-encoded (unpadded) so any secret stays inside
 * RFC 6455's subprotocol `token` grammar; the server echoes back only the
 * scheme name. The server still accepts `?token=` during the rollout window
 * (logging a deprecation warning), so an old tab keeps working — but this
 * client never sends one.
 *
 * Auto-reconnect with 1s/2s/4s/8s/16s backoff; cap at 30s.
 */

/** Subprotocol scheme name — must match `_deps.WS_BEARER_SUBPROTOCOL`. */
export const WS_BEARER_SUBPROTOCOL = 'legba.bearer.v1'

/** base64url, unpadded — the subprotocol grammar allows no `=`, `+` or `/`. */
export function encodeBearerSubprotocol(token: string): string {
  const bytes = new TextEncoder().encode(token)
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * The URL the events socket connects to. Exported so a test can assert what
 * everyone can read: it carries NO credential.
 */
export function registryEventsUrl(filter: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return (
    `${proto}//${window.location.host}` +
    `/api/v1/registry/events?filter=${encodeURIComponent(filter)}`
  )
}

/** The subprotocols offered on the upgrade — `[]` when no token is stored. */
export function registryEventsProtocols(token: string): string[] {
  return token ? [WS_BEARER_SUBPROTOCOL, encodeBearerSubprotocol(token)] : []
}

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
  const url = registryEventsUrl(filter)
  const protocols = registryEventsProtocols(token)

  const open = () => {
    if (closed) return
    try {
      ws = protocols.length
        ? new WebSocket(url, protocols)
        : new WebSocket(url)
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
