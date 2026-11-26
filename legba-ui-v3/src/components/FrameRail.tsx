/**
 * FrameRail — the slim left rail of every page (U-6; brief §12: "a slim left
 * rail (subjects, history, saved views)"). Collapsible to a strip of three
 * icons; open, it is 15 rem (`lib/frameGrid.ts`).
 *
 *   SUBJECTS        the roster, ranked by what changed since the reader last
 *                   looked, with the count (a click reads the subject: it
 *                   lands on the stage and becomes the point)
 *   CONVERSATIONS   Consult's prior chats; a click continues one in the chat
 *   SAVED VIEWS     links — a page, its point and its window (never an
 *                   arrangement); "Save this view" keeps the current link
 *
 * On the operator side the rail is the object rail (units, sources, queues).
 * Saved views are per-viewer links kept in this browser (a convenience; the
 * link itself is the saved view and can be copied anywhere).
 */
import { useCallback, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bookmark, ChevronLeft, ChevronRight, MessagesSquare, Users, X } from 'lucide-react'
import type { Mode } from '@/types'
import { SubjectRoster, ObjectRail } from '@/components/ObjectRail'
import { PROFILE_UI } from '@/lib/profileUi'
import { listConsultSessions, loadConsultSession } from '@/lib/api'
import { consultActions, useConsultPanel } from '@/state/consultSession'
import { CONSULT_PANEL_ID } from '@/lib/caseReplay'
import { drive } from '@/lib/applyAction'
import { parseAddress } from '@/lib/address'
import { cn } from '@/lib/cn'
import { useChangeCounts } from '@/v4/read/boardData'

export const SAVED_VIEWS_KEY = 'legba_saved_views'

export interface SavedView {
  name: string
  hash: string
  at: string
}

export function readSavedViews(): SavedView[] {
  try {
    const raw = localStorage.getItem(SAVED_VIEWS_KEY)
    const parsed = raw ? (JSON.parse(raw) as unknown) : []
    return Array.isArray(parsed)
      ? parsed.filter(
          (v): v is SavedView => !!v && typeof v === 'object' && typeof (v as SavedView).name === 'string' && typeof (v as SavedView).hash === 'string',
        )
      : []
  } catch {
    return []
  }
}

function writeSavedViews(views: SavedView[]): void {
  try {
    localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views.slice(0, 30)))
  } catch {
    // a per-viewer convenience
  }
}

type Section = 'subjects' | 'conversations' | 'views'

function SectionHead({ children, testId }: { children: React.ReactNode; testId: string }) {
  return (
    <div className="flex items-center justify-between px-3 pb-1 pt-3 text-label uppercase tracking-[0.06em] text-ink-3" data-testid={testId}>
      {children}
    </div>
  )
}

function Conversations() {
  // Read on demand, not with the landing: the list costs a request the reader
  // may never want (the landing's request budget, tools/walk/budgets.mjs).
  const [shown, setShown] = useState(false)
  if (!shown) {
    return (
      <button
        type="button"
        onClick={() => setShown(true)}
        data-testid="rail-conversations-show"
        className="mx-3 text-label text-ink-3 underline underline-offset-2 hover:text-ink-1"
      >
        Show prior conversations
      </button>
    )
  }
  return <ConversationList />
}

function ConversationList() {
  const q = useQuery({
    queryKey: ['frame-rail', 'consult-sessions'],
    queryFn: () => listConsultSessions({ mode: 'chat', limit: 12 }),
    staleTime: 60_000,
  })
  const current = useConsultPanel(CONSULT_PANEL_ID).sessionId
  const busy = !!useConsultPanel(CONSULT_PANEL_ID).pendingTurn
  const open = useCallback(
    async (id: string) => {
      if (busy) return
      try {
        consultActions().adoptSession(CONSULT_PANEL_ID, await loadConsultSession(id))
      } catch (err) {
        consultActions().setError(CONSULT_PANEL_ID, err instanceof Error ? err.message : String(err))
      }
    },
    [busy],
  )
  if (q.isLoading) return <p className="px-3 text-label text-ink-3">loading…</p>
  if (q.isError) return <p className="px-3 text-label text-ink-3">prior conversations could not be read</p>
  const rows = Array.isArray(q.data) ? q.data : []
  if (rows.length === 0) return <p className="px-3 text-label text-ink-3" data-testid="rail-conversations-empty">no prior conversations</p>
  return (
    <ul className="space-y-px" data-testid="rail-conversations">
      {rows.map((s) => (
        <li key={s.id}>
          <button
            type="button"
            onClick={() => void open(s.id)}
            disabled={busy}
            title={s.title || '(untitled)'}
            data-testid="rail-conversation"
            className={cn(
              'w-full truncate px-3 py-1 text-left text-body hover:bg-surf-3 disabled:opacity-50',
              s.id === current ? 'text-point' : 'text-ink-2 hover:text-ink-1',
            )}
          >
            {s.title || '(untitled)'}
          </button>
        </li>
      ))}
    </ul>
  )
}

function SavedViews({ pageLabel }: { pageLabel: string }) {
  const [views, setViews] = useState<SavedView[]>(() => readSavedViews())
  const save = () => {
    const hash = window.location.hash || ''
    const name = `${pageLabel}${hash.includes('scope=') || hash.includes('sel=') ? ' · ' + decodeURIComponent((/(?:scope|sel)=[^:&]*:([^&]+)/.exec(hash)?.[1] ?? '')).slice(0, 40) : ''}`
    const next = [{ name, hash, at: new Date().toISOString() }, ...views.filter((v) => v.hash !== hash)]
    setViews(next)
    writeSavedViews(next)
  }
  const remove = (hash: string) => {
    const next = views.filter((v) => v.hash !== hash)
    setViews(next)
    writeSavedViews(next)
  }
  return (
    <div>
      <ul className="space-y-px" data-testid="rail-saved-views">
        {views.map((v) => (
          <li key={v.hash} className="group flex items-center">
            <button
              type="button"
              className="min-w-0 flex-1 truncate px-3 py-1 text-left text-body text-ink-2 hover:bg-surf-3 hover:text-ink-1"
              title={`${v.name} — ${v.hash || 'the landing'}`}
              data-testid="rail-saved-view"
              onClick={() => {
                const parsed = parseAddress(v.hash)
                if (parsed.actions.length) drive(parsed.actions, { origin: 'saved-view' })
              }}
            >
              {v.name}
            </button>
            <button
              type="button"
              onClick={() => remove(v.hash)}
              className="mr-1 hidden rounded-control p-1 text-ink-3 hover:text-ink-1 group-hover:block"
              title="Forget this saved view"
              aria-label="Forget this saved view"
            >
              <X size={12} aria-hidden />
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        onClick={save}
        data-testid="rail-save-view"
        className="mx-3 mt-1 text-label text-ink-3 underline underline-offset-2 hover:text-ink-1"
        title="Keep a link to this page, its point and its window"
      >
        Save this view
      </button>
    </div>
  )
}

/** The rail's subjects, ranked by what changed since the reader last looked. */
function RankedSubjects() {
  const changes = useChangeCounts()
  return <SubjectRoster changes={changes} />
}

export function FrameRail({
  mode,
  side,
  open,
  onToggle,
  pageLabel,
  onOpenPalette,
}: {
  mode: Mode
  side: 'reader' | 'operator'
  open: boolean
  onToggle: (open: boolean) => void
  pageLabel: string
  onOpenPalette: () => void
}) {
  const [focus, setFocus] = useState<Section | null>(null)
  useEffect(() => {
    if (!focus || !open) return
    document.querySelector(`[data-testid="rail-section-${focus}"]`)?.scrollIntoView({ block: 'start' })
  }, [focus, open])
  const openAt = (s: Section) => {
    setFocus(s)
    onToggle(true)
  }
  return (
    <nav
      aria-label="Rail"
      data-testid="frame-rail"
      data-open={open ? 'true' : 'false'}
      className="flex h-full min-h-0 shrink-0 flex-col border-r border-line bg-surf-base"
      style={{ width: open ? 'var(--rail-open)' : 'var(--rail-shut)' }}
    >
      <div className={cn('flex h-8 shrink-0 items-center border-b border-line', open ? 'justify-end px-1' : 'justify-center')}>
        <button
          type="button"
          onClick={() => onToggle(!open)}
          data-testid="rail-toggle"
          title={open ? 'Fold the rail' : 'Open the rail'}
          aria-label={open ? 'Fold the rail' : 'Open the rail'}
          aria-expanded={open}
          className="flex h-6 w-6 items-center justify-center rounded-control text-ink-3 hover:bg-surf-3 hover:text-ink-1"
        >
          {open ? <ChevronLeft size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
        </button>
      </div>
      {!open ? (
        side === 'reader' ? (
          <div className="flex flex-col items-center gap-1 py-2">
            {(
              [
                ['subjects', Users, PROFILE_UI.subject.plural],
                ['conversations', MessagesSquare, 'Conversations'],
                ['views', Bookmark, 'Saved views'],
              ] as const
            ).map(([id, Icon, title]) => (
              <button
                key={id}
                type="button"
                onClick={() => openAt(id)}
                title={title}
                aria-label={title}
                data-testid={`rail-icon-${id}`}
                className="flex h-8 w-8 items-center justify-center rounded-control text-ink-3 hover:bg-surf-3 hover:text-ink-1"
              >
                <Icon size={15} aria-hidden />
              </button>
            ))}
          </div>
        ) : null
      ) : side === 'operator' ? (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <ObjectRail mode={mode} onOpenPalette={onOpenPalette} />
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto pb-3">
          <div data-testid="rail-section-subjects">
            <SectionHead testId="rail-head-subjects">{PROFILE_UI.subject.plural}</SectionHead>
            <RankedSubjects />
          </div>
          <div data-testid="rail-section-conversations">
            <SectionHead testId="rail-head-conversations">Conversations</SectionHead>
            <Conversations />
          </div>
          <div data-testid="rail-section-views">
            <SectionHead testId="rail-head-views">Saved views</SectionHead>
            <SavedViews pageLabel={pageLabel} />
          </div>
        </div>
      )}
    </nav>
  )
}
