/**
 * Workspace bar — the front door (UI_HOLISTIC_DESIGN_2026-08-24 §2.3).
 *
 * Six tabs, always visible, one keystroke each: the app stops asking "which of
 * thirty-six panels do you want?" and asks "why did you open this?". This is
 * Blender's workspace tab row — task-scoped layouts you switch between, with
 * the surfaces a stance doesn't need simply absent — rendered horizontally
 * because six items don't earn a vertical rail's permanent width.
 *
 * PLACEMENT: this strip sits ABOVE THE DOCK, inside the workspace column. The
 * sidebar is deliberately untouched (operator's call: the sidebar + panels
 * layout stays exactly as it is; the fix is the catalog inside it, not the
 * shell around it), so the bar is additive chrome over the dock canvas rather
 * than the design's full-width top strip.
 *
 * Switching never destroys: `App.tsx` serializes the outgoing stance into its
 * own slot before the incoming one is restored/seeded (see lib/workspaces.ts).
 * The bar itself is presentational — it owns no layout state.
 */

import { RotateCcw } from 'lucide-react'
import { cn } from '@/lib/cn'
import { WORKSPACES, type WorkspaceId } from '@/lib/workspaces'

export interface WorkspaceBarProps {
  /** The stance currently mounted in the dock. */
  active: WorkspaceId
  /** Switch stances (saves the outgoing layout, restores/seeds the incoming). */
  onSwitch: (ws: WorkspaceId) => void
  /** Discard this stance's saved layout and re-seed its curated default. */
  onReset: () => void
}

export function WorkspaceBar({ active, onSwitch, onReset }: WorkspaceBarProps) {
  const activeDef = WORKSPACES.find((w) => w.id === active)
  return (
    <nav
      aria-label="Workspaces"
      data-testid="workspace-bar"
      className="flex h-8 shrink-0 items-center gap-1 border-b border-line bg-surf-1 px-2"
    >
      <div role="tablist" aria-label="Workspaces" className="flex items-center gap-0.5">
        {WORKSPACES.map((ws) => {
          const isActive = ws.id === active
          return (
            <button
              key={ws.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              data-testid={`workspace-tab-${ws.id}`}
              title={`${ws.question}  ·  Alt+${ws.index}`}
              onClick={() => onSwitch(ws.id)}
              className={cn(
                'relative rounded-sm px-2.5 py-1 text-label font-medium tracking-wide transition-colors',
                isActive
                  ? 'bg-surf-3 text-ink-1'
                  : 'text-ink-3 hover:bg-surf-2 hover:text-ink-2',
              )}
            >
              {ws.label}
              {isActive && (
                <span
                  aria-hidden
                  className="absolute inset-x-1.5 -bottom-px h-px bg-[var(--accent)]"
                />
              )}
            </button>
          )
        })}
      </div>

      <div className="ml-auto flex items-center gap-2">
        <span className="hidden truncate text-label text-ink-3 md:inline" data-testid="workspace-question">
          {activeDef?.question}
        </span>
        <button
          type="button"
          data-testid="workspace-reset"
          onClick={onReset}
          title="Reset this workspace to its default arrangement (Alt+Shift+R)"
          className="rounded-sm p-1 text-ink-3 hover:bg-surf-2 hover:text-ink-1"
        >
          <RotateCcw size={12} aria-hidden />
          <span className="sr-only">Reset this workspace</span>
        </button>
      </div>
    </nav>
  )
}
