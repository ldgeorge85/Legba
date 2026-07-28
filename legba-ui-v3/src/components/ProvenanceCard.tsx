/**
 * ProvenanceCard — the reusable in-panel lineage/provenance surface (P4-5).
 *
 * Adapts worldmonitor's `LayerExplanation` grammar: for a displayed datum or
 * panel, it answers purpose / source / freshness / confidence / limitations
 * IN-PANEL — no walker click, no second round-trip. Everything it shows is
 * pulled from what the substrate ALREADY carries (lineage `derived_from`,
 * verify state, source, produced/fetched_at, confidence), shaped by the pure
 * `describeProvenance` in `lib/provenance`.
 *
 * Two entry points:
 *   * `<ProvenanceCard facts={...} />`   — pass already-shaped ProvenanceFacts.
 *   * `<ProvenanceCardFor source={...} />` — pass raw substrate fields; the card
 *     shapes them via `describeProvenance`.
 */
import { HelpCircle } from 'lucide-react'
import { relTime } from '@/lib/evalOps'
import {
  describeProvenance,
  type ProvenanceFacts,
  type ProvenanceSource,
} from '@/lib/provenance'
import { ProvenanceStateBadge } from '@/components/ProvenanceBadge'

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="w-20 shrink-0 uppercase tracking-wide text-ink-3">{label}</span>
      <span className="min-w-0 flex-1 text-ink-2">{children}</span>
    </div>
  )
}

export function ProvenanceCard({
  facts,
  title = 'Provenance',
  className,
}: {
  facts: ProvenanceFacts
  title?: string
  className?: string
}) {
  const freshness =
    facts.freshnessLabel ?? (facts.freshnessAt ? relTime(facts.freshnessAt) : null)
  return (
    <div
      className={`rounded border border-line bg-surf-1 p-2 space-y-1 ${className ?? ''}`}
      data-testid="provenance-card"
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1 text-label font-semibold uppercase tracking-wider text-ink-2">
          <HelpCircle className="h-3 w-3" aria-hidden />
          {title}
        </span>
        <ProvenanceStateBadge state={facts.state} />
      </div>
      {facts.purpose && <Row label="Purpose">{facts.purpose}</Row>}
      {facts.source && (
        <Row label="Source">
          <span className="font-mono text-ink-1">{facts.source}</span>
        </Row>
      )}
      <Row label="Freshness">
        {freshness ? (
          freshness
        ) : (
          <span className="text-ink-3">no timestamp recorded</span>
        )}
      </Row>
      {facts.confidence && <Row label="Confidence">{facts.confidence}</Row>}
      <Row label="Limits">
        {facts.limitations.length === 0 ? (
          <span className="text-ink-3">none noted</span>
        ) : (
          <ul className="list-disc space-y-0.5 pl-3">
            {facts.limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        )}
      </Row>
    </div>
  )
}

/** Shape raw substrate fields into a card in one step. */
export function ProvenanceCardFor({
  source,
  title,
  className,
}: {
  source: ProvenanceSource
  title?: string
  className?: string
}) {
  return <ProvenanceCard facts={describeProvenance(source)} title={title} className={className} />
}
