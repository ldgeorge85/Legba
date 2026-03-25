import { useState, useEffect, useCallback } from 'react'
import { useEntities, useEntityTypes } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { cn, entityTypeColor } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { EntityLink } from '@/components/EntityLink'
import {
  Search,
  ChevronLeft,
  ChevronRight,
  SlidersHorizontal,
  X,
} from 'lucide-react'

const PAGE_SIZE = 50
const ENTITY_TYPES_FALLBACK = ['person', 'organization', 'location', 'country', 'event', 'concept', 'weapon', 'military_unit', 'infrastructure']

interface EntityFilters {
  entityType: string
  createdAfter: string
  minCompleteness: number
}

const DEFAULT_FILTERS: EntityFilters = {
  entityType: '',
  createdAfter: '',
  minCompleteness: 0,
}

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

export function EntitiesPanel() {
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [filters, setFilters] = useState<EntityFilters>(DEFAULT_FILTERS)
  const [filtersOpen, setFiltersOpen] = useState(false)

  const debouncedSearch = useDebounce(search, 300)
  const debouncedFilters = useDebounce(filters, 300)

  const queryParams = {
    offset,
    limit: PAGE_SIZE,
    q: debouncedSearch || undefined,
    type: debouncedFilters.entityType || undefined,
    min_completeness: debouncedFilters.minCompleteness > 0 ? debouncedFilters.minCompleteness : undefined,
    created_after: debouncedFilters.createdAfter || undefined,
  }

  const { data, isLoading } = useEntities(queryParams)
  const { data: entityTypeCounts } = useEntityTypes()
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  // Use dynamic types from API, fall back to hardcoded list
  const typeOptions = entityTypeCounts && entityTypeCounts.length > 0
    ? entityTypeCounts.map((t) => ({ value: t.type, label: `${t.type} (${t.count})` }))
    : ENTITY_TYPES_FALLBACK.map((t) => ({ value: t, label: t }))

  // Count active filters
  const activeFilterCount =
    (filters.entityType ? 1 : 0) +
    (filters.createdAfter ? 1 : 0) +
    (filters.minCompleteness > 0 ? 1 : 0)

  const clearFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS)
    setOffset(0)
  }, [])

  // Filter chips
  const activeChips: { label: string; onRemove: () => void }[] = []
  if (filters.entityType) {
    activeChips.push({
      label: `type: ${filters.entityType}`,
      onRemove: () => { setFilters((f) => ({ ...f, entityType: '' })); setOffset(0) },
    })
  }
  if (filters.createdAfter) {
    activeChips.push({
      label: `after: ${filters.createdAfter}`,
      onRemove: () => { setFilters((f) => ({ ...f, createdAfter: '' })); setOffset(0) },
    })
  }
  if (filters.minCompleteness > 0) {
    activeChips.push({
      label: `completeness >= ${Math.round(filters.minCompleteness * 100)}%`,
      onRemove: () => { setFilters((f) => ({ ...f, minCompleteness: 0 })); setOffset(0) },
    })
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <button
          onClick={() => setFiltersOpen(!filtersOpen)}
          className={cn(
            'p-1.5 rounded border border-border hover:bg-secondary transition-colors relative',
            filtersOpen && 'bg-primary/10 border-primary/40'
          )}
          title="Toggle filters"
        >
          <SlidersHorizontal size={14} />
          {activeFilterCount > 0 && (
            <span className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-primary text-[9px] flex items-center justify-center text-primary-foreground font-bold">
              {activeFilterCount}
            </span>
          )}
        </button>
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
      </div>

      {/* Filter bar */}
      {filtersOpen && (
        <div className="flex items-center gap-3 px-3 py-2 border-b border-border shrink-0 bg-card/50 flex-wrap">
          {/* Entity type */}
          <div className="flex items-center gap-1.5">
            <label className="text-[10px] text-muted-foreground whitespace-nowrap">Type</label>
            <select
              value={filters.entityType}
              onChange={(e) => { setFilters((f) => ({ ...f, entityType: e.target.value })); setOffset(0) }}
              className="text-xs bg-secondary border border-border rounded px-1.5 py-1 focus:outline-none focus:ring-1 focus:ring-primary"
            >
              <option value="">All</option>
              {typeOptions.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>

          {/* Created after */}
          <div className="flex items-center gap-1.5">
            <label className="text-[10px] text-muted-foreground whitespace-nowrap">Created after</label>
            <input
              type="date"
              value={filters.createdAfter}
              onChange={(e) => { setFilters((f) => ({ ...f, createdAfter: e.target.value })); setOffset(0) }}
              className="text-xs bg-secondary border border-border rounded px-1.5 py-1 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>

          {/* Completeness slider */}
          <div className="flex items-center gap-1.5">
            <label className="text-[10px] text-muted-foreground whitespace-nowrap">Min completeness</label>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={filters.minCompleteness}
              onChange={(e) => {
                setFilters((f) => ({ ...f, minCompleteness: parseFloat(e.target.value) }))
                setOffset(0)
              }}
              className="w-20 h-1.5 accent-primary cursor-pointer"
            />
            <span className="text-[10px] text-foreground font-mono tabular-nums w-7">
              {Math.round(filters.minCompleteness * 100)}%
            </span>
          </div>

          {activeFilterCount > 0 && (
            <button
              onClick={clearFilters}
              className="text-[10px] text-muted-foreground hover:text-foreground ml-auto"
            >
              Clear all
            </button>
          )}
        </div>
      )}

      {/* Active filter chips */}
      {activeChips.length > 0 && !filtersOpen && (
        <div className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0 flex-wrap">
          <span className="text-[10px] text-muted-foreground mr-1">Filters:</span>
          {activeChips.map((chip) => (
            <span
              key={chip.label}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] bg-primary/10 text-primary border border-primary/20 rounded"
            >
              {chip.label}
              <button
                onClick={chip.onRemove}
                className="hover:text-destructive transition-colors"
              >
                <X size={10} />
              </button>
            </span>
          ))}
          <button
            onClick={clearFilters}
            className="text-[10px] text-muted-foreground hover:text-foreground ml-1"
          >
            Clear all
          </button>
        </div>
      )}

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">Loading...</div>
        ) : !data?.items.length ? (
          <div className="p-4 text-sm text-muted-foreground">No entities found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card border-b border-border z-10">
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Completeness</th>
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
                  <td className="px-3 py-2">
                    <EntityLink name={entity.name} id={entity.entity_id} type={entity.entity_type} />
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {entity.completeness != null ? `${Math.round(entity.completeness * 100)}%` : '--'}
                  </td>
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

      {/* Pagination */}
      <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
        <span>
          {data ? `${data.total} entit${data.total !== 1 ? 'ies' : 'y'}` : '--'}
          {activeFilterCount > 0 && ' matching'}
        </span>
        {data && data.total > PAGE_SIZE && (
          <div className="flex items-center gap-1">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              className="p-1 rounded hover:bg-secondary disabled:opacity-30"
            >
              <ChevronLeft size={14} />
            </button>
            <span>
              {Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(data.total / PAGE_SIZE)}
            </span>
            <button
              disabled={offset + PAGE_SIZE >= data.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              className="p-1 rounded hover:bg-secondary disabled:opacity-30"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
