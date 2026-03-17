import { useState, useEffect, useCallback } from 'react'
import { useSignals, useSignalFacets } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { cn, categoryColor } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import {
  Search,
  ChevronLeft,
  ChevronRight,
  SlidersHorizontal,
  X,
  ChevronDown,
  ChevronUp,
} from 'lucide-react'

const PAGE_SIZE = 50
const CATEGORIES = [
  'conflict',
  'political',
  'economic',
  'technology',
  'health',
  'environment',
  'social',
  'disaster',
  'other',
]

interface SignalFilters {
  categories: string[]
  source: string
  startDate: string
  endDate: string
  minConfidence: number
}

const DEFAULT_FILTERS: SignalFilters = {
  categories: [],
  source: '',
  startDate: '',
  endDate: '',
  minConfidence: 0,
}

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

export function SignalsPanel() {
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [filters, setFilters] = useState<SignalFilters>(DEFAULT_FILTERS)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sectionsOpen, setSectionsOpen] = useState({
    categories: true,
    dates: true,
    confidence: true,
    sources: false,
  })

  const debouncedFilters = useDebounce(filters, 300)
  const debouncedSearch = useDebounce(search, 300)

  const { data: facets } = useSignalFacets()

  // Build query params from debounced filters
  const queryParams = {
    offset,
    limit: PAGE_SIZE,
    q: debouncedSearch || undefined,
    category: debouncedFilters.categories.length > 0 ? debouncedFilters.categories.join(',') : undefined,
    source: debouncedFilters.source || undefined,
    start_date: debouncedFilters.startDate || undefined,
    end_date: debouncedFilters.endDate || undefined,
    min_confidence: debouncedFilters.minConfidence > 0 ? debouncedFilters.minConfidence : undefined,
  }

  const { data, isLoading } = useSignals(queryParams)
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  // Count active filters
  const activeFilterCount =
    (filters.categories.length > 0 ? 1 : 0) +
    (filters.source ? 1 : 0) +
    (filters.startDate ? 1 : 0) +
    (filters.endDate ? 1 : 0) +
    (filters.minConfidence > 0 ? 1 : 0)

  const toggleCategory = useCallback(
    (cat: string) => {
      setFilters((f) => ({
        ...f,
        categories: f.categories.includes(cat)
          ? f.categories.filter((c) => c !== cat)
          : [...f.categories, cat],
      }))
      setOffset(0)
    },
    []
  )

  const clearFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS)
    setOffset(0)
  }, [])

  const toggleSection = (section: keyof typeof sectionsOpen) => {
    setSectionsOpen((s) => ({ ...s, [section]: !s[section] }))
  }

  // Filter chips for display
  const activeChips: { label: string; onRemove: () => void }[] = []
  if (filters.categories.length > 0) {
    filters.categories.forEach((cat) => {
      activeChips.push({
        label: cat,
        onRemove: () => toggleCategory(cat),
      })
    })
  }
  if (filters.source) {
    activeChips.push({
      label: `source: ${filters.source}`,
      onRemove: () => setFilters((f) => ({ ...f, source: '' })),
    })
  }
  if (filters.startDate) {
    activeChips.push({
      label: `from: ${filters.startDate}`,
      onRemove: () => setFilters((f) => ({ ...f, startDate: '' })),
    })
  }
  if (filters.endDate) {
    activeChips.push({
      label: `to: ${filters.endDate}`,
      onRemove: () => setFilters((f) => ({ ...f, endDate: '' })),
    })
  }
  if (filters.minConfidence > 0) {
    activeChips.push({
      label: `conf >= ${Math.round(filters.minConfidence * 100)}%`,
      onRemove: () => setFilters((f) => ({ ...f, minConfidence: 0 })),
    })
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className={cn(
            'p-1.5 rounded border border-border hover:bg-secondary transition-colors relative',
            sidebarOpen && 'bg-primary/10 border-primary/40'
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
          <Search
            size={14}
            className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <input
            type="text"
            placeholder="Search signals..."
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setOffset(0)
            }}
            className="w-full pl-7 pr-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
      </div>

      {/* Active filter chips */}
      {activeChips.length > 0 && (
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

      {/* Main content: sidebar + table */}
      <div className="flex flex-1 overflow-hidden">
        {/* Filter Sidebar */}
        {sidebarOpen && (
          <div className="w-[200px] shrink-0 border-r border-border overflow-y-auto bg-card/50">
            {/* Categories */}
            <FilterSection
              title="Category"
              open={sectionsOpen.categories}
              onToggle={() => toggleSection('categories')}
            >
              {CATEGORIES.map((cat) => {
                const count = facets?.categories?.[cat] ?? 0
                const isSelected = filters.categories.includes(cat)
                return (
                  <label
                    key={cat}
                    className="flex items-center gap-2 px-3 py-1 hover:bg-secondary/50 cursor-pointer text-xs"
                  >
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => toggleCategory(cat)}
                      className="rounded border-border bg-secondary accent-primary w-3 h-3"
                    />
                    <span
                      className={cn(
                        'flex-1 capitalize',
                        isSelected ? 'text-foreground' : 'text-muted-foreground'
                      )}
                    >
                      {cat}
                    </span>
                    <span className="text-[10px] text-muted-foreground tabular-nums">
                      {count}
                    </span>
                  </label>
                )
              })}
            </FilterSection>

            {/* Date Range */}
            <FilterSection
              title="Date Range"
              open={sectionsOpen.dates}
              onToggle={() => toggleSection('dates')}
            >
              <div className="px-3 py-1 space-y-1.5">
                <div>
                  <label className="text-[10px] text-muted-foreground">From</label>
                  <input
                    type="date"
                    value={filters.startDate}
                    onChange={(e) => {
                      setFilters((f) => ({ ...f, startDate: e.target.value }))
                      setOffset(0)
                    }}
                    className="w-full text-xs bg-secondary border border-border rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-muted-foreground">To</label>
                  <input
                    type="date"
                    value={filters.endDate}
                    onChange={(e) => {
                      setFilters((f) => ({ ...f, endDate: e.target.value }))
                      setOffset(0)
                    }}
                    className="w-full text-xs bg-secondary border border-border rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary"
                  />
                </div>
              </div>
            </FilterSection>

            {/* Confidence */}
            <FilterSection
              title="Confidence"
              open={sectionsOpen.confidence}
              onToggle={() => toggleSection('confidence')}
            >
              <div className="px-3 py-1 space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-muted-foreground">Minimum</span>
                  <span className="text-[10px] text-foreground font-mono tabular-nums">
                    {Math.round(filters.minConfidence * 100)}%
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={filters.minConfidence}
                  onChange={(e) => {
                    setFilters((f) => ({
                      ...f,
                      minConfidence: parseFloat(e.target.value),
                    }))
                    setOffset(0)
                  }}
                  className="w-full h-1.5 accent-primary cursor-pointer"
                />
                <div className="flex justify-between text-[9px] text-muted-foreground">
                  <span>0%</span>
                  <span>50%</span>
                  <span>100%</span>
                </div>
              </div>
            </FilterSection>

            {/* Sources */}
            <FilterSection
              title="Source"
              open={sectionsOpen.sources}
              onToggle={() => toggleSection('sources')}
            >
              <div className="px-3 py-1">
                <select
                  value={filters.source}
                  onChange={(e) => {
                    setFilters((f) => ({ ...f, source: e.target.value }))
                    setOffset(0)
                  }}
                  className="w-full text-xs bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
                >
                  <option value="">All sources</option>
                  {facets?.sources &&
                    Object.entries(facets.sources).map(([name, count]) => (
                      <option key={name} value={name}>
                        {name} ({count})
                      </option>
                    ))}
                </select>
              </div>
            </FilterSection>
          </div>
        )}

        {/* Signal Table */}
        <div className="flex-1 overflow-auto">
          {isLoading ? (
            <div className="p-4 text-sm text-muted-foreground">Loading...</div>
          ) : !data?.items.length ? (
            <div className="p-4 text-sm text-muted-foreground">No signals found</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-card border-b border-border z-10">
                <tr className="text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Category</th>
                  <th className="px-3 py-2 font-medium">Title</th>
                  <th className="px-3 py-2 font-medium">Confidence</th>
                  <th className="px-3 py-2 font-medium">Time</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((signal) => (
                  <tr
                    key={signal.event_id}
                    className="border-b border-border/50 hover:bg-secondary/50 cursor-pointer"
                    onClick={() => {
                      select({
                        type: 'event',
                        id: signal.event_id,
                        name: signal.title,
                      })
                      openPanel('event-detail', { id: signal.event_id })
                    }}
                  >
                    <td className="px-3 py-2">
                      <Badge
                        className={cn('text-[10px]', categoryColor(signal.category))}
                      >
                        {signal.category}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 truncate max-w-[400px]">{signal.title}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {Math.round(signal.confidence * 100)}%
                    </td>
                    <td className="px-3 py-2">
                      <TimeAgo
                        date={signal.timestamp}
                        className="text-xs text-muted-foreground"
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
        <span>
          {data ? `${data.total} signal${data.total !== 1 ? 's' : ''}` : '--'}
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
              {Math.floor(offset / PAGE_SIZE) + 1} /{' '}
              {Math.ceil(data.total / PAGE_SIZE)}
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

/** Collapsible sidebar section */
function FilterSection({
  title,
  open,
  onToggle,
  children,
}: {
  title: string
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div className="border-b border-border/50">
      <button
        onClick={onToggle}
        className="flex items-center justify-between w-full px-3 py-2 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
      >
        {title}
        {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
      </button>
      {open && <div className="pb-2">{children}</div>}
    </div>
  )
}
