/**
 * P-UI6-A. Global Search (`system.search`) — UI-6 (Tier G).
 *
 * There is no backend `/search` endpoint (confirmed 404), so global search is
 * composed client-side. It fans out — in parallel — to the live substrate-read
 * endpoints and merges them into a common `SearchHit`:
 *   - signals    : GET /signals  (target/language scoped — the headline lane)
 *   - findings   : GET /findings
 *   - situations : GET /situations
 *   - sources    : GET /registry/sources
 *
 * Signals are the source-first substrate's atom and are TARGET-AGNOSTIC: the
 * `target_id` scope filters by the target's `scope.geo`, and `language` filters
 * by the signal language — both pushed to the server query so the merge stays
 * cheap. Findings / situations / sources are normalised via the DOM-free,
 * unit-tested helpers in `@/lib/searchModel`; signals are normalised here
 * (the shared model predates the signal kind).
 *
 * Ranking + facet-filtering of the merged set is client-side. A hit click
 * fires `legba:open-lineage` (same cross-panel pattern as the Findings feed) so
 * the Lineage panel can walk its provenance — for the kinds it supports
 * (finding · signal · situation).
 *
 * Endpoints that 404 on the running rig degrade gracefully (that kind
 * contributes zero hits); the panel never hard-fails on a missing read.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'
import {
  selectHit,
  normFinding,
  normSituation,
  normSource,
  passesFacets,
  queryTerms,
  scoreHit,
  type SearchFacets,
  type SearchHit,
  type SearchKind,
} from '@/lib/searchModel'

const SEVERITY_OPTIONS = ['all', 'low', 'medium', 'high', 'critical'] as const

/**
 * The kinds this panel spans. `signal` is layered on top of the shared
 * `SearchKind` union (which the panel-owned model predates) — it behaves as a
 * full search kind locally without touching the cross-panel model.
 */
type PanelKind = SearchKind | 'signal'
const PANEL_KINDS: readonly PanelKind[] = ['signal', 'finding', 'situation', 'source'] as const

/** A SearchHit whose `kind` is widened to the panel superset (adds `signal`). */
type PanelHit = Omit<SearchHit, 'kind'> & { kind: PanelKind }

/** Lineage walk is only meaningful for kinds the Lineage panel resolves. */
const LINEAGE_KINDS = new Set<PanelKind>(['finding', 'signal', 'situation'])

/**
 * Facets used by this panel. Mirrors the model's `SearchFacets` but widens
 * `kinds` to the panel's superset (`signal` included). Helpers from the model
 * (`passesFacets`) read `kinds.has(...)`, so a structural cast at the call site
 * is safe — the widened set is a runtime-compatible superset.
 */
interface PanelFacets extends Omit<SearchFacets, 'kinds'> {
  kinds: ReadonlySet<PanelKind>
}

const DEFAULT_PANEL_FACETS: PanelFacets = {
  kinds: new Set(PANEL_KINDS),
  target_id: '',
  owner_tenant: '',
  severity: 'all',
}

/** Best-effort GET — resolves to [] on any error so one dead endpoint can't sink search. */
async function softGet(path: string): Promise<Record<string, unknown>[]> {
  try {
    const r = await apiGet<unknown>(path)
    if (Array.isArray(r)) return r as Record<string, unknown>[]
    // findings / signals page shape: { data: [...] }
    if (r && typeof r === 'object' && Array.isArray((r as { data?: unknown }).data)) {
      return (r as { data: Record<string, unknown>[] }).data
    }
    return []
  } catch {
    return []
  }
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}
function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null
}
function arr(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => String(x)) : []
}

/** Normalise a signal row into a PanelHit (the model predates this kind). */
function normSignal(row: Record<string, unknown>): PanelHit {
  const geo = arr(row.geo)
  const tags = arr(row.tags)
  const snippetBits = [str(row.source_id), geo.join(' '), tags.join(' ')].filter(Boolean)
  return {
    kind: 'signal',
    id: str(row.id),
    title: str(row.title) || '(untitled signal)',
    snippet: snippetBits.join(' · '),
    target_id: strOrNull(row.target_id),
    owner_tenant: null,
    severity: null,
    produced_at: strOrNull(row.produced_at) ?? strOrNull(row.event_timestamp),
    score: 0,
  }
}

interface ScopeQuery {
  query: string
  target_id: string
  language: string
}

async function fanout(scope: ScopeQuery): Promise<PanelHit[]> {
  const q = scope.query ? `&q=${encodeURIComponent(scope.query)}` : ''
  const tgt = scope.target_id ? `&target_id=${encodeURIComponent(scope.target_id)}` : ''
  const lang = scope.language ? `&language=${encodeURIComponent(scope.language)}` : ''
  const [signals, findings, situations, sources] = await Promise.all([
    softGet(`/signals?limit=100${tgt}${lang}`),
    softGet(`/findings?limit=100${tgt}${q}`),
    softGet('/situations?limit=100'),
    softGet('/registry/sources'),
  ])
  // Model normalisers return SearchHit (narrower kind) — assignable to PanelHit.
  return [
    ...signals.map(normSignal),
    ...findings.map(normFinding),
    ...situations.map(normSituation),
    ...sources.map(normSource),
  ]
}

/** Per-kind counts over a hit set — drives the facet pill badges. */
function panelKindCounts(hits: PanelHit[]): Record<PanelKind, number> {
  const out: Record<PanelKind, number> = { signal: 0, finding: 0, situation: 0, source: 0, entity: 0 }
  for (const h of hits) out[h.kind] = (out[h.kind] ?? 0) + 1
  return out
}

/** Merge + dedup + facet-filter + score + sort (signal-aware superset of the model's rankHits). */
function rankPanelHits(hits: PanelHit[], query: string, facets: PanelFacets): PanelHit[] {
  const terms = queryTerms(query)
  const seen = new Set<string>()
  const out: PanelHit[] = []
  for (const h of hits) {
    const key = `${h.kind}:${h.id}`
    if (seen.has(key)) continue
    seen.add(key)
    // Model helpers read kind only as a string; the cast is runtime-safe.
    if (!passesFacets(h as SearchHit, facets as SearchFacets)) continue
    const score = scoreHit(h as SearchHit, terms)
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

function togglePanelKind(kinds: ReadonlySet<PanelKind>, kind: PanelKind): Set<PanelKind> {
  const next = new Set(kinds)
  if (next.has(kind)) next.delete(kind)
  else next.add(kind)
  return next
}

export default function GlobalSearchPanel({ registration, scope }: PanelProps) {
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [language, setLanguage] = useState('')
  const [facets, setFacets] = useState<PanelFacets>(DEFAULT_PANEL_FACETS)

  // The bound target (if any) seeds the target_id scope facet.
  const boundTarget = scope.target_id ?? ''
  const targetScope = facets.target_id || boundTarget

  const scopeQuery: ScopeQuery = { query, target_id: targetScope, language }

  const { data, isFetching, error, refetch } = useQuery<PanelHit[]>({
    queryKey: ['global-search', query, targetScope, language],
    queryFn: () => fanout(scopeQuery),
  })

  const allHits = data ?? []
  const counts = useMemo(() => panelKindCounts(allHits), [allHits])
  const ranked = useMemo(() => rankPanelHits(allHits, query, facets), [allHits, query, facets])

  function submit(e: React.FormEvent) {
    e.preventDefault()
    setQuery(draft.trim())
  }

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${ranked.length} result${ranked.length === 1 ? '' : 's'}${
        query ? ` for “${query}”` : ' (browse)'
      }${targetScope ? ` · ${targetScope}` : ''}`}
      onRefresh={() => refetch()}
    >
      <form onSubmit={submit} className="flex items-center gap-2 mb-2 text-xs">
        <input
          className="flex-1 bg-surface-200 border border-slate-700 rounded p-1.5 px-2"
          placeholder="search signals · findings · situations · sources…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          data-testid="search-input"
          autoFocus
        />
        <button
          type="submit"
          className="bg-surface-200 hover:bg-surface-300 border border-slate-700 rounded px-3 py-1.5"
          data-testid="search-submit"
        >
          search
        </button>
      </form>

      {/* kind facet pills */}
      <div className="flex items-center gap-1.5 mb-2 flex-wrap text-[11px]">
        {PANEL_KINDS.map((k) => {
          const on = facets.kinds.has(k)
          return (
            <button
              key={k}
              onClick={() => setFacets((f) => ({ ...f, kinds: togglePanelKind(f.kinds, k) }))}
              className={`px-2 py-0.5 rounded border ${
                on ? 'border-accent-info text-accent-info' : 'border-slate-700 text-slate-500'
              }`}
              data-testid={`search-facet-${k}`}
            >
              {k} ({counts[k]})
            </button>
          )
        })}
      </div>

      {/* scope facets */}
      <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
        <input
          className="flex-1 min-w-[110px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder={boundTarget ? `target_id (${boundTarget})…` : 'target_id scope…'}
          value={facets.target_id}
          onChange={(e) => setFacets((f) => ({ ...f, target_id: e.target.value }))}
          data-testid="search-facet-target"
        />
        <input
          className="w-28 bg-surface-200 border border-slate-700 rounded p-1 px-2"
          placeholder="language…"
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          data-testid="search-facet-language"
        />
        <select
          className="bg-surface-200 border border-slate-700 rounded p-1 px-2"
          value={facets.severity}
          onChange={(e) => setFacets((f) => ({ ...f, severity: e.target.value }))}
          data-testid="search-facet-severity"
        >
          {SEVERITY_OPTIONS.map((s) => (
            <option key={s} value={s}>
              severity: {s}
            </option>
          ))}
        </select>
        {isFetching && <span className="text-slate-500">searching…</span>}
      </div>

      {error instanceof Error && (
        <div className="text-rose-400 text-sm mb-2">error: {error.message}</div>
      )}

      <div className="flex-1 overflow-auto space-y-1.5 text-xs" data-testid="search-results">
        {ranked.map((hit) => (
          <HitCard key={`${hit.kind}:${hit.id}`} hit={hit} />
        ))}
        {ranked.length === 0 && !isFetching && (
          <div className="text-slate-500 text-sm py-4 text-center">
            {query ? 'no results match' : 'type a query or adjust facets'}
          </div>
        )}
      </div>
    </PanelChrome>
  )
}

const KIND_COLOR: Record<PanelKind, string> = {
  signal: 'bg-sky-900 text-sky-200',
  finding: 'bg-emerald-900 text-emerald-200',
  situation: 'bg-amber-900 text-amber-200',
  source: 'bg-violet-900 text-violet-200',
  entity: 'bg-slate-700 text-slate-200',
}

function HitCard({ hit }: { hit: PanelHit }) {
  const kind = hit.kind
  const walkable = LINEAGE_KINDS.has(kind)
  return (
    <button
      onClick={() => walkable && selectHit(hit as SearchHit)}
      className={`w-full text-left bg-surface-100 border border-slate-800 rounded p-2 block ${
        walkable ? 'hover:bg-surface-200 cursor-pointer' : 'cursor-default'
      }`}
      title={walkable ? 'open lineage walk' : 'no lineage walk for this kind'}
      data-testid={`search-hit-${hit.kind}-${hit.id}`}
    >
      <div className="flex items-center gap-2">
        <span className={`shrink-0 rounded px-1 ${KIND_COLOR[kind]}`}>{hit.kind}</span>
        <span className="text-slate-200 font-medium truncate">{hit.title}</span>
        {hit.severity && <span className="shrink-0 text-slate-400">[{hit.severity}]</span>}
        <span className="ml-auto shrink-0 text-slate-600" title="relevance">
          {(hit.score * 100).toFixed(0)}%
        </span>
      </div>
      {hit.snippet && <div className="mt-1 text-slate-400 line-clamp-2">{hit.snippet}</div>}
      <div className="mt-1 text-slate-600 flex items-center gap-2">
        {hit.target_id && <span className="font-mono">{hit.target_id}</span>}
        {hit.produced_at && <span>{new Date(hit.produced_at).toLocaleString()}</span>}
        {walkable && <span className="ml-auto text-accent-info/70">lineage →</span>}
      </div>
    </button>
  )
}
