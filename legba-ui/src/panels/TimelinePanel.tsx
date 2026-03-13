import { useEffect, useRef, useState } from 'react'
import { useEvents } from '@/api/hooks'
import { useSelectionStore } from '@/stores/selection'
import { useWorkspaceStore } from '@/stores/workspace'
import type { DataSet } from 'vis-data'
import type { Timeline, TimelineOptions } from 'vis-timeline'

const CATEGORY_COLORS: Record<string, string> = {
  conflict: '#ef4444',
  political: '#8b5cf6',
  economic: '#f59e0b',
  technology: '#06b6d4',
  health: '#10b981',
  environment: '#22c55e',
  social: '#ec4899',
  disaster: '#f97316',
  other: '#6b7280',
}

export function TimelinePanel() {
  const containerRef = useRef<HTMLDivElement>(null)
  const timelineRef = useRef<Timeline | null>(null)
  const [page, setPage] = useState(0)
  const { data } = useEvents({ offset: page * 200, limit: 200 })
  const select = useSelectionStore((s) => s.select)
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  useEffect(() => {
    if (!containerRef.current || !data?.items.length) return

    // Dynamically import vis-timeline + vis-data (heavy libs)
    let cancelled = false

    async function init() {
      const [{ DataSet: DS }, { Timeline: TL }] = await Promise.all([
        import('vis-data'),
        import('vis-timeline'),
      ])

      if (cancelled || !containerRef.current) return

      // Build items — filter out any events with invalid/null timestamps
      const validEvents = data!.items.filter((ev) => {
        if (!ev.timestamp) return false
        const d = new Date(ev.timestamp)
        return !isNaN(d.getTime())
      })

      if (validEvents.length === 0) return

      const items = new DS(
        validEvents.map((ev) => ({
            id: ev.event_id,
            content: ev.title.length > 60 ? ev.title.slice(0, 57) + '...' : ev.title,
            start: new Date(ev.timestamp),
            title: ev.title,
            style: `background-color: ${CATEGORY_COLORS[ev.category] ?? CATEGORY_COLORS.other}22; border-color: ${CATEGORY_COLORS[ev.category] ?? CATEGORY_COLORS.other}; color: #e2e8f0;`,
          })),
      ) as DataSet<{ id: string; content: string; start: Date; title: string; style: string }>

      // Compute explicit time bounds from data to prevent vis-timeline
      // from drawing excessive grid lines when it can't determine range
      const timestamps = validEvents.map((ev) => new Date(ev.timestamp).getTime())
      const minTime = Math.min(...timestamps)
      const maxTime = Math.max(...timestamps)
      const range = maxTime - minTime
      // Add 5% padding on each side, minimum 1 hour
      const padding = Math.max(range * 0.05, 1000 * 60 * 60)

      const options: TimelineOptions = {
        height: '100%',
        margin: { item: 4 },
        orientation: 'top',
        showCurrentTime: true,
        start: new Date(minTime - padding),
        end: new Date(maxTime + padding),
        min: new Date(minTime - padding * 4),
        max: new Date(maxTime + padding * 4),
        zoomMin: 1000 * 60 * 60,        // 1 hour
        zoomMax: 1000 * 60 * 60 * 24 * 90, // 90 days
        stack: true,
        verticalScroll: true,
        maxHeight: '100%',
      }

      // Kill previous instance
      if (timelineRef.current) {
        timelineRef.current.destroy()
      }

      const tl = new TL(containerRef.current!, items as any, options)

      // Click handler
      tl.on('select', (props: { items: string[] }) => {
        if (props.items.length > 0) {
          const eventId = props.items[0]
          const ev = data!.items.find((e) => e.event_id === eventId)
          if (ev) {
            select({ type: 'event', id: ev.event_id, name: ev.title })
            openPanel('event-detail', { id: ev.event_id })
          }
        }
      })

      timelineRef.current = tl
    }

    init()

    return () => {
      cancelled = true
      if (timelineRef.current) {
        timelineRef.current.destroy()
        timelineRef.current = null
      }
    }
  }, [data, select, openPanel])

  // Inject vis-timeline dark styles
  useEffect(() => {
    const style = document.createElement('style')
    style.textContent = `
      .vis-timeline {
        border: none !important;
        background: hsl(240 10% 3.9%) !important;
        font-family: Inter, system-ui, sans-serif !important;
      }
      .vis-time-axis .vis-text { color: #71717a !important; font-size: 11px !important; }
      .vis-time-axis .vis-grid.vis-minor { border-color: #27272a !important; }
      .vis-time-axis .vis-grid.vis-major { border-color: #3f3f46 !important; }
      .vis-panel.vis-center, .vis-panel.vis-left, .vis-panel.vis-right, .vis-panel.vis-top, .vis-panel.vis-bottom {
        border-color: #27272a !important;
      }
      .vis-item { border-radius: 3px !important; font-size: 11px !important; border-width: 1px !important; }
      .vis-item .vis-item-content { padding: 2px 6px !important; }
      .vis-item.vis-selected { border-color: #3b82f6 !important; box-shadow: 0 0 0 1px #3b82f6 !important; }
      .vis-current-time { background-color: #3b82f6 !important; width: 1px !important; }
      .vis-custom-time { background-color: #ef4444 !important; }
      .vis-panel.vis-background { background: transparent !important; }
    `
    document.head.appendChild(style)
    return () => { document.head.removeChild(style) }
  }, [])

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0 text-xs text-muted-foreground">
        <span>
          Timeline
          {data && ` — ${data.items.filter((e) => e.timestamp && !isNaN(new Date(e.timestamp).getTime())).length} events`}
        </span>
        <div className="ml-auto flex items-center gap-1">
          {page > 0 && (
            <button onClick={() => setPage(page - 1)} className="px-2 py-0.5 rounded bg-secondary hover:bg-secondary/80">
              Newer
            </button>
          )}
          {data && data.total > (page + 1) * 200 && (
            <button onClick={() => setPage(page + 1)} className="px-2 py-0.5 rounded bg-secondary hover:bg-secondary/80">
              Older
            </button>
          )}
        </div>
      </div>
      <div className="flex-1 relative">
        {!data?.items.length ? (
          <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
            Loading timeline data...
          </div>
        ) : null}
        <div ref={containerRef} className="absolute inset-0" />
      </div>

      {/* Category legend */}
      <div className="flex items-center gap-3 px-3 py-1 border-t border-border text-[10px] text-muted-foreground shrink-0 flex-wrap">
        {Object.entries(CATEGORY_COLORS).map(([cat, color]) => (
          <span key={cat} className="flex items-center gap-1">
            <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: color }} />
            {cat}
          </span>
        ))}
      </div>
    </div>
  )
}
