/**
 * Notable Structure (`system.notable_structure`) — the #99 overlay.
 *
 * Surfaces the ranked `interesting` shortlist the graph-analysis handlers
 * (structural_balance + graph_mining) distil every run and persist to
 * graph_metrics. Reads GET /api/v1/graph/structure and lists the items
 * (tense actors, brokers, new-hostile edges, sign-imbalanced triads, proxy
 * chains) with their rationale + score.
 *
 * Selection-aware (mirrors the findings-follows-selection keystone): when a
 * country/entity is selected, the endpoint scopes+prioritises items touching
 * it (matched first, then the rest as context).
 */
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import type { PanelProps } from '@/types'
import { useSelection } from '@/state/selection'

interface StructureItem {
  kind: string
  label: string
  score: number
  rationale: string
  entities: string[]
  source: string
}
interface StructureResp {
  data: StructureItem[]
  scoped_entity: string | null
  computed_at: string | null
}

// Per-kind glyph + accent so the operator can scan the shortlist by shape.
const KIND_META: Record<string, { glyph: string; tint: string; label: string }> = {
  tense_actor: { glyph: '⚡', tint: '#f59e0b', label: 'tense actor' },
  broker: { glyph: '◆', tint: '#60a5fa', label: 'broker' },
  new_hostile_edge: { glyph: '⚔', tint: '#f87171', label: 'new hostile edge' },
  sign_imbalanced_triad: { glyph: '△', tint: '#a78bfa', label: 'imbalanced triad' },
  proxy_chain: { glyph: '⇢', tint: '#fb923c', label: 'proxy chain' },
  unknown: { glyph: '•', tint: '#94a3b8', label: 'structure' },
}

function metaFor(kind: string) {
  return KIND_META[kind] ?? KIND_META.unknown
}

/** The shared selection scopes the shortlist when it names an actor/country. */
function scopeFromSelection(sel: { kind: string; id: string; label?: string } | null): string | null {
  if (!sel) return null
  if (sel.kind === 'entity' || sel.kind === 'target') return sel.label ?? sel.id
  return null
}

export default function NotableStructurePanel({ registration }: PanelProps) {
  const selection = useSelection((s) => s.selection)
  const scope = scopeFromSelection(selection)

  const q = useQuery<StructureResp>({
    queryKey: ['graph-structure', scope],
    queryFn: () =>
      apiGet<StructureResp>(`/graph/structure?limit=40${scope ? `&entity=${encodeURIComponent(scope)}` : ''}`),
    refetchInterval: 120_000,
  })

  const items = q.data?.data ?? []
  const scopeNeedle = (q.data?.scoped_entity ?? '').toLowerCase()

  const subtitle = useMemo(() => {
    const n = items.length
    if (scope) return `${n} item(s) · scoped to ${scope}`
    return `${n} item(s) · ranked by structural interest`
  }, [items.length, scope])

  return (
    <PanelChrome registration={registration} subtitle={subtitle} onRefresh={() => q.refetch()}>
      <div className="flex-1 overflow-auto p-2" data-testid="notable-structure">
        {q.isLoading && <div className="text-slate-500 text-sm p-2">loading notable structure…</div>}
        {!q.isLoading && items.length === 0 && (
          <div className="text-slate-500 text-sm p-2" data-testid="notable-structure-empty">
            no notable structure yet — the graph-analysis handlers populate this as nexuses accrue
          </div>
        )}
        <ul className="space-y-1.5">
          {items.map((it, i) => {
            const m = metaFor(it.kind)
            const onScope =
              scopeNeedle.length > 0 &&
              (it.label.toLowerCase().includes(scopeNeedle) ||
                it.entities.some((e) => e.toLowerCase().includes(scopeNeedle)))
            return (
              <li
                key={`${it.kind}-${it.label}-${i}`}
                data-testid="notable-structure-item"
                className={`rounded border px-2 py-1.5 ${
                  onScope ? 'border-slate-500 bg-surface-200' : 'border-slate-800 bg-surface-100'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span style={{ color: m.tint }} className="text-sm" title={m.label}>
                    {m.glyph}
                  </span>
                  <span className="text-xs font-medium text-slate-200 truncate flex-1" title={it.label}>
                    {it.label}
                  </span>
                  <span className="text-[10px] tabular-nums text-slate-400">{it.score.toFixed(2)}</span>
                </div>
                <div className="mt-0.5 flex items-center gap-2">
                  <span
                    className="text-[9px] uppercase tracking-wide px-1 rounded"
                    style={{ color: m.tint, border: `1px solid ${m.tint}55` }}
                  >
                    {m.label}
                  </span>
                  <span className="text-[10px] text-slate-500">{it.source}</span>
                </div>
                <p className="mt-1 text-[11px] leading-snug text-slate-400">{it.rationale}</p>
              </li>
            )
          })}
        </ul>
      </div>
    </PanelChrome>
  )
}
