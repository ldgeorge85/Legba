/**
 * The Case — Excalidraw casework canvas (v4 Wave 3).
 *
 * A freeform Excalidraw board over the orchestrator-owned `useCaseStore`. Cards
 * pinned from the other rooms are seeded onto the canvas as typed, tinted
 * rectangles (idempotent — diffed by `customData.cardId`); the analyst draws
 * typed edges between them whose stroke color is picked from the floating
 * relation toolbar. The whole scene persists to localStorage (via the store's
 * `setScene`) so a case survives reloads.
 *
 * API notes (against the INSTALLED @excalidraw/excalidraw@0.18.1):
 *  - `excalidrawAPI` prop hands us the `ExcalidrawImperativeAPI` once mounted;
 *    we stash it in a ref and treat it as null until then.
 *  - `convertToExcalidrawElements(skeleton[])` inflates lightweight skeletons
 *    (here: `type:"rectangle"` containers with a bound text `label`) into full
 *    `OrderedExcalidrawElement`s. `customData` and `backgroundColor` ride on the
 *    skeleton via `ElementConstructorOpts`.
 *  - `api.updateScene({ elements, appState })` replaces the scene; `appState` is
 *    a partial `Pick<AppState, K>`, so we can nudge a single field
 *    (`currentItemStrokeColor`) without disturbing the rest.
 *  - `api.getSceneElements()` / `api.getAppState()` read the live scene for
 *    persistence. We persist only a serializable subset of appState (no
 *    collaborators / transient interaction state).
 */
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { Excalidraw, convertToExcalidrawElements, ROUNDNESS } from '@excalidraw/excalidraw'
import type {
  AppState,
  ExcalidrawImperativeAPI,
  ExcalidrawInitialDataState,
} from '@excalidraw/excalidraw/types'
import type { ExcalidrawElementSkeleton } from '@excalidraw/excalidraw/data/transform'
import '@excalidraw/excalidraw/index.css'
import { Link2, Trash2 } from 'lucide-react'
import { cn } from '@/lib/cn'
import {
  useCaseStore,
  CASE_KIND_COLOR,
  RELATION_COLOR,
  type CaseCard,
  type CaseRelation,
} from './caseStore'

/** Card box geometry + grid layout for newly-seeded cards. */
const CARD_W = 160
const CARD_H = 70
const GRID_COLS = 4
const GRID_GAP_X = 40
const GRID_GAP_Y = 40
const GRID_ORIGIN_X = 120
const GRID_ORIGIN_Y = 120

/** Debounce window for persisting the scene back to the store (ms). */
const PERSIST_DEBOUNCE_MS = 600

const RELATION_LABEL: Record<CaseRelation, string> = {
  supports: 'Supports',
  contradicts: 'Contradicts',
  derived_from: 'Derived from',
}
const RELATIONS: CaseRelation[] = ['supports', 'contradicts', 'derived_from']

/** A minimal element view exposing the fields we read for card diffing. */
interface ElementWithCustomData {
  customData?: Record<string, unknown> | null
}

/** Pull the pinned cardId we tagged onto an element, if any. */
function cardIdOf(el: ElementWithCustomData): string | undefined {
  const raw = el.customData?.cardId
  return typeof raw === 'string' ? raw : undefined
}

/** Grid position for the n-th newly added card. */
function gridXY(index: number): { x: number; y: number } {
  const col = index % GRID_COLS
  const row = Math.floor(index / GRID_COLS)
  return {
    x: GRID_ORIGIN_X + col * (CARD_W + GRID_GAP_X),
    y: GRID_ORIGIN_Y + row * (CARD_H + GRID_GAP_Y),
  }
}

/** Build the skeleton for a single pinned card at a grid slot. */
function cardSkeleton(card: CaseCard, slot: number): ExcalidrawElementSkeleton {
  const { x, y } = gridXY(slot)
  return {
    type: 'rectangle',
    x,
    y,
    width: CARD_W,
    height: CARD_H,
    backgroundColor: CASE_KIND_COLOR[card.kind],
    strokeColor: '#0f172a',
    roundness: { type: ROUNDNESS.ADAPTIVE_RADIUS },
    customData: { cardId: card.id, kind: card.kind, refId: card.refId },
    label: {
      text: card.label,
      fontSize: 16,
      strokeColor: '#0f172a',
    },
  }
}

/**
 * Distil the live appState into a serializable subset safe to persist.
 * Deliberately drops transient/interaction state (collaborators, selection,
 * editing handles, etc.) — we keep only viewport + the active item defaults so a
 * reload restores roughly where the analyst left off.
 */
function persistableAppState(s: AppState): Partial<AppState> {
  return {
    viewBackgroundColor: s.viewBackgroundColor,
    currentItemStrokeColor: s.currentItemStrokeColor,
    currentItemBackgroundColor: s.currentItemBackgroundColor,
    scrollX: s.scrollX,
    scrollY: s.scrollY,
    zoom: s.zoom,
    theme: s.theme,
    gridSize: s.gridSize,
  }
}

export default function CaseBoard() {
  const apiRef = useRef<ExcalidrawImperativeAPI | null>(null)
  const persistTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Subscribe to the pinned cards so the canvas reseeds as rooms pin refs.
  const cards = useCaseStore((s) => s.cards)

  // Snapshot the persisted scene ONCE for restore — reading it reactively would
  // fight our own onChange persistence. The store is the source of truth across
  // reloads, but within a session the live Excalidraw scene leads.
  const initialScene = useRef<unknown>(useCaseStore.getState().scene)

  const initialData = useMemo<ExcalidrawInitialDataState | null>(() => {
    const scene = initialScene.current
    if (scene && typeof scene === 'object') {
      return scene as ExcalidrawInitialDataState
    }
    return null
  }, [])

  const hadInitialScene = initialData !== null

  /** Add any pinned cards not yet present on the canvas (idempotent). */
  const seedMissingCards = useCallback((cardList: CaseCard[]) => {
    const api = apiRef.current
    if (!api) return

    const current = api.getSceneElements()
    const present = new Set<string>()
    for (const el of current) {
      const id = cardIdOf(el as ElementWithCustomData)
      if (id) present.add(id)
    }

    const missing = cardList.filter((c) => !present.has(c.id))
    if (missing.length === 0) return

    // Start the layout grid below whatever is already on the board so freshly
    // pinned cards don't land on top of existing ones.
    const existingCardCount = present.size
    const skeletons = missing.map((card, i) => cardSkeleton(card, existingCardCount + i))
    const added = convertToExcalidrawElements(skeletons)

    api.updateScene({ elements: [...current, ...added] })
  }, [])

  // Seed on (re)mount and whenever the pinned card set changes. The api may not
  // be ready on the very first run; the excalidrawAPI callback effect re-seeds.
  useEffect(() => {
    seedMissingCards(cards)
  }, [cards, seedMissingCards])

  /** Debounced persist of the current scene back into the store. */
  const handleChange = useCallback(() => {
    const api = apiRef.current
    if (!api) return
    if (persistTimer.current) clearTimeout(persistTimer.current)
    persistTimer.current = setTimeout(() => {
      const live = apiRef.current
      if (!live) return
      useCaseStore.getState().setScene({
        elements: live.getSceneElements(),
        appState: persistableAppState(live.getAppState()),
      })
    }, PERSIST_DEBOUNCE_MS)
  }, [])

  useEffect(() => {
    return () => {
      if (persistTimer.current) clearTimeout(persistTimer.current)
    }
  }, [])

  /** Type the next-drawn arrow by setting the active stroke color. */
  const pickRelation = useCallback((relation: CaseRelation) => {
    const api = apiRef.current
    if (!api) return
    api.updateScene({
      appState: { currentItemStrokeColor: RELATION_COLOR[relation] },
    })
  }, [])

  const clearCase = useCallback(() => {
    if (!window.confirm('Clear this case? All pinned cards and links will be removed.')) {
      return
    }
    useCaseStore.getState().clear()
    const api = apiRef.current
    if (api) api.updateScene({ elements: [] })
  }, [])

  const showEmptyOverlay = cards.length === 0 && !hadInitialScene

  return (
    <div className="relative h-full w-full bg-surface-300">
      {/* Floating relation toolbar (top-left over the canvas). */}
      <div className="absolute left-3 top-3 z-10 flex flex-col gap-2 rounded-lg border border-slate-800 bg-surface-200/95 p-2 shadow-lg backdrop-blur">
        <div className="flex items-center gap-1.5 px-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-500">
          <Link2 className="h-3 w-3" />
          Link type
        </div>
        <div className="flex flex-col gap-1">
          {RELATIONS.map((rel) => (
            <button
              key={rel}
              type="button"
              onClick={() => pickRelation(rel)}
              title={`Draw the next link as "${RELATION_LABEL[rel]}"`}
              className="group flex items-center gap-2 rounded px-2 py-1 text-left text-xs text-slate-300 transition-colors hover:bg-surface-50"
            >
              <span
                className="h-3 w-3 shrink-0 rounded-sm ring-1 ring-inset ring-black/40"
                style={{ backgroundColor: RELATION_COLOR[rel] }}
              />
              <span className="group-hover:text-slate-100">{RELATION_LABEL[rel]}</span>
            </button>
          ))}
        </div>
        <div className="mt-0.5 border-t border-slate-800 pt-1.5">
          <button
            type="button"
            onClick={clearCase}
            className={cn(
              'flex w-full items-center gap-2 rounded px-2 py-1 text-xs',
              'text-slate-400 transition-colors hover:bg-rose-950/40 hover:text-rose-300',
            )}
          >
            <Trash2 className="h-3 w-3 shrink-0" />
            Clear case
          </button>
        </div>
      </div>

      {/* Empty-state hint (only when there is genuinely nothing to show). */}
      {showEmptyOverlay && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center px-6">
          <p className="max-w-sm text-center text-sm text-slate-500">
            Pin signals, findings, and entities from the other rooms to start a case.
          </p>
        </div>
      )}

      <div className="h-full w-full">
        <Excalidraw
          theme="dark"
          initialData={initialData}
          excalidrawAPI={(api) => {
            apiRef.current = api
            // The api can arrive after the first cards-effect ran; reseed now
            // so cards present at mount appear without waiting for a change.
            seedMissingCards(useCaseStore.getState().cards)
          }}
          onChange={handleChange}
        />
      </div>
    </div>
  )
}
