/**
 * Shared panel chrome — header bar with title + provenance + budget meter.
 *
 * Per L-092 §5 rule 5 (provenance visible at row level) and rule 6
 * (budget meter visible on analyst panels), every panel wraps its body
 * in this chrome so the affordances are consistent.
 */

import type { PanelRegistration } from '@/types'
import { cn } from '@/lib/cn'
import { RotateCw } from 'lucide-react'
import type { ReactNode } from 'react'
import { usePanelTier } from '@/components/PanelTierContext'
import { useDebugMode } from '@/lib/debugMode'

export interface PanelChromeProps {
  registration: PanelRegistration
  /** Optional override title (default = registration.title). */
  title?: string
  /** Optional right-aligned actions (refresh button, mode toggle, etc.). */
  actions?: ReactNode
  /** Optional small subtitle below the title (e.g. scope/analyst name). */
  subtitle?: string
  children: ReactNode
  /** Per L-092 rule 6 — budget meter for analyst-bound panels. */
  budget?: { used: number; ceiling: number; unit: string }
  /**
   * Optional refresh hook — when present, chrome renders a small refresh
   * button next to `actions`. Most data-fetching panels want this; pass
   * the react-query refetch fn or any zero-arg callable.
   */
  onRefresh?: () => void | Promise<unknown>
  /**
   * Release tier of the panel kind (from its registry definition). When
   * `'preview'` the chrome shows a small "Preview" badge next to the title so
   * operators can tell a guarded-preview surface from a live product surface.
   * `'live'` (or omitted) renders no badge.
   */
  tier?: 'preview' | 'live'
}

export function PanelChrome({
  registration,
  title,
  subtitle,
  actions,
  children,
  budget,
  onRefresh,
  tier,
}: PanelChromeProps) {
  const headerTitle = title ?? registration.title
  // Tier comes from the panel kind's registry definition via context; an
  // explicit `tier=` prop overrides it (rare).
  const ctxTier = usePanelTier()
  const effectiveTier = tier ?? ctxTier
  // The `from <descriptor>@<version>` provenance stamp is developer plumbing —
  // a bare "(singleton)@00000000" for a singleton — so hide it unless debug
  // chrome is on (item 5).
  const debug = useDebugMode()

  return (
    <div className="flex flex-col h-full w-full bg-surf-2 text-ink-1">
      <header className="flex items-center justify-between gap-2 px-density py-2 border-b border-line bg-surf-3">
        <div className="min-w-0">
          <h2 className="text-heading font-semibold truncate flex items-center gap-2">
            <span className="truncate">{headerTitle}</span>
            {effectiveTier === 'preview' && <PreviewBadge />}
          </h2>
          <div className="text-label text-ink-2 truncate flex items-center gap-2">
            {subtitle && <span>{subtitle}</span>}
            {debug && (
              <span className="text-ink-3">
                from <code className="font-mono">{registration.descriptor_id}</code>
                @<code className="font-mono">{registration.descriptor_version.slice(0, 8)}</code>
              </span>
            )}
            {budget && <BudgetPill budget={budget} />}
          </div>
        </div>
        <div className="flex items-center gap-1">
          {onRefresh && (
            <button
              onClick={() => {
                void onRefresh()
              }}
              className="inline-flex items-center justify-center text-ink-2 hover:text-ink-1 p-1 rounded border border-line hover:border-line-strong"
              title="Refresh"
              aria-label="Refresh panel"
              data-testid="panel-refresh"
            >
              <RotateCw className="h-3.5 w-3.5" aria-hidden />
            </button>
          )}
          {actions}
        </div>
      </header>
      <div className="flex-1 overflow-auto pad-density">{children}</div>
    </div>
  )
}

function PreviewBadge() {
  return (
    <span
      className="inline-flex items-center shrink-0 px-1.5 py-0.5 rounded text-label font-medium uppercase tracking-wide bg-surf-1 text-accent-warning border border-line"
      title="Preview surface — guarded preview or honest pending-state route; registered and usable but not part of the everyday product surface (see docs/UI.md)."
      data-testid="panel-tier-preview"
    >
      Preview
    </span>
  )
}

function BudgetPill({ budget }: { budget: { used: number; ceiling: number; unit: string } }) {
  const pct = budget.ceiling > 0 ? (budget.used / budget.ceiling) * 100 : 0
  const color =
    pct >= 90 ? 'bg-accent-critical' : pct >= 70 ? 'bg-accent-warning' : 'bg-accent-ok'
  return (
    <span className={cn('inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-label', 'bg-surf-1')}>
      <span className={cn('w-1.5 h-1.5 rounded-full', color)} />
      {budget.used.toFixed(2)} / {budget.ceiling.toFixed(2)} {budget.unit}
    </span>
  )
}

export function UnboundPanelPlaceholder({ panelId, descriptorId }: { panelId: string; descriptorId: string }) {
  return (
    <div className="flex items-center justify-center h-full w-full bg-surf-2 text-ink-2">
      <div className="text-center max-w-md p-6">
        <h3 className="text-heading font-semibold mb-2">Unbound panel</h3>
        <p className="text-body">
          Descriptor <code className="font-mono">{descriptorId}</code> declares a panel id{' '}
          <code className="font-mono">panels.{panelId}</code> that isn't shipped in this UI bundle. The
          descriptor needs a UI redeploy, or the panel module is missing.
        </p>
      </div>
    </div>
  )
}
