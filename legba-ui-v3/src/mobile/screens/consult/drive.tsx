/**
 * Consult driving the phone (M-3g; the owner's rule on the desktop: "made
 * modes in the Consult … If toggled on, yeah, do it with an undo. if not,
 * well, not, or maybe offer the button then" — `state/driveMode.ts`).
 *
 *   ON   a turn is sent as a case (the desktop's `present_case`); the view
 *        actions it presents — `panel_action` above all ("show me X") — run
 *        at once through the ONE executor (`lib/applyAction`), which routes a
 *        panel action through `applyPanelAction` to the PHONE's frame port, so
 *        "show me the map" navigates to the Map screen. One Undo (the case
 *        player's snapshot, taken before step 1: the phone's screen) puts the
 *        phone back; it is a toast on whatever screen the case left you on.
 *   OFF  the turn is a plain turn: it moves nothing. An answer that carries a
 *        recorded case offers a button that runs it, with the same Undo.
 *
 * Anything the phone does not have (an operator page, a desktop arrangement,
 * a preset, a mission, a view with no phone screen) is REFUSED by the ports
 * and said here in plain words — never dropped silently.
 */
import { contractOf } from '@/panel-registry/contractOf'
import { casePlayer, isCasePlaying, useCasePlayer } from '@/state/casePlayer'
import type { CaseRecord, CaseStep } from '@/lib/caseModel'
import { NOT_ON_THIS_DEVICE } from '@/mobile/shell/phoneShellPort'
import { usePhoneScreen } from '@/mobile/shell/phoneState'
import { MOBILE_PANEL_ID } from '@/mobile/mobileConsult'
import { useState } from 'react'

/** Steps that do not move anything (the rail's own lines). */
const STILL = new Set(['narrate', 'done'])

/** How many of a case's steps moved the phone. */
export function movedSteps(steps: readonly CaseStep[]): number {
  return steps.filter((s) => s.state === 'executed' && s.action && !STILL.has(s.action.action)).length
}

/** The steps the phone could not do. */
export function refusedSteps(steps: readonly CaseStep[]): CaseStep[] {
  return steps.filter((s) => s.state === 'refused' || s.state === 'skipped' || s.state === 'unreadable')
}

/** One refused step, in plain words (no view ids, no reason codes). */
export function refusalWords(step: CaseStep): string {
  const reason = step.reason ?? ''
  if (reason.includes(NOT_ON_THIS_DEVICE)) return 'Arranging views, presets and missions is on the desktop only.'
  const a = step.action
  if (a && a.action === 'panel_action') {
    const view = typeof a.args.view === 'string' ? a.args.view : ''
    const c = view ? contractOf(view) : undefined
    if (c && (c.side === 'operator' || /^(not opened|not bound|no frame)/.test(reason))) return `“${c.name}” is on the desktop only.`
    if (!c) return 'The answer asked for a view this app does not have.'
  }
  if (a && a.action === 'open_panel') return 'That view is on the desktop only.'
  if (step.state === 'unreadable') return 'One step could not be read, so nothing was done for it.'
  return 'One step could not be shown on the phone.'
}

/** Run a recorded case now, through the same executor (drive mode off: the offered button). */
export function runRecordedCase(rec: CaseRecord): void {
  const cp = casePlayer()
  cp.replay(rec, MOBILE_PANEL_ID)
  // every step, in order; the player stops at a hole or after the last
  for (let i = 0; i < 64 && casePlayer().stepReplay(); i++) {
    // stepReplay ran one step
  }
}

/** The case strip on the Consult screen: what is moving, what was refused, Stop and Undo. */
export function DriveStrip() {
  const player = useCasePlayer((s) => (s.panelId === MOBILE_PANEL_ID ? s.player : null))
  const snapshot = useCasePlayer((s) => (s.panelId === MOBILE_PANEL_ID ? s.snapshot : null))
  if (!player) return null
  const playing = isCasePlaying(player)
  const moved = movedSteps(player.steps)
  const refused = refusedSteps(player.steps)
  if (!playing && moved === 0 && refused.length === 0) return null
  return (
    <div className="ph-drive" role="status" data-testid="consult-drive-strip" data-state={playing ? 'moving' : 'done'}>
      <p className="ph-drive-line">
        {playing
          ? 'Opening what the answer points to…'
          : moved > 0 && !snapshot
            ? 'You went back to where you were.'
            : moved > 0
            ? `The answer opened ${moved === 1 ? 'a screen' : `${moved} screens`} for you.`
            : 'The answer pointed to nothing this phone can open.'}
      </p>
      {refused.length > 0 ? (
        <ul className="ph-drive-refused" data-testid="consult-drive-refusals">
          {refused.map((s, i) => (
            <li key={`${s.seq ?? 'r'}-${i}`} data-testid="consult-drive-refusal" data-reason={s.reason ?? ''}>
              {refusalWords(s)}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="ph-drive-actions">
        {playing ? (
          <button type="button" className="ph-btn ph-btn-stop" onClick={() => void casePlayer().stop()} data-testid="consult-drive-stop">
            Stop
          </button>
        ) : null}
        {snapshot && !playing && moved > 0 ? (
          <button type="button" className="ph-btn" onClick={() => casePlayer().restore()} data-testid="consult-drive-undo">
            Undo
          </button>
        ) : null}
        {!playing ? (
          <button type="button" className="ph-btn ph-btn-quiet" onClick={() => casePlayer().dismiss()} data-testid="consult-drive-close">
            Close
          </button>
        ) : null}
      </div>
    </div>
  )
}

/**
 * The UNDO toast, on whatever screen a Consult answer opened (the shell
 * mounts it over every tab but Consult, where the strip says the same).
 */
export function DriveToast() {
  const player = useCasePlayer((s) => (s.panelId === MOBILE_PANEL_ID ? s.player : null))
  const snapshot = useCasePlayer((s) => (s.panelId === MOBILE_PANEL_ID ? s.snapshot : null))
  const screen = usePhoneScreen()
  const [dismissed, setDismissed] = useState<string | null>(null)
  if (!player || !snapshot || screen.tab === 'consult') return null
  if (dismissed === player.requestId) return null
  const moved = movedSteps(player.steps)
  if (moved === 0) return null
  const refused = refusedSteps(player.steps).length
  return (
    <div className="ph-toast" role="status" data-testid="phone-drive-toast">
      <span className="ph-toast-text">
        Consult opened this{refused ? ` · ${refused} step${refused === 1 ? '' : 's'} stayed on the desktop` : ''}
      </span>
      <button type="button" className="ph-btn ph-toast-undo" onClick={() => casePlayer().restore()} data-testid="phone-drive-undo">
        Undo
      </button>
      <button type="button" className="ph-icon-btn" aria-label="Dismiss" onClick={() => setDismissed(player.requestId)} data-testid="phone-drive-toast-close">
        ×
      </button>
    </div>
  )
}
