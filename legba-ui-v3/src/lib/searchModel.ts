/**
 * UI-6 (Tier G) — global-search data model.
 *
 * Pure, DOM-free helpers for the cross-substrate search panel
 * (`system.search`). The panel fans out to several substrate-read
 * endpoints (findings / situations / entities / sources), normalises each
 * hit into a common `SearchHit` shape, then ranks + facet-filters the
 * merged set client-side. Keeping the merge/rank/facet logic here lets it
 * be unit-tested without a DOM (same split as `@/lib/findingsViews`).
 */

import { selectRow } from '@/state/selection'

/** The kinds of substrate row global search spans. */
export type SearchKind = 'finding' | 'situation' | 'entity' | 'source'

export const SEARCH_KINDS: readonly SearchKind[] = [
  'finding',
  'situation',
  'entity',
  'source',
] as const

/** A normalised search hit — one row from any substrate kind. */
export interface SearchHit {
  kind: SearchKind
  id: string
  title: string
  snippet: string
  /** Optional scoping facets used by the facet filter UI. */
  target_id: string | null
  /** Authoring analyst id. Findings carry one (drives the analyst-set facet,
   *  mirroring the server `analyst_id_in`); other kinds may omit it. */
  analyst_id?: string | null
  owner_tenant: string | null
  severity: string | null
  /** ISO timestamp used for recency ranking; null sorts last. */
  produced_at: string | null
  /** Relevance score (0..1), assigned by `rankHits`. */
  score: number
}

/** Facet selections applied client-side over the merged hit set. */
export interface SearchFacets {
  kinds: ReadonlySet<SearchKind>
  target_id: string
  owner_tenant: string
  severity: string // 'all' | low | medium | high | critical
  /** Orphan reachability: when true keep ONLY NULL-target hits — the ~1115
   *  unreachable findings (world_assessor reads + thematic proposals) that no
   *  country view surfaces. Mirrors the server `target_id_null=true` finding
   *  facet. */
  orphans_only: boolean
  /** Comma-separated analyst-id allow-list (mirrors the server `analyst_id_in`
   *  facet). Empty = all analysts. */
  analyst_id: string
}

export const DEFAULT_FACETS: SearchFacets = {
  kinds: new Set(SEARCH_KINDS),
  target_id: '',
  owner_tenant: '',
  severity: 'all',
  orphans_only: false,
  analyst_id: '',
}

// ---------------------------------------------------------------------------
// Normalisers — map each endpoint's row shape into a SearchHit.
// Defensive: every field is optional on the wire, so coalesce.
// ---------------------------------------------------------------------------

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}
function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null
}

export function normFinding(row: Record<string, unknown>): SearchHit {
  return {
    kind: 'finding',
    id: str(row.id),
    title: str(row.title) || str(row.summary) || '(untitled finding)',
    snippet: str(row.body) || str(row.summary),
    target_id: strOrNull(row.target_id),
    analyst_id: strOrNull(row.analyst_id),
    owner_tenant: strOrNull(row.owner_tenant),
    severity: strOrNull(row.severity),
    produced_at: strOrNull(row.produced_at),
    score: 0,
  }
}

export function normSituation(row: Record<string, unknown>): SearchHit {
  return {
    kind: 'situation',
    id: str(row.id),
    title: str(row.summary) || str(row.title) || '(untitled situation)',
    snippet: str(row.summary),
    target_id: strOrNull(row.target_id),
    owner_tenant: strOrNull(row.owner_tenant),
    severity: strOrNull(row.severity),
    produced_at: strOrNull(row.opened_at) ?? strOrNull(row.produced_at),
    score: 0,
  }
}

export function normEntity(row: Record<string, unknown>): SearchHit {
  const name = str(row.name) || str(row.label) || str(row.canonical_name)
  return {
    kind: 'entity',
    id: str(row.id) || str(row.entity_id),
    title: name || '(unnamed entity)',
    snippet: str(row.entity_type) || str(row.type) || str(row.summary),
    target_id: strOrNull(row.target_id),
    owner_tenant: strOrNull(row.owner_tenant),
    severity: null,
    produced_at: strOrNull(row.updated_at) ?? strOrNull(row.produced_at),
    score: 0,
  }
}

export function normSource(row: Record<string, unknown>): SearchHit {
  return {
    kind: 'source',
    id: str(row.descriptor_id) || str(row.id),
    title: str(row.name) || str(row.descriptor_id) || '(unnamed source)',
    snippet: str(row.kind) || str(row.state),
    target_id: null,
    owner_tenant: strOrNull(row.owner_tenant),
    severity: null,
    produced_at: null,
    score: 0,
  }
}

// ---------------------------------------------------------------------------
// Rank + facet.
// ---------------------------------------------------------------------------

/** Lowercased, whitespace-split query terms (deduped, empties dropped). */
export function queryTerms(q: string): string[] {
  return Array.from(
    new Set(
      q
        .toLowerCase()
        .split(/\s+/)
        .map((t) => t.trim())
        .filter(Boolean),
    ),
  )
}

/**
 * Relevance score for a hit against query terms.
 *
 *  - +0.5 per term matched in the title (title hits weigh more)
 *  - +0.2 per term matched in the snippet
 *  - +0.1 per term matched in target_id / id (identifier hits)
 *
 * Capped at 1.0. With no terms, everything scores 0.5 (browse mode).
 */
export function scoreHit(hit: SearchHit, terms: string[]): number {
  if (terms.length === 0) return 0.5
  const title = hit.title.toLowerCase()
  const snippet = hit.snippet.toLowerCase()
  const ident = `${hit.id} ${hit.target_id ?? ''} ${hit.analyst_id ?? ''}`.toLowerCase()
  let s = 0
  for (const t of terms) {
    if (title.includes(t)) s += 0.5
    else if (snippet.includes(t)) s += 0.2
    else if (ident.includes(t)) s += 0.1
  }
  return Math.min(1, s)
}

/** Parse a comma-separated analyst-id allow-list into a trimmed, non-empty set. */
export function analystIdSet(csv: string): Set<string> {
  return new Set(
    csv
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
  )
}

/** Does the hit pass the active facet selections? */
export function passesFacets(hit: SearchHit, f: SearchFacets): boolean {
  if (!f.kinds.has(hit.kind)) return false
  // Orphan reachability — keep only NULL-target hits. Kinds that carry no
  // target (e.g. sources) report `target_id === null` and so pass.
  if (f.orphans_only && hit.target_id !== null) return false
  if (f.target_id && (hit.target_id ?? '') !== f.target_id) return false
  if (f.owner_tenant && (hit.owner_tenant ?? '') !== f.owner_tenant) return false
  if (f.severity !== 'all' && (hit.severity ?? '') !== f.severity) return false
  if (f.analyst_id) {
    const allow = analystIdSet(f.analyst_id)
    // A non-empty allow-list keeps only hits authored by one of those analysts;
    // kinds without an analyst id are excluded while the filter is active.
    if (allow.size > 0 && !(hit.analyst_id != null && allow.has(hit.analyst_id))) {
      return false
    }
  }
  return true
}

/**
 * Merge + dedup (by kind:id) + facet-filter + score + sort.
 *
 * When the query is non-empty, hits scoring 0 are dropped (no term matched
 * anything). Sort is by score desc, then recency desc (null last).
 */
export function rankHits(
  hits: SearchHit[],
  query: string,
  facets: SearchFacets,
): SearchHit[] {
  const terms = queryTerms(query)
  const seen = new Set<string>()
  const out: SearchHit[] = []
  for (const h of hits) {
    const key = `${h.kind}:${h.id}`
    if (seen.has(key)) continue
    seen.add(key)
    if (!passesFacets(h, facets)) continue
    const score = scoreHit(h, terms)
    if (terms.length > 0 && score === 0) continue
    out.push({ ...h, score })
  }
  out.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score
    const ta = a.produced_at ? Date.parse(a.produced_at) : -Infinity
    const tb = b.produced_at ? Date.parse(b.produced_at) : -Infinity
    return tb - ta
  })
  return out
}

/** Per-kind counts over a hit set — drives the facet pill badges. */
export function kindCounts(hits: SearchHit[]): Record<SearchKind, number> {
  const out: Record<SearchKind, number> = {
    finding: 0,
    situation: 0,
    entity: 0,
    source: 0,
  }
  for (const h of hits) out[h.kind] += 1
  return out
}

/** Toggle a kind in the facet set, returning a new set. */
export function toggleKind(
  kinds: ReadonlySet<SearchKind>,
  kind: SearchKind,
): Set<SearchKind> {
  const next = new Set(kinds)
  if (next.has(kind)) next.delete(kind)
  else next.add(kind)
  return next
}

/**
 * Drive the unified selection store from a hit click (redesign Move 2) — opens
 * the Inspector + brushes every room. Replaces the former `legba:open-lineage`
 * `hitOpenEvent` window dispatch.
 */
export function selectHit(hit: SearchHit): void {
  selectRow(hit.kind, hit.id, hit.title, { origin: 'search' })
}
