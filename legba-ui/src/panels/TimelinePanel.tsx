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

      // Build items — use timestamp, fall back to created_at, filter invalid
      const now = Date.now()
      const sevenDaysAgo = now - 1000 * 60 * 60 * 24 * 7
      const oneDayAhead = now + 1000 * 60 * 60 * 24

      const validEvents = data!.items.filter((ev) => {
        const raw = ev.timestamp ?? ev.created_at ?? ''
        if (!raw) return false
        const d = new Date(raw)
        return !isNaN(d.getTime())
      }).map((ev) => {
        let ts = new Date(ev.timestamp ?? ev.created_at ?? '')
        // Clamp outliers: if timestamp is in the future or very old, use created_at instead
        if (ts.getTime() > oneDayAhead || ts.getTime() < sevenDaysAgo) {
          const createdStr = ev.created_at
          if (createdStr) {
            const created = new Date(createdStr)
            if (!isNaN(created.getTime()) && created.getTime() <= oneDayAhead) {
              ts = created
            }
          }
        }
        return { ...ev, _ts: ts }
      })

      if (validEvents.length === 0) return

      const items = new DS(
        validEvents.map((ev) => ({
            id: ev.event_id,
            content: ev.title.length > 60 ? ev.title.slice(0, 57) + '...' : ev.title,
            start: ev._ts,
            title: `${ev.title}\n${ev._ts.toLocaleString()}`,
            className: `cat-${ev.category || 'other'}`,
            style: `background-color: ${CATEGORY_COLORS[ev.category] ?? CATEGORY_COLORS.other}22; border-color: ${CATEGORY_COLORS[ev.category] ?? CATEGORY_COLORS.other}; color: #e2e8f0;`,
          })),
      ) as DataSet<{ id: string; content: string; start: Date; title: string; style: string }>

      // Compute time bounds — clamp to reasonable range (ignore outliers)
      const timestamps = validEvents.map((ev) => ev._ts.getTime())
      const minTime = Math.max(Math.min(...timestamps), sevenDaysAgo)
      const maxTime = Math.min(Math.max(...timestamps), oneDayAhead)

      // Initial view: show last 48 hours
      const twoDays = 1000 * 60 * 60 * 48
      const viewStart = now - twoDays
      const viewEnd = now + 1000 * 60 * 60  // 1 hour into future

      // Data bounds with padding
      const dataRange = maxTime - minTime
      const boundPadding = Math.max(dataRange * 0.1, 1000 * 60 * 60 * 24)

      // vis-timeline needs a concrete pixel height, not percentages
      const containerHeight = containerRef.current!.clientHeight || 400

      const options: TimelineOptions = {
        height: containerHeight,
        margin: { item: 4 },
        orientation: 'top',
        showCurrentTime: true,
        start: new Date(viewStart),
        end: new Date(viewEnd),
        min: new Date(minTime - boundPadding),
        max: new Date(maxTime + boundPadding),
        zoomMin: 1000 * 60 * 60,            // 1 hour
        zoomMax: 1000 * 60 * 60 * 24 * 365, // 1 year
        stack: true,
        verticalScroll: true,
        maxHeight: containerHeight,
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
          {data && ` — ${data.items.length} events (${data.total} total)`}
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
