import { useEffect, useRef, useState, useCallback } from 'react'
import { useEvents } from '@/api/hooks'
import { useSelectionStore } from '@/stores/selection'
import { useWorkspaceStore } from '@/stores/workspace'
import 'vis-timeline/styles/vis-timeline-graph2d.min.css'

const CATEGORIES = ['conflict', 'political', 'economic', 'disaster', 'health', 'technology', 'environment', 'social', 'other'] as const

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

interface SelectedEvent {
  event_id: string
  title: string
  category: string
  timestamp: string
  created_at?: string
  description?: string
  source_name?: string | null
}

export function TimelinePanel() {
  const containerRef = useRef<HTMLDivElement>(null)
  const timelineRef = useRef<any>(null)
  const dataRef = useRef<any>(null)
  const [tlHeight, setTlHeight] = useState(300)
  const [page, setPage] = useState(0)
  const [selectedEvent, setSelectedEvent] = useState<SelectedEvent | null>(null)
  const [hiddenCats, setHiddenCats] = useState<Set<string>>(new Set())
  const { data } = useEvents({ offset: page * 200, limit: 200 })
  const select = useSelectionStore((s) => s.select)
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  // Track container size
  const wrapRef = useCallback((node: HTMLDivElement | null) => {
    if (!node) return
    const update = () => {
      // Timeline gets ~60% of available height, rest for detail
      const h = node.clientHeight
      if (h > 100) setTlHeight(selectedEvent ? Math.floor(h * 0.55) : h)
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(node)
  }, [selectedEvent])

  const toggleCat = (cat: string) => {
    setHiddenCats((prev) => {
      const next = new Set(prev)
      if (next.has(cat)) next.delete(cat); else next.add(cat)
      return next
    })
  }

  const zoomIn = () => timelineRef.current?.zoomIn(0.4)
  const zoomOut = () => timelineRef.current?.zoomOut(0.4)
  const fitAll = () => timelineRef.current?.fit()

  useEffect(() => {
    if (!containerRef.current || !data?.items.length || tlHeight < 50) return

    let cancelled = false

    async function init() {
      const [visData, visTL] = await Promise.all([
        import('vis-data'),
        import('vis-timeline'),
      ])
      const { DataSet } = visData
      const { Timeline } = visTL

      if (cancelled || !containerRef.current) return

      // Build groups
      const visibleCats = CATEGORIES.filter((c) => !hiddenCats.has(c))
      const groups = new DataSet(
        visibleCats.map((cat) => ({
          id: cat,
          content: `<span style="color:${CATEGORY_COLORS[cat]}">${cat}</span>`,
        })),
      )

      // Build items as boxes with truncated title text
      const allTimestamps: number[] = []
      const processed: any[] = []
      for (const ev of data!.items) {
        const cat = (CATEGORIES as readonly string[]).includes(ev.category) ? ev.category : 'other'
        if (hiddenCats.has(cat)) continue
        const ms = new Date(ev.created_at ?? ev.timestamp ?? '').getTime()
        if (isNaN(ms)) continue
        allTimestamps.push(ms)
        const color = CATEGORY_COLORS[cat] ?? CATEGORY_COLORS.other
        const label = ev.title.length > 45 ? ev.title.slice(0, 42) + '…' : ev.title
        processed.push({
          id: ev.event_id,
          group: cat,
          content: `<span style="color:#e2e8f0">${label}</span>`,
          start: ms,
          type: 'box',
          title: ev.title,
          style: `background:${color}18; border-left:3px solid ${color}; border-color:${color}40; font-size:11px; cursor:pointer;`,
        })
      }
      if (processed.length === 0 || allTimestamps.length === 0) return

      const items = new DataSet(processed)
      const minTs = Math.min(...allTimestamps)
      const maxTs = Math.max(...allTimestamps)

      // Store data ref for click handler
      dataRef.current = data

      // Destroy previous
      if (timelineRef.current) {
        timelineRef.current.destroy()
        timelineRef.current = null
      }

      const tl = new Timeline(containerRef.current!, items as any, groups as any, {
        width: '100%',
        height: `${tlHeight}px`,
        showCurrentTime: false,
        zoomMin: 1800000,        // 30 min
        zoomMax: 86400000 * 14,  // 14 days
        zoomKey: 'ctrlKey',
        stack: true,
        groupOrder: (a: any, b: any) => CATEGORIES.indexOf(a.id) - CATEGORIES.indexOf(b.id),
        tooltip: { followMouse: true, overflowMethod: 'cap', delay: 100 },
        margin: { item: { horizontal: 2, vertical: 2 } },
        orientation: { axis: 'bottom', item: 'top' },
        verticalScroll: true,
        maxHeight: `${tlHeight}px`,
      })

      // Set window from data
      tl.setWindow(minTs - 3600000, maxTs + 3600000)

      // Click → show detail in panel, not new tab
      tl.on('select', (props: { items: string[] }) => {
        if (props.items.length > 0) {
          const eventId = props.items[0]
          const ev = dataRef.current?.items.find((e: any) => e.event_id === eventId)
          if (ev) {
            setSelectedEvent(ev)
            select({ type: 'event', id: ev.event_id, name: ev.title })
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
  }, [data, tlHeight, hiddenCats, select])

  // Dark theme CSS
  useEffect(() => {
    const id = 'vis-tl-dark'
    if (document.getElementById(id)) return
    const s = document.createElement('style')
    s.id = id
    s.textContent = `
      .vis-timeline { border: none !important; background: hsl(240 10% 3.9%) !important; font-family: Inter, system-ui, sans-serif !important; }
      .vis-time-axis .vis-text { color: #a1a1aa !important; font-size: 11px !important; }
      .vis-time-axis .vis-grid.vis-minor { border-color: #1a1a20 !important; }
      .vis-time-axis .vis-grid.vis-major { border-color: #27272a !important; }
      .vis-panel.vis-center, .vis-panel.vis-left, .vis-panel.vis-right, .vis-panel.vis-top, .vis-panel.vis-bottom { border-color: #27272a !important; }
      .vis-labelset .vis-label { color: #a1a1aa !important; font-size: 12px !important; font-weight: 500 !important; border-bottom: 1px solid #1a1a20 !important; padding-left: 8px !important; }
      .vis-foreground .vis-group { border-bottom: 1px solid #1a1a20 !important; }
      .vis-item { border-radius: 3px !important; font-size: 11px !important; min-height: 20px !important; }
      .vis-item .vis-item-content { padding: 2px 6px !important; white-space: nowrap !important; overflow: hidden !important; }
      .vis-item.vis-box { min-width: 8px !important; }
      .vis-item.vis-box .vis-item-content { min-width: 0 !important; }
      .vis-item.vis-selected { box-shadow: 0 0 0 2px #3b82f6 !important; z-index: 10 !important; }
      .vis-item .vis-item-overflow { overflow: hidden !important; }
      .vis-tooltip { background: #18181b !important; color: #e2e8f0 !important; border: 1px solid #3f3f46 !important; border-radius: 6px !important; padding: 6px 10px !important; font-size: 12px !important; max-width: 350px !important; box-shadow: 0 4px 12px rgba(0,0,0,0.5) !important; pointer-events: none !important; }
      .vis-current-time { background-color: #3b82f680 !important; width: 2px !important; }
    `
    document.head.appendChild(s)
    return () => { document.getElementById(id)?.remove() }
  }, [])

  const openFull = () => {
    if (selectedEvent) openPanel('event-detail', { id: selectedEvent.event_id })
  }

  return (
    <div className="flex flex-col h-full overflow-hidden bg-background">
      {/* Toolbar */}
      <div className="flex items-center gap-1.5 px-2 py-1 border-b border-border shrink-0">
        <span className="text-xs font-medium text-foreground mr-1">Timeline</span>
        {data && <span className="text-[11px] text-muted-foreground">{data.items.length} events</span>}
        <div className="flex-1" />

        {/* Category toggles */}
        <div className="flex items-center gap-0.5">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => toggleCat(cat)}
              className="px-1.5 py-0.5 rounded text-[10px] font-medium transition-opacity"
              style={{
                backgroundColor: hiddenCats.has(cat) ? 'transparent' : CATEGORY_COLORS[cat] + '25',
                color: hiddenCats.has(cat) ? '#52525b' : CATEGORY_COLORS[cat],
                border: `1px solid ${hiddenCats.has(cat) ? '#27272a' : CATEGORY_COLORS[cat] + '50'}`,
              }}
              title={`${hiddenCats.has(cat) ? 'Show' : 'Hide'} ${cat}`}
            >
              {cat}
            </button>
          ))}
        </div>

        <div className="w-px h-4 bg-border mx-1" />

        {/* Zoom controls */}
        <button onClick={zoomIn} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-xs text-foreground" title="Zoom in">+</button>
        <button onClick={zoomOut} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-xs text-foreground" title="Zoom out">−</button>
        <button onClick={fitAll} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-[10px] text-foreground" title="Fit all events">Fit</button>

        <div className="w-px h-4 bg-border mx-1" />

        {/* Pagination */}
        {page > 0 && (
          <button onClick={() => setPage(page - 1)} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-[10px] text-foreground">← Newer</button>
        )}
        {data && data.total > (page + 1) * 200 && (
          <button onClick={() => setPage(page + 1)} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-[10px] text-foreground">Older →</button>
        )}
      </div>

      {/* Main content area */}
      <div ref={wrapRef} className="flex-1 min-h-0 flex flex-col">
        {/* Timeline */}
        {!data?.items.length && (
          <div className="flex items-center justify-center h-32 text-sm text-muted-foreground">Loading...</div>
        )}
        <div ref={containerRef} className="shrink-0" style={{ height: `${tlHeight}px` }} />

        {/* Selected event detail */}
        {selectedEvent && (
          <div className="flex-1 min-h-0 border-t border-border overflow-auto">
            <div className="p-3">
              <div className="flex items-start gap-2 mb-2">
                <span
                  className="shrink-0 mt-0.5 inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold"
                  style={{ backgroundColor: CATEGORY_COLORS[selectedEvent.category] + '25', color: CATEGORY_COLORS[selectedEvent.category] }}
                >
                  {selectedEvent.category}
                </span>
                <h3 className="text-sm font-medium text-foreground leading-tight flex-1">{selectedEvent.title}</h3>
                <button
                  onClick={() => setSelectedEvent(null)}
                  className="shrink-0 text-muted-foreground hover:text-foreground text-xs px-1"
                  title="Close detail"
                >✕</button>
              </div>

              <div className="flex items-center gap-3 text-[11px] text-muted-foreground mb-2">
                <span>{new Date(selectedEvent.created_at ?? selectedEvent.timestamp).toLocaleString()}</span>
                {selectedEvent.source_name && <span>via {selectedEvent.source_name}</span>}
              </div>

              {selectedEvent.description && (
                <p className="text-xs text-muted-foreground leading-relaxed mb-3 max-h-32 overflow-auto">
                  {selectedEvent.description}
                </p>
              )}

              <button
                onClick={openFull}
                className="text-[11px] text-blue-400 hover:text-blue-300 hover:underline"
              >
                Open full detail →
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
