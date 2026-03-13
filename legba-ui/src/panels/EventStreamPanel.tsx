import { useState, useEffect, useRef } from 'react'
import { cn, categoryColor } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import type { EventSummary } from '@/api/types'

export function EventStreamPanel() {
  const [events, setEvents] = useState<(EventSummary & { _received: string })[]>([])
  const [connected, setConnected] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const source = new EventSource('/sse/stream')

    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false)

    source.addEventListener('event:new', (e: MessageEvent) => {
      const data = JSON.parse(e.data) as EventSummary
      setEvents((prev) => [{ ...data, _received: new Date().toISOString() }, ...prev].slice(0, 200))
    })

    return () => source.close()
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
  }, [events.length])

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <div className={cn('w-2 h-2 rounded-full', connected ? 'bg-green-400' : 'bg-red-400')} />
        <span className="text-xs text-muted-foreground">
          {connected ? 'Connected' : 'Disconnected'} — {events.length} events
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
