import type { QueryClient } from '@tanstack/react-query'
import type { SSEEventType } from './types'

type SSEHandler = (data: Record<string, unknown>) => void

export class SSEClient {
  private source: EventSource | null = null
  private handlers = new Map<string, SSEHandler[]>()
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectDelay = 1000

  constructor(private queryClient: QueryClient) {}

  connect() {
    if (this.source) return

    this.source = new EventSource('/sse/stream')

    this.source.onopen = () => {
      this.reconnectDelay = 1000
    }

    this.source.onerror = () => {
      this.source?.close()
      this.source = null
      this.scheduleReconnect()
    }

    // Register handlers for each event type
    const eventTypes: SSEEventType[] = [
      'event:new',
      'watch:trigger',
      'cycle:start',
      'cycle:end',
      'agent:status',
      'situation:update',
    ]

    for (const type of eventTypes) {
      this.source.addEventListener(type, (e: MessageEvent) => {
        const data = JSON.parse(e.data)
        this.dispatch(type, data)
      })
    }

    // Default query invalidation on events
    this.on('event:new', () => {
      this.queryClient.invalidateQueries({ queryKey: ['events'] })
      this.queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    })

    this.on('cycle:end', () => {
      this.queryClient.invalidateQueries({ queryKey: ['cycles'] })
      this.queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    })

    this.on('situation:update', () => {
      this.queryClient.invalidateQueries({ queryKey: ['situations'] })
    })

    this.on('watch:trigger', () => {
      this.queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    })
  }

  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.source?.close()
    this.source = null
  }

  on(type: string, handler: SSEHandler) {
    const list = this.handlers.get(type) ?? []
    list.push(handler)
    this.handlers.set(type, list)
  }

  off(type: string, handler: SSEHandler) {
    const list = this.handlers.get(type)
    if (list) {
      this.handlers.set(type, list.filter(h => h !== handler))
    }
  }

  private dispatch(type: string, data: Record<string, unknown>) {
    const list = this.handlers.get(type)
    if (list) {
      for (const handler of list) {
        handler(data)
      }
    }
  }

  private scheduleReconnect() {
    this.reconnectTimer = setTimeout(() => {
      this.connect()
    }, this.reconnectDelay)
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30_000)
  }
}
