/**
 * Timeline (`system.timeline`) — U-3 merge set 1.
 *
 * Two panels were both literally named "Timeline" (the U-wave critique's
 * headline example): the v4 event-lanes scatter (`v4.timeline`) and the
 * validity-window ranged-item view (`system.timeline`). This wrapper folds
 * them into one sidebar row with a mode switch — Events / Validity.
 *
 * Both child components render UNMODIFIED. U-1 is fixing a blank-render bug
 * inside `panels/system/Timeline.tsx` (the Validity mode) this same wave —
 * this wrapper only adds the switch above it and never touches its internals,
 * per COHERENCE_WAVES_PLAN_2026-07-28 §U-3's coordination note. Both mount
 * inside `PanelEmbedProvider` so `ValidityTimeline`'s own (otherwise-
 * standalone) `PanelChrome` header/border stays suppressed — this wrapper's
 * header is the ONLY chrome that renders (the "double chrome" fix;
 * `EventLanesTimeline` doesn't use `PanelChrome` so is unaffected either way).
 *
 * `v4.timeline` stays registered (hidden from the sidebar) pointing at the
 * SAME untouched event-lanes component — see panel-registry/registry.ts
 * HIDDEN_KINDS — so a saved layout referencing the old id keeps resolving
 * exactly as before.
 */
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import EventLanesTimeline from '@/panels/v4/TimelinePanel'
import ValidityTimeline from '@/panels/system/Timeline'
import { pinForSelection, TIMELINE_PIN_KINDS, usePinScope } from '@/lib/consultContext'
import { useSelection } from '@/state/selection'
import { useMergedTab } from './useMergedTab'

type Mode = 'events' | 'validity'

export const TIMELINE_TABS: readonly PanelTabDef[] = [
  { id: 'events', label: 'Events' },
  { id: 'validity', label: 'Validity' },
]
const TABS = TIMELINE_TABS

export default function TimelineMerged(props: PanelProps) {
  // `v4.timeline` retired onto this panel's "events" mode (aliases.ts). UI-B6:
  // through `useMergedTab`, so an `open_panel {tab}` (a case's, or a retired
  // id's alias) switches an ALREADY-OPEN Timeline too, not only a fresh one.
  const [mode, setMode] = useMergedTab<Mode>('system.timeline', props.initialTab, TABS, 'events')
  // THE PIN HOOK (UI-B6): the record the Timeline holds selected — a mark it
  // places in time, or the subject the validity view reads — is what the
  // reader is on. Any other selection pins nothing from here.
  const selection = useSelection((s) => s.selection)
  usePinScope(pinForSelection(selection, TIMELINE_PIN_KINDS), 'timeline')
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Timeline</span>
        <PanelTabStrip
          tabs={TABS}
          active={mode}
          onChange={(id) => setMode(id as Mode)}
          ariaLabel="Timeline mode"
          testIdPrefix="timeline-mode"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {mode === 'events' ? <EventLanesTimeline /> : <ValidityTimeline {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
