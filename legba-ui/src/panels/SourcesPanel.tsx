import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useSources } from '@/api/hooks'
import { api } from '@/api/client'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Search, ChevronLeft, ChevronRight, Plus, Pencil, Trash2, Check, X, Circle } from 'lucide-react'

const PAGE_SIZE = 50

const SOURCE_TYPES = ['rss', 'api', 'telegram', 'geojson', 'csv', 'static_json'] as const

function healthColor(s: { fetch_count: number; fail_count: number }) {
  if (s.fetch_count === 0) return 'text-muted-foreground'
  const rate = s.fail_count / s.fetch_count
  if (rate > 0.5) return 'text-red-400'
  if (rate > 0.2) return 'text-amber-400'
  return 'text-green-400'
}

/** Health dot: green=active+recent, yellow=active+stale, red=error, gray=paused */
function HealthDot({ source }: { source: { status: string; last_fetched: string | null; fetch_count: number; fail_count: number } }) {
  let color = 'text-gray-500'  // paused / unknown
  let title = 'Paused'

  if (source.status === 'failed') {
    color = 'text-red-500'
    title = 'Error'
  } else if (source.status === 'active') {
    if (!source.last_fetched) {
      color = 'text-yellow-500'
      title = 'Active - never fetched'
    } else {
      const lastMs = new Date(source.last_fetched).getTime()
      const ageHours = (Date.now() - lastMs) / (1000 * 60 * 60)
      // Recent = fetched within 6 hours
      if (source.fail_count > 0 && source.fail_count / Math.max(source.fetch_count, 1) > 0.5) {
        color = 'text-red-500'
        title = `Error - ${source.fail_count} failures`
      } else if (ageHours <= 6) {
        color = 'text-green-500'
        title = 'Active - recent fetch'
      } else {
        color = 'text-yellow-500'
        title = `Active - stale (${Math.round(ageHours)}h ago)`
      }
    }
  }

  return <span title={title}><Circle size={8} className={`${color} fill-current`} /></span>
}

export function SourcesPanel() {
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [showAddForm, setShowAddForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editFields, setEditFields] = useState<{ name: string; url: string; status: string }>({ name: '', url: '', status: '' })
  const [addFields, setAddFields] = useState<{ name: string; url: string; source_type: string }>({ name: '', url: '', source_type: 'rss' })

  const queryClient = useQueryClient()
  const { data, isLoading } = useSources({ offset, limit: PAGE_SIZE, q: search || undefined, status: status || undefined })

  // Client-side type filter (API does not support type filter natively)
  const filteredItems = data?.items
    ? typeFilter
      ? data.items.filter((s) => s.source_type === typeFilter)
      : data.items
    : []

  const createMutation = useMutation({
    mutationFn: (body: { name: string; url: string; source_type: string }) =>
      api.post('/api/v2/sources', body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      setShowAddForm(false)
      setAddFields({ name: '', url: '', source_type: 'rss' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ sourceId, body }: { sourceId: string; body: Record<string, string> }) =>
      api.put(`/api/v2/sources/${sourceId}`, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      setEditingId(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (sourceId: string) => api.delete(`/api/v2/sources/${sourceId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
    },
  })

  function startEditing(source: { source_id: string; name: string; url: string; status: string }) {
    setEditingId(source.source_id)
    setEditFields({ name: source.name, url: source.url, status: source.status })
  }

  function saveEdit(sourceId: string) {
    updateMutation.mutate({ sourceId, body: editFields })
  }

  function handleDelete(sourceId: string, name: string) {
    if (confirm(`Delete source "${name}"?`)) {
      deleteMutation.mutate(sourceId)
    }
  }

  function handleAddSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!addFields.name.trim() || !addFields.url.trim()) return
    createMutation.mutate(addFields)
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search sources..."
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOffset(0) }}
            className="w-full pl-7 pr-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <select
          value={status}
          onChange={(e) => { setStatus(e.target.value); setOffset(0) }}
          className="text-sm bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
          <option value="failed">Failed</option>
        </select>
        <select
          value={typeFilter}
          onChange={(e) => { setTypeFilter(e.target.value); setOffset(0) }}
          className="text-sm bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
        >
          <option value="">All types</option>
          {SOURCE_TYPES.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <button
          onClick={() => setShowAddForm(!showAddForm)}
          className="flex items-center gap-1 px-2 py-1 text-xs bg-primary/20 text-primary border border-primary/30 rounded hover:bg-primary/30 transition-colors"
          title="Add source"
        >
          <Plus size={14} />
          <span>Add</span>
        </button>
      </div>

      {showAddForm && (
        <form onSubmit={handleAddSubmit} className="flex items-center gap-2 px-3 py-2 border-b border-border bg-secondary/50 shrink-0">
          <input
            type="text"
            placeholder="Name"
            value={addFields.name}
            onChange={(e) => setAddFields({ ...addFields, name: e.target.value })}
            className="flex-1 min-w-0 px-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
            autoFocus
          />
          <input
            type="text"
            placeholder="URL"
            value={addFields.url}
            onChange={(e) => setAddFields({ ...addFields, url: e.target.value })}
            className="flex-[2] min-w-0 px-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <select
            value={addFields.source_type}
            onChange={(e) => setAddFields({ ...addFields, source_type: e.target.value })}
            className="text-sm bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
          >
            {SOURCE_TYPES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={createMutation.isPending || !addFields.name.trim() || !addFields.url.trim()}
            className="p-1 text-green-400 hover:text-green-300 disabled:opacity-30"
            title="Save"
          >
            <Check size={16} />
          </button>
          <button
            type="button"
            onClick={() => { setShowAddForm(false); setAddFields({ name: '', url: '', source_type: 'rss' }) }}
            className="p-1 text-muted-foreground hover:text-foreground"
            title="Cancel"
          >
            <X size={16} />
          </button>
        </form>
      )}

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">Loading...</div>
        ) : !filteredItems.length ? (
          <div className="p-4 text-sm text-muted-foreground">No sources found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-card border-b border-border">
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-2 py-2 font-medium w-6"></th>
                <th className="px-2 py-2 font-medium">Name</th>
                <th className="px-2 py-2 font-medium">Type</th>
                <th className="px-2 py-2 font-medium">Status</th>
                <th className="px-2 py-2 font-medium">Signals</th>
                <th className="px-2 py-2 font-medium">Health</th>
                <th className="px-2 py-2 font-medium">Last Fetch</th>
                <th className="px-2 py-2 font-medium w-16"></th>
              </tr>
            </thead>
            <tbody>
              {filteredItems.map((source) => (
                <tr key={source.source_id} className="border-b border-border/50 hover:bg-secondary/50">
                  {editingId === source.source_id ? (
                    <>
                      <td className="px-2 py-2">
                        <HealthDot source={source} />
                      </td>
                      <td className="px-2 py-1">
                        <input
                          type="text"
                          value={editFields.name}
                          onChange={(e) => setEditFields({ ...editFields, name: e.target.value })}
                          className="w-full px-1.5 py-0.5 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary"
                        />
                      </td>
                      <td className="px-2 py-2">
                        <Badge className="bg-secondary text-[10px]">{source.source_type}</Badge>
                      </td>
                      <td className="px-2 py-1">
                        <select
                          value={editFields.status}
                          onChange={(e) => setEditFields({ ...editFields, status: e.target.value })}
                          className="text-xs bg-secondary border border-border rounded px-1.5 py-0.5 focus:outline-none"
                        >
                          <option value="active">active</option>
                          <option value="paused">paused</option>
                          <option value="failed">failed</option>
                        </select>
                      </td>
                      <td className="px-2 py-2 text-muted-foreground">{source.event_count}</td>
                      <td className={`px-2 py-2 ${healthColor(source)}`}>
                        {source.fetch_count > 0 ? `${source.fetch_count - source.fail_count}/${source.fetch_count}` : '-'}
                      </td>
                      <td className="px-2 py-2">
                        {source.last_fetched ? <TimeAgo date={source.last_fetched} className="text-xs text-muted-foreground" /> : '-'}
                      </td>
                      <td className="px-2 py-1">
                        <div className="flex items-center gap-0.5">
                          <button
                            onClick={() => saveEdit(source.source_id)}
                            disabled={updateMutation.isPending}
                            className="p-1 text-green-400 hover:text-green-300 disabled:opacity-30"
                            title="Save"
                          >
                            <Check size={14} />
                          </button>
                          <button
                            onClick={() => setEditingId(null)}
                            className="p-1 text-muted-foreground hover:text-foreground"
                            title="Cancel"
                          >
                            <X size={14} />
                          </button>
                        </div>
                      </td>
                    </>
                  ) : (
                    <>
                      <td className="px-2 py-2">
                        <HealthDot source={source} />
                      </td>
                      <td className="px-2 py-2 truncate max-w-[200px]" title={source.url}>
                        {source.name}
                      </td>
                      <td className="px-2 py-2">
                        <Badge className="bg-secondary text-[10px]">{source.source_type}</Badge>
                      </td>
                      <td className="px-2 py-2">
                        <Badge className={
                          source.status === 'active' ? 'bg-green-500/20 text-green-400'
                          : source.status === 'failed' ? 'bg-red-500/20 text-red-400'
                          : 'bg-secondary text-muted-foreground'
                        }>
                          {source.status}
                        </Badge>
                      </td>
                      <td className="px-2 py-2 text-muted-foreground">{source.event_count}</td>
                      <td className={`px-2 py-2 ${healthColor(source)}`}>
                        {source.fetch_count > 0 ? `${source.fetch_count - source.fail_count}/${source.fetch_count}` : '-'}
                      </td>
                      <td className="px-2 py-2">
                        {source.last_fetched ? <TimeAgo date={source.last_fetched} className="text-xs text-muted-foreground" /> : <span className="text-xs text-muted-foreground">-</span>}
                      </td>
                      <td className="px-2 py-1">
                        <div className="flex items-center gap-0.5">
                          <button
                            onClick={() => startEditing(source)}
                            className="p-1 text-muted-foreground hover:text-foreground"
                            title="Edit"
                          >
                            <Pencil size={13} />
                          </button>
                          <button
                            onClick={() => handleDelete(source.source_id, source.name)}
                            disabled={deleteMutation.isPending}
                            className="p-1 text-muted-foreground hover:text-red-400 disabled:opacity-30"
                            title="Delete"
                          >
                            <Trash2 size={13} />
                          </button>
                        </div>
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {data && data.total > PAGE_SIZE && (
        <div className="flex items-center justify-between px-3 py-1.5 border-t border-border text-xs text-muted-foreground shrink-0">
          <span>
            {typeFilter ? `${filteredItems.length} of ` : ''}{data.total} sources
          </span>
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
