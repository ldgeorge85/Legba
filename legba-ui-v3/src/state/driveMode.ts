/**
 * driveMode — Consult's "Drive the view" mode (U-2 shell; the owner: "made
 * modes in the Consult … If toggled on, yeah, do it with an undo. if not,
 * well, not, or maybe offer the button then").
 *
 *   ON  — a chat turn is sent as a case: the view actions it presents (open,
 *         focus, scope, window, map) run at once through the ONE executor
 *         (`lib/applyAction.ts`, driver `case`), the case rail lists them, and
 *         one Undo puts the view back (the player's snapshot, taken before
 *         step 1). STOP halts a sequence mid-way.
 *   OFF — a chat turn is sent exactly as a plain turn: Consult answers and
 *         moves nothing. A recorded case reopened from a link, the rail or ⌘K
 *         is offered step by step — each step a button — never auto-run.
 *
 * A change to the system (sources, subjects, units, facts) is never a view
 * action and always waits for the reader's explicit say, in either mode.
 *
 * The mode is a per-viewer preference, kept in this browser only; a browser
 * that refuses storage keeps it for the session. It starts OFF: turning it on
 * changes what a turn is sent as (a case turn carries the case round cap), so
 * it is the reader's choice, never a default that spends on their behalf.
 */
import { create } from 'zustand'

const KEY = 'legba_consult_drive'

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === 'on'
  } catch {
    return false
  }
}

interface DriveModeState {
  on: boolean
  setOn: (on: boolean) => void
}

export const useDriveMode = create<DriveModeState>((set) => ({
  on: read(),
  setOn: (on) => {
    set({ on })
    try {
      localStorage.setItem(KEY, on ? 'on' : 'off')
    } catch {
      // per-viewer convenience only
    }
  },
}))

/** Test helper: back to the stored (or default) value. */
export function resetDriveMode(): void {
  useDriveMode.setState({ on: read() })
}
