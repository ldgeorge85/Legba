/**
 * Sidebar — the ONE grouped navigation tree (S7-T2).
 *
 * A single grouped tree modeled on the original mission-control UI: a Search
 * (⌘K) launcher, a compact Layouts menu (named save/restore workspaces), then
 * five collapsible verb-grouped sections — Awareness / Investigation / Analysis
 * / Products / Operations — over the singleton panel catalog, followed by the
 * per-target and per-analyst instance groups. The instance groups render the
 * runtime registry's rows UNION the synthesized bound-panel set built from
 * descriptor heads (useRegistry + panel-registry/synthesize.ts) — the live
 * `ui_panel_registrations` surface is empty, so without synthesis the bound
 * panels (Target Map/Timeline/…, Analyst Runs/…) were unreachable here.
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

const COLLAPSE_KEY = 'legba_nav_collapsed'

/** Groups collapsed by default on first run — plumbing folds away, and the
 *  registry-scale Targets/Analysts sections (~124/~64 records live) start
 *  folded so the first screenful stays the five-group tree. */
const DEFAULT_COLLAPSED = ['operations', 'targets', 'analysts']

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

      {/* The one grouped tree — five verb-grouped sections. */}
      {navGroups.map((group) => (
        <CollapsibleSection
          key={group.id}
          id={group.id}
          title={group.label}
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
        </CollapsibleSection>
      ))}

      {/* Per-target groups — instance-scoped analysis panels from the registry
          (real rows) + the synthesized bound-panel set (useRegistry/synthesize).
          At live scale (~124 targets) each record collapses to one row and a
          filter box narrows by id. */}
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
    </aside>
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
