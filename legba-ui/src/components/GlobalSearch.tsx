import { useState, useRef, useEffect, useCallback } from 'react'
import { Search, Loader2, Users, Newspaper, FileText, AlertTriangle, X } from 'lucide-react'
import { useGlobalSearch } from '@/api/hooks'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { cn, categoryColor, entityTypeColor } from '@/lib/utils'
import type { SearchResults } from '@/api/types'

export function GlobalSearch({ collapsed }: { collapsed: boolean }) {
  const [inputValue, setInputValue] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const selectEntity = useSelectionStore((s) => s.select)

  // Debounce: update query 300ms after user stops typing
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQuery(inputValue.trim())
    }, 300)
    return () => clearTimeout(timer)
  }, [inputValue])

  const { data, isLoading, isFetching } = useGlobalSearch(debouncedQuery)

  // Open dropdown when we have a query
  useEffect(() => {
    if (debouncedQuery.length >= 2) {
      setOpen(true)
    }
  }, [debouncedQuery])

  // Close on click outside
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // Close on Escape
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setOpen(false)
      inputRef.current?.blur()
    }
  }, [])

  const handleSelect = useCallback(
    (type: string, id: string, name?: string) => {
      switch (type) {
        case 'entity':
          if (name) selectEntity({ type: 'entity', id, name })
          openPanel('entity-detail', { id })
          break
        case 'event':
          openPanel('event-detail', { id })
          break
        case 'fact':
          openPanel('facts')
          break
        case 'situation':
          openPanel('situations')
          break
      }
      setOpen(false)
      setInputValue('')
      setDebouncedQuery('')
    },
    [openPanel, selectEntity],
  )

  const clearInput = useCallback(() => {
    setInputValue('')
    setDebouncedQuery('')
    setOpen(false)
    inputRef.current?.focus()
  }, [])

  const results: SearchResults | undefined = data
  const hasResults =
    results &&
    (results.entities.length > 0 ||
      results.events.length > 0 ||
      results.facts.length > 0 ||
      results.situations.length > 0)
  const showDropdown = open && debouncedQuery.length >= 2

  // Collapsed mode: just show the search icon button
  if (collapsed) {
    return (
      <div className="px-2 py-2">
        <button
          onClick={() => {
            // Can't search when collapsed; user should expand sidebar
          }}
          className="flex items-center justify-center w-full p-1.5 rounded
                     text-muted-foreground hover:bg-secondary transition-colors"
          title="Expand sidebar to search"
        >
          <Search size={16} />
        </button>
      </div>
    )
  }

  return (
    <div ref={containerRef} className="relative px-2 py-2">
      {/* Search input */}
      <div className="relative">
        <Search
          size={14}
          className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"
        />
        <input
          ref={inputRef}
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onFocus={() => {
            if (debouncedQuery.length >= 2) setOpen(true)
          }}
          onKeyDown={handleKeyDown}
          placeholder="Search..."
          className="w-full pl-8 pr-7 py-1.5 text-xs rounded-md
                     bg-secondary/60 border border-border
                     text-foreground placeholder:text-muted-foreground
                     focus:outline-none focus:ring-1 focus:ring-primary/50 focus:border-primary/50
                     transition-colors"
        />
        {(inputValue || isFetching) && (
          <button
            onClick={clearInput}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground
                       hover:text-foreground transition-colors"
          >
            {isFetching ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <X size={13} />
            )}
          </button>
        )}
      </div>

      {/* Dropdown results */}
      {showDropdown && (
        <div
          className="absolute left-2 right-2 top-full mt-1 z-50
                     bg-card border border-border rounded-md shadow-xl
                     max-h-[70vh] overflow-y-auto"
        >
          {isLoading && !results && (
            <div className="flex items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
              <Loader2 size={14} className="animate-spin" />
              <span>Searching...</span>
            </div>
          )}

          {!isLoading && !hasResults && (
            <div className="py-6 text-center text-xs text-muted-foreground">
              No results for &ldquo;{debouncedQuery}&rdquo;
            </div>
          )}

          {results && hasResults && (
            <div className="py-1">
              {/* Entities */}
              {results.entities.length > 0 && (
                <ResultGroup icon={<Users size={13} />} label="Entities">
                  {results.entities.map((e) => (
                    <ResultItem
                      key={e.id}
                      onClick={() => handleSelect('entity', e.id, e.canonical_name)}
                    >
                      <span className="truncate flex-1">{e.canonical_name}</span>
                      <span
                        className={cn(
                          'shrink-0 text-[10px] px-1.5 py-0.5 rounded-full border',
                          entityTypeColor(e.entity_type),
                        )}
                      >
                        {e.entity_type}
                      </span>
                    </ResultItem>
                  ))}
                </ResultGroup>
              )}

              {/* Events */}
              {results.events.length > 0 && (
                <ResultGroup icon={<Newspaper size={13} />} label="Events">
                  {results.events.map((e) => (
                    <ResultItem
                      key={e.id}
                      onClick={() => handleSelect('event', e.id)}
                    >
                      <span className="truncate flex-1">{e.title}</span>
                      <span
                        className={cn(
                          'shrink-0 text-[10px] px-1.5 py-0.5 rounded-full border',
                          categoryColor(e.category),
                        )}
                      >
                        {e.category}
                      </span>
                    </ResultItem>
                  ))}
                </ResultGroup>
              )}

              {/* Facts */}
              {results.facts.length > 0 && (
                <ResultGroup icon={<FileText size={13} />} label="Facts">
                  {results.facts.map((f) => (
                    <ResultItem
                      key={f.id}
                      onClick={() => handleSelect('fact', f.id)}
                    >
                      <span className="truncate flex-1">
                        <span className="text-foreground">{f.subject}</span>
                        <span className="text-muted-foreground mx-1">{f.predicate}</span>
                        <span className="text-foreground">{f.value}</span>
                      </span>
                    </ResultItem>
                  ))}
                </ResultGroup>
              )}

              {/* Situations */}
              {results.situations.length > 0 && (
                <ResultGroup icon={<AlertTriangle size={13} />} label="Situations">
                  {results.situations.map((s) => (
                    <ResultItem
                      key={s.id}
                      onClick={() => handleSelect('situation', s.id)}
                    >
                      <span className="truncate flex-1">{s.title}</span>
                      <span
                        className={cn(
                          'shrink-0 text-[10px] px-1.5 py-0.5 rounded-full',
                          s.status === 'active'
                            ? 'bg-emerald-500/20 text-emerald-400'
                            : s.status === 'escalating'
                              ? 'bg-red-500/20 text-red-400'
                              : 'bg-gray-500/20 text-gray-400',
                        )}
                      >
                        {s.status}
                      </span>
                    </ResultItem>
                  ))}
                </ResultGroup>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ResultGroup({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode
  label: string
  children: React.ReactNode
}) {
  return (
    <div>
      <div className="flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        {icon}
        {label}
      </div>
      {children}
    </div>
  )
}

function ResultItem({
  onClick,
  children,
}: {
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-2 w-full px-3 py-1.5 text-xs text-foreground
                 hover:bg-secondary/80 transition-colors cursor-pointer text-left"
    >
      {children}
    </button>
  )
}
