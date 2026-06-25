/**
 * RecordLink — "every id is a link" (redesign Move 4).
 *
 * A 1:1 wrapper over the unified `select()` store. Render any `kind:id`
 * reference as a clickable link; clicking it re-selects, which brushes every
 * room AND loads the Inspector with that record — so the next selection is one
 * click away. "Make an id a link" == "wrap it in `<RecordLink>`". There is no
 * per-call-site routing logic; the store is the router.
 */
import { useSelection, type SelectionKind } from '@/state/selection'
import { cn } from '@/lib/cn'

export interface RecordLinkProps {
  kind: SelectionKind
  id: string
  /** Display text — defaults to the id. */
  label?: string
  /** Tag the breadcrumb provenance with the originating surface. */
  origin?: string
  /** Show a small kind badge before the label. */
  showKind?: boolean
  /** Render the id monospace (default true for raw ids, false when labelled). */
  mono?: boolean
  className?: string
  title?: string
}

const KIND_BADGE: Record<SelectionKind, string> = {
  finding: 'bg-emerald-900/60 text-emerald-200',
  situation: 'bg-indigo-900/60 text-indigo-200',
  signal: 'bg-sky-900/60 text-sky-200',
  entity: 'bg-fuchsia-900/60 text-fuchsia-200',
  target: 'bg-teal-900/60 text-teal-200',
  analyst: 'bg-violet-900/60 text-violet-200',
  source: 'bg-amber-900/60 text-amber-200',
}

export function RecordLink({
  kind,
  id,
  label,
  origin,
  showKind = false,
  mono,
  className,
  title,
}: RecordLinkProps) {
  const select = useSelection((s) => s.select)
  const text = label ?? id
  const useMono = mono ?? label == null
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation()
        select({ kind, id, label: label ?? id, origin: origin ?? 'record-link' })
      }}
      className={cn(
        'inline-flex max-w-full items-center gap-1 text-left align-baseline',
        'text-accent-info underline decoration-dotted underline-offset-2 hover:text-blue-300',
        className,
      )}
      title={title ?? `${kind} · ${id}`}
      data-testid="record-link"
      data-kind={kind}
      data-id={id}
    >
      {showKind && (
        <span className={cn('shrink-0 rounded px-1 text-[10px] leading-tight', KIND_BADGE[kind])}>
          {kind}
        </span>
      )}
      <span className={cn('truncate', useMono && 'font-mono')}>{text}</span>
    </button>
  )
}

/**
 * Heuristic: does this `{key, value}` field look like a `kind:id` reference
 * we can render as a RecordLink? Keys like `finding_id`, `target_id`,
 * `analyst_id`, `source_id`, `situation_id`, `signal_id`, `entity_id` whose
 * value is a non-empty string. Returns the resolved kind or null.
 */
const FIELD_KIND_SUFFIX: Array<[string, SelectionKind]> = [
  ['finding_id', 'finding'],
  ['situation_id', 'situation'],
  ['signal_id', 'signal'],
  ['entity_id', 'entity'],
  ['target_id', 'target'],
  ['analyst_id', 'analyst'],
  ['source_id', 'source'],
]

export function refKindForField(key: string, value: unknown): SelectionKind | null {
  if (typeof value !== 'string' || value.length === 0) return null
  const k = key.toLowerCase()
  for (const [suffix, kind] of FIELD_KIND_SUFFIX) {
    if (k === suffix || k.endsWith(`_${suffix}`) || k === suffix.replace('_id', '')) return kind
  }
  return null
}
