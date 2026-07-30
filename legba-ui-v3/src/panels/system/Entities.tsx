/**
 * Entities (`system.entities`) — the entity knowledge-graph roster.
 *
 * Source-first analogue of v2's Entities panel. Reads the entity substrate the
 * backfill/resolution pipeline populates:
 *   - GET /api/v1/entities?q=&entity_class=&limit=   (nodes + mention counts + geo)
 *   - GET /api/v1/entities/{id}                       (profile + linked signals + relationships)
 *
 * Search + class-facet filter, mention-count sort, expandable rows showing the
 * entity's recent signals (lineage-clickable) + its co-occurrence relationships.
 *
 * U-3 merge: two panels that were really "more entity views" now live here as
 * tabs, UNMODIFIED — `system.entity_graph` (the full knowledge-graph viz) as
 * "Graph", and `system.notable_structure` (the ranked cross-entity structural
 * shortlist — tense actors/brokers/hostile edges/triads/proxy chains) as
 * "Structure". Both mount inside `PanelEmbedProvider` so their own
 * (otherwise-standalone) `PanelChrome` header/border stays suppressed — this
 * panel's own header above is the ONLY chrome that renders (the "double
 * chrome" fix, same mechanism as `panels/merged/*.tsx`). Both kinds stay
 * registered (hidden from the sidebar) pointing at the SAME original
 * components — see panel-registry/registry.ts HIDDEN_KINDS — so a saved
 * layout referencing either old id keeps resolving exactly as before.
 */
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { apiGet } from '@/lib/api'
import { resolveCountry } from '@/lib/countryGeo'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import EntityGraphPanel from './EntityGraph'
import NotableStructurePanel from './NotableStructure'

type EntitiesTab = 'list' | 'graph' | 'structure'

const ENTITIES_TABS: readonly PanelTabDef[] = [
  { id: 'list', label: 'List' },
  { id: 'graph', label: 'Graph' },
  { id: 'structure', label: 'Structure' },
]

interface EntityNode {
  id: string
  canonical_name: string
  entity_class: string
  entity_type: string
  mentions: number
  geo_country: string | null
}
interface EntitiesPage {
  data: EntityNode[]
  total: number
}
interface EntitySignalRef {
  id: string
  title: string | null
  source_id: string | null
  produced_at: string | null
  role: string
}
interface EntityRelationship {
  other: string
  relationship_type: string
  confidence: number
  direction: 'in' | 'out'
  evidence_text: string
}
interface EntityDetailResp {
  node: EntityNode
  signals: EntitySignalRef[]
  relationships: EntityRelationship[]
}

const CLASS_COLOR: Record<string, string> = {
  person: '#f59e0b',
  organization: '#60a5fa',
  location: '#10b981',
  event: '#a78bfa',
  entity: '#94a3b8',
}

/**
 * The NER pipeline classes country mentions as the generic `entity` class with
 * no geo. When a generic-`entity` row resolves to a recognized country
 * (lib/countryGeo), promote it to the `location` class and backfill the country
 * name so it colors + facets correctly and surfaces its geo. Backend-resolved
 * rows (already `location` or carrying `geo_country`) pass through untouched.
 */
function geoEnrich<T extends { entity_class: string; canonical_name: string; geo_country: string | null }>(
  e: T,
): T {
  if (e.entity_class !== 'entity' || e.geo_country) return e
  const fix = resolveCountry(e.canonical_name)
  if (!fix) return e
  return { ...e, entity_class: 'location', geo_country: fix.name }
}

function openLineage(rowId: string, title?: string) {
  // Redesign Move 2: unified selection store (opens the Inspector).
  selectRow('signal', rowId, title, { origin: 'entities' })
}

function openEntityGraph(name: string) {
  // Redesign Move 2: selecting an entity brushes the entity graph (which now
  // subscribes to the shared selection store) AND opens the Inspector.
  useSelection.getState().select({ kind: 'entity', id: name, label: name, origin: 'entities' })
}

export default function EntitiesPanel({ registration, scope, mode }: PanelProps) {
  const [tab, setTab] = useState<EntitiesTab>('list')
  const [q, setQ] = useState('')
  const [cls, setCls] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)

  // The class facet filters client-side (not via `&entity_class=`): country
  // entities are re-classed `location` in the browser, so a server-side filter
  // would miss the promoted rows. We still pass `q` to the server.
  const listQ = useQuery<EntitiesPage>({
    queryKey: ['entities', q],
    queryFn: () =>
      apiGet<EntitiesPage>(
        `/entities?limit=200${q ? `&q=${encodeURIComponent(q)}` : ''}`,
      ),
    refetchInterval: 60_000,
  })

  // Geo-enrich: promote generic-`entity` country mentions to `location` + geo.
  const enriched = useMemo(() => (listQ.data?.data ?? []).map(geoEnrich), [listQ.data])

  const classes = useMemo(() => {
    const s = new Set<string>()
    for (const e of enriched) s.add(e.entity_class)
    return [...s].sort()
  }, [enriched])

  const rows = useMemo(
    () => (cls ? enriched.filter((e) => e.entity_class === cls) : enriched),
    [enriched, cls],
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={tab === 'list' ? `${rows.length} shown · ${listQ.data?.total ?? 0} entities` : undefined}
      onRefresh={tab === 'list' ? () => listQ.refetch() : undefined}
      actions={
        <div className="flex items-center gap-2">
          <PanelTabStrip
            tabs={ENTITIES_TABS}
            active={tab}
            onChange={(id) => setTab(id as EntitiesTab)}
            ariaLabel="Entities surface"
            testIdPrefix="entities-tab"
          />
          {tab === 'list' && (
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="search entities…"
              className="bg-surface-200 border border-slate-700 rounded px-2 py-0.5 text-[11px] w-40"
              data-testid="entities-search"
            />
          )}
        </div>
      }
    >
      {tab === 'list' && (
        <div className="flex-1 overflow-auto text-xs">
          {/* class facet chips */}
          <div className="flex items-center gap-1 mb-2 flex-wrap" data-testid="entities-class-filter">
            <Chip label="all" on={cls === null} onClick={() => setCls(null)} />
            {classes.map((c) => (
              <Chip key={c} label={c} on={cls === c} color={CLASS_COLOR[c]} onClick={() => setCls(cls === c ? null : c)} />
            ))}
          </div>

          {listQ.isLoading && <div className="text-slate-500 py-4 text-center">loading entities…</div>}
          {!listQ.isLoading && rows.length === 0 && (
            <div className="text-slate-500 py-4 text-center" data-testid="entities-empty">
              no entities — the entity graph populates as the NER + linking pipeline runs
            </div>
          )}

          <div className="space-y-0.5">
            {rows.map((e) => (
              <div key={e.id} className="border border-slate-800 rounded bg-surface-100">
                <button
                  onClick={() => setOpen(open === e.id ? null : e.id)}
                  className="w-full flex items-center gap-2 px-2 py-1 hover:bg-surface-200 text-left"
                  data-testid={`entities-row-${e.id}`}
                >
                  <span className="w-2 h-2 rounded-full shrink-0" style={{ background: CLASS_COLOR[e.entity_class] ?? '#94a3b8' }} />
                  <span className="text-slate-200 flex-1 truncate">{e.canonical_name}</span>
                  {e.geo_country && <span className="text-[10px] text-emerald-400">{e.geo_country}</span>}
                  <span className="text-[10px] text-slate-500">{e.entity_class}</span>
                  <span className="text-[10px] text-slate-400 tabular-nums">{e.mentions}×</span>
                </button>
                {open === e.id && <EntityDetail id={e.id} name={e.canonical_name} />}
              </div>
            ))}
          </div>
        </div>
      )}
      {/* This panel's own PanelChrome header above already carries the tab
          strip, so EntityGraphPanel/NotableStructurePanel — each otherwise a
          standalone panel with its own PanelChrome — mount embedded, per the
          same "double chrome" fix `panels/merged/*.tsx` uses. */}
      <PanelEmbedProvider>
        {tab === 'graph' && <EntityGraphPanel registration={registration} scope={scope} mode={mode} />}
        {tab === 'structure' && <NotableStructurePanel registration={registration} scope={scope} mode={mode} />}
      </PanelEmbedProvider>
    </PanelChrome>
  )
}

function EntityDetail({ id, name }: { id: string; name: string }) {
  const detailQ = useQuery<EntityDetailResp>({
    queryKey: ['entity-detail', id],
    queryFn: () => apiGet<EntityDetailResp>(`/entities/${encodeURIComponent(id)}`),
  })
  if (detailQ.isLoading) return <div className="px-3 py-2 text-slate-500">loading…</div>
  const d = detailQ.data
  if (!d) return <div className="px-3 py-2 text-rose-400">failed to load</div>
  return (
    <div className="px-3 py-2 border-t border-slate-800 space-y-2">
      <button
        onClick={() => openEntityGraph(name)}
        className="text-[10px] border border-slate-700 rounded px-1.5 py-0.5 text-slate-300 hover:bg-surface-200"
        data-testid={`entities-open-graph-${id}`}
      >
        open in graph →
      </button>
      {d.relationships.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-0.5">relationships ({d.relationships.length})</div>
          <div className="flex flex-wrap gap-1">
            {d.relationships.slice(0, 24).map((r, i) => (
              <span key={i} className="text-[10px] bg-surface-200 border border-slate-700 rounded px-1.5 py-0.5 text-slate-300" title={r.evidence_text}>
                {r.direction === 'out' ? '→' : '←'} {r.other} <span className="text-slate-500">({Math.round(r.confidence * 100)}%)</span>
              </span>
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="text-[10px] uppercase tracking-wide text-slate-500 mb-0.5">mentioned in ({d.signals.length})</div>
        <div className="space-y-0.5">
          {d.signals.map((s) => (
            <button
              key={s.id}
              onClick={() => openLineage(s.id, s.title ?? undefined)}
              className="block w-full text-left text-[11px] text-slate-400 hover:text-slate-200 truncate"
            >
              · {s.title ?? '(untitled)'} <span className="text-slate-600">{s.source_id}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function Chip({ label, on, color, onClick }: { label: string; on: boolean; color?: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`text-[10px] rounded px-1.5 py-0.5 border flex items-center gap-1 ${on ? 'border-slate-500 text-slate-200' : 'border-slate-800 text-slate-500'}`}
      style={on && color ? { borderColor: color } : undefined}
    >
      {color && <span className="w-2 h-2 rounded-full inline-block" style={{ background: color, opacity: on ? 1 : 0.4 }} />}
      {label}
    </button>
  )
}
