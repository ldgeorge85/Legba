/**
 * frame — the frame's own state (U-6): where the chat sits and how much room
 * it has, per page; what the stage shows and its history; whether the rail is
 * open. Kept apart from the panel side's Dockview arrangement
 * (`lib/surfaces.ts`, one versioned slot per page).
 *
 *   CHAT    placement 'bottom' (under the stage — the default), 'left' or
 *           'right' (a column of its own), or 'full' (the whole centre, the
 *           stage one click away); `collapsed` shrinks it to the ask bar;
 *           `share` is the divider's position (the chat's share of the
 *           centre's height); `sideRem` the width of a side column.
 *           Remembered PER PAGE, in `legba_frame` under a version stamp: a
 *           stamp that differs, or a body that does not parse, is dropped for
 *           the defaults (the page's own `chat` declaration).
 *   STAGE   a stack of entries per page — the page's default first, then what
 *           was pulled up — and the index shown. Not stored: a page opens on
 *           its default (today's read on the Board).
 *   RAIL    open or shut; follows the width until the reader chooses.
 */
import { create } from 'zustand'
import { CHAT_DEFAULT_SHARE, CONSULT_SIDE_REM } from '@/lib/frameGrid'

export type ChatPlacement = 'bottom' | 'left' | 'right' | 'full'

export interface PageFrame {
  placement: ChatPlacement
  collapsed: boolean
  /** The chat's share of the centre's height (bottom placement). */
  share: number
  /** A side column's width (rem). */
  sideRem: number
  /** The centre's width override (rem), when the reader dragged the centre's edge. */
  centreRem: number | null
}

/**
 * Where a view sat in the panel side before it was sent to the stage, so
 * that going Back past it puts it back there: in the same region as `within`,
 * or, when it was alone in its region, beside the region holding `beside.ref`.
 */
export interface StageReturn {
  /** The dock id it had. */
  id: string
  /** It was one of the page's own (seeded) views, not an opened one. */
  seeded: boolean
  within?: string
  beside?: { ref: string; direction: 'left' | 'right' | 'above' | 'below' }
}

/** One thing on the stage. */
export interface StageEntry {
  /** The view id (`v4.morning_read`, `subject.page`, `view:forecasts`, a registered view). */
  view: string
  /** A bound read: the record it is bound to. */
  recordId?: string
  axis?: 'target' | 'analyst'
  tab?: string
  /** A record pulled up onto the stage (the Inspector follows it). */
  record?: { kind: string; id: string; label?: string }
  /** The breadcrumb's words. */
  title: string
  /** Who put it there: `page` (the default), `operator` (a click), `case` (Consult). */
  origin: string
  /** Set when it came from the panel side: where Back puts it again. */
  returnTo?: StageReturn
}

/** The stage instance id of an entry (also its key in the on-screen report). */
export function stageInstanceId(e: StageEntry): string {
  if (e.record) return `record:${e.record.kind}:${e.record.id}`
  return e.recordId ? `${e.view}:${e.recordId}` : e.view
}

export interface StageState {
  stack: StageEntry[]
  index: number
}

export const FRAME_STORE_KEY = 'legba_frame'
/** Bump when PageFrame's meaning changes; a stored body with another stamp is dropped. */
export const FRAME_STORE_VERSION = 'f1'

interface Stored {
  v: string
  pages: Record<string, Partial<PageFrame>>
  rail?: boolean
}

export function defaultPageFrame(chat: 'open' | 'collapsed' = 'open'): PageFrame {
  return { placement: 'bottom', collapsed: chat === 'collapsed', share: CHAT_DEFAULT_SHARE, sideRem: CONSULT_SIDE_REM, centreRem: null }
}

const PLACEMENTS: readonly ChatPlacement[] = ['bottom', 'left', 'right', 'full']

function sane(p: Partial<PageFrame> | undefined, chat: 'open' | 'collapsed'): PageFrame {
  const d = defaultPageFrame(chat)
  if (!p || typeof p !== 'object') return d
  return {
    placement: PLACEMENTS.includes(p.placement as ChatPlacement) ? (p.placement as ChatPlacement) : d.placement,
    collapsed: typeof p.collapsed === 'boolean' ? p.collapsed : d.collapsed,
    share: typeof p.share === 'number' && p.share > 0 && p.share < 1 ? p.share : d.share,
    sideRem: typeof p.sideRem === 'number' && p.sideRem >= 20 && p.sideRem <= 80 ? p.sideRem : d.sideRem,
    centreRem: typeof p.centreRem === 'number' && p.centreRem >= 24 && p.centreRem <= 120 ? p.centreRem : null,
  }
}

function readStored(): Stored | null {
  try {
    const raw = localStorage.getItem(FRAME_STORE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Stored
    if (!parsed || parsed.v !== FRAME_STORE_VERSION || typeof parsed.pages !== 'object') return null
    return parsed
  } catch {
    return null
  }
}

function writeStored(s: Stored): void {
  try {
    localStorage.setItem(FRAME_STORE_KEY, JSON.stringify(s))
  } catch {
    // a per-viewer convenience; the defaults are always there
  }
}

interface FrameState {
  pages: Record<string, PageFrame>
  /** null = follow the width. */
  railOpen: boolean | null
  stages: Record<string, StageState>
  /** Read a page's frame (its stored one, else its default). */
  page: (surface: string, chat?: 'open' | 'collapsed') => PageFrame
  setPage: (surface: string, patch: Partial<PageFrame>, chat?: 'open' | 'collapsed') => void
  resetPage: (surface: string) => void
  setRail: (open: boolean) => void
  /** Put a page's stage back on its default alone. */
  seedStage: (surface: string, entry: StageEntry) => void
  /** Show `entry` on the stage: the same entry already in the history is gone back to; else pushed after the current one. */
  pushStage: (surface: string, entry: StageEntry) => void
  goStage: (surface: string, index: number) => void
  setStage: (surface: string, stage: StageState) => void
  /** Load what the browser stored (boot, tests). */
  hydrate: () => void
}

export const useFrame = create<FrameState>((set, get) => ({
  pages: {},
  railOpen: null,
  stages: {},
  page: (surface, chat = 'open') => get().pages[surface] ?? sane(readStored()?.pages[surface], chat),
  setPage: (surface, patch, chat = 'open') => {
    const next = { ...get().page(surface, chat), ...patch }
    set((s) => ({ pages: { ...s.pages, [surface]: next } }))
    const stored = readStored() ?? { v: FRAME_STORE_VERSION, pages: {} }
    stored.pages[surface] = next
    if (get().railOpen !== null) stored.rail = get().railOpen!
    writeStored(stored)
  },
  resetPage: (surface) => {
    set((s) => {
      const pages = { ...s.pages }
      delete pages[surface]
      return { pages }
    })
    const stored = readStored()
    if (stored) {
      delete stored.pages[surface]
      writeStored(stored)
    }
  },
  setRail: (open) => {
    set({ railOpen: open })
    const stored = readStored() ?? { v: FRAME_STORE_VERSION, pages: {} }
    stored.rail = open
    writeStored(stored)
  },
  seedStage: (surface, entry) => set((s) => ({ stages: { ...s.stages, [surface]: { stack: [entry], index: 0 } } })),
  pushStage: (surface, entry) =>
    set((s) => {
      const cur = s.stages[surface] ?? { stack: [], index: -1 }
      const key = stageInstanceId(entry)
      const at = cur.stack.findIndex((e) => stageInstanceId(e) === key)
      if (at >= 0) {
        const stack = cur.stack.slice()
        stack[at] = { ...stack[at], ...entry, title: entry.title || stack[at].title }
        return { stages: { ...s.stages, [surface]: { stack, index: at } } }
      }
      // A breadcrumb, not a browser history: what was ahead of the current
      // entry is dropped, and the trail is capped at eight.
      const stack = [...cur.stack.slice(0, cur.index + 1), entry].slice(-8)
      return { stages: { ...s.stages, [surface]: { stack, index: stack.length - 1 } } }
    }),
  goStage: (surface, index) =>
    set((s) => {
      const cur = s.stages[surface]
      if (!cur || index < 0 || index >= cur.stack.length) return s
      return { stages: { ...s.stages, [surface]: { ...cur, index } } }
    }),
  setStage: (surface, stage) => set((s) => ({ stages: { ...s.stages, [surface]: stage } })),
  hydrate: () => {
    const stored = readStored()
    if (!stored && localStorage.getItem(FRAME_STORE_KEY)) {
      // a body from another version: dropped, so it can never steer the frame
      try {
        localStorage.removeItem(FRAME_STORE_KEY)
      } catch {
        // ignore
      }
    }
    // A fresh frame: the stages start on each page's default (they are never stored).
    set({ pages: {}, stages: {}, railOpen: typeof stored?.rail === 'boolean' ? stored.rail : null })
  },
}))

/** The current stage entry of a page, or null. */
export function currentStage(st: StageState | undefined): StageEntry | null {
  if (!st || st.index < 0) return null
  return st.stack[st.index] ?? null
}

/** Test/teardown helper. */
export function resetFrameState(): void {
  useFrame.setState({ pages: {}, railOpen: null, stages: {} })
}
