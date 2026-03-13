import { useEntity } from '@/api/hooks'
import { useSelectionStore } from '@/stores/selection'
import { cn, entityTypeColor, formatConfidence } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'

interface Props {
  entityId: string | null
}

export function EntityDetailPanel({ entityId: propId }: Props) {
  const selected = useSelectionStore((s) => s.selected)
  const id = propId ?? (selected?.type === 'entity' ? selected.id : null)
  const { data, isLoading } = useEntity(id)

  if (!id) return <div className="p-4 text-sm text-muted-foreground">Select an entity to view details</div>
  if (isLoading) return <div className="p-4 text-sm text-muted-foreground">Loading...</div>
  if (!data) return <div className="p-4 text-sm text-muted-foreground">Entity not found</div>

  return (
    <div className="p-4 space-y-4 max-w-3xl">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Badge className={cn(entityTypeColor(data.entity_type))}>{data.entity_type}</Badge>
          <span className="text-xs text-muted-foreground font-mono">{data.entity_id.slice(0, 8)}</span>
        </div>
        <h2 className="text-lg font-semibold">{data.name}</h2>
        {data.aliases.length > 0 && (
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

      {data.assertions.length > 0 && (
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

      {data.relationships.length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-1">Relationships ({data.relationships.length})</h3>
          <div className="space-y-1">
            {data.relationships.map((r, i) => (
              <div key={i} className="flex items-center gap-2 px-2 py-1 rounded bg-secondary/50 text-sm">
                <span>{r.source}</span>
                <span className="text-primary font-mono text-xs">{r.rel_type}</span>
                <span>{r.target}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
