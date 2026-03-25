import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { TimeAgo } from '@/components/common/TimeAgo'
import { EntityLink } from '@/components/EntityLink'
import { Check, X, ArrowRight, Loader2, GitPullRequest } from 'lucide-react'
import { cn } from '@/lib/utils'

interface ProposedEdge {
  id: string
  source_entity: string
  target_entity: string
  relationship_type: string
  confidence: number
  evidence_text: string
  source_cycle: number | null
  status: string
  reviewed_at: string | null
  created_at: string | null
}

function useProposedEdges(status: string) {
  return useQuery({
    queryKey: ['proposed-edges', status],
    queryFn: () => api.get<{ items: ProposedEdge[]; total: number }>(
      `/api/v2/proposed-edges?status=${status}&limit=100`
    ),
    refetchInterval: 30_000,
  })
}

const REL_COLORS: Record<string, string> = {
  HostileTo: 'text-red-400',
  AlliedWith: 'text-green-400',
  SuppliesWeaponsTo: 'text-orange-400',
  SanctionedBy: 'text-yellow-400',
  LeaderOf: 'text-blue-400',
  MemberOf: 'text-cyan-400',
  OperatesIn: 'text-purple-400',
  TradesWith: 'text-emerald-400',
}

export function ProposedEdgesPanel() {
  const [tab, setTab] = useState<'pending' | 'approved' | 'rejected'>('pending')
  const queryClient = useQueryClient()
  const { data, isLoading } = useProposedEdges(tab)

  const reviewMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      api.post(`/api/v2/proposed-edges/${id}/review`, { action }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['proposed-edges'] })
    },
  })

  const items = data?.items ?? []

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-border shrink-0">
        <GitPullRequest size={16} className="text-primary" />
        <h2 className="text-sm font-medium">Proposed Edges</h2>
        <span className="text-xs text-muted-foreground ml-auto">
          {items.length} {tab}
        </span>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border shrink-0">
        {(['pending', 'approved', 'rejected'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cn(
              'flex-1 px-3 py-1.5 text-xs font-medium transition-colors',
              tab === t
                ? 'text-primary border-b-2 border-primary'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="animate-spin text-muted-foreground" size={20} />
          </div>
        ) : items.length === 0 ? (
          <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
            No {tab} edges
          </div>
        ) : (
          <div className="divide-y divide-border">
            {items.map((edge) => (
              <div
                key={edge.id}
                className="px-4 py-3 hover:bg-secondary/30 transition-colors"
              >
                {/* Relationship line */}
                <div className="flex items-center gap-2 text-sm">
                  <EntityLink name={edge.source_entity} className="font-medium" />
                  <ArrowRight size={12} className="text-muted-foreground shrink-0" />
                  <span className={cn('text-xs font-mono', REL_COLORS[edge.relationship_type] ?? 'text-muted-foreground')}>
                    {edge.relationship_type}
                  </span>
                  <ArrowRight size={12} className="text-muted-foreground shrink-0" />
                  <EntityLink name={edge.target_entity} className="font-medium" />
                </div>

                {/* Evidence */}
                {edge.evidence_text && (
                  <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
                    {edge.evidence_text}
                  </p>
                )}

                {/* Metadata + actions */}
                <div className="flex items-center gap-3 mt-2">
                  <span className="text-xs text-muted-foreground">
                    conf: {(edge.confidence * 100).toFixed(0)}%
                  </span>
                  {edge.source_cycle && (
                    <span className="text-xs text-muted-foreground">
                      cycle {edge.source_cycle}
                    </span>
                  )}
                  {edge.created_at && (
                    <span className="text-xs text-muted-foreground">
                      <TimeAgo date={edge.created_at} />
                    </span>
                  )}

                  {/* Action buttons for pending items */}
                  {tab === 'pending' && (
                    <div className="flex items-center gap-1 ml-auto">
                      <button
                        onClick={() => reviewMutation.mutate({ id: edge.id, action: 'approved' })}
                        disabled={reviewMutation.isPending}
                        className="flex items-center gap-1 px-2 py-0.5 text-xs rounded
                          bg-green-500/10 text-green-400 hover:bg-green-500/20 transition-colors
                          disabled:opacity-50"
                      >
                        <Check size={12} /> Approve
                      </button>
                      <button
                        onClick={() => reviewMutation.mutate({ id: edge.id, action: 'rejected' })}
                        disabled={reviewMutation.isPending}
                        className="flex items-center gap-1 px-2 py-0.5 text-xs rounded
                          bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors
                          disabled:opacity-50"
                      >
                        <X size={12} /> Reject
                      </button>
                    </div>
                  )}

                  {/* Status badge for reviewed items */}
                  {tab !== 'pending' && edge.reviewed_at && (
                    <span className="text-xs text-muted-foreground ml-auto">
                      reviewed <TimeAgo date={edge.reviewed_at} />
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
