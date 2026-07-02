/**
 * ProvenanceTrail — "where did this come from", as a row of chips (v4, The Why).
 *
 * Given the global `Selection`, this answers the operator's first provenance
 * question: the ordered chain that produced the selected row, oldest → newest
 * (e.g. `signal → finding → situation`). For the lineage-walkable kinds
 * (finding / situation / signal) it fetches the upstream lineage report and
 * threads a single primary path from the deepest ancestor down to the selected
 * row; every chip is clickable and re-drives the shared selection so the other
 * rooms follow.
 *
 * For the non-walkable kinds (entity / source / target / analyst) there is no
 * `derived_from` chain to walk, so we render a single self-chip — the
 * provenance "trail" of a source IS the source.
 *
 * --- Lineage-shape assumptions (verified against lineage_api.py) ---
 *  - Edges are `parent → child` where `parent ∈ child.derived_from`. To go
 *    upstream (toward origins) we follow child→parent links; "oldest" is the
 *    deepest ancestor, "newest" is the selected root.
 *  - A row can have multiple parents (DAG, not a list). We pick ONE primary
 *    path — the chain that climbs to the greatest `depth` — so the trail stays
 *    a readable single line. The full DAG lives in the lineage graph panel.
 *  - `report.nodes` excludes the root; `report.root` carries `depth=0`.
 *  - A 404 / missing report ⇒ render the selected row as a lone self-chip.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, ShieldCheck, ExternalLink } from 'lucide-react'
import { apiGet, ApiError } from '@/lib/api'
import {
  buildPrimaryTrail,
  RECEIPT_BADGE,
  type LineageNode,
  type LineageReport,
} from '@/lib/graphModel'
import { useSelection, type Selection, type SelectionKind } from '@/state/selection'
import type { ProvenanceRef } from './types'
import ProvenanceChip from '@/v4/components/ProvenanceChip'
import ContestedBadge from '@/v4/components/ContestedBadge'

/** Selection kinds that map to a lineage-walkable substrate row.
 *  `signal` is not a `SelectionKind` per se but the lineage API + the chips
 *  contract admit it, so we accept it defensively. */
const WALKABLE_KINDS = new Set<string>(['finding', 'situation', 'signal'])

function isWalkable(kind: SelectionKind | string): boolean {
  return WALKABLE_KINDS.has(kind)
}

/** The kinds the shared selection store accepts. A lineage walk can surface
 *  other row kinds (e.g. `signal`, `fact`) which render as chips but aren't
 *  cross-room selectable, so clicking them is a no-op rather than a type lie. */
const SELECTABLE = new Set<SelectionKind>([
  'target',
  'entity',
  'source',
  'analyst',
  'finding',
  'situation',
])

/** Project a lineage node to the chip contract. */
function nodeToRef(n: LineageNode): ProvenanceRef {
  return {
    kind: n.row_kind as ProvenanceRef['kind'],
    id: n.id,
    label: n.title ?? n.id,
    via: n.analyst_id ?? n.target_id ?? undefined,
  }
}

/** A trail step carries both the chip ref and the node it came from, so a
 *  click can drive the shared selection with the right row_kind. */
interface TrailStep {
  ref: ProvenanceRef
  node: LineageNode
}

/**
 * Build the ordered oldest→newest primary-path steps from a lineage report.
 *
 * The single-line walk itself (root → deepest ancestor, greatest-depth parent)
 * lives in the shared `buildPrimaryTrail` (so the lineage DAG panel walks the
 * exact same line); here we just wrap each node as a clickable `TrailStep`.
 */
function buildPrimaryPath(report: LineageReport): TrailStep[] {
  return buildPrimaryTrail(report).map((node) => ({ ref: nodeToRef(node), node }))
}

interface ProvenanceTrailProps {
  selection: Selection
}

/**
 * The provenance trail for the current selection. Self-contained: it owns its
 * own lineage query and renders loading / empty / error states inline.
 */
export default function ProvenanceTrail({ selection }: ProvenanceTrailProps) {
  const select = useSelection((s) => s.select)
  const walkable = isWalkable(selection.kind)

  const {
    data: report,
    isLoading,
    error,
  } = useQuery<LineageReport>({
    enabled: walkable,
    queryKey: ['why-provenance-trail', selection.kind, selection.id],
    queryFn: () =>
      apiGet<LineageReport>(
        `/lineage/${selection.kind}/${encodeURIComponent(selection.id)}?direction=upstream&depth=6`,
      ),
  })

  // A 404 (or any row-not-found) is a normal, graceful outcome — the row exists
  // in the selection but has no walkable lineage. Treat it like an empty trail.
  const notFound = error instanceof ApiError && error.status === 404

  // The self-chip for the selected row — the fallback for non-walkable kinds,
  // 404s, and empty reports.
  const selfStep: TrailStep = useMemo(() => {
    const ref: ProvenanceRef = {
      kind: selection.kind,
      id: selection.id,
      label: selection.label ?? selection.id,
    }
    const node: LineageNode = {
      id: selection.id,
      row_kind: selection.kind,
      title: selection.label ?? null,
      produced_at: '',
      target_id: null,
      analyst_id: null,
      schema_uri: '',
      depth: 0,
    }
    return { ref, node }
  }, [selection.kind, selection.id, selection.label])

  const steps: TrailStep[] = useMemo(() => {
    if (!walkable) return [selfStep]
    if (!report) return [selfStep]
    const path = buildPrimaryPath(report)
    return path.length > 0 ? path : [selfStep]
  }, [walkable, report, selfStep])

  /** Drive the shared selection from a chip — only for kinds the selection
   *  store accepts (see `SELECTABLE`). */
  function onChipClick(node: LineageNode) {
    if (!SELECTABLE.has(node.row_kind as SelectionKind)) return
    select({
      kind: node.row_kind as SelectionKind,
      id: node.id,
      label: node.title ?? node.id,
    })
  }

  // The id of the chip to mark active: the newest step (the selected row).
  const activeId = steps.length > 0 ? steps[steps.length - 1].node.id : selection.id

  if (walkable && isLoading) {
    return (
      <div
        className="flex flex-wrap items-center gap-1.5"
        data-testid="why-provenance-trail"
        aria-busy="true"
      >
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex items-center gap-1.5">
            <div className="h-5 w-28 animate-pulse rounded-full bg-surface-100" />
            {i < 2 && <ChevronRight className="h-3.5 w-3.5 text-slate-700" aria-hidden />}
          </div>
        ))}
      </div>
    )
  }

  // A real (non-404) error: surface it, but still show the self-chip so the
  // operator keeps an anchor on what they selected.
  const hardError = walkable && error && !notFound

  return (
    <div
      className="flex flex-col gap-1"
      data-testid="why-provenance-trail"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        {steps.map((step, i) => (
          <div key={`${step.node.id}-${i}`} className="flex max-w-full items-center gap-1.5">
            <ProvenanceChip
              refItem={step.ref}
              active={step.node.id === activeId}
              onClick={
                SELECTABLE.has(step.node.row_kind as SelectionKind)
                  ? () => onChipClick(step.node)
                  : undefined
              }
            />
            {/* P1-T5 — the honest per-hop receipt badge + source link (analyst
                hops show 'chain-consistent (single-node)'; signal hops have no
                receipt and open their real source URL). Renders nothing for hops
                with neither (the common case), so the chip line stays clean. */}
            <HopReceipt node={step.node} />
            {i < steps.length - 1 && (
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-600" aria-hidden />
            )}
          </div>
        ))}
      </div>

      {/* #101 contested-claims surface: any trail step that IS a fact carries a
          true `facts.id`, so we can precisely look up whether that fact belongs
          to a live dispute. Renders nothing when uncontested (the common case). */}
      {steps
        .filter((step) => step.node.row_kind === 'fact')
        .map((step) => (
          <ContestedBadge key={`contested-${step.node.id}`} factId={step.node.id} />
        ))}

      {hardError && (
        <p className="text-[10px] text-slate-500">
          lineage unavailable
          {error instanceof ApiError ? ` (HTTP ${error.status})` : ''} — showing the selected
          row only
        </p>
      )}
      {walkable && (notFound || (report && steps.length === 1)) && (
        <p className="text-[10px] text-slate-600">no upstream lineage recorded</p>
      )}
    </div>
  )
}

/**
 * The honest per-hop receipt indicator + source link, inline beside a trail chip.
 *
 * Analyst hops carry a `receipt` — a SHA-256 chain *consistency* check the
 * backend RE-COMPUTES per node (NOT a signature). We render that node's own
 * `badge` verbatim (`'chain-consistent (single-node)'`) only when the re-hash
 * matched; a mismatch DEGRADES to "chain inconsistent" rather than fabricating a
 * green badge. Signal / source hops carry no receipt (`receipt=null`) and show
 * no badge — instead they expose their real `canonical_url`, so the line ends at
 * a clickable source. Renders nothing when a hop has neither.
 */
function HopReceipt({ node }: { node: LineageNode }) {
  const r = node.receipt
  if (!r && !node.canonical_url) return null
  return (
    <span className="inline-flex shrink-0 items-center gap-1">
      {r &&
        (r.chain_consistent ? (
          <span
            className="inline-flex items-center gap-1 rounded bg-accent-ok/15 px-1 py-0.5 text-[9px] leading-none text-accent-ok"
            title={`receipt ${r.receipt_hash.slice(0, 12)}… · re-hash matched`}
          >
            <ShieldCheck className="h-2.5 w-2.5" aria-hidden />
            {r.badge || RECEIPT_BADGE}
          </span>
        ) : (
          <span
            className="inline-flex items-center gap-1 rounded bg-accent-critical/15 px-1 py-0.5 text-[9px] leading-none text-accent-critical"
            title={`receipt ${r.receipt_hash.slice(0, 12)}… · re-hash MISMATCH`}
          >
            <ShieldCheck className="h-2.5 w-2.5" aria-hidden />
            chain inconsistent
          </span>
        ))}
      {node.canonical_url && (
        <a
          href={node.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          title={node.canonical_url}
          className="inline-flex items-center gap-0.5 text-[9px] text-accent-info hover:underline"
        >
          <ExternalLink className="h-2.5 w-2.5" aria-hidden />
          source
        </a>
      )}
    </span>
  )
}
