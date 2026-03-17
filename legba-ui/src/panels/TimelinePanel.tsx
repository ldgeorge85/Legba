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

const DENSITY_BUCKETS = 24

const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ef4444',
  high: '#f97316',
  medium: '#eab308',
  low: '#3b82f6',
}

interface SelectedEvent {
  event_id: string
  title: string
  category: string
  timestamp: string
  created_at?: string
  description?: string
  source_name?: string | null
  severity?: string | null
}

// Density bar component rendered above the timeline
function DensityBar({ timestamps, hiddenCats, categoryMap }: {
  timestamps: { ms: number; cat: string }[]
  hiddenCats: Set<string>
  categoryMap: Record<string, string>
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || timestamps.length === 0) return

    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * dpr
    canvas.height = rect.height * dpr
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, rect.width, rect.height)

    const visible = timestamps.filter(t => !hiddenCats.has(t.cat))
    if (visible.length === 0) return

    const minMs = Math.min(...visible.map(t => t.ms))
    const maxMs = Math.max(...visible.map(t => t.ms))
    const range = maxMs - minMs || 1

    // Bucket events by time, tracking category breakdown
    const buckets: Record<string, number>[] = Array.from({ length: DENSITY_BUCKETS }, () => ({}))
    for (const t of visible) {
      const idx = Math.min(Math.floor(((t.ms - minMs) / range) * DENSITY_BUCKETS), DENSITY_BUCKETS - 1)
      buckets[idx][t.cat] = (buckets[idx][t.cat] || 0) + 1
    }

    const maxCount = Math.max(...buckets.map(b => Object.values(b).reduce((s, v) => s + v, 0)), 1)
    const barW = rect.width / DENSITY_BUCKETS
    const maxH = rect.height - 2 // 1px padding top/bottom

    for (let i = 0; i < DENSITY_BUCKETS; i++) {
      const bucket = buckets[i]
      const total = Object.values(bucket).reduce((s, v) => s + v, 0)
      if (total === 0) continue

      const barH = Math.max(2, (total / maxCount) * maxH)
      const x = i * barW
      const y = rect.height - barH - 1

      // Draw stacked segments by category
      let offsetY = 0
      for (const cat of CATEGORIES) {
        const count = bucket[cat] || 0
        if (count === 0) continue
        const segH = (count / total) * barH
        const color = categoryMap[cat] || '#6b7280'
        ctx.fillStyle = color + '90' // semi-transparent
        ctx.fillRect(x + 1, y + offsetY, barW - 2, segH)
        offsetY += segH
      }
    }
  }, [timestamps, hiddenCats, categoryMap])

  return (
    <canvas
      ref={canvasRef}
      className="w-full shrink-0"
      style={{ height: '28px', background: 'transparent' }}
    />
  )
}

export function TimelinePanel() {
  const containerRef = useRef<HTMLDivElement>(null)
  const timelineRef = useRef<any>(null)
  const dataRef = useRef<any>(null)
  const timestampsRef = useRef<{ ms: number; cat: string }[]>([])
  const [tlHeight, setTlHeight] = useState(300)
  const [page, setPage] = useState(0)
  const [selectedEvent, setSelectedEvent] = useState<SelectedEvent | null>(null)
  const [hiddenCats, setHiddenCats] = useState<Set<string>>(new Set())
  const [activeRange, setActiveRange] = useState<string>('all')
  const [densityTs, setDensityTs] = useState<{ ms: number; cat: string }[]>([])
  const { data } = useEvents({ offset: page * 200, limit: 200 })
  const select = useSelectionStore((s) => s.select)
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  // Track container size
  const wrapRef = useCallback((node: HTMLDivElement | null) => {
    if (!node) return
    const update = () => {
      // Timeline gets ~55% of available height when detail is shown, rest for detail
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
  const fitAll = () => {
    timelineRef.current?.fit()
    setActiveRange('all')
  }

  // Time range presets — set the timeline window to the appropriate range
  const setTimeRange = (range: string) => {
    const tl = timelineRef.current
    if (!tl) return

    setActiveRange(range)
    const now = Date.now()

    switch (range) {
      case '24h':
        tl.setWindow(now - 86400000, now + 3600000)
        break
      case '7d':
        tl.setWindow(now - 86400000 * 7, now + 3600000)
        break
      case '30d':
        tl.setWindow(now - 86400000 * 30, now + 3600000)
        break
      case 'all':
        tl.fit()
        break
    }
  }

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

      // Build groups with subtle bottom border for separation
      const visibleCats = CATEGORIES.filter((c) => !hiddenCats.has(c))
      const groups = new DataSet(
        visibleCats.map((cat) => ({
          id: cat,
          content: `<span style="color:${CATEGORY_COLORS[cat]}; text-transform:uppercase; letter-spacing:0.5px">${cat}</span>`,
        })),
      )

      // Build items — use point type for density, box for detail
      const allTimestamps: { ms: number; cat: string }[] = []
      const processed: any[] = []
      for (const ev of data!.items) {
        const cat = (CATEGORIES as readonly string[]).includes(ev.category) ? ev.category : 'other'
        if (hiddenCats.has(cat)) continue
        const ms = new Date(ev.created_at ?? ev.timestamp ?? '').getTime()
        if (isNaN(ms)) continue
        allTimestamps.push({ ms, cat })
        const color = CATEGORY_COLORS[cat] ?? CATEGORY_COLORS.other
        const sevColor = ev.severity && ev.severity !== 'medium' ? SEVERITY_COLORS[ev.severity] : null
        // Short label for timeline items — keeps things compact at overview zoom
        const label = ev.title.length > 28 ? ev.title.slice(0, 25) + '...' : ev.title
        const sevStyle = sevColor ? `border-left: 3px solid ${sevColor};` : ''
        processed.push({
          id: ev.event_id,
          group: cat,
          content: `<span class="tl-item-label" style="color:#e2e8f0">${label}</span>`,
          start: ms,
          type: 'point',
          title: `${ev.title}${ev.severity ? ` [${ev.severity}]` : ''}`,
          style: `background:${color}30; border-color:${color}; color:${color}; font-size:11px; cursor:pointer; ${sevStyle}`,
        })
      }
      if (processed.length === 0 || allTimestamps.length === 0) return

      const items = new DataSet(processed)
      const tsMs = allTimestamps.map(t => t.ms)
      const minTs = Math.min(...tsMs)
      const maxTs = Math.max(...tsMs)

      // Store refs for click handler and density bar
      dataRef.current = data
      timestampsRef.current = allTimestamps
      setDensityTs(allTimestamps)

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
        zoomMax: 86400000 * 60,  // 60 days (increased for 30d preset)
        zoomKey: 'ctrlKey',
        stack: true,
        groupOrder: (a: any, b: any) => CATEGORIES.indexOf(a.id) - CATEGORIES.indexOf(b.id),
        tooltip: { followMouse: true, overflowMethod: 'cap', delay: 100 },
        margin: { item: { horizontal: 2, vertical: 3 } },
        orientation: { axis: 'bottom', item: 'top' },
        verticalScroll: true,
        maxHeight: `${tlHeight}px`,
      })

      // Set window from data
      tl.setWindow(minTs - 3600000, maxTs + 3600000)

      // Zoom-adaptive rendering: switch between point and box based on visible range
      tl.on('rangechanged', () => {
        const win = tl.getWindow()
        const visibleMs = win.end.getTime() - win.start.getTime()
        // If zoomed in to less than 2 days, switch to box mode with labels
        const useBox = visibleMs < 86400000 * 2

        const currentItems = items.get()
        const updates: any[] = []
        for (const item of currentItems) {
          const newType = useBox ? 'box' : 'point'
          if (item.type !== newType) {
            updates.push({ id: item.id, type: newType })
          }
        }
        if (updates.length > 0) {
          items.update(updates)
        }
      })

      // Click -> show detail in panel
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

  // Dark theme CSS + density improvements
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
      .vis-labelset .vis-label { color: #a1a1aa !important; font-size: 11px !important; font-weight: 500 !important; border-bottom: 1px solid #27272a !important; padding-left: 8px !important; }
      .vis-foreground .vis-group { border-bottom: 1px solid #27272a !important; }
      .vis-item { border-radius: 3px !important; font-size: 11px !important; min-height: 18px !important; }
      .vis-item .vis-item-content { padding: 2px 6px !important; white-space: nowrap !important; overflow: hidden !important; }
      .vis-item.vis-point .vis-dot { border-width: 3px !important; border-radius: 50% !important; }
      .vis-item.vis-point .vis-item-content { padding: 1px 4px !important; }
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

  const rangePresets = [
    { key: '24h', label: '24h' },
    { key: '7d', label: '7d' },
    { key: '30d', label: '30d' },
    { key: 'all', label: 'All' },
  ]

  return (
    <div className="flex flex-col h-full overflow-hidden bg-background">
      {/* Toolbar */}
      <div className="flex items-center gap-1.5 px-2 py-1 border-b border-border shrink-0 flex-wrap">
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

        {/* Time range presets */}
        <div className="flex items-center gap-0.5">
          {rangePresets.map((p) => (
            <button
              key={p.key}
              onClick={() => setTimeRange(p.key)}
              className="px-1.5 py-0.5 rounded text-[10px] font-medium transition-colors"
              style={{
                backgroundColor: activeRange === p.key ? '#3b82f630' : 'transparent',
                color: activeRange === p.key ? '#60a5fa' : '#a1a1aa',
                border: `1px solid ${activeRange === p.key ? '#3b82f650' : '#27272a'}`,
              }}
              title={`Show ${p.label}`}
            >
              {p.label}
            </button>
          ))}
        </div>

        <div className="w-px h-4 bg-border mx-1" />

        {/* Zoom controls */}
        <button onClick={zoomIn} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-xs text-foreground" title="Zoom in">+</button>
        <button onClick={zoomOut} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-xs text-foreground" title="Zoom out">-</button>
        <button onClick={fitAll} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-[10px] text-foreground" title="Fit all events">Fit</button>

        <div className="w-px h-4 bg-border mx-1" />

        {/* Pagination */}
        {page > 0 && (
          <button onClick={() => setPage(page - 1)} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-[10px] text-foreground">Newer</button>
        )}
        {data && data.total > (page + 1) * 200 && (
          <button onClick={() => setPage(page + 1)} className="px-1.5 py-0.5 rounded bg-secondary hover:bg-secondary/80 text-[10px] text-foreground">Older</button>
        )}
      </div>

      {/* Main content area */}
      <div ref={wrapRef} className="flex-1 min-h-0 flex flex-col">
        {/* Density bar */}
        {densityTs.length > 0 && (
          <DensityBar timestamps={densityTs} hiddenCats={hiddenCats} categoryMap={CATEGORY_COLORS} />
        )}

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
                {selectedEvent.severity && selectedEvent.severity !== 'medium' && (
                  <span
                    className="shrink-0 mt-0.5 inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase"
                    style={{
                      backgroundColor: (SEVERITY_COLORS[selectedEvent.severity] ?? '#6b7280') + '25',
                      color: SEVERITY_COLORS[selectedEvent.severity] ?? '#6b7280',
                    }}
                  >
                    {selectedEvent.severity}
                  </span>
                )}
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
