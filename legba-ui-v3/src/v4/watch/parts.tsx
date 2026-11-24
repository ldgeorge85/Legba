/**
 * Small shared pieces of the Watch views (U-9): the view's head, a filter
 * chip, a figure that prints "–" until it is read, and a link that makes a
 * subject the frame's point. Hairline rules, no boxed cards; 12 px floor; the
 * one accent (magenta, `point`) only marks the reader's point.
 */
import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'
import { useSelection } from '@/state/selection'
import type { SubjectRef } from './model'

export function ViewHead({ title, line, testId }: { title: string; line: ReactNode; testId: string }) {
  return (
    <header className="border-b border-line-subtle pb-2" data-testid={testId}>
      <h2 className="font-serif text-heading-lg text-ink-1">{title}</h2>
      <div className="mt-0.5 text-label text-ink-2">{line}</div>
    </header>
  )
}

/** A figure: "–" until its read answered; a measured zero stays 0 (brief §6 rule 2). */
export function Figure({ value, testId }: { value: number | null | undefined; testId?: string }) {
  const read = typeof value === 'number' && Number.isFinite(value)
  return (
    <span className="font-mono tabular-nums text-ink-1" data-testid={testId} data-read={read ? '1' : '0'}>
      {read ? value : '–'}
    </span>
  )
}

export function Chip({
  on,
  onClick,
  children,
  testId,
  title,
}: {
  on: boolean
  onClick: () => void
  children: ReactNode
  testId?: string
  title?: string
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      title={title}
      onClick={onClick}
      data-testid={testId}
      className={cn(
        'rounded-control border px-2 py-0.5 text-label transition-colors',
        on ? 'border-line-strong bg-surf-3 text-ink-1' : 'border-line-subtle text-ink-2 hover:text-ink-1',
      )}
    >
      {children}
    </button>
  )
}

export function ChipRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label={label}>
      <span className="mr-1 text-label text-ink-3">{label}</span>
      {children}
    </div>
  )
}

/**
 * A subject as a link: a click makes it the frame's point (the Record sheet,
 * the map and Consult follow). Marked in the accent while it IS the point. A
 * subject that is not a record (the host, a route) prints as plain text.
 */
export function SubjectLink({ subject, origin = 'watch', onPick }: { subject: SubjectRef; origin?: string; onPick?: () => void }) {
  const isPoint = useSelection((s) => !!subject.kind && s.selection?.kind === subject.kind && s.selection?.id === subject.id)
  if (!subject.kind) return <span className="text-ink-1">{subject.label}</span>
  return (
    <button
      type="button"
      className={cn('text-left underline decoration-line-strong underline-offset-2 hover:decoration-ink-2', isPoint ? 'text-point' : 'text-ink-1')}
      data-testid="watch-subject-link"
      data-point={isPoint ? '1' : '0'}
      onClick={(e) => {
        e.stopPropagation()
        useSelection.getState().select({ kind: subject.kind!, id: subject.id, label: subject.label, origin })
        onPick?.()
      }}
    >
      {subject.label}
    </button>
  )
}

/** A plain sentence that stands where a list would be (empty, unread, not set up). */
export function Notice({ title, children, testId }: { title: string; children?: ReactNode; testId: string }) {
  return (
    <div className="py-4" data-testid={testId}>
      <p className="font-serif text-heading text-ink-1">{title}</p>
      {children ? <div className="mt-1 max-w-[62ch] text-body text-ink-2">{children}</div> : null}
    </div>
  )
}
