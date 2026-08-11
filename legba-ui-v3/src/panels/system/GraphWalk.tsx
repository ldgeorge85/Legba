/**
 * Graph Walk (`system.graph_walk`) — the K-G4 viewer verb.
 *
 * *"Walking the world graph, asking multi-hop questions interactively IS
 * basically the entire vision."* This panel is that walk: anchor on one actor,
 * see what is around it, click a neighbour to keep going, click an edge to see
 * why it exists.
 *
 * Reads `GET /graph/ego` + `GET /graph/edge/{id}`
 * (`src/legba/data/registry/graph_walk_api.py`) over the reified `entity_edges`
 * store. Distinct from `system.entity_graph`, which renders the older
 * `proposed_edges` projection as a top-N subgraph; this one walks the typed,
 * evidentiary edges one anchored hop at a time.
 *
 * Three decisions worth stating, because they are what keep it from becoming a
 * hairball:
 *
 *  1. **`cooccurrence` is off by default.** 8,722 of the graph's 12,566 edges
 *     are co-mentions. Drawing them by default would bury the 881 `relation`
 *     edges that actually assert something. It stays one chip away, and the
 *     disclosure strip always reports how many are being withheld — the view
 *     is filtered, never silently partial. Switching it ON adds density rather
 *     than replacing the claims, because `/graph/ego` spends its edge budget
 *     per family; a chip that removed what the operator came to read would be
 *     worse than one that did nothing.
 *  2. **Families are rendered as different KINDS of line**, not just different
 *     colours — solid claims, dashed membership, faint dotted co-mention (see
 *     `@/lib/graphWalkModel`). Polarity colours only `relation`, the one family
 *     where a sign means anything.
 *  3. **Expansion is per-click, never per-depth.** There is no "3 hops" button
 *     because the probe measured that question at 472 ms on a million edges;
 *     each click is one index-driven hop, and `known` is sent so the induced
 *     edges between nodes already on screen come back too (otherwise a dense
 *     graph draws as a tree).
 *
 * The cytoscape mount follows the #90 crash-safe pattern exactly: a stable
 * module-constant no-op preset layout, with the real `cose` run from the
 * resize observer once `useVisibleSize` says the Dockview tab is on-screen.
 */
import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type cytoscape from 'cytoscape'
import type { Core, ElementDefinition, StylesheetStyle } from 'cytoscape'
import CytoscapeComponent from 'react-cytoscapejs'
import { PanelChrome } from '@/components/PanelChrome'
import { GraphControls } from '@/components/GraphControls'
import { apiGet, ApiError } from '@/lib/api'
import { attachFitOnResize, useVisibleSize } from '@/lib/cytoscapeFit'
import { entityClassColor } from '@/lib/graphModel'
import {
  ALL_FAMILIES,
  EMPTY_CANVAS,
  FAMILY_STYLES,
  POLARITY_COLORS,
  buildWalkElements,
  drawnFamilyStats,
  egoQueryString,
  facetSummary,
  hiddenEdgeCount,
  hiddenFamilyRows,
  mergeEgo,
  seedCanvas,
  type EdgeEvidence,
  type EdgeFamily,
  type EgoResponse,
  type WalkCanvas,
} from '@/lib/graphWalkModel'
import type { PanelProps } from '@/types'
import { useSelection } from '@/state/selection'

/** Families drawn on first paint. See decision (1) in the header. */
const DEFAULT_FAMILIES: readonly EdgeFamily[] = ['relation', 'reference']

const EDGE_LIMIT = 80

const TIME_WINDOWS: { id: string; label: string; days: number | null }[] = [
  { id: 'all', label: 'all time', days: null },
  { id: '90d', label: '90 days', days: 90 },
  { id: '30d', label: '30 days', days: 30 },
  { id: '7d', label: '7 days', days: 7 },
]

const CONFIDENCE_STEPS = [0, 0.3, 0.5, 0.7]

const STYLESHEET: StylesheetStyle[] = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(color)',
      label: 'data(label)',
      'font-size': 10,
      color: '#e2e8f0',
      'text-valign': 'bottom',
      'text-halign': 'center',
      'text-margin-y': 3,
      'text-outline-color': '#0a0c10',
      'text-outline-width': 2,
      'min-zoomed-font-size': 4,
      width: 'data(size)',
      height: 'data(size)',
      'border-width': 1,
      'border-color': '#1e293b',
    },
  },
  // The anchor is the thing you are standing on — it must never be ambiguous.
  {
    selector: 'node[?anchor]',
    style: {
      'border-width': 3,
      'border-color': '#e2e8f0',
      'font-size': 12,
      'font-weight': 'bold',
    },
  },
  // A node the store holds more edges for than are drawn. The dashed ring is
  // the honest "clicking this reveals something" affordance.
  {
    selector: 'node[?expandable]',
    style: { 'border-width': 2, 'border-color': '#64748b', 'border-style': 'dashed' },
  },
  {
    selector: 'node[?expanded]',
    style: { 'border-width': 1, 'border-color': '#334155', 'border-style': 'solid' },
  },
  // An endpoint with no entity_profiles row — visibly unresolved, not dropped.
  {
    selector: 'node[!resolved]',
    style: { 'background-opacity': 0.35, 'border-style': 'dotted' },
  },
  {
    selector: 'edge',
    style: {
      width: 'data(w)',
      label: 'data(label)',
      'line-color': 'data(color)',
      'curve-style': 'bezier',
      'font-size': 9,
      color: '#cbd5e1',
      'text-rotation': 'autorotate',
      'text-outline-color': '#0a0c10',
      'text-outline-width': 2,
      'min-zoomed-font-size': 5,
    },
  },
  // Per-family line KIND and weight, generated from the shared model so the
  // canvas, the chips and the legend cannot drift apart. This is the rule that
  // keeps the three tiers legible as different things rather than one hairball
  // in three colours: solid claims, dashed membership, faint dotted co-mention.
  ...ALL_FAMILIES.map(
    (fam): StylesheetStyle => ({
      selector: `edge[edge_family="${fam}"]`,
      style: {
        'line-style': FAMILY_STYLES[fam].lineStyle,
        opacity: FAMILY_STYLES[fam].opacity,
      },
    }),
  ),
  { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#38bdf8' } },
  { selector: 'edge:selected', style: { width: 5, opacity: 1 } },
]

const PRESET_NOOP = { name: 'preset', fit: false, animate: false } as cytoscape.LayoutOptions
const COSE_LAYOUT = {
  name: 'cose',
  animate: false,
  idealEdgeLength: 110,
  nodeRepulsion: 9000,
} as cytoscape.LayoutOptions

interface EntityHit {
  id: string
  canonical_name: string
  entity_class: string
}

export default function GraphWalkPanel({ registration }: PanelProps) {
  const [anchorId, setAnchorId] = useState<string | null>(null)
  const [canvas, setCanvas] = useState<WalkCanvas>(EMPTY_CANVAS)
  const [families, setFamilies] = useState<Set<string>>(() => new Set(DEFAULT_FAMILIES))
  const [minConfidence, setMinConfidence] = useState(0)
  const [windowId, setWindowId] = useState('all')
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [expandError, setExpandError] = useState<string | null>(null)
  const [expanding, setExpanding] = useState<string | null>(null)

  const cyRef = useRef<Core | null>(null)
  const fitCleanup = useRef<(() => void) | null>(null)
  const { ref: canvasRef, visible } = useVisibleSize<HTMLDivElement>()

  const sinceDays = TIME_WINDOWS.find((w) => w.id === windowId)?.days ?? null
  const familyKey = [...families].sort().join(',')

  // Follow the shared selection when it names an entity, exactly as the other
  // graph surfaces do — the keystone is "select an actor, see its graph".
  //
  // The reverse is deliberately NOT wired: tapping a node expands it and must
  // not publish a selection, because this effect would then fire, re-anchor
  // the walk on the tapped node and discard everything accumulated so far.
  // Expanding a neighbour and re-anchoring on it are different verbs; the
  // panel keeps them different.
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (selection?.kind === 'entity') setAnchorId(selection.id)
  }, [selection])

  useEffect(() => () => fitCleanup.current?.(), [])

  // ---- the anchor hop ----

  const egoQ = useQuery<EgoResponse>({
    queryKey: ['graph-ego', anchorId, familyKey, minConfidence, sinceDays],
    enabled: !!anchorId,
    queryFn: () =>
      apiGet<EgoResponse>(
        `/graph/ego?${egoQueryString({
          entityId: anchorId as string,
          families,
          minConfidence,
          sinceDays,
          limit: EDGE_LIMIT,
        })}`,
      ),
  })

  // A new anchor or a changed filter restarts the walk: the canvas accumulated
  // under the old filter would be a mix of two different questions.
  useEffect(() => {
    if (egoQ.data) {
      setCanvas(seedCanvas(egoQ.data))
      setSelectedEdgeId(null)
      setExpandError(null)
    }
  }, [egoQ.data])

  // ---- expand-on-click ----

  const expand = useCallback(
    async (nodeId: string) => {
      if (!nodeId || canvas.expanded.includes(nodeId)) return
      setExpanding(nodeId)
      setExpandError(null)
      try {
        // `known` carries what is already on screen so the server can return
        // the induced edges among those nodes too.
        const known = Object.keys(canvas.nodes).slice(0, 200)
        const resp = await apiGet<EgoResponse>(
          `/graph/ego?${egoQueryString({
            entityId: nodeId,
            families,
            minConfidence,
            sinceDays,
            limit: EDGE_LIMIT,
            known,
          })}`,
        )
        setCanvas((prev) => mergeEgo(prev, resp, nodeId))
      } catch (err) {
        setExpandError(
          err instanceof ApiError
            ? `expand failed (${err.status}): ${String(err.message)}`
            : `expand failed: ${String(err)}`,
        )
      } finally {
        setExpanding(null)
      }
    },
    [canvas, families, minConfidence, sinceDays],
  )

  // Cytoscape's `cy` callback captures its closure once, so the live handlers
  // are reached through refs — otherwise every expand would see the canvas as
  // it was at mount.
  const expandRef = useRef(expand)
  expandRef.current = expand
  const selectEdgeRef = useRef(setSelectedEdgeId)
  selectEdgeRef.current = setSelectedEdgeId

  // ---- edge evidence ----

  const evidenceQ = useQuery<EdgeEvidence>({
    queryKey: ['graph-edge', selectedEdgeId],
    enabled: !!selectedEdgeId,
    queryFn: () => apiGet<EdgeEvidence>(`/graph/edge/${selectedEdgeId}`),
  })

  // ---- anchor picker ----

  const searchQ = useQuery<{ data: EntityHit[] }>({
    queryKey: ['graph-walk-search', search],
    enabled: search.trim().length >= 2,
    queryFn: () =>
      apiGet<{ data: EntityHit[] }>(
        `/entities?q=${encodeURIComponent(search.trim())}&limit=8`,
      ),
  })

  // ---- projection ----

  const elements = useMemo<ElementDefinition[]>(
    () => buildWalkElements(canvas, { visibleFamilies: families }),
    [canvas, families],
  )

  // ---- disclosure, computed off the MERGED canvas ----
  //
  // Everything below used to read `egoQ.data`, which is the anchor hop and
  // only ever the anchor hop: `mergeEgo` grew the canvas underneath it while
  // the denominators stood still, so after one expand the strip described a
  // picture that was no longer on screen. The rule now is that anything the
  // strip claims about the drawing is counted off the drawing, and per-anchor
  // quantities are attributed to the hop they belong to rather than blurred
  // into one total that would double-count the edges between two anchors.
  const anchorNode = canvas.anchorId ? canvas.nodes[canvas.anchorId] : undefined
  const truncatedHops = useMemo(
    () => canvas.hops.filter((h) => h.truncated),
    [canvas.hops],
  )
  const hiddenRows = useMemo(
    () => hiddenFamilyRows(canvas.hops, families),
    [canvas.hops, families],
  )
  const drawnStats = useMemo(
    () => drawnFamilyStats(canvas, families),
    [canvas, families],
  )

  // Only used by the nothing-is-drawn branch, which by definition can only be
  // reached on the anchor hop — there is no canvas yet to expand from.
  const hidden = hiddenEdgeCount(
    facetSummary(canvas.hops[0]?.facets ?? [], families),
  )

  const toggleFamily = (id: string) =>
    setFamilies((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const nodeCount = elements.filter((e) => !(e.data as { source?: string }).source).length
  const edgeCount = elements.length - nodeCount

  // The header states what is drawn and nothing else. The per-hop numbers
  // behind "truncated" live in the strip, where they can be attributed.
  const subtitle = anchorNode
    ? `${anchorNode.canonical_name} · ${nodeCount} nodes / ${edgeCount} edges drawn` +
      (canvas.hops.length > 1 ? ` over ${canvas.hops.length} hops` : '') +
      (truncatedHops.length > 0 ? ' · truncated' : '')
    : 'pick an actor to start walking'

  return (
    <PanelChrome
      registration={registration}
      subtitle={subtitle}
      onRefresh={() => egoQ.refetch()}
      actions={
        anchorId ? (
          <button
            onClick={() => {
              setAnchorId(null)
              setCanvas(EMPTY_CANVAS)
              setSelectedEdgeId(null)
            }}
            className="text-label border border-line rounded px-1.5 py-0.5 text-ink-2 hover:bg-surf-2"
            data-testid="graph-walk-reset"
          >
            ← new walk
          </button>
        ) : undefined
      }
    >
      <div className="flex h-full w-full flex-col">
        {/* ---- controls strip ---- */}
        <div
          className="flex flex-wrap items-center gap-2 border-b border-line px-2 py-1.5"
          data-testid="graph-walk-controls"
        >
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="find an actor…"
            aria-label="find an actor"
            className="bg-surf-2 border border-line rounded px-2 py-1 text-body text-ink-1 w-44"
            data-testid="graph-walk-search"
          />
          <label className="text-label text-ink-3">
            confidence ≥{' '}
            <select
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value))}
              className="bg-surf-2 border border-line rounded px-1 py-0.5 text-label text-ink-1"
              data-testid="graph-walk-confidence"
            >
              {CONFIDENCE_STEPS.map((c) => (
                <option key={c} value={c}>
                  {c === 0 ? 'any' : c.toFixed(1)}
                </option>
              ))}
            </select>
          </label>
          <label className="text-label text-ink-3">
            seen within{' '}
            <select
              value={windowId}
              onChange={(e) => setWindowId(e.target.value)}
              className="bg-surf-2 border border-line rounded px-1 py-0.5 text-label text-ink-1"
              data-testid="graph-walk-window"
            >
              {TIME_WINDOWS.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.label}
                </option>
              ))}
            </select>
          </label>
          {expanding && (
            <span className="text-label text-ink-3" data-testid="graph-walk-expanding">
              expanding…
            </span>
          )}
        </div>

        {/* ---- search results ---- */}
        {search.trim().length >= 2 && (searchQ.data?.data?.length ?? 0) > 0 && (
          <div
            className="border-b border-line max-h-28 overflow-y-auto"
            data-testid="graph-walk-search-results"
          >
            {searchQ.data?.data.map((hit) => (
              <button
                key={hit.id}
                onClick={() => {
                  setAnchorId(hit.id)
                  setSearch('')
                }}
                className="block w-full text-left px-2 py-1 text-body text-ink-1 hover:bg-surf-2"
              >
                {hit.canonical_name}{' '}
                <span className="text-label text-ink-3">{hit.entity_class}</span>
              </button>
            ))}
          </div>
        )}

        {/* ---- canvas ---- */}
        <div
          ref={canvasRef}
          className="relative flex-1 min-h-[280px]"
          data-testid="graph-walk-canvas"
        >
          {!anchorId && (
            <div
              className="absolute inset-0 flex items-center justify-center text-ink-3 text-body px-6 text-center"
              data-testid="graph-walk-empty"
            >
              search for an actor above, or select one anywhere in the workstation,
              to anchor a walk through the world graph
            </div>
          )}
          {anchorId && egoQ.isLoading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center text-ink-3 text-body">
              walking…
            </div>
          )}
          {anchorId && egoQ.error instanceof Error && (
            <div
              className="absolute inset-0 z-10 flex items-center justify-center text-rose-400 text-body px-6 text-center"
              data-testid="graph-walk-error"
            >
              {egoQ.error instanceof ApiError && egoQ.error.status === 404
                ? 'no such actor in the entity store — the walk cannot start here'
                : `error: ${egoQ.error.message}`}
            </div>
          )}
          {anchorId && !egoQ.isLoading && !egoQ.error && elements.length === 0 && (
            <div
              className="absolute inset-0 z-10 flex items-center justify-center text-ink-3 text-body px-6 text-center"
              data-testid="graph-walk-no-edges"
            >
              this actor exists but has no edges matching the current filters
              {hidden > 0 ? ` — ${hidden} edges are hidden by the family filter` : ''}
            </div>
          )}

          {elements.length > 0 && (
            <GraphControls
              chipsLabel="Families"
              legendLabel="Polarity (relation)"
              chips={ALL_FAMILIES.map((f) => ({
                id: f,
                label: f,
                color: FAMILY_STYLES[f].color,
              }))}
              activeChips={families}
              onToggleChip={toggleFamily}
              onSelectAllChips={() => setFamilies(new Set(ALL_FAMILIES))}
              onClearChips={() => setFamilies(new Set())}
              legend={[
                { id: 'hostile', label: 'hostile', color: POLARITY_COLORS['-1'] },
                { id: 'neutral', label: 'neutral', color: POLARITY_COLORS['0'] },
                { id: 'allied', label: 'allied', color: POLARITY_COLORS['1'] },
              ]}
              onZoomIn={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.3)}
              onZoomOut={() => cyRef.current?.zoom(cyRef.current.zoom() / 1.3)}
              onFit={() => cyRef.current?.fit(undefined, 30)}
            />
          )}

          {visible && elements.length > 0 && (
            <CytoscapeComponent
              elements={elements}
              stylesheet={STYLESHEET}
              layout={PRESET_NOOP}
              style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 }}
              userZoomingEnabled
              userPanningEnabled
              minZoom={0.2}
              maxZoom={3}
              cy={(cy: Core) => {
                cyRef.current = cy
                cy.removeListener('tap', 'node')
                cy.removeListener('tap', 'edge')
                cy.on('tap', 'node', (evt) => {
                  void expandRef.current(evt.target.id())
                })
                cy.on('tap', 'edge', (evt) => {
                  selectEdgeRef.current(evt.target.id())
                })
                fitCleanup.current?.()
                fitCleanup.current = attachFitOnResize(cy, {
                  layout: COSE_LAYOUT,
                  padding: 30,
                })
              }}
            />
          )}

          {/* ---- edge evidence drawer ---- */}
          {selectedEdgeId && (
            <div
              className="absolute top-0 right-0 bottom-0 w-72 bg-surf-1 border-l border-line overflow-y-auto z-20 p-2.5"
              data-testid="graph-walk-evidence"
            >
              <div className="flex items-start justify-between gap-2 mb-2">
                <span className="text-label text-ink-3 uppercase tracking-wide">
                  edge evidence
                </span>
                <button
                  onClick={() => setSelectedEdgeId(null)}
                  className="text-label text-ink-3 hover:text-ink-1"
                  data-testid="graph-walk-evidence-close"
                >
                  ✕
                </button>
              </div>
              {evidenceQ.isLoading && (
                <div className="text-body text-ink-3">loading evidence…</div>
              )}
              {evidenceQ.error instanceof Error && (
                <div className="text-body text-rose-400">
                  error: {evidenceQ.error.message}
                </div>
              )}
              {evidenceQ.data && (
                <EvidenceBody evidence={evidenceQ.data} />
              )}
            </div>
          )}
        </div>

        {/* ---- disclosure strip: what this view is NOT showing ---- */}
        {canvas.hops.length > 0 && (
          <div
            className="border-t border-line px-2 py-1 text-label text-ink-3 flex flex-wrap gap-x-3 gap-y-0.5"
            data-testid="graph-walk-disclosure"
          >
            <span>
              open degree <span className="text-ink-1">{anchorNode?.degree ?? 0}</span>
            </span>
            {canvas.hops.length > 1 && (
              <span data-testid="graph-walk-hops">{canvas.hops.length} hops walked</span>
            )}
            {/* One hop reads as a plain sentence; past that every number is
                named, because "80 of 111" stops being true of the canvas the
                moment a second anchor is folded into it. */}
            {truncatedHops.length > 0 && (
              <span className="text-amber-400" data-testid="graph-walk-truncated">
                truncated:{' '}
                {canvas.hops.length === 1
                  ? `${truncatedHops[0].returned} of ${truncatedHops[0].matched}`
                  : truncatedHops
                      .map((h) => `${h.returned} of ${h.matched} (${h.name})`)
                      .join(', ')}{' '}
                matching edges drawn
              </span>
            )}
            {hiddenRows.map((row) => (
              <span key={row.family} data-testid={`graph-walk-hidden-${row.family}`}>
                {canvas.hops.length === 1
                  ? `${row.hops[0].count} ${row.family} hidden`
                  : `${row.family} hidden: ${row.hops
                      .map((h) => `${h.count} (${h.name})`)
                      .join(', ')}`}
              </span>
            ))}
            {/* Counted off the canvas and labelled by family: two bare "N
                hostile" chips read as a typo, and a facet-sourced one went on
                claiming hostility under a canvas drawing none of it. */}
            {drawnStats
              .filter((s) => s.negative > 0)
              .map((s) => (
                <span
                  key={`${s.family}-neg`}
                  className="text-rose-400"
                  data-testid={`graph-walk-hostile-${s.family}`}
                >
                  {s.family}: {s.negative} hostile drawn
                </span>
              ))}
            {expandError && (
              <span className="text-rose-400" data-testid="graph-walk-expand-error">
                {expandError}
              </span>
            )}
          </div>
        )}
      </div>
    </PanelChrome>
  )
}

/**
 * The evidence body — split out so the honest-empty branch is obvious, and
 * exported so it can be tested directly (the drawer only mounts on a canvas
 * tap, which a mocked cytoscape cannot dispatch).
 */
export function EvidenceBody({ evidence }: { evidence: EdgeEvidence }) {
  const { edge } = evidence
  return (
    <div className="space-y-2">
      <div className="text-body text-ink-1">
        <span style={{ color: entityClassColor(evidence.src.entity_class) }}>
          {evidence.src.canonical_name}
        </span>{' '}
        <span className="text-ink-3">—{edge.edge_type}→</span>{' '}
        <span style={{ color: entityClassColor(evidence.dst.entity_class) }}>
          {evidence.dst.canonical_name}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-label">
        <dt className="text-ink-3">family</dt>
        <dd className="text-ink-1">{edge.edge_family}</dd>
        <dt className="text-ink-3">polarity</dt>
        <dd className="text-ink-1">{edge.polarity}</dd>
        <dt className="text-ink-3">confidence</dt>
        <dd className="text-ink-1">{edge.confidence.toFixed(2)}</dd>
        <dt className="text-ink-3">observed</dt>
        <dd className="text-ink-1">{edge.observed_count}×</dd>
        <dt className="text-ink-3">provenance</dt>
        <dd className="text-ink-1">{edge.source_type}</dd>
        {evidence.analyst_id && (
          <>
            <dt className="text-ink-3">analyst</dt>
            <dd className="text-ink-1 break-all">{evidence.analyst_id}</dd>
          </>
        )}
      </dl>

      {/* The honest-empty branch: say WHY, never render a blank box. */}
      {!evidence.evidence_available && (
        <div
          className="text-label text-ink-3 border border-line rounded p-1.5"
          data-testid="graph-walk-evidence-absent"
        >
          {evidence.detail}
        </div>
      )}

      {evidence.evidence_text && (
        <blockquote
          className="text-label text-ink-2 border-l-2 border-line pl-2 whitespace-pre-wrap"
          data-testid="graph-walk-evidence-text"
        >
          {evidence.evidence_text}
        </blockquote>
      )}

      {evidence.signals.length > 0 && (
        <div data-testid="graph-walk-evidence-signals">
          <div className="text-label text-ink-3 uppercase tracking-wide mb-1">
            {evidence.signal_count} source signal
            {evidence.signal_count === 1 ? '' : 's'}
          </div>
          <ul className="space-y-1">
            {evidence.signals.map((s) => (
              <li key={s.id} className="text-label">
                {s.url ? (
                  <a
                    href={s.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-sky-400 hover:underline"
                  >
                    {s.title || s.url}
                  </a>
                ) : (
                  <span className="text-ink-1">{s.title || s.id}</span>
                )}
                <div className="text-ink-3">{s.source_id}</div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {evidence.unresolved_signal_ids.length > 0 && (
        <div
          className="text-label text-amber-400"
          data-testid="graph-walk-evidence-unresolved"
        >
          {evidence.unresolved_signal_ids.length} referenced signal
          {evidence.unresolved_signal_ids.length === 1 ? '' : 's'} no longer resolve
        </div>
      )}
    </div>
  )
}
