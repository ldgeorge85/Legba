/**
 * The Why — shared contract (orchestrator-owned). Wave-2 agents code against
 * these. Lineage rendering reuses @/lib/graphModel (LineageReport / GraphElements
 * / kindColor); this only adds the thin provenance + assessment shapes.
 */
import type { RowKind } from '@/lib/graphModel'

/** A provenance reference, rendered as a clickable chip. */
export interface ProvenanceRef {
  kind: RowKind | 'source' | 'target' | 'analyst' | 'entity'
  id: string
  label?: string
  /** Producing analyst / target, when known. */
  via?: string
}

/** The world_assessor's latest one-pager, projected from its Finding. */
export interface WorldAssessment {
  id: string
  title: string
  /** Markdown body. */
  summary: string
  severity?: string
  /** epoch ms. */
  producedAt: number
}

/** v4 SelectionKind → the lineage row_kind for GET /lineage/{kind}/{id}.
 *  Only the lineage-walkable kinds map; entity/source/target/analyst don't. */
export const SELECTION_TO_ROW_KIND: Partial<Record<string, RowKind>> = {
  finding: 'finding',
  situation: 'situation',
  signal: 'signal',
}
