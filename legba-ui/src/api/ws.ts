import type { ConsultResponse } from './types'

type ConsultHandler = (msg: ConsultResponse) => void

export class ConsultWS {
  private ws: WebSocket | null = null
  private handlers: ConsultHandler[] = []
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  connect() {
    if (this.ws) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    this.ws = new WebSocket(`${protocol}//${window.location.host}/ws/consult`)

    this.ws.onmessage = (e) => {
      const msg: ConsultResponse = JSON.parse(e.data)
      for (const handler of this.handlers) {
        handler(msg)
      }
    }

    this.ws.onclose = () => {
      this.ws = null
      this.reconnectTimer = setTimeout(() => this.connect(), 2000)
    }

    this.ws.onerror = () => {
      this.ws?.close()
    }
  }

  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.ws?.close()
    this.ws = null
  }

  send(content: string) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'message', content }))
    }
  }

  clearSession() {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'clear' }))
    }
  }

  onMessage(handler: ConsultHandler) {
    this.handlers.push(handler)
    return () => {
      this.handlers = this.handlers.filter(h => h !== handler)
    }
  }

  get connected() {
    return this.ws?.readyState === WebSocket.OPEN
  }
}
