/** Flow (N-J v0) — small read-only presentation pieces shared by the views. */
import { useState, type ReactNode } from 'react'
import { cn } from '@/lib/cn'

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1">
      <h3 className="text-label uppercase tracking-wide text-ink-3">{title}</h3>
      {children}
    </section>
  )
}

export function KV({ k, v, mono }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex gap-2 text-body min-w-0">
      <span className="w-28 shrink-0 text-ink-3">{k}</span>
      <span className={cn('text-ink-1 min-w-0 break-all', mono && 'font-mono text-[11px]')}>{v}</span>
    </div>
  )
}

/** Monospace, scrollable, selectable. */
export function Mono({ text, maxH = 'max-h-96', testId }: { text: string; maxH?: string; testId?: string }) {
  return (
    <pre
      className={cn('overflow-auto whitespace-pre-wrap break-words rounded-control border border-line bg-surf-1 p-2 font-mono text-[11px] text-ink-1', maxH)}
      data-testid={testId}
    >
      {text}
    </pre>
  )
}

/** Copies text to the clipboard. Not an edit: it changes nothing anywhere. */
export function CopyButton({ text, label = 'copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      className="text-[11px] text-accent-info hover:underline"
      onClick={() => {
        try {
          void navigator.clipboard?.writeText(text).then(() => setDone(true))
        } catch {
          // clipboard unavailable (insecure context) — the text is selectable.
        }
      }}
    >
      {done ? 'copied' : label}
    </button>
  )
}

export function asText(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v
  return JSON.stringify(v, null, 1)
}
