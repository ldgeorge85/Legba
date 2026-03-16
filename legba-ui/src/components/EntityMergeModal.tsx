import { useState, useRef, useEffect, useCallback } from 'react'
import { X, Search, Merge, AlertTriangle, Check, Loader2 } from 'lucide-react'
import { useSearchEntities, useMergePreview, useMergeEntities } from '@/api/hooks'
import { Badge } from '@/components/common/Badge'
import { cn, entityTypeColor } from '@/lib/utils'
import type { EntitySummary } from '@/api/types'

interface Props {
  removeId: string
  removeName: string
  onClose: () => void
}

export function EntityMergeModal({ removeId, removeName, onClose }: Props) {
  const [query, setQuery] = useState('')
  const [selectedTarget, setSelectedTarget] = useState<EntitySummary | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const { data: searchResults, isLoading: searching } = useSearchEntities(query)
  const { data: preview, isLoading: previewing } = useMergePreview(
    selectedTarget?.entity_id ?? null,
    removeId,
  )
  const merge = useMergeEntities()

  // Focus the search input on mount
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const handleConfirm = useCallback(async () => {
    if (!selectedTarget) return
    setConfirmed(true)
    try {
      await merge.mutateAsync({ keepId: selectedTarget.entity_id, removeId })
    } catch {
      setConfirmed(false)
    }
  }, [selectedTarget, removeId, merge])

  // Filter out the entity being removed from search results
  const filteredResults = (searchResults?.items ?? []).filter(
    (e) => e.entity_id !== removeId,
  )

  const mergeSucceeded = merge.isSuccess && merge.data?.success

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />

      {/* Modal */}
      <div className="relative w-full max-w-lg mx-4 rounded-lg border border-gray-700 bg-gray-900 shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
          <div className="flex items-center gap-2">
            <Merge className="h-4 w-4 text-gray-400" />
            <h3 className="text-sm font-semibold text-gray-100">Merge Entity</h3>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Body */}
        <div className="px-4 py-4 space-y-4">
          {/* Success state */}
          {mergeSucceeded ? (
            <div className="text-center py-6 space-y-3">
              <div className="mx-auto w-10 h-10 rounded-full bg-green-500/20 flex items-center justify-center">
                <Check className="h-5 w-5 text-green-400" />
              </div>
              <p className="text-sm text-gray-100">
                Merged <span className="font-medium">{removeName}</span> into{' '}
                <span className="font-medium">{selectedTarget?.name}</span>
              </p>
              <div className="text-xs text-gray-400 space-y-0.5">
                {merge.data?.events_moved != null && (
                  <p>{merge.data.events_moved} events moved</p>
                )}
                {merge.data?.facts_moved != null && (
                  <p>{merge.data.facts_moved} facts moved</p>
                )}
                {merge.data?.graph_edges_moved != null && (
                  <p>{merge.data.graph_edges_moved} graph edges moved</p>
                )}
              </div>
              <button
                onClick={onClose}
                className="mt-2 px-4 py-1.5 text-sm rounded bg-gray-800 hover:bg-gray-700 text-gray-200 transition-colors"
              >
                Close
              </button>
            </div>
          ) : (
            <>
              {/* Entity being removed */}
              <div className="rounded border border-red-500/30 bg-red-500/5 px-3 py-2">
                <p className="text-xs text-red-400 font-medium mb-1">Will be removed</p>
                <p className="text-sm text-gray-100">{removeName}</p>
                <p className="text-xs text-gray-500 font-mono">{removeId.slice(0, 8)}</p>
              </div>

              {/* Search for target */}
              {!selectedTarget ? (
                <div className="space-y-2">
                  <label className="text-xs text-gray-400 font-medium">
                    Search target entity to merge into
                  </label>
                  <div className="relative">
                    <Search className="absolute left-2.5 top-2 h-4 w-4 text-gray-500" />
                    <input
                      ref={inputRef}
                      type="text"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      placeholder="Search entities..."
                      className="w-full pl-8 pr-3 py-1.5 text-sm rounded border border-gray-700 bg-gray-800 text-gray-100 placeholder-gray-500 focus:outline-none focus:border-gray-500 transition-colors"
                    />
                  </div>

                  {/* Search results */}
                  {searching && (
                    <div className="flex items-center gap-2 py-3 text-xs text-gray-400">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      Searching...
                    </div>
                  )}

                  {!searching && query.length >= 2 && filteredResults.length === 0 && (
                    <p className="py-3 text-xs text-gray-500 text-center">No entities found</p>
                  )}

                  {filteredResults.length > 0 && (
                    <div className="max-h-48 overflow-y-auto rounded border border-gray-700 divide-y divide-gray-800">
                      {filteredResults.map((entity) => (
                        <button
                          key={entity.entity_id}
                          onClick={() => setSelectedTarget(entity)}
                          className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-800 transition-colors"
                        >
                          <Badge className={cn(entityTypeColor(entity.entity_type), 'text-[10px]')}>
                            {entity.entity_type}
                          </Badge>
                          <span className="text-sm text-gray-100 flex-1 truncate">
                            {entity.name}
                          </span>
                          <span className="text-xs text-gray-500">{entity.event_count} events</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <>
                  {/* Selected target */}
                  <div className="rounded border border-green-500/30 bg-green-500/5 px-3 py-2">
                    <div className="flex items-center justify-between">
                      <p className="text-xs text-green-400 font-medium mb-1">Will be kept</p>
                      <button
                        onClick={() => {
                          setSelectedTarget(null)
                          setQuery('')
                        }}
                        className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
                      >
                        Change
                      </button>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge className={cn(entityTypeColor(selectedTarget.entity_type), 'text-[10px]')}>
                        {selectedTarget.entity_type}
                      </Badge>
                      <span className="text-sm text-gray-100">{selectedTarget.name}</span>
                    </div>
                    <p className="text-xs text-gray-500 font-mono mt-0.5">
                      {selectedTarget.entity_id.slice(0, 8)}
                    </p>
                  </div>

                  {/* Preview */}
                  {previewing && (
                    <div className="flex items-center gap-2 py-3 text-xs text-gray-400">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      Loading preview...
                    </div>
                  )}

                  {preview && preview.success && (
                    <div className="rounded border border-gray-700 bg-gray-800/50 px-3 py-2">
                      <p className="text-xs text-gray-400 font-medium mb-1.5">Merge preview</p>
                      <div className="grid grid-cols-3 gap-2 text-center">
                        <div>
                          <p className="text-lg font-semibold text-gray-100">
                            {preview.events_moved ?? 0}
                          </p>
                          <p className="text-[10px] text-gray-500">events</p>
                        </div>
                        <div>
                          <p className="text-lg font-semibold text-gray-100">
                            {preview.facts_moved ?? 0}
                          </p>
                          <p className="text-[10px] text-gray-500">facts</p>
                        </div>
                        <div>
                          <p className="text-lg font-semibold text-gray-100">
                            {preview.graph_edges_moved ?? 0}
                          </p>
                          <p className="text-[10px] text-gray-500">edges</p>
                        </div>
                      </div>
                    </div>
                  )}

                  {preview && !preview.success && (
                    <div className="rounded border border-red-500/30 bg-red-500/5 px-3 py-2">
                      <p className="text-xs text-red-400">{preview.error ?? 'Preview failed'}</p>
                    </div>
                  )}

                  {/* Warning + confirm */}
                  <div className="flex items-start gap-2 text-xs text-amber-400">
                    <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                    <p>
                      This action is irreversible. All data from{' '}
                      <span className="font-medium">{removeName}</span> will be transferred and the
                      entity will be deleted.
                    </p>
                  </div>
                </>
              )}

              {/* Error */}
              {merge.isError && (
                <div className="rounded border border-red-500/30 bg-red-500/5 px-3 py-2">
                  <p className="text-xs text-red-400">
                    {merge.error instanceof Error ? merge.error.message : 'Merge failed'}
                  </p>
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        {!mergeSucceeded && (
          <div className="flex justify-end gap-2 px-4 py-3 border-t border-gray-700">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm rounded border border-gray-700 text-gray-300 hover:bg-gray-800 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirm}
              disabled={!selectedTarget || !preview?.success || confirmed}
              className={cn(
                'px-3 py-1.5 text-sm rounded font-medium transition-colors flex items-center gap-1.5',
                selectedTarget && preview?.success && !confirmed
                  ? 'bg-red-600 hover:bg-red-500 text-white'
                  : 'bg-gray-800 text-gray-500 cursor-not-allowed',
              )}
            >
              {confirmed && <Loader2 className="h-3 w-3 animate-spin" />}
              Confirm Merge
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
