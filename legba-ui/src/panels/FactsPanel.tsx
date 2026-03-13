import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useFacts } from '@/api/hooks'
import { api } from '@/api/client'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Search, ChevronLeft, ChevronRight, X } from 'lucide-react'

const PAGE_SIZE = 50

export function FactsPanel() {
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const { data, isLoading } = useFacts({ offset, limit: PAGE_SIZE, q: search || undefined })
  const queryClient = useQueryClient()
  const deleteMutation = useMutation({
    mutationFn: (factId: string) => api.delete(`/api/v2/facts/${factId}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['facts'] }),
  })

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
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

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">Loading...</div>
        ) : !data?.items.length ? (
          <div className="p-4 text-sm text-muted-foreground">No facts found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card border-b border-border">
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Subject</th>
                <th className="px-3 py-2 font-medium">Predicate</th>
                <th className="px-3 py-2 font-medium">Object</th>
                <th className="px-3 py-2 font-medium">Confidence</th>
                <th className="px-3 py-2 font-medium">Source</th>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium w-8"></th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((fact) => (
                <tr key={fact.fact_id} className="border-b border-border/50 hover:bg-secondary/50">
                  <td className="px-3 py-2 font-medium">{fact.subject}</td>
                  <td className="px-3 py-2 text-muted-foreground">{fact.predicate}</td>
                  <td className="px-3 py-2">{fact.object}</td>
                  <td className="px-3 py-2 text-muted-foreground">{Math.round(fact.confidence * 100)}%</td>
                  <td className="px-3 py-2 text-muted-foreground truncate max-w-[150px]">{fact.source}</td>
                  <td className="px-3 py-2">
                    <TimeAgo date={fact.timestamp} className="text-xs text-muted-foreground" />
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
      {data && data.total > PAGE_SIZE && (
        <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
          <span>{data.total} facts</span>
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
