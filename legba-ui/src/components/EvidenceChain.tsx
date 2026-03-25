import { useEffect, useMemo } from 'react'
import { X, Link2, Loader2 } from 'lucide-react'
import { useEvent, useFacts, useSituation, useHypotheses } from '@/api/hooks'
import { Badge } from '@/components/common/Badge'
import { TimeAgo } from '@/components/common/TimeAgo'
import { EntityLink } from '@/components/EntityLink'
import { useWorkspaceStore } from '@/stores/workspace'
import { useSelectionStore } from '@/stores/selection'
import { cn, categoryColor } from '@/lib/utils'
import type { Fact, HypothesisSummary } from '@/api/types'

type EntityType = 'fact' | 'event' | 'situation'

interface EvidenceChainProps {
  entityType: EntityType
  entityId: string
  /** Extra context for facts (subject name for fetching related) */
  factSubject?: string
  /** The claim label to show at top */
  label?: string
  onClose: () => void
}

// ── Confidence badge ──

function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  const color =
    pct >= 80
      ? 'bg-green-500/20 text-green-400 border-green-500/30'
      : pct >= 50
        ? 'bg-amber-500/20 text-amber-400 border-amber-500/30'
        : 'bg-red-500/20 text-red-400 border-red-500/30'
  return (
    <Badge className={cn('text-[10px]', color)}>
      {pct}%
    </Badge>
  )
}

// ── Tree connector components ──

function TreeBranch({ last, children }: { last?: boolean; children: React.ReactNode }) {
  return (
    <div className="flex">
      <div className="flex flex-col items-center w-5 shrink-0">
        <div className="w-px h-2 bg-border" />
        <div className="flex items-center">
          <div className="w-2.5 h-px bg-border" />
        </div>
        {!last && <div className="w-px flex-1 bg-border" />}
      </div>
      <div className="flex-1 min-w-0 pb-1.5">{children}</div>
    </div>
  )
}

function TreeTrunk({ children }: { children: React.ReactNode }) {
  return (
    <div className="pl-2 border-l border-border ml-2.5">{children}</div>
  )
}

// ── Evidence chain for a FACT ──

function FactChain({ fact }: { fact: Fact }) {
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  // Fetch related facts with same subject to show context
  const { data: relatedFacts } = useFacts({ subject: fact.subject, limit: 10 })

  // Fetch hypotheses to find any referencing this fact's subject
  const { data: hypotheses } = useHypotheses('active')

  const relatedHypotheses = useMemo(() => {
    if (!hypotheses) return []
    const subjectLower = fact.subject.toLowerCase()
    const objectLower = fact.object.toLowerCase()
    return hypotheses.filter(
      (h) =>
        h.thesis.toLowerCase().includes(subjectLower) ||
        h.thesis.toLowerCase().includes(objectLower) ||
        h.counter_thesis.toLowerCase().includes(subjectLower) ||
        h.counter_thesis.toLowerCase().includes(objectLower),
    )
  }, [hypotheses, fact.subject, fact.object])

  const otherFacts = useMemo(() => {
    if (!relatedFacts?.items) return []
    return relatedFacts.items.filter((f) => f.fact_id !== fact.fact_id).slice(0, 5)
  }, [relatedFacts, fact.fact_id])

  return (
    <div className="space-y-1">
      {/* Root claim */}
      <div className="flex items-center gap-2 px-3 py-2 rounded bg-primary/10 border border-primary/30">
        <span className="text-sm font-medium text-foreground">
          Claim: &quot;{fact.subject} {fact.predicate} {fact.object}&quot;
        </span>
        <ConfidenceBadge value={fact.confidence} />
      </div>

      {/* Source */}
      <TreeBranch last={otherFacts.length === 0 && relatedHypotheses.length === 0}>
        <div className="px-2 py-1.5 rounded bg-secondary/40 text-sm">
          <span className="text-muted-foreground">Source:</span>{' '}
          <span className="text-foreground">{fact.source}</span>
          <span className="text-xs text-muted-foreground ml-2">
            <TimeAgo date={fact.timestamp} />
          </span>
        </div>
      </TreeBranch>

      {/* Related facts from same subject */}
      {otherFacts.length > 0 && (
        <TreeBranch last={relatedHypotheses.length === 0}>
          <div className="text-xs text-muted-foreground font-medium mb-1">
            Related Facts ({otherFacts.length})
          </div>
          <TreeTrunk>
            {otherFacts.map((rf) => (
              <div
                key={rf.fact_id}
                className="flex items-center gap-2 px-2 py-1 rounded hover:bg-secondary/60 text-sm cursor-pointer mb-0.5"
                onClick={() => {
                  // No dedicated fact detail panel — just highlight in facts list
                  openPanel('facts', { subject: rf.subject })
                }}
              >
                <span className="text-muted-foreground font-mono text-xs shrink-0">{rf.predicate}</span>
                <span className="flex-1 truncate">{rf.object}</span>
                <ConfidenceBadge value={rf.confidence} />
              </div>
            ))}
          </TreeTrunk>
        </TreeBranch>
      )}

      {/* Related hypotheses */}
      {relatedHypotheses.length > 0 && (
        <TreeBranch last>
          <div className="text-xs text-muted-foreground font-medium mb-1">
            Related Hypotheses ({relatedHypotheses.length})
          </div>
          <TreeTrunk>
            {relatedHypotheses.map((h) => (
              <HypothesisRow key={h.id} hypothesis={h} />
            ))}
          </TreeTrunk>
        </TreeBranch>
      )}
    </div>
  )
}

// ── Evidence chain for an EVENT ──

function EventChain({ eventId }: { eventId: string }) {
  const { data: event, isLoading } = useEvent(eventId)

  // Find hypotheses referencing this event's title keywords
  const { data: hypotheses } = useHypotheses('active')

  const relatedHypotheses = useMemo(() => {
    if (!hypotheses || !event) return []
    const titleWords = event.title
      .toLowerCase()
      .split(/\s+/)
      .filter((w) => w.length > 4)
    if (titleWords.length === 0) return []
    return hypotheses.filter((h) => {
      const combined = (h.thesis + ' ' + h.counter_thesis).toLowerCase()
      return titleWords.some((w) => combined.includes(w))
    }).slice(0, 3)
  }, [hypotheses, event])

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading event details...
      </div>
    )
  }

  if (!event) {
    return <div className="text-sm text-muted-foreground py-2">Event not found</div>
  }

  return (
    <div className="space-y-1">
      {/* Root event */}
      <div className="flex items-center gap-2 px-3 py-2 rounded bg-primary/10 border border-primary/30">
        <Badge className={cn(categoryColor(event.category), 'text-[10px]')}>{event.category}</Badge>
        <span className="text-sm font-medium text-foreground flex-1 truncate">{event.title}</span>
        <ConfidenceBadge value={event.confidence} />
      </div>

      {/* Linked entities */}
      {event.entities.length > 0 && (
        <TreeBranch last={event.linked_signals.length === 0 && relatedHypotheses.length === 0}>
          <div className="text-xs text-muted-foreground font-medium mb-1">
            Entities ({event.entities.length})
          </div>
          <TreeTrunk>
            {event.entities.map((ent) => (
              <div
                key={ent.entity_id}
                className="flex items-center gap-2 px-2 py-1 rounded hover:bg-secondary/60 text-sm mb-0.5"
              >
                <EntityLink name={ent.name} id={ent.entity_id} type={ent.entity_type} />
                {ent.role && <span className="text-xs text-muted-foreground">({ent.role})</span>}
              </div>
            ))}
          </TreeTrunk>
        </TreeBranch>
      )}

      {/* Supporting signals */}
      {event.linked_signals.length > 0 && (
        <TreeBranch last={relatedHypotheses.length === 0}>
          <div className="text-xs text-muted-foreground font-medium mb-1">
            Supporting Signals ({event.linked_signals.length})
          </div>
          <TreeTrunk>
            {event.linked_signals.map((sig, i) => (
              <div
                key={i}
                className="flex items-center gap-2 px-2 py-1.5 rounded bg-secondary/30 text-sm mb-0.5"
              >
                <Badge className={cn(categoryColor(sig.category), 'text-[10px]')}>{sig.category}</Badge>
                <span className="flex-1 truncate">{sig.title}</span>
                <ConfidenceBadge value={sig.confidence} />
                <TimeAgo date={sig.timestamp} className="text-[10px] text-muted-foreground shrink-0" />
              </div>
            ))}
          </TreeTrunk>
        </TreeBranch>
      )}

      {/* Related hypotheses */}
      {relatedHypotheses.length > 0 && (
        <TreeBranch last>
          <div className="text-xs text-muted-foreground font-medium mb-1">
            Related Hypotheses ({relatedHypotheses.length})
          </div>
          <TreeTrunk>
            {relatedHypotheses.map((h) => (
              <HypothesisRow key={h.id} hypothesis={h} />
            ))}
          </TreeTrunk>
        </TreeBranch>
      )}
    </div>
  )
}

// ── Evidence chain for a SITUATION ──

function SituationChain({ situationId }: { situationId: string }) {
  const { data: situation, isLoading } = useSituation(situationId)
  const openPanel = useWorkspaceStore((s) => s.openPanel)
  const select = useSelectionStore((s) => s.select)

  // Find hypotheses referencing this situation
  const { data: hypotheses } = useHypotheses('active')

  const relatedHypotheses = useMemo(() => {
    if (!hypotheses || !situation) return []
    return hypotheses.filter(
      (h) => h.situation_name && situation.title.toLowerCase().includes(h.situation_name.toLowerCase()),
    ).slice(0, 5)
  }, [hypotheses, situation])

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading situation details...
      </div>
    )
  }

  if (!situation) {
    return <div className="text-sm text-muted-foreground py-2">Situation not found</div>
  }

  return (
    <div className="space-y-1">
      {/* Root situation */}
      <div className="flex items-center gap-2 px-3 py-2 rounded bg-primary/10 border border-primary/30">
        <Badge className="bg-amber-500/20 text-amber-400 border-amber-500/30 text-[10px]">
          {situation.severity}
        </Badge>
        <span className="text-sm font-medium text-foreground flex-1 truncate">{situation.title}</span>
        <Badge className="text-[10px]">{situation.status}</Badge>
      </div>

      {/* Description */}
      {situation.description && (
        <div className="px-3 py-1.5 text-xs text-muted-foreground leading-relaxed">
          {situation.description}
        </div>
      )}

      {/* Linked events */}
      {situation.events.length > 0 && (
        <TreeBranch last={relatedHypotheses.length === 0}>
          <div className="text-xs text-muted-foreground font-medium mb-1">
            Linked Events ({situation.events.length})
          </div>
          <TreeTrunk>
            {situation.events.map((ev) => (
              <div
                key={ev.event_id}
                className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-secondary/60 text-sm cursor-pointer mb-0.5"
                onClick={() => {
                  select({ type: 'event', id: ev.event_id, name: ev.title })
                  openPanel('event-detail', { id: ev.event_id })
                }}
              >
                <Badge className={cn(categoryColor(ev.category), 'text-[10px]')}>{ev.category}</Badge>
                <span className="flex-1 truncate">{ev.title}</span>
                <ConfidenceBadge value={ev.confidence} />
                <TimeAgo date={ev.timestamp} className="text-[10px] text-muted-foreground shrink-0" />
              </div>
            ))}
          </TreeTrunk>
        </TreeBranch>
      )}

      {/* Related hypotheses */}
      {relatedHypotheses.length > 0 && (
        <TreeBranch last>
          <div className="text-xs text-muted-foreground font-medium mb-1">
            Related Hypotheses ({relatedHypotheses.length})
          </div>
          <TreeTrunk>
            {relatedHypotheses.map((h) => (
              <HypothesisRow key={h.id} hypothesis={h} />
            ))}
          </TreeTrunk>
        </TreeBranch>
      )}
    </div>
  )
}

// ── Shared hypothesis row ──

function HypothesisRow({ hypothesis }: { hypothesis: HypothesisSummary }) {
  const support = hypothesis.support_count ?? 0
  const refute = hypothesis.refute_count ?? 0

  return (
    <div className="px-2 py-1.5 rounded bg-secondary/30 text-sm mb-0.5">
      <div className="text-xs text-foreground/90 truncate">{hypothesis.thesis}</div>
      <div className="text-[10px] text-muted-foreground italic truncate">vs. {hypothesis.counter_thesis}</div>
      <div className="flex items-center gap-2 mt-0.5">
        <span className="text-[10px] text-green-400">+{support} supporting</span>
        <span className="text-[10px] text-red-400">-{refute} refuting</span>
        <Badge className="text-[10px] bg-secondary">{hypothesis.status}</Badge>
      </div>
    </div>
  )
}

// ── Main modal component ──

export function EvidenceChainModal({ entityType, entityId, factSubject, label, onClose }: EvidenceChainProps) {
  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Fetch fact data if entityType is 'fact' (we need the full fact object)
  const { data: factsData } = useFacts(
    entityType === 'fact' ? { subject: factSubject, limit: 50 } : { limit: 0 },
  )

  const fact = useMemo(() => {
    if (entityType !== 'fact' || !factsData?.items) return null
    return factsData.items.find((f) => f.fact_id === entityId) ?? null
  }, [entityType, entityId, factsData])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />

      {/* Modal */}
      <div className="relative w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col rounded-lg border border-gray-700 bg-gray-900 shadow-xl">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700 shrink-0">
          <div className="flex items-center gap-2">
            <Link2 className="h-4 w-4 text-gray-400" />
            <h3 className="text-sm font-semibold text-gray-100">Evidence Chain</h3>
            {label && (
              <span className="text-xs text-muted-foreground truncate max-w-[300px]">
                — {label}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-4 py-4">
          {entityType === 'fact' && fact && <FactChain fact={fact} />}
          {entityType === 'fact' && !fact && factsData && (
            <div className="text-sm text-muted-foreground">Fact not found in results</div>
          )}
          {entityType === 'fact' && !factsData && (
            <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading fact data...
            </div>
          )}
          {entityType === 'event' && <EventChain eventId={entityId} />}
          {entityType === 'situation' && <SituationChain situationId={entityId} />}
        </div>
      </div>
    </div>
  )
}
