/**
 * bandChrome — the Morning Read judgment bands' shared furniture.
 *
 * Extracted from `JudgmentBands.tsx` when the GAPS band moved to its own file
 * (k5b): the band frame, the honest state line, the held-back counter, the
 * desk sub-head and the cited row are the surface's VISUAL CONTRACT, and two
 * copies of a contract is how one band quietly starts looking like a
 * different product from the band above it.
 *
 * The caps live here for the same reason. Twenty minutes is the design
 * constraint, so every band shows the worst {@link DESKS_PER_BAND} desks and
 * the worst {@link ITEMS_PER_DESK} rows on each, and then SAYS how many it is
 * holding back — a capped list presented as the whole story is the one
 * failure mode a judgment surface cannot have.
 */
import { selectRow } from '@/state/selection'
import { bandTestId, type BandId } from '@/lib/morningReadBands'

/** Worst desks shown per band before the held-back line. */
export const DESKS_PER_BAND = 6
/** Worst rows shown per desk before the held-back line. */
export const ITEMS_PER_DESK = 5

export type QueryState = 'loading' | 'error' | 'ready'

export function stateOf(q: { isLoading: boolean; isError: boolean }): QueryState {
  if (q.isLoading) return 'loading'
  if (q.isError) return 'error'
  return 'ready'
}

/** X1/D1 — what a band's count reads as before its routes have answered:
 *  `—` while reading, `unavailable` when the read failed (docs/UI.md, the
 *  shell's rule: "a count not read yet is `—` … neither is ever `0`"). */
export const COUNT_PENDING = '—'
export const COUNT_UNAVAILABLE = 'unavailable'

/**
 * A band's headline count, or null when it has not been read. The Morning
 * Read printed "0 across 0 desks · 0 verified · 0 claims flagged" for eleven
 * seconds on every landing (review D, 2026-10-08) because each band reduced
 * over an empty list while its query was still in flight. A zero is only a
 * zero once the band is READY.
 */
export function bandCount(state: QueryState, n: number): number | null {
  return state === 'ready' ? n : null
}

/** The words a band head and the rail print for a count that is not a number yet. */
export function pendingCountText(state: QueryState): string {
  return state === 'error' ? COUNT_UNAVAILABLE : COUNT_PENDING
}

/** An honest state line — loading, failed, or nothing to report. */
export function StateLine({
  children,
  testid,
}: {
  children: React.ReactNode
  testid?: string
}) {
  return (
    <p className="text-xs leading-relaxed text-ink-3" data-testid={testid}>
      {children}
    </p>
  )
}

/** "+N more" — the held-back count, always stated. */
export function HeldBack({ n, noun }: { n: number; noun: string }) {
  if (n <= 0) return null
  return (
    <p className="mt-1 text-xs text-ink-3" data-testid="morning-read-held-back">
      +{n} more {noun}
      {n === 1 ? '' : 's'} not shown.
    </p>
  )
}

export interface BandFrameProps {
  id: BandId
  label: string
  question: string
  /** The band's own headline count — rendered in the head and on the rail.
   *  Null until the band's routes have answered ({@link bandCount}). */
  count: number | null
  /** The head's note. Read only when `count` is a number: a band that has
   *  not been read prints {@link pendingCountText} instead, never a sentence
   *  of zeros. */
  note: string
  /** The band's read state — decides `—` (reading) vs `unavailable`. */
  state: QueryState
  children: React.ReactNode
}

export function BandFrame({ id, label, question, count, note, state, children }: BandFrameProps) {
  return (
    <section
      id={bandTestId(id)}
      className="read-col mb-7"
      data-band={id}
      data-count={count ?? undefined}
      data-count-state={count === null ? (state === 'error' ? 'unavailable' : 'pending') : 'read'}
      data-testid={bandTestId(id)}
    >
      <div className="read-band-head">
        <h2>{label}</h2>
        <div className="read-rule" />
        <span className="read-band-note" data-testid={`${bandTestId(id)}-note`}>
          {count === null ? pendingCountText(state) : note}
        </span>
      </div>
      <p className="mb-2 text-xs italic leading-relaxed text-ink-3">{question}</p>
      {children}
    </section>
  )
}

/** One desk's sub-head inside a band. */
export function DeskHead({ label, note }: { label: string; note: string }) {
  return (
    <div className="mb-1 flex items-baseline justify-between gap-3">
      <h3 className="text-sm font-semibold text-ink-1">{label}</h3>
      <span className="shrink-0 text-xs text-ink-3">{note}</span>
    </div>
  )
}

/** A cited row: clicking brushes the substrate row into the Inspector. */
export function CitedRow({
  rowKind,
  rowId,
  label,
  children,
  testid,
  title,
}: {
  rowKind: string
  rowId: string
  label: string
  children: React.ReactNode
  testid: string
  title?: string
}) {
  return (
    <li>
      <button
        type="button"
        title={title}
        onClick={() => selectRow(rowKind, rowId, label, { origin: 'morning-read-judgment' })}
        className="w-full rounded px-1 py-0.5 text-left hover:bg-surf-1"
        data-testid={testid}
      >
        {children}
      </button>
    </li>
  )
}
