import { useState } from 'react'
import { useEntities, useEntityTypes } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { cn, entityTypeColor } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Search, ChevronLeft, ChevronRight } from 'lucide-react'

const PAGE_SIZE = 50
const ENTITY_TYPES_FALLBACK = ['person', 'organization', 'location', 'country', 'event', 'concept', 'weapon', 'military_unit', 'infrastructure']

export function EntitiesPanel() {
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [entityType, setEntityType] = useState('')
  const { data, isLoading } = useEntities({ offset, limit: PAGE_SIZE, q: search || undefined, type: entityType || undefined })
  const { data: entityTypeCounts } = useEntityTypes()
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  // Use dynamic types from API, fall back to hardcoded list
  const typeOptions = entityTypeCounts && entityTypeCounts.length > 0
    ? entityTypeCounts.map((t) => ({ value: t.type, label: `${t.type} (${t.count})` }))
    : ENTITY_TYPES_FALLBACK.map((t) => ({ value: t, label: t }))

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search entities..."
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOffset(0) }}
            className="w-full pl-7 pr-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <select
          value={entityType}
          onChange={(e) => { setEntityType(e.target.value); setOffset(0) }}
          className="text-sm bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
        >
          <option value="">All types</option>
          {typeOptions.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">Loading...</div>
        ) : !data?.items.length ? (
          <div className="p-4 text-sm text-muted-foreground">No entities found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card border-b border-border">
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Events</th>
                <th className="px-3 py-2 font-medium">Last Seen</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((entity) => (
                <tr
                  key={entity.entity_id}
                  className="border-b border-border/50 hover:bg-secondary/50 cursor-pointer"
                  onClick={() => {
                    select({ type: 'entity', id: entity.entity_id, name: entity.name })
                    openPanel('entity-detail', { id: entity.entity_id })
                  }}
                >
                  <td className="px-3 py-2">
                    <Badge className={cn('text-[10px]', entityTypeColor(entity.entity_type))}>
                      {entity.entity_type}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">{entity.name}</td>
                  <td className="px-3 py-2 text-muted-foreground">{entity.event_count}</td>
                  <td className="px-3 py-2">
                    <TimeAgo date={entity.last_seen} className="text-xs text-muted-foreground" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {data && data.total > PAGE_SIZE && (
        <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
          <span>{data.total} entities</span>
          <div className="flex items-center gap-1">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} className="p-1 rounded hover:bg-secondary disabled:opacity-30">
              <ChevronLeft size={14} />
            </button>
            <span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(data.total / PAGE_SIZE)}</span>
            <button disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)} className="p-1 rounded hover:bg-secondary disabled:opacity-30">
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
