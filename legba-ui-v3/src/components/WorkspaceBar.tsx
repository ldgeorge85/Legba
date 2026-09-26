/**
 * Workspace bar — the front door (UI_HOLISTIC_DESIGN_2026-08-24 §2.3), and
 * since the wave-P design pass the MISSION chooser as well.
 *
 * Six tabs, always visible, one keystroke each: the app stops asking "which of
 * thirty-six panels do you want?" and asks "why did you open this?". This is
 * Blender's workspace tab row — task-scoped layouts you switch between, with
 * the surfaces a stance doesn't need simply absent — rendered horizontally
 * because six items don't earn a vertical rail's permanent width.
 *
 * THE MISSION BUTTON sits beside the tabs and opens a chooser of the four
 * missions (`lib/missions.ts`), each with its one-line description and a
 * derived "what this mission shows" line. A mission is a stance PLUS the three
 * axes a tab strip never carried — the target scope, the temporal window and
 * the map's layer selection — so choosing one moves the map and the scrubber,
 * not just the tab strip, and leaves the Consult tile pinned to the new scope.
 * The two stances that carry no mission are named in the chooser's footer
 * rather than silently omitted: they are about the machine, and a machine has
 * no desk, no window and no layers to declare.
 *
 * The chooser is a plain popover over the bar — no modal library, no portal, no
 * new dependency. Escape and a click outside dismiss it; the trigger carries
 * `aria-expanded`, and the list is a `menu` of `menuitem`s.
 *
 * PLACEMENT: this strip sits ABOVE THE DOCK, inside the workspace column. The
 * sidebar is deliberately untouched (operator's call: the sidebar + panels
 * layout stays exactly as it is; the fix is the catalog inside it, not the
 * shell around it), so the bar is additive chrome over the dock canvas rather
 * than the design's full-width top strip.
 *
 * Switching never destroys: `App.tsx` serializes the outgoing stance into its
 * own slot before the incoming one is restored/seeded (see lib/workspaces.ts).
 * The bar itself is presentational — it owns no layout state, and it applies no
 * mission; it asks the shell to.
 */

import { useEffect, useRef, useState } from 'react'
import { RotateCcw, Target } from 'lucide-react'
import { cn } from '@/lib/cn'
import { WORKSPACES, type WorkspaceId } from '@/lib/workspaces'
import {
  MISSIONS,
  findMission,
  missionShows,
  type MissionId,
} from '@/lib/missions'

export interface WorkspaceBarProps {
  /** The stance currently mounted in the dock. */
  active: WorkspaceId
  /** Switch stances (saves the outgoing layout, restores/seeds the incoming). */
  onSwitch: (ws: WorkspaceId) => void
  /** Discard this stance's saved layout and re-seed its curated default. */
  onReset: () => void
  /** The mission the reader last chose, or null when they are on a bare stance. */
  activeMission?: MissionId | null
  /**
   * What applying that mission actually did to the wall's scope, in one clause
   * — including "no desk selected, so the scope was left alone". Rendered in
   * place of the stance question, because a mission that could not do what its
   * name promises must say so where the reader is looking.
   */
  missionNote?: string | null
  /** Apply a mission: its stance, its scope, its window, its layers, its tiles. */
  onChooseMission?: (id: MissionId) => void
}

/** The stances that carry no mission — named, never quietly omitted. */
function missionlessStances(): string {
  const missioned = new Set(MISSIONS.map((m) => m.workspace))
  return WORKSPACES.filter((w) => !missioned.has(w.id))
    .map((w) => w.label)
    .join(' and ')
}

export function WorkspaceBar({
  active,
  onSwitch,
  onReset,
  activeMission = null,
  missionNote = null,
  onChooseMission,
}: WorkspaceBarProps) {
  const activeDef = WORKSPACES.find((w) => w.id === active)
  const activeMissionDef = activeMission ? findMission(activeMission) : undefined
  const [chooserOpen, setChooserOpen] = useState(false)
  const chooserRef = useRef<HTMLDivElement | null>(null)

  // Escape closes the chooser wherever focus sits — the popover is chrome over
  // the dock, and a dismissable overlay that only listens inside itself traps a
  // reader who tabbed out of it.
  useEffect(() => {
    if (!chooserOpen) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setChooserOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [chooserOpen])

  const caption = missionNote ?? activeDef?.question

  return (
    <nav
      aria-label="Workspaces"
      data-testid="workspace-bar"
      className="relative flex h-8 shrink-0 items-center gap-1 border-b border-line bg-surf-1 px-2"
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

      {onChooseMission && (
        <div className="relative ml-2" ref={chooserRef}>
          <button
            type="button"
            data-testid="mission-trigger"
            aria-expanded={chooserOpen}
            aria-haspopup="menu"
            onClick={() => setChooserOpen((o) => !o)}
            title="Choose a mission — a stance with its scope, its window and its layers set together"
            className={cn(
              'flex items-center gap-1 rounded-sm border px-2 py-0.5 text-label tracking-wide transition-colors',
              activeMissionDef
                ? 'border-line-strong bg-surf-3 text-ink-1'
                : 'border-line text-ink-3 hover:bg-surf-2 hover:text-ink-2',
            )}
          >
            <Target size={11} aria-hidden />
            <span className="uppercase">Mission</span>
            {activeMissionDef && (
              <span className="text-ink-2" data-testid="mission-active-chip">
                · {activeMissionDef.label}
              </span>
            )}
          </button>

          {chooserOpen && (
            <>
              {/* Click-outside dismissal without a portal or a focus trap. */}
              <button
                type="button"
                aria-hidden
                tabIndex={-1}
                data-testid="mission-scrim"
                onClick={() => setChooserOpen(false)}
                className="fixed inset-0 z-40 cursor-default"
              />
              <div
                role="menu"
                aria-label="Missions"
                data-testid="mission-chooser"
                className="absolute left-0 top-full z-50 mt-1 w-[26rem] max-w-[92vw] overflow-hidden rounded-lg border border-line-strong bg-surf-1 shadow-xl"
              >
                <div className="border-b border-line px-3 py-2">
                  <div className="text-label uppercase tracking-wider text-ink-3">Mission</div>
                  <div className="text-body text-ink-1">Choose what you are here to do</div>
                </div>
                <ul>
                  {MISSIONS.map((m) => {
                    const isActive = m.id === activeMission
                    return (
                      <li key={m.id}>
                        <button
                          type="button"
                          role="menuitem"
                          data-testid={`mission-option-${m.id}`}
                          onClick={() => {
                            setChooserOpen(false)
                            onChooseMission(m.id)
                          }}
                          className={cn(
                            'w-full border-b border-line px-3 py-2 text-left transition-colors last:border-b-0',
                            isActive ? 'bg-surf-3' : 'hover:bg-surf-2',
                          )}
                        >
                          <div className="flex items-baseline gap-2">
                            <span className="text-body-lg font-medium text-ink-1">{m.label}</span>
                            {isActive && (
                              <span className="text-label uppercase tracking-wider text-ink-3">
                                current
                              </span>
                            )}
                          </div>
                          <div className="text-body text-ink-2">{m.description}</div>
                          <div
                            className="mt-0.5 text-label text-ink-3"
                            data-testid={`mission-shows-${m.id}`}
                          >
                            {missionShows(m)}
                          </div>
                        </button>
                      </li>
                    )
                  })}
                </ul>
                <div
                  className="border-t border-line px-3 py-2 text-label leading-relaxed text-ink-3"
                  data-testid="mission-chooser-footnote"
                >
                  {missionlessStances()} carry no mission: they are about the machine, so they
                  declare no desk, no window and no layers. Their tabs above still open them.
                </div>
              </div>
            </>
          )}
        </div>
      )}

      <div className="ml-auto flex min-w-0 items-center gap-2">
        <span
          className="hidden truncate text-label text-ink-3 md:inline"
          data-testid="workspace-question"
          title={caption ?? undefined}
        >
          {caption}
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
