/**
 * PinButton — the cross-room "pin to case" affordance (v4 Wave 3).
 *
 * A small, self-contained toggle that pins/unpins one typed ref (signal /
 * finding / entity / situation / source / target / analyst) to the casework
 * store. It subscribes to the `hasCard` predicate so its pinned/unpinned state
 * stays live as cards are added or removed anywhere (rail, board, other rooms).
 *
 * Reused across the World / Flow / Why rooms, so it pulls in nothing
 * room-specific — just the casework store, the shared palette, `cn`, and a
 * lucide Pin icon. `compact` collapses it to an icon-only button with a title
 * tooltip for dense rows.
 */
import { Pin } from 'lucide-react'
import { cn } from '@/lib/cn'
import {
  useCaseStore,
  CASE_KIND_COLOR,
  type CaseCardKind,
} from '@/v4/case/caseStore'

export interface PinButtonProps {
  kind: CaseCardKind
  refId: string
  label: string
  /** Icon-only (no text) — relies on the title tooltip for affordance. */
  compact?: boolean
}

/**
 * Toggle a ref's membership in the current case. Renders a filled, accent
 * Pin when pinned (click removes) and an outline Pin when not (click adds).
 */
export default function PinButton({ kind, refId, label, compact }: PinButtonProps) {
  // Subscribe to the cards slice (not the action) so pinned-state re-renders
  // live as the store changes; recompute the predicate against the slice.
  const pinned = useCaseStore((s) => s.cards.some((c) => c.id === `card_${kind}_${refId}`))
  const addCard = useCaseStore((s) => s.addCard)
  const removeCard = useCaseStore((s) => s.removeCard)

  const accent = CASE_KIND_COLOR[kind]
  const title = pinned ? `Unpin "${label}" from the case` : `Pin "${label}" to the case`

  const handleClick = () => {
    if (pinned) {
      removeCard(`card_${kind}_${refId}`)
    } else {
      addCard({ kind, refId, label })
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      title={title}
      aria-label={title}
      aria-pressed={pinned}
      data-kind={kind}
      className={cn(
        'inline-flex shrink-0 items-center gap-1 rounded-md border text-[11px] leading-none transition-colors',
        'focus:outline-none focus:ring-1 focus:ring-accent-info',
        compact ? 'h-6 w-6 justify-center p-0' : 'px-2 py-1',
        pinned
          ? 'border-slate-700 bg-surface-100 text-slate-200 hover:border-slate-600 hover:bg-surface-50'
          : 'border-slate-800 bg-surface-200 text-slate-400 hover:border-slate-700 hover:text-slate-200',
      )}
    >
      <Pin
        className="h-3.5 w-3.5 shrink-0"
        aria-hidden
        // Filled tint when pinned; outline (no fill) when not.
        fill={pinned ? accent : 'none'}
        color={pinned ? accent : 'currentColor'}
      />
      {!compact && <span className="truncate">{pinned ? 'Pinned' : 'Pin to case'}</span>}
    </button>
  )
}
