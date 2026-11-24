/**
 * CatalogPopover — the `＋` catalog (UI-B1/shell; U-6: grouped, searchable,
 * built to hold many more views).
 *
 * It lists every view that renders on its own in the active mode — and NO
 * DRILL (a kind whose params need a ref opens from its parent object,
 * `kindPlan.ts::isDrill`). Each entry is its name, its one-line description
 * and, on hover, where it can go:
 *
 *   ┌ Find a view…                                    ┐
 *   │ READ                                             │
 *   │   Today's read          What changed since…  ◧ ◨ │
 *   │   At a glance           The band of headline…    │
 *   │ WATCH                                            │
 *   │   …                                              │
 *   └ 9 reader views · the operator side holds the rest┘
 *
 * Names, descriptions and groups come from the view's CONTRACT
 * (`panel-registry/contracts.ts`; a view without a declared one gets its
 * registry name and description, grouped under Operator). The filter searches
 * the name, the id and the description, so "calibration" finds Accuracy.
 *
 * Choosing a registry kind compiles to `open_panel` and runs through the ONE
 * executor (`drive`); a frame view (the forecast ledger, a slot, a registered
 * view) opens through its contract (`lib/panelActions.ts`). Either lands
 * where the contract says — the stage or a panel — and the two small buttons
 * put it on the stage or in the panels instead.
 *
 * TWO SIDES, TWO CATALOGS. On the reader side it lists the reader's views
 * only (`lib/surfaces.ts::readerKinds`, less Consult and the Record sheet,
 * which are frame); on the operator side, everything.
 */
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { PanelBottom, PanelRight } from 'lucide-react'
import type { Mode, PanelKind } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { catalogKinds } from '@/panel-registry/kindPlan'
import { allContracts, onContractsChanged, type CatalogGroup } from '@/panel-registry/contracts'
import { contractOf } from '@/panel-registry/contractOf'
import { drive } from '@/lib/applyAction'
import { applyPanelAction } from '@/lib/panelActions'
import { railActions } from '@/lib/commands'
import { READER_SURFACES, pageViews, readerKinds, isFrameView, type Side } from '@/lib/surfaces'

/** Frame, not views: never offered as a region's view. */
const FRAME_KINDS = new Set(['system.consult', 'system.inspector'])

const GROUP_ORDER: readonly CatalogGroup[] = ['Read', 'Subjects', 'Map', 'Forecasts and proof', 'Watch', 'Records', 'Operator', 'Other']

export interface CatalogEntry {
  kind: string
  title: string
  description: string | null
  group: CatalogGroup
  /** A registry kind (opens through `drive`), or a frame view (through its contract). */
  isKind: boolean
  /** What the filter searches: the names, the id, both descriptions, the group. */
  search: string
}

function viewsFor(mode: Mode, side: Side): string[] {
  const reader = side === 'reader' ? new Set<string>(readerKinds()) : null
  const kinds = catalogKinds()
    .filter((k) => !reader || (reader.has(k) && !FRAME_KINDS.has(k)))
    .filter((k) => {
      const m = PANEL_REGISTRY[k].definition.modes
      return m.length === 0 || m.includes(mode)
    }) as string[]
  // The views that are not registry kinds: the reader pages' frame views
  // (the forecast ledger), and every view registered with its own contract.
  const frameViews = new Set<string>()
  for (const s of READER_SURFACES) for (const v of pageViews(s)) if (isFrameView(v) && !v.startsWith('slot:')) frameViews.add(v)
  for (const c of allContracts()) {
    if (PANEL_REGISTRY[c.id as PanelKind] || c.id.startsWith('slot:')) continue
    if (side === 'reader' && c.side !== 'reader') continue
    frameViews.add(c.id)
  }
  return [...kinds, ...frameViews]
}

export function catalogEntries(mode: Mode, side: Side = 'operator'): CatalogEntry[] {
  return viewsFor(mode, side)
    .map((k) => {
      const c = contractOf(k)
      const kindDef = PANEL_REGISTRY[k as PanelKind]?.definition
      return {
        kind: k,
        title: c?.name ?? kindDef?.defaultTitle ?? k,
        description: c?.description || kindDef?.description || null,
        group: c?.group ?? 'Other',
        isKind: !!kindDef,
        search: [c?.name, kindDef?.defaultTitle, k, c?.description, kindDef?.description, c?.group].filter(Boolean).join(' ').toLowerCase(),
      }
    })
    .sort((a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group) || a.title.localeCompare(b.title))
}

function useContractsVersion(): number {
  const ver = useRef(0)
  return useSyncExternalStore(
    (cb) =>
      onContractsChanged(() => {
        ver.current++
        cb()
      }),
    () => ver.current,
  )
}

export function CatalogPopover({ mode, side = 'operator', onClose }: { mode: Mode; side?: Side; onClose: () => void }) {
  const [filter, setFilter] = useState('')
  const ref = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const version = useContractsVersion()
  const entries = useMemo(() => catalogEntries(mode, side), [mode, side, version]) // eslint-disable-line react-hooks/exhaustive-deps
  const q = filter.trim().toLowerCase()
  const visible = q
    ? entries.filter((e) => e.search.includes(q))
    : entries
  const groups = GROUP_ORDER.map((g) => ({ g, rows: visible.filter((e) => e.group === g) })).filter((x) => x.rows.length > 0)

  useEffect(() => {
    input.current?.focus()
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const open = (e: CatalogEntry, place?: 'stage' | 'panel') => {
    if (!place && e.isKind) drive(railActions.catalog(e.kind as PanelKind))
    else applyPanelAction(e.kind, 'open', place ? { place } : {})
    onClose()
  }

  return (
    <div
      ref={ref}
      role="dialog"
      aria-label={side === 'reader' ? 'Add a view' : 'Open a view'}
      data-testid="catalog-popover"
      className="absolute right-0 top-full z-50 mt-1 w-[26rem] max-w-[90vw] border border-line-strong bg-surf-4 shadow-[0_8px_24px_rgba(0,0,0,0.35)]"
    >
      <input
        ref={input}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Find a view by name or what it is for…"
        aria-label="Filter the panel catalog"
        data-testid="catalog-filter"
        className="w-full border-b border-line bg-surf-4 px-3 py-2 text-body text-ink-1 placeholder:text-ink-3 outline-none"
      />
      <div className="max-h-[60vh] overflow-y-auto py-1" data-testid="catalog-list">
        {groups.length === 0 && <p className="px-3 py-1 text-label text-ink-3">no view matches</p>}
        {groups.map(({ g, rows }) => (
          <section key={g} data-testid={`catalog-group-${g}`}>
            <h3 className="px-3 pb-0.5 pt-2 text-label uppercase tracking-[0.06em] text-ink-3">{g}</h3>
            <ul>
              {rows.map((e) => (
                <li key={e.kind} className="group flex items-stretch hover:bg-surf-3">
                  <button
                    type="button"
                    data-testid={`catalog-item-${e.kind}`}
                    onClick={() => open(e)}
                    className="min-w-0 flex-1 px-3 py-1.5 text-left text-body text-ink-2 group-hover:text-ink-1"
                  >
                    <span className="block">{e.title}</span>
                    {e.description && (
                      <span className="block text-label text-ink-3" data-testid={`catalog-desc-${e.kind}`}>
                        {e.description}
                      </span>
                    )}
                  </button>
                  <span className="flex shrink-0 items-center gap-0.5 pr-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100">
                    <button
                      type="button"
                      onClick={() => open(e, 'stage')}
                      title="Open on the stage"
                      aria-label={`Open ${e.title} on the stage`}
                      data-testid={`catalog-stage-${e.kind}`}
                      className="flex h-6 w-6 items-center justify-center rounded-control text-ink-3 hover:text-ink-1"
                    >
                      <PanelBottom size={13} aria-hidden />
                    </button>
                    <button
                      type="button"
                      onClick={() => open(e, 'panel')}
                      title="Open as a panel"
                      aria-label={`Open ${e.title} as a panel`}
                      data-testid={`catalog-panel-${e.kind}`}
                      className="flex h-6 w-6 items-center justify-center rounded-control text-ink-3 hover:text-ink-1"
                    >
                      <PanelRight size={13} aria-hidden />
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      <div className="border-t border-line px-3 py-1 text-label text-ink-3">
        {side === 'reader'
          ? `${entries.length} reader views · the operator side holds the rest`
          : `${entries.length} views · a drill opens from the object it reads`}
      </div>
    </div>
  )
}
