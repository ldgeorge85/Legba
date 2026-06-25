/**
 * Casework store (v4 Wave 3) — orchestrator-owned contract + persistence.
 *
 * The /case room is a freeform Excalidraw board where the analyst pins typed
 * cards (signal / finding / entity / situation / source / target / analyst) and
 * draws typed edges (supports / contradicts / derived_from). Pins come from the
 * other rooms via the global selection. Everything persists to localStorage so a
 * case survives reloads — there is no backend casework store (by design, v1).
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type CaseCardKind =
  | 'signal'
  | 'finding'
  | 'entity'
  | 'situation'
  | 'source'
  | 'target'
  | 'analyst'

export interface CaseCard {
  /** Stable board id (derived from kind+refId so pinning is idempotent). */
  id: string
  kind: CaseCardKind
  /** The underlying descriptor / substrate-row id. */
  refId: string
  label: string
}

export type CaseRelation = 'supports' | 'contradicts' | 'derived_from'

export interface CaseEdge {
  id: string
  /** CaseCard.id */
  from: string
  /** CaseCard.id */
  to: string
  relation: CaseRelation
}

/** Card tint by kind (dark-theme friendly). */
export const CASE_KIND_COLOR: Record<CaseCardKind, string> = {
  signal: '#60a5fa',
  finding: '#fcd34d',
  entity: '#a78bfa',
  situation: '#34d399',
  source: '#3b82f6',
  target: '#10b981',
  analyst: '#f59e0b',
}

/** Typed-edge color by relation. */
export const RELATION_COLOR: Record<CaseRelation, string> = {
  supports: '#55ff55',
  contradicts: '#ff5555',
  derived_from: '#5599ff',
}

interface CaseState {
  cards: CaseCard[]
  edges: CaseEdge[]
  /** Opaque Excalidraw scene blob ({elements, appState}) for freeform layout. */
  scene: unknown | null

  /** Pin a ref; idempotent on (kind, refId). Returns the (existing or new) id. */
  addCard: (card: Omit<CaseCard, 'id'>) => string
  removeCard: (id: string) => void
  hasCard: (kind: CaseCardKind, refId: string) => boolean
  addEdge: (edge: Omit<CaseEdge, 'id'>) => void
  removeEdge: (id: string) => void
  setScene: (scene: unknown) => void
  clear: () => void
}

const cardId = (kind: string, refId: string) => `card_${kind}_${refId}`

export const useCaseStore = create<CaseState>()(
  persist(
    (set, get) => ({
      cards: [],
      edges: [],
      scene: null,

      addCard: (card) => {
        const id = cardId(card.kind, card.refId)
        if (!get().cards.some((c) => c.id === id)) {
          set((s) => ({ cards: [...s.cards, { ...card, id }] }))
        }
        return id
      },
      removeCard: (id) =>
        set((s) => ({
          cards: s.cards.filter((c) => c.id !== id),
          edges: s.edges.filter((e) => e.from !== id && e.to !== id),
        })),
      hasCard: (kind, refId) => get().cards.some((c) => c.id === cardId(kind, refId)),
      addEdge: (edge) =>
        set((s) => {
          const id = `edge_${edge.from}__${edge.to}__${edge.relation}`
          if (s.edges.some((e) => e.id === id)) return s
          return { edges: [...s.edges, { ...edge, id }] }
        }),
      removeEdge: (id) => set((s) => ({ edges: s.edges.filter((e) => e.id !== id) })),
      setScene: (scene) => set({ scene }),
      clear: () => set({ cards: [], edges: [], scene: null }),
    }),
    { name: 'legba-v4-casework' },
  ),
)
