/**
 * LayerPanel — floating top-left Windy-style layer switcher for the World map.
 *
 * Reads the orchestrator-owned world store: toggles per-layer visibility and
 * shows live counts (badges) the map/rails publish via setCount.
 *
 * THE LAYER CATALOG (UI-B4/map). The draw layers listed here come from
 * `lib/layerCatalog.ts` for THIS profile — a profile whose geometry cannot
 * carry a layer never lists it — and each carries an `i`
 * ({@link LayerApertureTip}, on the `src/ui/tooltip.tsx` primitive) stating
 * the layer's APERTURE: the route it reads, the source layers that can reach
 * it, the window it is cut to, the subject roster it reads and how a row earns
 * a place. A layer whose read has not answered prints *no read yet*, never a
 * 0; a measured 0 stays 0. The renderer-local layers (the choropleth, density,
 * arcs, convergence, watch points) carry the same `i` on the map's own mode
 * control, so every layer the map can draw says what it can see.
 *
 * THE APERTURE SECTION (wave-P design pass). Beneath the draw layers sits the
 * other kind of layer entirely — the six SOURCE layers of `docs/DATA_SOURCES.md`,
 * with the per-country aperture declaration behind an `i` on each. World
 * Monitor's layer catalog carries a per-layer `i`; ours wraps the object that
 * `i` is worth having for: the operator's own declaration of whether a layer is
 * present, declared absent (and whether anything is nonetheless arriving on
 * it), unmeasured, or undeclared for the country in scope — the statement that
 * keeps a structurally missing layer from reading as agreement.
 *
 * It costs NO new route: the aperture is folded out of the divergence receipt
 * (`GET /v3/layers/divergence`) the Layer Divergence panel already reads, on
 * the same query key, and only once the switcher is expanded.
 */
import { useId, useState } from 'react'
import { ChevronDown, ChevronUp, Info, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { cn } from '@/lib/cn'
import { COUNTRY_BY_ISO2 } from '@/lib/countryGeo'
import { InfoTip } from '@/components/InfoTip'
import { useBelowAdminPassive as useBelowAdmin } from '@/auth/roles'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/tooltip'
import { fetchLayerDivergence, type LayerDivergenceResponse } from '@/lib/api'
import {
  APERTURE_WORD,
  LAYER_LABEL,
  apertureExplainer,
  apertureSummary,
  deskAperture,
  type ApertureState,
} from '@/lib/layerAperture'
import {
  apertureLines,
  apertureSentence,
  layerCountFigure,
  layersForProfile,
  utcStamp,
  type CatalogLayer,
} from '@/lib/layerCatalog'
import { Figure, figureText, figureValueText, type FigureData } from '@/lib/Figure'
import { mapSurfaceDeclared } from '@/lib/subjectGeometry'
import { useScope } from '@/state/scope'
import { useWorldState, type WorldLayer } from './worldState'
import type { Severity } from './types'

/** Swatch per store layer (matches the map's encoding). */
const SWATCH: Record<WorldLayer, string> = {
  signals: 'bg-severity-critical',
  findings: 'bg-accent-info',
  situations: 'bg-accent-warning',
  events: 'bg-accent-critical', // V3/P6 — lifecycle-colored markers
  entities: 'bg-ink-3',
}

const numberFmt = new Intl.NumberFormat('en-US')

/**
 * A map layer's `i`: its aperture from the catalog, on the Tooltip primitive
 * (hover, keyboard focus and a tap all open it). The same sentence stays in
 * the DOM as the trigger's accessible description. `figure`, when given, is
 * the layer's count with its unit and as-of; a windowed layer also states the
 * shared window it is cut to.
 */
export function LayerApertureTip({
  layer,
  figure,
  windowMs,
  className,
}: {
  layer: CatalogLayer
  figure?: FigureData
  windowMs?: { startMs: number; endMs: number }
  className?: string
}) {
  const id = useId()
  const testId = `layer-aperture-${layer.id}`
  // The route a layer reads is the admin's detail; a reader is told what it shows.
  const technical = !useBelowAdmin()
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          aria-describedby={id}
          data-testid={testId}
          className={cn('inline-flex shrink-0 cursor-help items-center rounded-control text-ink-3', className)}
          onClick={(e) => e.stopPropagation()}
        >
          <Info className="h-3 w-3" aria-hidden />
          <span className="sr-only">What the {layer.label} layer can see</span>
          <span id={id} className="sr-only" data-testid={`${testId}-description`}>
            {apertureSentence(layer, technical)}
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent
        side="right"
        align="start"
        className="w-72 text-[10px] leading-relaxed"
        data-testid={`${testId}-floating`}
      >
        <div className="mb-1 font-semibold text-ink-1">{layer.label} · what this layer can see</div>
        <dl className="space-y-0.5">
          {apertureLines(layer, technical).map((l) => (
            <div key={l.label}>
              <dt className="inline font-semibold text-ink-2">{l.label}: </dt>
              <dd className="inline text-ink-2">{l.text}</dd>
            </div>
          ))}
          {layer.windowed && windowMs && (
            <div>
              <dt className="inline font-semibold text-ink-2">Shared window now: </dt>
              <dd className="inline font-mono text-ink-2">
                {utcStamp(windowMs.startMs)} → {utcStamp(windowMs.endMs)}
              </dd>
            </div>
          )}
        </dl>
        {figure && (
          <div className="mt-1 border-t border-line pt-1">
            <Figure {...figure} label="drawn" testId={`${testId}-figure`} format={(v) => numberFmt.format(v)} />
          </div>
        )}
      </TooltipContent>
    </Tooltip>
  )
}

/** Severity floor options, high→low (the map filters at-or-above this rank). */
const SEVERITY_FLOORS: Severity[] = ['critical', 'high', 'medium', 'low', 'info']

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** ISO2 → readable label for the country dropdown (falls back to the code). */
function countryLabel(iso2: string): string {
  return COUNTRY_BY_ISO2[iso2]?.name ?? iso2
}

/** The divergence receipt's own `fired_days` default — the SAME key the Layer
 *  Divergence panel observes, so an open panel and an expanded switcher share
 *  one request rather than issuing two. */
const APERTURE_FIRED_DAYS = 30

/** Aperture state → chip styling. `absent` is the loud one; `unmeasured` and
 *  `undeclared` recede, because neither is a claim that a layer is missing. */
const APERTURE_TONE: Record<ApertureState, string> = {
  present: 'border-accent-ok/40 bg-accent-ok/15 text-accent-ok',
  absent: 'border-accent-critical/40 bg-accent-critical/15 text-accent-critical',
  unmeasured: 'border-line bg-surf-2 text-ink-3',
  undeclared: 'border-dashed border-line-strong bg-surf-base text-ink-3',
  no_map: 'border-dashed border-line-strong bg-surf-base text-ink-3',
  unread: 'border-accent-warning/40 bg-accent-warning/15 text-accent-warning',
}

/**
 * The six SOURCE layers with their per-country aperture declaration, each
 * behind an `i`.
 *
 * The country comes from the wall's SCOPE when it names one desk, and falls
 * back to the map's own country filter — the two deliberate ways a reader says
 * "this country" on this surface. With neither, the section says it has nothing
 * to declare rather than rendering six blanks.
 */
function ApertureSection() {
  const scope = useScope((s) => s.scope)
  const countryFilter = useWorldState((s) => s.filters.country)
  const scopeTargetId =
    scope && scope.kind === 'target'
      ? scope.id
      : scope && scope.members.targetIds.length === 1
        ? scope.members.targetIds[0]
        : null

  const { data, isError } = useQuery<LayerDivergenceResponse>({
    queryKey: ['layer-divergence', APERTURE_FIRED_DAYS],
    queryFn: () => fetchLayerDivergence({ firedDays: APERTURE_FIRED_DAYS }),
    refetchInterval: 300_000,
    retry: false,
  })

  const desk = deskAperture(
    data,
    { targetId: scopeTargetId, iso2: countryFilter },
    isError,
  )

  return (
    <div className="space-y-1 border-t border-line px-3 py-2" data-testid="aperture-section">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-ink-3">
        Source layers · aperture
      </div>
      <div className="text-[10px] leading-snug text-ink-3" data-testid="aperture-summary">
        {apertureSummary(desk)}
      </div>
      <ul className="pt-0.5">
        {desk.rows.map((row) => (
          <li
            key={row.layer}
            className="flex items-center gap-1.5 py-0.5"
            data-testid={`aperture-row-${row.layer}`}
          >
            <span className="flex-1 truncate text-xs text-ink-2">
              {LAYER_LABEL[row.layer] ?? row.layer}
            </span>
            <span
              data-testid={`aperture-state-${row.layer}`}
              className={cn(
                'shrink-0 rounded border px-1 text-[10px] leading-4',
                APERTURE_TONE[row.declared],
              )}
            >
              {APERTURE_WORD[row.declared]}
            </span>
            <InfoTip
              text={apertureExplainer(row, desk)}
              testId={`aperture-info-${row.layer}`}
              popoverClassName="w-72 left-auto right-0"
              className="shrink-0 text-ink-3"
            >
              <Info className="h-3 w-3" aria-hidden />
              <span className="sr-only">
                What the aperture declares for {LAYER_LABEL[row.layer] ?? row.layer}
              </span>
            </InfoTip>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Shared select styling so the three filter dropdowns match the dark chrome. */
const SELECT_CLASS = cn(
  'w-full rounded border border-line bg-surf-2 px-2 py-1',
  'text-xs text-ink-1',
  'focus:outline-none focus:ring-1 focus:ring-accent-info',
)

export default function LayerPanel() {
  // Collapsed by default (UI direction §"map panel" — LAYERS collapsed) so the
  // map surface leads; the operator expands the switcher when they need it.
  const [collapsed, setCollapsed] = useState(true)
  const layers = useWorldState((s) => s.layers)
  const counts = useWorldState((s) => s.counts)
  const toggleLayer = useWorldState((s) => s.toggleLayer)
  const filters = useWorldState((s) => s.filters)
  const setFilter = useWorldState((s) => s.setFilter)
  const clearFilters = useWorldState((s) => s.clearFilters)
  const filterOptions = useWorldState((s) => s.filterOptions)
  const decay = useWorldState((s) => s.decay)
  const toggleDecay = useWorldState((s) => s.toggleDecay)
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)

  // A profile that declares no map geometry mounts no map: nothing to switch.
  if (!mapSurfaceDeclared()) return null
  // The profile's draw layers (the store's toggles), in catalog order.
  const drawLayers = layersForProfile().filter(
    (l): l is CatalogLayer & { storeKey: WorldLayer } => l.storeKey !== null,
  )

  const hasFilters =
    filters.minSeverity != null || filters.source != null || filters.country != null

  return (
    <div
      className={cn(
        'absolute left-3 top-3 z-10 w-[240px] overflow-hidden rounded-lg',
        'border border-line bg-surf-1/95 shadow-lg backdrop-blur-sm',
      )}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
        aria-label={collapsed ? 'Expand layers panel' : 'Collapse layers panel'}
        className={cn(
          'flex w-full items-center justify-between px-3 py-2',
          'text-left text-xs font-semibold uppercase tracking-wide text-ink-3',
          'transition-colors hover:text-ink-1',
        )}
      >
        <span>Layers</span>
        {collapsed ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronUp className="h-4 w-4" />
        )}
      </button>

      {!collapsed && (
        <ul className="border-t border-line py-1" data-testid="layer-catalog">
          {drawLayers.map((layer) => {
            const key = layer.storeKey
            const fig = layerCountFigure(layer, counts[key], windowEndMs)
            const on = layers[key]
            const sentence = figureText(fig, (v) => numberFmt.format(v))
            return (
              <li
                key={key}
                className="flex items-center gap-2 px-3 py-1.5 transition-colors hover:bg-surf-3/60"
                data-testid={`layer-row-${layer.id}`}
              >
                <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2">
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => toggleLayer(key)}
                    aria-label={`Toggle ${key} layer`}
                    className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-accent-info"
                  />
                  <span
                    aria-hidden
                    className={cn('h-2.5 w-2.5 shrink-0 rounded-sm', SWATCH[key], !on && 'opacity-30')}
                  />
                  <span className={cn('flex-1 truncate text-sm', on ? 'text-ink-1' : 'text-ink-3')}>
                    {layer.label}
                  </span>
                </label>
                <span
                  className={cn(
                    'shrink-0 text-xs',
                    fig.state === 'measured' ? 'font-mono tabular-nums text-ink-2' : 'text-ink-3',
                  )}
                  data-testid={`layer-count-${layer.id}`}
                  data-state={fig.state}
                  aria-label={sentence}
                  title={sentence}
                >
                  {fig.state === 'measured' ? numberFmt.format(fig.value) : figureValueText(fig)}
                </span>
                <LayerApertureTip
                  layer={layer}
                  figure={fig}
                  windowMs={{ startMs: windowStartMs, endMs: windowEndMs }}
                />
              </li>
            )
          })}
        </ul>
      )}

      {!collapsed && (
        <div className="space-y-2 border-t border-line px-3 py-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-ink-3">
              Filters
            </span>
            {hasFilters && (
              <button
                type="button"
                onClick={clearFilters}
                className={cn(
                  'flex items-center gap-0.5 rounded px-1 py-0.5 text-[10px]',
                  'text-ink-3 transition-colors hover:text-ink-1',
                  'focus:outline-none focus:ring-1 focus:ring-accent-info',
                )}
              >
                <X className="h-3 w-3" />
                Clear
              </button>
            )}
          </div>

          <label className="block">
            <span className="mb-0.5 block text-[10px] text-ink-3">Severity floor</span>
            <select
              value={filters.minSeverity ?? ''}
              onChange={(e) =>
                setFilter('minSeverity', (e.target.value || null) as Severity | null)
              }
              aria-label="Minimum severity"
              className={SELECT_CLASS}
            >
              <option value="">Any severity</option>
              {SEVERITY_FLOORS.map((sev) => (
                <option key={sev} value={sev}>
                  {capitalize(sev)} +
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-0.5 block text-[10px] text-ink-3">Source</span>
            <select
              value={filters.source ?? ''}
              onChange={(e) => setFilter('source', e.target.value || null)}
              aria-label="Filter by source"
              className={SELECT_CLASS}
            >
              <option value="">All sources</option>
              {filterOptions.sources.map((src) => (
                <option key={src} value={src}>
                  {src}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-0.5 block text-[10px] text-ink-3">Country</span>
            <select
              value={filters.country ?? ''}
              onChange={(e) => setFilter('country', e.target.value || null)}
              aria-label="Filter by country"
              className={SELECT_CLASS}
            >
              <option value="">All countries</option>
              {filterOptions.countries.map((iso2) => (
                <option key={iso2} value={iso2}>
                  {countryLabel(iso2)}
                </option>
              ))}
            </select>
          </label>

          <label
            className={cn(
              'mt-1 flex cursor-pointer items-center gap-2 rounded px-1 py-1',
              'transition-colors hover:bg-surf-3/60',
            )}
          >
            <input
              type="checkbox"
              checked={decay}
              onChange={toggleDecay}
              aria-label="Toggle time-decay fade"
              className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-accent-info"
            />
            <span className="flex-1 text-xs text-ink-2">Time-decay fade</span>
          </label>
        </div>
      )}

      {/* The other kind of layer: the six SOURCE layers and what the operator
          declared about each for the country in scope. Mounted only while the
          switcher is open, which is also what keeps its read lazy. */}
      {!collapsed && <ApertureSection />}
    </div>
  )
}
