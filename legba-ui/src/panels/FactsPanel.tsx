import { useState, useEffect, useCallback } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useFacts, useFactPredicates } from '@/api/hooks'
import { api } from '@/api/client'
import { TimeAgo } from '@/components/common/TimeAgo'
import { EntityLink } from '@/components/EntityLink'
import { EvidenceChainModal } from '@/components/EvidenceChain'
import {
  Search,
  ChevronLeft,
  ChevronRight,
  SlidersHorizontal,
  Link2,
  X,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import type { Fact } from '@/api/types'

const PAGE_SIZE = 50

interface FactFilters {
  predicate: string
  minConfidence: number
  subject: string
}

const DEFAULT_FILTERS: FactFilters = {
  predicate: '',
  minConfidence: 0,
  subject: '',
}

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

export function FactsPanel() {
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [filters, setFilters] = useState<FactFilters>(DEFAULT_FILTERS)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [chainFact, setChainFact] = useState<Fact | null>(null)

  const debouncedSearch = useDebounce(search, 300)
  const debouncedFilters = useDebounce(filters, 300)

  const queryParams = {
    offset,
    limit: PAGE_SIZE,
    q: debouncedSearch || undefined,
    predicate: debouncedFilters.predicate || undefined,
    min_confidence: debouncedFilters.minConfidence > 0 ? debouncedFilters.minConfidence : undefined,
    subject: debouncedFilters.subject || undefined,
  }

  const { data, isLoading } = useFacts(queryParams)
  const { data: predicates } = useFactPredicates()
  const queryClient = useQueryClient()
  const deleteMutation = useMutation({
    mutationFn: (factId: string) => api.delete(`/api/v2/facts/${factId}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['facts'] }),
  })

  // Count active filters
  const activeFilterCount =
    (filters.predicate ? 1 : 0) +
    (filters.minConfidence > 0 ? 1 : 0) +
    (filters.subject ? 1 : 0)

  const clearFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS)
    setOffset(0)
  }, [])

  // Filter chips
  const activeChips: { label: string; onRemove: () => void }[] = []
  if (filters.predicate) {
    activeChips.push({
      label: `predicate: ${filters.predicate}`,
      onRemove: () => { setFilters((f) => ({ ...f, predicate: '' })); setOffset(0) },
    })
  }
  if (filters.minConfidence > 0) {
    activeChips.push({
      label: `conf >= ${Math.round(filters.minConfidence * 100)}%`,
      onRemove: () => { setFilters((f) => ({ ...f, minConfidence: 0 })); setOffset(0) },
    })
  }
  if (filters.subject) {
    activeChips.push({
      label: `subject: ${filters.subject}`,
      onRemove: () => { setFilters((f) => ({ ...f, subject: '' })); setOffset(0) },
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
            placeholder="Search facts..."
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOffset(0) }}
            className="w-full pl-7 pr-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
      </div>

      {/* Filter bar */}
      {filtersOpen && (
        <div className="flex items-center gap-3 px-3 py-2 border-b border-border shrink-0 bg-card/50 flex-wrap">
          {/* Predicate dropdown */}
          <div className="flex items-center gap-1.5">
            <label className="text-[10px] text-muted-foreground whitespace-nowrap">Predicate</label>
            <select
              value={filters.predicate}
              onChange={(e) => { setFilters((f) => ({ ...f, predicate: e.target.value })); setOffset(0) }}
              className="text-xs bg-secondary border border-border rounded px-1.5 py-1 focus:outline-none focus:ring-1 focus:ring-primary max-w-[180px]"
            >
              <option value="">All</option>
              {predicates?.map((p) => (
                <option key={p.predicate} value={p.predicate}>
                  {p.predicate} ({p.count})
                </option>
              ))}
            </select>
          </div>

          {/* Subject search */}
          <div className="flex items-center gap-1.5">
            <label className="text-[10px] text-muted-foreground whitespace-nowrap">Subject</label>
            <input
              type="text"
              placeholder="Filter by subject..."
              value={filters.subject}
              onChange={(e) => { setFilters((f) => ({ ...f, subject: e.target.value })); setOffset(0) }}
              className="text-xs bg-secondary border border-border rounded px-1.5 py-1 focus:outline-none focus:ring-1 focus:ring-primary w-36"
            />
          </div>

          {/* Confidence slider */}
          <div className="flex items-center gap-1.5">
            <label className="text-[10px] text-muted-foreground whitespace-nowrap">Min confidence</label>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={filters.minConfidence}
              onChange={(e) => {
                setFilters((f) => ({ ...f, minConfidence: parseFloat(e.target.value) }))
                setOffset(0)
              }}
              className="w-20 h-1.5 accent-primary cursor-pointer"
            />
            <span className="text-[10px] text-foreground font-mono tabular-nums w-7">
              {Math.round(filters.minConfidence * 100)}%
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

      {/* Active filter chips (shown when filter bar is collapsed) */}
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
          <div className="p-4 text-sm text-muted-foreground">No facts found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card border-b border-border z-10">
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Subject</th>
                <th className="px-3 py-2 font-medium">Predicate</th>
                <th className="px-3 py-2 font-medium">Object</th>
                <th className="px-3 py-2 font-medium">Confidence</th>
                <th className="px-3 py-2 font-medium">Source</th>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium w-8"></th>
                <th className="px-3 py-2 font-medium w-8"></th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((fact) => (
                <tr key={fact.fact_id} className="border-b border-border/50 hover:bg-secondary/50">
                  <td className="px-3 py-2 font-medium">
                    <EntityLink name={fact.subject} />
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{fact.predicate}</td>
                  <td className="px-3 py-2">{fact.object}</td>
                  <td className="px-3 py-2 text-muted-foreground">{Math.round(fact.confidence * 100)}%</td>
                  <td className="px-3 py-2 text-muted-foreground truncate max-w-[150px]">{fact.source}</td>
                  <td className="px-3 py-2">
                    <TimeAgo date={fact.timestamp} className="text-xs text-muted-foreground" />
                  </td>
                  <td className="px-1 py-2">
                    <button
                      title="Evidence chain"
                      onClick={() => setChainFact(fact)}
                      className="p-0.5 rounded text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors"
                    >
                      <Link2 size={14} />
                    </button>
                  </td>
                  <td className="px-1 py-2">
                    <button
                      title="Delete fact"
                      disabled={deleteMutation.isPending}
                      onClick={() => {
                        if (confirm(`Delete fact "${fact.subject} ${fact.predicate} ${fact.object}"?`)) {
                          deleteMutation.mutate(fact.fact_id)
                        }
                      }}
                      className="p-0.5 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 disabled:opacity-30 transition-colors"
                    >
                      <X size={14} />
                    </button>
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
          {data ? `${data.total} fact${data.total !== 1 ? 's' : ''}` : '--'}
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

      {/* Evidence chain modal */}
      {chainFact && (
        <EvidenceChainModal
          entityType="fact"
          entityId={chainFact.fact_id}
          factSubject={chainFact.subject}
          label={`${chainFact.subject} ${chainFact.predicate} ${chainFact.object}`}
          onClose={() => setChainFact(null)}
        />
      )}
    </div>
  )
}
