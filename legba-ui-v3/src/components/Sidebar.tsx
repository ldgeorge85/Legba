/**
 * Sidebar — grouped panel tree.
 *
 * Singleton (non-binding) panels are bucketed into collapsible, named
 * sections (Registry / System / Analysis / Stats / Ops / Product / …) by
 * `buildNavGroups`, derived from the panel-kind taxonomy so new panels
 * auto-slot. Per-target and per-analyst panels keep their instance-scoped
 * groups expanded from registry rows.
 *
 * Clicking an item opens the panel in the Dockview shell via the
 * `onOpen` callback.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Search, LayoutGrid, Telescope, Wrench } from 'lucide-react'
import type { PanelRegistration, PanelKind } from '@/types'
import {
  PANEL_ID_TO_KIND,
  PANEL_REGISTRY,
  SINGLETON_PANELS,
} from '@/panel-registry/registry'
import {
  buildNavGroups,
  productForKind,
  PRODUCT_GROUP_DEFS,
} from '@/panel-registry/navGroups'
import { extractScope } from '@/panel-registry/loader'
import { LAYOUT_PRESETS, findPreset } from '@/lib/layoutPresets'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/cn'

const COLLAPSE_KEY = 'legba_nav_collapsed'

/**
 * The curated task workspaces — the PRIMARY way into the app (redesign Move 3b).
 * Each maps to an existing layout preset; the giant flat panel list is demoted
 * behind a single "More panels" disclosure below these. Order = render order.
 */
const WORKSPACE_BUTTONS: ReadonlyArray<{ presetId: string; label: string; icon: typeof LayoutGrid }> = [
  { presetId: 'monitoring', label: 'Monitoring', icon: LayoutGrid },
  { presetId: 'investigation', label: 'Investigation', icon: Telescope },
  { presetId: 'operations', label: 'Operations', icon: Wrench },
]

/** Load the set of collapsed group ids from localStorage (tolerant of junk). */
function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? new Set(arr.filter((x) => typeof x === 'string')) : new Set()
  } catch {
    return new Set()
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
  /** Open the target-scoped analysis grid for a target (investigateLayout.ts). */
  onInvestigateTarget: (targetId: string) => void
  /** Open the analyst-scoped analysis grid for an analyst (investigateLayout.ts). */
  onInvestigateAnalyst: (analystId: string) => void
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
  onInvestigateTarget,
  onInvestigateAnalyst,
  onOpenPalette,
  onSaveLayout,
  onRestoreLayout,
  canRestoreLayout,
}: SidebarProps) {
  // Singleton (non-binding) panels, bucketed into collapsible nav groups, then
  // split into the two TOP-LEVEL product sections (#89): Intelligence (the
  // product the operator reads) leads; Operations (plumbing) is demoted below.
  // Derived from the panel-kind taxonomy so new singleton panels auto-slot
  // (see navGroups.ts).
  const productSections = useMemo(
    () =>
      PRODUCT_GROUP_DEFS.map((pdef) => ({
        ...pdef,
        groups: buildNavGroups(
          SINGLETON_PANELS.filter((k) => productForKind(k) === pdef.id),
        ),
      })).filter((s) => s.groups.length > 0),
    [],
  )

  // Collapsed-group ids — persisted to localStorage so the operator's
  // expand/collapse layout survives reloads.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    const s = loadCollapsed()
    // #89 — on first run (nothing persisted) demote Operations: the plumbing
    // section starts collapsed so the Intelligence product leads the rail.
    if (localStorage.getItem(COLLAPSE_KEY) == null) s.add('product-operations')
    return s
  })
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
    const dashboards: PanelRegistration[] = []
    // Operator + system panels show up in the singleton nav groups above
    // (they're bundle-registered), so we don't need a registered-instance
    // list for them in the sidebar grouping.
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
      } else if (cat === 'dashboard') {
        dashboards.push(reg)
      }
    }
    return {
      targets: Object.values(targets).sort((a, b) => a.target_id.localeCompare(b.target_id)),
      analysts: Object.values(analysts).sort((a, b) => a.analyst_id.localeCompare(b.analyst_id)),
      dashboards,
    }
  }, [registrations])

  // The flat panel list is DEMOTED (redesign Move 3b): the curated workspaces +
  // palette are the primary way in, so the ~37-row nav-group wall is folded
  // behind a single "More panels" disclosure that defaults collapsed. Every
  // panel stays one click away — just not shouted at once.

  return (
    <aside className="w-60 flex-shrink-0 bg-surf-base text-ink-1 border-r border-line overflow-y-auto">
      <div className="px-3 py-2 border-b border-line">
        <h1 className="text-heading font-bold tracking-tight">Legba</h1>
        <div className="text-label text-ink-3">intelligence workstation</div>
      </div>

      {/* Command palette — the primary record/panel/workspace jump (⌘K). */}
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

      {/* Curated task workspaces — the PRIMARY way in (Move 3b). */}
      <div className="px-3 py-2 border-b border-line">
        <div className="text-label uppercase tracking-wider text-ink-2 mb-1">Workspace</div>
        <div className="space-y-1">
          {WORKSPACE_BUTTONS.map((ws) => {
            const preset = findPreset(ws.presetId)
            if (!preset) return null
            const Icon = ws.icon
            return (
              <button
                key={ws.presetId}
                type="button"
                data-testid={`workspace-${ws.presetId}`}
                title={preset.description}
                onClick={() => onApplyPreset(ws.presetId)}
                className="w-full flex items-center gap-2 rounded border border-line bg-surf-2 px-2 py-1.5 text-body text-ink-2 hover:bg-surf-3 hover:text-ink-1"
              >
                <Icon size={13} className="shrink-0 text-ink-3" />
                <span className="flex-1 text-left">{ws.label}</span>
              </button>
            )
          })}
        </div>
        <div className="flex gap-1 mt-2">
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
              'flex-1 text-[11px] rounded px-2 py-1 border border-slate-700/60',
              canRestoreLayout
                ? 'bg-surf-2 text-ink-2 hover:bg-surf-3'
                : 'bg-surf-1 text-ink-3 cursor-not-allowed',
            )}
          >
            Restore
          </button>
        </div>
      </div>

      {/* Secondary preset launcher — keeps non-featured workspaces reachable. */}
      <OtherWorkspacesSelector onApplyPreset={onApplyPreset} />

      {/* Investigate a target — opens the bound analysis grid (Map/Timeline/…). */}
      <InvestigatePicker onInvestigate={onInvestigateTarget} />

      {/* Investigate an analyst — opens the bound analyst grid (Outputs/Runs/…). */}
      <InvestigateAnalystPicker onInvestigate={onInvestigateAnalyst} />

      {/* #89 — panels split into two TOP-LEVEL product sections: Intelligence
          (the product the operator reads) leads; Operations (plumbing) is
          demoted below and starts collapsed. Each section keeps its nav-group
          sub-headers (Monitor / Investigate / Configure / Operate). */}
      {productSections.map((section) => {
        const sid = `product-${section.id}`
        const count = section.groups.reduce((n, g) => n + g.kinds.length, 0)
        return (
          <CollapsibleSection
            key={sid}
            title={`${section.label} · ${count}`}
            count={count}
            collapsed={collapsed.has(sid)}
            onToggle={() => toggleGroup(sid)}
            hideCount
          >
            {section.groups.map((group) => (
              <div key={group.id} className="mt-1">
                <div className="px-3 text-label uppercase tracking-wider text-ink-3">
                  {group.label}
                </div>
                <ul className="space-y-px">
                  {group.kinds.map((kind) => (
                    <SidebarRow
                      key={kind}
                      label={PANEL_REGISTRY[kind].definition.defaultTitle}
                      onClick={() => onOpen(kind, null)}
                    />
                  ))}
                </ul>
              </div>
            ))}
          </CollapsibleSection>
        )
      })}

      {/* Dashboards from registry. */}
      {grouped.dashboards.length > 0 && (
        <Section title="Dashboards (registered)">
          {grouped.dashboards.map((reg) => {
            const kind = PANEL_ID_TO_KIND[reg.panel_id]
            if (!kind) return null
            return (
              <SidebarRow
                key={reg.id}
                label={reg.title}
                onClick={() => onOpen(kind, reg)}
              />
            )
          })}
        </Section>
      )}

      {/* Per-target groups — collapsed list view. */}
      {grouped.targets.length > 0 && (
        <Section title="Targets">
          {grouped.targets.map((group) => (
            <TargetGroupRows key={group.target_id} group={group} onOpen={onOpen} />
          ))}
        </Section>
      )}

      {/* Per-analyst groups. */}
      {grouped.analysts.length > 0 && (
        <Section title="Analysts">
          {grouped.analysts.map((group) => (
            <AnalystGroupRows key={group.analyst_id} group={group} onOpen={onOpen} />
          ))}
        </Section>
      )}
    </aside>
  )
}

/**
 * Secondary preset picker — every layout preset (including ones not featured as
 * a primary workspace button, e.g. Analysis) stays reachable from the sidebar.
 * The featured workspaces live in the prominent switcher above; this is the
 * slim fallback so no curated arrangement becomes unreachable.
 */
function OtherWorkspacesSelector({ onApplyPreset }: { onApplyPreset: (presetId: string) => void }) {
  return (
    <div className="px-3 py-2 border-b border-line">
      <label className="block text-label uppercase tracking-wider text-ink-2 mb-1">
        Other workspaces
      </label>
      <select
        data-testid="layout-preset-select"
        defaultValue=""
        aria-label="Apply layout preset"
        className="w-full bg-surf-2 text-body text-ink-2 rounded px-2 py-1 border border-line outline-none focus:border-line-strong"
        onChange={(e) => {
          const id = e.target.value
          if (!id) return
          onApplyPreset(id)
          // Reset to the placeholder so re-selecting the same preset re-fires.
          e.target.value = ''
        }}
      >
        <option value="" disabled>
          More layouts…
        </option>
        {LAYOUT_PRESETS.map((preset) => (
          <option key={preset.id} value={preset.id} title={preset.description}>
            {preset.label}
          </option>
        ))}
      </select>
    </div>
  )
}

interface TargetOption {
  descriptor_id: string
  state: string
  name?: string
}

/**
 * Target picker that opens the analysis grid for the chosen target. Fetches the
 * active target descriptors directly (independent of whether per-target panel
 * registrations exist) so the binding-scoped Map/Timeline/etc. are reachable in
 * one action from a cold workspace.
 */
function InvestigatePicker({ onInvestigate }: { onInvestigate: (targetId: string) => void }) {
  const { data } = useQuery<TargetOption[]>({
    queryKey: ['investigate-targets'],
    queryFn: () =>
      apiGet<TargetOption[]>('/registry/descriptors?family=target&head_only=true&limit=500'),
    refetchInterval: 300_000,
  })
  // Show every usable (non-retired) target: the country targets ship as
  // `draft` in the registry but still receive fan-out + findings, so an
  // active-only filter hid exactly the targets worth investigating.
  const targets = (data ?? [])
    .filter((r) => r.state !== 'retired')
    .sort((a, b) => a.descriptor_id.localeCompare(b.descriptor_id))
  return (
    <div className="px-3 py-2 border-b border-line">
      <label className="block text-label uppercase tracking-wider text-ink-2 mb-1">
        Investigate target
      </label>
      <select
        data-testid="investigate-target-select"
        defaultValue=""
        aria-label="Investigate a target"
        className="w-full bg-surf-2 text-body text-ink-2 rounded px-2 py-1 border border-line outline-none focus:border-line-strong"
        onChange={(e) => {
          const v = e.target.value
          if (!v) return
          onInvestigate(v)
          // Reset to the placeholder so re-selecting the same target re-fires.
          e.target.value = ''
        }}
      >
        <option value="" disabled>
          {targets.length ? 'Open analysis grid…' : 'No targets'}
        </option>
        {targets.map((t) => (
          <option key={t.descriptor_id} value={t.descriptor_id} title={t.name ?? t.descriptor_id}>
            {t.descriptor_id}
            {t.state !== 'active' ? ` · ${t.state}` : ''}
          </option>
        ))}
      </select>
    </div>
  )
}

/**
 * Analyst picker — the mirror of InvestigatePicker (redesign Move 3b, P-B2).
 * Opens the per-analyst grid (Outputs/Runs/Critiques/Cross-target) for a chosen
 * analyst, surfacing the binding-scoped analyst panels that no preset or boot
 * grid reaches today.
 */
function InvestigateAnalystPicker({ onInvestigate }: { onInvestigate: (analystId: string) => void }) {
  const { data } = useQuery<TargetOption[]>({
    queryKey: ['investigate-analysts'],
    queryFn: () =>
      apiGet<TargetOption[]>('/registry/descriptors?family=analyst&head_only=true&limit=500'),
    refetchInterval: 300_000,
  })
  const analysts = (data ?? [])
    .filter((r) => r.state !== 'retired')
    .sort((a, b) => a.descriptor_id.localeCompare(b.descriptor_id))
  return (
    <div className="px-3 py-2 border-b border-line">
      <label className="block text-label uppercase tracking-wider text-ink-2 mb-1">
        Investigate analyst
      </label>
      <select
        data-testid="investigate-analyst-select"
        defaultValue=""
        aria-label="Investigate an analyst"
        className="w-full bg-surf-2 text-body text-ink-2 rounded px-2 py-1 border border-line outline-none focus:border-line-strong"
        onChange={(e) => {
          const v = e.target.value
          if (!v) return
          onInvestigate(v)
          // Reset to the placeholder so re-selecting the same analyst re-fires.
          e.target.value = ''
        }}
      >
        <option value="" disabled>
          {analysts.length ? 'Open analyst grid…' : 'No analysts'}
        </option>
        {analysts.map((a) => (
          <option key={a.descriptor_id} value={a.descriptor_id} title={a.name ?? a.descriptor_id}>
            {a.descriptor_id}
            {a.state !== 'active' ? ` · ${a.state}` : ''}
          </option>
        ))}
      </select>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="py-2">
      <div className="px-3 text-label uppercase tracking-wider text-ink-2 mb-1">{title}</div>
      <ul className="space-y-px">{children}</ul>
    </div>
  )
}

/**
 * A collapsible nav section — clickable header with a chevron + row count,
 * and a body that is hidden (not unmounted) when collapsed. `hideCount` drops
 * the trailing count (the title already carries it, e.g. "More panels · 32").
 */
function CollapsibleSection({
  title,
  count,
  collapsed,
  onToggle,
  hideCount,
  children,
}: {
  title: string
  count: number
  collapsed: boolean
  onToggle: () => void
  hideCount?: boolean
  children: React.ReactNode
}) {
  return (
    <div className="py-1">
      <button
        type="button"
        data-testid={`nav-group-${title}`}
        aria-expanded={!collapsed}
        onClick={onToggle}
        className="w-full flex items-center gap-1 px-2 py-1 text-label uppercase tracking-wider text-ink-2 hover:text-ink-1"
      >
        <ChevronRight
          size={12}
          className={cn('flex-shrink-0 transition-transform', !collapsed && 'rotate-90')}
        />
        <span className="flex-1 text-left">{title}</span>
        {!hideCount && <span className="text-ink-3 normal-case tracking-normal">{count}</span>}
      </button>
      {!collapsed && <div className="space-y-px mt-0.5">{children}</div>}
    </div>
  )
}

function SidebarRow({ label, onClick, indent }: { label: string; onClick: () => void; indent?: number }) {
  return (
    <li>
      <button
        onClick={onClick}
        className={cn(
          'w-full text-left px-3 row-density text-body hover:bg-surf-2 text-ink-2 truncate',
          indent === 1 && 'pl-6',
        )}
      >
        {label}
      </button>
    </li>
  )
}

function TargetGroupRows({ group, onOpen }: { group: TargetGroup; onOpen: SidebarProps['onOpen'] }) {
  return (
    <li className="py-1">
      <div className="px-3 text-body font-medium text-ink-1">{group.target_id}</div>
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
    </li>
  )
}

function AnalystGroupRows({ group, onOpen }: { group: AnalystGroup; onOpen: SidebarProps['onOpen'] }) {
  return (
    <li className="py-1">
      <div className="px-3 text-body font-medium text-ink-1">{group.analyst_id}</div>
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
    </li>
  )
}
