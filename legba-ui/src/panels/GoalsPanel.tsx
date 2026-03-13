import { useState, useMemo } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useGoals } from '@/api/hooks'
import { api } from '@/api/client'
import { Badge } from '@/components/common/Badge'
import type { Goal } from '@/api/types'

const STATUSES = ['active', 'completed', 'paused', 'abandoned']

function statusColor(status: string) {
  switch (status) {
    case 'active': return 'bg-blue-500/20 text-blue-400'
    case 'completed': return 'bg-green-500/20 text-green-400'
    case 'paused': return 'bg-amber-500/20 text-amber-400'
    case 'failed': return 'bg-red-500/20 text-red-400'
    case 'abandoned': return 'bg-red-500/20 text-red-400'
    default: return 'bg-secondary text-muted-foreground'
  }
}

function filterGoalTree(goals: Goal[], status: string): Goal[] {
  return goals
    .map((goal) => {
      const filteredChildren = filterGoalTree(goal.children ?? [], status)
      if (goal.status === status || filteredChildren.length > 0) {
        return { ...goal, children: filteredChildren }
      }
      return null
    })
    .filter((g): g is Goal => g !== null)
}

function GoalNode({ goal, depth = 0 }: { goal: Goal; depth?: number }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [editStatus, setEditStatus] = useState(goal.status)
  const [editPriority, setEditPriority] = useState(goal.priority)

  const updateMutation = useMutation({
    mutationFn: (payload: { status: string; priority: number }) =>
      api.put(`/api/v2/goals/${goal.goal_id}`, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['goals'] })
      setEditing(false)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => api.delete(`/api/v2/goals/${goal.goal_id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['goals'] }),
  })

  const handleSave = () => {
    updateMutation.mutate({ status: editStatus, priority: editPriority })
  }

  const handleDelete = () => {
    if (window.confirm(`Delete goal "${goal.description.slice(0, 60)}"?`)) {
      deleteMutation.mutate()
    }
  }

  return (
    <div>
      <div
        className="flex items-center gap-2 px-3 py-2 hover:bg-secondary/50 border-b border-border/30 group"
        style={{ paddingLeft: `${12 + depth * 20}px` }}
      >
        <Badge className={`text-[10px] ${statusColor(goal.status)}`}>{goal.status}</Badge>
        <span className="flex-1 text-sm">{goal.description}</span>
        <div className="flex items-center gap-2 shrink-0">
          <div className="w-20 h-1.5 bg-secondary rounded-full overflow-hidden">
            <div
              className="h-full bg-primary rounded-full transition-all"
              style={{ width: `${goal.progress_pct}%` }}
            />
          </div>
          <span className="text-xs text-muted-foreground w-8 text-right">{goal.progress_pct}%</span>

          {/* Edit button */}
          <button
            onClick={() => { setEditing(!editing); setEditStatus(goal.status); setEditPriority(goal.priority) }}
            className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-foreground text-xs px-1 transition-opacity"
            title="Edit"
          >
            &#9998;
          </button>

          {/* Delete button */}
          <button
            onClick={handleDelete}
            disabled={deleteMutation.isPending}
            className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-red-400 text-xs px-1 transition-opacity"
            title="Delete"
          >
            &#10005;
          </button>
        </div>
      </div>

      {/* Inline edit row */}
      {editing && (
        <div
          className="flex items-center gap-2 px-3 py-1.5 bg-secondary/30 border-b border-border/30"
          style={{ paddingLeft: `${12 + depth * 20 + 20}px` }}
        >
          <label className="text-xs text-muted-foreground">Status</label>
          <select
            value={editStatus}
            onChange={(e) => setEditStatus(e.target.value)}
            className="text-xs bg-secondary border border-border rounded px-1.5 py-0.5 focus:outline-none"
          >
            {STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <label className="text-xs text-muted-foreground ml-2">Priority</label>
          <input
            type="number"
            value={editPriority}
            onChange={(e) => setEditPriority(Number(e.target.value))}
            className="text-xs bg-secondary border border-border rounded px-1.5 py-0.5 w-14 focus:outline-none"
            min={0}
            max={100}
          />
          <button
            onClick={handleSave}
            disabled={updateMutation.isPending}
            className="text-xs bg-primary/20 text-primary hover:bg-primary/30 rounded px-2 py-0.5 transition-colors"
          >
            {updateMutation.isPending ? '...' : 'Save'}
          </button>
          <button
            onClick={() => setEditing(false)}
            className="text-xs text-muted-foreground hover:text-foreground px-1"
          >
            Cancel
          </button>
          {updateMutation.isError && (
            <span className="text-xs text-red-400">Failed</span>
          )}
        </div>
      )}

      {goal.children?.map((child) => (
        <GoalNode key={child.goal_id} goal={child} depth={depth + 1} />
      ))}
    </div>
  )
}

export function GoalsPanel() {
  const { data, isLoading } = useGoals()
  const [statusFilter, setStatusFilter] = useState('')

  const filtered = useMemo(() => {
    if (!data) return []
    if (!statusFilter) return data
    return filterGoalTree(data, statusFilter)
  }, [data, statusFilter])

  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>

  return (
    <div className="flex flex-col h-full">
      {/* Status filter */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="text-sm bg-secondary border border-border rounded px-2 py-1 focus:outline-none"
        >
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      {/* Goal tree */}
      <div className="flex-1 overflow-auto">
        {!filtered.length ? (
          <div className="p-4 text-sm text-muted-foreground">No goals found</div>
        ) : (
          filtered.map((goal) => (
            <GoalNode key={goal.goal_id} goal={goal} />
          ))
        )}
      </div>
    </div>
  )
}
