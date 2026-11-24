/**
 * SurfaceBar — the top bar (U-2 shell, kept by the U-6 frame; 04 §2 "The
 * common frame"). 2.75 rem, fixed frame, on every page.
 *
 *   READER    ◆ Legba │ Board  Subject  Ledger  Watch │ [subject ▾] │ ⌘K ＋ ↶ ↺ 🔗 │ Operator ▸
 *   OPERATOR  ◆ Legba │ ◂ Reader │ Engine  Evaluation  Gate  Investigate │       │ ⌘K ＋ ↶ ↺ 🔗
 *
 * The two sides never share a tab row (the owner: "we shouldn't mix the infra
 * and technical bits with the actual workstation usage side"): the operator
 * side is one clearly separate entry at the right end of the reader's bar.
 *
 * Every control compiles to the shell's own calls: the tabs to `arrange`
 * through the ONE executor, Reset and Copy link to the shell commands. The
 * window lives in ONE place, the time band under the surface (the one clock's
 * own control), never a second set of chips here. The bar owns no
 * arrangement state.
 */
import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Link2, Plus, RotateCcw, Search, ArrowLeft, Undo2, Wrench } from 'lucide-react'
import type { Mode } from '@/types'
import { cn } from '@/lib/cn'
import { OPERATOR_SURFACES, READER_SURFACES, findSurface, type SurfaceId } from '@/lib/surfaces'
import { useSelection } from '@/state/selection'
import { useScope } from '@/state/scope'
import { PROFILE_UI } from '@/lib/profileUi'
import { SubjectRoster } from '@/components/ObjectRail'
import { CatalogPopover } from '@/components/CatalogPopover'
import { OPERATOR_SIDE_REASON, useBelowContributor } from '@/auth/roles'

export interface SurfaceBarProps {
  mode: Mode
  active: SurfaceId
  onSwitch: (id: SurfaceId) => void
  onReset: () => void
  onCopyLink: () => void
  onOpenPalette: () => void
  /** Undo the last view change (a move, an open, a pulled-up record); none when nothing to undo. */
  onUndo?: () => void
  undoLabel?: string | null
  /** One line the shell needs the reader to see (a reset, a copied link). */
  notice?: string | null
}

/** The reader's point as the bar names it: the selected or scoped subject. */
function usePointSubject(): string | null {
  const selection = useSelection((s) => s.selection)
  const scope = useScope((s) => s.scope)
  if (selection?.kind === 'target') return selection.label ?? selection.id
  if (scope?.kind === 'target') return scope.label ?? scope.id
  return null
}

function SubjectSwitcher() {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const point = usePointSubject()
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])
  const noun = PROFILE_UI.subject.singular.toLowerCase()
  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        data-testid="subject-switcher"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        title={`Choose a ${noun}`}
        className={cn(
          'flex h-7 max-w-[14rem] items-center gap-1.5 rounded-control border px-2 text-body',
          point ? 'border-point/50 text-ink-1' : 'border-line text-ink-3 hover:text-ink-1',
        )}
      >
        {point && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-point" aria-hidden />}
        <span className="truncate" data-testid="subject-switcher-label">
          {point ?? `Choose a ${noun}`}
        </span>
        <ChevronDown size={13} className="shrink-0 text-ink-3" aria-hidden />
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={`Choose a ${noun}`}
          data-testid="subject-switcher-popover"
          className="absolute left-0 top-full z-50 mt-1 max-h-[70vh] w-72 overflow-y-auto border border-line-strong bg-surf-4 py-1 shadow-[0_8px_24px_rgba(0,0,0,0.35)]"
        >
          <SubjectRoster onPicked={() => setOpen(false)} />
        </div>
      )}
    </div>
  )
}

function IconButton({
  testId,
  title,
  onClick,
  children,
  expanded,
}: {
  testId: string
  title: string
  onClick: () => void
  children: React.ReactNode
  expanded?: boolean
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      title={title}
      aria-label={title}
      aria-expanded={expanded}
      onClick={onClick}
      className="flex h-7 w-7 items-center justify-center rounded-control text-ink-3 hover:bg-surf-3 hover:text-ink-1"
    >
      {children}
    </button>
  )
}

export function SurfaceBar({ mode, active, onSwitch, onReset, onCopyLink, onOpenPalette, onUndo, undoLabel, notice }: SurfaceBarProps) {
  const def = findSurface(active)
  const side = def?.side ?? 'reader'
  const tabs = side === 'reader' ? READER_SURFACES : OPERATOR_SURFACES
  const [catalogOpen, setCatalogOpen] = useState(false)
  const readerOnly = useBelowContributor()
  return (
    <header
      className="relative flex shrink-0 items-center gap-3 border-b border-line bg-surf-base px-3"
      style={{ height: 'var(--frame-top)' }}
      data-testid="surface-bar"
      data-side={side}
    >
      <div className="flex shrink-0 items-baseline gap-2">
        <span className="font-serif text-heading font-semibold tracking-tight text-ink-1">Legba</span>
        {side === 'operator' && (
          <span className="text-label uppercase tracking-[0.06em] text-ink-3" data-testid="operator-side-mark">
            operator
          </span>
        )}
      </div>

      {side === 'operator' && (
        <button
          type="button"
          data-testid="to-reader-side"
          onClick={() => onSwitch('board')}
          title="Back to the reader side"
          className="flex h-7 shrink-0 items-center gap-1 rounded-control border border-line px-2 text-body text-ink-2 hover:text-ink-1"
        >
          <ArrowLeft size={13} aria-hidden />
          Reader
        </button>
      )}

      <nav role="tablist" aria-label={side === 'reader' ? 'Reader surfaces' : 'Operator sections'} className="flex items-center">
        {tabs.map((s) => {
          const on = s.id === active
          return (
            <button
              key={s.id}
              type="button"
              role="tab"
              aria-selected={on}
              data-testid={`surface-tab-${s.id}`}
              title={`${s.question}  ·  Alt+${s.key}`}
              onClick={() => onSwitch(s.id)}
              className={cn(
                'relative h-[var(--frame-top)] px-3 text-body-lg transition-colors',
                on ? 'text-ink-1' : 'text-ink-3 hover:text-ink-1',
              )}
            >
              {s.label}
              {on && <span aria-hidden className="absolute inset-x-3 bottom-0 h-[2px] bg-ink-1" />}
            </button>
          )
        })}
      </nav>

      {side === 'reader' && (
        <div className="flex min-w-0 items-center gap-3">
          <SubjectSwitcher />
        </div>
      )}

      <div className="ml-auto flex min-w-0 items-center gap-1">
        {notice && (
          <span className="mr-2 truncate text-label text-ink-3" data-testid="surface-notice" title={notice}>
            {notice}
          </span>
        )}
        <button
          type="button"
          data-testid="open-palette"
          onClick={onOpenPalette}
          title="Search everything (Ctrl/Cmd+K)"
          className="flex h-7 items-center gap-2 rounded-control border border-line px-2 text-body text-ink-3 hover:text-ink-1"
        >
          <Search size={13} aria-hidden />
          <span className="hidden xl:inline">Search</span>
          <kbd className="font-mono text-label text-ink-3">⌘K</kbd>
        </button>
        <div className="relative">
          <IconButton
            testId="open-catalog"
            title={side === 'reader' ? 'Swap in a reader view' : 'Open any view'}
            expanded={catalogOpen}
            onClick={() => setCatalogOpen((o) => !o)}
          >
            <Plus size={15} aria-hidden />
          </IconButton>
          {catalogOpen && <CatalogPopover mode={mode} side={side} onClose={() => setCatalogOpen(false)} />}
        </div>
        {onUndo && (
          <button
            type="button"
            data-testid="view-undo"
            onClick={onUndo}
            disabled={!undoLabel}
            title={undoLabel ? `Undo: ${undoLabel} (Ctrl/Cmd+Z)` : 'Nothing to undo'}
            aria-label={undoLabel ? `Undo: ${undoLabel}` : 'Nothing to undo'}
            className="flex h-7 w-7 items-center justify-center rounded-control text-ink-3 hover:bg-surf-3 hover:text-ink-1 disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <Undo2 size={14} aria-hidden />
          </button>
        )}
        <IconButton testId="surface-reset" title="Reset this page to its default (Alt+Shift+R)" onClick={onReset}>
          <RotateCcw size={14} aria-hidden />
        </IconButton>
        <IconButton testId="copy-view-link" title="Copy a link to this view" onClick={onCopyLink}>
          <Link2 size={14} aria-hidden />
        </IconButton>
        {side === 'reader' && (
          <button
            type="button"
            data-testid="to-operator-side"
            // RA-1: a reader has no way across — a plain reason, no request
            disabled={readerOnly}
            data-reason={readerOnly ? 'role' : undefined}
            onClick={() => { if (!readerOnly) onSwitch('engine') }}
            title={readerOnly ? OPERATOR_SIDE_REASON : 'The operator side: engine, evaluation internals, the gate, investigation'}
            className="ml-2 flex h-7 shrink-0 items-center gap-1.5 rounded-control border border-line px-2 text-body text-ink-3 hover:text-ink-1 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:text-ink-3"
          >
            <Wrench size={13} aria-hidden />
            Operator
          </button>
        )}
      </div>
    </header>
  )
}
