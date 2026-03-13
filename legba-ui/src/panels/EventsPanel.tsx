import { useState } from 'react'
import { useEvents } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { cn, categoryColor } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Search, ChevronLeft, ChevronRight } from 'lucide-react'

const PAGE_SIZE = 50
const CATEGORIES = ['conflict', 'political', 'economic', 'technology', 'health', 'environment', 'social', 'disaster', 'other']

export function EventsPanel() {
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('')
  const { data, isLoading } = useEvents({ offset, limit: PAGE_SIZE, q: search || undefined, category: category || undefined })
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search events..."
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOffset(0) }}
            className="w-full pl-7 pr-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <select
          value={category}
          onChange={(e) => { setCategory(e.target.value); setOffset(0) }}
          className="text-sm bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
        >
          <option value="">All categories</option>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">Loading...</div>
        ) : !data?.items.length ? (
          <div className="p-4 text-sm text-muted-foreground">No events found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card border-b border-border">
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Category</th>
                <th className="px-3 py-2 font-medium">Title</th>
                <th className="px-3 py-2 font-medium">Confidence</th>
                <th className="px-3 py-2 font-medium">Time</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((event) => (
                <tr
                  key={event.event_id}
                  className="border-b border-border/50 hover:bg-secondary/50 cursor-pointer"
                  onClick={() => {
                    select({ type: 'event', id: event.event_id, name: event.title })
                    openPanel('event-detail', { id: event.event_id })
                  }}
                >
                  <td className="px-3 py-2">
                    <Badge className={cn('text-[10px]', categoryColor(event.category))}>
                      {event.category}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 truncate max-w-[400px]">{event.title}</td>
                  <td className="px-3 py-2 text-muted-foreground">{Math.round(event.confidence * 100)}%</td>
                  <td className="px-3 py-2">
                    <TimeAgo date={event.timestamp} className="text-xs text-muted-foreground" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {data && data.total > PAGE_SIZE && (
        <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
          <span>{data.total} events</span>
          <div className="flex items-center gap-1">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              className="p-1 rounded hover:bg-secondary disabled:opacity-30"
            >
              <ChevronLeft size={14} />
            </button>
            <span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(data.total / PAGE_SIZE)}</span>
            <button
              disabled={offset + PAGE_SIZE >= data.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              className="p-1 rounded hover:bg-secondary disabled:opacity-30"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
