import { useState, useMemo } from 'react'
import { useEntity, useSignals, useHypotheses, useFacts } from '@/api/hooks'
import { useSelectionStore } from '@/stores/selection'
import { cn, entityTypeColor, formatConfidence } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { EntityLink } from '@/components/EntityLink'
import { EntityMergeModal } from '@/components/EntityMergeModal'
import { Merge, ChevronDown, ChevronRight, Activity } from 'lucide-react'

interface Props {
  entityId: string | null
}

/** Collapsible section with disclosure triangle */
function CollapsibleSection({
  title,
  count,
  defaultOpen = false,
  children,
}: {
  title: string
  count?: number
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 w-full text-sm font-medium hover:text-foreground transition-colors py-1"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {title}
        {count != null && (
          <span className="text-xs text-muted-foreground font-normal ml-1">({count})</span>
        )}
      </button>
      {open && <div className="pl-1 mt-1">{children}</div>}
    </div>
  )
}

export function EntityDetailPanel({ entityId: propId }: Props) {
  const selected = useSelectionStore((s) => s.selected)
  const id = propId ?? (selected?.type === 'entity' ? selected.id : null)
  const { data, isLoading } = useEntity(id)
  const [showMergeModal, setShowMergeModal] = useState(false)

  // Recent Signals query — search by entity name
  const entityName = data?.name ?? ''
  const { data: signalsData, isLoading: signalsLoading } = useSignals({
    q: entityName || undefined,
    limit: 10,
  })

  // Hypotheses query — fetch all active, filter client-side
  const { data: hypothesesData, isLoading: hypothesesLoading } = useHypotheses('active')
  const matchingHypotheses = useMemo(() => {
    if (!hypothesesData || !entityName) return []
    const lower = entityName.toLowerCase()
    return hypothesesData.filter(
      (h) =>
        h.thesis.toLowerCase().includes(lower) ||
        h.counter_thesis.toLowerCase().includes(lower)
    )
  }, [hypothesesData, entityName])

  // Facts query — by subject
  const { data: factsData, isLoading: factsLoading } = useFacts({
    subject: entityName || undefined,
    limit: 20,
  })

  // Activity indicator: heuristic based on last_seen within 7 days
  const isRecentlyActive = useMemo(() => {
    if (!data?.last_seen) return false
    const sevenDaysAgo = Date.now() - 7 * 24 * 60 * 60 * 1000
    return new Date(data.last_seen).getTime() > sevenDaysAgo && data.event_count > 0
  }, [data?.last_seen, data?.event_count])

  if (!id) return <div className="p-4 text-sm text-muted-foreground">Select an entity to view details</div>
  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>
  if (!data) return <div className="p-4 text-sm text-muted-foreground">Entity not found</div>

  return (
    <div className="p-4 space-y-4 max-w-3xl overflow-auto h-full">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Badge className={cn(entityTypeColor(data.entity_type))}>{data.entity_type}</Badge>
          <span className="text-xs text-muted-foreground font-mono">{(data.entity_id ?? '').slice(0, 8)}</span>
          {/* Activity Indicator */}
          {isRecentlyActive ? (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium rounded bg-green-500/20 text-green-400 border border-green-500/30">
              <Activity size={10} />
              Active
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium rounded bg-gray-500/20 text-gray-400 border border-gray-500/30">
              <Activity size={10} />
              Quiet
            </span>
          )}
          <div className="flex-1" />
          <button
            onClick={() => setShowMergeModal(true)}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded border border-gray-700 text-gray-400 hover:text-gray-200 hover:bg-gray-800 transition-colors"
            title="Merge this entity into another"
          >
            <Merge className="h-3 w-3" />
            Merge Into...
          </button>
        </div>
        <h2 className="text-lg font-semibold">{data.name}</h2>
        {(data.aliases ?? []).length > 0 && (
          <p className="text-xs text-muted-foreground">aka: {data.aliases.join(', ')}</p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 text-sm">
        <div><span className="text-muted-foreground">First seen:</span> <TimeAgo date={data.first_seen} /></div>
        <div><span className="text-muted-foreground">Last seen:</span> <TimeAgo date={data.last_seen} /></div>
        <div><span className="text-muted-foreground">Events:</span> {data.event_count}</div>
        {data.completeness != null && (
          <div><span className="text-muted-foreground">Completeness:</span> {Math.round(data.completeness * 100)}%</div>
        )}
      </div>

      {(data.assertions ?? []).length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-1">Assertions ({data.assertions.length})</h3>
          <div className="space-y-1">
            {data.assertions.map((a, i) => (
              <div key={i} className="flex items-center gap-2 px-2 py-1 rounded bg-secondary/50 text-sm">
                <span className="font-medium text-muted-foreground w-32 shrink-0">{a.key}</span>
                <span className="flex-1">{a.value}</span>
                <span className="text-xs text-muted-foreground">{formatConfidence(a.confidence)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {(data.relationships ?? []).length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-1">Relationships ({data.relationships.length})</h3>
          <div className="space-y-1">
            {data.relationships.map((r, i) => (
              <div key={i} className="flex items-center gap-2 px-2 py-1 rounded bg-secondary/50 text-sm">
                <EntityLink name={r.source} />
                <span className="text-primary font-mono text-xs">{r.rel_type}</span>
                <EntityLink name={r.target} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Recent Signals */}
      <CollapsibleSection
        title="Recent Signals"
        count={signalsData?.items.length}
        defaultOpen={false}
      >
        {signalsLoading ? (
          <p className="text-xs text-muted-foreground">Loading signals...</p>
        ) : !signalsData?.items.length ? (
          <p className="text-xs text-muted-foreground">No recent signals found</p>
        ) : (
          <div className="space-y-1">
            {signalsData.items.map((s) => (
              <div
                key={s.event_id}
                className="flex items-center gap-2 px-2 py-1.5 rounded bg-secondary/50 text-sm"
              >
                <div className="flex-1 min-w-0">
                  <p className="text-sm truncate">{s.title}</p>
                  <TimeAgo date={s.timestamp} className="text-[10px] text-muted-foreground" />
                </div>
                <span className={cn(
                  'inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded border shrink-0',
                  s.confidence >= 0.8 ? 'bg-green-500/20 text-green-400 border-green-500/30' :
                  s.confidence >= 0.5 ? 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30' :
                  'bg-gray-500/20 text-gray-400 border-gray-500/30'
                )}>
                  {formatConfidence(s.confidence)}
                </span>
              </div>
            ))}
          </div>
        )}
      </CollapsibleSection>

      {/* Connected Hypotheses */}
      <CollapsibleSection
        title="Hypotheses"
        count={matchingHypotheses.length}
        defaultOpen={false}
      >
        {hypothesesLoading ? (
          <p className="text-xs text-muted-foreground">Loading hypotheses...</p>
        ) : matchingHypotheses.length === 0 ? (
          <p className="text-xs text-muted-foreground">No connected hypotheses</p>
        ) : (
          <div className="space-y-1.5">
            {matchingHypotheses.map((h) => (
              <div
                key={h.id}
                className="px-2 py-1.5 rounded bg-secondary/50 text-sm space-y-1"
              >
                <div className="flex items-start gap-2">
                  <span className="text-green-400 text-[10px] font-mono shrink-0 mt-0.5">T</span>
                  <p className="flex-1 text-xs">{h.thesis}</p>
                </div>
                <div className="flex items-start gap-2">
                  <span className="text-red-400 text-[10px] font-mono shrink-0 mt-0.5">C</span>
                  <p className="flex-1 text-xs text-muted-foreground">{h.counter_thesis}</p>
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-[10px] text-muted-foreground">
                    Balance: {h.evidence_balance > 0 ? '+' : ''}{h.evidence_balance.toFixed(1)}
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    Support: {h.support_count ?? 0} / Refute: {h.refute_count ?? 0}
                  </span>
                  <Badge className="text-[10px] bg-secondary text-muted-foreground ml-auto">{h.status}</Badge>
                </div>
              </div>
            ))}
          </div>
        )}
      </CollapsibleSection>

      {/* Knowledge Base (Facts) */}
      <CollapsibleSection
        title="Knowledge Base"
        count={factsData?.items.length}
        defaultOpen={false}
      >
        {factsLoading ? (
          <p className="text-xs text-muted-foreground">Loading facts...</p>
        ) : !factsData?.items.length ? (
          <p className="text-xs text-muted-foreground">No facts found for this entity</p>
        ) : (
          <div className="space-y-1">
            {factsData.items.map((f) => (
              <div
                key={f.fact_id}
                className="flex items-center gap-2 px-2 py-1 rounded bg-secondary/50 text-sm"
              >
                <span className="font-medium text-muted-foreground w-36 shrink-0 text-xs truncate" title={f.predicate}>
                  {f.predicate}
                </span>
                <span className="flex-1 text-xs truncate" title={f.object}>{f.object}</span>
                <span className={cn(
                  'inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded border shrink-0',
                  f.confidence >= 0.8 ? 'bg-green-500/20 text-green-400 border-green-500/30' :
                  f.confidence >= 0.5 ? 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30' :
                  'bg-gray-500/20 text-gray-400 border-gray-500/30'
                )}>
                  {formatConfidence(f.confidence)}
                </span>
              </div>
            ))}
          </div>
        )}
      </CollapsibleSection>

      {showMergeModal && (
        <EntityMergeModal
          removeId={data.entity_id}
          removeName={data.name}
          onClose={() => setShowMergeModal(false)}
        />
      )}
    </div>
  )
}
