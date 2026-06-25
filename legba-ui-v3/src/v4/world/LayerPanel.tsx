/**
 * LayerPanel — floating top-left Windy-style layer switcher for the World map.
 *
 * Reads the orchestrator-owned world store: toggles per-layer visibility and
 * shows live counts (badges) the map/rails publish via setCount.
 */
import { useState } from 'react'
import { ChevronDown, ChevronUp, X } from 'lucide-react'
import { cn } from '@/lib/cn'
import { COUNTRY_BY_ISO2 } from '@/lib/countryGeo'
import { useWorldState, type WorldLayer } from './worldState'
import type { Severity } from './types'

/** Render order + swatch color per layer (matches the map's encoding). */
const LAYERS: { key: WorldLayer; swatch: string }[] = [
  { key: 'signals', swatch: 'bg-severity-critical' },
  { key: 'findings', swatch: 'bg-accent-info' },
  { key: 'situations', swatch: 'bg-accent-warning' },
  { key: 'entities', swatch: 'bg-slate-400' },
]

/** Severity floor options, high→low (the map filters at-or-above this rank). */
const SEVERITY_FLOORS: Severity[] = ['critical', 'high', 'medium', 'low', 'info']

const numberFmt = new Intl.NumberFormat('en-US')

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** ISO2 → readable label for the country dropdown (falls back to the code). */
function countryLabel(iso2: string): string {
  return COUNTRY_BY_ISO2[iso2]?.name ?? iso2
}

/** Shared select styling so the three filter dropdowns match the dark chrome. */
const SELECT_CLASS = cn(
  'w-full rounded border border-slate-800 bg-surface-100 px-2 py-1',
  'text-xs text-slate-200',
  'focus:outline-none focus:ring-1 focus:ring-accent-info',
)

export default function LayerPanel() {
  const [collapsed, setCollapsed] = useState(false)
  const layers = useWorldState((s) => s.layers)
  const counts = useWorldState((s) => s.counts)
  const toggleLayer = useWorldState((s) => s.toggleLayer)
  const filters = useWorldState((s) => s.filters)
  const setFilter = useWorldState((s) => s.setFilter)
  const clearFilters = useWorldState((s) => s.clearFilters)
  const filterOptions = useWorldState((s) => s.filterOptions)
  const decay = useWorldState((s) => s.decay)
  const toggleDecay = useWorldState((s) => s.toggleDecay)

  const hasFilters =
    filters.minSeverity != null || filters.source != null || filters.country != null

  return (
    <div
      className={cn(
        'absolute left-3 top-3 z-10 w-[220px] overflow-hidden rounded-lg',
        'border border-slate-800 bg-surface-200/95 shadow-lg backdrop-blur-sm',
      )}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
        aria-label={collapsed ? 'Expand layers panel' : 'Collapse layers panel'}
        className={cn(
          'flex w-full items-center justify-between px-3 py-2',
          'text-left text-xs font-semibold uppercase tracking-wide text-slate-400',
          'transition-colors hover:text-slate-200',
          'focus:outline-none focus:ring-1 focus:ring-accent-info',
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
        <ul className="border-t border-slate-800 py-1">
          {LAYERS.map(({ key, swatch }) => {
            const count = counts[key]
            return (
              <li key={key}>
                <label
                  className={cn(
                    'flex cursor-pointer items-center gap-2 px-3 py-1.5',
                    'transition-colors hover:bg-surface-50/60',
                  )}
                >
                  <input
                    type="checkbox"
                    checked={layers[key]}
                    onChange={() => toggleLayer(key)}
                    aria-label={`Toggle ${key} layer`}
                    className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-accent-info"
                  />
                  <span
                    aria-hidden
                    className={cn(
                      'h-2.5 w-2.5 shrink-0 rounded-sm',
                      swatch,
                      !layers[key] && 'opacity-30',
                    )}
                  />
                  <span
                    className={cn(
                      'flex-1 text-sm',
                      layers[key] ? 'text-slate-200' : 'text-slate-500',
                    )}
                  >
                    {capitalize(key)}
                  </span>
                  <span className="shrink-0 text-xs tabular-nums text-slate-500">
                    {count == null ? '–' : numberFmt.format(count)}
                  </span>
                </label>
              </li>
            )
          })}
        </ul>
      )}

      {!collapsed && (
        <div className="space-y-2 border-t border-slate-800 px-3 py-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">
              Filters
            </span>
            {hasFilters && (
              <button
                type="button"
                onClick={clearFilters}
                className={cn(
                  'flex items-center gap-0.5 rounded px-1 py-0.5 text-[10px]',
                  'text-slate-400 transition-colors hover:text-slate-200',
                  'focus:outline-none focus:ring-1 focus:ring-accent-info',
                )}
              >
                <X className="h-3 w-3" />
                Clear
              </button>
            )}
          </div>

          <label className="block">
            <span className="mb-0.5 block text-[10px] text-slate-500">Severity floor</span>
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
            <span className="mb-0.5 block text-[10px] text-slate-500">Source</span>
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
            <span className="mb-0.5 block text-[10px] text-slate-500">Country</span>
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
              'transition-colors hover:bg-surface-50/60',
            )}
          >
            <input
              type="checkbox"
              checked={decay}
              onChange={toggleDecay}
              aria-label="Toggle time-decay fade"
              className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-accent-info"
            />
            <span className="flex-1 text-xs text-slate-300">Time-decay fade</span>
          </label>
        </div>
      )}
    </div>
  )
}
