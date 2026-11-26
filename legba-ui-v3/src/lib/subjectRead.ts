/**
 * subjectRead — what a click on a SUBJECT does (X3/D3; review D §C5).
 *
 * A subject picked from the rail (or ⌘K, the top bar's switcher, a Watch row's
 * subject) used to be a bare `focus`, then a bound copy of its page opened
 * inside whatever tab the reader was on ("Alert log › Ukraine"). The click
 * now compiles to three drive actions, in this order, through the ONE
 * executor:
 *
 *   1. `set_scope {target}` — the subject is THE POINT: the top bar names it,
 *      the regions that follow it narrow to it, Consult asks about it;
 *   2. `focus {target}`     — the selection follows it;
 *   3. `arrange subject`    — its home: the subject tab, whose page follows
 *      the point. A subject has one home and is never opened elsewhere.
 *
 * X4/D4: `lib/commands.ts::railActions.subject` now compiles exactly these
 * three, and this is the one definition (`subjectReadActions` below IS
 * `railActions.subject`) — the rail, ⌘K and any other caller of either name get
 * the same read. The on-screen noun lives in the leaf `lib/subjectNoun.ts`.
 */
import type { WorkstationAction } from '@/lib/applyAction'
import { railActions } from '@/lib/commands'

/**
 * A click on a subject: make it the point, focus it, go to its home. The
 * order is the contract (the point lands first so the page opens on it).
 */
export function subjectReadActions(id: string, label?: string): WorkstationAction[] {
  return railActions.subject(id, label)
}
