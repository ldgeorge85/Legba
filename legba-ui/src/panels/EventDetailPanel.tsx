import { useEvent } from '@/api/hooks'
import { useSelectionStore } from '@/stores/selection'
import { useWorkspaceStore } from '@/stores/workspace'
import { cn, categoryColor, entityTypeColor } from '@/lib/utils'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'

interface Props {
  eventId: string | null
}

export function EventDetailPanel({ eventId: propId }: Props) {
  const selected = useSelectionStore((s) => s.selected)
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  const id = propId ?? (selected?.type === 'event' ? selected.id : null)
  const { data, isLoading } = useEvent(id)

  if (!id) {
    return <div className="p-4 text-sm text-muted-foreground">Select an event to view details</div>
  }
  if (isLoading) {
    return <div className="p-4 text-sm text-muted-foreground">Loading...</div>
  }
  if (!data) {
    return <div className="p-4 text-sm text-muted-foreground">Event not found</div>
  }

  return (
    <div className="p-4 space-y-4 max-w-3xl">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Badge className={cn(categoryColor(data.category))}>{data.category}</Badge>
          <span className="text-xs text-muted-foreground font-mono">{data.event_id.slice(0, 8)}</span>
        </div>
        <h2 className="text-lg font-semibold">{data.title}</h2>
        <TimeAgo date={data.timestamp} className="text-xs text-muted-foreground" />
      </div>

      {/* Description */}
      {data.description && (
        <div className="text-sm text-foreground/90 leading-relaxed">{data.description}</div>
      )}

      {/* Metadata */}
      <div className="grid grid-cols-2 gap-2 text-sm">
        <div>
          <span className="text-muted-foreground">Confidence:</span>{' '}
          <span>{Math.round(data.confidence * 100)}%</span>
        </div>
        {data.source_name && (
          <div>
            <span className="text-muted-foreground">Source:</span> <span>{data.source_name}</span>
          </div>
        )}
        {data.source_url && (
          <div className="col-span-2">
            <span className="text-muted-foreground">URL:</span>{' '}
            <a href={data.source_url} target="_blank" rel="noopener" className="text-primary hover:underline break-all">
              {data.source_url}
            </a>
          </div>
        )}
      </div>

      {/* Tags */}
      {data.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {data.tags.map((tag) => (
            <Badge key={tag} className="bg-secondary text-secondary-foreground text-[10px]">{tag}</Badge>
          ))}
        </div>
      )}

      {/* Linked Entities */}
      {data.entities.length > 0 && (
        <div>
          <h3 className="text-sm font-medium mb-1">Linked Entities</h3>
          <div className="space-y-1">
            {data.entities.map((ent) => (
              <div
                key={ent.entity_id}
                className="flex items-center gap-2 px-2 py-1 rounded hover:bg-secondary cursor-pointer text-sm"
                onClick={() => {
                  select({ type: 'entity', id: ent.entity_id, name: ent.name })
                  openPanel('entity-detail', { id: ent.entity_id })
                }}
              >
                <Badge className={cn('text-[10px]', entityTypeColor(ent.entity_type))}>
                  {ent.entity_type}
                </Badge>
                <span>{ent.name}</span>
                {ent.role && <span className="text-xs text-muted-foreground">({ent.role})</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Supporting Signals */}
      <div>
        <h3 className="text-sm font-medium mb-1">Supporting Signals</h3>
        {(!data.linked_signals || data.linked_signals.length === 0) ? (
          <p className="text-sm text-muted-foreground">No linked signals</p>
        ) : (
          <div className="space-y-1">
            {data.linked_signals.map((sig, i) => (
              <div
                key={i}
                className="flex items-center gap-2 px-2 py-1.5 rounded bg-secondary/40 text-sm"
              >
                <Badge className={cn('text-[10px]', categoryColor(sig.category))}>
                  {sig.category}
                </Badge>
                <span className="flex-1 truncate">{sig.title}</span>
                <span className="text-[10px] text-muted-foreground shrink-0">
                  {Math.round(sig.confidence * 100)}%
                </span>
                <TimeAgo date={sig.timestamp} className="text-[10px] text-muted-foreground shrink-0" />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
