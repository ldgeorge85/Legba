import { useState, useEffect, useRef, useCallback } from 'react'
import { cn, categoryColor } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import type { EventSummary } from '@/api/types'

type ConnectionStatus = 'connected' | 'reconnecting' | 'disconnected'

const MAX_EVENTS = 100

export function EventStreamPanel() {
  const [events, setEvents] = useState<(EventSummary & { _received: string })[]>([])
  const [status, setStatus] = useState<ConnectionStatus>('disconnected')
  const scrollRef = useRef<HTMLDivElement>(null)
  const sourceRef = useRef<EventSource | null>(null)
  const hadConnectionRef = useRef(false)

  const connect = useCallback(() => {
    if (sourceRef.current) {
      sourceRef.current.close()
    }

    const source = new EventSource('/sse/stream')
    sourceRef.current = source

    source.onopen = () => {
      hadConnectionRef.current = true
      setStatus('connected')
    }

    source.onerror = () => {
      // EventSource auto-reconnects on error. If we had a prior connection,
      // show "reconnecting"; otherwise show "disconnected".
      if (hadConnectionRef.current && source.readyState === EventSource.CONNECTING) {
        setStatus('reconnecting')
      } else if (source.readyState === EventSource.CLOSED) {
        setStatus('disconnected')
      } else {
        setStatus('reconnecting')
      }
    }

    source.addEventListener('event:new', (e: MessageEvent) => {
      const data = JSON.parse(e.data) as EventSummary
      setEvents((prev) =>
        [{ ...data, _received: new Date().toISOString() }, ...prev].slice(0, MAX_EVENTS)
      )
    })

    return source
  }, [])

  useEffect(() => {
    const source = connect()
    return () => source.close()
  }, [connect])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
  }, [events.length])

  const statusDot = {
    connected: 'bg-green-400',
    reconnecting: 'bg-yellow-400 animate-pulse',
    disconnected: 'bg-red-400',
  }

  const statusLabel = {
    connected: 'Connected',
    reconnecting: 'Reconnecting...',
    disconnected: 'Disconnected',
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <div className={cn('w-2 h-2 rounded-full', statusDot[status])} />
        <span className="text-xs text-muted-foreground">
          {statusLabel[status]} — {events.length} events
        </span>
      </div>
      <div ref={scrollRef} className="flex-1 overflow-auto">
        {events.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground">Waiting for events...</div>
        ) : (
          <div className="space-y-0.5 p-1">
            {events.map((event, i) => (
              <div key={`${event.event_id}-${i}`} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-secondary text-sm">
                <Badge className={cn('text-[10px] shrink-0', categoryColor(event.category))}>
                  {event.category}
                </Badge>
                <span className="flex-1 truncate">{event.title}</span>
                <TimeAgo date={event._received} className="text-[10px] text-muted-foreground shrink-0" />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
