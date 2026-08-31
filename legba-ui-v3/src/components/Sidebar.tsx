/**
 * Sidebar — the ONE grouped navigation tree (S7-T2; U-3 task order + Engine
 * Room).
 *
 * A single grouped tree modeled on the original mission-control UI: a Search
 * (⌘K) launcher, a compact Layouts menu (named save/restore workspaces), the
 * Desks group (U-2, human country desks, first overall), then five
 * collapsible verb-grouped sections — Awareness / Investigation / Analysis /
 * Products / Engine Room (id: `operations`) — over the singleton panel
 * catalog. Within each group, rows sort by task order where U-3 pins one
 * (`navGroups.ts` TASK_ORDER), alphabetical otherwise — NOT purely
 * alphabetical.
 *
 * THE CATALOG IS FOLDED (UI_HOLISTIC_DESIGN_2026-08-24). All five verb groups
 * start collapsed and carry a count, so the sidebar opens as five rows plus the
 * Desks content — not the thirty-six-row catalog the operator called unusable.
 * The shell around it is unchanged: this is the menu being fixed, not the
 * workstation being re-shelled.
 *
 * Engine Room (U-3 §2) additionally nests the per-target and per-analyst
 * instance groups inside its own collapsible body (see the `group.id ===
 * 'operations'` branch below) rather than as separate top-level sections: all
 * 14 Operations rows PLUS the raw Targets/Analysts instance groups fold
 * behind ONE collapsed row, so nothing is lost but the daily-driver catalog
 * stays short. The instance groups render the runtime registry's rows UNION
 * the synthesized bound-panel set built from descriptor heads (useRegistry +
 * panel-registry/synthesize.ts) — the live `ui_panel_registrations` surface is
 * empty, so without synthesis the bound panels (Target Map/Timeline/…,
 * Analyst Runs/…) were unreachable here.
 *
 * This REPLACES the five stacked nav systems the prior sidebar carried (search +
 * 3 workspace-preset buttons + a "More layouts" dropdown + two Investigate
 * builder dropdowns + a two-level Intelligence/Operations tree). The three
 * presets collapse into the Layouts menu; the Investigate builders become
 * palette verbs ("Investigate · <target>" / "· <analyst>" in ⌘K).
 *
 * Clicking a panel row opens/focuses it in the Dockview shell via `onOpen`.
 */

import { useMemo, useState } from 'react'
import { ChevronRight, Search, LayoutTemplate, type LucideIcon } from 'lucide-react'
import * as LucideIcons from 'lucide-react'
import type { PanelRegistration, PanelKind } from '@/types'
import {
  PANEL_ID_TO_KIND,
  PANEL_REGISTRY,
  SINGLETON_PANELS,
} from '@/panel-registry/registry'
import { buildNavGroups } from '@/panel-registry/navGroups'
import { extractScope } from '@/panel-registry/loader'
import { LAYOUT_PRESETS } from '@/lib/layoutPresets'
import { cn } from '@/lib/cn'
import { countryNameForTargetId, thematicDeskName, humanizeId } from '@/lib/deskNames'
import { relativeTime } from '@/lib/findingsViews'
import type { ConfidenceLevel } from '@/lib/verdictModel'
import { CONFIDENCE_FILL, useCountryVerdicts, type CountryVerdict } from '@/v4/world/countryVerdicts'
import { useSupplyChainDesks, type SupplyChainDesk } from '@/v4/world/supplyChainDesks'
import { selectRow } from '@/state/selection'

const COLLAPSE_KEY = 'legba_nav_collapsed'

/**
 * THE CATALOG FOLD (UI_HOLISTIC_DESIGN_2026-08-24 §1.2/§3.4, operator's call).
 *
 * Every verb group starts COLLAPSED, so the default menu is five rows — one
 * per verb — plus the Desks section, which is the only genuinely content-shaped
 * thing in the sidebar and stays open. Before this, three of the five groups
 * rendered expanded and the sidebar opened as thirty-six panel rows: a table of
 * contents for the codebase, presented as navigation. The rows are all still
 * there, one click away, with a count on the header so a folded row says how
 * much is behind it.
 *
 * Targets/Analysts (~124/~64 records, nested INSIDE Engine Room per U-3 §2)
 * stay folded for the same reason they always did. The Desks group (U-2) is
 * deliberately absent from this list — it is the keystone entry point and must
 * be open on a cold boot.
 */
const DEFAULT_COLLAPSED = [
  'awareness',
  'investigation',
  'analysis',
  'products',
  'operations',
  'targets',
  'analysts',
]

/** Short label for a country desk's confidence band chip (mirrors the Wall's
 *  choropleth legend wording). */
const CONFIDENCE_LABEL: Record<ConfidenceLevel, string> = {
  high: 'High',
  moderate: 'Moderate',
  low: 'Low',
  unassessed: 'Unverified',
}

/** Resolve a registry `iconName` to a lucide component (fallback: none). */
function iconFor(name?: string): LucideIcon | null {
  if (!name) return null
  const map = LucideIcons as unknown as Record<string, LucideIcon>
  return map[name] ?? null
}

/** Load the set of collapsed group ids from localStorage (tolerant of junk). */
function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    if (raw == null) return new Set(DEFAULT_COLLAPSED)
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? new Set(arr.filter((x) => typeof x === 'string')) : new Set()
  } catch {
    return new Set(DEFAULT_COLLAPSED)
  }
}

function persistCollapsed(ids: Set<string>) {
  try {
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...ids]))
  } catch {
    // localStorage unavailable (private mode / quota) — collapse state is a
    // non-critical convenience; ignore.
  }
}

export interface SidebarProps {
  registrations: PanelRegistration[]
  onOpen: (kind: PanelKind, registration: PanelRegistration | null) => void
  /** Apply a named layout preset by id (see layoutPresets.ts). */
  onApplyPreset: (presetId: string) => void
  /** Open the command palette (the primary record/panel/workspace jump). */
  onOpenPalette: () => void
  /** Persist the live Dockview layout for the current mode. */
  onSaveLayout: () => void
  /** Restore the previously-saved layout for the current mode. */
  onRestoreLayout: () => void
  /** Whether a saved layout exists to restore. */
  canRestoreLayout: boolean
}

interface TargetGroup {
  target_id: string
  rows: PanelRegistration[]
}

interface AnalystGroup {
  analyst_id: string
  rows: PanelRegistration[]
}

export function Sidebar({
  registrations,
  onOpen,
  onApplyPreset,
  onOpenPalette,
  onSaveLayout,
  onRestoreLayout,
  canRestoreLayout,
}: SidebarProps) {
  // The five-group singleton tree — derived from the panel-kind taxonomy so new
  // singleton panels auto-slot (see navGroups.ts). Hidden panels are already
  // filtered out of SINGLETON_PANELS.
  const navGroups = useMemo(() => buildNavGroups(SINGLETON_PANELS), [])

  const [collapsed, setCollapsed] = useState<Set<string>>(() => loadCollapsed())
  const toggleGroup = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      persistCollapsed(next)
      return next
    })
  }

  const grouped = useMemo(() => {
    const targets: Record<string, TargetGroup> = {}
    const analysts: Record<string, AnalystGroup> = {}
    for (const reg of registrations) {
      if (reg.retired) continue
      const scope = extractScope(reg)
      const kind = PANEL_ID_TO_KIND[reg.panel_id]
      if (!kind) continue
      const cat = PANEL_REGISTRY[kind].definition.category
      if (cat === 'target' && scope.target_id) {
        ;(targets[scope.target_id] ??= { target_id: scope.target_id, rows: [] }).rows.push(reg)
      } else if (cat === 'analyst' && scope.analyst_id) {
        ;(analysts[scope.analyst_id] ??= { analyst_id: scope.analyst_id, rows: [] }).rows.push(reg)
      }
    }
    return {
      targets: Object.values(targets).sort((a, b) => a.target_id.localeCompare(b.target_id)),
      analysts: Object.values(analysts).sort((a, b) => a.analyst_id.localeCompare(b.analyst_id)),
    }
  }, [registrations])

  return (
    <aside className="w-60 flex-shrink-0 bg-surf-base text-ink-1 border-r border-line overflow-y-auto">
      <div className="px-3 py-2 border-b border-line">
        <h1 className="text-heading font-bold tracking-tight">Legba</h1>
        <div className="text-label text-ink-3">intelligence workstation</div>
      </div>

      {/* Search everything — the primary record/panel/workspace jump (⌘K). */}
      <div className="px-3 py-2 border-b border-line">
        <button
          type="button"
          data-testid="open-palette"
          onClick={onOpenPalette}
          className="w-full flex items-center gap-2 rounded border border-line bg-surf-2 px-2 py-1.5 text-body text-ink-2 hover:bg-surf-3 hover:text-ink-1"
        >
          <Search size={13} className="shrink-0 text-ink-3" />
          <span className="flex-1 text-left">Search everything…</span>
          <kbd className="shrink-0 rounded bg-surf-base px-1 text-label text-ink-3">⌘K</kbd>
        </button>
      </div>

      {/* Layouts — named save/restore workspaces (the 3 presets collapse here). */}
      <LayoutsMenu
        onApplyPreset={onApplyPreset}
        onSaveLayout={onSaveLayout}
        onRestoreLayout={onRestoreLayout}
        canRestoreLayout={canRestoreLayout}
      />

      {/* Desks (U-2) — the keystone entry point, human country names first.
          Lives ABOVE the five verb-grouped sections and stays open on a cold
          boot (see DEFAULT_COLLAPSED above). */}
      <DesksSection collapsed={collapsed.has('desks')} onToggle={() => toggleGroup('desks')} />

      {/* The one grouped tree — five verb-grouped sections. Engine Room (id:
          operations — U-3 §2) additionally nests the raw Targets/Analysts
          instance groups inside its own collapsible body, after its 14
          singleton rows, instead of as separate top-level sections: all the
          plumbing — catalog panels AND per-record instances — folds behind
          one collapsed row. */}
      {navGroups.map((group) => (
        <CollapsibleSection
          key={group.id}
          id={group.id}
          // The count is what makes a FOLDED row honest: five verb rows that
          // each say how many surfaces are behind them, instead of a wall of
          // thirty-six rows (design §3.4 — the catalog is secondary).
          title={`${group.label} (${group.kinds.length})`}
          collapsed={collapsed.has(group.id)}
          onToggle={() => toggleGroup(group.id)}
        >
          <ul className="space-y-px">
            {group.kinds.map((kind) => {
              const def = PANEL_REGISTRY[kind].definition
              return (
                <SidebarRow
                  key={kind}
                  label={def.defaultTitle}
                  Icon={iconFor(def.iconName)}
                  onClick={() => onOpen(kind, null)}
                />
              )
            })}
          </ul>

          {group.id === 'operations' && (
            <>
              {/* Per-target groups — instance-scoped analysis panels from the
                  registry (real rows) + the synthesized bound-panel set
                  (useRegistry/synthesize). At live scale (~124 targets) each
                  record collapses to one row and a filter box narrows by id. */}
              {grouped.targets.length > 0 && (
                <InstanceSection
                  id="targets"
                  title="Targets"
                  groups={grouped.targets.map((g) => ({ record_id: g.target_id, rows: g.rows }))}
                  collapsed={collapsed.has('targets')}
                  onToggle={() => toggleGroup('targets')}
                  onOpen={onOpen}
                />
              )}

              {/* Per-analyst groups. */}
              {grouped.analysts.length > 0 && (
                <InstanceSection
                  id="analysts"
                  title="Analysts"
                  groups={grouped.analysts.map((g) => ({ record_id: g.analyst_id, rows: g.rows }))}
                  collapsed={collapsed.has('analysts')}
                  onToggle={() => toggleGroup('analysts')}
                  onOpen={onOpen}
                />
              )}
            </>
          )}
        </CollapsibleSection>
      ))}
    </aside>
  )
}

/** One resolved desk row: the verdict data plus its human country name. */
interface DeskRow {
  targetId: string
  iso2: string
  name: string
  confidence: ConfidenceLevel
}

function toDeskRow(v: CountryVerdict): DeskRow {
  return {
    targetId: v.targetId,
    iso2: v.iso2,
    name: countryNameForTargetId(v.targetId) ?? v.iso2,
    confidence: v.verdict.confidence,
  }
}

/** One resolved supply-chain desk row: no composition tier yet, so no
 *  confidence band — only a name and (when available) a recency stamp. */
interface SupplyChainDeskRow {
  targetId: string
  name: string
  latestFindingAt: string | null
}

function toSupplyChainRow(d: SupplyChainDesk): SupplyChainDeskRow {
  return {
    targetId: d.targetId,
    // A registered lane/flow gets its curated name; an unrecognized future id
    // still degrades honestly via humanizeId's generic de-prefix fallback.
    name: thematicDeskName(d.targetId) ?? humanizeId(d.targetId),
    latestFindingAt: d.latestFindingAt,
  }
}

/**
 * Desks (U-2) — country desks as first-class, human nav rows: the human
 * country NAME plus its scorecard band chip, sourced from the SAME
 * `useCountryVerdicts` hook the Wall's band grid and the World map's
 * choropleth read (so a desk's band never disagrees across surfaces).
 * Clicking a row fires the SAME keystone action a Wall band-grid chip does —
 * `selectRow('target', …)` — so the Inspector (and every other subscriber:
 * map, feed, timeline, Why graph) follows the same selection, no new flow.
 *
 * Supply-chain follow-up: a "Supply chain" subsection nests the thematic
 * `lane_*`/`flow_*` desks (`useSupplyChainDesks` — active + tagged
 * `supply_chain` registry targets, see that module) under the same group,
 * BELOW the country list, rather than a new top-level section (the U-2 tree
 * stays deliberately compact). These desks have no `country_composition`
 * tier yet, so they render name-only (+ an optional recency stamp) — never a
 * fabricated confidence chip. Clicking one fires the identical
 * `selectRow('target', …)` a country desk does.
 */
function DesksSection({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const { verdicts, isLoading: countryLoading } = useCountryVerdicts()
  const { desks: supplyChainRaw, isLoading: supplyChainLoading } = useSupplyChainDesks()

  const desks = useMemo(
    () => [...verdicts.values()].map(toDeskRow).sort((a, b) => a.name.localeCompare(b.name)),
    [verdicts],
  )
  const supplyChainDesks = useMemo(
    () => supplyChainRaw.map(toSupplyChainRow).sort((a, b) => a.name.localeCompare(b.name)),
    [supplyChainRaw],
  )

  const isLoading = countryLoading || supplyChainLoading
  const totalCount = desks.length + supplyChainDesks.length

  return (
    <CollapsibleSection
      id="desks"
      title={`Desks (${totalCount})`}
      collapsed={collapsed}
      onToggle={onToggle}
    >
      {isLoading && totalCount === 0 && (
        <div className="px-3 py-1 text-label text-ink-3">loading desks…</div>
      )}
      {!isLoading && totalCount === 0 && (
        <div className="px-3 py-1 text-label text-ink-3">no assessed desks yet</div>
      )}
      {desks.length > 0 && (
        <ul className="space-y-px">
          {desks.map((d) => (
            <li key={d.targetId}>
              <button
                type="button"
                data-testid={`nav-desk-${d.iso2}`}
                onClick={() => selectRow('target', d.targetId, d.name, { origin: 'desks' })}
                title={`${d.name} — ${CONFIDENCE_LABEL[d.confidence]} confidence`}
                className="w-full flex items-center gap-2 text-left px-3 row-density text-body hover:bg-surf-2 text-ink-2"
              >
                <span
                  className="h-2 w-2 shrink-0 rounded-sm"
                  style={{ backgroundColor: CONFIDENCE_FILL[d.confidence] }}
                  aria-hidden
                />
                <span className="flex-1 truncate">{d.name}</span>
                <span className="shrink-0 text-label text-ink-3">
                  {CONFIDENCE_LABEL[d.confidence]}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {supplyChainDesks.length > 0 && (
        <>
          <div
            className="mt-1 px-3 py-1 text-label uppercase tracking-wider text-ink-3"
            data-testid="nav-desks-supply-chain-header"
          >
            Supply chain
          </div>
          <ul className="space-y-px">
            {supplyChainDesks.map((d) => (
              <li key={d.targetId}>
                <button
                  type="button"
                  data-testid={`nav-desk-${d.targetId}`}
                  onClick={() => selectRow('target', d.targetId, d.name, { origin: 'desks' })}
                  title={d.name}
                  className="w-full flex items-center gap-2 text-left px-3 row-density text-body hover:bg-surf-2 text-ink-2"
                >
                  <span className="flex-1 truncate">{d.name}</span>
                  {d.latestFindingAt && (
                    <span className="shrink-0 text-label text-ink-3">
                      {relativeTime(d.latestFindingAt)}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </CollapsibleSection>
  )
}

/**
 * Layouts menu — the single home for named workspaces. Applies a curated preset,
 * saves the live drag layout, or restores the saved one. Collapses the prior
 * three preset buttons + "More layouts" dropdown + Save/Restore into one block.
 */
function LayoutsMenu({
  onApplyPreset,
  onSaveLayout,
  onRestoreLayout,
  canRestoreLayout,
}: {
  onApplyPreset: (presetId: string) => void
  onSaveLayout: () => void
  onRestoreLayout: () => void
  canRestoreLayout: boolean
}) {
  return (
    <div className="px-3 py-2 border-b border-line">
      <div className="mb-1 flex items-center gap-1.5 text-label uppercase tracking-wider text-ink-2">
        <LayoutTemplate size={12} className="text-ink-3" />
        <span>Layouts</span>
      </div>
      <select
        data-testid="layout-preset-select"
        defaultValue=""
        aria-label="Apply a named workspace layout"
        className="w-full bg-surf-2 text-body text-ink-2 rounded px-2 py-1 border border-line outline-none focus:border-line-strong"
        onChange={(e) => {
          const id = e.target.value
          if (!id) return
          onApplyPreset(id)
          e.target.value = '' // reset so re-selecting the same preset re-fires
        }}
      >
        <option value="" disabled>
          Open a workspace…
        </option>
        {LAYOUT_PRESETS.map((preset) => (
          <option key={preset.id} value={preset.id} title={preset.description}>
            {preset.label}
          </option>
        ))}
      </select>
      <div className="mt-2 flex gap-1">
        <button
          type="button"
          data-testid="layout-save"
          onClick={onSaveLayout}
          title="Save the current layout for this mode"
          className="flex-1 bg-surf-2 text-label text-ink-2 rounded px-2 py-1 border border-line hover:bg-surf-3"
        >
          Save
        </button>
        <button
          type="button"
          data-testid="layout-restore"
          onClick={onRestoreLayout}
          disabled={!canRestoreLayout}
          title={canRestoreLayout ? 'Restore the saved layout' : 'No saved layout yet'}
          className={cn(
            'flex-1 text-label rounded px-2 py-1 border border-line',
            canRestoreLayout
              ? 'bg-surf-2 text-ink-2 hover:bg-surf-3'
              : 'bg-surf-1 text-ink-3 cursor-not-allowed',
          )}
        >
          Restore
        </button>
      </div>
    </div>
  )
}

/** One record's registration rows inside an InstanceSection. */
interface InstanceGroup {
  record_id: string
  rows: PanelRegistration[]
}

/**
 * A collapsible per-record section (Targets / Analysts). Built to stay usable
 * at live registry scale (~124 targets): the section itself collapses (state
 * persisted with the nav groups), each record collapses to a single row by
 * default, and a filter box narrows records by id once the list is long.
 */
function InstanceSection({
  id,
  title,
  groups,
  collapsed,
  onToggle,
  onOpen,
}: {
  id: string
  title: string
  groups: InstanceGroup[]
  collapsed: boolean
  onToggle: () => void
  onOpen: SidebarProps['onOpen']
}) {
  const [filter, setFilter] = useState('')
  // Records expanded to show their panel rows — collapsed by default so the
  // section reads as one row per record (session-local, not persisted).
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const toggleRecord = (recordId: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(recordId)) next.delete(recordId)
      else next.add(recordId)
      return next
    })
  }

  const q = filter.trim().toLowerCase()
  const visible = q ? groups.filter((g) => g.record_id.toLowerCase().includes(q)) : groups

  return (
    <CollapsibleSection
      id={id}
      title={`${title} (${groups.length})`}
      collapsed={collapsed}
      onToggle={onToggle}
    >
      {groups.length > 8 && (
        <div className="px-3 pb-1">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={`filter ${title.toLowerCase()}…`}
            spellCheck={false}
            data-testid={`nav-filter-${id}`}
            className="w-full rounded border border-line bg-surf-2 px-2 py-1 text-body text-ink-2 placeholder:text-ink-3 outline-none focus:border-line-strong"
          />
        </div>
      )}
      {visible.length === 0 && (
        <div className="px-3 py-1 text-label text-ink-3">no {title.toLowerCase()} match</div>
      )}
      <ul className="space-y-px">
        {visible.map((group) => (
          <RecordGroupRows
            key={group.record_id}
            group={group}
            expanded={expanded.has(group.record_id)}
            onToggleExpand={() => toggleRecord(group.record_id)}
            onOpen={onOpen}
          />
        ))}
      </ul>
    </CollapsibleSection>
  )
}

/**
 * A collapsible nav section — a clickable header with a chevron, and a body
 * that is hidden (not unmounted) when collapsed.
 */
function CollapsibleSection({
  id,
  title,
  collapsed,
  onToggle,
  children,
}: {
  id: string
  title: string
  collapsed: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div className="py-1">
      <button
        type="button"
        data-testid={`nav-group-${id}`}
        aria-expanded={!collapsed}
        onClick={onToggle}
        className="w-full flex items-center gap-1 px-2 py-1 text-label uppercase tracking-wider text-ink-2 hover:text-ink-1"
      >
        <ChevronRight
          size={12}
          className={cn('flex-shrink-0 transition-transform', !collapsed && 'rotate-90')}
        />
        <span className="flex-1 text-left">{title}</span>
      </button>
      {!collapsed && <div className="space-y-px mt-0.5">{children}</div>}
    </div>
  )
}

function SidebarRow({
  label,
  Icon,
  onClick,
  indent,
}: {
  label: string
  Icon?: LucideIcon | null
  onClick: () => void
  indent?: number
}) {
  return (
    <li>
      <button
        onClick={onClick}
        className={cn(
          'w-full flex items-center gap-2 text-left px-3 row-density text-body hover:bg-surf-2 text-ink-2',
          indent === 1 && 'pl-6',
        )}
      >
        {Icon && <Icon size={13} className="shrink-0 text-ink-3" />}
        <span className="flex-1 truncate">{label}</span>
      </button>
    </li>
  )
}

/** One record row — collapsed to a single line; expanded, its panel rows. */
function RecordGroupRows({
  group,
  expanded,
  onToggleExpand,
  onOpen,
}: {
  group: InstanceGroup
  expanded: boolean
  onToggleExpand: () => void
  onOpen: SidebarProps['onOpen']
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onToggleExpand}
        aria-expanded={expanded}
        data-testid={`nav-record-${group.record_id}`}
        className="w-full flex items-center gap-1 px-3 row-density text-body font-medium text-ink-1 hover:bg-surf-2 text-left"
      >
        <ChevronRight
          size={11}
          className={cn('flex-shrink-0 text-ink-3 transition-transform', expanded && 'rotate-90')}
        />
        <span className="flex-1 truncate">{group.record_id}</span>
      </button>
      {expanded && (
        <ul className="space-y-px">
          {group.rows.map((reg) => {
            const kind = PANEL_ID_TO_KIND[reg.panel_id]
            if (!kind) return null
            return (
              <SidebarRow
                key={reg.id}
                indent={1}
                label={PANEL_REGISTRY[kind].definition.defaultTitle}
                onClick={() => onOpen(kind, reg)}
              />
            )
          })}
        </ul>
      )}
    </li>
  )
}
