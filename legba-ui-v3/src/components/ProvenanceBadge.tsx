/**
 * ProvenanceBadge — the `live | fallback | absent` stamp on a displayed NUMBER
 * (P4-5).
 *
 * Wherever a panel shows a computed number that could come from live data, a
 * degraded fallback table, or nothing, this tiny badge tells the viewer WHICH,
 * so a real-live figure is never confused with a fallback or an honest empty.
 * Shape/label carry the meaning (not color alone) so it survives a colorblind
 * viewer, and the tone reads the token palette so it flips with the theme.
 *
 * The state is resolved by `resolveNumberProvenance` in `lib/provenance` — this
 * component NEVER decides live-vs-fallback itself; it renders what the resolver
 * returns. Pass an explicit `fallback` only when the backend actually signals
 * one (see the lib's honesty note).
 */
import { CircleDot, CircleSlash, Database } from 'lucide-react'
import { cn } from '@/lib/cn'
import {
  PROVENANCE_META,
  resolveNumberProvenance,
  type ProvenanceState,
} from '@/lib/provenance'

const TONE_CLASS: Record<'ok' | 'warn' | 'muted', string> = {
  ok: 'text-accent-ok',
  warn: 'text-accent-warning',
  muted: 'text-ink-3',
}

const STATE_ICON = {
  live: CircleDot,
  fallback: Database,
  absent: CircleSlash,
} as const

/** Render a badge for an already-resolved state. */
export function ProvenanceStateBadge({
  state,
  className,
}: {
  state: ProvenanceState
  className?: string
}) {
  const meta = PROVENANCE_META[state]
  const Icon = STATE_ICON[state]
  return (
    <span
      className={cn(
        'inline-flex items-center gap-0.5 rounded px-1 text-[10px] font-medium uppercase tracking-wide',
        'border border-line bg-surf-1',
        TONE_CLASS[meta.tone],
        className,
      )}
      title={meta.title}
      data-testid={`provenance-badge-${state}`}
      data-provenance-state={state}
    >
      <Icon className="h-2.5 w-2.5" aria-hidden />
      {meta.label}
    </span>
  )
}

/**
 * Resolve + render a provenance badge for a displayed number in one step.
 * `value` present (finite) → live; missing → absent; pass `fallback` ONLY when
 * the backend signals a degraded source.
 */
export function ProvenanceBadge({
  value,
  fallback,
  treatAsAbsent,
  className,
}: {
  value: number | null | undefined
  fallback?: boolean
  treatAsAbsent?: boolean
  className?: string
}) {
  const state = resolveNumberProvenance({ value, fallback, treatAsAbsent })
  return <ProvenanceStateBadge state={state} className={className} />
}
