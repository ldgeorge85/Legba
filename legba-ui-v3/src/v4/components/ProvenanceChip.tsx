/**
 * ProvenanceChip — the cross-room provenance pill (v4).
 *
 * A small, self-contained, keyboard-accessible button rendering one
 * `ProvenanceRef`: a kind-colored dot, the (truncated) label, and a faint
 * `via {…}` suffix when the producing analyst/target is known. Reused by The
 * Why's lineage trail and (potentially) the World/Flow drawers, so it pulls in
 * nothing room-specific — just the shared `kindColor` palette + `cn`.
 *
 * `kindColor` (from @/lib/graphModel) only knows the lineage row_kinds; the
 * provenance contract also admits `source | target | analyst | entity`, which
 * have no row_kind entry, so we layer a small fallback palette for those.
 */
import { cn } from '@/lib/cn'
import { kindColor, KIND_DEFAULT_COLOR } from '@/lib/graphModel'
import type { ProvenanceRef } from '@/v4/why/types'

/** Hex fallbacks for the non-lineage provenance kinds (kindColor has no entry).
 *  Chosen to read distinctly against the dark surface and not collide with the
 *  signal/finding/situation ramp. */
const NON_LINEAGE_COLORS: Record<string, string> = {
  source: '#38bdf8', // sky-400 — acquisition origin
  target: '#a3e635', // lime-400 — what's being watched
  analyst: '#f0abfc', // fuchsia-300 — the producer
  entity: '#fdba74', // orange-300 — a graph entity
}

/** Resolve a chip's dot color across both the lineage row_kinds and the
 *  extra provenance kinds, falling back to slate for anything unknown. */
function chipColor(kind: string): string {
  return NON_LINEAGE_COLORS[kind] ?? kindColor(kind) ?? KIND_DEFAULT_COLOR
}

const MAX_LABEL = 28

/** Truncate to ~`MAX_LABEL` chars with an ellipsis (kept local so the chip has
 *  no dependency on graphModel's `truncate` signature). */
function clampLabel(s: string): string {
  return s.length <= MAX_LABEL ? s : `${s.slice(0, MAX_LABEL - 1)}…`
}

export interface ProvenanceChipProps {
  refItem: ProvenanceRef
  onClick?: () => void
  /** Marks the selected / current row's chip (ring highlight). */
  active?: boolean
}

/**
 * One clickable provenance pill. Renders as a `<button>` so it's keyboard- and
 * screen-reader-accessible; when no `onClick` is given it's still rendered as a
 * button but non-interactive (disabled), preserving layout.
 */
export default function ProvenanceChip({ refItem, onClick, active }: ProvenanceChipProps) {
  const label = refItem.label ?? refItem.id
  const display = clampLabel(label)
  const color = chipColor(refItem.kind)
  const title = refItem.via ? `${label} · via ${refItem.via}` : label

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      title={title}
      aria-label={title}
      aria-current={active ? 'true' : undefined}
      data-kind={refItem.kind}
      className={cn(
        'group inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5',
        'text-[11px] leading-none transition-colors',
        'border-slate-700 bg-surface-100 text-slate-200',
        onClick && 'cursor-pointer hover:border-slate-600 hover:bg-surface-50',
        !onClick && 'cursor-default',
        active && 'ring-1 ring-accent-info ring-offset-1 ring-offset-surface-200',
      )}
    >
      <span
        aria-hidden
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ backgroundColor: color }}
      />
      <span className="truncate">{display}</span>
      {refItem.via && (
        <span className="shrink-0 truncate text-[10px] text-slate-500">
          via {clampLabel(refItem.via)}
        </span>
      )}
    </button>
  )
}
