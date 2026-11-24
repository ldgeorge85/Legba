/**
 * The Accuracy page's shared parts (`system.eval_scorecard`, UI-1): the
 * numbered section frame, the code-span prose renderer, and the figure cell
 * every table on the page uses. Tokens only — no raw palette class.
 */
import type { ReactNode } from 'react'
import { Figure, type FigureData } from '@/lib/Figure'

/** One numbered section of the page. */
export function Section({
  n,
  title,
  lead,
  testId,
  children,
}: {
  n: number
  title: string
  lead?: ReactNode
  testId: string
  children: ReactNode
}) {
  return (
    <section className="mb-3 space-y-2 text-xs" data-testid={testId} aria-label={title}>
      <header className="flex items-baseline gap-2 border-b border-line pb-1">
        <span className="font-mono text-label text-ink-3">{n}</span>
        <h3 className="text-label uppercase tracking-wide text-ink-2">{title}</h3>
      </header>
      {lead && <div className="text-label leading-relaxed text-ink-3">{lead}</div>}
      {children}
    </section>
  )
}

/** Text with `code` spans — the generated list keeps the source's backticks. */
export function Prose({ text, className }: { text: string; className?: string }) {
  const parts = text.split('`')
  return (
    <span className={className}>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <code key={i} className="font-mono text-label text-ink-2">
            {p}
          </code>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </span>
  )
}

/** A figure in a table cell: value + unit + as-of, or the absence phrase. */
export function FigureCell({ fig, testId, label }: { fig: FigureData; testId: string; label?: string }) {
  return <Figure {...fig} label={label} testId={testId} className="flex-wrap text-label" />
}
