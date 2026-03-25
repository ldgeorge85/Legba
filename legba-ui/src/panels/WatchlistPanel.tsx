import { useState, useCallback } from 'react'
import { useWatchlist, useWatchTriggers, useCreateWatch, useUpdateWatch, useDeleteWatch } from '@/api/hooks'
import { TimeAgo } from '@/components/common/TimeAgo'
import { Badge } from '@/components/common/Badge'
import { EntityLink } from '@/components/EntityLink'
import { Eye, Bell, Plus, Pencil, Trash2, X, Check } from 'lucide-react'

function priorityColor(priority: string) {
  switch (priority) {
    case 'critical': return 'bg-red-500/20 text-red-400'
    case 'high': return 'bg-orange-500/20 text-orange-400'
    case 'medium': return 'bg-amber-500/20 text-amber-400'
    case 'low': return 'bg-green-500/20 text-green-400'
    default: return 'bg-secondary text-muted-foreground'
  }
}

const inputClass = 'w-full px-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary'
const btnClass = 'p-1 rounded hover:bg-secondary text-muted-foreground hover:text-foreground transition-colors'

const KNOWN_CATEGORIES = [
  'conflict',
  'political',
  'economic',
  'health',
  'environment',
  'technology',
  'disaster',
  'social',
]

const PRIORITY_OPTIONS = ['critical', 'high', 'normal', 'low'] as const

/** Chip input: renders existing chips + a text input to add new ones */
function ChipInput({
  label,
  values,
  onChange,
  placeholder,
}: {
  label: string
  values: string[]
  onChange: (vals: string[]) => void
  placeholder?: string
}) {
  const [inputVal, setInputVal] = useState('')

  const addChip = useCallback(() => {
    const trimmed = inputVal.trim()
    if (trimmed && !values.includes(trimmed)) {
      onChange([...values, trimmed])
    }
    setInputVal('')
  }, [inputVal, values, onChange])

  const removeChip = useCallback(
    (val: string) => {
      onChange(values.filter((v) => v !== val))
    },
    [values, onChange]
  )

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      addChip()
    } else if (e.key === 'Backspace' && inputVal === '' && values.length > 0) {
      removeChip(values[values.length - 1])
    }
  }

  return (
    <div>
      <label className="text-[10px] text-muted-foreground uppercase font-medium">{label}</label>
      <div className="flex flex-wrap items-center gap-1 p-1 min-h-[32px] bg-secondary border border-border rounded mt-0.5">
        {values.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 text-xs bg-primary/15 text-primary border border-primary/20 rounded"
          >
            {v}
            <button
              type="button"
              onClick={() => removeChip(v)}
              className="hover:text-destructive transition-colors"
            >
              <X size={10} />
            </button>
          </span>
        ))}
        <input
          type="text"
          value={inputVal}
          onChange={(e) => setInputVal(e.target.value)}
          onKeyDown={handleKeyDown}
          onBlur={() => { if (inputVal.trim()) addChip() }}
          placeholder={values.length === 0 ? (placeholder ?? `Add ${label.toLowerCase()}...`) : ''}
          className="flex-1 min-w-[80px] px-1 py-0.5 text-sm bg-transparent border-none outline-none"
        />
      </div>
    </div>
  )
}

/** Category checkbox group */
function CategoryCheckboxGroup({
  selected,
  onChange,
}: {
  selected: string[]
  onChange: (vals: string[]) => void
}) {
  const toggle = (cat: string) => {
    if (selected.includes(cat)) {
      onChange(selected.filter((c) => c !== cat))
    } else {
      onChange([...selected, cat])
    }
  }

  return (
    <div>
      <label className="text-[10px] text-muted-foreground uppercase font-medium">Categories</label>
      <div className="grid grid-cols-2 gap-1 mt-0.5">
        {KNOWN_CATEGORIES.map((cat) => (
          <label
            key={cat}
            className="flex items-center gap-1.5 px-2 py-1 text-xs rounded hover:bg-secondary/80 cursor-pointer"
          >
            <input
              type="checkbox"
              checked={selected.includes(cat)}
              onChange={() => toggle(cat)}
              className="rounded border-border bg-secondary accent-primary w-3 h-3"
            />
            <span className="capitalize">{cat}</span>
          </label>
        ))}
      </div>
    </div>
  )
}

interface StructuredFormData {
  name: string
  description: string
  entities: string[]
  keywords: string[]
  categories: string[]
  regions: string[]
  priority: string
}

const emptyStructuredForm: StructuredFormData = {
  name: '',
  description: '',
  entities: [],
  keywords: [],
  categories: [],
  regions: [],
  priority: 'normal',
}

function AddWatchForm({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState<StructuredFormData>(emptyStructuredForm)
  const createWatch = useCreateWatch()

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!form.name.trim()) return
    createWatch.mutate(
      {
        name: form.name.trim(),
        description: form.description.trim(),
        entities: form.entities,
        keywords: [...form.keywords, ...form.regions.map((r) => `region:${r}`)],
        categories: form.categories,
        priority: form.priority,
      },
      { onSuccess: () => onClose() },
    )
  }

  return (
    <form onSubmit={handleSubmit} className="px-3 py-2 border border-primary/30 rounded bg-card space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase text-muted-foreground">New Watch Item</span>
        <button type="button" onClick={onClose} className={btnClass}><X size={14} /></button>
      </div>
      <input
        placeholder="Name *"
        value={form.name}
        onChange={(e) => setForm({ ...form, name: e.target.value })}
        className={inputClass}
        required
      />
      <input
        placeholder="Description"
        value={form.description}
        onChange={(e) => setForm({ ...form, description: e.target.value })}
        className={inputClass}
      />

      <ChipInput
        label="Entities"
        values={form.entities}
        onChange={(entities) => setForm({ ...form, entities })}
        placeholder="Type entity name and press Enter..."
      />

      <ChipInput
        label="Keywords"
        values={form.keywords}
        onChange={(keywords) => setForm({ ...form, keywords })}
        placeholder="Type keyword and press Enter..."
      />

      <CategoryCheckboxGroup
        selected={form.categories}
        onChange={(categories) => setForm({ ...form, categories })}
      />

      <ChipInput
        label="Regions"
        values={form.regions}
        onChange={(regions) => setForm({ ...form, regions })}
        placeholder="Type region and press Enter..."
      />

      <div className="flex items-center gap-2">
        <div>
          <label className="text-[10px] text-muted-foreground uppercase font-medium">Priority</label>
          <select
            value={form.priority}
            onChange={(e) => setForm({ ...form, priority: e.target.value })}
            className={`${inputClass} w-auto mt-0.5`}
          >
            {PRIORITY_OPTIONS.map((p) => (
              <option key={p} value={p}>
                {p.charAt(0).toUpperCase() + p.slice(1)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex-1" />
        <button type="button" onClick={onClose} className="px-2 py-1 text-xs text-muted-foreground hover:text-foreground">Cancel</button>
        <button type="submit" disabled={createWatch.isPending || !form.name.trim()} className="px-3 py-1 text-xs bg-primary text-primary-foreground rounded hover:bg-primary/80 disabled:opacity-50">
          {createWatch.isPending ? 'Adding...' : 'Add'}
        </button>
      </div>
    </form>
  )
}

interface EditState {
  name: string
  description: string
  priority: string
}

function WatchItemCard({ w }: { w: { watch_id: string; entity_name: string; watch_type: string; description: string | null; entities: string[]; keywords: string[]; categories: string[]; trigger_count: number } }) {
  const [editing, setEditing] = useState(false)
  const [editForm, setEditForm] = useState<EditState>({ name: '', description: '', priority: '' })
  const [confirmDelete, setConfirmDelete] = useState(false)
  const updateWatch = useUpdateWatch()
  const deleteWatch = useDeleteWatch()

  const startEdit = () => {
    setEditForm({ name: w.entity_name, description: w.description ?? '', priority: w.watch_type })
    setEditing(true)
    setConfirmDelete(false)
  }

  const saveEdit = () => {
    updateWatch.mutate(
      { watchId: w.watch_id, body: { name: editForm.name.trim(), description: editForm.description.trim(), priority: editForm.priority } },
      { onSuccess: () => setEditing(false) },
    )
  }

  const handleDelete = () => {
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    deleteWatch.mutate(w.watch_id)
  }

  if (editing) {
    return (
      <div className="px-3 py-2 rounded border border-primary/30 space-y-2">
        <input value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} className={inputClass} placeholder="Name" />
        <input value={editForm.description} onChange={(e) => setEditForm({ ...editForm, description: e.target.value })} className={inputClass} placeholder="Description" />
        <div className="flex items-center gap-2">
          <select value={editForm.priority} onChange={(e) => setEditForm({ ...editForm, priority: e.target.value })} className={`${inputClass} w-auto`}>
            {PRIORITY_OPTIONS.map((p) => (
              <option key={p} value={p}>
                {p.charAt(0).toUpperCase() + p.slice(1)}
              </option>
            ))}
          </select>
          <div className="flex-1" />
          <button onClick={() => setEditing(false)} className="px-2 py-1 text-xs text-muted-foreground hover:text-foreground">Cancel</button>
          <button onClick={saveEdit} disabled={updateWatch.isPending || !editForm.name.trim()} className="px-3 py-1 text-xs bg-primary text-primary-foreground rounded hover:bg-primary/80 disabled:opacity-50">
            {updateWatch.isPending ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="px-3 py-2 rounded hover:bg-secondary border border-border/50 space-y-1.5 group">
      <div className="flex items-center gap-2">
        <Eye size={14} className="text-primary shrink-0" />
        <span className="flex-1 text-sm font-medium truncate">{w.entity_name}</span>
        <Badge className={`text-[10px] ${priorityColor(w.watch_type)}`}>{w.watch_type}</Badge>
        {w.trigger_count > 0 && (
          <span className="text-[10px] text-amber-400 font-mono">{w.trigger_count} triggers</span>
        )}
        <div className="hidden group-hover:flex items-center gap-0.5">
          <button onClick={startEdit} className={btnClass} title="Edit"><Pencil size={12} /></button>
          <button onClick={handleDelete} className={`${btnClass} ${confirmDelete ? 'text-red-400 hover:text-red-300' : ''}`} title={confirmDelete ? 'Click again to confirm' : 'Delete'}>
            {confirmDelete ? <Check size={12} /> : <Trash2 size={12} />}
          </button>
          {confirmDelete && (
            <button onClick={() => setConfirmDelete(false)} className={btnClass} title="Cancel delete"><X size={12} /></button>
          )}
        </div>
      </div>
      {w.description && (
        <p className="text-xs text-muted-foreground pl-[22px]">{w.description}</p>
      )}
      {(w.entities ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1 pl-[22px]">
          <span className="text-[10px] text-muted-foreground">Entities:</span>
          {w.entities.map((e) => (
            <Badge key={e} className="text-[10px] bg-blue-500/10">
              <EntityLink name={e} className="text-blue-400 text-[10px]" />
            </Badge>
          ))}
        </div>
      )}
      {(w.keywords ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1 pl-[22px]">
          {w.keywords.map((kw) => (
            <Badge key={kw} className="text-[10px] bg-purple-500/10 text-purple-400">{kw}</Badge>
          ))}
        </div>
      )}
      {(w.categories ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1 pl-[22px]">
          {w.categories.map((cat) => (
            <Badge key={cat} className="text-[10px] bg-secondary text-muted-foreground">{cat}</Badge>
          ))}
        </div>
      )}
    </div>
  )
}

export function WatchlistPanel() {
  const { data: watches, isLoading } = useWatchlist()
  const { data: triggers } = useWatchTriggers()
  const [showAdd, setShowAdd] = useState(false)

  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>

  return (
    <div className="h-full overflow-auto p-2 space-y-4">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-2">
        <h3 className="text-xs font-semibold uppercase text-muted-foreground">Active Watches ({watches?.length ?? 0})</h3>
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="flex items-center gap-1 px-2 py-1 text-xs bg-primary text-primary-foreground rounded hover:bg-primary/80"
        >
          <Plus size={12} />
          Add Watch
        </button>
      </div>

      {/* Add form */}
      {showAdd && <AddWatchForm onClose={() => setShowAdd(false)} />}

      {/* Active Watches */}
      <div>
        {!watches?.length ? (
          <p className="text-sm text-muted-foreground px-2">No active watches</p>
        ) : (
          <div className="space-y-1">
            {watches.map((w) => (
              <WatchItemCard key={w.watch_id} w={w} />
            ))}
          </div>
        )}
      </div>

      {/* Recent Triggers */}
      {triggers && triggers.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold uppercase text-muted-foreground px-2 mb-1">Recent Triggers</h3>
          <div className="space-y-1">
            {triggers.map((t, i) => (
              <div key={i} className="flex items-center gap-2 px-3 py-2 rounded bg-amber-500/5 border border-amber-500/20">
                <Bell size={14} className="text-amber-400 shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm">{t.entity_name}: {t.details}</p>
                  <TimeAgo date={t.triggered_at} className="text-xs text-muted-foreground" />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
