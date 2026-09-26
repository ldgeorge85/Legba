/**
 * Live-Feed view store — the feed's OWN, feed-local filter/view state.
 *
 * ## Why this store exists (the feed's third defect)
 *
 * The feed used to DERIVE its server-side target filter straight from the
 * global {@link useSelection} store:
 *
 *     const serverTargetId = selection?.kind === 'target' ? selection.id : ''
 *
 * That coupling made the feed's filter a projection of "whatever is selected",
 * so the two things the operator does with a feed fought each other: picking a
 * desk in the sidebar filtered the feed (good), but then clicking any ROW to
 * read it in the Inspector flipped the selection to `kind: 'finding'`, the
 * derived target went empty, the query key changed, the page refetched, and the
 * desk filter, the loaded pages and the scroll position all vanished.
 *
 * The semantics are now split, and this store owns one half:
 *
 *   * **Feed row click → global selection ONLY.** It drives the Inspector and
 *     never touches anything in here. The feed just highlights the row.
 *
 * ## What replaced `seedDeskFilter` (WORKSTATION_V2_FLOW_DESIGN §3)
 *
 * A desk click used to reach in here and write a `target:` chip
 * (`seedDeskFilter`). That was a workaround for a missing channel: there was
 * nowhere to say "the wall is about this desk" except the feed's own filter
 * store, so a desk had to be smuggled in as a filter. `state/scope.ts` is that
 * channel, and the feed now MERGES the scope into its server params and renders
 * it as a pinned, removable chip ahead of the filter bar — visible and
 * reversible for the same reason the seeded chip was, but owned by the store
 * that means it. This store keeps exactly what it was always for: the feed's
 * OWN posture (stream, sort, hand-typed chips, scroll).
 *
 * ## Why a store rather than component state
 *
 * The feed is a Dockview panel: it unmounts when its tab is hidden. Component
 * state would drop the filters and the scroll offset every time the operator
 * flipped to another tab and back. This store is the panel's session-scoped
 * memory — filters, stream, sort, and the scroll offset survive an unmount, and
 * `sessionStorage` carries them across an in-session reload (deliberately NOT
 * `localStorage`: a filter set is a working posture, not a preference; the
 * durable form of one is a SAVED VIEW, which still persists to localStorage via
 * `feedFilters.persistFeedViews`).
 *
 * `scrollTop` is written on every scroll frame, so nothing should SUBSCRIBE to
 * it — read it imperatively with `useFeedView.getState().scrollTop`.
 */
import { create } from 'zustand'
import {
  DEFAULT_VIEW,
  parseFilterInput,
  serializeFilter,
  setChip,
  readViewHash,
  type FacetKey,
  type FeedSort,
  type FeedStream,
  type FeedViewState,
  type ParsedFilter,
} from '@/lib/feedFilters'

const STORAGE_KEY = 'legba_feed_view_v1'

/** The persisted slice — the view state plus the two display toggles and the
 *  scroll offset. Deliberately small and defensively parsed. */
interface PersistedFeedView extends FeedViewState {
  hideSuperseded: boolean
  live: boolean
  scrollTop: number
}

export interface FeedViewStore extends PersistedFeedView {
  /** The parsed form of `query` — what the panel and the FilterBar actually read. */
  filter: ParsedFilter
  setStream: (stream: FeedStream) => void
  setSort: (sort: FeedSort) => void
  setFilter: (filter: ParsedFilter) => void
  /** Set (or, with an empty value, CLEAR) one single-valued facet chip. */
  setFacet: (key: FacetKey, value: string) => void
  /**
   * Seed the desk/target chip from a sidebar (or map, or wall) desk selection.
   * A plain `target:` chip write — the operator can remove it, or replace it
   * from the filter bar, exactly as if they had set it themselves.
   */
  setHideSuperseded: (v: boolean) => void
  setLive: (v: boolean) => void
  setScrollTop: (v: number) => void
  /** Apply a whole saved view (stream + sort + filter) in one write. */
  applyView: (view: FeedViewState) => void
  /** Back to pristine defaults — used by tests and a "clear everything" action. */
  reset: () => void
}

const DEFAULTS: PersistedFeedView = {
  ...DEFAULT_VIEW,
  hideSuperseded: true,
  live: true,
  scrollTop: 0,
}

function isStream(v: unknown): v is FeedStream {
  return v === 'intelligence' || v === 'signals'
}
function isSort(v: unknown): v is FeedSort {
  return v === 'recency' || v === 'severity' || v === 'confidence'
}

/** Defensive parse of the persisted blob — any garbage degrades to defaults
 *  field-by-field rather than throwing a panel into its error boundary. */
export function parsePersistedFeedView(raw: string | null | undefined): PersistedFeedView {
  if (!raw) return { ...DEFAULTS }
  let o: Record<string, unknown>
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { ...DEFAULTS }
    o = parsed as Record<string, unknown>
  } catch {
    return { ...DEFAULTS }
  }
  return {
    stream: isStream(o.stream) ? o.stream : DEFAULTS.stream,
    sort: isSort(o.sort) ? o.sort : DEFAULTS.sort,
    query: typeof o.query === 'string' ? o.query : DEFAULTS.query,
    hideSuperseded: typeof o.hideSuperseded === 'boolean' ? o.hideSuperseded : DEFAULTS.hideSuperseded,
    live: typeof o.live === 'boolean' ? o.live : DEFAULTS.live,
    scrollTop:
      typeof o.scrollTop === 'number' && Number.isFinite(o.scrollTop) && o.scrollTop >= 0
        ? o.scrollTop
        : 0,
  }
}

/**
 * True when this tab's feed posture came from somewhere REAL — a `#view=`
 * deep-link or a session the operator has already been driving — rather than
 * from cold defaults.
 *
 * The feed reads it to decide one thing: whether mounting may adopt a target
 * that is ALREADY selected globally. On a pristine feed that adoption is the
 * keystone deep-link behaviour ("open a link to a desk → the feed shows that
 * desk"); on a restored one it would silently overwrite filters the operator
 * set earlier in the session, which is precisely the clobbering this rework
 * exists to end. Computed once at module load, like the initial state itself.
 */
export let FEED_VIEW_RESTORED = false

function loadInitial(): PersistedFeedView {
  // A `#view=` deep-link WINS over the session posture — following a shared
  // link must land on the linked view, not on whatever this tab was doing.
  let hashed: FeedViewState | null = null
  try {
    hashed = readViewHash()
  } catch {
    hashed = null
  }
  let raw: string | null = null
  let stored: PersistedFeedView
  try {
    raw = sessionStorage.getItem(STORAGE_KEY)
    stored = parsePersistedFeedView(raw)
  } catch {
    stored = { ...DEFAULTS }
  }
  FEED_VIEW_RESTORED = hashed !== null || raw !== null
  // A deep-link carries no scroll offset — start it at the top.
  return hashed ? { ...stored, ...hashed, scrollTop: 0 } : stored
}

function persist(s: PersistedFeedView): void {
  try {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        stream: s.stream,
        sort: s.sort,
        query: s.query,
        hideSuperseded: s.hideSuperseded,
        live: s.live,
        scrollTop: s.scrollTop,
      }),
    )
  } catch {
    /* sessionStorage unavailable (private mode / SSR) — persistence is best-effort. */
  }
}

const initial = loadInitial()

export const useFeedView = create<FeedViewStore>((set, get) => {
  /** Apply a patch, keep `query` ⇄ `filter` in lockstep, and persist. */
  const commit = (patch: Partial<FeedViewStore>): void => {
    set((prev) => {
      const next = { ...prev, ...patch }
      persist(next)
      return next
    })
  }

  return {
    ...initial,
    filter: parseFilterInput(initial.query),

    setStream: (stream) => commit({ stream }),
    setSort: (sort) => commit({ sort }),
    setFilter: (filter) => commit({ filter, query: serializeFilter(filter) }),
    setFacet: (key, value) => {
      const filter = { ...get().filter, chips: setChip(get().filter.chips, key, value) }
      commit({ filter, query: serializeFilter(filter) })
    },
    setHideSuperseded: (hideSuperseded) => commit({ hideSuperseded }),
    setLive: (live) => commit({ live }),
    // Scroll is high-frequency: skip the write when nothing moved so we don't
    // re-render every subscriber (and hammer sessionStorage) on each frame.
    setScrollTop: (scrollTop) => {
      if (get().scrollTop === scrollTop) return
      commit({ scrollTop })
    },
    applyView: (view) =>
      commit({ ...view, filter: parseFilterInput(view.query) }),
    reset: () => commit({ ...DEFAULTS, filter: parseFilterInput(DEFAULTS.query) }),
  }
})

/**
 * Test/teardown helper — pristine defaults, a cleared session blob, and the
 * "restored" flag back to cold so each test starts from a genuinely fresh feed.
 */
export function resetFeedView(): void {
  FEED_VIEW_RESTORED = false
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
  useFeedView.getState().reset()
  // `reset()` persists — clear again so the store really is cold on disk too.
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}
