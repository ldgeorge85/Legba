/**
 * ConsultComposer — the one compact block at the foot of Consult.
 *
 * What a person needs to ask is always on screen: the line naming what the
 * question is about (with the time window, read-only: the time band owns the
 * window), a Deep switch, an "On record only" switch, the question box and Ask.
 * Everything else lives behind one "⋯" menu, and the menu's button says in one
 * short line which of its settings are not at their defaults, so nothing that
 * changes an answer is hidden silently.
 *
 * Ask sits beside the box, never below it or after a row of controls, so it is
 * fully on screen at any dock width. The component draws no state of its own;
 * Consult passes the values and the menu's body.
 */
import { useLayoutEffect, useRef, type ReactNode } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/ui/popover'
import { cn } from '@/lib/cn'

/** The tallest the question box grows before it scrolls. */
const BOX_MAX_PX = 120

function Switch({
  on,
  onChange,
  disabled,
  testId,
  title,
  children,
}: {
  on: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  testId: string
  title?: string
  children: ReactNode
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={() => onChange(!on)}
      disabled={disabled}
      title={title}
      data-testid={testId}
      className={cn(
        'flex shrink-0 items-center gap-1.5 rounded-control border px-2 py-0.5 text-label disabled:opacity-50',
        on ? 'border-point/60 text-ink-1' : 'border-line text-ink-3 hover:text-ink-1',
      )}
    >
      <span aria-hidden className={cn('inline-block h-2 w-2 rounded-full', on ? 'bg-point' : 'border border-ink-3')} />
      {children}
    </button>
  )
}

export function ConsultComposer({
  compact,
  point,
  draft,
  onDraft,
  onSend,
  placeholder,
  canAsk,
  busy,
  deep,
  onDeep,
  recordOnly,
  onRecordOnly,
  recordOnlyHint,
  stop,
  menu,
  nonDefault,
}: {
  compact: boolean
  point: ReactNode
  draft: string
  onDraft: (v: string) => void
  onSend: () => void
  placeholder: string
  canAsk: boolean
  busy: boolean
  deep: boolean
  onDeep: (v: boolean) => void
  recordOnly: boolean
  onRecordOnly: (v: boolean) => void
  recordOnlyHint?: string
  /** The Stop control, shown beside Ask only while a turn is in flight. */
  stop: ReactNode
  /** The body of the "⋯" menu. */
  menu: ReactNode
  /** Short phrases for every menu setting that is not at its default. */
  nonDefault: readonly string[]
}) {
  const summary = nonDefault.join(' · ')
  // The box is one line tall and grows with what is typed (to a cap), so the
  // answers above it keep the room.
  const box = useRef<HTMLTextAreaElement | null>(null)
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    if (el.scrollHeight > 0) el.style.height = `${Math.min(el.scrollHeight, BOX_MAX_PX)}px`
  }, [draft])
  return (
    <div className="space-y-1" data-testid="consult-composer">
      <div
        className="flex flex-wrap items-center gap-x-2 gap-y-1"
        data-testid="consult-composer-controls"
      >
        <div className="min-w-[10rem] flex-1">{point}</div>
        {/* Folded to the ask bar, only the point and the menu stay. */}
        {!compact && <Switch
          on={deep}
          onChange={onDeep}
          disabled={busy}
          testId="consult-deep-toggle"
          title={
            deep
              ? 'Deep is on: the question runs as a long task that writes a finding you can trace back to its sources'
              : 'Deep is off: a quick answer in this conversation. Turn on for a long task that writes a finding'
          }
        >
          Deep
        </Switch>}
        {!compact && <Switch on={recordOnly} onChange={onRecordOnly} disabled={busy} testId="consult-grounding" title={recordOnlyHint}>
          On record only
        </Switch>}
        <Popover>
          <PopoverTrigger asChild>
            <button
              type="button"
              className="flex shrink-0 items-center rounded-control border border-line px-1.5 py-0.5 text-ink-3 hover:text-ink-1"
              data-testid="consult-more"
              aria-label="More Consult options"
              title={summary ? `More options. Not at their defaults: ${summary}` : 'More options'}
            >
              <MoreHorizontal size={14} aria-hidden />
            </button>
          </PopoverTrigger>
          <PopoverContent side="top" align="end" className="max-h-[70vh] w-80 space-y-3 overflow-y-auto" data-testid="consult-more-menu">
            {menu}
          </PopoverContent>
        </Popover>
      </div>
      {!compact && summary && (
        <p className="truncate text-label text-ink-3" title={summary} data-testid="consult-nondefault">
          Changed: {summary}
        </p>
      )}
      <div className="flex items-stretch gap-2" data-testid="consult-question-row">
        <textarea
          className="block min-h-[2rem] min-w-0 flex-1 resize-none rounded border border-line bg-surface-200 px-2 py-1.5 text-sm"
          ref={box}
          rows={1}
          value={draft}
          onChange={(e) => onDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              onSend()
            }
          }}
          placeholder={placeholder}
          aria-label="Your question"
          data-testid="consult-question"
        />
        <div className="flex shrink-0 flex-col justify-end gap-1">
          <button
            type="button"
            onClick={onSend}
            disabled={busy || !canAsk}
            className="shrink-0 rounded-control border border-point/60 px-4 py-1 text-body text-ink-1 hover:bg-point/15 disabled:opacity-50"
            data-testid="consult-submit"
          >
            {busy ? 'Asking…' : 'Ask'}
          </button>
          {stop}
        </div>
      </div>
    </div>
  )
}
